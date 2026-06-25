"""Parsing dei tag dal buffer del lettore (comando 0x29)."""
from __future__ import annotations

from dataclasses import dataclass

from .protocol import (
    META_READ_COUNT,
    META_RSSI,
    META_ANTENNA_ID,
    META_FREQUENCY,
    META_TIMESTAMP,
    META_PHASE,
    META_PROTOCOL_ID,
    META_TAG_DATA,
)


@dataclass
class Tag:
    epc: str               # EPC in esadecimale (senza PC/CRC)
    pc: int                # PC word (16 bit)
    crc: int               # Tag CRC (16 bit)
    read_count: int | None = None
    rssi: int | None = None       # dBm (signed)
    antenna_id: int | None = None
    frequency: int | None = None  # kHz
    timestamp: int | None = None  # ms
    phase: int | None = None              # solo se META_PHASE
    protocol_id: int | None = None        # solo se META_PROTOCOL_ID
    embedded_data: bytes | None = None    # solo se META_TAG_DATA (embedded read)

    def __str__(self) -> str:
        parts = [f"EPC={self.epc}"]
        if self.antenna_id is not None:
            parts.append(f"ant={self.antenna_id}")
        if self.rssi is not None:
            parts.append(f"rssi={self.rssi}dBm")
        if self.read_count is not None:
            parts.append(f"cnt={self.read_count}")
        return " ".join(parts)


def parse_tag_buffer(data: bytes, metadata_flags: int) -> list[Tag]:
    """Parser della risposta di Get Tag Buffer (0x29).

    Layout di `data` (dopo lo Status), da manuale EX10 §5.1.3/§5.1.4::

        MetadataFlags(2) | Option(1) | TagCount(1) | TagInfo[...]

    Ogni `TagInfo` = i campi metadati abilitati dai MetadataFlags, **in ordine di
    bit crescente**, seguiti SEMPRE dal blocco EPC::

        EpcLength(2, in BIT, include PC+CRC) | PC(2) | EPC(N) | TagCRC(2)

    con ``N = EpcLength/8 - 4`` (si tolgono le 2 word di PC e CRC).

    Campi metadati (larghezza in byte) nell'ordine in cui compaiono:
        BIT0 ReadCount(1)  BIT1 RSSI(1)  BIT2 AntennaID(1)  BIT3 Frequency(3)
        BIT4 Timestamp(4)  BIT5 Phase(2)  BIT6 ProtocolID(1)
        BIT7 TagDataLength(2, in BIT) + TagData (embedded read)

    NB: il campo "Tag Data Length" esiste **solo** se BIT7 e' attivo e fa parte
    del blocco metadati (prima di EpcLength); NON e' un campo sempre presente.
    Con il default ``metadata_flags=0x0007`` (ReadCount|RSSI|AntennaID) si parsano
    solo i primi tre metadati.
    """
    if len(data) < 4:
        return []
    flags = (data[0] << 8) | data[1]
    # option = data[2]
    tag_count = data[3]
    pos = 4
    tags: list[Tag] = []

    for _ in range(tag_count):
        read_count = rssi = ant_id = freq = ts = None
        phase = proto_id = embedded = None

        # --- blocco metadati (ordine = bit crescente) ---
        if flags & META_READ_COUNT:
            read_count = data[pos]; pos += 1
        if flags & META_RSSI:
            rssi = data[pos]
            if rssi >= 128:          # byte signed (complemento a 2)
                rssi -= 256
            pos += 1
        if flags & META_ANTENNA_ID:
            # Il byte antenna codifica (TX<<4 | RX): l'antenna LOGICA e' il nibble
            # basso, con 0 -> 16 (come ParseNextTag del codice C di riferimento
            # Silion). Setup monostatico: 0x11->1, 0x22->2, 0x33->3.
            ant_id = data[pos] & 0x0F
            if ant_id == 0:
                ant_id = 16
            pos += 1
        if flags & META_FREQUENCY:
            freq = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
            pos += 3
        if flags & META_TIMESTAMP:
            ts = int.from_bytes(data[pos:pos + 4], "big")
            pos += 4
        if flags & META_PHASE:
            phase = (data[pos] << 8) | data[pos + 1]
            pos += 2
        if flags & META_PROTOCOL_ID:
            proto_id = data[pos]; pos += 1
        if flags & META_TAG_DATA:
            tag_data_bits = (data[pos] << 8) | data[pos + 1]
            pos += 2
            n = tag_data_bits // 8
            embedded = bytes(data[pos:pos + n])
            pos += n

        # --- blocco EPC (sempre presente) ---
        # EpcLength e' la lunghezza in BIT di PC + EPC + TagCRC.
        epc_len_bits = (data[pos] << 8) | data[pos + 1]
        pos += 2
        pc = (data[pos] << 8) | data[pos + 1]
        pos += 2
        epc_bytes = (epc_len_bits // 8) - 4   # togli PC(2) + CRC(2)
        if epc_bytes < 0:
            epc_bytes = 0
        epc = data[pos:pos + epc_bytes]
        pos += epc_bytes
        crc = (data[pos] << 8) | data[pos + 1]
        pos += 2

        tags.append(Tag(
            epc=bytes(epc).hex().upper(),
            pc=pc,
            crc=crc,
            read_count=read_count,
            rssi=rssi,
            antenna_id=ant_id,
            frequency=freq,
            timestamp=ts,
            phase=phase,
            protocol_id=proto_id,
            embedded_data=embedded,
        ))
    return tags
