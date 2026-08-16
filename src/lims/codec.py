"""Schema binario dei dati scritti sul tag: EPC pseudonimo e payload del campione.

Due formati distinti, con ruoli diversi:

* **EPC (12 byte, in chiaro)** — lo pseudonimo. Non contiene alcun dato personale,
  ma abbastanza struttura da riconoscere una spedizione e contare i contenitori
  mancanti senza possedere la chiave di cifratura.
* **Payload (lunghezza variabile, destinato a essere cifrato)** — i dati del
  paziente e del reperto. Questo modulo produce il testo in chiaro; sigillarlo e'
  compito di `lims.crypto`.

Entrambi i formati iniziano con un byte di versione: un lettore che incontra una
versione che non conosce **deve fallire in modo esplicito**, mai interpretare i
byte alla cieca. Su un tag che viaggia fra laboratori l'unica compatibilita'
possibile e' quella dichiarata.

Il payload **non ripete** cio' che sta gia' nell'EPC: numero di accettazione e
numerazione del contenitore si leggono da li'. Non e' solo risparmio di spazio —
sono anche dati autenticati nel sigillo, quindi non possono discordare dal
payload, e una copia in piu' sarebbe una fonte di contraddizioni senza arbitro.
Per questo `unpack_payload` richiede l'`EpcInfo` del tag da cui i byte provengono.

Bilancio dello spazio, per un tag con 64 byte di USER memory (il limite di una
singola scrittura del driver, `reader.py:429`):

    64 byte USER
     - 4 byte  intestazione in chiaro (versione, key_id/revisione, lunghezza)
     - 16 byte tag di autenticazione GCM
     = 44 byte utili, di cui 18 fissi e 26 per "COGNOME NOME"
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from enum import IntFlag

from .model import normalize_codice_fiscale, normalize_name

__all__ = [
    "CODEBOOK_VERSION",
    "CF_PACKED_SIZE",
    "EPC_SCHEMA_V1",
    "EPC_SIZE",
    "FIXATIVES",
    "MATERIALS",
    "PAYLOAD_FIXED_SIZE",
    "PAYLOAD_SCHEMA_V1",
    "SITES",
    "EpcInfo",
    "SpecimenFlags",
    "TagPayload",
    "build_epc",
    "describe_fixative",
    "describe_material",
    "describe_site",
    "pack_codice_fiscale",
    "pack_payload",
    "parse_epc",
    "unpack_codice_fiscale",
    "unpack_payload",
]


# --------------------------------------------------------------------------
# Codebook condivisi fra i laboratori
# --------------------------------------------------------------------------
# Il codice numerico e' cio' che viaggia sul tag; la descrizione serve solo a
# mostrarla all'operatore. Aggiungere voci e' retrocompatibile, **riassegnare un
# codice esistente no**: cambierebbe il significato dei tag gia' in circolazione.
CODEBOOK_VERSION = 1

MATERIALS: dict[int, str] = {
    0: "non specificato",
    1: "biopsia",
    2: "agobiopsia",
    3: "pezzo operatorio",
    4: "escissione",
    5: "polipectomia",
    6: "citologico su liquido",
    7: "citologico per striscio",
    8: "brushing",
    9: "agoaspirato",
    10: "sangue",
    11: "midollo osseo",
    12: "urina",
    13: "tampone",
    14: "blocchetto in paraffina",
    15: "vetrino",
    16: "tessuto congelato",
    17: "placenta",
    18: "autopsia",
}

FIXATIVES: dict[int, str] = {
    0: "nessuno / a fresco",
    1: "formalina 10% tamponata",
    2: "formalina 4%",
    3: "alcool 95%",
    4: "Bouin",
    5: "Carnoy",
    6: "glutaraldeide",
    7: "terreno di trasporto RPMI",
    8: "soluzione fisiologica",
    9: "congelato",
    10: "sotto vuoto",
    11: "citofissativo spray",
}

SITES: dict[int, str] = {
    0: "non specificata",
    1: "cute",
    2: "mammella",
    3: "polmone",
    4: "stomaco",
    5: "colon",
    6: "retto",
    7: "esofago",
    8: "fegato",
    9: "pancreas",
    10: "rene",
    11: "vescica",
    12: "prostata",
    13: "utero",
    14: "cervice uterina",
    15: "ovaio",
    16: "tiroide",
    17: "linfonodo",
    18: "encefalo",
    19: "osso",
    20: "muscolo",
    21: "cavo orale",
    22: "laringe",
    23: "testicolo",
    24: "surrene",
    25: "milza",
}


def _describe(codebook: dict[int, str], code: int) -> str:
    return codebook.get(code, f"codice sconosciuto ({code})")


def describe_material(code: int) -> str:
    return _describe(MATERIALS, code)


def describe_fixative(code: int) -> str:
    return _describe(FIXATIVES, code)


def describe_site(code: int) -> str:
    return _describe(SITES, code)


class SpecimenFlags(IntFlag):
    """Avvertenze operative che devono raggiungere chi apre il contenitore."""

    NONE = 0
    URGENT = 0x01
    FROZEN = 0x02
    BIOBANK = 0x04
    INFECTIOUS = 0x08      # rischio biologico noto: va visto prima di manipolare
    DECALCIFIED = 0x10
    UNDER_VACUUM = 0x20


# --------------------------------------------------------------------------
# Impacchettamento del codice fiscale
# --------------------------------------------------------------------------
# 16 caratteri alfanumerici sono 16 byte in ASCII, ma solo 36^16 combinazioni
# possibili: interpretandoli come un intero in base 36 servono 83 bit, cioe'
# 11 byte. Cinque byte risparmiati su un payload da 46 non sono un dettaglio.
_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ALPHABET_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
_CF_LENGTH = 16
_CF_MAX_VALUE = 36 ** _CF_LENGTH
CF_PACKED_SIZE = 11


def pack_codice_fiscale(value: str) -> bytes:
    """Comprime un codice fiscale di 16 caratteri in 11 byte (base 36)."""
    cleaned = normalize_codice_fiscale(value)
    packed = 0
    for char in cleaned:
        try:
            packed = packed * 36 + _ALPHABET_INDEX[char]
        except KeyError:
            raise ValueError(f"carattere non alfanumerico nel codice fiscale: {char!r}") from None
    return packed.to_bytes(CF_PACKED_SIZE, "big")


def unpack_codice_fiscale(data: bytes) -> str:
    """Ricostruisce il codice fiscale dagli 11 byte impacchettati."""
    if len(data) != CF_PACKED_SIZE:
        raise ValueError(f"servono {CF_PACKED_SIZE} byte, ricevuti {len(data)}")
    packed = int.from_bytes(data, "big")
    if packed >= _CF_MAX_VALUE:
        raise ValueError("i byte non rappresentano un codice fiscale valido")
    chars = []
    for _ in range(_CF_LENGTH):
        packed, remainder = divmod(packed, 36)
        chars.append(_ALPHABET[remainder])
    return "".join(reversed(chars))


# --------------------------------------------------------------------------
# EPC pseudonimo (12 byte, in chiaro)
# --------------------------------------------------------------------------
EPC_SCHEMA_V1 = 0x01        # contenitore campione
EPC_SCHEMA_BOX = 0x02       # scatola di trasporto (tag sul coperchio)
EPC_SIZE = 12
_EPC_RANDOM_SIZE = 3
_BOX_RANDOM_SIZE = 4


@dataclass(frozen=True)
class EpcInfo:
    """Contenuto decodificato di un EPC generato da questo sistema."""

    schema: int
    lab_id: int
    accession_id: int
    container_index: int
    container_total: int
    random_hex: str

    @property
    def label(self) -> str:
        return f"{self.container_index}/{self.container_total}"


def build_epc(
    lab_id: int,
    accession_id: int,
    container_index: int,
    container_total: int,
    *,
    random_suffix: bytes | None = None,
) -> bytes:
    """Costruisce lo pseudonimo di 12 byte scritto nella banca EPC.

    Non contiene dati personali. La coda casuale impedisce di indovinare gli EPC
    di una spedizione a partire da uno solo di essi.
    """
    if not 0 <= lab_id <= 0xFFFF:
        raise ValueError("lab_id fuori intervallo 0..65535")
    if not 0 <= accession_id <= 0xFFFFFFFF:
        raise ValueError("accession_id fuori intervallo 0..4294967295")
    if not 1 <= container_total <= 0xFF:
        raise ValueError("container_total fuori intervallo 1..255")
    if not 1 <= container_index <= container_total:
        raise ValueError(f"container_index deve essere compreso tra 1 e {container_total}")
    if random_suffix is None:
        random_suffix = secrets.token_bytes(_EPC_RANDOM_SIZE)
    elif len(random_suffix) != _EPC_RANDOM_SIZE:
        raise ValueError(f"random_suffix deve essere di {_EPC_RANDOM_SIZE} byte")
    return b"".join(
        (
            bytes([EPC_SCHEMA_V1]),
            lab_id.to_bytes(2, "big"),
            accession_id.to_bytes(4, "big"),
            bytes([container_index, container_total]),
            random_suffix,
        )
    )


def parse_epc(data: bytes | str) -> EpcInfo:
    """Decodifica un EPC prodotto da `build_epc`.

    Solleva `ValueError` per gli EPC estranei al sistema: e' il comportamento
    voluto, cosi' la postazione di ricezione puo' segnalarli invece di ignorarli.
    """
    if isinstance(data, str):
        data = bytes.fromhex("".join(data.split()))
    if len(data) != EPC_SIZE:
        raise ValueError(f"un EPC di questo sistema e' di {EPC_SIZE} byte, ricevuti {len(data)}")
    schema = data[0]
    if schema != EPC_SCHEMA_V1:
        raise ValueError(f"versione schema EPC non supportata: 0x{schema:02X}")
    container_index = data[7]
    container_total = data[8]
    if not 1 <= container_total <= 0xFF or not 1 <= container_index <= container_total:
        raise ValueError(
            f"numerazione contenitore incoerente: {container_index}/{container_total}"
        )
    return EpcInfo(
        schema=schema,
        lab_id=int.from_bytes(data[1:3], "big"),
        accession_id=int.from_bytes(data[3:7], "big"),
        container_index=container_index,
        container_total=container_total,
        random_hex=data[9:12].hex().upper(),
    )


# --------------------------------------------------------------------------
# EPC della scatola di trasporto (tag sul coperchio)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class BoxInfo:
    """Identita' di una scatola di trasporto, letta dal tag sul coperchio."""

    schema: int
    lab_id: int
    box_id: int
    random_hex: str

    @property
    def label(self) -> str:
        return f"scatola {self.box_id}"


def build_box_epc(
    lab_id: int, box_id: int, *, random_suffix: bytes | None = None
) -> bytes:
    """EPC di una scatola di trasporto.

    Un tag sul coperchio dice **quale** scatola e', non che il coperchio sia
    chiuso: e' un'identita', non un sensore. Serve a legare la spedizione a un
    contenitore fisico preciso, cosi' che la distinta e il record di sigillo
    parlino di una scatola nominata e non di "quella sul banco".
    """
    if not 0 <= lab_id <= 0xFFFF:
        raise ValueError("lab_id fuori intervallo 0..65535")
    if not 0 <= box_id <= 0xFFFFFFFF:
        raise ValueError("box_id fuori intervallo 0..4294967295")
    if random_suffix is None:
        random_suffix = secrets.token_bytes(_BOX_RANDOM_SIZE)
    elif len(random_suffix) != _BOX_RANDOM_SIZE:
        raise ValueError(f"random_suffix deve essere di {_BOX_RANDOM_SIZE} byte")
    return (
        bytes([EPC_SCHEMA_BOX])
        + lab_id.to_bytes(2, "big")
        + box_id.to_bytes(4, "big")
        + bytes([0x00])          # riservato: tipo di scatola, per il futuro
        + random_suffix
    )


def parse_box_epc(data: bytes | str) -> BoxInfo:
    """Decodifica l'EPC di una scatola."""
    if isinstance(data, str):
        data = bytes.fromhex("".join(data.split()))
    if len(data) != EPC_SIZE:
        raise ValueError(f"un EPC di scatola e' di {EPC_SIZE} byte, ricevuti {len(data)}")
    if data[0] != EPC_SCHEMA_BOX:
        raise ValueError(f"non e' l'EPC di una scatola: schema 0x{data[0]:02X}")
    return BoxInfo(
        schema=data[0],
        lab_id=int.from_bytes(data[1:3], "big"),
        box_id=int.from_bytes(data[3:7], "big"),
        random_hex=data[8:12].hex().upper(),
    )


def epc_kind(data: bytes | str) -> str:
    """Che cosa e' questo EPC: `container`, `box` o `foreign`.

    Serve alla postazione di ricezione per smistare i tag letti senza doverli
    provare uno schema alla volta.
    """
    try:
        grezzo = bytes.fromhex("".join(data.split())) if isinstance(data, str) else bytes(data)
    except ValueError:
        return "foreign"
    if len(grezzo) != EPC_SIZE:
        return "foreign"
    if grezzo[0] == EPC_SCHEMA_V1:
        return "container"
    if grezzo[0] == EPC_SCHEMA_BOX:
        return "box"
    return "foreign"


# --------------------------------------------------------------------------
# Payload del campione (parte fissa + nome a lunghezza variabile)
# --------------------------------------------------------------------------
PAYLOAD_SCHEMA_V1 = 0x01
PAYLOAD_FIXED_SIZE = 18

# Le date viaggiano come giorni trascorsi da questa epoca: due byte coprono
# 179 anni, contro i tre o piu' di una data ISO o di un timestamp.
_DATE_EPOCH = date(2020, 1, 1)
_DATE_UNKNOWN = 0xFFFF


def _encode_date(value: date | None) -> int:
    if value is None:
        return _DATE_UNKNOWN
    if not isinstance(value, date):
        raise TypeError("la data deve essere un datetime.date")
    delta = (value - _DATE_EPOCH).days
    if not 0 <= delta < _DATE_UNKNOWN:
        raise ValueError(
            f"data fuori dall'intervallo rappresentabile "
            f"({_DATE_EPOCH.isoformat()} .. 2199): {value.isoformat()}"
        )
    return delta


def _decode_date(value: int) -> date | None:
    if value == _DATE_UNKNOWN:
        return None
    from datetime import timedelta

    return _DATE_EPOCH + timedelta(days=value)


@dataclass(frozen=True)
class TagPayload:
    """I dati del campione, in chiaro, prima di essere sigillati.

    `accession_id`, `container_index` e `container_total` fanno parte del record
    logico ma **non vengono serializzati**: viaggiano nell'EPC. In lettura li
    reinserisce `unpack_payload` a partire dall'`EpcInfo`.

    `display_name` e' l'unico campo a lunghezza variabile e occupa tutto lo
    spazio residuo: si accorcia quando serve, senza far fallire la scrittura.
    """

    codice_fiscale: str
    accession_id: int
    container_index: int
    container_total: int
    display_name: str = ""
    data_prelievo: date | None = None
    material_code: int = 0
    fixative_code: int = 0
    site_code: int = 0
    flags: SpecimenFlags = SpecimenFlags.NONE
    schema: int = PAYLOAD_SCHEMA_V1
    name_truncated: bool = False

    def describe(self) -> dict[str, object]:
        """Vista leggibile, per la schermata di ricezione e per i log."""
        return {
            "codice_fiscale": self.codice_fiscale,
            "paziente": self.display_name,
            "accettazione": self.accession_id,
            "contenitore": f"{self.container_index}/{self.container_total}",
            "data_prelievo": self.data_prelievo.isoformat() if self.data_prelievo else None,
            "materiale": describe_material(self.material_code),
            "fissativo": describe_fixative(self.fixative_code),
            "sede": describe_site(self.site_code),
            "avvertenze": [flag.name for flag in SpecimenFlags if flag and flag in self.flags],
        }


def pack_payload(payload: TagPayload, *, max_bytes: int | None = None) -> bytes:
    """Serializza il payload in chiaro, troncando il nome allo spazio disponibile.

    `max_bytes` e' lo spazio utile nella USER memory al netto di intestazione e
    tag di autenticazione. Il troncamento riguarda **solo** il nome: tutti i
    campi identificativi strutturati sono preservati integralmente.
    """
    if payload.schema != PAYLOAD_SCHEMA_V1:
        raise ValueError(f"versione schema payload non supportata: 0x{payload.schema:02X}")
    if not 1 <= payload.container_total <= 0xFF:
        raise ValueError("container_total fuori intervallo 1..255")
    if not 1 <= payload.container_index <= payload.container_total:
        raise ValueError(
            f"container_index deve essere compreso tra 1 e {payload.container_total}"
        )
    if not 0 <= payload.accession_id <= 0xFFFFFFFF:
        raise ValueError("accession_id fuori intervallo 0..4294967295")
    for name in ("material_code", "fixative_code", "site_code"):
        code = getattr(payload, name)
        if not 0 <= code <= 0xFF:
            raise ValueError(f"{name} fuori intervallo 0..255")

    if max_bytes is not None:
        if max_bytes < PAYLOAD_FIXED_SIZE:
            raise ValueError(
                f"spazio insufficiente: servono almeno {PAYLOAD_FIXED_SIZE} byte "
                f"per la parte fissa, disponibili {max_bytes}"
            )
        name_budget = max_bytes - PAYLOAD_FIXED_SIZE
    else:
        name_budget = None

    name = normalize_name(payload.display_name).encode("ascii", "ignore")
    if name_budget is not None:
        name = name[:name_budget]

    head = b"".join(
        (
            bytes([PAYLOAD_SCHEMA_V1]),
            pack_codice_fiscale(payload.codice_fiscale),
            _encode_date(payload.data_prelievo).to_bytes(2, "big"),
            bytes(
                [
                    payload.material_code,
                    payload.fixative_code,
                    payload.site_code,
                    int(payload.flags) & 0xFF,
                ]
            ),
        )
    )
    assert len(head) == PAYLOAD_FIXED_SIZE, "parte fissa di dimensione inattesa"
    return head + name


def unpack_payload(data: bytes, epc_info: EpcInfo) -> TagPayload:
    """Deserializza un payload in chiaro, completandolo con i dati dell'EPC.

    `epc_info` proviene dallo stesso tag da cui arrivano i byte: il sigillo
    autentica EPC e payload insieme, quindi i due non possono appartenere a
    contenitori diversi.
    """
    if len(data) < PAYLOAD_FIXED_SIZE:
        raise ValueError(
            f"payload troppo corto: {len(data)} byte, minimo {PAYLOAD_FIXED_SIZE}"
        )
    schema = data[0]
    if schema != PAYLOAD_SCHEMA_V1:
        raise ValueError(f"versione schema payload non supportata: 0x{schema:02X}")

    name = data[PAYLOAD_FIXED_SIZE:].decode("ascii", "ignore").rstrip("\x00").strip()
    return TagPayload(
        schema=schema,
        codice_fiscale=unpack_codice_fiscale(data[1:12]),
        data_prelievo=_decode_date(int.from_bytes(data[12:14], "big")),
        material_code=data[14],
        fixative_code=data[15],
        site_code=data[16],
        flags=SpecimenFlags(data[17]),
        display_name=name,
        accession_id=epc_info.accession_id,
        container_index=epc_info.container_index,
        container_total=epc_info.container_total,
    )
