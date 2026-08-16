"""Modello di dominio per la tracciabilita' dei campioni istologici.

Le entita' qui definite sono pure: nessun accesso al database, nessun I/O RFID.
La gerarchia rispecchia il flusso reale di un laboratorio di anatomia patologica:

    Patient (paziente)
      └── Case (accettazione: una richiesta, un prelievo)
            └── Specimen (reperto: un materiale prelevato)
                  └── Container (contenitore fisico: 1 di N, un tag RFID ciascuno)

Un `Container` e' l'unita' che viaggia e che porta il tag: e' l'unico oggetto con
un EPC e un TID associati.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from enum import Enum

__all__ = [
    "CODICE_FISCALE_RE",
    "Case",
    "Container",
    "ContainerState",
    "Patient",
    "Sex",
    "Shipment",
    "ShipmentState",
    "Specimen",
    "TagState",
    "normalize_name",
    "normalize_codice_fiscale",
    "codice_fiscale_check_char",
    "validate_codice_fiscale",
]


# Forma permissiva del codice fiscale: tiene conto dell'omocodia, cioe' della
# sostituzione delle cifre con lettere quando due persone genererebbero lo stesso
# codice. Le posizioni numeriche accettano quindi anche L M N P Q R S T U V.
_OMOCODIA_DIGITS = "LMNPQRSTUV"
CODICE_FISCALE_RE = re.compile(
    r"^[A-Z]{6}"
    rf"[0-9{_OMOCODIA_DIGITS}]{{2}}"
    r"[A-Z]"
    rf"[0-9{_OMOCODIA_DIGITS}]{{2}}"
    r"[A-Z]"
    rf"[0-9{_OMOCODIA_DIGITS}]{{3}}"
    r"[A-Z]$"
)

# Tabelle ufficiali per il carattere di controllo (DM 23/12/1976).
_CHECK_ODD = {
    "0": 1, "1": 0, "2": 5, "3": 7, "4": 9, "5": 13, "6": 15, "7": 17, "8": 19, "9": 21,
    "A": 1, "B": 0, "C": 5, "D": 7, "E": 9, "F": 13, "G": 15, "H": 17, "I": 19, "J": 21,
    "K": 2, "L": 4, "M": 18, "N": 20, "O": 11, "P": 3, "Q": 6, "R": 8, "S": 12, "T": 14,
    "U": 16, "V": 10, "W": 22, "X": 25, "Y": 24, "Z": 23,
}
_CHECK_EVEN = {
    **{str(digit): digit for digit in range(10)},
    **{chr(ord("A") + offset): offset for offset in range(26)},
}


def normalize_codice_fiscale(value: str) -> str:
    """Ripulisce un codice fiscale: maiuscolo, senza spazi ne' punteggiatura."""
    if not isinstance(value, str):
        raise TypeError("il codice fiscale deve essere una stringa")
    cleaned = "".join(ch for ch in value.upper() if ch.isalnum())
    if len(cleaned) != 16:
        raise ValueError(
            f"il codice fiscale deve avere 16 caratteri, ricevuti {len(cleaned)}"
        )
    return cleaned


def codice_fiscale_check_char(first_fifteen: str) -> str:
    """Calcola il sedicesimo carattere di controllo dai primi quindici."""
    if len(first_fifteen) != 15:
        raise ValueError("servono esattamente 15 caratteri per il carattere di controllo")
    total = 0
    for position, char in enumerate(first_fifteen, start=1):
        table = _CHECK_ODD if position % 2 == 1 else _CHECK_EVEN
        if char not in table:
            raise ValueError(f"carattere non ammesso nel codice fiscale: {char!r}")
        total += table[char]
    return chr(ord("A") + total % 26)


def validate_codice_fiscale(value: str) -> str:
    """Normalizza e verifica forma e carattere di controllo. Ritorna il codice pulito.

    La verifica del carattere di controllo intercetta gli errori di battitura in
    accettazione: su un tag che viaggia senza database di riscontro, un codice
    fiscale sbagliato non e' piu' correggibile a destinazione.
    """
    cleaned = normalize_codice_fiscale(value)
    if not CODICE_FISCALE_RE.match(cleaned):
        raise ValueError(f"codice fiscale con formato non valido: {cleaned}")
    expected = codice_fiscale_check_char(cleaned[:15])
    if cleaned[15] != expected:
        raise ValueError(
            f"codice fiscale con carattere di controllo errato: atteso {expected}, "
            f"trovato {cleaned[15]}"
        )
    return cleaned


def normalize_name(value: str) -> str:
    """Riduce un nome ad ASCII maiuscolo adatto alla scrittura sul tag.

    Gli accenti vengono decomposti e scartati (PERUZZI NICOLO' -> PERUZZI NICOLO'),
    perche' il payload del tag e' ASCII: un carattere non rappresentabile andrebbe
    perso in modo silenzioso a destinazione.
    """
    if not isinstance(value, str):
        raise TypeError("il nome deve essere una stringa")
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    upper = stripped.upper()
    kept = [ch if ch.isalnum() or ch in " '-" else " " for ch in upper]
    collapsed = " ".join("".join(kept).split())
    return "".join(ch for ch in collapsed if ch.isascii())


class Sex(str, Enum):
    MALE = "M"
    FEMALE = "F"
    UNKNOWN = "X"


class ContainerState(str, Enum):
    """Ciclo di vita di un contenitore fisico."""

    PLANNED = "planned"          # registrato a sistema, tag non ancora scritto
    PROVISIONED = "provisioned"  # tag scritto e riletto con successo
    SHIPPED = "shipped"          # inserito in una spedizione
    RECEIVED = "received"        # letto dal laboratorio destinatario
    VOIDED = "voided"            # annullato prima della spedizione: contenitore
                                 # rotto o tag guasto. Il campione passa a un
                                 # contenitore nuovo, non si riscrive nulla.


class TagState(str, Enum):
    """Stato di un tag fisico, identificato dal suo TID.

    Il tag **non torna indietro**: parte col contenitore e resta al laboratorio
    destinatario, dove il campione va conservato intatto. Non c'e' quindi un
    ciclo di riuso, ma un percorso a senso unico che finisce in `SHIPPED`.

    `VOIDED` e' l'unico modo in cui un tag esce dal percorso prima della
    spedizione: il contenitore si e' rotto, o il tag stesso non ha funzionato.
    In quel caso il campione passa a un altro contenitore con un altro tag.
    """

    FREE = "free"              # censito, mai scritto
    ASSIGNED = "assigned"      # scritto e verificato, in attesa di spedizione
    SHIPPED = "shipped"        # partito: non tornera' e non va piu' toccato
    VOIDED = "voided"          # scartato prima della spedizione
    QUARANTINE = "quarantine"  # difettoso, da verificare prima di riusarlo
    RETIRED = "retired"        # fuori servizio definitivamente


class ShipmentState(str, Enum):
    OPEN = "open"
    SEALED = "sealed"
    SENT = "sent"
    RECEIVED = "received"


@dataclass
class Patient:
    """Anagrafica del paziente. `codice_fiscale` e' la chiave naturale."""

    codice_fiscale: str
    cognome: str
    nome: str
    data_nascita: date | None = None
    sesso: Sex = Sex.UNKNOWN
    id: int | None = None

    def __post_init__(self) -> None:
        self.codice_fiscale = validate_codice_fiscale(self.codice_fiscale)
        self.cognome = normalize_name(self.cognome)
        self.nome = normalize_name(self.nome)
        if not self.cognome:
            raise ValueError("cognome obbligatorio")
        if not self.nome:
            raise ValueError("nome obbligatorio")
        if not isinstance(self.sesso, Sex):
            self.sesso = Sex(str(self.sesso).upper())

    @property
    def display_name(self) -> str:
        """Forma "COGNOME NOME" usata sul tag e nelle liste."""
        return f"{self.cognome} {self.nome}"


@dataclass
class Case:
    """Accettazione: una richiesta di esame con la sua data di prelievo.

    `accession_id` e' il numero di accettazione, univoco all'interno del
    laboratorio di origine: finisce sia nell'EPC sia nel payload cifrato.
    """

    accession_id: int
    patient_id: int | None = None
    data_prelievo: date | None = None
    reparto: str = ""
    medico: str = ""
    note: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.accession_id, int) or isinstance(self.accession_id, bool):
            raise TypeError("accession_id deve essere un intero")
        if not 0 <= self.accession_id <= 0xFFFFFFFF:
            raise ValueError("accession_id fuori intervallo 0..4294967295")


@dataclass
class Specimen:
    """Reperto: il materiale prelevato, descritto tramite codebook condivisi."""

    descrizione: str = ""
    material_code: int = 0
    site_code: int = 0
    fixative_code: int = 0
    case_id: int | None = None
    id: int | None = None

    def __post_init__(self) -> None:
        for name in ("material_code", "site_code", "fixative_code"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} deve essere un intero")
            if not 0 <= value <= 0xFF:
                raise ValueError(f"{name} fuori intervallo 0..255")


@dataclass
class Container:
    """Contenitore fisico con il suo tag: `index` di `total` per lo stesso reperto."""

    index: int
    total: int
    specimen_id: int | None = None
    epc: str = ""
    tid: str = ""
    state: ContainerState = ContainerState.PLANNED
    revision: int = 0
    provisioned_at: str = ""
    id: int | None = None

    def __post_init__(self) -> None:
        for name in ("index", "total"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} deve essere un intero")
        if not 1 <= self.total <= 255:
            raise ValueError("total deve essere compreso tra 1 e 255")
        if not 1 <= self.index <= self.total:
            raise ValueError(f"index deve essere compreso tra 1 e {self.total}")
        if not 0 <= self.revision <= 15:
            raise ValueError("revision deve essere compresa tra 0 e 15")
        self.epc = self.epc.strip().upper()
        self.tid = self.tid.strip().upper()
        if not isinstance(self.state, ContainerState):
            self.state = ContainerState(str(self.state))

    @property
    def label(self) -> str:
        """Etichetta leggibile, es. "2/3"."""
        return f"{self.index}/{self.total}"


@dataclass
class Shipment:
    """Spedizione verso il laboratorio successivo."""

    destinazione: str
    data: date | None = None
    state: ShipmentState = ShipmentState.OPEN
    note: str = ""
    container_ids: list[int] = field(default_factory=list)
    id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ShipmentState):
            self.state = ShipmentState(str(self.state))
