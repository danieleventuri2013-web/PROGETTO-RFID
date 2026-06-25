"""Livello protocollo Silion (SIM7200).

Frame Host->Reader:  0xFF | DataLen | CmdCode | Data[N] | CRC16(MSB,LSB)
Frame Reader->Host:  0xFF | DataLen | CmdCode | Status(2) | Data[N] | CRC16(MSB,LSB)

CRC-16 CCITT, poly 0x1021, init 0xFFFF, calcolato su tutti i byte eccetto
l'header 0xFF e i 2 byte di CRC (come CalcCRC del manuale, loop da i=1).
"""
from __future__ import annotations

from dataclasses import dataclass

HEADER = 0xFF
_POLY = 0x1021
_INIT = 0xFFFF


class SilionFrameError(Exception):
    """Errore di framing/CRC nella risposta del lettore."""


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
            f"Length mismatch: got {len(buf)} bytes, expected {expected} "
            f"(data_len={data_len})"
        )
    status = (buf[3] << 8) | buf[4]
    data = bytes(buf[5:5 + data_len])
    # CRC su: DataLen + Cmd + Status + Data  (skip header, skip CRC)
    crc_recv = (buf[-2] << 8) | buf[-1]
    crc_calc = crc16(buf[1:-2])
    if crc_recv != crc_calc:
        raise SilionFrameError(
            f"CRC mismatch: recv 0x{crc_recv:04X} calc 0x{crc_calc:04X}"
        )
    return Response(cmd=cmd, status=status, data=data)


# --- Costanti di comando (dal manuale) ---
CMD_BOOT_FIRMWARE = 0x04
CMD_SINGLE_TAG_INVENTORY = 0x21
CMD_SYNCHRONOUS_INVENTORY = 0x22
CMD_WRITE_TAG_EPC = 0x23
CMD_WRITE_TAG_DATA = 0x24
CMD_READ_TAG_DATA = 0x28
CMD_GET_TAG_BUFFER = 0x29
CMD_GET_ANTENNA_PORTS = 0x61
CMD_SET_ANTENNA_PORTS = 0x91
CMD_SET_CURRENT_REGION = 0x97

# Banche memoria Gen2
BANK_RESERVED = 0x00
BANK_EPC = 0x01
BANK_TID = 0x02
BANK_USER = 0x03

# Metadata Flags (bit mask) - ordine di apparizione nel record = bit crescente
META_READ_COUNT = 0x0001   # BIT0  ReadCount(1)
META_RSSI = 0x0002         # BIT1  RSSI(1, signed)
META_ANTENNA_ID = 0x0004   # BIT2  AntennaID(1)
META_FREQUENCY = 0x0008    # BIT3  Frequency(3, kHz)
META_TIMESTAMP = 0x0010    # BIT4  Timestamp(4, ms)
META_PHASE = 0x0020        # BIT5  Phase(2)
META_PROTOCOL_ID = 0x0040  # BIT6  Protocol ID(1)
META_TAG_DATA = 0x0080     # BIT7  Tag Data Length(2, bit) + Tag Data
