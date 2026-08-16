"""Scoperta delle capacita' di memoria di un tag sconosciuto.

Nessun datasheet del tag e' mai entrato nel progetto: l'offerta Silion copre
modulo, baseboard e antenne, non i tag. Quanti dati si possono scrivere dipende
pero' interamente dal chip, non dal lettore. Questo modulo risponde alla domanda
misurando, invece di fidarsi di una tabella:

1. legge il banco TID e ne decodifica costruttore e modello (ISO/IEC 15963);
2. **misura** la USER memory leggendo a indirizzi crescenti finche' il tag
   risponde, con ricerca binaria;
3. verifica che il TID sia serializzato, requisito del legame anti-clonazione.

Usa soltanto `RFIDBackend`: nessuna modifica al driver e' necessaria, perche'
`ReadRequest` accetta gia' banco e indirizzo arbitrari.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from .codec import PAYLOAD_FIXED_SIZE
from .crypto import max_plaintext_bytes
from .responses import ServiceCallError, first_read, inventory_epcs, raise_for_status

__all__ = [
    "MANUFACTURERS",
    "TID_AAD_BYTES",
    "TagLostError",
    "TagProfile",
    "TidInfo",
    "decode_tid",
    "profile_tag",
    "summarize",
]

log = logging.getLogger("lims.profiler")

# Il legame crittografico con il chip usa **sempre** i primi 12 byte del banco
# TID. La lunghezza deve essere fissa e uguale nei due laboratori: il
# destinatario non ha modo di sapere quanti byte aveva letto il mittente.
# Dodici byte sono la lunghezza di un TID serializzato Gen2v2 (classe + MDID +
# TMN + numero di serie a 48 bit); un tag che non li fornisce non e' adatto.
TID_AAD_BYTES = 12
TID_AAD_WORDS = TID_AAD_BYTES // 2

_TID_HEADER_WORDS = 2      # classe + MDID + TMN: presenti su qualsiasi tag Gen2
_MAX_PROBE_WORDS = 2048    # 4 kB di USER memory: oltre nessun chip UHF corrente

# Identificativi di costruttore (MDID) di cui la corrispondenza e' consolidata.
# La tabella serve solo a dare un nome leggibile: il dato che conta e' la
# dimensione **misurata**, non quella dedotta dal modello.
MANUFACTURERS: dict[int, str] = {
    0x001: "Impinj",
    0x002: "Texas Instruments",
    0x003: "Alien Technology",
    0x005: "Atmel / Microchip",
    0x006: "NXP Semiconductors",
    0x00B: "EM Microelectronic",
    0x00F: "Quanray",
    0x010: "Fujitsu",
}


class TagLostError(RuntimeError):
    """Il tag ha smesso di rispondere durante la profilazione.

    Distinta dal raggiungimento del limite di memoria: un tag che esce dal campo
    produrrebbe altrimenti una misura falsa e piu' piccola del vero.
    """


class _Backend(Protocol):
    def inventory(self, request: Any) -> Any: ...
    def read(self, request: Any) -> Any: ...


@dataclass(frozen=True)
class TidInfo:
    """Identita' di fabbrica del chip, decodificata dal banco TID."""

    tid_hex: str
    class_id: int
    mdid: int
    tmn: int
    xtid: bool

    @property
    def manufacturer(self) -> str:
        return MANUFACTURERS.get(self.mdid, f"costruttore sconosciuto (MDID 0x{self.mdid:03X})")

    @property
    def is_epc_class(self) -> bool:
        """0xE2 identifica i tag EPCglobal Gen2; altri valori sono altre classi."""
        return self.class_id == 0xE2

    @property
    def serialized(self) -> bool:
        """Vero se il TID contiene un numero di serie univoco per esemplare.

        Un TID tutto a zeri oltre l'intestazione identifica il modello ma non
        l'esemplare: non basta a legare il payload a un chip specifico.
        """
        if len(self.tid_hex) < TID_AAD_BYTES * 2:
            return False
        serie = self.tid_hex[_TID_HEADER_WORDS * 4:]
        return bool(serie) and set(serie) != {"0"}

    def describe(self) -> str:
        return (
            f"{self.manufacturer}, modello 0x{self.tmn:03X}"
            f"{', TID serializzato' if self.serialized else ', TID non serializzato'}"
        )


def decode_tid(data: bytes) -> TidInfo:
    """Decodifica l'intestazione del banco TID secondo EPC Gen2 / ISO 15963.

    Struttura dei primi 32 bit: 8 bit di classe (0xE2 per EPCglobal), 1 bit di
    indicatore XTID, 11 bit di identificativo costruttore, 12 bit di modello.
    """
    if len(data) < 4:
        raise ValueError(f"il TID richiede almeno 4 byte, ricevuti {len(data)}")
    return TidInfo(
        tid_hex=data.hex().upper(),
        class_id=data[0],
        xtid=bool(data[1] & 0x80),
        mdid=((data[1] & 0x7F) << 4) | (data[2] >> 4),
        tmn=((data[2] & 0x0F) << 8) | data[3],
    )


@dataclass
class TagProfile:
    """Esito della profilazione, serializzabile in JSON."""

    ok: bool = False
    epc: str = ""
    tid: TidInfo | None = None
    user_words: int = 0
    user_bytes: int = 0
    user_capped: bool = False
    usable_payload_bytes: int = 0
    tier: str = "sconosciuto"
    recommendation: str = ""
    reads_performed: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    @property
    def suitable(self) -> bool:
        """Vero se questo tag puo' portare un campione con lo schema corrente.

        Servono due cose insieme: spazio sufficiente per la parte fissa del
        payload e un TID serializzato su cui ancorare l'autenticazione.
        """
        return (
            self.ok
            and self.usable_payload_bytes >= PAYLOAD_FIXED_SIZE
            and self.tid is not None
            and self.tid.serialized
        )

    def to_dict(self) -> dict[str, Any]:
        documento: dict[str, Any] = {
            "ok": self.ok,
            "epc": self.epc,
            "user_words": self.user_words,
            "user_bytes": self.user_bytes,
            "user_capped": self.user_capped,
            "usable_payload_bytes": self.usable_payload_bytes,
            "tier": self.tier,
            "recommendation": self.recommendation,
            "suitable": self.suitable,
            "reads_performed": self.reads_performed,
            "warnings": list(self.warnings),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.error:
            documento["error"] = self.error
        if self.tid is not None:
            documento["tid"] = {
                "hex": self.tid.tid_hex,
                "class_id": f"0x{self.tid.class_id:02X}",
                "mdid": f"0x{self.tid.mdid:03X}",
                "tmn": f"0x{self.tid.tmn:03X}",
                "xtid": self.tid.xtid,
                "manufacturer": self.tid.manufacturer,
                "serialized": self.tid.serialized,
            }
        return documento


def _classify(user_bytes: int) -> tuple[str, str]:
    """Assegna il tier e la raccomandazione conseguente."""
    utilizzabili = max_plaintext_bytes(user_bytes)
    if user_bytes == 0:
        return "A", (
            "il chip non ha USER memory: sul tag entra solo lo pseudonimo EPC. "
            "Il requisito di far viaggiare i dati nel tag non e' soddisfacibile "
            "con questi tag, vanno sostituiti."
        )
    if utilizzabili < PAYLOAD_FIXED_SIZE:
        return "A+", (
            f"USER memory troppo piccola: {utilizzabili} byte utili contro i "
            f"{PAYLOAD_FIXED_SIZE} della sola parte fissa del payload. Servono tag "
            "con almeno 512 bit di USER memory."
        )
    if user_bytes < 64:
        return "B-", (
            f"si possono scrivere i dati strutturati ({utilizzabili} byte utili), "
            "ma per il nome del paziente resta poco o nulla."
        )
    if user_bytes < 128:
        return "B", (
            f"dimensione adatta allo schema corrente: {utilizzabili} byte utili, "
            "sufficienti per i dati strutturati e per il nome, in una sola scrittura."
        )
    return "C", (
        f"{utilizzabili} byte utili: oltre allo schema corrente c'e' spazio per un "
        "profilo esteso (descrizione del reperto, reparto, medico). La scrittura "
        "dovra' essere spezzata in blocchi da 64 byte."
    )


def profile_tag(
    backend: _Backend,
    *,
    antennas: tuple[int, ...] = (1, 2, 3),
    access_password_hex: str = "00000000",
    timeout_ms: int = 1000,
    max_probe_words: int = _MAX_PROBE_WORDS,
) -> TagProfile:
    """Profila l'unico tag presente nel campo.

    Richiede **un solo tag in campo**: con l'opzione 0x05 i comandi di lettura
    agiscono sul primo tag che risponde, quindi con piu' tag la misura
    mescolerebbe chip diversi.
    """
    from rfid_silion.service import InventoryRequest, MemoryBank, ReadRequest

    profilo = TagProfile(started_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    letture = 0

    def _leggi(bank: int, address: int, words: int, *, attempts: int = 2):
        """Legge ritentando: una lettura persa per rumore radio non e' un esito.

        Vale sia per il TID sia per la USER memory. Nella misura della memoria la
        distinzione e' critica: un fallimento accidentale verrebbe scambiato per
        il confine della banca e la dimensione risulterebbe piu' piccola del vero.
        """
        nonlocal letture
        risposta = None
        for _ in range(max(1, attempts)):
            letture += 1
            risposta = backend.read(
                ReadRequest(
                    bank=bank,
                    address=address,
                    word_count=words,
                    antennas=antennas,
                    access_password_hex=access_password_hex,
                    timeout_ms=timeout_ms,
                )
            )
            if getattr(risposta, "ok", False):
                return risposta
        return risposta

    def _leggibile(address: int, words: int = 1) -> bool:
        return bool(getattr(_leggi(MemoryBank.USER, address, words), "ok", False))

    try:
        inventario = raise_for_status(
            backend.inventory(InventoryRequest(antennas=antennas, timeout_ms=timeout_ms)),
            "inventory iniziale",
        )
        epcs = inventory_epcs(inventario)
        if not epcs:
            raise TagLostError("nessun tag nel campo: avvicinare un solo tag alle antenne")
        if len(epcs) > 1:
            raise TagLostError(
                f"nel campo ci sono {len(epcs)} tag: la profilazione richiede un tag solo "
                f"({', '.join(epcs)})"
            )
        profilo.epc = epcs[0]

        # --- identita' del chip ------------------------------------------
        intestazione = raise_for_status(
            _leggi(MemoryBank.TID, 0, _TID_HEADER_WORDS), "lettura TID"
        )
        _, tid_bytes = first_read(intestazione)

        # Il TID esteso non e' presente su tutti i chip: si tenta, non si pretende.
        esteso = _leggi(MemoryBank.TID, 0, TID_AAD_WORDS)
        if getattr(esteso, "ok", False):
            try:
                _, tid_bytes = first_read(esteso)
            except ServiceCallError:
                pass
        profilo.tid = decode_tid(tid_bytes)

        if not profilo.tid.is_epc_class:
            profilo.warnings.append(
                f"classe TID 0x{profilo.tid.class_id:02X} anziche' 0xE2: non e' un tag EPC Gen2"
            )
        if not profilo.tid.serialized:
            profilo.warnings.append(
                f"TID non serializzato ({len(tid_bytes)} byte letti, ne servono "
                f"{TID_AAD_BYTES}): senza numero di serie di fabbrica il payload non puo' "
                "essere legato al chip e la difesa anti-clonazione decade"
            )

        # --- misura della USER memory ------------------------------------
        if not _leggibile(0):
            # Un tag che non risponde piu' darebbe lo stesso esito di un tag
            # senza USER memory: si distinguono i due casi rileggendo il TID.
            if not getattr(_leggi(MemoryBank.TID, 0, _TID_HEADER_WORDS), "ok", False):
                raise TagLostError(
                    "il tag ha smesso di rispondere durante la misura: rifare la prova "
                    "con il tag fermo davanti a un'antenna"
                )
            profilo.user_words = 0
        else:
            basso, alto = 1, 2
            while alto <= max_probe_words and _leggibile(alto - 1):
                basso, alto = alto, alto * 2
            if alto > max_probe_words and _leggibile(max_probe_words - 1):
                profilo.user_words = max_probe_words
                profilo.user_capped = True
                profilo.warnings.append(
                    f"misura interrotta al limite di {max_probe_words} word: la USER "
                    "memory e' almeno di questa dimensione"
                )
            else:
                # `basso` e' leggibile, `alto` no: la dimensione sta nel mezzo.
                while basso < alto:
                    mezzo = (basso + alto + 1) // 2
                    if _leggibile(mezzo - 1):
                        basso = mezzo
                    else:
                        alto = mezzo - 1
                profilo.user_words = basso

            if not _leggibile(0):
                raise TagLostError(
                    "il tag ha smesso di rispondere prima della fine della misura: "
                    "il risultato sarebbe piu' piccolo del vero"
                )

        profilo.user_bytes = profilo.user_words * 2
        profilo.usable_payload_bytes = max_plaintext_bytes(profilo.user_bytes)
        profilo.tier, profilo.recommendation = _classify(profilo.user_bytes)
        profilo.ok = True
        log.info(
            "Profilo tag: %s, USER %d byte, tier %s",
            profilo.tid.describe() if profilo.tid else "TID sconosciuto",
            profilo.user_bytes,
            profilo.tier,
        )
    except (TagLostError, ServiceCallError, ValueError) as exc:
        profilo.error = str(exc)
        profilo.ok = False
        log.warning("Profilazione fallita: %s", exc)

    profilo.reads_performed = letture
    profilo.finished_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    return profilo


def summarize(profile: TagProfile) -> str:
    """Frase conclusiva da mostrare all'operatore."""
    if not profile.ok:
        return f"Profilazione non riuscita: {profile.error}"
    esito = "utilizzabile" if profile.suitable else "NON utilizzabile"
    return f"Tier {profile.tier} — {esito}. {profile.recommendation}"
