"""Classe high-level per il lettore Silion SIM7200.

Il modulo SIM7200 puo' essere collegato direttamente via UART (TTL 3.3V) oppure,
montato sulla baseboard SLD1090, via USB / RS232 / Ethernet TCP / WiFi / 4G.
Il protocollo Silion e' indipendente dal trasporto: qui si astrae con la classe
`Transport` (vedi `transports.py`).

Diagnostica: ogni istanza espone `reader.diag` (contatori runtime, vedi
`diagnostics.DiagCounters`) e `reader.health_check()` per un report sintetico.
"""
from __future__ import annotations

import datetime as dt
import logging

from .transports import Transport, SerialTransport, TcpTransport
from . import protocol as P
from .errors import check_status, SilionError, NoTagError
from .tags import Tag, parse_tag_buffer
from .diagnostics import DiagCounters, checkpoint

log = logging.getLogger("rfid_silion.reader")

# Limiti di validazione (datasheet SIM7200 / manuale EX10)
_ANTENNA_IDS = (1, 2, 3, 4)      # il modulo ha 4 porte SMA
_MAX_POWER_CDBM = 3300           # 33.00 dBm (solo versione full-band)
_STD_MAX_POWER_CDBM = 3000       # 30.00 dBm (versione standard/CE)
_MAX_READ_WORDS = 96             # oltre 96 word il lettore risponde 0x040B
_MAX_RESYNC_BYTES = 64           # max byte spuri scartati cercando l'header
_BANKS = (P.BANK_RESERVED, P.BANK_EPC, P.BANK_TID, P.BANK_USER)


def _check_antenna(ant: int, what: str = "antenna") -> None:
    if ant not in _ANTENNA_IDS:
        raise ValueError(f"invalid {what} id: {ant} (expected one of {_ANTENNA_IDS})")


def _check_timeout(timeout_ms: int) -> None:
    if not 1 <= timeout_ms <= 0xFFFF:
        raise ValueError(f"timeout_ms out of range 1..65535: {timeout_ms}")


def _check_bank(bank: int) -> None:
    if bank not in _BANKS:
        raise ValueError(f"invalid bank: {bank} (expected one of {_BANKS})")


class SIM7200Reader:
    """Driver per lettore Silion SIM7200 (seriale o TCP, vedi Transport)."""

    def __init__(self, transport: Transport):
        self._t = transport
        #: contatori diagnostici runtime (vedi diagnostics.DiagCounters)
        self.diag = DiagCounters()
        #: info firmware dall'ultimo boot_firmware() riuscito (None = non bootato)
        self.firmware_info: dict | None = None

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
        """Legge un frame completo, riallineandosi sull'header 0xFF.

        Eventuali byte spuri prima dell'header (rumore seriale, echo, resti
        di risposte precedenti) vengono scartati e conteggiati in
        `diag.bytes_discarded`, fino a un massimo di _MAX_RESYNC_BYTES.
        """
        discarded = 0
        b = self._read_exact(1)
        while b[0] != P.HEADER:
            discarded += 1
            if discarded > _MAX_RESYNC_BYTES:
                raise P.SilionFrameError(
                    f"Header 0xFF not found after discarding {discarded} bytes")
            b = self._read_exact(1)
        if discarded:
            self.diag.bytes_discarded += discarded
            log.warning("Resync: scartati %d byte spuri prima dell'header", discarded)
        data_len = self._read_exact(1)[0]
        # cmd(1) + status(2) + data_len + crc(2)
        rest = self._read_exact(1 + 2 + data_len + 2)
        frame = bytes([P.HEADER, data_len]) + rest
        log.debug("RX %s", frame.hex(" ").upper())
        return P.parse_response(frame)

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._t.read(n - len(buf))
            if not chunk:
                raise P.SilionTimeoutError(
                    f"Transport timeout reading {n} bytes (got {len(buf)})"
                )
            buf.extend(chunk)
        return bytes(buf)

    def _command(self, cmd: int, data: bytes = b"") -> P.Response:
        """Invia un comando e restituisce la risposta verificata.

        Aggiorna i contatori diagnostici (`self.diag`): timeout, errori di
        frame e status di errore vengono conteggiati qui; l'eccezione per
        status != 0 resta compito di check_status() nel metodo chiamante.
        """
        self.diag.commands_sent += 1
        self._flush()
        self._send(cmd, data)
        try:
            resp = self._recv_frame()
        except P.SilionTimeoutError as e:
            self.diag.timeouts += 1
            self.diag.record_error(e)
            raise
        except P.SilionFrameError as e:
            self.diag.frame_errors += 1
            self.diag.record_error(e)
            raise
        if resp.cmd != cmd:
            self.diag.frame_errors += 1
            err = P.SilionFrameError(
                f"Cmd mismatch: sent 0x{cmd:02X} recv 0x{resp.cmd:02X}")
            self.diag.record_error(err)
            raise err
        if resp.status == 0x0000:
            self.diag.responses_ok += 1
        elif resp.status == 0x0400:
            self.diag.no_tag_events += 1
        else:
            self.diag.status_errors += 1
            self.diag.record_error(
                f"cmd 0x{cmd:02X} -> status 0x{resp.status:04X}")
        return resp

    # ------------------------------------------------------------------ #
    # Comandi
    # ------------------------------------------------------------------ #
    def boot_firmware(self) -> dict:
        """0x04 - passa in App firmware. Obbligatorio al power-on."""
        resp = self._command(P.CMD_BOOT_FIRMWARE)
        check_status(resp.status)
        d = resp.data
        if len(d) < 20:
            raise P.SilionFrameError(
                f"Boot response too short: {len(d)} bytes ({d.hex().upper()})")
        info = {
            "bootloader_version": d[0:4].hex().upper(),
            "hardware_version": d[4:8].hex().upper(),
            "firmware_date": d[8:12].hex().upper(),
            "firmware_version": d[12:16].hex().upper(),
            "supported_protocol": d[16:20].hex().upper(),
        }
        self.firmware_info = info
        checkpoint(log, "boot_firmware",
                   firmware=info["firmware_version"],
                   hardware=info["hardware_version"])
        log.info("Boot firmware OK: %s", info)
        return info

    def set_region(self, region: int) -> None:
        """0x97 - imposta regione (EU=0x08)."""
        if not 0 <= region <= 0xFF:
            raise ValueError(f"region out of range 0..255: {region}")
        resp = self._command(P.CMD_SET_CURRENT_REGION, bytes([region]))
        check_status(resp.status)
        log.info("Region set to 0x%02X", region)

    def set_antennas_for_inventory(self, antenna_pairs: list[tuple[int, int]]) -> None:
        """0x91 option 0x02 - antenne (tx,rx) usate per inventory, nell'ordine di ciclo."""
        if not antenna_pairs:
            raise ValueError("antenna_pairs must not be empty")
        data = bytearray([0x02])
        for tx, rx in antenna_pairs:
            _check_antenna(tx, "tx antenna")
            _check_antenna(rx, "rx antenna")
            data += bytes([tx, rx])
        resp = self._command(P.CMD_SET_ANTENNA_PORTS, bytes(data))
        check_status(resp.status)
        log.info("Inventory antennas set: %s", antenna_pairs)

    def set_antenna_for_access(self, tx: int, rx: int) -> None:
        """0x91 option 0x00 - antenna singola per tag access (read/write)."""
        _check_antenna(tx, "tx antenna")
        _check_antenna(rx, "rx antenna")
        data = bytes([0x00, tx, rx])
        resp = self._command(P.CMD_SET_ANTENNA_PORTS, bytes(data))
        check_status(resp.status)
        log.debug("Access antenna set: tx=%d rx=%d", tx, rx)

    def set_antennas_power(self, powers: list[tuple[int, int, int]]) -> None:
        """0x91 option 0x03 - imposta potenze.

        powers: lista di (antenna_id, read_power_cdBm, write_power_cdBm)
        es. [(1, 2000, 2000), (2, 2000, 2000), (3, 2000, 2000)]  # 20 dBm

        Le potenze sono in centesimi di dBm; range valido 1..3300 (33 dBm solo
        sulla versione full-band; la versione CE arriva a 3000 = 30 dBm).
        """
        if not powers:
            raise ValueError("powers must not be empty")
        data = bytearray([0x03])
        for ant_id, rp, wp in powers:
            _check_antenna(ant_id)
            for name, p in (("read_power", rp), ("write_power", wp)):
                if not 0 < p <= _MAX_POWER_CDBM:
                    raise ValueError(
                        f"{name} out of range 1..{_MAX_POWER_CDBM} cdBm: {p}")
                if p > _STD_MAX_POWER_CDBM:
                    log.warning("%s=%d cdBm supera 30 dBm: valido solo su "
                                "versione full-band (fuori limiti ETSI in EU)",
                                name, p)
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
        _check_timeout(timeout_ms)
        # Option(1) | SearchFlags(2) | Timeout(2)   (no access pwd, no select)
        data = bytes([0x00,
                      (search_flags >> 8) & 0xFF, search_flags & 0xFF,
                      (timeout_ms >> 8) & 0xFF, timeout_ms & 0xFF])
        resp = self._command(P.CMD_SYNCHRONOUS_INVENTORY, data)
        check_status(resp.status)
        d = resp.data
        # Option(1) | SearchFlags(2) | TagsFound(1 o 4)
        if len(d) < 4:
            raise P.SilionFrameError(
                f"Sync inventory response too short: {d.hex().upper() or '-'}")
        flags = (d[1] << 8) | d[2]
        if flags & 0x10:  # >255 tag -> 4 byte
            if len(d) < 7:
                raise P.SilionFrameError(
                    f"Sync inventory response too short for 4-byte count: "
                    f"{d.hex().upper()}")
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
        count = self.sync_inventory(timeout_ms=timeout_ms)
        tags = self.get_tag_buffer(metadata_flags=metadata_flags)
        checkpoint(log, "inventory", stored=count, parsed=len(tags),
                   epcs=len({t.epc for t in tags}))
        return tags

    # ------------------------------------------------------------------ #
    # Tag access (read / write)
    # ------------------------------------------------------------------ #
    def read_tag_data(self, bank: int, address: int, word_count: int,
                      access_password: bytes = b"\x00\x00\x00\x00",
                      timeout_ms: int = 1000) -> bytes:
        """0x28 - legge word_count parole (16 bit) dalla banca.

        Usa Option=0x05 (access password senza select): il primo tag risposto.
        """
        _check_bank(bank)
        if not 0 <= address <= 0xFFFFFFFF:
            raise ValueError(f"address out of range 0..0xFFFFFFFF: {address}")
        if not 1 <= word_count <= _MAX_READ_WORDS:
            raise ValueError(
                f"word_count out of range 1..{_MAX_READ_WORDS}: {word_count}")
        _check_timeout(timeout_ms)
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
        _check_bank(bank)
        if not 0 <= address <= 0xFFFFFFFF:
            raise ValueError(f"address out of range 0..0xFFFFFFFF: {address}")
        if not write_data:
            raise ValueError("write_data must not be empty")
        if len(write_data) % 2 != 0:
            raise ValueError("write_data length must be a multiple of 2")
        if len(write_data) > 64:
            raise ValueError("write_data max 64 bytes (32 words)")
        _check_timeout(timeout_ms)
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
        if not epc or len(epc) % 2 != 0 or len(epc) > 62:
            raise ValueError(
                f"epc must be non-empty, even-length, max 62 bytes (got {len(epc)})")
        _check_timeout(timeout_ms)
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
            try:
                self.set_antenna_for_access(ant, ant)
                data = self.read_tag_data(bank, address, word_count,
                                          access_password, timeout_ms)
                results[ant] = {"ok": True, "data": data}
            except NoTagError as e:
                results[ant] = {"ok": False, "error": str(e)}
            except SilionError as e:
                results[ant] = {"ok": False, "error": str(e)}
            except P.SilionFrameError as e:
                # timeout/frame su QUESTA antenna: registra e prova la prossima
                log.warning("Ant %d: errore trasporto/frame in lettura: %s", ant, e)
                results[ant] = {"ok": False, "error": f"frame/timeout: {e}"}
        ok_ants = [a for a, r in results.items() if r["ok"]]
        checkpoint(log, "read_try_all_antennas", ok=ok_ants,
                   fail=[a for a in antennas if a not in ok_ants])
        return results

    def write_try_all_antennas(self, antennas: list[int], bank: int,
                               address: int, write_data: bytes,
                               access_password: bytes = b"\x00\x00\x00\x00",
                               timeout_ms: int = 1000) -> dict:
        """Prova la scrittura su ciascuna antenna; ritorna dict {ant: ok|error}."""
        results = {}
        for ant in antennas:
            try:
                self.set_antenna_for_access(ant, ant)
                self.write_tag_data(bank, address, write_data,
                                    access_password, timeout_ms)
                results[ant] = {"ok": True}
            except SilionError as e:
                results[ant] = {"ok": False, "error": str(e)}
            except P.SilionFrameError as e:
                log.warning("Ant %d: errore trasporto/frame in scrittura: %s", ant, e)
                results[ant] = {"ok": False, "error": f"frame/timeout: {e}"}
        ok_ants = [a for a, r in results.items() if r["ok"]]
        checkpoint(log, "write_try_all_antennas", ok=ok_ants,
                   fail=[a for a in antennas if a not in ok_ants])
        return results

    # ------------------------------------------------------------------ #
    # Health check / diagnostica
    # ------------------------------------------------------------------ #
    def health_check(self, check_antennas: bool = True) -> dict:
        """Controllo di salute sintetico (per assistenza/monitoraggio).

        Interroga solo la connessione antenne (0x61/0x05): non modifica lo
        stato del lettore. Ritorna un dict serializzabile in JSON con
        trasporto, info firmware, antenne connesse e contatori diagnostici.
        """
        report: dict = {
            "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
            "transport": self._t.describe(),
            "booted": self.firmware_info is not None,
            "firmware_info": self.firmware_info,
            "ok": True,
        }
        if check_antennas:
            try:
                report["antennas_connected"] = self.get_antenna_connection()
            except (SilionError, P.SilionFrameError) as e:
                report["antennas_error"] = str(e)
                report["ok"] = False
        # snapshot DOPO l'interrogazione antenne, cosi' include il suo esito
        report["counters"] = self.diag.snapshot()
        checkpoint(log, "health_check", ok=report["ok"],
                   antennas=report.get("antennas_connected", "-"))
        return report


# ---------------------------------------------------------------------- #
# Factory da dizionario di configurazione (config.yaml)
# ---------------------------------------------------------------------- #
def reader_from_config(cfg: dict) -> "SIM7200Reader":
    """Crea un SIM7200Reader con il trasporto appropriato dalla config.

    Supporta:
      serial: {port, baudrate, timeout_s, inter_byte_timeout_s}
      tcp:    {host, port, timeout_s}

    NB: la selezione e' per presenza di chiave, 'serial' ha priorita' su 'tcp'.
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
