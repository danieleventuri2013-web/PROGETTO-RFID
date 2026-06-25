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
import datetime as dt
import json
import logging
import math
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np
import yaml

import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rfid_silion.reader import SIM7200Reader
from rfid_silion.transports import SerialTransport, TcpTransport
from rfid_silion import protocol as P
from rfid_silion.errors import SilionError, NoTagError
from rfid_silion.tags import Tag

# --- Geometria volume (cm) ---------------------------------------------------
# SLP1027: 220 mm di lato. 2 antenne affiancate formano la base (44x22 cm),
# 1 antenna verticale dà l'altezza (22 cm). Volume: 44 x 22 x 22 cm.
ANT_SIDE_CM = 22.0
VOL_X = 2 * ANT_SIDE_CM   # 44 (larghezza, 2 antenne affiancate)
VOL_Y = ANT_SIDE_CM       # 22 (profondità)
VOL_Z = ANT_SIDE_CM       # 22 (altezza, antenna verticale)

# Posizioni dei centri antenna per la stima della posizione del tag (cm).
ANT_CENTERS = {
    1: np.array([-ANT_SIDE_CM / 2, VOL_Y / 2, 0.0]),          # base sx, orizzontale
    2: np.array([+ANT_SIDE_CM / 2, VOL_Y / 2, 0.0]),          # base dx, orizzontale
    3: np.array([0.0,            0.0,       VOL_Z / 2]),      # parete verticale
}

REGIONS = {
    "EU (865-868 MHz)": 0x08,
    "FCC NA (902-928 MHz)": 0x01,
    "China1 (920-925 MHz)": 0x06,
    "Full band (840-960 MHz)": 0xFF,
}


class RFIDGui:
    def __init__(self, root: tk.Tk, cfg: dict, cfg_path: Path):
        self.root = root
        self.cfg = cfg
        self.cfg_path = cfg_path
        self.reader: SIM7200Reader | None = None
        self.connected = False
        self.last_tags: list[Tag] = []
        self.ant_rssi: dict[int, int | None] = {1: None, 2: None, 3: None}
        self._poll_id = None
        self._lock = threading.Lock()

        root.title("SIM7200 RFID — controllo 3 antenne")
        root.geometry("1280x820")

        self._build_connection(root)
        self._build_antenna(root)
        self._build_test(root)
        self._build_plot(root)
        self.log("Pronto. Selezionare il trasporto e connettere.")
        self._populate_from_cfg()

    # ------------------------------------------------------------------ #
    # Costruzione UI
    # ------------------------------------------------------------------ #
    def _build_connection(self, parent):
        f = ttk.LabelFrame(parent, text="Connessione")
        f.pack(fill="x", padx=8, pady=4)

        self.transport_var = tk.StringVar(value="serial")
        ttk.Radiobutton(f, text="Seriale (USB/RS232)", variable=self.transport_var,
                        value="serial", command=self._on_transport_change).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(f, text="TCP/IP (SLD1090)", variable=self.transport_var,
                        value="tcp", command=self._on_transport_change).grid(row=0, column=1, sticky="w")

        # Seriale
        self.ser_frame = ttk.Frame(f)
        self.ser_frame.grid(row=1, column=0, columnspan=2, sticky="we", padx=4)
        ttk.Label(self.ser_frame, text="Porta:").grid(row=0, column=0)
        self.ser_port = ttk.Entry(self.ser_frame, width=18)
        self.ser_port.grid(row=0, column=1)
        ttk.Label(self.ser_frame, text="Baud:").grid(row=0, column=2)
        self.ser_baud = ttk.Entry(self.ser_frame, width=8)
        self.ser_baud.grid(row=0, column=3)
        ttk.Label(self.ser_frame, text="Timeout(s):").grid(row=0, column=4)
        self.ser_timeout = ttk.Entry(self.ser_frame, width=5)
        self.ser_timeout.grid(row=0, column=5)

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
        ttk.OptionMenu(f, self.region_var, self.region_var.get(), *REGIONS.keys()).grid(row=0, column=1, sticky="w")

        ttk.Label(f, text="Read dBm").grid(row=0, column=3)
        ttk.Label(f, text="Write dBm").grid(row=0, column=5)
        self.pw_vars = {}        # {ant: {"read": IntVar, "write": IntVar}}
        self.pw_labels = {}
        roles = {1: "Base sx (orizz.)", 2: "Base dx (orizz.)", 3: "Parete (verticale)"}
        for i, ant in enumerate([1, 2, 3], start=1):
            ttk.Label(f, text=f"Ant {ant} ({roles[ant]})").grid(row=i, column=0, sticky="w")
            rv = tk.IntVar(value=20)
            wv = tk.IntVar(value=20)
            self.pw_vars[ant] = {"read": rv, "write": wv}
            r = ttk.Scale(f, from_=5, to=30, variable=rv, orient="horizontal",
                          command=lambda e, a=ant: self._on_pw_change(a))
            r.grid(row=i, column=2, sticky="we", padx=4)
            rl = ttk.Label(f, text="20")
            rl.grid(row=i, column=3); self.pw_labels[(ant, "read")] = rl
            w = ttk.Scale(f, from_=5, to=30, variable=wv, orient="horizontal",
                          command=lambda e, a=ant: self._on_pw_change(a))
            w.grid(row=i, column=4, sticky="we", padx=4)
            wl = ttk.Label(f, text="20")
            wl.grid(row=i, column=5); self.pw_labels[(ant, "write")] = wl
        f.columnconfigure(2, weight=1)
        f.columnconfigure(4, weight=1)

        self.btn_apply = ttk.Button(f, text="Applica regione + potenze", command=self.apply_settings)
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

        self.btn_inv = ttk.Button(f, text="Inventory", command=lambda: self._run_async(self.do_inventory))
        self.btn_inv.grid(row=1, column=0, pady=4)
        self.btn_read = ttk.Button(f, text="Read USER", command=lambda: self._run_async(self.do_read))
        self.btn_read.grid(row=1, column=1)
        self.btn_write = ttk.Button(f, text="Write USER", command=lambda: self._run_async(self.do_write))
        self.btn_write.grid(row=1, column=2)
        self.btn_verify = ttk.Button(f, text="Read + Verify", command=lambda: self._run_async(self.do_verify))
        self.btn_verify.grid(row=1, column=3)

        self.poll_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text="Polling continuo (1s)", variable=self.poll_var,
                        command=self.toggle_poll).grid(row=1, column=4, columnspan=2)

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

    def _on_pw_change(self, ant):
        for k in ("read", "write"):
            v = self.pw_vars[ant][k].get()
            self.pw_labels[(ant, k)].config(text=str(v))

    def _populate_from_cfg(self):
        s = self.cfg.get("serial", {})
        self.ser_port.insert(0, s.get("port", "COM3"))
        self.ser_baud.insert(0, str(s.get("baudrate", 115200)))
        self.ser_timeout.insert(0, str(s.get("timeout_s", 2.0)))
        t = self.cfg.get("tcp", {})
        self.tcp_host.insert(0, t.get("host", "192.168.1.100"))
        self.tcp_port.insert(0, str(t.get("port", 8080)))
        self.tcp_timeout.insert(0, str(t.get("timeout_s", 2.0)))
        if "tcp" in self.cfg and "serial" not in self.cfg:
            self.transport_var.set("tcp"); self._on_transport_change()
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
                self.region_var.set(name); break

    def log(self, msg: str):
        ts = dt.datetime.now().strftime("%H:%M:%S")
        self.root.after(0, lambda: self._log_insert(f"[{ts}] {msg}"))

    def _log_insert(self, line: str):
        self.txt_log.config(state="normal")
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        self.txt_log.config(state="disabled")

    def _set_status(self, connected: bool):
        self.connected = connected
        if connected:
            self.lbl_status.config(text="● connesso", foreground="green")
            self.btn_connect.config(text="Disconnetti")
        else:
            self.lbl_status.config(text="● non connesso", foreground="red")
            self.btn_connect.config(text="Connetti")

    def _run_async(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Connessione
    # ------------------------------------------------------------------ #
    def _build_reader(self) -> SIM7200Reader:
        if self.transport_var.get() == "serial":
            t = SerialTransport(
                port=self.ser_port.get(),
                baudrate=int(self.ser_baud.get() or 115200),
                timeout_s=float(self.ser_timeout.get() or 2.0),
            )
        else:
            t = TcpTransport(
                host=self.tcp_host.get(),
                port=int(self.tcp_port.get() or 8080),
                timeout_s=float(self.tcp_timeout.get() or 2.0),
            )
        return SIM7200Reader(t)

    def toggle_connect(self):
        if self.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        try:
            self.reader = self._build_reader()
            self.reader.open()
            info = self.reader.boot_firmware()
            self.log(f"Connesso. FW {info['firmware_version']} HW {info['hardware_version']}")
            self._set_status(True)
        except Exception as e:
            self.log(f"Errore connessione: {e}")
            messagebox.showerror("Connessione", str(e))
            self.reader = None

    def _disconnect(self):
        if self.poll_var.get():
            self.poll_var.set(False); self.toggle_poll()
        if self.reader:
            self.reader.close()
        self.reader = None
        self._set_status(False)
        self.log("Disconnesso.")

    # ------------------------------------------------------------------ #
    # Impostazioni
    # ------------------------------------------------------------------ #
    def apply_settings(self):
        if not self.connected:
            messagebox.showwarning("Attenzione", "Connettere prima il lettore.")
            return
        def _do():
            try:
                region = REGIONS[self.region_var.get()]
                self.reader.set_region(region)
                powers = [(ant, self.pw_vars[ant]["read"].get() * 100,
                                self.pw_vars[ant]["write"].get() * 100) for ant in (1, 2, 3)]
                self.reader.set_antennas_power(powers)
                self.log(f"Regione+potenze applicate: {powers}")
            except Exception as e:
                self.log(f"Errore impostazioni: {e}")
        self._run_async(_do)

    def save_config(self):
        cfg = {
            "serial": {
                "port": self.ser_port.get(),
                "baudrate": int(self.ser_baud.get() or 115200),
                "timeout_s": float(self.ser_timeout.get() or 2.0),
            },
            "reader": {"region": REGIONS[self.region_var.get()]},
            "antennas": [
                {"id": ant, "role": r,
                 "read_power": self.pw_vars[ant]["read"].get() * 100,
                 "write_power": self.pw_vars[ant]["write"].get() * 100}
                for ant, r in [(1, "floor"), (2, "floor"), (3, "wall_90")]
            ],
        }
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        self.log(f"Config salvata in {self.cfg_path}")

    # ------------------------------------------------------------------ #
    # Operazioni tag
    # ------------------------------------------------------------------ #
    def _guard(self) -> bool:
        if not self.connected or not self.reader:
            messagebox.showwarning("Attenzione", "Connettere prima il lettore.")
            return False
        return True

    def do_inventory(self):
        if not self._guard():
            return
        try:
            self.reader.set_antennas_for_inventory([(a, a) for a in (1, 2, 3)])
            tags = self.reader.inventory(timeout_ms=1000, metadata_flags=0x0007)
            self.last_tags = tags
            # mappa antenna -> rssi più forte
            self.ant_rssi = {1: None, 2: None, 3: None}
            for t in tags:
                if t.antenna_id in self.ant_rssi:
                    cur = self.ant_rssi[t.antenna_id]
                    if cur is None or (t.rssi is not None and t.rssi > cur):
                        self.ant_rssi[t.antenna_id] = t.rssi
            n = len(tags)
            self.log(f"Inventory: {n} tag. RSSI/ant: {self.ant_rssi}")
            self.root.after(0, lambda: self._update_plots(tags))
        except NoTagError:
            self.last_tags = []
            self.ant_rssi = {1: None, 2: None, 3: None}
            self.log("Inventory: nessun tag.")
            self.root.after(0, lambda: self._update_plots([]))
        except Exception as e:
            self.log(f"Errore inventory: {e}")

    def do_read(self):
        if not self._guard():
            return
        try:
            pwd = bytes.fromhex(self.acc_pwd.get() or "00000000")
            words = int(self.read_words.get() or 2)
            res = self.reader.read_try_all_antennas([1, 2, 3], P.BANK_USER, 0, words, pwd, 1000)
            for ant, r in res.items():
                if r["ok"]:
                    self.log(f"Read USER ant{ant}: {r['data'].hex().upper()}")
                else:
                    self.log(f"Read USER ant{ant}: FAIL {r['error']}")
        except Exception as e:
            self.log(f"Errore read: {e}")

    def do_write(self):
        if not self._guard():
            return
        try:
            data = bytes.fromhex(self.write_data.get())
            pwd = bytes.fromhex(self.acc_pwd.get() or "00000000")
            res = self.reader.write_try_all_antennas([1, 2, 3], P.BANK_USER, 0, data, pwd, 1000)
            for ant, r in res.items():
                self.log(f"Write USER ant{ant}: {'OK' if r['ok'] else 'FAIL '+r['error']}")
        except Exception as e:
            self.log(f"Errore write: {e}")

    def do_verify(self):
        if not self._guard():
            return
        self.do_read()
        # confronto
        data = bytes.fromhex(self.write_data.get())
        try:
            pwd = bytes.fromhex(self.acc_pwd.get() or "00000000")
            res = self.reader.read_try_all_antennas([1, 2, 3], P.BANK_USER, 0, len(data)//2, pwd, 1000)
            for ant, r in res.items():
                if r["ok"]:
                    match = r["data"].hex().upper() == data.hex().upper()
                    self.log(f"Verify ant{ant}: letto={r['data'].hex().upper()} match={match}")
        except Exception as e:
            self.log(f"Errore verify: {e}")

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #
    def toggle_poll(self):
        if self.poll_var.get():
            self._poll_loop()
        else:
            if self._poll_id:
                self.root.after_cancel(self._poll_id); self._poll_id = None

    def _poll_loop(self):
        if not self.poll_var.get():
            return
        self._run_async(self.do_inventory)
        self._poll_id = self.root.after(1000, self._poll_loop)

    # ------------------------------------------------------------------ #
    # Plot
    # ------------------------------------------------------------------ #
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
        ax.set_xticks(x); ax.set_xticklabels(labels)
        ax.set_ylim(-90, 35)
        ax.axhline(0, color="gray", lw=0.5)
        ax.legend(loc="upper left")
        for i, v in enumerate(rssi):
            ax.text(i - 0.2, (v if v is not None else -90) + 1,
                    (f"{v}" if v is not None else "—"), ha="center", fontsize=8)
        self._draw_volume(tags)

    def _antenna_plate(self, ant_id) -> list:
        """Vertici del piatto antenna (rettangolo) per il disegno 3D."""
        s = ANT_SIDE_CM / 2
        if ant_id in (1, 2):
            cx, cy, cz = ANT_CENTERS[ant_id]
            # piano orizzontale z=0
            return [(cx - s, cy - s, 0), (cx + s, cy - s, 0),
                    (cx + s, cy + s, 0), (cx - s, cy + s, 0)]
        else:  # verticale, parete y=0
            cx, cy, cz = ANT_CENTERS[ant_id]
            return [(cx - s, 0, cz - s), (cx + s, 0, cz - s),
                    (cx + s, 0, cz + s), (cx - s, 0, cz + s)]

    def _estimate_tag_positions(self, tags: list[Tag]) -> list[tuple[np.ndarray, str]]:
        """Stima posizione (cm) per ogni EPC tramite centroide pesato su RSSI lineare."""
        groups = defaultdict(list)
        for t in tags:
            groups[t.epc].append(t)
        positions = []
        for epc, group in groups.items():
            num = np.zeros(3); den = 0.0
            for t in group:
                if t.antenna_id in ANT_CENTERS and t.rssi is not None:
                    w = 10 ** (t.rssi / 10.0)  # potenza lineare (mW)
                    num += w * ANT_CENTERS[t.antenna_id]
                    den += w
            if den > 0:
                pos = num / den
                # clamp dentro il volume
                pos[0] = np.clip(pos[0], -VOL_X/2, VOL_X/2)
                pos[1] = np.clip(pos[1], 0, VOL_Y)
                pos[2] = np.clip(pos[2], 0, VOL_Z)
                positions.append((pos, epc))
        return positions

    def _draw_volume(self, tags: list[Tag]):
        ax = self.ax3d
        ax.clear()
        ax.set_title("Volume di lettura (parallelepipedo 44×22×22 cm)")
        ax.set_xlabel("X (cm)"); ax.set_ylabel("Y (cm)"); ax.set_zlabel("Z (cm)")

        # box wireframe
        xs = [-VOL_X/2, VOL_X/2]; ys = [0, VOL_Y]; zs = [0, VOL_Z]
        import itertools
        corners = list(itertools.product(xs, ys, zs))
        edges = [(0,1),(0,2),(0,4),(1,3),(1,5),(2,3),(2,6),(3,7),(4,5),(4,6),(5,7),(6,7)]
        for a, b in edges:
            ax.plot(*zip(corners[a], corners[b]), color="gray", lw=0.5)

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
                ax.plot([a[0], pos[0]], [a[1], pos[1]], [a[2], pos[2]],
                        color=col, lw=1.5, alpha=0.8)

        ax.set_xlim(-VOL_X/2, VOL_X/2); ax.set_ylim(0, VOL_Y); ax.set_zlim(0, VOL_Z)
        try:
            ax.set_box_aspect((VOL_X, VOL_Y, VOL_Z))
        except Exception:
            pass
        self.canvas.draw()


def plt_color_for_power(pw_dbm: int) -> str:
    # 5..30 dBm: blu chiaro -> verde -> giallo -> rosso
    f = max(0.0, min(1.0, (pw_dbm - 5) / 25.0))
    if f < 0.5:
        g = 1.0; b = 1 - 2*f; r = 0
    else:
        r = (f - 0.5) * 2; g = 1 - (f - 0.5) * 2; b = 0
    return (r, g, b)


def plt_color_for_rssi(rssi: int | None) -> str:
    if rssi is None:
        return "lightgray"
    # -80..0 dBm: rosso -> giallo -> verde
    f = max(0.0, min(1.0, (rssi + 80) / 80.0))
    if f < 0.5:
        r = 1.0; g = 2*f; b = 0
    else:
        r = 1 - (f - 0.5) * 2; g = 1.0; b = 0
    return (r, g, 0)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    ap = argparse.ArgumentParser(description="GUI SIM7200 RFID")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    args = ap.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = tk.Tk()
    RFIDGui(root, cfg, Path(args.config))
    root.mainloop()


if __name__ == "__main__":
    main()
