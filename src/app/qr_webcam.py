"""Prova locale del QR della distinta: webcam, acquisizione manuale e confronto.

Non apre il lettore RFID o il database e non salva immagini della webcam.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from lims.crypto import CryptoError, Keyring  # noqa: E402
from lims.manifest import MANIFEST_MAGIC, Manifest, open_manifest  # noqa: E402
from lims.tabella import (  # noqa: E402
    COLONNE,
    DistintaLetta,
    TabellaError,
    analizza_parte,
    decodifica_tabella,
    per_stampa,
    righe_da_manifest,
)


class RaccoltaQR:
    """Accetta solo parti coerenti della stessa distinta, su comando esplicito."""

    def __init__(self, chiavi: tuple[bytes, ...] = ()):
        self.chiavi = chiavi
        self.parti: dict[int, str] = {}
        self.identificativo = ""
        self.totale = 0

    def acquisisci(self, testi: list[str]) -> DistintaLetta | None:
        if not testi:
            raise TabellaError("Nessun QR leggibile: avvicina il foglio e riprova.")
        candidate = dict(self.parti)
        ident, totale = self.identificativo, self.totale
        for testo in testi:
            if len(testo) > 10000:
                raise TabellaError("QR troppo lungo per una distinta.")
            parte, quante, codice, _ = analizza_parte(testo)
            if not 1 <= parte <= quante <= 9 or not re.fullmatch(r"[0-9A-F]{8}", codice):
                raise TabellaError("Intestazione QR non valida.")
            if ident and (ident != codice or totale != quante):
                raise TabellaError("QR di distinte diverse: premi Nuova scansione prima di cambiare foglio.")
            ident, totale = codice, quante
            testo = testo.strip("\r\n\t")  # Lo spazio finale fa parte della Base45.
            if parte in candidate and candidate[parte] != testo:
                raise TabellaError("La stessa parte contiene dati diversi: ricomincia la scansione.")
            candidate[parte] = testo
        risultato = None
        if len(candidate) == totale:
            scansioni = [candidate[i] for i in range(1, totale + 1)]
            if self.chiavi:
                ultimo = None
                for chiave in self.chiavi:
                    try:
                        risultato = decodifica_tabella(scansioni, chiave=chiave)
                        break
                    except TabellaError as exc:
                        ultimo = exc
                if risultato is None:
                    raise ultimo
            else:
                risultato = decodifica_tabella(scansioni)
        self.parti, self.identificativo, self.totale = candidate, ident, totale
        return risultato

    @property
    def avanzamento(self) -> str:
        mancanti = [str(i) for i in range(1, self.totale + 1) if i not in self.parti]
        return (f"Acquisite {len(self.parti)}/{self.totale} parti. "
                + ("Inquadra e acquisisci le parti: " + ", ".join(mancanti) if mancanti else "QR completo."))


def _norm(testo: str) -> str:
    return " ".join(str(testo or "").split()).casefold()


def confronta(letta: DistintaLetta, distinta: Manifest) -> dict:
    """Confronta per EPC e solo i campi realmente presenti in entrambi i formati."""
    qr = {r.epc.upper(): r for r in letta.righe}
    file = {r.epc.upper(): r for r in righe_da_manifest(distinta)}
    nomi = {e.epc.upper(): e.display_name for e in distinta.entries}
    differenze = []
    if len(qr) != len(letta.righe) or len(file) != len(distinta.entries):
        differenze.append("EPC duplicati: impossibile dichiarare il confronto conforme.")
    if "" in qr or "" in file:
        differenze.append("Una riga non contiene l'EPC.")
    campi = ("codice_fiscale", "data_prelievo", "descrizione", "materiale", "fissativo", "sede", "etichetta")
    for epc in sorted(qr.keys() & file.keys()):
        # Il file conserva il nome completo: non indovinare dove finisce un
        # cognome composto separando arbitrariamente display_name.
        coppie = [("paziente", qr[epc].paziente, nomi[epc])]
        coppie += [(c, getattr(qr[epc], c), getattr(file[epc], c)) for c in campi]
        for campo, acquisito, atteso in coppie:
            if _norm(acquisito) != _norm(atteso):
                differenze.append(f"{epc} — {campo}: QR «{acquisito}»; file «{atteso}»")
    mancanti = sorted(file.keys() - qr.keys())
    estranei = sorted(qr.keys() - file.keys())
    return {"ok": bool(qr) and not (mancanti or estranei or differenze),
            "corrispondenti": len(qr.keys() & file.keys()), "attesi": len(distinta.entries),
            "mancanti": mancanti, "estranei": estranei, "differenze": differenze}


def carica_riferimento(percorso: Path | None, config_path: Path) -> tuple[Manifest | None, tuple[bytes, ...]]:
    """Legge configurazione e chiavi esistenti, senza importare il documento nel DB."""
    import yaml

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    key_path = Path((cfg.get("lims") or {}).get("keyring", "logs/lims_keys.json"))
    if not key_path.is_absolute():
        key_path = ROOT / key_path
    keyring = Keyring.load(key_path) if key_path.exists() else Keyring()
    chiavi = tuple(keyring.require(i) for i in keyring.key_ids)
    if percorso is None:
        return None, chiavi
    blob = percorso.read_bytes()
    if not blob.startswith(MANIFEST_MAGIC) or len(blob) <= len(MANIFEST_MAGIC):
        raise ValueError("Il file non è una distinta RFID valida.")
    versione = blob[len(MANIFEST_MAGIC)]
    if versione == 1:
        return open_manifest(blob, keyring), chiavi
    if versione != 2:
        raise ValueError(f"Versione distinta non supportata: {versione}")
    from lims.pec import resolve_secret
    from lims.secure_manifest import load_certificate, load_private_key, open_secure_manifest

    sicurezza = cfg.get("security") or {}

    def risolvi(nome):
        if not sicurezza.get(nome):
            raise ValueError(f"Configurazione necessaria per la distinta v2: security.{nome}")
        p = Path(sicurezza[nome])
        return p if p.is_absolute() else config_path.parent / p

    servizio = sicurezza.get("recipient_credential_service") or sicurezza.get("credential_service")
    password = resolve_secret(servizio, sicurezza.get("recipient_credential_username")
                              or sicurezza.get("credential_username") or "recipient-decryption-key") if servizio else None
    ca = sicurezza.get("trusted_ca") or []
    ca = ca if isinstance(ca, list) else [ca]
    autorita = [load_certificate(Path(p) if Path(p).is_absolute() else config_path.parent / p) for p in ca]
    aperta = open_secure_manifest(blob,
        recipient_private_key=load_private_key(risolvi("recipient_private_key"), password),
        recipient_certificate=load_certificate(risolvi("recipient_certificate")),
        trusted_cas=autorita, revoked_serials=sicurezza.get("revoked_serials") or ())
    return aperta.manifest, chiavi


def leggi_qr(immagine):
    """Restituisce testo e vertici dei QR effettivamente decodificabili."""
    import zxingcpp

    risultati = zxingcpp.read_barcodes(immagine, formats=zxingcpp.BarcodeFormat.QRCode)
    return [(r.text, [(p.x, p.y) for p in (r.position.top_left, r.position.top_right,
             r.position.bottom_right, r.position.bottom_left)]) for r in risultati if r.valid]


def descrivi(letta: DistintaLetta, distinta: Manifest | None) -> str:
    firma = "verificata" if letta.firma_verificata else "NON verificata: portachiavi non disponibile"
    righe = [f"QR {letta.identificativo}: {len(letta.righe)} contenitori, {letta.parti} parti.",
             f"Firma QR: {firma}."]
    if distinta is not None:
        confronto = confronta(letta, distinta)
        righe += ["", "CONFRONTO CON IL FILE: " + ("CORRISPONDE" if confronto["ok"] else "DIFFERENZE RILEVATE"),
                  f"EPC corrispondenti: {confronto['corrispondenti']}/{confronto['attesi']}."]
        righe += ["Mancante nel QR: " + epc for epc in confronto["mancanti"]]
        righe += ["Non presente nel file: " + epc for epc in confronto["estranei"]]
        righe += confronto["differenze"]
        righe += ["Sesso, data di nascita e ora del prelievo sono mostrati dal QR:",
                  "questi campi non sono disponibili nel file .rfidman per il confronto."]
    for i, riga in enumerate(per_stampa(letta.righe), 1):
        righe += ["", f"CONTENITORE {i}", *[f"  {titolo}: {riga[campo] or '—'}" for campo, titolo in COLONNE]]
    return "\n".join(righe)


class Camera:
    """Video continuo e ricerca QR indipendenti: il decoder non ferma l'anteprima."""

    def __init__(self, indice: int, backend: str = "Auto"):
        self.indice = indice
        self.backend = backend
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.ultimo = (None, 0.0, "Avvio webcam…")
        self.qr = ([], 0.0)
        self.nero = False
        self.contatore = 0
        self.fps = 0.0
        self.errore_qr = ""
        self.thread = threading.Thread(target=self._esegui, daemon=True)
        self.decoder = threading.Thread(target=self._decodifica, daemon=True)
        self.thread.start()
        self.decoder.start()

    def snapshot(self):
        with self.lock:
            frame, quando, errore = self.ultimo
            rilevati, letti_il = self.qr
            if self.nero or time.monotonic() - letti_il > 1.0:
                rilevati = []
            return frame, rilevati, letti_il if rilevati else quando, errore

    def diagnostica(self):
        with self.lock:
            return self.contatore, self.fps, self.nero, self.errore_qr

    def _decodifica(self):
        precedente = 0.0
        while not self.stop.wait(0.12):
            with self.lock:
                frame, quando, errore = self.ultimo
                nero = self.nero
            if frame is None or errore or nero or quando == precedente:
                continue
            precedente = quando
            try:
                rilevati = leggi_qr(frame)
                with self.lock:
                    self.qr = (rilevati, quando)
                    self.errore_qr = ""
            except Exception as exc:
                with self.lock:
                    self.qr = ([], 0.0)
                    self.errore_qr = f"Decoder QR: {exc}"

    def attendi_chiusura(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.decoder.join(timeout=2)

    def _esegui(self):
        import cv2

        cap = None
        try:
            api = {"Auto": cv2.CAP_ANY, "DirectShow": cv2.CAP_DSHOW,
                   "Media Foundation": cv2.CAP_MSMF}[self.backend]
            cap = cv2.VideoCapture(self.indice, api)
            if not cap.isOpened() and self.backend == "Auto" and sys.platform == "win32":
                cap.release()
                cap = cv2.VideoCapture(self.indice, cv2.CAP_DSHOW)
            if not cap.isOpened():
                raise RuntimeError("Webcam non disponibile. Chiudi altre app o prova --camera 1.")
            # Mantiene il formato nativo negoziato dal dispositivo.
            errori = 0
            campione_il = time.monotonic()
            campione_n = 0
            while not self.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    errori += 1
                    if errori >= 15:
                        raise RuntimeError("La webcam non restituisce immagini. Controlla il collegamento.")
                    self.stop.wait(0.1)
                    continue
                errori = 0
                letto_il = time.monotonic()
                nero = int(frame[::8, ::8].max()) <= 2
                with self.lock:
                    self.ultimo = (frame, letto_il, "")
                    self.nero = nero
                    self.contatore += 1
                    campione_n += 1
                    if letto_il - campione_il >= 1.0:
                        self.fps = campione_n / (letto_il - campione_il)
                        campione_n, campione_il = 0, letto_il
                self.stop.wait(0.005)
        except Exception as exc:
            with self.lock:
                self.ultimo = (None, 0.0, str(exc))
        finally:
            if cap is not None:
                cap.release()


class FinestraQR:
    def __init__(self, root, camera: int, distinta: Manifest | None, chiavi: tuple[bytes, ...], nome: str):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root, self.distinta, self.chiavi = root, distinta, chiavi
        self.raccolta = RaccoltaQR(chiavi)
        self.camera = Camera(camera)
        self.frame_attuale = (None, [], 0.0, "")
        self.chiusura = False
        self.riavvio = False
        root.title("Prova QR distinta — Webcam")
        root.geometry("920x690")
        root.minsize(720, 540)
        base = ttk.Frame(root, padding=12)
        base.pack(fill="both", expand=True)
        sorgente = ttk.Frame(base)
        sorgente.pack(fill="x", pady=(0, 10))
        ttk.Label(sorgente, text="Webcam:").pack(side="left")
        self.indice_camera = tk.StringVar(value=str(camera))
        ttk.Spinbox(sorgente, from_=0, to=9, width=3, textvariable=self.indice_camera).pack(side="left", padx=5)
        self.backend_camera = tk.StringVar(value="Auto")
        ttk.Combobox(sorgente, textvariable=self.backend_camera, width=19, state="readonly",
                     values=("Auto", "DirectShow", "Media Foundation") if sys.platform == "win32" else ("Auto",)).pack(side="left", padx=5)
        self.riavvia_button = ttk.Button(sorgente, text="Avvia / riavvia webcam", command=self.riavvia)
        self.riavvia_button.pack(side="left", padx=5)
        alto = ttk.Frame(base)
        alto.pack(fill="x")
        self.video = ttk.Label(alto, text="Avvio webcam…", anchor="center")
        self.video.pack(side="left", padx=(0, 14))
        info = ttk.Frame(alto)
        info.pack(side="left", fill="both", expand=True)
        ttk.Label(info, text="Inquadra il QR del foglio", font=("Segoe UI", 14, "bold")).pack(anchor="w")
        ttk.Label(info, text="Riquadro verde: QR leggibile.\nSpazio o Invio: acquisisci.\nPiù QR: acquisisci tutte le parti.\nR: nuova scansione · Esc: chiudi.",
                  wraplength=350).pack(anchor="w", pady=8)
        riferimento = f"File: {nome}\n{len(distinta.entries)} contenitori attesi" if distinta else "Nessun file di confronto selezionato"
        ttk.Label(info, text=riferimento, wraplength=340).pack(anchor="w", pady=6)
        self.stato = tk.StringVar(value="Avvio webcam…")
        ttk.Label(base, textvariable=self.stato, wraplength=870).pack(anchor="w", pady=8)
        azioni = ttk.Frame(base)
        azioni.pack(fill="x", pady=(0, 8))
        self.acquisisci_button = ttk.Button(azioni, text="Acquisisci QR [Spazio]", command=self.acquisisci, state="disabled")
        self.acquisisci_button.pack(side="left")
        ttk.Button(azioni, text="Nuova scansione [R]", command=self.azzera).pack(side="left", padx=8)
        ttk.Button(azioni, text="Chiudi [Esc]", command=self.chiudi).pack(side="right")
        self.esito = tk.StringVar(value="Nessun QR acquisito.")
        ttk.Label(base, textvariable=self.esito, wraplength=870).pack(anchor="w", pady=(0, 8))
        self.testo = ScrolledText(base, wrap="word", font=("Consolas", 10), state="disabled")
        self.testo.pack(fill="both", expand=True)
        root.bind("<space>", self._tasto_acquisisci)
        root.bind("<Return>", self._tasto_acquisisci)
        root.bind("<Key-r>", lambda _e: self.azzera())
        root.bind("<Key-R>", lambda _e: self.azzera())
        root.bind("<Escape>", lambda _e: self.chiudi())
        root.protocol("WM_DELETE_WINDOW", self.chiudi)
        self._aggiorna()

    def _scrivi(self, testo):
        self.testo.configure(state="normal")
        self.testo.delete("1.0", "end")
        self.testo.insert("1.0", testo)
        self.testo.configure(state="disabled")

    def _tasto_acquisisci(self, evento):
        # Evita la doppia azione quando Spazio/Invio attivano già un pulsante.
        if evento.widget.winfo_class() not in {"TButton", "TSpinbox", "TCombobox"}:
            self.acquisisci()
            return "break"
        return None

    def acquisisci(self):
        _, rilevati, quando, errore = self.frame_attuale
        try:
            if errore or time.monotonic() - quando > 1.0:
                raise TabellaError(errore or "Immagine non aggiornata: attendi la webcam e riprova.")
            risultato = self.raccolta.acquisisci([testo for testo, _ in rilevati])
            self.esito.set(self.raccolta.avanzamento)
            if risultato is not None:
                self._scrivi(descrivi(risultato, self.distinta))
        except (TabellaError, ValueError) as exc:
            self.esito.set(f"Acquisizione non riuscita: {exc}")

    def azzera(self):
        self.raccolta = RaccoltaQR(self.chiavi)
        self.esito.set("Nuova scansione. Inquadra il QR e premi Spazio.")
        self._scrivi("")

    def riavvia(self):
        try:
            indice = int(self.indice_camera.get())
            if not 0 <= indice <= 9:
                raise ValueError
        except ValueError:
            self.esito.set("Scegli un numero di webcam fra 0 e 9.")
            return
        self.riavvio = True
        self.riavvia_button.configure(state="disabled")
        self.camera.stop.set()
        self.video.configure(image="", text="Riavvio webcam…")
        self._attendi_riavvio(indice, self.backend_camera.get())

    def _attendi_riavvio(self, indice, backend):
        if self.chiusura:
            return
        if self.camera.thread.is_alive() or self.camera.decoder.is_alive():
            self.root.after(100, lambda: self._attendi_riavvio(indice, backend))
            return
        self.camera = Camera(indice, backend)
        self.frame_attuale = (None, [], 0.0, "")
        self.riavvio = False
        self.riavvia_button.configure(state="normal")

    def _aggiorna(self):
        import cv2
        import numpy as np
        from PIL import Image, ImageTk

        if self.chiusura:
            return
        if self.riavvio:
            self.acquisisci_button.configure(state="disabled")
            self.stato.set("Rilascio della webcam e riavvio in corso…")
            self.root.after(80, self._aggiorna)
            return
        self.frame_attuale = self.camera.snapshot()
        frame, rilevati, quando, errore = self.frame_attuale
        fresco = time.monotonic() - quando <= 1.0
        self.acquisisci_button.configure(state="normal" if fresco and rilevati and not errore else "disabled")
        if frame is not None:
            preview = frame.copy()
            for _, vertici in rilevati:
                cv2.polylines(preview, [np.array(vertici, dtype=np.int32)], True, (30, 200, 30), 3)
            img = Image.fromarray(cv2.cvtColor(preview, cv2.COLOR_BGR2RGB))
            img.thumbnail((480, 270))
            self.foto = ImageTk.PhotoImage(img)
            self.video.configure(image=self.foto, text="")
        contatore, fps, nero, errore_qr = self.camera.diagnostica()
        if frame is None:
            self.video.configure(image="", text=errore or "Attesa webcam…")
            self.stato.set(errore)
        else:
            prefisso = f"Webcam {self.camera.indice} · {frame.shape[1]}×{frame.shape[0]} · {fps:.0f} fps · fotogramma {contatore}. "
            messaggio = ("La webcam invia immagini nere: controlla copriobiettivo, tasto privacy o seleziona un'altra webcam."
                         if nero else errore_qr or (f"{len(rilevati)} QR leggibili — premi Spazio per acquisire."
                         if fresco and rilevati else "Video attivo. Inquadra il QR e tieni fermo il foglio."))
            self.stato.set(prefisso + messaggio)
        self.root.after(80, self._aggiorna)

    def chiudi(self):
        if self.chiusura:
            return
        self.chiusura = True
        self.camera.stop.set()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distinta", type=Path, help="File .rfidman da confrontare con il QR")
    parser.add_argument("--config", type=Path, default=ROOT / "src/app/config.yaml")
    parser.add_argument("--camera", type=int, default=0, help="Indice webcam (predefinito: 0)")
    parser.add_argument("--immagine", type=Path, nargs="+", help="Decodifica immagini locali al posto della webcam")
    args = parser.parse_args(argv)
    try:
        import cv2
        import zxingcpp  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("Installa le dipendenze: python -m pip install -r requirements-qr.txt")
        return 2
    try:
        distinta, chiavi = carica_riferimento(args.distinta, args.config.resolve())
        if args.immagine:
            raccolta = RaccoltaQR(chiavi)
            risultato = None
            for path in args.immagine:
                import numpy as np
                frame = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError(f"Immagine illeggibile: {path.name}")
                risultato = raccolta.acquisisci([testo for testo, _ in leggi_qr(frame)])
            if risultato is None:
                raise TabellaError(raccolta.avanzamento)
            print(descrivi(risultato, distinta))
            return int(distinta is not None and not confronta(risultato, distinta)["ok"])
        import tkinter as tk
        root = tk.Tk()
        finestra = FinestraQR(root, args.camera, distinta, chiavi, args.distinta.name if args.distinta else "")
        try:
            root.mainloop()
        finally:
            finestra.camera.attendi_chiusura()
        return 0
    except (OSError, ValueError, RuntimeError, CryptoError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
