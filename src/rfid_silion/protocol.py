"""Livello protocollo Silion (SIM7200).

Frame Host->Reader:  0xFF | DataLen | CmdCode | Data[N] | CRC16(MSB,LSB)
Frame Reader->Host:  0xFF | DataLen | CmdCode | Status(2) | Data[N] | CRC16(MSB,LSB)

CRC-16 CCITT, poly 0x1021, init 0xFFFF, calcolato su tutti i byte eccetto
l'header 0xFF e i 2 byte di CRC (come CalcCRC del manuale, loop da i=1).

Esiste un **secondo formato**, detto esteso o "Moduletech" (manuale EX10 2024-12
§3.2), usato dai comandi a due byte di opcode (0xAA48 inventory asincrono, 0xAA58
modalita' tag densi, 0xAA4A rilevamento onda stazionaria, 0xAA4C filtro
multi-tag). Il CRC esterno e' lo stesso; cambia il contenuto del campo Data.
Vedi `build_extended_packet` / `parse_extended_response`.
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER = 0xFF
_POLY = 0x1021
_INIT = 0xFFFF


class SilionFrameError(Exception):
    """Errore di framing/CRC nella risposta del lettore."""


class SilionTimeoutError(SilionFrameError):
    """Timeout del trasporto: nessuna risposta (completa) dal lettore.

    Sottoclasse di SilionFrameError per retrocompatibilita' (chi intercettava
    SilionFrameError continua a catturare anche i timeout), ma distinguibile
    per la diagnostica (contatore `timeouts` vs `frame_errors`).
    """


class SilionTransportError(SilionFrameError):
    """Errore I/O del trasporto (porta chiusa, peer TCP disconnesso, ecc.)."""


def crc16(payload: bytes) -> int:
    """CRC-16 CCITT (poly 0x1021, init 0xFFFF) su `payload`.

    Implementazione fedele a `CRC_calcCrc8` del manuale: i bit del byte di dato
    (MSB first) entrano nel registro dal LSB; il payload deve escludere l'header
    0xFF e i 2 byte di CRC.
    """
    crc = _INIT
    for u8 in payload:
        mask = 0x80
        for _ in range(8):
            xor_flag = crc & 0x8000
            crc = (crc << 1) & 0xFFFF
            bit = 1 if (u8 & mask) else 0
            crc |= bit
            if xor_flag:
                crc ^= _POLY
            mask >>= 1
    return crc & 0xFFFF


def build_packet(cmd: int, data: bytes = b"") -> bytes:
    """Costruisce un pacchetto Host->Reader completo (con CRC)."""
    if len(data) > 252:
        raise ValueError(f"Data too long ({len(data)} > 252), frame limit 255 bytes")
    # payload per CRC = DataLen + Cmd + Data (esclude header e CRC)
    body = bytes([len(data), cmd]) + data
    crc = crc16(body)
    return bytes([HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


# --------------------------------------------------------------------------
# Formato esteso "Moduletech" (manuale EX10 2024-12 §3.2)
# --------------------------------------------------------------------------
# Invio:    FF | DataLen | AA | "Moduletech"(10) | SubCmd(2) | SubData(N) | SubCRC(1) | BB | CRC(2)
# Risposta: FF | DataLen | AA | Status(2) | "Moduletech"(10) | SubCmd(2) | SubData(N) | CRC(2)
#
# `DataLen` copre da "Moduletech" al terminatore incluso in invio, e da
# "Moduletech" alla fine di SubData in risposta (lo Status non vi rientra, come
# nel formato comune).
CMD_EXTENDED = 0xAA
EXTENDED_MARKER = b"Moduletech"
EXTENDED_TERMINATOR = 0xBB


def extended_subcrc(subcmd: int, subdata: bytes = b"") -> int:
    """Byte di controllo del frame esteso: somma di SubCmd e SubData, modulo 256.

    Il manuale non pubblica la formula, solo esempi. Questa e' ricavata da quelli
    e verificata contro tutti e cinque (0xAA48, 0xAA49, 0xAA58, 0xAA4D, 0xAA4A);
    `test_extended_protocol.py` li conserva come casi noti, cosi' un'eventuale
    smentita da hardware reale si manifesta come test rosso e non come guasto
    silenzioso.
    """
    return (((subcmd >> 8) & 0xFF) + (subcmd & 0xFF) + sum(subdata)) & 0xFF


def build_extended_packet(subcmd: int, subdata: bytes = b"") -> bytes:
    """Costruisce un pacchetto esteso Host->Reader completo."""
    if not 0 <= subcmd <= 0xFFFF:
        raise ValueError(f"subcmd out of range 0..0xFFFF: {subcmd}")
    payload = (
        EXTENDED_MARKER
        + bytes([(subcmd >> 8) & 0xFF, subcmd & 0xFF])
        + subdata
        + bytes([extended_subcrc(subcmd, subdata), EXTENDED_TERMINATOR])
    )
    if len(payload) > 252:
        raise ValueError(
            f"Extended data too long ({len(payload)} > 252), frame limit 255 bytes"
        )
    body = bytes([len(payload), CMD_EXTENDED]) + payload
    crc = crc16(body)
    return bytes([HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


@dataclass
class ExtendedResponse:
    subcmd: int
    status: int
    data: bytes

    @property
    def ok(self) -> bool:
        return self.status == 0x0000


def parse_extended_response(buf: bytes) -> ExtendedResponse:
    """Parser di una risposta completa nel formato esteso.

    Le risposte estese **non** portano SubCRC ne' terminatore: il manuale li
    prevede solo in invio.
    """
    _MIN = 1 + 1 + 1 + 2 + len(EXTENDED_MARKER) + 2 + 2  # header..CRC, senza SubData
    if len(buf) < _MIN:
        raise SilionFrameError(f"Extended buffer too short: {len(buf)} bytes")
    if buf[0] != HEADER:
        raise SilionFrameError(f"Bad header 0x{buf[0]:02X}, expected 0xFF")
    if buf[2] != CMD_EXTENDED:
        raise SilionFrameError(
            f"Not an extended frame: cmd 0x{buf[2]:02X}, expected 0x{CMD_EXTENDED:02X}"
        )
    data_len = buf[1]
    expected = 7 + data_len
    if len(buf) != expected:
        raise SilionFrameError(
            f"Length mismatch: got {len(buf)} bytes, expected {expected} (data_len={data_len})"
        )
    crc_recv = (buf[-2] << 8) | buf[-1]
    crc_calc = crc16(buf[1:-2])
    if crc_recv != crc_calc:
        raise SilionFrameError(f"CRC mismatch: recv 0x{crc_recv:04X} calc 0x{crc_calc:04X}")

    status = (buf[3] << 8) | buf[4]
    start = 5
    marker = bytes(buf[start : start + len(EXTENDED_MARKER)])
    if marker != EXTENDED_MARKER:
        raise SilionFrameError(f"Missing Moduletech marker, got {marker!r}")
    start += len(EXTENDED_MARKER)
    subcmd = (buf[start] << 8) | buf[start + 1]
    return ExtendedResponse(subcmd=subcmd, status=status, data=bytes(buf[start + 2 : -2]))


# --------------------------------------------------------------------------
# Pacchetti auto-caricati dall'inventory asincrono
# --------------------------------------------------------------------------
# Formato: FF | DataLen | AA | Status(2) | MetadataFlags(2) | record tag | CRC(2)
#
# Attenzione: **non** portano il marcatore "Moduletech". Sono l'unica cosa che
# il modulo invia senza che gliel'abbia chiesta, e vanno distinti dalle risposte
# estese proprio dall'assenza del marcatore.
#: Heartbeat inviato ogni 15 s se richiesto: non e' un tag, va scartato.
ASYNC_HEARTBEAT_MARKER = b"XTSJ"


@dataclass
class AsyncUpload:
    """Un pacchetto spinto dal modulo durante l'inventory asincrono."""

    status: int
    metadata_flags: int
    record: bytes

    @property
    def is_heartbeat(self) -> bool:
        return self.record.startswith(ASYNC_HEARTBEAT_MARKER)


def is_extended_frame(buf: bytes) -> bool:
    """Distingue una risposta estesa da un pacchetto auto-caricato.

    Entrambi hanno opcode 0xAA; solo la risposta porta il marcatore.
    """
    inizio = 5
    return (
        len(buf) >= inizio + len(EXTENDED_MARKER)
        and buf[2] == CMD_EXTENDED
        and bytes(buf[inizio : inizio + len(EXTENDED_MARKER)]) == EXTENDED_MARKER
    )


def parse_async_upload(buf: bytes) -> AsyncUpload:
    """Parser di un pacchetto tag auto-caricato."""
    if len(buf) < 9:
        raise SilionFrameError(f"Async upload too short: {len(buf)} bytes")
    if buf[0] != HEADER:
        raise SilionFrameError(f"Bad header 0x{buf[0]:02X}, expected 0xFF")
    if buf[2] != CMD_EXTENDED:
        raise SilionFrameError(
            f"Not an async upload: cmd 0x{buf[2]:02X}, expected 0x{CMD_EXTENDED:02X}"
        )
    data_len = buf[1]
    expected = 7 + data_len
    if len(buf) != expected:
        raise SilionFrameError(
            f"Length mismatch: got {len(buf)} bytes, expected {expected} (data_len={data_len})"
        )
    crc_recv = (buf[-2] << 8) | buf[-1]
    crc_calc = crc16(buf[1:-2])
    if crc_recv != crc_calc:
        raise SilionFrameError(f"CRC mismatch: recv 0x{crc_recv:04X} calc 0x{crc_calc:04X}")
    return AsyncUpload(
        status=(buf[3] << 8) | buf[4],
        metadata_flags=(buf[5] << 8) | buf[6],
        record=bytes(buf[7:-2]),
    )


@dataclass
class Response:
    cmd: int
    status: int
    data: bytes

    @property
    def ok(self) -> bool:
        return self.status == 0x0000


def parse_response(buf: bytes) -> Response:
    """Parser di una risposta completa Reader->Host.

    `buf` deve contenere esattamente un frame (header .. CRC). La funzione
    verifica header, lunghezza e CRC.
    """
    if len(buf) < 6:
        raise SilionFrameError(f"Buffer too short: {len(buf)} bytes")
    if buf[0] != HEADER:
        raise SilionFrameError(f"Bad header 0x{buf[0]:02X}, expected 0xFF")
    data_len = buf[1]
    cmd = buf[2]
    # frame totale = 1(header)+1(len)+1(cmd)+2(status)+data_len+2(crc)
    expected = 7 + data_len
    if len(buf) != expected:
        raise SilionFrameError(
            f"Length mismatch: got {len(buf)} bytes, expected {expected} (data_len={data_len})"
        )
    status = (buf[3] << 8) | buf[4]
    data = bytes(buf[5 : 5 + data_len])
    # CRC su: DataLen + Cmd + Status + Data  (skip header, skip CRC)
    crc_recv = (buf[-2] << 8) | buf[-1]
    crc_calc = crc16(buf[1:-2])
    if crc_recv != crc_calc:
        raise SilionFrameError(f"CRC mismatch: recv 0x{crc_recv:04X} calc 0x{crc_calc:04X}")
    return Response(cmd=cmd, status=status, data=data)


# --- Costanti di comando (dal manuale) ---
CMD_BOOT_FIRMWARE = 0x04
CMD_SINGLE_TAG_INVENTORY = 0x21
CMD_SYNCHRONOUS_INVENTORY = 0x22
CMD_WRITE_TAG_EPC = 0x23
CMD_WRITE_TAG_DATA = 0x24
CMD_LOCK_TAG = 0x25
CMD_KILL_TAG = 0x26  # opcode noto, volutamente non implementato: distrugge il tag
CMD_READ_TAG_DATA = 0x28
CMD_GET_TAG_BUFFER = 0x29
CMD_GET_ANTENNA_PORTS = 0x61
CMD_GET_PROTOCOL_CONFIG = 0x6B
CMD_SET_ANTENNA_PORTS = 0x91
CMD_SET_READER_CONFIG = 0x95
CMD_SET_CURRENT_REGION = 0x97
CMD_SET_POWER_MODE = 0x98
CMD_SET_UNIQUE_CONFIG = 0x9A
CMD_SET_PROTOCOL_CONFIG = 0x9B

# Sottocomandi del formato esteso
SUBCMD_ASYNC_INVENTORY_START = 0xAA48
SUBCMD_ASYNC_INVENTORY_STOP = 0xAA49
SUBCMD_STANDING_WAVE = 0xAA4A
SUBCMD_DENSE_INVENTORY_START = 0xAA58
SUBCMD_DENSE_INVENTORY_STOP = 0xAA59
SUBCMD_RSSI_FILTER = 0xAA5B

# --- Modalita' di consumo (0x98) ---
# 0 e 1 tengono la radio pronta; 2 e 3 spengono l'RF fra un comando e l'altro e
# richiedono 80-200 ms per riaccenderlo, penalizzando la lettura ripetuta.
POWER_MODE_RESPONSIVE = 0x00
POWER_MODE_LOWEST = 0x03
POWER_MODES_RESPONSIVE = (0x00, 0x01)

# --- Configurazioni proprietarie (0x9A): Option fisso 0x01, poi Key e Value ---
UNIQUE_OPT = 0x01
UNIQUE_KEY_ANTENNA_IN_BUFFER_KEY = 0x00  # 0 = un record per antenna, 1 = uno solo
UNIQUE_KEY_RSSI_MODE = 0x06              # 0 = ultimo RSSI, 1 = massimo osservato
RSSI_MODE_LAST = 0x00
RSSI_MODE_MAX = 0x01

# --- Filtro RSSI (0xAA5B, formato esteso) ---
# SubData di 1 byte 0x00 = lettura stato; di 4 byte = scrittura, con un byte
# marcatore 0xAA fra il comando e la soglia.
RSSI_FILTER_QUERY = b"\x00"
RSSI_FILTER_MARKER = 0xAA

# --- Parametri di protocollo Gen2 (comando 0x9B, manuale EX10 §7.8) ---
# Il primo byte del payload e' il protocollo, e l'unico supportato e' Gen2.
PROTOCOL_GEN2 = 0x05

GEN2_PARAM_SESSION = 0x00
GEN2_PARAM_TARGET = 0x01
GEN2_PARAM_RF_MODE = 0x02
GEN2_PARAM_Q = 0x12

# Target: Option 0x00 = dinamico (il modulo ribalta da solo A<->B a fine
# passata), Option 0x01 = statico. Il target dinamico ha effetto solo sui
# comandi 0x22 e 0xAA48.
GEN2_TARGET_DYNAMIC = 0x00
GEN2_TARGET_STATIC = 0x01
GEN2_TARGET_A = 0x00
GEN2_TARGET_B = 0x01
GEN2_TARGET_AB = 0x00  # dinamico: parte da A, poi passa a B
GEN2_TARGET_BA = 0x01  # dinamico: parte da B, poi passa ad A

GEN2_Q_DYNAMIC = 0x00
GEN2_Q_STATIC = 0x01

# Modalita' RF: il valore da inviare non e' l'ID del modo. La sensibilita'
# indicata e' quella dichiarata dal manuale per il modulo E710.
# Il default di fabbrica e' 0x6B (-88 dBm); 0x71 arriva a -93 dBm, cioe' 5 dB
# in piu', al prezzo del throughput (50+ tag/s contro 150+).
RF_MODE_DEFAULT = 0x6B
RF_MODE_MAX_SENSITIVITY = 0x71
RF_MODE_SENSITIVITY_DBM: dict[int, int] = {
    0xCB: -78, 0x6F: -78, 0xDC: -81, 0x65: -81, 0x2D: -84, 0x73: -84,
    0x70: -84, 0x67: -84, 0x69: -87, 0x6B: -88, 0x71: -93,
}

# --- Opzioni del comando 0x95 (configurazione lettore) ---
READER_OPT_ANTENNA_DWELL = 0x02   # tempo di permanenza per antenna, ms
READER_OPT_DUTY_CYCLE = 0x11      # Time1 a pieno carico + Time2 periodo, ms

# Soglia oltre la quale il manuale considera l'antenna disadattata, danneggiata
# o non collegata (comando 0xAA4A).
VSWR_ALERT_THRESHOLD = 7.0

# Potenza dichiarata nel frame 0xAA4A: il modulo la ignora e misura sempre a
# 20 dBm. Si usa il valore dell'esempio ufficiale del manuale.
STANDING_WAVE_TEST_POWER_CDBM = 3000


# --------------------------------------------------------------------------
# Tag Singulation: filtro Select (manuale EX10 §5.1.1 e §5.1.2)
# --------------------------------------------------------------------------
# I bit 0-2 di Option scelgono su cosa filtrare. Il caso 0x05 non filtra affatto:
# serve solo a poter inviare la password di accesso, ed e' quello usato finora da
# tutti i comandi di questo driver — con la conseguenza che agiscono sul primo
# tag che risponde.
SELECT_NONE = 0x00
SELECT_BY_EPC_ID = 0x01
SELECT_BY_TID = 0x02
SELECT_BY_USER = 0x03
SELECT_BY_EPC_BANK = 0x04
SELECT_PASSWORD_ONLY = 0x05

SELECT_OPT_INVERTED = 0x08     # ritorna i tag che NON corrispondono
SELECT_OPT_LONG_LENGTH = 0x20  # lunghezza del confronto su 2 byte invece di 1

#: Nel banco EPC l'EPCID comincia a questo indirizzo di bit: i primi 32 sono
#: occupati da CRC e PC.
EPC_BANK_EPCID_BIT_OFFSET = 0x20


def build_tag_singulation(
    select: int,
    access_password: bytes = b"\x00\x00\x00\x00",
    *,
    select_data: bytes = b"",
    select_address_bits: int | None = None,
    select_bit_length: int | None = None,
    inverted: bool = False,
) -> tuple[int, bytes]:
    """Compone Option e campo Tag Singulation per puntare un tag preciso.

    Ritorna `(option, dati)` da concatenare nel payload del comando.

    Attenzione a un dettaglio facile da sbagliare: con `SELECT_BY_EPC_ID` il
    campo Select Address **non va inviato**, perche' l'indirizzo e' implicito.
    Con gli altri banchi invece serve, ed e' espresso **in bit**, non in byte —
    come la lunghezza del confronto.
    """
    if select not in (
        SELECT_NONE,
        SELECT_BY_EPC_ID,
        SELECT_BY_TID,
        SELECT_BY_USER,
        SELECT_BY_EPC_BANK,
        SELECT_PASSWORD_ONLY,
    ):
        raise ValueError(f"select non supportato: 0x{select:02X}")
    if len(access_password) != 4:
        raise ValueError("access_password must be 4 bytes")

    option = select
    if inverted:
        if select in (SELECT_NONE, SELECT_PASSWORD_ONLY):
            raise ValueError("il flag inverted richiede un filtro attivo")
        option |= SELECT_OPT_INVERTED

    if select == SELECT_NONE:
        return option, b""
    if select == SELECT_PASSWORD_ONLY:
        return option, access_password

    if not select_data:
        raise ValueError("un filtro attivo richiede select_data")
    bit_length = len(select_data) * 8 if select_bit_length is None else select_bit_length
    if not 1 <= bit_length <= 0xFFFF:
        raise ValueError("select_bit_length fuori intervallo 1..65535")
    if bit_length > len(select_data) * 8:
        raise ValueError(
            f"select_bit_length {bit_length} eccede i {len(select_data) * 8} bit forniti"
        )

    dati = bytearray(access_password)
    if select != SELECT_BY_EPC_ID:
        # Solo il filtro sull'EPCID ha l'indirizzo implicito.
        if select_address_bits is None:
            raise ValueError(
                "questo filtro richiede select_address_bits (indirizzo in bit)"
            )
        if not 0 <= select_address_bits <= 0xFFFFFFFF:
            raise ValueError("select_address_bits fuori intervallo")
        dati += select_address_bits.to_bytes(4, "big")
    elif select_address_bits is not None:
        raise ValueError(
            "con il filtro sull'EPCID l'indirizzo e' implicito e non va inviato"
        )

    if bit_length > 0xFF:
        option |= SELECT_OPT_LONG_LENGTH
        dati += bit_length.to_bytes(2, "big")
    else:
        dati += bytes([bit_length])
    dati += select_data[: (bit_length + 7) // 8]
    return option, bytes(dati)


# --------------------------------------------------------------------------
# Embedded read: leggere una banca di tutti i tag durante l'inventory
# --------------------------------------------------------------------------
# Rimuove il vincolo "un tag alla volta" in lettura: l'inventory restituisce
# anche il contenuto della banca richiesta, tag per tag, nel metadata BIT7.
# Il manuale precisa che se la lettura fallisce l'EPC viene comunque riportato,
# quindi non peggiora la completezza dell'inventario.
SEARCH_FLAG_EMBEDDED_DATA = 0x0004   # BIT2 del byte basso di Search Flags
SEARCH_FLAG_TAGFOCUS = 0x0010        # BIT4: valido solo con S1 + Target A statico
EMBEDDED_MAX_GROUPS = 10
EMBEDDED_MAX_WORDS_PER_GROUP = 32
EMBEDDED_MAX_WORDS_TOTAL = 56


def build_embedded_read(groups: list[tuple[int, int, int]]) -> bytes:
    """Compone il campo Embedded Command Content per l'inventory.

    `groups` e' una lista di `(bank, address_words, word_count)`. Timeout e
    Option interni sono a zero: il manuale li dichiara privi di effetto, perche'
    il tempo dell'operazione e' quello dell'inventory che li contiene.
    """
    if not groups:
        raise ValueError("serve almeno un gruppo di lettura")
    if len(groups) > EMBEDDED_MAX_GROUPS:
        raise ValueError(f"al massimo {EMBEDDED_MAX_GROUPS} gruppi, ricevuti {len(groups)}")
    totale = sum(word_count for _, _, word_count in groups)
    if totale > EMBEDDED_MAX_WORDS_TOTAL:
        raise ValueError(
            f"al massimo {EMBEDDED_MAX_WORDS_TOTAL} word complessive, richieste {totale}"
        )

    # Timeout e Option compaiono UNA volta sola, prima dei gruppi: e' il motivo
    # per cui la lunghezza dichiarata e' 3 + 6*N e non 9*N.
    corpo = bytearray(b"\x00\x00\x00")            # Emb Timeout(2) + Emb Option(1)
    for bank, address, word_count in groups:
        if bank not in (BANK_RESERVED, BANK_EPC, BANK_TID, BANK_USER):
            raise ValueError(f"banca non valida: {bank}")
        if not 0 <= address <= 0xFFFFFFFF:
            raise ValueError(f"address fuori intervallo: {address}")
        if not 1 <= word_count <= EMBEDDED_MAX_WORDS_PER_GROUP:
            raise ValueError(
                f"word_count fuori intervallo 1..{EMBEDDED_MAX_WORDS_PER_GROUP}: {word_count}"
            )
        corpo += bytes([bank])
        corpo += address.to_bytes(4, "big")
        corpo += bytes([word_count])
    lunghezza = 3 + 6 * len(groups)
    return bytes([len(groups), lunghezza, CMD_READ_TAG_DATA]) + bytes(corpo)


def vswr_from_return_loss(raw_tenths_db: int) -> float:
    """Converte il byte di return loss di 0xAA4A (unita' 0,1 dB) in VSWR.

    Formula del manuale: `r = 10^(RL_dB/20)`, `VSWR = (r+1)/(r-1)`.
    Il caso `r <= 1` (return loss nullo, cioe' riflessione totale) non ha VSWR
    finito: si restituisce infinito invece di dividere per zero.
    """
    ratio = 10 ** (raw_tenths_db / 200.0)
    if ratio <= 1.0:
        return float("inf")
    return (ratio + 1.0) / (ratio - 1.0)

# Banche memoria Gen2
BANK_RESERVED = 0x00
BANK_EPC = 0x01
BANK_TID = 0x02
BANK_USER = 0x03

# Metadata Flags (bit mask) - ordine di apparizione nel record = bit crescente
META_READ_COUNT = 0x0001  # BIT0  ReadCount(1)
META_RSSI = 0x0002  # BIT1  RSSI(1, signed)
META_ANTENNA_ID = 0x0004  # BIT2  AntennaID(1)
META_FREQUENCY = 0x0008  # BIT3  Frequency(3, kHz)
META_TIMESTAMP = 0x0010  # BIT4  Timestamp(4, ms)
META_PHASE = 0x0020  # BIT5  Phase(2)
META_PROTOCOL_ID = 0x0040  # BIT6  Protocol ID(1)
META_TAG_DATA = 0x0080  # BIT7  Tag Data Length(2, bit) + Tag Data

# --- Lock Tag (0x25): campi Mask e Action ---
# Disposizione dei bit dalla Figura 6 del manuale EX10 2024-12 §6.3. Entrambi i
# campi sono di 2 byte e solo i 10 bit bassi sono validi; i bit 15..10 non sono
# usati. Per ogni banca ci sono due bit: quello di scrittura (o lettura/scrittura
# per le password) e quello di permanenza.
LOCK_FIELD_BITS: dict[str, tuple[int, int]] = {
    # nome:            (bit W/RW, bit Perm)
    "kill_password": (9, 8),
    "access_password": (7, 6),
    "epc": (5, 4),
    "tid": (3, 2),
    "user": (1, 0),
}

# Le cinque operazioni possibili, come (mask_w, mask_perm, action_w, action_perm)
# dalla tabella del manuale §6.3. Un bit di Action ha effetto solo se il
# corrispondente bit di Mask vale 1: le combinazioni non vanno inventate.
LOCK_NO_ACTION = "no_action"
LOCK_LOCK = "lock"
LOCK_UNLOCK = "unlock"
LOCK_PERMALOCK = "permalock"
LOCK_PERMAUNLOCK = "permaunlock"

_LOCK_OPERATIONS: dict[str, tuple[int, int, int, int]] = {
    LOCK_NO_ACTION: (0, 0, 0, 0),
    LOCK_LOCK: (1, 0, 1, 0),
    LOCK_UNLOCK: (1, 0, 0, 0),
    LOCK_PERMALOCK: (1, 1, 1, 1),
    LOCK_PERMAUNLOCK: (1, 1, 0, 1),
}

#: Operazioni irreversibili: dopo un permalock la banca non e' piu' scrivibile,
#: dopo un permaunlock non e' piu' proteggibile. Nessuna delle due si annulla.
LOCK_PERMANENT_OPERATIONS = frozenset({LOCK_PERMALOCK, LOCK_PERMAUNLOCK})


def build_lock_bits(operations: dict[str, str]) -> tuple[int, int]:
    """Compone i campi Mask e Action del comando 0x25.

    `operations` associa un nome di `LOCK_FIELD_BITS` a una delle costanti
    `LOCK_*`. Le banche non citate restano intoccate, perche' i loro bit di Mask
    restano a zero.
    """
    mask = 0
    action = 0
    for campo, operazione in operations.items():
        if campo not in LOCK_FIELD_BITS:
            raise ValueError(f"campo di lock sconosciuto: {campo!r}")
        if operazione not in _LOCK_OPERATIONS:
            raise ValueError(f"operazione di lock sconosciuta: {operazione!r}")
        bit_w, bit_perm = LOCK_FIELD_BITS[campo]
        mask_w, mask_perm, action_w, action_perm = _LOCK_OPERATIONS[operazione]
        mask |= (mask_w << bit_w) | (mask_perm << bit_perm)
        action |= (action_w << bit_w) | (action_perm << bit_perm)
    return mask, action
