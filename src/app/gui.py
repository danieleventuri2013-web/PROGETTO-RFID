"""GUI Tkinter per il pilotaggio del lettore SIM7200 con 3 antenne.

Permette di:
  - scegliere il trasporto (seriale / TCP) e i parametri di connessione
  - impostare regione e potenze Read/Write per ciascuna antenna
  - eseguire inventory, lettura e scrittura (verifica)
  - visualizzare:
      * grafico lineare: RSSI (segnale) e potenza configurata per ogni antenna
      * grafico 3D del volume (parallelepipedo) con le 3 antenne posizionate
        (2 affiancate sulla base + 1 verticale in altezza) e la posizione
        stimata del tag a partire dall'RSSI

Uso:
    python src/app/gui.py --config src/app/config.yaml
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import queue
import threading
import time
import tkinter as tk
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tkinter import messagebox, ttk

import matplotlib
import numpy as np
import yaml

matplotlib.use("TkAgg")
import sys

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rfid_silion.diagnostics import setup_logging
from rfid_silion.service import (
    AntennaPower,
    EpcGenerationRequest,
    InventoryRequest,
    MemoryBank,
    ReaderSettings,
    ReadRequest,
    RFIDBackend,
    RFIDService,
    RFIDServiceBinding,
    WriteEpcRequest,
    WriteRequest,
    barcode_to_epc_hex,
    discover_serial_ports,
    epc_hex_to_barcode,
)
from rfid_silion.tags import Tag, TagReadAccumulator, TagReadSummary

# --- Geometria volume (cm) ---------------------------------------------------
# SLP1027: 220 mm di lato. 2 antenne affiancate formano la base (44x22 cm),
# 1 antenna verticale dà l'altezza (22 cm). Volume: 44 x 22 x 22 cm.
ANT_SIDE_CM = 22.0
VOL_X = 2 * ANT_SIDE_CM  # 44 (larghezza, 2 antenne affiancate)
VOL_Y = ANT_SIDE_CM  # 22 (profondità)
VOL_Z = ANT_SIDE_CM  # 22 (altezza, antenna verticale)

# Posizioni dei centri antenna per la stima della posizione del tag (cm).
ANT_CENTERS = {
    1: np.array([-ANT_SIDE_CM / 2, VOL_Y / 2, 0.0]),  # base sx, orizzontale
    2: np.array([+ANT_SIDE_CM / 2, VOL_Y / 2, 0.0]),  # base dx, orizzontale
    3: np.array([0.0, 0.0, VOL_Z / 2]),  # parete verticale
}

REGIONS = {
    "EU (865-868 MHz)": 0x08,
}


class RFIDGui:
    def __init__(
        self,
        root: tk.Tk,
        cfg: dict,
        cfg_path: Path,
        *,
        service: RFIDBackend | None = None,
        owns_service: bool | None = None,
        auto_connect: bool | None = None,
        service_factory: Callable[[dict], RFIDBackend] = RFIDService,
    ):
        self.root = root
        self.cfg = cfg
        self.cfg_path = cfg_path
        self._shared_service = service
        self._owns_service = service is None if owns_service is None else bool(owns_service)
        self._service_factory = service_factory
        self._auto_connect = service is not None if auto_connect is None else bool(auto_connect)
        self._binding: RFIDServiceBinding | None = None
        self.service: RFIDBackend | None = None
        self.connected = False
        self.last_tags: list[Tag] = []
        self.ant_rssi: dict[int, int | None] = {1: None, 2: None, 3: None}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rfid")
        self._ui_queue: queue.Queue = queue.Queue()
        self._closing = False
        self._destroyed = False
        self.operation_running = False
        self.inventory_running = False
        self.inventory_stop_event = threading.Event()
        inv_cfg = cfg.get("inventory", {}) or {}
        self.inventory_accumulator = TagReadAccumulator(
            max_unique_epcs=int(inv_cfg.get("max_unique_epcs", 1000))
        )
        self.inventory_cycles = 0
        self.inventory_no_tag_cycles = 0
        self.inventory_error_cycles = 0
        self.inventory_started_at: float | None = None
        self.inventory_presence_mode = "missed_cycles"
        self.inventory_presence_threshold: int | float = 2

        root.title("SIM7200 RFID — controllo 3 antenne")
        root.geometry("1280x900")
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(50, self._drain_ui_queue)

        self._build_connection(root)
        self._build_antenna(root)
        self._build_test(root)
        self._build_barcode_epc(root)
        self._build_inventory_table(root)
        self._build_plot(root)
        self.log("Pronto. Selezionare il trasporto e connettere.")
        if self._shared_service is not None and not self._owns_service:
            self.log(
                "Modalità framework: service condiviso, lifecycle hardware non posseduto dalla GUI."
            )
        elif self._shared_service is not None:
            self.log("Modalita collaudo: GUI proprietaria del lifecycle del service.")
        self._populate_from_cfg()
        self._refresh_serial_port_list(log_result=False)
        self._refresh_controls()
        if self._auto_connect:
            self.root.after(0, self._connect)

    # ------------------------------------------------------------------ #
    # Costruzione UI
    # ------------------------------------------------------------------ #
    def _build_connection(self, parent):
        f = ttk.LabelFrame(parent, text="Connessione")
        f.pack(fill="x", padx=8, pady=4)

        self.transport_var = tk.StringVar(value="serial")
        self.rb_serial = ttk.Radiobutton(
            f,
            text="Seriale (USB/RS232)",
            variable=self.transport_var,
            value="serial",
            command=self._on_transport_change,
        )
        self.rb_serial.grid(row=0, column=0, sticky="w")
        self.rb_tcp = ttk.Radiobutton(
            f,
            text="TCP/IP (SLD1090)",
            variable=self.transport_var,
            value="tcp",
            command=self._on_transport_change,
        )
        self.rb_tcp.grid(row=0, column=1, sticky="w")

        # Seriale
        self.ser_frame = ttk.Frame(f)
        self.ser_frame.grid(row=1, column=0, columnspan=2, sticky="we", padx=4)
        ttk.Label(self.ser_frame, text="Porta:").grid(row=0, column=0)
        self.ser_port = ttk.Combobox(self.ser_frame, width=16)
        self.ser_port.grid(row=0, column=1)
        ttk.Label(self.ser_frame, text="Baud:").grid(row=0, column=2)
        self.ser_baud = ttk.Entry(self.ser_frame, width=8)
        self.ser_baud.grid(row=0, column=3)
        ttk.Label(self.ser_frame, text="Timeout(s):").grid(row=0, column=4)
        self.ser_timeout = ttk.Entry(self.ser_frame, width=5)
        self.ser_timeout.grid(row=0, column=5)
        self.btn_refresh_ports = ttk.Button(
            self.ser_frame,
            text="Aggiorna porte",
            command=self._refresh_serial_port_list,
        )
        self.btn_refresh_ports.grid(row=0, column=6, padx=(6, 0))

        # TCP
        self.tcp_frame = ttk.Frame(f)
        self.tcp_frame.grid(row=2, column=0, columnspan=2, sticky="we", padx=4)
        ttk.Label(self.tcp_frame, text="Host:").grid(row=0, column=0)
        self.tcp_host = ttk.Entry(self.tcp_frame, width=18)
        self.tcp_host.grid(row=0, column=1)
        ttk.Label(self.tcp_frame, text="Porta TCP:").grid(row=0, column=2)
        self.tcp_port = ttk.Entry(self.tcp_frame, width=8)
        self.tcp_port.grid(row=0, column=3)
        ttk.Label(self.tcp_frame, text="Timeout(s):").grid(row=0, column=4)
        self.tcp_timeout = ttk.Entry(self.tcp_frame, width=5)
        self.tcp_timeout.grid(row=0, column=5)

        self.btn_connect = ttk.Button(f, text="Connetti", command=self.toggle_connect)
        self.btn_connect.grid(row=0, column=3, padx=8)
        self.lbl_status = ttk.Label(f, text="● non connesso", foreground="red")
        self.lbl_status.grid(row=0, column=4, sticky="w")

        self._on_transport_change()

    def _build_antenna(self, parent):
        f = ttk.LabelFrame(parent, text="Parametri lettore e antenne")
        f.pack(fill="x", padx=8, pady=4)

        ttk.Label(f, text="Regione:").grid(row=0, column=0, sticky="w")
        self.region_var = tk.StringVar(value="EU (865-868 MHz)")
        ttk.OptionMenu(f, self.region_var, self.region_var.get(), *REGIONS.keys()).grid(
            row=0, column=1, sticky="w"
        )

        ttk.Label(f, text="Read dBm").grid(row=0, column=3)
        ttk.Label(f, text="Write dBm").grid(row=0, column=5)
        self.pw_vars = {}  # {ant: {"read": IntVar, "write": IntVar}}
        self.pw_labels = {}
        roles = {1: "Base sx (orizz.)", 2: "Base dx (orizz.)", 3: "Parete (verticale)"}
        for i, ant in enumerate([1, 2, 3], start=1):
            ttk.Label(f, text=f"Ant {ant} ({roles[ant]})").grid(row=i, column=0, sticky="w")
            rv = tk.IntVar(value=20)
            wv = tk.IntVar(value=20)
            self.pw_vars[ant] = {"read": rv, "write": wv}
            r = ttk.Scale(
                f,
                from_=5,
                to=30,
                variable=rv,
                orient="horizontal",
                command=lambda e, a=ant: self._on_pw_change(a),
            )
            r.grid(row=i, column=2, sticky="we", padx=4)
            rl = ttk.Label(f, text="20")
            rl.grid(row=i, column=3)
            self.pw_labels[(ant, "read")] = rl
            w = ttk.Scale(
                f,
                from_=5,
                to=30,
                variable=wv,
                orient="horizontal",
                command=lambda e, a=ant: self._on_pw_change(a),
            )
            w.grid(row=i, column=4, sticky="we", padx=4)
            wl = ttk.Label(f, text="20")
            wl.grid(row=i, column=5)
            self.pw_labels[(ant, "write")] = wl
        f.columnconfigure(2, weight=1)
        f.columnconfigure(4, weight=1)

        self.btn_apply = ttk.Button(
            f, text="Applica regione + potenze", command=self.apply_settings
        )
        self.btn_apply.grid(row=1, column=6, padx=8, rowspan=3)
        self.btn_save_cfg = ttk.Button(f, text="Salva config", command=self.save_config)
        self.btn_save_cfg.grid(row=4, column=6, padx=8)

    def _build_test(self, parent):
        f = ttk.LabelFrame(parent, text="Test lettura / scrittura")
        f.pack(fill="x", padx=8, pady=4)

        ttk.Label(f, text="Dati scrittura USER (hex):").grid(row=0, column=0)
        self.write_data = ttk.Entry(f, width=24)
        self.write_data.insert(0, "12345678")
        self.write_data.grid(row=0, column=1)
        ttk.Label(f, text="Password accesso (hex):").grid(row=0, column=2)
        self.acc_pwd = ttk.Entry(f, width=12)
        self.acc_pwd.insert(0, "00000000")
        self.acc_pwd.grid(row=0, column=3)
        ttk.Label(f, text="Parole da leggere:").grid(row=0, column=4)
        self.read_words = ttk.Entry(f, width=4)
        self.read_words.insert(0, "2")
        self.read_words.grid(row=0, column=5)

        self.btn_inv_once = ttk.Button(
            f, text="Inventory singolo", command=self.start_single_inventory
        )
        self.btn_inv_once.grid(row=1, column=0, pady=4, sticky="we")
        self.btn_inv_start = ttk.Button(f, text="Avvia inventario", command=self.start_inventory)
        self.btn_inv_start.grid(row=1, column=1, padx=4, sticky="we")
        ttk.Label(f, text="Durata (s):").grid(row=1, column=2, sticky="e")
        self.inv_duration = ttk.Entry(f, width=6)
        self.inv_duration.insert(0, "30")
        self.inv_duration.grid(row=1, column=3, sticky="w")
        self.btn_inv_timed = ttk.Button(
            f, text="Avvia per durata", command=self.start_timed_inventory
        )
        self.btn_inv_timed.grid(row=1, column=4, padx=4, sticky="we")
        self.btn_inv_stop = ttk.Button(
            f, text="Stop inventario", command=self.stop_inventory, state="disabled"
        )
        self.btn_inv_stop.grid(row=1, column=5, padx=4, sticky="we")

        self.btn_read = ttk.Button(f, text="Read USER", command=self.start_read)
        self.btn_read.grid(row=2, column=0, pady=4, sticky="we")
        self.btn_write = ttk.Button(f, text="Write USER", command=self.start_write)
        self.btn_write.grid(row=2, column=1, sticky="we")
        self.btn_verify = ttk.Button(f, text="Read + Verify", command=self.start_verify)
        self.btn_verify.grid(row=2, column=2, sticky="we")
        self.btn_diag = ttk.Button(f, text="Report diagnostico", command=self.start_diag_report)
        self.btn_diag.grid(row=2, column=5, padx=4, sticky="we")

        ttk.Label(f, text="Nuovo EPC (hex):").grid(row=3, column=0, sticky="w")
        self.epc_value = ttk.Entry(f, width=34)
        self.epc_value.grid(row=3, column=1, columnspan=2, padx=4, sticky="we")
        self.btn_epc_auto = ttk.Button(f, text="AUTO 96 bit", command=self.start_generate_epc)
        self.btn_epc_auto.grid(row=3, column=3, padx=4, sticky="we")
        self.btn_write_epc = ttk.Button(f, text="Scrivi EPC", command=self.start_write_epc)
        self.btn_write_epc.grid(row=3, column=4, padx=4, sticky="we")
        ttk.Label(f, text="Richiede 1 solo tag", foreground="darkred").grid(
            row=3, column=5, sticky="w"
        )

        ttk.Label(f, text="Antenne inventory:").grid(row=4, column=0, sticky="w")
        self.inv_ant_vars: dict[int, tk.BooleanVar] = {}
        for i, ant in enumerate((1, 2, 3), start=1):
            var = tk.BooleanVar(value=True)
            self.inv_ant_vars[ant] = var
            ttk.Checkbutton(f, text=f"Ant {ant}", variable=var).grid(row=4, column=i, sticky="w")

        ttk.Label(f, text="Considera assente dopo:").grid(row=5, column=0, sticky="w")
        self.presence_threshold = ttk.Entry(f, width=6)
        self.presence_threshold.insert(0, "2")
        self.presence_threshold.grid(row=5, column=1, sticky="w")
        self.presence_mode_var = tk.StringVar(value="missed_cycles")
        self.rb_presence_cycles = ttk.Radiobutton(
            f,
            text="cicli senza lettura",
            variable=self.presence_mode_var,
            value="missed_cycles",
        )
        self.rb_presence_cycles.grid(row=5, column=2, columnspan=2, sticky="w")
        self.rb_presence_seconds = ttk.Radiobutton(
            f,
            text="secondi",
            variable=self.presence_mode_var,
            value="seconds",
        )
        self.rb_presence_seconds.grid(row=5, column=4, sticky="w")

    def _build_barcode_epc(self, parent) -> None:
        f = ttk.LabelFrame(parent, text="Barcode → EPC 96 bit (12 byte)")
        f.pack(fill="x", padx=8, pady=4)

        ttk.Label(f, text="Barcode ASCII:").grid(row=0, column=0, sticky="w")
        self.barcode_value = ttk.Entry(f, width=28)
        self.barcode_value.grid(row=0, column=1, padx=4, sticky="we")
        self.barcode_value.bind("<Return>", lambda _event: self.convert_barcode_epc())
        self.btn_barcode_convert = ttk.Button(
            f, text="Converti in HEX", command=self.convert_barcode_epc
        )
        self.btn_barcode_convert.grid(row=0, column=2, padx=4)

        ttk.Label(f, text="EPC HEX:").grid(row=0, column=3, sticky="e")
        self.barcode_epc_hex_var = tk.StringVar()
        ttk.Entry(
            f, textvariable=self.barcode_epc_hex_var, width=30, state="readonly"
        ).grid(row=0, column=4, padx=4, sticky="we")

        ttk.Label(f, text="Tentativi max:").grid(row=0, column=5, sticky="e")
        self.barcode_verify_attempts = ttk.Spinbox(f, from_=1, to=10, width=4)
        self.barcode_verify_attempts.set("3")
        self.barcode_verify_attempts.grid(row=0, column=6, padx=4)
        self.btn_barcode_write = ttk.Button(
            f, text="Scrivi EPC e verifica", command=self.start_barcode_write
        )
        self.btn_barcode_write.grid(row=0, column=7, padx=4)

        ttk.Label(f, text="HEX riletto:").grid(row=1, column=0, sticky="w")
        self.barcode_verified_hex_var = tk.StringVar(value="-")
        ttk.Entry(
            f, textvariable=self.barcode_verified_hex_var, width=30, state="readonly"
        ).grid(row=1, column=1, columnspan=2, padx=4, sticky="we")
        ttk.Label(f, text="Stringa riletta:").grid(row=1, column=3, sticky="e")
        self.barcode_verified_text_var = tk.StringVar(value="-")
        ttk.Entry(
            f, textvariable=self.barcode_verified_text_var, width=24, state="readonly"
        ).grid(row=1, column=4, padx=4, sticky="we")
        self.barcode_compare_var = tk.StringVar(value="Non verificato")
        self.lbl_barcode_compare = ttk.Label(
            f, textvariable=self.barcode_compare_var, foreground="gray"
        )
        self.lbl_barcode_compare.grid(row=1, column=5, columnspan=3, padx=4, sticky="w")
        ttk.Label(f, text="Esito scrittura:").grid(row=2, column=0, sticky="w")
        self.barcode_write_status_var = tk.StringVar(value="Non eseguita")
        self.lbl_barcode_write_status = ttk.Label(
            f, textvariable=self.barcode_write_status_var, foreground="gray"
        )
        self.lbl_barcode_write_status.grid(
            row=2, column=1, columnspan=3, padx=4, sticky="w"
        )
        ttk.Label(f, text="Esito verifica:").grid(row=2, column=4, sticky="e")
        self.lbl_barcode_compare.grid_configure(
            row=2, column=5, columnspan=3, padx=4, sticky="w"
        )
        f.columnconfigure(1, weight=1)
        f.columnconfigure(4, weight=1)

    def _build_inventory_table(self, parent):
        f = ttk.LabelFrame(parent, text="Tag presenti nell'inventario")
        f.pack(fill="x", padx=8, pady=4)

        cols = ("epc", "reads", "records", "antennas", "best_rssi", "last_rssi")
        self.tag_table = ttk.Treeview(f, columns=cols, show="headings", height=6)
        headings = {
            "epc": "EPC",
            "reads": "Letture totali",
            "records": "Record",
            "antennas": "Antenne",
            "best_rssi": "RSSI migliore",
            "last_rssi": "RSSI ultimo",
        }
        widths = {
            "epc": 430,
            "reads": 110,
            "records": 80,
            "antennas": 90,
            "best_rssi": 110,
            "last_rssi": 100,
        }
        for col in cols:
            self.tag_table.heading(col, text=headings[col])
            self.tag_table.column(
                col,
                width=widths[col],
                minwidth=60,
                anchor="w" if col == "epc" else "center",
                stretch=(col == "epc"),
            )

        yscroll = ttk.Scrollbar(f, orient="vertical", command=self.tag_table.yview)
        self.tag_table.configure(yscrollcommand=yscroll.set)
        self.tag_table.grid(row=0, column=0, sticky="nsew", padx=(4, 0), pady=4)
        yscroll.grid(row=0, column=1, sticky="ns", padx=(0, 4), pady=4)

        self.lbl_inventory_summary = ttk.Label(f, text="Nessun inventario eseguito")
        self.lbl_inventory_summary.grid(
            row=1, column=0, columnspan=2, sticky="w", padx=4, pady=(0, 4)
        )
        f.columnconfigure(0, weight=1)

    def _build_plot(self, parent):
        f = ttk.LabelFrame(parent, text="Visualizzazione segnale e volume")
        f.pack(fill="both", expand=True, padx=8, pady=4)
        self.fig = Figure(figsize=(12, 5), dpi=100)
        self.ax_bar = self.fig.add_subplot(1, 2, 1)
        self.ax3d = self.fig.add_subplot(1, 2, 2, projection="3d")
        self.canvas = FigureCanvasTkAgg(self.fig, master=f)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Log
        lf = ttk.LabelFrame(parent, text="Log")
        lf.pack(fill="x", padx=8, pady=4)
        self.txt_log = tk.Text(lf, height=6, state="disabled")
        self.txt_log.pack(fill="x", padx=4, pady=4)

        self._draw_empty_bar()
        self._draw_volume([])

    # ------------------------------------------------------------------ #
    # Helpers UI
    # ------------------------------------------------------------------ #
    def _on_transport_change(self, *_):
        if self.transport_var.get() == "serial":
            self.tcp_frame.grid_remove()
            self.ser_frame.grid()
        else:
            self.ser_frame.grid_remove()
            self.tcp_frame.grid()

    def _refresh_serial_port_list(self, log_result: bool = True) -> None:
        """Aggiorna l'elenco COM mantenendo possibile l'inserimento manuale."""
        try:
            ports = discover_serial_ports()
        except Exception as exc:
            if log_result:
                self.log(f"Ricerca porte seriali fallita: {exc}")
            return
        devices = [item["device"] for item in ports]
        self.ser_port.configure(values=devices)
        if not self.ser_port.get().strip() and devices:
            self.ser_port.set(devices[0])
        if log_result:
            if ports:
                details = ", ".join(
                    f"{item['device']} ({item['description'] or 'senza descrizione'})"
                    for item in ports
                )
                self.log(f"Porte seriali rilevate: {details}")
            else:
                self.log("Nessuna porta seriale rilevata; e' possibile inserirla manualmente.")

    def _on_pw_change(self, ant):
        for k in ("read", "write"):
            v = self.pw_vars[ant][k].get()
            self.pw_labels[(ant, k)].config(text=str(v))

    def _populate_from_cfg(self):
        # accetta anche le sezioni "parcheggiate" da save_config (trasporto inattivo)
        s = self.cfg.get("serial") or self.cfg.get("serial_disabled") or {}
        self.ser_port.insert(0, s.get("port", "COM3"))
        self.ser_baud.insert(0, str(s.get("baudrate", 115200)))
        self.ser_timeout.insert(0, str(s.get("timeout_s", 2.0)))
        t = self.cfg.get("tcp") or self.cfg.get("tcp_disabled") or {}
        self.tcp_host.insert(0, t.get("host", "192.168.1.100"))
        self.tcp_port.insert(0, str(t.get("port", 8080)))
        self.tcp_timeout.insert(0, str(t.get("timeout_s", 2.0)))
        if "tcp" in self.cfg and "serial" not in self.cfg:
            self.transport_var.set("tcp")
            self._on_transport_change()
        # potenze
        for a in self.cfg.get("antennas", []):
            ant = a["id"]
            if ant in self.pw_vars:
                self.pw_vars[ant]["read"].set(a.get("read_power", 2000) // 100)
                self.pw_vars[ant]["write"].set(a.get("write_power", 2000) // 100)
                self._on_pw_change(ant)
        # regione
        for name, code in REGIONS.items():
            if code == self.cfg.get("reader", {}).get("region", 0x08):
                self.region_var.set(name)
                break
        # inventory
        inv_cfg = self.cfg.get("inventory", {}) or {}
        selected = set(inv_cfg.get("antennas") or [1, 2, 3])
        for ant, var in self.inv_ant_vars.items():
            var.set(ant in selected)
        self.inv_duration.delete(0, "end")
        self.inv_duration.insert(0, str(inv_cfg.get("duration_s", 30)))

        presence_mode = str(inv_cfg.get("presence_mode", "missed_cycles"))
        if presence_mode not in {"missed_cycles", "seconds"}:
            presence_mode = "missed_cycles"
        self.presence_mode_var.set(presence_mode)
        self.presence_threshold.delete(0, "end")
        self.presence_threshold.insert(0, str(inv_cfg.get("presence_threshold", 2)))
        tag_cfg = self.cfg.get("tag_access", {}) or {}
        self.barcode_verify_attempts.delete(0, "end")
        self.barcode_verify_attempts.insert(
            0, str(tag_cfg.get("barcode_verify_attempts", 3))
        )

    def _post_ui(self, callback) -> None:
        """Accoda una callback Tk; i worker non chiamano mai Tk direttamente."""
        if not self._destroyed:
            self._ui_queue.put(callback)

    def _drain_ui_queue(self) -> None:
        if self._destroyed:
            return
        while True:
            try:
                callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except tk.TclError:
                if not self._closing:
                    raise
        if not self._destroyed:
            self.root.after(50, self._drain_ui_queue)

    def log(self, msg: str) -> None:
        ts = dt.datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._post_ui(lambda line=line: self._log_insert(line))

    def _log_insert(self, line: str) -> None:
        if self._destroyed:
            return
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _set_status(self, connected: bool) -> None:
        self.connected = connected
        if connected:
            shared = self._shared_service is not None and not self._owns_service
            text = "● service condiviso" if shared else "● connesso"
            self.lbl_status.config(text=text, foreground="green")
            self.btn_connect.config(text="Scollega GUI" if shared else "Disconnetti")
        else:
            self.lbl_status.config(text="● non connesso", foreground="red")
            self.btn_connect.config(text="Connetti")
        self._refresh_controls()

    def _refresh_controls(self) -> None:
        if not hasattr(self, "btn_connect"):
            return
        blocked = self.operation_running or self._closing
        connected_ready = self.connected and not blocked
        shared_running = self._shared_service is not None and self._shared_service.ready
        transport_editable = not blocked and not self.connected and not shared_running
        transport_state = "normal" if transport_editable else "disabled"
        for widget in (
            self.rb_serial,
            self.rb_tcp,
            self.ser_port,
            self.ser_baud,
            self.ser_timeout,
            self.btn_refresh_ports,
            self.tcp_host,
            self.tcp_port,
            self.tcp_timeout,
        ):
            widget.config(state=transport_state)
        self.btn_connect.config(state="disabled" if blocked else "normal")
        self.btn_apply.config(state="normal" if connected_ready else "disabled")
        self.btn_save_cfg.config(state="disabled" if blocked else "normal")
        for button in (
            self.btn_inv_once,
            self.btn_inv_start,
            self.btn_inv_timed,
            self.btn_read,
            self.btn_write,
            self.btn_epc_auto,
            self.btn_write_epc,
            self.btn_barcode_write,
            self.btn_verify,
            self.btn_diag,
        ):
            button.config(state="normal" if connected_ready else "disabled")
        self.btn_inv_stop.config(
            state="normal" if self.inventory_running and not self._closing else "disabled"
        )

    def _finish_operation(self) -> None:
        self.operation_running = False
        self._refresh_controls()

    def _run_async(self, fn) -> bool:
        """Esegue una sola operazione reader alla volta, senza accodare click."""
        if self.operation_running or self._closing:
            self.log("Lettore occupato: attendere il termine dell'operazione.")
            return False
        self.operation_running = True
        self._refresh_controls()

        def wrapped():
            try:
                fn()
            except Exception as e:
                self.log(f"Errore interno operazione: {e}")
            finally:
                self._post_ui(self._finish_operation)

        self._executor.submit(wrapped)
        return True

    # ------------------------------------------------------------------ #
    # Connessione
    # ------------------------------------------------------------------ #
    def _runtime_config(self) -> dict:
        """Costruisce la config del service senza esporre trasporti alla GUI."""
        cfg = copy.deepcopy(self.cfg or {})
        cfg.pop("serial", None)
        cfg.pop("tcp", None)
        active = self.transport_var.get()
        section = dict(self.cfg.get(active) or self.cfg.get(f"{active}_disabled") or {})
        if active == "serial":
            section.update(
                {
                    "port": self.ser_port.get().strip(),
                    "baudrate": int(self.ser_baud.get() or 115200),
                    "timeout_s": float(self.ser_timeout.get() or 2.0),
                }
            )
        else:
            section.update(
                {
                    "host": self.tcp_host.get().strip(),
                    "port": int(self.tcp_port.get() or 8080),
                    "timeout_s": float(self.tcp_timeout.get() or 2.0),
                }
            )
        cfg[active] = section
        return cfg

    def _build_binding(self, config: dict) -> RFIDServiceBinding:
        backend = (
            self._shared_service
            if self._shared_service is not None
            else self._service_factory(config)
        )
        return RFIDServiceBinding(backend, owns_lifecycle=self._owns_service)

    def toggle_connect(self) -> None:
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        try:
            config = self._runtime_config()
            binding = self._build_binding(config)
        except (TypeError, ValueError, KeyError) as e:
            messagebox.showerror("Connessione", f"Parametri non validi: {e}")
            return
        if self._shared_service is not None and self._shared_service.ready:
            self.log(
                "Service condiviso già attivo: i parametri di trasporto della GUI "
                "non vengono sostituiti."
            )
        self._run_async(lambda: self._connect_worker(binding, config))

    def _connect_worker(self, binding: RFIDServiceBinding, config: dict) -> None:
        self._binding = binding
        self.service = binding.backend
        response = binding.attach(config)
        if not response.ok:
            binding.detach()
            self._binding = None
            self.service = None
            error = self._service_error(response)
            self.log(f"Errore connessione: {error}")
            self._post_ui(lambda msg=error: messagebox.showerror("Connessione", msg))
            return
        info = response.data["firmware_info"]
        self.log(f"Connesso. FW {info['firmware_version']} HW {info['hardware_version']}")
        self._post_ui(lambda: self._set_status(True))

    def _disconnect(self) -> None:
        if self.inventory_running:
            self.stop_inventory()
            self.log("Arresto inventario in corso; disconnettere al termine.")
            return
        self._run_async(self._disconnect_worker)

    def _disconnect_worker(self) -> None:
        binding, self._binding = self._binding, None
        self.service = None
        if binding is not None:
            response = binding.detach()
            if not response.ok:
                self.log(f"Errore disconnessione: {self._service_error(response)}")
            elif not binding.owns_lifecycle:
                self.log("GUI scollegata; il service condiviso resta attivo.")
            else:
                self.log("Disconnesso.")
        else:
            self.log("Disconnesso.")
        self._post_ui(lambda: self._set_status(False))

    def on_close(self) -> None:
        """Arresto ordinato: ferma i worker e chiude seriale/TCP prima di Tk."""
        if self._closing:
            return
        self._closing = True
        self.inventory_stop_event.set()
        self._refresh_controls()
        self.log("Chiusura in corso...")
        self._executor.submit(self._shutdown_worker)

    def _shutdown_worker(self) -> None:
        try:
            binding, self._binding = self._binding, None
            self.service = None
            if binding is not None:
                binding.detach()
        finally:
            self._post_ui(self._finish_close)

    def _finish_close(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    # ------------------------------------------------------------------ #
    # Impostazioni
    # ------------------------------------------------------------------ #
    def apply_settings(self) -> None:
        if not self._guard():
            return
        settings = ReaderSettings(
            region=0x08,
            powers=tuple(
                AntennaPower(
                    antenna_id=antenna,
                    read_power_cdbm=self.pw_vars[antenna]["read"].get() * 100,
                    write_power_cdbm=self.pw_vars[antenna]["write"].get() * 100,
                )
                for antenna in (1, 2, 3)
            ),
        )
        self._run_async(lambda: self._apply_settings_worker(settings))

    def _apply_settings_worker(self, settings: ReaderSettings) -> None:
        service = self.service
        if service is None:
            self.log("Errore impostazioni: servizio non connesso")
            return
        response = service.configure(settings)
        if response.ok:
            self.log(f"Regione EU + potenze applicate: {response.data['settings']['powers']}")
        else:
            self.log(f"Errore impostazioni: {self._service_error(response)}")

    def save_config(self) -> None:
        """Salva la configurazione completa preservando le sezioni non gestite."""
        cfg = dict(self.cfg or {})
        active = self.transport_var.get()
        other = "tcp" if active == "serial" else "serial"
        section = dict(cfg.get(active) or cfg.get(f"{active}_disabled") or {})
        try:
            if active == "serial":
                section.update(
                    {
                        "port": self.ser_port.get().strip(),
                        "baudrate": int(self.ser_baud.get() or 115200),
                        "timeout_s": float(self.ser_timeout.get() or 2.0),
                    }
                )
            else:
                section.update(
                    {
                        "host": self.tcp_host.get().strip(),
                        "port": int(self.tcp_port.get() or 8080),
                        "timeout_s": float(self.tcp_timeout.get() or 2.0),
                    }
                )
            duration_s = float((self.inv_duration.get() or "30").strip().replace(",", "."))
            if duration_s <= 0:
                raise ValueError("la durata inventory deve essere > 0")
            presence_mode, presence_threshold = self._presence_policy_from_ui()
            barcode_verify_attempts = self._barcode_verify_attempts_from_ui()
        except ValueError as e:
            messagebox.showerror("Configurazione", f"Valore non valido: {e}")
            return

        cfg[active] = section
        cfg.pop(f"{active}_disabled", None)
        if other in cfg:
            cfg[f"{other}_disabled"] = cfg.pop(other)

        reader_cfg = dict(cfg.get("reader") or {})
        reader_cfg["region"] = 0x08
        cfg["reader"] = reader_cfg

        default_roles = {1: "floor_left", 2: "floor_right", 3: "wall_90"}
        old_ants = {a.get("id"): a for a in cfg.get("antennas") or []}
        cfg["antennas"] = []
        for ant in (1, 2, 3):
            antenna_cfg = dict(old_ants.get(ant) or {"id": ant, "role": default_roles[ant]})
            antenna_cfg["read_power"] = self.pw_vars[ant]["read"].get() * 100
            antenna_cfg["write_power"] = self.pw_vars[ant]["write"].get() * 100
            cfg["antennas"].append(antenna_cfg)

        selected = self._selected_inventory_antennas()
        if not selected:
            return
        inventory_cfg = dict(cfg.get("inventory") or {})
        inventory_cfg["antennas"] = selected
        inventory_cfg["duration_s"] = duration_s
        inventory_cfg.setdefault("max_unique_epcs", 1000)
        inventory_cfg.setdefault("stop_after_consecutive_errors", 3)
        inventory_cfg["presence_mode"] = presence_mode
        inventory_cfg["presence_threshold"] = presence_threshold
        cfg["inventory"] = inventory_cfg
        tag_access_cfg = dict(cfg.get("tag_access") or {})
        tag_access_cfg["barcode_verify_attempts"] = barcode_verify_attempts
        cfg["tag_access"] = tag_access_cfg

        try:
            with open(self.cfg_path, "w", encoding="utf-8") as config_file:
                yaml.safe_dump(cfg, config_file, sort_keys=False)
        except OSError as e:
            self.log(f"Errore salvataggio config: {e}")
            return
        self.cfg = cfg
        self.log(f"Config salvata in {self.cfg_path}")

    # ------------------------------------------------------------------ #
    # Operazioni tag
    # ------------------------------------------------------------------ #
    @staticmethod
    def _service_error(response) -> str:
        if response.error:
            return str(response.error.get("message", response.error))
        return f"operazione {response.operation} fallita"

    @staticmethod
    def _tag_from_service(payload: dict) -> Tag:
        values = dict(payload)
        embedded = values.get("embedded_data")
        if isinstance(embedded, str):
            values["embedded_data"] = bytes.fromhex(embedded)
        return Tag(**values)

    def _guard(self) -> bool:
        if not self.connected or self.service is None or not self.service.ready:
            messagebox.showwarning("Attenzione", "Connettere prima il lettore.")
            return False
        return True

    def _hex_bytes(self, value: str, name: str, even: bool = True) -> bytes | None:
        value = (value or "").strip().replace(" ", "")
        try:
            parsed = bytes.fromhex(value)
        except ValueError:
            self.log(f"{name}: valore esadecimale non valido: '{value}'")
            return None
        if even and len(parsed) % 2:
            self.log(f"{name}: la lunghezza deve essere pari (word da 16 bit)")
            return None
        return parsed

    def _password_bytes(self) -> bytes | None:
        password = self._hex_bytes(self.acc_pwd.get() or "00000000", "Password accesso", even=False)
        if password is not None and len(password) != 4:
            self.log("Password accesso: servono esattamente 4 byte (8 cifre hex)")
            return None
        return password

    def _barcode_verify_attempts_from_ui(self) -> int:
        raw = (self.barcode_verify_attempts.get() or "").strip()
        try:
            attempts = int(raw)
        except ValueError as exc:
            raise ValueError("le verifiche barcode devono essere un numero intero") from exc
        if not 1 <= attempts <= 10:
            raise ValueError("le verifiche barcode devono essere comprese tra 1 e 10")
        return attempts

    def _selected_inventory_antennas(self) -> list[int]:
        antennas = [ant for ant, var in self.inv_ant_vars.items() if var.get()]
        if not antennas:
            self.log("Inventory: selezionare almeno un'antenna.")
        return antennas

    def _presence_policy_from_ui(self) -> tuple[str, int | float]:
        mode = self.presence_mode_var.get()
        raw = (self.presence_threshold.get() or "").strip().replace(",", ".")
        if mode == "missed_cycles":
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError("le mancate letture devono essere un numero intero") from exc
            if value < 1:
                raise ValueError("le mancate letture devono essere almeno 1")
            return mode, value
        if mode == "seconds":
            try:
                value_s = float(raw)
            except ValueError as exc:
                raise ValueError("i secondi devono essere un numero") from exc
            if value_s <= 0:
                raise ValueError("i secondi devono essere maggiori di 0")
            return mode, value_s
        raise ValueError("modalità di rilevamento presenza non valida")

    def _capture_presence_policy(self) -> bool:
        try:
            mode, threshold = self._presence_policy_from_ui()
        except ValueError as exc:
            self.log(f"Soglia presenza non valida: {exc}")
            return False
        self.inventory_presence_mode = mode
        self.inventory_presence_threshold = threshold
        return True

    def _presence_policy_label(self) -> str:
        unit = "cicli senza lettura" if self.inventory_presence_mode == "missed_cycles" else "s"
        return f"{self.inventory_presence_threshold:g} {unit}"

    def _inventory_timeout_ms(self) -> int:
        return int((self.cfg.get("inventory", {}) or {}).get("timeout_ms", 1000))

    def _inventory_metadata_flags(self) -> int:
        return int((self.cfg.get("inventory", {}) or {}).get("metadata_flags", 0x0007))

    def _reset_inventory_session(self) -> None:
        inventory_cfg = self.cfg.get("inventory", {}) or {}
        self.inventory_accumulator = TagReadAccumulator(
            max_unique_epcs=int(inventory_cfg.get("max_unique_epcs", 1000))
        )
        self.inventory_cycles = 0
        self.inventory_no_tag_cycles = 0
        self.inventory_error_cycles = 0
        self.inventory_started_at = time.monotonic()
        self.last_tags = []
        self.ant_rssi = {1: None, 2: None, 3: None}
        self._update_inventory_results([], [], 0, 0)

    def _refresh_ant_rssi(self, tags: list[Tag]) -> None:
        self.ant_rssi = {1: None, 2: None, 3: None}
        for tag in tags:
            if tag.antenna_id in self.ant_rssi and tag.rssi is not None:
                current = self.ant_rssi[tag.antenna_id]
                if current is None or tag.rssi > current:
                    self.ant_rssi[tag.antenna_id] = tag.rssi

    def start_single_inventory(self) -> None:
        if not self._guard():
            return
        if not self._capture_presence_policy():
            return
        antennas = self._selected_inventory_antennas()
        if not antennas:
            return
        self._reset_inventory_session()
        self._run_async(lambda: self._inventory_cycle(antennas))

    def start_inventory(self) -> None:
        self._start_inventory_loop(duration_s=None)

    def start_timed_inventory(self) -> None:
        raw = (self.inv_duration.get() or "").strip().replace(",", ".")
        try:
            duration_s = float(raw)
        except ValueError:
            self.log(f"Durata inventario non valida: '{self.inv_duration.get()}'")
            return
        if duration_s <= 0:
            self.log("Durata inventario: inserire un valore maggiore di 0 secondi")
            return
        self._start_inventory_loop(duration_s=duration_s)

    def _start_inventory_loop(self, duration_s: float | None) -> None:
        if not self._guard():
            return
        if not self._capture_presence_policy():
            return
        antennas = self._selected_inventory_antennas()
        if not antennas:
            return
        self.inventory_running = True
        self.inventory_stop_event.clear()
        self._reset_inventory_session()
        mode = "continuo" if duration_s is None else f"per {duration_s:g}s"
        self.log(
            f"Inventory {mode} avviato su antenne {antennas}; "
            f"rimozione tag dopo {self._presence_policy_label()}"
        )
        if not self._run_async(lambda: self._inventory_loop(antennas, duration_s)):
            self.inventory_running = False
        self._refresh_controls()

    def stop_inventory(self) -> None:
        if self.inventory_running:
            self.inventory_stop_event.set()
            self.log("Stop inventario richiesto.")

    def _inventory_loop(self, antennas: list[int], duration_s: float | None) -> None:
        consecutive_errors = 0
        inventory_cfg = self.cfg.get("inventory", {}) or {}
        max_errors = int(inventory_cfg.get("stop_after_consecutive_errors", 3))
        try:
            while not self.inventory_stop_event.is_set():
                if duration_s is not None and self.inventory_started_at is not None:
                    if time.monotonic() - self.inventory_started_at >= duration_s:
                        break
                if self._inventory_cycle(antennas):
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= max_errors:
                        self.log(
                            f"Inventory arrestato dopo {consecutive_errors} errori consecutivi."
                        )
                        break
                self.inventory_stop_event.wait(0.05)
        except Exception as e:
            self.inventory_error_cycles += 1
            self.log(f"Errore avvio inventory: {e}")
        finally:
            self._log_inventory_summary("terminato")
            self._post_ui(self._finish_inventory)

    def _finish_inventory(self) -> None:
        self.inventory_running = False
        self.inventory_stop_event.clear()
        self._refresh_controls()

    def _log_inventory_summary(self, status: str) -> None:
        rows = self.inventory_accumulator.summaries()
        total_reads = sum(row.reads for row in rows)
        elapsed = (
            time.monotonic() - self.inventory_started_at
            if self.inventory_started_at is not None
            else 0.0
        )
        self.log(
            f"Inventario {status}: {self.inventory_cycles} cicli, "
            f"{self.inventory_no_tag_cycles} senza tag, "
            f"{self.inventory_error_cycles} errori, {len(rows)} EPC presenti al termine, "
            f"{total_reads} letture in {elapsed:.1f}s"
        )

    def _update_inventory_presence(self, tags: list[Tag]) -> tuple[str, ...]:
        if self.inventory_presence_mode == "seconds":
            return self.inventory_accumulator.update_presence(
                tags, remove_after_seconds=float(self.inventory_presence_threshold)
            )
        return self.inventory_accumulator.update_presence(
            tags, remove_after_missed_cycles=int(self.inventory_presence_threshold)
        )

    def _inventory_cycle(self, antennas: list[int]) -> bool:
        try:
            service = self.service
            if service is None:
                raise RuntimeError("servizio non connesso")
            response = service.inventory(
                InventoryRequest(
                    antennas=tuple(antennas),
                    timeout_ms=self._inventory_timeout_ms(),
                    metadata_flags=self._inventory_metadata_flags(),
                )
            )
            if not response.ok:
                raise RuntimeError(self._service_error(response))
            tags = [self._tag_from_service(dict(item)) for item in response.data["tags"]]
            self.last_tags = list(tags)
            self.inventory_cycles += 1
            expired_epcs = self._update_inventory_presence(tags)
            self._refresh_ant_rssi(tags)
            rows = self.inventory_accumulator.summaries()
            if expired_epcs:
                self.log(
                    "Tag non più rilevati rimossi dall'elenco: "
                    + ", ".join(expired_epcs)
                )
            if not tags:
                self.inventory_no_tag_cycles += 1
                self.log(f"Inventory ciclo {self.inventory_cycles}: nessun tag.")
            else:
                cycle_reads = sum(
                    tag.read_count if tag.read_count is not None else 1 for tag in tags
                )
                total_reads = sum(row.reads for row in rows)
                self.log(
                    f"Inventory ciclo {self.inventory_cycles}: "
                    f"{len({tag.epc for tag in tags})} EPC, {cycle_reads} letture; "
                    f"totale {len(rows)} EPC / {total_reads} letture"
                )
            self._queue_inventory_results(rows, tags)
            return True
        except Exception as e:
            self.inventory_error_cycles += 1
            self.log(f"Errore inventory: {e}")
            return False

    def _queue_inventory_results(self, rows: list[TagReadSummary], latest: list[Tag]) -> None:
        rows_copy = list(rows)
        latest_copy = list(latest)
        record_count = self.inventory_accumulator.record_count
        evicted = self.inventory_accumulator.evicted_epcs
        self._post_ui(
            lambda: self._update_inventory_results(rows_copy, latest_copy, record_count, evicted)
        )

    def start_read(self) -> None:
        if not self._guard():
            return
        password = self._password_bytes()
        if password is None:
            return
        try:
            words = int(self.read_words.get() or 2)
        except ValueError:
            self.log(f"Parole da leggere: numero non valido: '{self.read_words.get()}'")
            return
        self._run_async(lambda: self._do_read(words, password))

    def _do_read(self, words: int, password: bytes) -> None:
        service = self.service
        if service is None:
            self.log("Errore read: servizio non connesso")
            return
        response = service.read(
            ReadRequest(
                bank=MemoryBank.USER,
                address=0,
                word_count=words,
                antennas=(1, 2, 3),
                access_password_hex=password.hex(),
                timeout_ms=1000,
            )
        )
        for antenna, result in response.data.get("results", {}).items():
            if result["ok"]:
                self.log(f"Read USER ant{antenna}: {result['data']}")
            else:
                self.log(f"Read USER ant{antenna}: FAIL {result['error']}")
        if not response.ok:
            self.log(f"Errore read: {self._service_error(response)}")

    def start_write(self) -> None:
        if not self._guard():
            return
        data = self._hex_bytes(self.write_data.get(), "Dati scrittura")
        if data is None:
            return
        if not data or len(data) > 64 or len(data) % 2:
            self.log(
                f"Dati scrittura: lunghezza {len(data)} byte non valida (1..64 byte, multiplo di 2)"
            )
            return
        password = self._password_bytes()
        if password is None:
            return
        epcs = sorted(row.epc for row in self.inventory_accumulator.summaries())
        if len(epcs) != 1 or self.inventory_accumulator.evicted_epcs:
            messagebox.showwarning(
                "Scrittura bloccata",
                "Eseguire prima una sessione inventory che rilevi esattamente un EPC.",
            )
            return
        confirmed = messagebox.askyesno(
            "Conferma scrittura",
            f"Scrivere {data.hex().upper()} sul tag EPC {epcs[0]}?\n"
            "La scrittura usa il primo tag rispondente.",
        )
        if confirmed:
            self._run_async(lambda: self._do_write(data, password, epcs[0]))

    def _do_write(self, data: bytes, password: bytes, expected_epc: str) -> None:
        service = self.service
        if service is None:
            self.log("Errore write: servizio non connesso")
            return
        fresh_inventory = service.inventory(
            InventoryRequest(
                antennas=(1, 2, 3),
                timeout_ms=self._inventory_timeout_ms(),
                metadata_flags=self._inventory_metadata_flags(),
            )
        )
        if not fresh_inventory.ok:
            self.log(
                "Scrittura bloccata: impossibile verificare il tag: "
                f"{self._service_error(fresh_inventory)}"
            )
            return
        response = service.write(
            WriteRequest(
                bank=MemoryBank.USER,
                address=0,
                data_hex=data.hex(),
                expected_epc=expected_epc,
                antennas=(1, 2, 3),
                access_password_hex=password.hex(),
                timeout_ms=1000,
            )
        )
        for antenna, result in response.data.get("results", {}).items():
            outcome = "OK" if result["ok"] else f"FAIL {result['error']}"
            self.log(f"Write USER ant{antenna}: {outcome}")
        if not response.ok:
            self.log(f"Errore write: {self._service_error(response)}")

    def _set_barcode_result(
        self, *, epc_hex: str = "-", text: str = "-", status: str, color: str
    ) -> None:
        self.barcode_verified_hex_var.set(epc_hex)
        self.barcode_verified_text_var.set(text)
        self.barcode_compare_var.set(status)
        self.lbl_barcode_compare.config(foreground=color)

    def _set_barcode_write_status(self, status: str, color: str) -> None:
        self.barcode_write_status_var.set(status)
        self.lbl_barcode_write_status.config(foreground=color)

    def _queue_barcode_write_status(self, status: str, color: str) -> None:
        self._post_ui(
            lambda status=status, color=color: self._set_barcode_write_status(
                status, color
            )
        )

    def _queue_barcode_status(self, status: str, color: str = "red") -> None:
        self._post_ui(
            lambda status=status, color=color: self._set_barcode_result(
                status=status, color=color
            )
        )

    def _set_barcode_verification(
        self, barcode: str, found_epcs: list[str], verified: bool, attempts: int
    ) -> None:
        epc_hex = found_epcs[0] if len(found_epcs) == 1 else ", ".join(found_epcs) or "-"
        decoded = "-"
        if len(found_epcs) == 1:
            try:
                decoded = epc_hex_to_barcode(found_epcs[0], byte_length=12)
            except ValueError:
                decoded = "<EPC non ASCII>"
        matches = verified and decoded == barcode
        reading_label = "lettura" if attempts == 1 else "letture"
        status = (
            f"CONFRONTO OK ({attempts} {reading_label})"
            if matches
            else f"NON CORRISPONDE / NON VERIFICATO ({attempts} {reading_label})"
        )
        self._set_barcode_result(
            epc_hex=epc_hex,
            text=decoded,
            status=status,
            color="green" if matches else "red",
        )

    def convert_barcode_epc(self) -> str | None:
        barcode = self.barcode_value.get()
        try:
            epc_hex = barcode_to_epc_hex(barcode, byte_length=12)
        except ValueError as exc:
            self.barcode_epc_hex_var.set("")
            self._set_barcode_write_status("Non eseguita", "gray")
            self._set_barcode_result(status=f"Errore: {exc}", color="red")
            self.log(f"Conversione barcode fallita: {exc}")
            return None
        self.barcode_epc_hex_var.set(epc_hex)
        self._set_epc_value(epc_hex)
        self._set_barcode_write_status("Non eseguita", "gray")
        self._set_barcode_result(
            status="Convertito; non ancora scritto", color="darkorange"
        )
        self.log(f"Barcode {barcode!r} -> EPC HEX {epc_hex}")
        return epc_hex

    def start_barcode_write(self) -> None:
        epc_hex = self.convert_barcode_epc()
        if epc_hex is None or not self._guard():
            return
        try:
            attempts = self._barcode_verify_attempts_from_ui()
        except ValueError as exc:
            self._set_barcode_result(status=f"Errore: {exc}", color="red")
            self.log(f"Verifica barcode non valida: {exc}")
            return
        password = self._password_bytes()
        if password is None:
            return
        antennas = self._selected_inventory_antennas()
        if not antennas:
            return
        barcode = self.barcode_value.get()
        rows = self.inventory_accumulator.summaries()
        current_epc = rows[0].epc if len(rows) == 1 else "(rilevamento automatico)"
        confirmed = messagebox.askyesno(
            "Conferma scrittura barcode",
            "ATTENZIONE: l'operazione modifica l'EPC del tag.\n\n"
            f"Barcode:       {barcode}\n"
            f"EPC corrente:  {current_epc}\n"
            f"Nuovo EPC HEX: {epc_hex}\n"
            f"Tentativi max: {attempts}\n\n"
            "Il programma eseguirà un inventory di sicurezza. "
            "Lasciare nel campo un solo tag e confermare?",
        )
        if confirmed:
            self._set_barcode_write_status("Scrittura in corso...", "blue")
            self._set_barcode_result(status="Scrittura e verifica in corso...", color="blue")
            started = self._run_async(
                lambda: self._do_barcode_write(
                    barcode=barcode,
                    epc_hex=epc_hex,
                    antennas=tuple(antennas),
                    password=password,
                    verification_attempts=attempts,
                )
            )
            if not started:
                self._set_barcode_write_status("NON ESEGUITA: lettore occupato", "red")

    def _do_barcode_write(
        self,
        *,
        barcode: str,
        epc_hex: str,
        antennas: tuple[int, ...],
        password: bytes,
        verification_attempts: int,
    ) -> None:
        service = self.service
        if service is None:
            self._queue_barcode_write_status("NON ESEGUITA: servizio non connesso", "red")
            self._queue_barcode_status("Servizio non connesso")
            return
        self._queue_barcode_write_status("Inventory di sicurezza in corso...", "blue")
        fresh = service.inventory(
            InventoryRequest(
                antennas=antennas,
                timeout_ms=self._inventory_timeout_ms(),
                metadata_flags=self._inventory_metadata_flags(),
            )
        )
        if not fresh.ok:
            error = f"Inventory barcode fallito: {self._service_error(fresh)}"
            self.log(error)
            self._queue_barcode_write_status("NON ESEGUITA: inventory fallito", "red")
            self._queue_barcode_status(error)
            return
        detected = list(fresh.data.get("unique_epcs", []))
        if len(detected) != 1:
            error = f"Scrittura barcode bloccata: atteso un solo tag, rilevati={detected}"
            self.log(error)
            self._queue_barcode_write_status("NON ESEGUITA: tag non univoco", "red")
            self._queue_barcode_status(error)
            return
        try:
            request = WriteEpcRequest(
                new_epc=epc_hex,
                expected_epc=detected[0],
                antennas=antennas,
                access_password_hex=password.hex(),
                timeout_ms=self._inventory_timeout_ms(),
            )
        except ValueError as exc:
            error = f"Barcode EPC non valido: {exc}"
            self.log(error)
            self._queue_barcode_write_status("NON ESEGUITA", "red")
            self._queue_barcode_status(error)
            return
        self.log(
            f"Target barcode identificato: EPC corrente={request.expected_epc}, "
            f"nuovo EPC={request.new_epc}"
        )
        self._do_write_epc(
            request,
            verification_attempts=verification_attempts,
            barcode=barcode,
        )

    def start_generate_epc(self) -> None:
        if not self._guard():
            return
        self._run_async(self._do_generate_epc)

    def _do_generate_epc(self) -> None:
        service = self.service
        if service is None:
            self.log("Generazione EPC: servizio non connesso")
            return
        tag_cfg = self.cfg.get("tag_access", {}) or {}
        request = EpcGenerationRequest(
            byte_length=int(tag_cfg.get("auto_epc_bytes", 12)),
            prefix_hex=str(tag_cfg.get("auto_epc_prefix_hex", "")),
        )
        response = service.generate_epc(request)
        if not response.ok:
            self.log(f"Generazione EPC fallita: {self._service_error(response)}")
            return
        epc = str(response.data["epc"])
        self._post_ui(lambda epc=epc: self._set_epc_value(epc))
        self.log(
            f"EPC AUTO generato: {epc} "
            f"({response.data['random_bits']} bit casuali; non ancora scritto)"
        )

    def _set_epc_value(self, epc: str) -> None:
        self.epc_value.delete(0, "end")
        self.epc_value.insert(0, epc)

    def start_write_epc(self) -> None:
        if not self._guard():
            return
        password = self._password_bytes()
        if password is None:
            return
        target = self._hex_bytes(self.epc_value.get(), "Nuovo EPC")
        if target is None:
            return
        if not 2 <= len(target) <= 62 or len(target) % 2:
            self.log("Nuovo EPC: servono 2..62 byte e un numero pari di byte.")
            return
        rows = self.inventory_accumulator.summaries()
        if len(rows) != 1 or self.inventory_accumulator.evicted_epcs:
            messagebox.showwarning(
                "Cambio EPC bloccato",
                "Eseguire prima un inventory che rilevi esattamente un EPC.",
            )
            return
        antennas = self._selected_inventory_antennas()
        if not antennas:
            return
        try:
            request = WriteEpcRequest(
                new_epc=target.hex().upper(),
                expected_epc=rows[0].epc,
                antennas=tuple(antennas),
                access_password_hex=password.hex(),
                timeout_ms=self._inventory_timeout_ms(),
            )
        except ValueError as exc:
            self.log(f"Nuovo EPC non valido: {exc}")
            return
        confirmed = messagebox.askyesno(
            "Conferma cambio EPC",
            "ATTENZIONE: l'operazione modifica l'identificativo del tag.\n\n"
            f"EPC corrente: {request.expected_epc}\n"
            f"Nuovo EPC:    {request.new_epc}\n\n"
            "Lasciare nel campo un solo tag e confermare?",
        )
        if confirmed:
            self._run_async(lambda request=request: self._do_write_epc(request))

    def _do_write_epc(
        self,
        request: WriteEpcRequest,
        *,
        verification_attempts: int = 3,
        barcode: str | None = None,
    ) -> None:
        service = self.service
        if service is None:
            self.log("Cambio EPC: servizio non connesso")
            if barcode is not None:
                self._queue_barcode_write_status("NON ESEGUITA: servizio non connesso", "red")
                self._queue_barcode_status("Servizio non connesso")
            return
        inventory_request = InventoryRequest(
            antennas=request.antennas,
            timeout_ms=self._inventory_timeout_ms(),
            metadata_flags=self._inventory_metadata_flags(),
        )
        fresh = service.inventory(inventory_request)
        if not fresh.ok:
            error = (
                "Cambio EPC bloccato: inventory di sicurezza fallito: "
                f"{self._service_error(fresh)}"
            )
            self.log(error)
            if barcode is not None:
                self._queue_barcode_write_status("NON ESEGUITA: controllo iniziale fallito", "red")
                self._queue_barcode_status(error)
            return
        detected = list(fresh.data.get("unique_epcs", []))
        if detected != [request.expected_epc]:
            error = (
                "Cambio EPC bloccato: inventory di sicurezza non univoco; "
                f"atteso={request.expected_epc}, rilevati={detected}"
            )
            self.log(error)
            if barcode is not None:
                self._queue_barcode_write_status("NON ESEGUITA: tag non univoco", "red")
                self._queue_barcode_status(error)
            return

        response = service.write_epc(request)
        for antenna, result in response.data.get("results", {}).items():
            outcome = "OK" if result.get("ok") else f"FAIL {result.get('error')}"
            self.log(f"Write EPC ant{antenna}: {outcome}")
        if not response.ok:
            error = f"Cambio EPC fallito: {self._service_error(response)}"
            self.log(error)
            if barcode is not None:
                self._queue_barcode_write_status("FALLITA", "red")
                self._queue_barcode_status(error)
            return
        if barcode is not None:
            success_antenna = response.data.get("success_antenna")
            self._queue_barcode_write_status(
                f"OK - antenna {success_antenna}", "green"
            )

        verified_tags: list[Tag] = []
        last_found: list[str] = []
        attempts_used = 0
        verification_error = ""
        for attempt in range(1, verification_attempts + 1):
            attempts_used = attempt
            if attempt > 1:
                time.sleep(0.15)
            verification = service.inventory(inventory_request)
            if not verification.ok:
                verification_error = self._service_error(verification)
                continue
            found = list(verification.data.get("unique_epcs", []))
            last_found = found
            if found == [request.new_epc]:
                verified_tags = [
                    self._tag_from_service(dict(item)) for item in verification.data.get("tags", [])
                ]
                break
            verification_error = f"EPC rilevati: {found}"

        if barcode is not None:
            reported_epcs = [request.new_epc] if verified_tags else list(last_found)
            self._post_ui(
                lambda reported_epcs=reported_epcs, attempts_used=attempts_used: (
                    self._set_barcode_verification(
                        barcode,
                        reported_epcs,
                        bool(verified_tags),
                        attempts_used,
                    )
                )
            )

        self.inventory_accumulator.clear()
        self.last_tags = list(verified_tags)
        self._refresh_ant_rssi(verified_tags)
        self.inventory_accumulator.add(verified_tags)
        self._queue_inventory_results(
            self.inventory_accumulator.summaries(),
            verified_tags,
        )
        if verified_tags:
            self.log(f"Cambio EPC verificato: {request.expected_epc} -> {request.new_epc}")
            if barcode is not None:
                self.log(
                    f"Barcode verificato: originale={barcode!r}, "
                    f"HEX={request.new_epc}, riconvertito="
                    f"{epc_hex_to_barcode(request.new_epc)!r}"
                )
            success_message = (
                f"Barcode originale: {barcode}\n"
                f"EPC HEX verificato: {request.new_epc}\n"
                f"Stringa riletta: {epc_hex_to_barcode(request.new_epc)}\n"
                "Confronto: OK"
                if barcode is not None
                else f"Nuovo EPC verificato:\n{request.new_epc}"
            )
            self._post_ui(
                lambda success_message=success_message: messagebox.showinfo(
                    "Barcode EPC completato" if barcode is not None else "Cambio EPC completato",
                    success_message,
                )
            )
        else:
            self.log(
                "EPC scritto ma non verificato tramite inventory: "
                f"{verification_error or 'nessun tag rilevato'}"
            )
            if barcode is not None:
                self.log(
                    f"Barcode non verificato dopo {attempts_used} letture: "
                    f"HEX rilevati={last_found}"
                )
            self._post_ui(
                lambda: messagebox.showwarning(
                    "Verifica EPC non riuscita",
                    "Il comando di scrittura ha risposto OK, ma il nuovo EPC non e' "
                    "stato confermato dall'inventory. Non ripetere la scrittura senza "
                    "prima identificare il tag.",
                )
            )

    def start_verify(self) -> None:
        if not self._guard():
            return
        data = self._hex_bytes(self.write_data.get(), "Dati scrittura")
        if not data:
            return
        password = self._password_bytes()
        if password is not None:
            self._run_async(lambda: self._do_verify(data, password))

    def _do_verify(self, data: bytes, password: bytes) -> None:
        service = self.service
        if service is None:
            self.log("Errore verify: servizio non connesso")
            return
        response = service.verify(
            ReadRequest(
                bank=MemoryBank.USER,
                address=0,
                word_count=len(data) // 2,
                antennas=(1, 2, 3),
                access_password_hex=password.hex(),
                timeout_ms=1000,
            ),
            data.hex(),
        )
        for antenna, result in response.data.get("results", {}).items():
            if result["ok"]:
                self.log(
                    f"Verify ant{antenna}: letto={result['data']} "
                    f"match={result.get('match', False)}"
                )
            else:
                self.log(f"Verify ant{antenna}: FAIL {result['error']}")
        if not response.ok:
            self.log(f"Errore verify: {self._service_error(response)}")

    def start_diag_report(self) -> None:
        if self._guard():
            self._run_async(self._do_diag_report)

    def _do_diag_report(self) -> None:
        try:
            service = self.service
            if service is None:
                raise RuntimeError("servizio non connesso")
            response = service.health()
            report = response.data.get("report")
            if report is None:
                raise RuntimeError(self._service_error(response))
            out_dir = Path(self.cfg.get("logging", {}).get("dir", "logs"))
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"diagnostica_gui_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
            with open(path, "w", encoding="utf-8") as report_file:
                json.dump(report, report_file, indent=2, ensure_ascii=False)
            self.log(f"Report diagnostico salvato: {path} (ok={report['ok']})")
            if not response.ok:
                self.log(f"Diagnostica segnala errore: {self._service_error(response)}")
        except Exception as e:
            self.log(f"Errore report diagnostico: {e}")

    # ------------------------------------------------------------------ #
    # Plot e tabella inventory
    # ------------------------------------------------------------------ #
    def _update_inventory_results(
        self, rows: list[TagReadSummary], plot_tags: list[Tag], record_count: int, evicted_epcs: int
    ) -> None:
        self._update_tag_table(rows, record_count, evicted_epcs)
        self._update_plots(plot_tags)

    def _update_tag_table(
        self, rows: list[TagReadSummary], record_count: int, evicted_epcs: int
    ) -> None:
        for item in self.tag_table.get_children():
            self.tag_table.delete(item)
        for row in rows:
            antennas = ", ".join(str(antenna) for antenna in row.antennas) or "-"
            best = f"{row.best_rssi} dBm" if row.best_rssi is not None else "-"
            last = f"{row.last_rssi} dBm" if row.last_rssi is not None else "-"
            self.tag_table.insert(
                "", "end", values=(row.epc, row.reads, row.observations, antennas, best, last)
            )

        if rows:
            total_reads = sum(row.reads for row in rows)
            evicted_note = f", {evicted_epcs} EPC rimossi dal limite" if evicted_epcs else ""
            self.lbl_inventory_summary.config(
                text=f"{len(rows)} EPC presenti, {total_reads} letture totali "
                f"({record_count} record, {self.inventory_cycles} cicli"
                f"{evicted_note})"
            )
        else:
            self.lbl_inventory_summary.config(
                text=f"Nessun tag presente ({self.inventory_cycles} cicli)"
            )

    def _draw_empty_bar(self):
        ax = self.ax_bar
        ax.clear()
        ax.set_title("Segnale (RSSI) e potenza per antenna")
        ax.set_ylabel("dBm")
        ax.set_ylim(-90, 35)
        ax.axhline(0, color="gray", lw=0.5)
        ax.bar([], [])
        self.canvas.draw()

    def _update_plots(self, tags: list[Tag]):
        # --- bar chart ---
        ax = self.ax_bar
        ax.clear()
        ax.set_title("Segnale (RSSI) e potenza per antenna")
        labels = ["Ant1 base sx", "Ant2 base dx", "Ant3 vert."]
        rssi = [self.ant_rssi.get(i) for i in (1, 2, 3)]
        rssi_bar = [v if v is not None else -90 for v in rssi]
        power = [self.pw_vars[i]["read"].get() for i in (1, 2, 3)]
        x = np.arange(3)
        ax.bar(x - 0.2, rssi_bar, 0.4, label="RSSI letto (dBm)", color="#d62728")
        ax.bar(x + 0.2, power, 0.4, label="Potenza read (dBm)", color="#1f77b4")
        ax.set_xticks(x)
        ax.set_xticklabels(labels)
        ax.set_ylim(-90, 35)
        ax.axhline(0, color="gray", lw=0.5)
        ax.legend(loc="upper left")
        for i, v in enumerate(rssi):
            ax.text(
                i - 0.2,
                (v if v is not None else -90) + 1,
                (f"{v}" if v is not None else "—"),
                ha="center",
                fontsize=8,
            )
        self._draw_volume(tags)

    def _antenna_plate(self, ant_id) -> list:
        """Vertici del piatto antenna (rettangolo) per il disegno 3D."""
        s = ANT_SIDE_CM / 2
        if ant_id in (1, 2):
            cx, cy, cz = ANT_CENTERS[ant_id]
            # piano orizzontale z=0
            return [
                (cx - s, cy - s, 0),
                (cx + s, cy - s, 0),
                (cx + s, cy + s, 0),
                (cx - s, cy + s, 0),
            ]
        else:  # verticale, parete y=0
            cx, cy, cz = ANT_CENTERS[ant_id]
            return [
                (cx - s, 0, cz - s),
                (cx + s, 0, cz - s),
                (cx + s, 0, cz + s),
                (cx - s, 0, cz + s),
            ]

    def _estimate_tag_positions(self, tags: list[Tag]) -> list[tuple[np.ndarray, str]]:
        """Stima posizione (cm) per ogni EPC tramite centroide pesato su RSSI lineare."""
        groups = defaultdict(list)
        for t in tags:
            groups[t.epc].append(t)
        positions = []
        for epc, group in groups.items():
            num = np.zeros(3)
            den = 0.0
            for t in group:
                if t.antenna_id in ANT_CENTERS and t.rssi is not None:
                    w = 10 ** (t.rssi / 10.0)  # potenza lineare (mW)
                    num += w * ANT_CENTERS[t.antenna_id]
                    den += w
            if den > 0:
                pos = num / den
                # clamp dentro il volume
                pos[0] = np.clip(pos[0], -VOL_X / 2, VOL_X / 2)
                pos[1] = np.clip(pos[1], 0, VOL_Y)
                pos[2] = np.clip(pos[2], 0, VOL_Z)
                positions.append((pos, epc))
        return positions

    def _draw_volume(self, tags: list[Tag]):
        ax = self.ax3d
        ax.clear()
        ax.set_title("Volume di lettura (parallelepipedo 44×22×22 cm)")
        ax.set_xlabel("X (cm)")
        ax.set_ylabel("Y (cm)")
        ax.set_zlabel("Z (cm)")

        # box wireframe
        xs = [-VOL_X / 2, VOL_X / 2]
        ys = [0, VOL_Y]
        zs = [0, VOL_Z]
        import itertools

        corners = list(itertools.product(xs, ys, zs))
        edges = [
            (0, 1),
            (0, 2),
            (0, 4),
            (1, 3),
            (1, 5),
            (2, 3),
            (2, 6),
            (3, 7),
            (4, 5),
            (4, 6),
            (5, 7),
            (6, 7),
        ]
        for a, b in edges:
            ax.plot(*zip(corners[a], corners[b], strict=True), color="gray", lw=0.5)

        # piastre antenna colorate per potenza
        for ant in (1, 2, 3):
            plate = self._antenna_plate(ant)
            pw = self.pw_vars[ant]["read"].get()
            col = plt_color_for_power(pw)
            poly = Poly3DCollection([plate], facecolor=col, edgecolor="black", alpha=0.5)
            ax.add_collection3d(poly)
            cx, cy, cz = ANT_CENTERS[ant]
            ax.text(cx, cy, cz, f"A{ant}\n{pw}dBm", fontsize=8, ha="center")

        # tag stimati
        positions = self._estimate_tag_positions(tags)
        for pos, epc in positions:
            ax.scatter(*pos, color="red", s=60)
            ax.text(pos[0], pos[1], pos[2] + 1.5, epc[-8:], fontsize=7, color="darkred")
            # linee da ciascuna antenna che lo ha letto
            for t in [x for x in tags if x.epc == epc and x.antenna_id in ANT_CENTERS]:
                a = ANT_CENTERS[t.antenna_id]
                col = plt_color_for_rssi(t.rssi)
                ax.plot(
                    [a[0], pos[0]], [a[1], pos[1]], [a[2], pos[2]], color=col, lw=1.5, alpha=0.8
                )

        ax.set_xlim(-VOL_X / 2, VOL_X / 2)
        ax.set_ylim(0, VOL_Y)
        ax.set_zlim(0, VOL_Z)
        try:
            ax.set_box_aspect((VOL_X, VOL_Y, VOL_Z))
        except Exception:
            pass
        self.canvas.draw()


def plt_color_for_power(pw_dbm: int) -> str:
    # 5..30 dBm: blu chiaro -> verde -> giallo -> rosso
    f = max(0.0, min(1.0, (pw_dbm - 5) / 25.0))
    if f < 0.5:
        g = 1.0
        b = 1 - 2 * f
        r = 0
    else:
        r = (f - 0.5) * 2
        g = 1 - (f - 0.5) * 2
        b = 0
    return (r, g, b)


def plt_color_for_rssi(rssi: int | None) -> str:
    if rssi is None:
        return "lightgray"
    # -80..0 dBm: rosso -> giallo -> verde
    f = max(0.0, min(1.0, (rssi + 80) / 80.0))
    if f < 0.5:
        r = 1.0
        g = 2 * f
    else:
        r = 1 - (f - 0.5) * 2
        g = 1.0
    return (r, g, 0)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def launch_gui(
    cfg: dict,
    cfg_path: Path,
    *,
    service: RFIDBackend | None = None,
    owns_service: bool | None = None,
    auto_connect: bool | None = None,
) -> None:
    """Apre la GUI standalone o collegata al service condiviso del framework."""
    root = tk.Tk()
    RFIDGui(
        root,
        cfg,
        cfg_path,
        service=service,
        owns_service=owns_service,
        auto_connect=auto_connect,
    )
    root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="GUI SIM7200 RFID")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--debug", action="store_true", help="log DEBUG (dump esadecimale frame TX/RX)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    log_cfg = cfg.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_dir=log_cfg.get("dir", "logs"),
        file_prefix="gui",
        force_debug=args.debug,
    )
    launch_gui(cfg, Path(args.config))


if __name__ == "__main__":
    main()
