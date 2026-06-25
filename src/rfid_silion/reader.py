"""Classe high-level per il lettore Silion SIM7200.

Il modulo SIM7200 puo' essere collegato direttamente via UART (TTL 3.3V) oppure,
montato sulla baseboard SLD1090, via USB / RS232 / Ethernet TCP / WiFi / 4G.
Il protocollo Silion e' indipendente dal trasporto: qui si astrae con la classe
`Transport` (vedi `transports.py`).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .transports import Transport, SerialTransport, TcpTransport
from . import protocol as P
from .errors import check_status, SilionError, NoTagError
from .tags import Tag, parse_tag_buffer

log = logging.getLogger("rfid_silion.reader")


class SIM7200Reader:
    """Driver per lettore Silion SIM7200 via porta seriale."""

    def __init__(self, transport: Transport):
        self._t = transport

    # ------------------------------------------------------------------ #
    # Connessione / I/O grezzo (delegato al trasporto)
    # ------------------------------------------------------------------ #
    def open(self) -> None:
        self._t.open()

    def close(self) -> None:
        self._t.close()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def _flush(self) -> None:
        self._t.flush_input()

    def _send(self, cmd: int, data: bytes = b"") -> None:
        pkt = P.build_packet(cmd, data)
        log.debug("TX %s", pkt.hex(" ").upper())
        self._t.write(pkt)

    def _recv_frame(self) -> P.Response:
        """Legge un frame completo dal seriale."""
        # header + datalen
        head = self._read_exact(2)
        if head[0] != P.HEADER:
            # scarta finche' non trova header (robustezza)
            raise P.SilionFrameError(f"Bad header 0x{head[0]:02X}")
        data_len = head[1]
        # cmd(1)+status(2)+data+len+crc(2)
        rest_len = 1 + 2 + data_len + 2
        rest = self._read_exact(rest_len)
        frame = head + rest
        log.debug("RX %s", frame.hex(" ").upper())
        return P.parse_response(frame)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._t.read(n - len(buf))
            if not chunk:
                raise P.SilionFrameError(
                    f"Transport timeout reading {n} bytes (got {len(buf)})"
                )
            buf.extend(chunk)
        return bytes(buf)

    def _command(self, cmd: int, data: bytes = b"") -> P.Response:
        """Invia un comando e restituisce la risposta verificata."""
        self._flush()
        self._send(cmd, data)
        resp = self._recv_frame()
        if resp.cmd != cmd:
            raise P.SilionFrameError(
                f"Cmd mismatch: sent 0x{cmd:02X} recv 0x{resp.cmd:02X}"
            )
        return resp

    # ------------------------------------------------------------------ #
    # Comandi
    # ------------------------------------------------------------------ #
    def boot_firmware(self) -> dict:
        """0x04 - passa in App firmware. Obbligatorio al power-on."""
        resp = self._command(P.CMD_BOOT_FIRMWARE)
        check_status(resp.status)
        d = resp.data
        info = {
            "bootloader_version": d[0:4].hex().upper(),
            "hardware_version": d[4:8].hex().upper(),
            "firmware_date": d[8:12].hex().upper(),
            "firmware_version": d[12:16].hex().upper(),
            "supported_protocol": d[16:20].hex().upper(),
        }
        log.info("Boot firmware OK: %s", info)
        return info

    def set_region(self, region: int) -> None:
        """0x97 - imposta regione (EU=0x08)."""
        resp = self._command(P.CMD_SET_CURRENT_REGION, bytes([region]))
        check_status(resp.status)
        log.info("Region set to 0x%02X", region)

    def set_antennas_for_inventory(self, antenna_pairs: list[tuple[int, int]]) -> None:
        """0x91 option 0x02 - antenne (tx,rx) usate per inventory, nell'ordine di ciclo."""
        data = bytearray([0x02])
        for tx, rx in antenna_pairs:
            data += bytes([tx, rx])
        resp = self._command(P.CMD_SET_ANTENNA_PORTS, bytes(data))
        check_status(resp.status)
        log.info("Inventory antennas set: %s", antenna_pairs)

    def set_antenna_for_access(self, tx: int, rx: int) -> None:
        """0x91 option 0x00 - antenna singola per tag access (read/write)."""
        data = bytes([0x00, tx, rx])
        resp = self._command(P.CMD_SET_ANTENNA_PORTS, bytes(data))
        check_status(resp.status)
        log.debug("Access antenna set: tx=%d rx=%d", tx, rx)

    def set_antennas_power(self, powers: list[tuple[int, int, int]]) -> None:
        """0x91 option 0x03 - imposta potenze.

        powers: lista di (antenna_id, read_power_cdBm, write_power_cdBm)
        es. [(1, 2000, 2000), (2, 2000, 2000), (3, 2000, 2000)]  # 20 dBm
        """
        data = bytearray([0x03])
        for ant_id, rp, wp in powers:
            data += bytes([ant_id])
            data += rp.to_bytes(2, "big")
            data += wp.to_bytes(2, "big")
        resp = self._command(P.CMD_SET_ANTENNA_PORTS, bytes(data))
        check_status(resp.status)
        log.info("Antenna powers set: %s", powers)

    def get_antenna_connection(self) -> list[int]:
        """0x61 option 0x05 - ritorna lista di antenna id connesse fisicamente."""
        resp = self._command(P.CMD_GET_ANTENNA_PORTS, bytes([0x05]))
        check_status(resp.status)
        # La risposta (0x61 option 0x02/0x03/0x04/0x05) RIPETE l'Option come
        # primo byte di Data; seguono le coppie (antenna_id, connection_state).
        # (manuale EX10 §8.1, esempio 5). Saltare quindi il byte Option iniziale.
        connected = []
        d = resp.data
        i = 1 if (d and d[0] == 0x05) else 0
        while i + 1 < len(d):
            ant_id = d[i]
            state = d[i + 1]
            if state:
                connected.append(ant_id)
            i += 2
        log.info("Connected antennas: %s", connected)
        return connected

    # ------------------------------------------------------------------ #
    # Inventory
    # ------------------------------------------------------------------ #
    def sync_inventory(self, timeout_ms: int = 1000,
                       search_flags: int = 0x0000) -> int:
        """0x22 - inventario sincrono, ritorna numero di tag archiviati.

        Senza select filter (Option=0x00): nessuna access password.
        """
        # Option(1) | SearchFlags(2) | Timeout(2)   (no access pwd, no select)
        data = bytes([0x00,
                      (search_flags >> 8) & 0xFF, search_flags & 0xFF,
                      (timeout_ms >> 8) & 0xFF, timeout_ms & 0xFF])
        resp = self._command(P.CMD_SYNCHRONOUS_INVENTORY, data)
        check_status(resp.status)
        d = resp.data
        # Option(1) | SearchFlags(2) | TagsFound(1 o 4)
        flags = (d[1] << 8) | d[2]
        if flags & 0x10:  # >255 tag -> 4 byte
            count = int.from_bytes(d[3:7], "big")
        else:
            count = d[3]
        log.info("Sync inventory: %d tags", count)
        return count

    def get_tag_buffer(self, metadata_flags: int = 0x0007,
                       option: int = 0x00) -> list[Tag]:
        """0x29 - recupera i tag archiviati dall'ultimo 0x22."""
        data = bytes([
            (metadata_flags >> 8) & 0xFF, metadata_flags & 0xFF,
            option,
        ])
        resp = self._command(P.CMD_GET_TAG_BUFFER, data)
        check_status(resp.status)
        return parse_tag_buffer(resp.data, metadata_flags)

    def inventory(self, timeout_ms: int = 1000,
                  metadata_flags: int = 0x0007) -> list[Tag]:
        """Comodo: inventario sincrono + recupero buffer."""
        self.sync_inventory(timeout_ms=timeout_ms)
        return self.get_tag_buffer(metadata_flags=metadata_flags)

    # ------------------------------------------------------------------ #
    # Tag access (read / write)
    # ------------------------------------------------------------------ #
    def read_tag_data(self, bank: int, address: int, word_count: int,
                      access_password: bytes = b"\x00\x00\x00\x00",
                      timeout_ms: int = 1000) -> bytes:
        """0x28 - legge word_count parole (16 bit) dalla banca.

        Usa Option=0x05 (access password senza select): il primo tag risposto.
        """
        if len(access_password) != 4:
            raise ValueError("access_password must be 4 bytes")
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([0x05])                      # Option: password, no select
        data += bytes([bank])
        data += address.to_bytes(4, "big")
        data += bytes([word_count])
        data += access_password
        resp = self._command(P.CMD_READ_TAG_DATA, bytes(data))
        check_status(resp.status)
        # data risposta: Option(1) | [MetadataFlags se bit4] | DataRead
        return bytes(resp.data[1:])

    def write_tag_data(self, bank: int, address: int, write_data: bytes,
                       access_password: bytes = b"\x00\x00\x00\x00",
                       timeout_ms: int = 1000) -> None:
        """0x24 - scrive dati (lunghezza multipla di 2, max 64 byte) al primo tag."""
        if len(write_data) % 2 != 0:
            raise ValueError("write_data length must be a multiple of 2")
        if len(write_data) > 64:
            raise ValueError("write_data max 64 bytes (32 words)")
        if len(access_password) != 4:
            raise ValueError("access_password must be 4 bytes")
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([0x05])                      # Option: password, no select
        data += address.to_bytes(4, "big")
        data += bytes([bank])
        data += access_password
        data += write_data
        resp = self._command(P.CMD_WRITE_TAG_DATA, bytes(data))
        check_status(resp.status)

    def write_tag_epc(self, epc: bytes,
                      access_password: bytes = b"\x00\x00\x00\x00",
                      timeout_ms: int = 1000) -> None:
        """0x23 - scrive l'EPC (aggiorna automaticamente il PC)."""
        if len(access_password) != 4:
            raise ValueError("access_password must be 4 bytes")
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([0x05])                      # Option: password, no select
        data += access_password
        data += epc
        resp = self._command(P.CMD_WRITE_TAG_EPC, bytes(data))
        check_status(resp.status)

    # ------------------------------------------------------------------ #
    # Helper Step 1: read/write provando su ogni antenna
    # ------------------------------------------------------------------ #
    def read_try_all_antennas(self, antennas: list[int], bank: int,
                              address: int, word_count: int,
                              access_password: bytes = b"\x00\x00\x00\x00",
                              timeout_ms: int = 1000) -> dict:
        """Prova la lettura su ciascuna antenna; ritorna dict {ant: data|error}."""
        results = {}
        for ant in antennas:
            self.set_antenna_for_access(ant, ant)
            try:
                data = self.read_tag_data(bank, address, word_count,
                                          access_password, timeout_ms)
                results[ant] = {"ok": True, "data": data}
            except NoTagError as e:
                results[ant] = {"ok": False, "error": str(e)}
            except SilionError as e:
                results[ant] = {"ok": False, "error": str(e)}
        return results

    def write_try_all_antennas(self, antennas: list[int], bank: int,
                               address: int, write_data: bytes,
                               access_password: bytes = b"\x00\x00\x00\x00",
                               timeout_ms: int = 1000) -> dict:
        """Prova la scrittura su ciascuna antenna; ritorna dict {ant: ok|error}."""
        results = {}
        for ant in antennas:
            self.set_antenna_for_access(ant, ant)
            try:
                self.write_tag_data(bank, address, write_data,
                                    access_password, timeout_ms)
                results[ant] = {"ok": True}
            except SilionError as e:
                results[ant] = {"ok": False, "error": str(e)}
        return results


# ---------------------------------------------------------------------- #
# Factory da dizionario di configurazione (config.yaml)
# ---------------------------------------------------------------------- #
def reader_from_config(cfg: dict) -> "SIM7200Reader":
    """Crea un SIM7200Reader con il trasporto appropriato dalla config.

    Supporta:
      serial: {port, baudrate, timeout_s, inter_byte_timeout_s}
      tcp:    {host, port, timeout_s}
    """
    if "serial" in cfg:
        s = cfg["serial"]
        t = SerialTransport(
            port=s["port"],
            baudrate=s.get("baudrate", 115200),
            timeout_s=s.get("timeout_s", 2.0),
            inter_byte_timeout_s=s.get("inter_byte_timeout_s", 0.1),
        )
    elif "tcp" in cfg:
        c = cfg["tcp"]
        t = TcpTransport(
            host=c["host"],
            port=c.get("port", 8080),
            timeout_s=c.get("timeout_s", 2.0),
        )
    else:
        raise ValueError("config deve contenere 'serial' o 'tcp'")
    return SIM7200Reader(t)
