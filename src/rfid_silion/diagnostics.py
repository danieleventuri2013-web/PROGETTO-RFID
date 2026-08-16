"""Diagnostica interna del driver: logging strutturato, checkpoint, contatori.

Tre strumenti, un solo modulo (vedi docs/DOCUMENTAZIONE_TECNICA.md):

  1. ``setup_logging()``  — unico punto di configurazione del logging per le
     app (console + file per-sessione in ``logs/``).
  2. ``checkpoint()``     — righe di log strutturate ("CHECKPOINT nome | k=v")
     nei punti chiave del flusso, filtrabili con findstr/grep.
  3. ``DiagCounters``     — contatori runtime esposti da ``SIM7200Reader.diag``
     e inclusi in ``health_check()``: dicono subito se il problema e' un
     timeout (collegamento), un errore di frame/CRC (rumore) o uno status
     di errore (comando rifiutato dal lettore).

Modalita' debug: variabile d'ambiente ``RFID_DEBUG=1`` oppure flag ``--debug``
delle app; forza il livello DEBUG, che include i dump esadecimali TX/RX dei
frame (gia' emessi da reader.py a livello DEBUG).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: variabile d'ambiente che forza la modalita' debug (RFID_DEBUG=1)
DEBUG_ENV = "RFID_DEBUG"

_configured = False


def debug_mode_enabled() -> bool:
    """True se la modalita' debug e' richiesta via ambiente (RFID_DEBUG=1)."""
    return os.environ.get(DEBUG_ENV, "").strip().lower() not in ("", "0", "false", "no")


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path | None = "logs",
    file_prefix: str = "rfid",
    console: bool = True,
    force_debug: bool = False,
) -> Path | None:
    """Configura il logging dell'applicazione: console + file per-sessione.

    - ``level``: livello base (tipicamente da config.yaml ``logging.level``).
    - ``log_dir``: directory dei file di log (None = solo console).
    - ``file_prefix``: prefisso del file, es. "step1" -> logs/step1_<ts>.log
    - ``force_debug``: True (o RFID_DEBUG=1) forza DEBUG (dump frame TX/RX).

    Ritorna il percorso del file di log creato (None se solo console).
    Richiamabile piu' volte: dalla seconda chiamata aggiorna solo il livello.
    """
    global _configured
    if force_debug or debug_mode_enabled():
        level = "DEBUG"
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(lvl)
    if _configured:
        return None
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    log_path: Path | None = None
    if log_dir is not None:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        log_path = d / f"{file_prefix}_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    _configured = True
    return log_path


def checkpoint(logger: logging.Logger, name: str, level: int = logging.INFO, **fields) -> None:
    """Checkpoint diagnostico: una riga di log strutturata e greppabile.

    Formato: ``CHECKPOINT <nome> | chiave=valore chiave=valore``
    Esempio:   ``CHECKPOINT inventory | stored=3 parsed=3 epcs=1``
    Estrazione: ``findstr CHECKPOINT logs\\*.log`` (Windows) / ``grep`` (Linux).

    I valori vengono compattati (niente spazi) per restare un token per campo.
    """
    payload = " ".join(f"{k}={_fmt_value(v)}" for k, v in fields.items())
    logger.log(level, "CHECKPOINT %s | %s", name, payload)


def _fmt_value(v: object) -> str:
    if isinstance(v, bytes):
        return v.hex().upper() or "-"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v).replace(" ", "")


@dataclass
class DiagCounters:
    """Contatori diagnostici runtime del reader (``SIM7200Reader.diag``).

    Aggiornati da ``SIM7200Reader._command()`` a ogni scambio; inclusi nel
    report di ``health_check()`` e nel JSON dello Step 1. ``no_tag_events``
    (status 0x0400) e' separato da ``status_errors`` perche' "nessun tag in
    campo" non e' un guasto.
    """

    started_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    commands_sent: int = 0
    responses_ok: int = 0
    no_tag_events: int = 0  # status 0x0400: non e' un guasto
    status_errors: int = 0  # status != 0x0000 e != 0x0400
    frame_errors: int = 0  # header/CRC/lunghezza/cmd-echo errati
    timeouts: int = 0  # nessuna risposta (completa) entro il timeout
    transport_errors: int = 0  # porta/peer chiusi o errore I/O del sistema
    bytes_discarded: int = 0  # byte spuri scartati cercando l'header 0xFF
    last_error: str | None = None
    last_error_at: str | None = None

    def record_error(self, err: object) -> None:
        self.last_error = str(err)
        self.last_error_at = dt.datetime.now().isoformat(timespec="seconds")

    def snapshot(self) -> dict:
        """Copia serializzabile in JSON (per report/health check)."""
        return asdict(self)
