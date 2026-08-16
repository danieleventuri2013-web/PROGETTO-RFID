"""Tracciabilita' campioni: accettazione, sigillo, ricezione.

Tre schermate, che corrispondono ai tre momenti reali del percorso di un
campione istologico:

* **Accettazione** — si registra il paziente, l'accettazione e il reperto, si
  decide in quanti contenitori va diviso, e si scrive un tag per contenitore.
  La scrittura e' guidata: un contenitore alla volta davanti all'antenna.
* **Sigillo e spedizione** — si chiude la scatola, si certifica il contenuto con
  piu' passate di lettura e si esporta la distinta cifrata. Il conteggio e'
  mostrato in grande perche' l'operatore la numerosita' la vede a occhio e deve
  poterla confrontare in un colpo.
* **Ricezione** — si importa la distinta, si posa la scatola nel volume e si
  riconcilia: mancanti e inattesi contano allo stesso modo.

Come la GUI di collaudo, questa applicazione e' un client del servizio: ogni
operazione RFID passa da `RFIDService` su un unico worker, e le callback tornano
all'interfaccia attraverso una coda posseduta da Tk. I worker non chiamano mai
direttamente widget Tkinter.

Uso:
  python run.py lims
  python src/app/lims_gui.py --config src/app/config.yaml
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import queue
import sys
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lims.codec import FIXATIVES, MATERIALS, SITES, SpecimenFlags, TagPayload
from lims.crypto import (
    Keyring,
    PayloadAuthenticationError,
    PayloadFormatError,
    UnknownKeyError,
    max_plaintext_bytes,
)
from lims.db import LimsDatabase
from lims.manifest import build_manifest, open_manifest, reconcile, seal_manifest
from lims.model import (
    Case,
    ContainerState,
    Patient,
    Sex,
    Shipment,
    ShipmentState,
    Specimen,
    validate_codice_fiscale,
)
from lims.sealing import ClosureProof, SealingPolicy, SealingSession, default_passes
from lims.tagio import TagIO
from rfid_silion.diagnostics import setup_logging
from rfid_silion.service import AntennaPower, ReaderSettings, RFIDService

log = logging.getLogger("lims.gui")

_STATO_ETICHETTE = {
    "decodificato": "letto",
    "solo_epc": "solo EPC",
    "chiave_mancante": "chiave mancante",
    "non_autenticato": "NON AUTENTICATO",
    "illeggibile": "illeggibile",
    "estraneo": "tag estraneo",
}


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _codebook_values(codebook: dict[int, str]) -> list[str]:
    return [f"{codice} — {nome}" for codice, nome in sorted(codebook.items())]


def _codebook_code(value: str) -> int:
    return int(str(value).split("—")[0].strip() or 0)


def _reader_settings(cfg: dict) -> ReaderSettings:
    return ReaderSettings(
        region=cfg.get("reader", {}).get("region", 0x08),
        powers=tuple(
            AntennaPower(
                antenna_id=antenna["id"],
                read_power_cdbm=antenna.get("read_power", 2000),
                write_power_cdbm=antenna.get("write_power", 2000),
            )
            for antenna in cfg.get("antennas", [])
        ),
    )


class LimsApp:
    """Finestra principale con le due schermate."""

    def __init__(self, root: tk.Tk, cfg: dict, *, service=None, owns_service: bool = True):
        self.root = root
        self.cfg = cfg
        self.lims_cfg = cfg.get("lims", {}) or {}
        self.owns_service = owns_service
        self.service = service or RFIDService(cfg)

        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lims")
        self._ui_queue: queue.Queue = queue.Queue()
        self._destroyed = False
        self._closing = False
        self._busy = False

        self.db = LimsDatabase(self.lims_cfg.get("database", "logs/lims.db"))
        self.keyring = self._load_keyring()

        # Contenitori pianificati e non ancora scritti, nella schermata di accettazione.
        self._pending: list[dict] = []
        self._specimen_id: int | None = None
        self._accession_id: int | None = None
        # Il conteggio si conferma una volta sola per accettazione, alla prima
        # scrittura: da li' in poi il totale e' nei tag.
        self._conteggio_confermato = False

        root.title("Tracciabilita' campioni — RFID")
        root.geometry("1080x720")
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(50, self._drain_ui_queue)

    # -- infrastruttura ----------------------------------------------------
    def _load_keyring(self) -> Keyring:
        percorso = Path(self.lims_cfg.get("keyring", "logs/lims_keys.json"))
        if percorso.exists():
            return Keyring.load(percorso)
        # Prima esecuzione: si genera una chiave e la si salva. Va poi consegnata
        # al laboratorio destinatario fuori banda, una volta sola.
        portachiavi = Keyring()
        portachiavi.generate(0)
        portachiavi.save(percorso)
        log.warning(
            "Creato un nuovo portachiavi in %s: va consegnato ai laboratori "
            "destinatari perche' possano leggere i tag",
            percorso,
        )
        return portachiavi

    def _tagio(self, *, writing: bool) -> TagIO:
        antenne = self.lims_cfg.get("write_antennas" if writing else "read_antennas")
        if not antenne:
            antenne = [1] if writing else [antenna["id"] for antenna in self.cfg.get("antennas", [])]
        accesso = self.cfg.get("tag_access", {}) or {}
        return TagIO(
            self.service,
            self.keyring,
            lab_id=int(self.lims_cfg.get("lab_id", 0)),
            antennas=tuple(antenne),
            access_password_hex=accesso.get("access_password_hex", "00000000"),
            timeout_ms=accesso.get("timeout_ms", 1000),
            user_memory_bytes=int(self.lims_cfg.get("user_memory_bytes", 64)),
            db=self.db,
            # Copia semplice del campo, non la variabile Tk: questo metodo viene
            # chiamato dal worker, e leggere un widget da li' non e' consentito.
            operator=self._operatore,
        )

    def _post_ui(self, callback) -> None:
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

    def _submit(self, funzione) -> None:
        """Esegue sul worker, con la finestra bloccata finche' non ha finito."""
        if self._busy:
            self.log("Operazione gia' in corso, attendere.")
            return
        self._busy = True
        self._set_busy(True)

        def compito():
            try:
                funzione()
            except Exception as exc:  # noqa: BLE001
                log.exception("Operazione fallita")
                self.log(f"ERRORE: {exc}")
            finally:
                self._post_ui(lambda: self._set_busy(False))
                self._busy = False

        self._executor.submit(compito)

    def _set_busy(self, busy: bool) -> None:
        stato = "disabled" if busy else "normal"
        for pulsante in self._buttons:
            try:
                pulsante.configure(state=stato)
            except tk.TclError:
                pass
        self.stato_var.set("operazione in corso..." if busy else "pronto")

    def log(self, messaggio: str) -> None:
        riga = f"[{dt.datetime.now():%H:%M:%S}] {messaggio}"
        self._post_ui(lambda: self._log_insert(riga))

    def _log_insert(self, riga: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", riga + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- costruzione dell'interfaccia --------------------------------------
    def _build_ui(self) -> None:
        self._buttons: list[ttk.Button] = []
        self.stato_var = tk.StringVar(value="non connesso")
        self.operatore = tk.StringVar(value="")
        # Il worker non puo' leggere una variabile Tk: se ne tiene una copia,
        # aggiornata qui sul thread dell'interfaccia a ogni battitura.
        self._operatore = ""
        self.operatore.trace_add(
            "write", lambda *_: setattr(self, "_operatore", self.operatore.get().strip())
        )

        barra = ttk.Frame(self.root, padding=6)
        barra.pack(fill="x")
        ttk.Label(barra, text="Operatore:").pack(side="left")
        ttk.Entry(barra, textvariable=self.operatore, width=14).pack(side="left", padx=(4, 12))
        self._button(barra, "Connetti", self._connetti).pack(side="left")
        self._button(barra, "Disconnetti", self._disconnetti).pack(side="left", padx=4)
        ttk.Label(barra, textvariable=self.stato_var).pack(side="right")

        note = ttk.Notebook(self.root)
        note.pack(fill="both", expand=True, padx=6)
        self._build_accettazione(note)
        self._build_sigillo(note)
        self._build_ricezione(note)

        self.log_text = tk.Text(self.root, height=8, state="disabled", wrap="none")
        self.log_text.pack(fill="both", padx=6, pady=6)

    def _button(self, parent, testo: str, comando) -> ttk.Button:
        pulsante = ttk.Button(parent, text=testo, command=comando)
        self._buttons.append(pulsante)
        return pulsante

    def _build_accettazione(self, note: ttk.Notebook) -> None:
        pagina = ttk.Frame(note, padding=8)
        note.add(pagina, text="Accettazione")

        modulo = ttk.LabelFrame(pagina, text="Paziente e reperto", padding=8)
        modulo.pack(fill="x")

        self.cf = tk.StringVar()
        self.cognome = tk.StringVar()
        self.nome = tk.StringVar()
        self.reparto = tk.StringVar()
        self.medico = tk.StringVar()
        self.descrizione = tk.StringVar()
        self.contenitori = tk.IntVar(value=1)
        self.materiale = tk.StringVar(value=_codebook_values(MATERIALS)[1])
        self.fissativo = tk.StringVar(value=_codebook_values(FIXATIVES)[1])
        self.sede = tk.StringVar(value=_codebook_values(SITES)[0])
        self.urgente = tk.BooleanVar(value=False)
        self.infettivo = tk.BooleanVar(value=False)

        campi = (
            ("Codice fiscale", self.cf, 20),
            ("Cognome", self.cognome, 20),
            ("Nome", self.nome, 20),
            ("Reparto", self.reparto, 20),
            ("Medico", self.medico, 20),
            ("Descrizione reperto", self.descrizione, 44),
        )
        for riga, (etichetta, variabile, larghezza) in enumerate(campi):
            ttk.Label(modulo, text=etichetta + ":").grid(row=riga, column=0, sticky="e", pady=2)
            ttk.Entry(modulo, textvariable=variabile, width=larghezza).grid(
                row=riga, column=1, sticky="w", padx=6
            )

        scelte = (
            ("Materiale", self.materiale, _codebook_values(MATERIALS)),
            ("Fissativo", self.fissativo, _codebook_values(FIXATIVES)),
            ("Sede", self.sede, _codebook_values(SITES)),
        )
        for indice, (etichetta, variabile, valori) in enumerate(scelte):
            ttk.Label(modulo, text=etichetta + ":").grid(row=indice, column=2, sticky="e", padx=6)
            ttk.Combobox(
                modulo, textvariable=variabile, values=valori, width=28, state="readonly"
            ).grid(row=indice, column=3, sticky="w")

        ttk.Label(modulo, text="Contenitori:").grid(row=3, column=2, sticky="e", padx=6)
        riga_conteggio = ttk.Frame(modulo)
        riga_conteggio.grid(row=3, column=3, sticky="w")
        ttk.Spinbox(
            riga_conteggio, from_=1, to=255, textvariable=self.contenitori, width=6
        ).pack(side="left")
        self._button(riga_conteggio, "Correggi", self._correggi_conteggio).pack(
            side="left", padx=4
        )
        # Il totale finisce dentro l'EPC e dentro il payload: sui contenitori
        # gia' scritti non e' piu' correggibile, perche' il tag non si riscrive.
        avviso = ttk.Label(
            modulo,
            text="Contare i campioni prima di scrivere: il totale finisce nel tag\n"
            "e sui contenitori gia' scritti non e' piu' correggibile.",
            foreground="#b00020",
            justify="left",
        )
        avviso.grid(row=6, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Checkbutton(modulo, text="Urgente", variable=self.urgente).grid(
            row=4, column=3, sticky="w"
        )
        ttk.Checkbutton(modulo, text="Rischio biologico", variable=self.infettivo).grid(
            row=5, column=3, sticky="w"
        )

        comandi = ttk.Frame(pagina, padding=(0, 8))
        comandi.pack(fill="x")
        self._button(comandi, "Registra accettazione", self._registra).pack(side="left")
        self._button(comandi, "Scrivi il prossimo contenitore", self._scrivi_prossimo).pack(
            side="left", padx=6
        )
        self._button(comandi, "Svuota", self._svuota_modulo).pack(side="left")

        elenco = ttk.LabelFrame(pagina, text="Contenitori dell'accettazione", padding=6)
        elenco.pack(fill="both", expand=True)
        self.tabella_contenitori = ttk.Treeview(
            elenco,
            columns=("contenitore", "stato", "epc", "tid"),
            show="headings",
            height=8,
        )
        for colonna, titolo, larghezza in (
            ("contenitore", "Contenitore", 100),
            ("stato", "Stato", 120),
            ("epc", "EPC", 260),
            ("tid", "TID", 260),
        ):
            self.tabella_contenitori.heading(colonna, text=titolo)
            self.tabella_contenitori.column(colonna, width=larghezza, anchor="w")
        self.tabella_contenitori.pack(fill="both", expand=True)

    def _build_sigillo(self, note: ttk.Notebook) -> None:
        """Chiusura della scatola e certificazione del contenuto."""
        pagina = ttk.Frame(note, padding=8)
        note.add(pagina, text="Sigillo e spedizione")

        testa = ttk.Frame(pagina)
        testa.pack(fill="x")
        ttk.Label(testa, text="Destinazione:").pack(side="left")
        self.destinazione = tk.StringVar(value="Laboratorio Centrale")
        ttk.Entry(testa, textvariable=self.destinazione, width=28).pack(side="left", padx=4)
        self._button(testa, "Prepara spedizione", self._prepara_spedizione).pack(side="left")
        self._button(testa, "Sigilla e certifica", self._sigilla).pack(side="left", padx=6)
        self._button(testa, "Esporta distinta", self._esporta_distinta).pack(side="left")

        # Il conteggio in grande: l'operatore la numerosita' la vede a occhio, e
        # deve poterla confrontare in un colpo senza leggere un elenco.
        self.conteggio_var = tk.StringVar(value="— / —")
        self.verdetto_var = tk.StringVar(value="nessuna spedizione preparata")
        riquadro = ttk.Frame(pagina, padding=(0, 10))
        riquadro.pack(fill="x")
        self.conteggio_label = ttk.Label(
            riquadro, textvariable=self.conteggio_var, font=("", 48, "bold")
        )
        self.conteggio_label.pack(side="left", padx=(0, 16))
        ttk.Label(riquadro, textvariable=self.verdetto_var, font=("", 12)).pack(
            side="left", anchor="s", pady=10
        )

        self.tabella_sigillo = ttk.Treeview(
            pagina,
            columns=("stato", "contenitore", "paziente", "rilevamento", "antenne"),
            show="headings",
            height=10,
        )
        for colonna, titolo, larghezza in (
            ("stato", "Stato", 130),
            ("contenitore", "Cont.", 70),
            ("paziente", "Paziente", 300),
            ("rilevamento", "Rilevamento", 110),
            ("antenne", "Antenne", 90),
        ):
            self.tabella_sigillo.heading(colonna, text=titolo)
            self.tabella_sigillo.column(colonna, width=larghezza, anchor="w")
        self.tabella_sigillo.pack(fill="both", expand=True, pady=6)
        self.tabella_sigillo.tag_configure("mancante", foreground="#b00020")
        self.tabella_sigillo.tag_configure("estraneo", foreground="#8a6d00")

        self._shipment_id: int | None = None
        self._sealing_record = None

    def _build_ricezione(self, note: ttk.Notebook) -> None:
        pagina = ttk.Frame(note, padding=8)
        note.add(pagina, text="Ricezione")

        comandi = ttk.Frame(pagina)
        comandi.pack(fill="x")
        self._button(comandi, "Importa distinta", self._importa_distinta).pack(side="left")
        self._button(comandi, "Leggi il contenuto del volume", self._leggi_volume).pack(
            side="left", padx=6
        )
        self.esito_var = tk.StringVar(value="")
        ttk.Label(comandi, textvariable=self.esito_var, font=("", 10, "bold")).pack(
            side="left", padx=12
        )
        #: Distinta ricevuta dal mittente. Senza, i mancanti si deducono dalla
        #: sola numerazione degli EPC letti — e un'accettazione di cui non arriva
        #: nessun contenitore resterebbe invisibile.
        self._distinta = None

        self.tabella_ricezione = ttk.Treeview(
            pagina,
            columns=("stato", "accettazione", "contenitore", "paziente", "materiale", "rssi"),
            show="headings",
            height=12,
        )
        for colonna, titolo, larghezza in (
            ("stato", "Stato", 130),
            ("accettazione", "Accettazione", 100),
            ("contenitore", "Cont.", 60),
            ("paziente", "Paziente / codice fiscale", 320),
            ("materiale", "Materiale", 180),
            ("rssi", "RSSI", 60),
        ):
            self.tabella_ricezione.heading(colonna, text=titolo)
            self.tabella_ricezione.column(colonna, width=larghezza, anchor="w")
        self.tabella_ricezione.pack(fill="both", expand=True, pady=6)
        self.tabella_ricezione.tag_configure("problema", foreground="#b00020")
        self.tabella_ricezione.tag_configure("mancante", foreground="#b00020")

        mancanti = ttk.LabelFrame(pagina, text="Contenitori mancanti", padding=6)
        mancanti.pack(fill="x")
        self.elenco_mancanti = tk.Listbox(mancanti, height=4)
        self.elenco_mancanti.pack(fill="x")

    # -- lifecycle del servizio --------------------------------------------
    def _connetti(self) -> None:
        def compito():
            avvio = self.service.start()
            if not avvio.ok:
                self.log(f"Connessione fallita: {(avvio.error or {}).get('message')}")
                self._post_ui(lambda: self.stato_var.set("non connesso"))
                return
            self.service.configure(_reader_settings(self.cfg))
            self.log("Lettore connesso e configurato.")

        self._submit(compito)

    def _disconnetti(self) -> None:
        def compito():
            self.service.stop()
            self.log("Lettore disconnesso.")

        self._submit(compito)

    # -- accettazione -------------------------------------------------------
    def _flags(self) -> SpecimenFlags:
        flags = SpecimenFlags.NONE
        if self.urgente.get():
            flags |= SpecimenFlags.URGENT
        if self.infettivo.get():
            flags |= SpecimenFlags.INFECTIOUS
        return flags

    def _registra(self) -> None:
        """Registra paziente, accettazione e reperto, e pianifica i contenitori."""
        try:
            paziente = Patient(
                codice_fiscale=validate_codice_fiscale(self.cf.get()),
                cognome=self.cognome.get(),
                nome=self.nome.get(),
                sesso=Sex.UNKNOWN,
            )
        except (ValueError, TypeError) as exc:
            messagebox.showerror("Dati non validi", str(exc))
            return

        totale = int(self.contenitori.get())
        try:
            patient_id = self.db.upsert_patient(paziente)
            accession_id = self.db.next_accession_id()
            case_id = self.db.create_case(
                Case(
                    accession_id=accession_id,
                    patient_id=patient_id,
                    data_prelievo=dt.date.today(),
                    reparto=self.reparto.get().strip(),
                    medico=self.medico.get().strip(),
                )
            )
            specimen_id = self.db.add_specimen(
                Specimen(
                    case_id=case_id,
                    descrizione=self.descrizione.get().strip(),
                    material_code=_codebook_code(self.materiale.get()),
                    fixative_code=_codebook_code(self.fissativo.get()),
                    site_code=_codebook_code(self.sede.get()),
                )
            )
            container_ids = self.db.plan_containers(specimen_id, totale)
            self._specimen_id = specimen_id
            self._accession_id = accession_id
            self._conteggio_confermato = False
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Registrazione fallita", str(exc))
            return

        self._pending = [
            {
                "container_id": container_id,
                "index": indice,
                "total": totale,
                "accession_id": accession_id,
                "payload": TagPayload(
                    codice_fiscale=paziente.codice_fiscale,
                    accession_id=accession_id,
                    container_index=indice,
                    container_total=totale,
                    display_name=paziente.display_name,
                    data_prelievo=dt.date.today(),
                    material_code=_codebook_code(self.materiale.get()),
                    fixative_code=_codebook_code(self.fissativo.get()),
                    site_code=_codebook_code(self.sede.get()),
                    flags=self._flags(),
                ),
            }
            for indice, container_id in enumerate(container_ids, start=1)
        ]
        self._aggiorna_tabella_contenitori()
        self.log(
            f"Accettazione {accession_id} registrata: {totale} contenitore/i da scrivere. "
            "Mettere il primo davanti all'antenna di scrittura."
        )

    def _aggiorna_tabella_contenitori(self) -> None:
        self.tabella_contenitori.delete(*self.tabella_contenitori.get_children())
        for voce in self._pending:
            self.tabella_contenitori.insert(
                "",
                "end",
                values=(
                    f"{voce['index']}/{voce['total']}",
                    voce.get("stato", "da scrivere"),
                    voce.get("epc", ""),
                    voce.get("tid", ""),
                ),
            )

    def _correggi_conteggio(self) -> None:
        """Cambia il numero di contenitori finche' e' ancora possibile."""
        if self._specimen_id is None:
            messagebox.showinfo(
                "Nessuna accettazione",
                "Registrare prima un'accettazione: il conteggio si corregge dopo.",
            )
            return
        try:
            esito = self.db.adjust_container_count(self._specimen_id, int(self.contenitori.get()))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Correzione non possibile", str(exc))
            return

        self._ricarica_pending()
        self.log(
            f"Numero contenitori portato a {esito.new_total} "
            f"(+{len(esito.added)} / -{len(esito.removed)})."
        )
        if esito.has_stale_tags:
            elenco = "\n".join(esito.describe()["tag_con_totale_superato"])
            messagebox.showwarning(
                "Tag gia' scritti con il vecchio totale",
                "Questi contenitori portano nel chip un totale ormai superato, e il "
                "tag non si riscrive:\n\n"
                f"{elenco}\n\n"
                "La distinta in archivio resta l'autorita' per il sigillo e per la "
                "spedizione. Se serve allinearli, vanno annullati e rifatti con tag nuovi.",
            )

    def _ricarica_pending(self) -> None:
        """Riallinea l'elenco in schermata ai contenitori attivi in archivio."""
        if self._accession_id is None:
            return
        attivi = self.db.active_containers_for_accession(self._accession_id)
        noti = {voce["container_id"]: voce for voce in self._pending}
        aggiornati = []
        for record in attivi:
            voce = noti.get(record.container_id)
            if voce is None:
                base = self._pending[0]["payload"] if self._pending else None
                if base is None:
                    continue
                voce = {
                    "container_id": record.container_id,
                    "index": record.index,
                    "total": record.total,
                    "accession_id": record.accession_id,
                    "payload": replace(
                        base,
                        container_index=record.index,
                        container_total=record.total,
                    ),
                }
            else:
                voce["index"] = record.index
                voce["total"] = record.total
                if not voce.get("epc"):
                    voce["payload"] = replace(
                        voce["payload"],
                        container_index=record.index,
                        container_total=record.total,
                    )
            aggiornati.append(voce)
        self._pending = aggiornati
        self._aggiorna_tabella_contenitori()

    def _scrivi_prossimo(self) -> None:
        da_fare = [voce for voce in self._pending if not voce.get("epc")]
        if not da_fare:
            messagebox.showinfo(
                "Nulla da scrivere",
                "Non ci sono contenitori in attesa: registrare prima un'accettazione.",
            )
            return

        # La conferma si chiede alla PRIMA scrittura, non alla registrazione: e'
        # li' che il numero diventa irreversibile, ed e' li' che l'operatore ha i
        # campioni davanti invece della tastiera.
        scritti = [voce for voce in self._pending if voce.get("epc")]
        if not scritti and not self._conteggio_confermato:
            totale = len(self._pending)
            if not messagebox.askyesno(
                "Confermare il numero di campioni",
                f"Stai per scrivere {totale} contenitor{'e' if totale == 1 else 'i'}.\n\n"
                "Hai contato fisicamente i campioni?\n\n"
                "Da questo momento il totale finisce dentro i tag e sui contenitori "
                "gia' scritti non e' piu' correggibile.",
            ):
                self.log("Scrittura annullata: verificare il numero di campioni.")
                return
            self._conteggio_confermato = True

        voce = da_fare[0]

        def compito():
            tagio = self._tagio(writing=True)
            capienza = max_plaintext_bytes(tagio.user_memory_bytes)
            self.log(
                f"Scrittura contenitore {voce['index']}/{voce['total']} "
                f"({capienza} byte utili)..."
            )
            esito = tagio.provision(voce["payload"], container_id=voce["container_id"])
            for passo in esito.steps:
                self.log(f"  {passo}")
            if esito.ok:
                voce["epc"] = esito.epc
                voce["tid"] = esito.tid
                voce["stato"] = "scritto"
                self.log(f"Contenitore {voce['index']}/{voce['total']} scritto: {esito.epc}")
            else:
                voce["stato"] = "errore"
                self.log(f"Scrittura fallita: {esito.error}")
            self._post_ui(self._aggiorna_tabella_contenitori)

        self._submit(compito)

    def _svuota_modulo(self) -> None:
        for variabile in (
            self.cf, self.cognome, self.nome, self.reparto, self.medico, self.descrizione
        ):
            variabile.set("")
        self.contenitori.set(1)
        self.urgente.set(False)
        self.infettivo.set(False)
        self._pending = []
        self._specimen_id = None
        self._accession_id = None
        self._conteggio_confermato = False
        self._aggiorna_tabella_contenitori()

    # -- sigillo e spedizione -----------------------------------------------
    def _prepara_spedizione(self) -> None:
        """Raccoglie i contenitori scritti e non ancora spediti."""
        pronti = [
            record
            for record in self.db.connection.execute(
                "SELECT id FROM containers WHERE state=? ORDER BY id",
                (ContainerState.PROVISIONED.value,),
            ).fetchall()
        ]
        if not pronti:
            messagebox.showinfo(
                "Niente da spedire",
                "Non ci sono contenitori scritti e in attesa di spedizione.",
            )
            return
        try:
            shipment_id = self.db.create_shipment(
                Shipment(destinazione=self.destinazione.get().strip() or "—",
                         data=dt.date.today())
            )
            self.db.add_to_shipment(shipment_id, [riga["id"] for riga in pronti])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Preparazione fallita", str(exc))
            return

        self._shipment_id = shipment_id
        self._sealing_record = None
        contenuto = self.db.shipment_contents(shipment_id)
        self.conteggio_var.set(f"0 / {len(contenuto)}")
        self.verdetto_var.set(
            f"spedizione {shipment_id} pronta: {len(contenuto)} contenitori da certificare"
        )
        self._mostra_attesi(contenuto)
        self.log(f"Spedizione {shipment_id} preparata con {len(contenuto)} contenitori.")

    def _mostra_attesi(self, contenuto) -> None:
        self.tabella_sigillo.delete(*self.tabella_sigillo.get_children())
        for record in contenuto:
            self.tabella_sigillo.insert(
                "",
                "end",
                values=("da leggere", record.label, record.display_name, "—", "—"),
            )

    def _sigilla(self) -> None:
        if self._shipment_id is None:
            messagebox.showinfo("Nessuna spedizione", "Preparare prima la spedizione.")
            return
        contenuto = self.db.shipment_contents(self._shipment_id)
        attesi = [record.epc for record in contenuto if record.epc]
        if not attesi:
            messagebox.showerror("Spedizione vuota", "Nessun contenitore con EPC assegnato.")
            return

        # La conferma di chiusura e' la prova che finisce nel record: finche' non
        # c'e' il sensore sugli agganci, e' la parola dell'operatore, e va
        # registrata come tale invece di essere data per scontata.
        if not messagebox.askyesno(
            "Confermare la chiusura",
            f"Il contenitore di trasporto e' chiuso e contiene {len(attesi)} campioni?\n\n"
            "La lettura definitiva parte adesso e viene registrata come prova.",
        ):
            self.log("Sigillo annullato: chiusura non confermata.")
            return

        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))
        potenze = tuple(self.lims_cfg.get("seal_powers_cdbm") or (2000, 2500, 2900))
        politica = SealingPolicy(
            min_antennas=int(self.lims_cfg.get("seal_min_antennas", 1)),
            stable_passes=2,
        )
        operatore = self._operatore

        def compito():
            self.log(f"Sigillo in corso su {len(attesi)} contenitori attesi...")
            sessione = SealingSession(
                self.service,
                attesi,
                passes=default_passes(antenne, powers_cdbm=potenze),
                policy=politica,
                region=self.cfg.get("reader", {}).get("region", 0x08),
                operator=operatore,
                db=self.db,
            )
            record = sessione.run(
                closure_proof=ClosureProof.OPERATOR,
                on_progress=lambda passata, trovati, totali: self._post_ui(
                    lambda: self.conteggio_var.set(f"{trovati} / {totali}")
                ),
            )
            self._sealing_record = record
            self._post_ui(lambda: self._mostra_sigillo(record, contenuto))

        self._submit(compito)

    def _mostra_sigillo(self, record, contenuto) -> None:
        per_epc = {r.epc: r for r in contenuto if r.epc}
        prove = {item.epc: item for item in record.evidence}
        self.tabella_sigillo.delete(*self.tabella_sigillo.get_children())

        for epc in record.expected:
            anagrafica = per_epc.get(epc)
            prova = prove.get(epc)
            trovato = epc in record.found
            self.tabella_sigillo.insert(
                "",
                "end",
                values=(
                    "letto" if trovato else "MANCANTE",
                    anagrafica.label if anagrafica else "—",
                    anagrafica.display_name if anagrafica else epc,
                    f"{prova.detection_rate:.0%}" if prova else "0%",
                    ", ".join(str(a) for a in prova.antennas) if prova else "—",
                ),
                tags=() if trovato else ("mancante",),
            )
        for epc in record.unexpected:
            self.tabella_sigillo.insert(
                "", "end",
                values=("FUORI DISTINTA", "—", epc, "—", "—"),
                tags=("estraneo",),
            )

        trovati, totali = record.counts
        self.conteggio_var.set(f"{trovati} / {totali}")
        if record.ok:
            scatola = f", scatola {record.box_id}" if record.box_id is not None else ""
            self.verdetto_var.set(
                f"SIGILLO VALIDO — {record.passes_run} passate{scatola}"
            )
            self.db.set_shipment_state(self._shipment_id, ShipmentState.SEALED)
        else:
            motivo = record.error or f"{len(record.missing)} contenitori mancanti"
            self.verdetto_var.set(f"SIGILLO NON VALIDO — {motivo}")
        self.log(f"Sigillo: {trovati}/{totali} in {record.passes_run} passate ({record.stop_reason}).")

    def _esporta_distinta(self) -> None:
        if self._shipment_id is None:
            messagebox.showinfo("Nessuna spedizione", "Preparare prima la spedizione.")
            return
        try:
            distinta = build_manifest(
                self.db,
                self._shipment_id,
                lab_id=int(self.lims_cfg.get("lab_id", 0)),
                operator=self._operatore,
                sealing=self._sealing_record,
                box_epc=getattr(self._sealing_record, "box_epc", ""),
            )
            blob = seal_manifest(distinta, self.keyring)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Esportazione fallita", str(exc))
            return

        percorso = filedialog.asksaveasfilename(
            title="Salva la distinta cifrata",
            defaultextension=".rfidman",
            initialfile=f"distinta_{self._shipment_id}_{dt.date.today():%Y%m%d}.rfidman",
            filetypes=[("Distinta cifrata", "*.rfidman"), ("Tutti i file", "*.*")],
        )
        if not percorso:
            return
        Path(percorso).write_bytes(blob)
        self.db.set_shipment_state(self._shipment_id, ShipmentState.SENT)
        self.log(f"Distinta esportata in {percorso} ({len(distinta.entries)} contenitori).")
        messagebox.showinfo(
            "Distinta esportata",
            f"{len(distinta.entries)} contenitori.\n\n"
            "Il file e' cifrato: il laboratorio destinatario lo apre solo con la "
            "chiave del circuito, gia' concordata.",
        )

    # -- ricezione ----------------------------------------------------------
    def _importa_distinta(self) -> None:
        """Carica la distinta cifrata inviata dal laboratorio mittente."""
        percorso = filedialog.askopenfilename(
            title="Apri la distinta cifrata",
            filetypes=[("Distinta cifrata", "*.rfidman"), ("Tutti i file", "*.*")],
        )
        if not percorso:
            return
        try:
            self._distinta = open_manifest(Path(percorso).read_bytes(), self.keyring)
        except UnknownKeyError as exc:
            messagebox.showerror(
                "Chiave mancante",
                f"{exc}\n\nLa chiave va concordata con il laboratorio mittente.",
            )
            return
        except (PayloadAuthenticationError, PayloadFormatError) as exc:
            messagebox.showerror("Distinta non valida", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Lettura fallita", str(exc))
            return

        distinta = self._distinta
        self.log(
            f"Distinta caricata: {len(distinta.entries)} contenitori da "
            f"{distinta.destination or 'mittente ignoto'} del {distinta.created_at}."
        )
        if distinta.sealing:
            trovati = distinta.sealing.get("trovati")
            attesi = distinta.sealing.get("attesi")
            prova = distinta.sealing.get("closure_proof")
            self.log(f"  Sigillo alla partenza: {trovati}/{attesi}, prova di chiusura «{prova}».")
        self.esito_var.set(f"distinta: {len(distinta.entries)} contenitori attesi")

    def _leggi_volume(self) -> None:
        attesi = list(self._distinta.epcs) if self._distinta is not None else None

        def compito():
            self.log("Lettura del volume in corso...")
            rilievo = self._tagio(writing=False).survey_field(expected_epcs=attesi)
            riconciliazione = (
                reconcile(self._distinta, [o.epc for o in rilievo.observations])
                if self._distinta is not None
                else None
            )
            self._post_ui(lambda: self._mostra_rilievo(rilievo, riconciliazione))

        self._submit(compito)

    def _mostra_rilievo(self, rilievo, riconciliazione=None) -> None:
        self.tabella_ricezione.delete(*self.tabella_ricezione.get_children())
        self.elenco_mancanti.delete(0, "end")

        if not rilievo.ok:
            self.esito_var.set("lettura fallita")
            self.log(f"Lettura fallita: {rilievo.error}")
            return

        for osservazione in rilievo.observations:
            info = osservazione.epc_info
            payload = osservazione.payload
            problema = osservazione.status in {"non_autenticato", "illeggibile", "estraneo"}
            self.tabella_ricezione.insert(
                "",
                "end",
                values=(
                    _STATO_ETICHETTE.get(osservazione.status, osservazione.status),
                    info.accession_id if info else "—",
                    info.label if info else "—",
                    (
                        f"{payload.display_name} ({payload.codice_fiscale})"
                        if payload
                        else osservazione.detail[:60] or "—"
                    ),
                    MATERIALS.get(payload.material_code, "—") if payload else "—",
                    osservazione.rssi if osservazione.rssi is not None else "—",
                ),
                tags=("problema",) if problema else (),
            )

        for mancante in rilievo.missing:
            self.elenco_mancanti.insert("end", mancante.label)

        if riconciliazione is not None:
            # Con la distinta il verdetto e' un confronto, non una deduzione: si
            # sa esattamente cosa doveva arrivare, e un contenitore in piu' conta
            # quanto uno in meno.
            arrivati, attesi = riconciliazione.counts
            if riconciliazione.ok:
                self.esito_var.set(f"{arrivati}/{attesi} — spedizione completa e conforme")
            else:
                pezzi = []
                if riconciliazione.missing:
                    pezzi.append(f"{len(riconciliazione.missing)} mancanti")
                if riconciliazione.unexpected:
                    pezzi.append(f"{len(riconciliazione.unexpected)} inattesi")
                self.esito_var.set(f"{arrivati}/{attesi} — " + ", ".join(pezzi))
            self.elenco_mancanti.delete(0, "end")
            for riga in riconciliazione.describe_missing():
                self.elenco_mancanti.insert("end", riga)
            for epc in riconciliazione.unexpected:
                self.elenco_mancanti.insert("end", f"INATTESO (non in distinta): {epc}")
        elif rilievo.complete and rilievo.observations:
            self.esito_var.set(f"{len(rilievo.known)} contenitori, spedizione completa")
        elif rilievo.missing:
            self.esito_var.set(f"MANCANO {len(rilievo.missing)} contenitori")
        else:
            self.esito_var.set("nessun tag nel volume")
        self.log(
            f"Letti {len(rilievo.observations)} tag "
            f"({len(rilievo.foreign)} estranei), {len(rilievo.missing)} mancanti."
        )

    # -- chiusura -----------------------------------------------------------
    def _on_close(self) -> None:
        self._closing = True
        self._destroyed = True
        try:
            self._executor.shutdown(wait=False)
            if self.owns_service:
                self.service.stop()
            self.db.close()
        finally:
            self.root.destroy()


def launch_lims_gui(cfg: dict, *, service=None, owns_service: bool = True) -> None:
    root = tk.Tk()
    LimsApp(root, cfg, service=service, owns_service=owns_service)
    root.mainloop()


def main() -> int:
    ap = argparse.ArgumentParser(description="Tracciabilita' campioni su tag RFID")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--debug", action="store_true", help="log DEBUG (dump frame TX/RX)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_cfg = cfg.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_dir=log_cfg.get("dir", "logs"),
        file_prefix="lims_gui",
        force_debug=args.debug,
    )
    launch_lims_gui(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
