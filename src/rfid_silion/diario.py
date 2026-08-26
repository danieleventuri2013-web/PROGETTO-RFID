"""Diario di prototipazione: tutto quello che accade, in ordine, con i numeri veri.

Finche' il lettore non e' collegato in modo stabile — e finche' il flusso di
lavoro cambia ancora — la domanda ricorrente non e' «funziona?» ma «cosa e'
successo davvero l'ultima volta?». Il diario risponde a quella: un file JSON
Lines per sessione con, in ordine di tempo, i tasti premuti, i dati digitati,
ogni operazione dell'interfaccia e ogni risposta della radio, con RSSI, antenna
e conteggio di letture per ciascun tag.

Serve a tre cose:

1. ragionare su casi veri mentre l'hardware non c'e';
2. ricostruire un caso andato storto senza chiedere all'operatore di
   ricordarselo;
3. **seminare il banco simulato**: da un diario si ricavano i tag realmente
   visti e si rimette in piedi la stessa scena senza lettore
   (``rfid_silion.scenario``).

Un record per riga::

    {"t": "2026-08-26T10:40:55.120+02:00", "seq": 12, "sessione": "a1b2c3d4",
     "canale": "radio", "nome": "inventory", "durata_ms": 41.2, "dati": {...}}

I cinque canali dicono da dove arriva il record: ``ui`` (il browser), ``api``
(una operazione dell'interfaccia), ``radio`` (il confine ``RFIDBackend``),
``evento`` (cio' che il server ha trasmesso al browser), ``nota``
(annotazioni del programma).

**Il diario contiene dati dei pazienti**, ed e' voluto: senza quelli non si
ragiona su un caso vero. Vive in ``logs/`` accanto all'archivio, che contiene
gli stessi dati. Con ``diario.pseudonimo: true`` nomi e codici fiscali
diventano un codice stabile: stabile davvero, perche' il sale sta in un file
accanto al diario invece di cambiare a ogni avvio, e due sessioni restano
confrontabili. Il sale serve perche' un codice fiscale ha poca entropia e un
hash senza sale si rovescia con un elenco di nomi.

Il diario non deve mai fermare il lavoro: qualunque errore di scrittura viene
annotato nel log e ingoiato. Un file di prototipazione che impedisce di
scrivere un tag sarebbe il peggiore dei baratti.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

__all__ = [
    "CANALI",
    "BackendTracciato",
    "Diario",
    "diario_da_config",
    "leggi_diario",
]

log = logging.getLogger("rfid.diario")

#: Da dove arriva un record. Sono pochi apposta: servono a filtrare, non a
#: classificare.
CANALI = ("ui", "api", "radio", "evento", "nota")

#: I campi che il pseudonimo sostituisce: quelli che identificano una persona.
#: La descrizione del campione resta in chiaro anche in quella modalita',
#: perche' senza non si capisce piu' niente del caso ed e' un dato sul reperto,
#: non sull'identita'.
_CAMPI_IDENTITA = frozenset(
    {
        "codice_fiscale",
        "cognome",
        "nome",
        "display_name",
        "paziente",
        "data_nascita",
        "medico",
        "email",
        "email_referente",
        "referente",
    }
)

#: Oltre questa lunghezza una stringa viene troncata: nel diario finiscono
#: anche distinte cifrate in base64, e una riga da due megabyte rende il file
#: illeggibile senza aggiungere niente.
_MAX_STRINGA = 400
#: Stesso ragionamento per gli elenchi. Trenta contenitori ci stanno comodi.
_MAX_ELEMENTI = 300
_MAX_PROFONDITA = 8

#: Metodi del contratto `RFIDBackend` che vale la pena registrare. Gli altri
#: (`snapshot`, `state`, `events`) vengono interrogati di continuo e
#: riempirebbero il file di righe che non dicono niente.
_METODI_RADIO = frozenset(
    {
        "start",
        "stop",
        "replace_config",
        "configure",
        "configure_gen2",
        "tune_reader",
        "inventory",
        "read",
        "write",
        "write_epc",
        "lock",
        "verify",
        "generate_epc",
        "antenna_diagnostics",
        "read_gen2_settings",
        "health",
    }
)


def _adesso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class Diario:
    """Registro append-only di una sessione di lavoro.

    Una sola istanza per processo, condivisa: il server HTTP serve ogni
    richiesta su un thread diverso e i record devono restare in un ordine solo.
    """

    def __init__(
        self,
        directory: str | Path = "logs/diario",
        *,
        attivo: bool = True,
        pseudonimo: bool = False,
        max_mb: float = 50.0,
        prefisso: str = "diario",
    ):
        self.attivo = bool(attivo)
        self.pseudonimo = bool(pseudonimo)
        self.directory = Path(directory)
        self.prefisso = prefisso
        self.max_byte = max(0, int(float(max_mb) * 1024 * 1024))
        self.sessione = secrets.token_hex(4)
        self._lock = threading.Lock()
        self._seq = 0
        self._parte = 1
        self._scritti = 0
        self._file: Any = None
        self._percorso: Path | None = None
        self._sale = b""
        if self.attivo:
            try:
                self._apri()
            except OSError as exc:  # noqa: BLE001
                log.warning("Diario non avviato (%s): si prosegue senza", exc)
                self.attivo = False

    # -- ciclo di vita -------------------------------------------------------
    @classmethod
    def spento(cls) -> "Diario":
        """Un diario che non scrive niente, da usare al posto di `None`."""
        return cls(attivo=False)

    @property
    def percorso(self) -> Path | None:
        return self._percorso

    def _apri(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._sale = self._carica_sale()
        marca = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        coda = "" if self._parte == 1 else f"_{self._parte:03d}"
        # L'identificativo di sessione nel nome non e' ridondante: due
        # postazioni avviate nello stesso secondo (o un riavvio immediato)
        # scriverebbero altrimenti nello stesso file, in modo alternato.
        self._percorso = (
            self.directory / f"{self.prefisso}_{marca}_{self.sessione}{coda}.jsonl"
        )
        self._file = self._percorso.open("a", encoding="utf-8")
        self._scritti = 0
        # Il nome del file corrente in un posto fisso: senza, chi vuole
        # guardare il diario deve indovinare quale dei venti e' quello di
        # adesso.
        try:
            (self.directory / "ultimo.txt").write_text(
                f"{self._percorso.name}\n", encoding="utf-8"
            )
        except OSError:
            log.debug("Segnalibro «ultimo.txt» non aggiornato")

    def _carica_sale(self) -> bytes:
        """Sale del pseudonimo, generato una volta e conservato.

        Se cambiasse a ogni avvio, lo stesso paziente avrebbe un codice diverso
        in ogni sessione e il diario diventerebbe inconfrontabile proprio nella
        dimensione che interessa: cosa e' successo a *quel* campione, nei
        giorni.
        """
        file_sale = self.directory / "sale.txt"
        try:
            if file_sale.exists():
                return bytes.fromhex(file_sale.read_text(encoding="utf-8").strip())
            sale = secrets.token_bytes(16)
            file_sale.write_text(sale.hex(), encoding="utf-8")
            return sale
        except (OSError, ValueError):
            log.debug("Sale del diario non persistente: se ne usa uno di sessione")
            return secrets.token_bytes(16)

    def chiudi(self) -> None:
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
        self.attivo = False

    # -- scrittura -----------------------------------------------------------
    def scrivi(
        self,
        canale: str,
        nome: str,
        dati: Mapping[str, Any] | None = None,
        *,
        durata_ms: float | None = None,
    ) -> None:
        """Aggiunge un record. Non solleva mai: il diario non ferma il lavoro."""
        if not self.attivo or self._file is None:
            return
        try:
            corpo = _compatta(dati or {})
            if self.pseudonimo:
                corpo = self._maschera(corpo)
            with self._lock:
                if self._file is None:  # chiuso mentre si preparava il record
                    return
                self._seq += 1
                record: dict[str, Any] = {
                    "t": _adesso(),
                    "seq": self._seq,
                    "sessione": self.sessione,
                    "canale": canale,
                    "nome": nome,
                    "dati": corpo,
                }
                if durata_ms is not None:
                    record["durata_ms"] = round(float(durata_ms), 2)
                riga = json.dumps(record, ensure_ascii=False, default=str) + "\n"
                self._file.write(riga)
                self._file.flush()
                self._scritti += len(riga.encode("utf-8"))
                if self.max_byte and self._scritti >= self.max_byte:
                    self._ruota()
        except Exception:  # noqa: BLE001
            # Un file che rifiuta una riga rifiutera' anche le successive
            # (disco pieno, chiavetta staccata, file chiuso sotto i piedi). Si
            # spegne dopo la prima: insistere significherebbe una traccia di
            # stack per ogni tag letto, e sarebbe il diario a rendere
            # illeggibile il log invece del contrario.
            log.exception("Diario spento: la scrittura del record non e' riuscita")
            self.attivo = False

    def nota(self, nome: str, **campi: Any) -> None:
        """Annotazione del programma: un fatto che nessun altro canale copre."""
        self.scrivi("nota", nome, campi)

    def _ruota(self) -> None:
        """Chiude il file e ne apre un altro. Si chiama gia' dentro il lock."""
        try:
            self._file.close()
        except OSError:
            pass
        self._parte += 1
        try:
            self._apri()
        except OSError as exc:  # noqa: BLE001
            log.warning("Rotazione del diario fallita (%s): diario spento", exc)
            self._file = None
            self.attivo = False

    # -- pseudonimo ----------------------------------------------------------
    def _codice(self, valore: str) -> str:
        impronta = hashlib.sha256(self._sale + valore.strip().upper().encode("utf-8"))
        return "PZ-" + impronta.hexdigest()[:10].upper()

    def _maschera(self, valore: Any, profondita: int = 0) -> Any:
        if profondita > _MAX_PROFONDITA:
            return valore
        if isinstance(valore, Mapping):
            mascherato: dict[str, Any] = {}
            for chiave, contenuto in valore.items():
                if str(chiave) in _CAMPI_IDENTITA and isinstance(contenuto, str) and contenuto:
                    mascherato[str(chiave)] = self._codice(contenuto)
                else:
                    mascherato[str(chiave)] = self._maschera(contenuto, profondita + 1)
            return mascherato
        if isinstance(valore, list):
            return [self._maschera(voce, profondita + 1) for voce in valore]
        return valore


def _compatta(valore: Any, profondita: int = 0) -> Any:
    """Riduce cio' che nel diario occuperebbe spazio senza dire niente."""
    if profondita > _MAX_PROFONDITA:
        return "…(troppo annidato)"
    if isinstance(valore, Mapping):
        return {str(k): _compatta(v, profondita + 1) for k, v in valore.items()}
    if isinstance(valore, (list, tuple)):
        elenco = list(valore)
        if len(elenco) > _MAX_ELEMENTI:
            testa = [_compatta(v, profondita + 1) for v in elenco[:_MAX_ELEMENTI]]
            return testa + [f"…(altri {len(elenco) - _MAX_ELEMENTI} elementi)"]
        return [_compatta(v, profondita + 1) for v in elenco]
    if isinstance(valore, bytes):
        return valore.hex().upper()[:_MAX_STRINGA]
    if isinstance(valore, str) and len(valore) > _MAX_STRINGA:
        return valore[:120] + f"…(in tutto {len(valore)} caratteri)"
    return valore


def diario_da_config(config: Mapping[str, Any]) -> Diario:
    """Costruisce il diario dalla sezione `diario:` di config.yaml."""
    sezione = dict(config.get("diario", {}) or {})
    if not sezione.get("attivo", True):
        return Diario.spento()
    return Diario(
        sezione.get("dir", "logs/diario"),
        pseudonimo=bool(sezione.get("pseudonimo", False)),
        max_mb=float(sezione.get("max_mb", 50)),
    )


def leggi_diario(percorso: str | Path) -> Iterator[dict[str, Any]]:
    """Scorre i record di un diario, saltando le righe rotte.

    Una riga incompleta capita davvero: e' l'ultima di un file interrotto da un
    Ctrl+C. Fermare tutta la lettura per quella significherebbe perdere la
    sessione proprio quando e' andata storta.
    """
    file = Path(percorso)
    with file.open("r", encoding="utf-8") as sorgente:
        for numero, riga in enumerate(sorgente, start=1):
            riga = riga.strip()
            if not riga:
                continue
            try:
                record = json.loads(riga)
            except ValueError:
                log.debug("Riga %s di %s non leggibile: saltata", numero, file.name)
                continue
            if isinstance(record, dict):
                yield record


class BackendTracciato:
    """Avvolge un `RFIDBackend` e annota nel diario ogni scambio con la radio.

    Sta esattamente sul confine che l'architettura gia' definisce: non conosce
    frame, CRC ne' trasporti, e va bene sia sopra `RFIDService` sia sopra il
    client JSON-RPC. Tutto cio' che non e' nell'elenco dei metodi tracciati
    viene inoltrato senza toccarlo, comprese le proprieta' `state` e `ready`.
    """

    def __init__(self, backend: Any, diario: Diario):
        # `object.__setattr__` e l'accesso esplicito piu' avanti evitano il
        # rischio classico di questo schema: un attributo cercato prima di
        # essere impostato manderebbe `__getattr__` in ricorsione.
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_diario", diario)

    @property
    def backend(self) -> Any:
        """Il backend avvolto, per chi deve arrivarci davvero (i test)."""
        return object.__getattribute__(self, "_backend")

    @property
    def diario(self) -> Diario:
        return object.__getattribute__(self, "_diario")

    def __getattr__(self, nome: str) -> Any:
        backend = object.__getattribute__(self, "_backend")
        attributo = getattr(backend, nome)
        if nome not in _METODI_RADIO or not callable(attributo):
            return attributo
        diario = object.__getattribute__(self, "_diario")
        if not diario.attivo:
            return attributo

        def tracciato(*args: Any, **kwargs: Any) -> Any:
            avvio = time.perf_counter()
            try:
                risposta = attributo(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                diario.scrivi(
                    "radio",
                    nome,
                    {"eccezione": f"{type(exc).__name__}: {exc}"},
                    durata_ms=(time.perf_counter() - avvio) * 1000,
                )
                raise
            diario.scrivi(
                "radio",
                nome,
                _sintesi(args, kwargs, risposta),
                durata_ms=(time.perf_counter() - avvio) * 1000,
            )
            return risposta

        return tracciato


def _dizionario(valore: Any) -> Any:
    """Un DTO del servizio in forma leggibile, senza pretendere di conoscerlo."""
    if isinstance(valore, Mapping):
        return dict(valore)
    if dataclasses.is_dataclass(valore) and not isinstance(valore, type):
        try:
            return dataclasses.asdict(valore)
        except (TypeError, ValueError):
            pass
    campi = getattr(valore, "__dict__", None)
    return dict(campi) if isinstance(campi, dict) else str(valore)


def _sintesi(args: tuple, kwargs: dict, risposta: Any) -> dict[str, Any]:
    """Cosa conservare di una chiamata alla radio.

    `ServiceResponse.data` porta gia' la richiesta in forma JSON-safe e, per
    l'inventory, l'elenco completo dei tag con RSSI, antenna e conteggio: e'
    esattamente il materiale che serve per rimettere in piedi la scena senza
    lettore, quindi si tiene com'e'.
    """
    dati: dict[str, Any] = {}
    ok = getattr(risposta, "ok", None)
    if ok is not None:
        dati["ok"] = bool(ok)
    errore = getattr(risposta, "error", None)
    if errore:
        dati["errore"] = dict(errore) if isinstance(errore, Mapping) else str(errore)
    corpo = getattr(risposta, "data", None)
    if isinstance(corpo, Mapping):
        dati["dati"] = dict(corpo)
    elif corpo is not None:
        dati["dati"] = corpo
    # La richiesta si conserva sempre, a meno che la risposta la riporti gia'
    # per conto suo. Non e' ridondanza difensiva: `RFIDService` la riecheggia,
    # il banco simulato no, e senza la richiesta un `write_epc` nel diario non
    # dice quale EPC ha sostituito quale — cioe' proprio il dato che serve per
    # rimettere in piedi il campo.
    if args and not (isinstance(corpo, Mapping) and "request" in corpo):
        dati["richiesta"] = _dizionario(args[0])
    if kwargs:
        dati["parametri"] = dict(kwargs)
    stato = getattr(risposta, "state", None)
    if stato is not None:
        dati["stato"] = getattr(stato, "value", str(stato))
    return dati
