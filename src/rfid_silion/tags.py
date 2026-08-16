"""Parsing dei tag dal buffer del lettore (comando 0x29)."""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .protocol import (
    META_ANTENNA_ID,
    META_FREQUENCY,
    META_PHASE,
    META_PROTOCOL_ID,
    META_READ_COUNT,
    META_RSSI,
    META_TAG_DATA,
    META_TIMESTAMP,
    SilionFrameError,
)

log = logging.getLogger("rfid_silion.tags")


@dataclass
class Tag:
    epc: str  # EPC in esadecimale (senza PC/CRC)
    pc: int  # PC word (16 bit)
    crc: int  # Tag CRC (16 bit)
    read_count: int | None = None
    rssi: int | None = None  # dBm (signed)
    antenna_id: int | None = None
    frequency: int | None = None  # kHz
    timestamp: int | None = None  # ms
    phase: int | None = None  # solo se META_PHASE
    protocol_id: int | None = None  # solo se META_PROTOCOL_ID
    embedded_data: bytes | None = None  # solo se META_TAG_DATA (embedded read)

    def __str__(self) -> str:
        parts = [f"EPC={self.epc}"]
        if self.antenna_id is not None:
            parts.append(f"ant={self.antenna_id}")
        if self.rssi is not None:
            parts.append(f"rssi={self.rssi}dBm")
        if self.read_count is not None:
            parts.append(f"cnt={self.read_count}")
        return " ".join(parts)


@dataclass(frozen=True)
class TagReadSummary:
    epc: str
    reads: int
    antennas: tuple[int, ...]
    best_rssi: int | None
    last_rssi: int | None
    observations: int
    first_seen_cycle: int = 0
    cycles: int = 0

    @property
    def detection_rate(self) -> float:
        """Frazione di cicli in cui il tag e' comparso, da 0 a 1.

        E' la misura di quanto un tag e' difficile da leggere in quella
        posizione: 1.0 significa visto sempre, 0.1 significa che serve fortuna.
        Sotto una scatola chiusa e' l'indicatore che dice se ci si puo' fidare
        di una singola passata o se ne servono molte.
        """
        return self.observations / self.cycles if self.cycles else 0.0


@dataclass
class _MutableTagSummary:
    reads: int = 0
    antennas: set[int] = field(default_factory=set)
    best_rssi: int | None = None
    last_rssi: int | None = None
    observations: int = 0
    last_seen_cycle: int = 0
    last_seen_at: float | None = None
    first_seen_cycle: int = 0


class TagReadAccumulator:
    """Aggregazione incrementale e limitata per sessioni inventory lunghe."""

    def __init__(self, max_unique_epcs: int = 1000):
        if max_unique_epcs < 1:
            raise ValueError("max_unique_epcs deve essere almeno 1")
        self.max_unique_epcs = max_unique_epcs
        self._rows: OrderedDict[str, _MutableTagSummary] = OrderedDict()
        self.record_count = 0
        self.evicted_epcs = 0
        self.expired_epcs = 0
        self.cycles = 0
        self._presence_cycle = 0

    def clear(self) -> None:
        self._rows.clear()
        self.record_count = 0
        self.evicted_epcs = 0
        self.expired_epcs = 0
        self.cycles = 0
        self._presence_cycle = 0

    def add(self, tags: list[Tag]) -> None:
        """Aggiunge il risultato di un ciclo, senza far scadere nulla.

        E' la modalita' da usare per certificare il contenuto di una scatola
        chiusa: li' un tag non "esce dal campo", e togliere dall'elenco chi non
        risponde per qualche giro cancellerebbe proprio l'informazione che
        interessa. Per il varco, dove i tag entrano ed escono davvero, si usa
        invece `update_presence`.
        """
        self.cycles += 1
        self._add(tags, seen_at=None)

    def _add(self, tags: list[Tag], *, seen_at: float | None) -> None:
        for tag in tags:
            self.record_count += 1
            row = self._rows.get(tag.epc)
            if row is None:
                if len(self._rows) >= self.max_unique_epcs:
                    self._rows.popitem(last=False)
                    self.evicted_epcs += 1
                row = _MutableTagSummary()
                self._rows[tag.epc] = row
            else:
                self._rows.move_to_end(tag.epc)
            row.reads += tag.read_count if tag.read_count is not None else 1
            if row.observations == 0:
                row.first_seen_cycle = self.cycles
            row.observations += 1
            row.last_seen_cycle = self._presence_cycle
            row.last_seen_at = seen_at
            if tag.antenna_id is not None:
                row.antennas.add(tag.antenna_id)
            if tag.rssi is not None:
                row.last_rssi = tag.rssi
                if row.best_rssi is None or tag.rssi > row.best_rssi:
                    row.best_rssi = tag.rssi

    def update_presence(
        self,
        tags: list[Tag],
        *,
        remove_after_missed_cycles: int | None = None,
        remove_after_seconds: float | None = None,
        now: float | None = None,
    ) -> tuple[str, ...]:
        """Aggiorna la presenza usando una soglia per cicli oppure per tempo."""
        if (remove_after_missed_cycles is None) == (remove_after_seconds is None):
            raise ValueError("specificare una sola soglia di assenza")
        if remove_after_missed_cycles is not None and remove_after_missed_cycles < 1:
            raise ValueError("la soglia in cicli deve essere almeno 1")
        if remove_after_seconds is not None and remove_after_seconds <= 0:
            raise ValueError("la soglia in secondi deve essere maggiore di 0")
        observed_at = time.monotonic() if now is None else float(now)
        self._presence_cycle += 1
        self.cycles += 1
        self._add(tags, seen_at=observed_at)
        return self._expire_missing(
            remove_after_missed_cycles, remove_after_seconds, observed_at
        )

    def _expire_missing(
        self, missed_cycles: int | None, seconds: float | None, now: float
    ) -> tuple[str, ...]:
        expired: list[str] = []
        for epc, row in self._rows.items():
            missed = self._presence_cycle - row.last_seen_cycle
            elapsed = now - row.last_seen_at if row.last_seen_at is not None else 0.0
            if (missed_cycles is not None and missed >= missed_cycles) or (
                seconds is not None and elapsed >= seconds
            ):
                expired.append(epc)
        for epc in expired:
            del self._rows[epc]
        self.expired_epcs += len(expired)
        return tuple(expired)

    def summaries(self) -> list[TagReadSummary]:
        rows = [
            TagReadSummary(
                epc=epc,
                reads=row.reads,
                antennas=tuple(sorted(row.antennas)),
                best_rssi=row.best_rssi,
                last_rssi=row.last_rssi,
                observations=row.observations,
                first_seen_cycle=row.first_seen_cycle,
                cycles=self.cycles,
            )
            for epc, row in self._rows.items()
        ]
        return sorted(rows, key=lambda row: (-row.reads, row.epc))


def summarize_tag_reads(tags: list[Tag]) -> list[TagReadSummary]:
    """Aggrega i record del buffer per EPC, sommando il numero di letture."""
    accumulator = TagReadAccumulator(max_unique_epcs=max(1, len(tags)))
    accumulator.add(tags)
    return accumulator.summaries()


def _need(data: bytes, pos: int, n: int, what: str) -> None:
    """Verifica che restino almeno `n` byte per il campo `what`.

    Un buffer troncato indica una risposta corrotta o un disallineamento del
    parser: meglio un errore esplicito (con offset) di un IndexError anonimo.
    """
    if pos + n > len(data):
        raise SilionFrameError(
            f"Tag buffer truncated: need {n} byte(s) for {what} at offset "
            f"{pos}, only {len(data) - pos} available (buffer={len(data)}B)"
        )


def parse_tag_record(data: bytes, pos: int, flags: int) -> tuple[Tag, int]:
    """Parsa **un** record tag e ritorna `(tag, posizione successiva)`.

    Estratta dal ciclo di `parse_tag_buffer` per poterla riusare sui pacchetti
    auto-caricati dall'inventory asincrono, che portano un solo record e non
    hanno intestazione di conteggio. Duplicare questo parser sarebbe stato il
    modo piu' rapido di far divergere due copie del pezzo di codice piu'
    delicato del driver.
    """
    read_count = rssi = ant_id = freq = ts = None
    phase = proto_id = embedded = None

    # --- blocco metadati (ordine = bit crescente) ---
    if flags & META_READ_COUNT:
        _need(data, pos, 1, "ReadCount")
        read_count = data[pos]
        pos += 1
    if flags & META_RSSI:
        _need(data, pos, 1, "RSSI")
        rssi = data[pos]
        if rssi >= 128:  # byte signed (complemento a 2)
            rssi -= 256
        pos += 1
    if flags & META_ANTENNA_ID:
        # Il byte antenna codifica (TX<<4 | RX): l'antenna LOGICA e' il nibble
        # basso, con 0 -> 16 (come ParseNextTag del codice C di riferimento
        # Silion). Setup monostatico: 0x11->1, 0x22->2, 0x33->3.
        _need(data, pos, 1, "AntennaID")
        ant_id = data[pos] & 0x0F
        if ant_id == 0:
            ant_id = 16
        pos += 1
    if flags & META_FREQUENCY:
        _need(data, pos, 3, "Frequency")
        freq = (data[pos] << 16) | (data[pos + 1] << 8) | data[pos + 2]
        pos += 3
    if flags & META_TIMESTAMP:
        _need(data, pos, 4, "Timestamp")
        ts = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4
    if flags & META_PHASE:
        _need(data, pos, 2, "Phase")
        phase = (data[pos] << 8) | data[pos + 1]
        pos += 2
    if flags & META_PROTOCOL_ID:
        _need(data, pos, 1, "ProtocolID")
        proto_id = data[pos]
        pos += 1
    if flags & META_TAG_DATA:
        _need(data, pos, 2, "TagDataLength")
        tag_data_bits = (data[pos] << 8) | data[pos + 1]
        pos += 2
        if tag_data_bits % 8:
            raise SilionFrameError(f"TagDataLength non allineata a byte: {tag_data_bits} bit")
        n = tag_data_bits // 8
        _need(data, pos, n, "TagData")
        embedded = bytes(data[pos : pos + n])
        pos += n

    # --- blocco EPC (sempre presente) ---
    # EpcLength e' la lunghezza in BIT di PC + EPC + TagCRC.
    _need(data, pos, 2, "EpcLength")
    epc_len_bits = (data[pos] << 8) | data[pos + 1]
    pos += 2
    if epc_len_bits < 32 or epc_len_bits % 8:
        raise SilionFrameError(
            f"EpcLength non valida: {epc_len_bits} bit (attesi almeno 32 bit e multiplo di 8)"
        )
    _need(data, pos, 2, "PC")
    pc = (data[pos] << 8) | data[pos + 1]
    pos += 2
    epc_bytes = (epc_len_bits // 8) - 4  # togli PC(2) + CRC(2)
    _need(data, pos, epc_bytes, "EPC")
    epc = data[pos : pos + epc_bytes]
    pos += epc_bytes
    _need(data, pos, 2, "TagCRC")
    crc = (data[pos] << 8) | data[pos + 1]
    pos += 2

    return (
        Tag(
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
        ),
        pos,
    )


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

    Solleva ``SilionFrameError`` se il buffer e' troncato rispetto al numero
    di tag dichiarato (risposta corrotta / parser disallineato).
    """
    if not 0 <= metadata_flags <= 0xFFFF:
        raise ValueError(f"metadata_flags fuori range 0..0xFFFF: {metadata_flags}")
    if len(data) < 4:
        raise SilionFrameError(
            f"Tag buffer header truncated: {len(data)} bytes, expected at least 4"
        )
    flags = (data[0] << 8) | data[1]
    if flags & ~0x00FF:
        raise SilionFrameError(f"Metadata flags non supportati: 0x{flags:04X}")
    if flags != metadata_flags:
        log.warning("Metadata flags richiesti 0x%04X, ricevuti 0x%04X", metadata_flags, flags)
    # option = data[2]
    tag_count = data[3]
    pos = 4
    tags: list[Tag] = []

    for _ in range(tag_count):
        tag, pos = parse_tag_record(data, pos, flags)
        tags.append(tag)

    if pos != len(data):
        raise SilionFrameError(
            f"Tag buffer contiene {len(data) - pos} byte residui dopo {tag_count} tag"
        )
    return tags
