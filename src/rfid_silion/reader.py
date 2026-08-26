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
import time
from typing import Any

from . import protocol as P
from .diagnostics import DiagCounters, checkpoint
from .errors import NoTagError, SilionError, check_status
from .tags import Tag, parse_tag_buffer, parse_tag_record
from .transports import SerialTransport, TcpTransport, Transport, TransportError

log = logging.getLogger("rfid_silion.reader")

# Limiti di validazione (datasheet SIM7200 / manuale EX10)
_ANTENNA_IDS = (1, 2, 3, 4)  # il modulo ha 4 porte SMA
_MAX_POWER_CDBM = 3300  # 33.00 dBm (solo versione full-band)
_STD_MAX_POWER_CDBM = 3000  # 30.00 dBm (versione standard/CE)
_MAX_READ_WORDS = 96  # oltre 96 word il lettore risponde 0x040B
_MAX_RESYNC_BYTES = 64  # max byte spuri scartati cercando l'header
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

    def __init__(
        self,
        transport: Transport,
        *,
        boot_timeout_ms: int = 3000,
        response_timeout_ms: int | None = None,
        max_power_dbm: int = 30,
    ):
        _check_timeout(boot_timeout_ms)
        if response_timeout_ms is not None:
            _check_timeout(response_timeout_ms)
        if not 1 <= max_power_dbm <= 33:
            raise ValueError(f"max_power_dbm fuori range 1..33: {max_power_dbm}")
        self._t = transport
        self.boot_timeout_ms = boot_timeout_ms
        self.response_timeout_ms = response_timeout_ms
        self.max_power_dbm = max_power_dbm
        self._max_power_cdbm = max_power_dbm * 100
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

    def _recv_raw_frame(self) -> bytes:
        """Legge i byte di un frame completo, riallineandosi sull'header 0xFF.

        Eventuali byte spuri prima dell'header (rumore seriale, echo, resti
        di risposte precedenti) vengono scartati e conteggiati in
        `diag.bytes_discarded`, fino a un massimo di _MAX_RESYNC_BYTES.

        Vale sia per il formato comune sia per quello esteso: la testa del frame
        e' identica e in entrambi i casi la risposta occupa `7 + DataLen` byte,
        perche' i due byte di stato non rientrano nella lunghezza dichiarata.
        """
        discarded = 0
        b = self._read_exact(1)
        while b[0] != P.HEADER:
            discarded += 1
            if discarded > _MAX_RESYNC_BYTES:
                raise P.SilionFrameError(
                    f"Header 0xFF not found after discarding {discarded} bytes"
                )
            b = self._read_exact(1)
        if discarded:
            self.diag.bytes_discarded += discarded
            log.warning("Resync: scartati %d byte spuri prima dell'header", discarded)
        data_len = self._read_exact(1)[0]
        # cmd(1) + status(2) + data_len + crc(2)
        rest = self._read_exact(1 + 2 + data_len + 2)
        frame = bytes([P.HEADER, data_len]) + rest
        log.debug("RX %s", frame.hex(" ").upper())
        return frame

    def _recv_frame(self) -> P.Response:
        """Legge e verifica una risposta nel formato comune."""
        return P.parse_response(self._recv_raw_frame())

    def _recv_extended_frame(self) -> P.ExtendedResponse:
        """Legge e verifica una risposta nel formato esteso "Moduletech"."""
        return P.parse_extended_response(self._recv_raw_frame())

    def _read_exact(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = self._t.read(n - len(buf))
            if not chunk:
                raise P.SilionTimeoutError(f"Transport timeout reading {n} bytes (got {len(buf)})")
            buf.extend(chunk)
        return bytes(buf)

    def _raise_frame_error(self, message: str) -> None:
        err = P.SilionFrameError(message)
        self.diag.frame_errors += 1
        self.diag.record_error(err)
        raise err

    def _operation_response_timeout(self, device_timeout_ms: int) -> int:
        """Mantiene il timeout host oltre la finestra di ricerca del lettore."""
        configured = self.response_timeout_ms or 0
        return max(configured, device_timeout_ms + 500)

    def _command(
        self, cmd: int, data: bytes = b"", response_timeout_ms: int | None = None
    ) -> P.Response:
        """Invia un comando e restituisce la risposta verificata.

        Aggiorna i contatori diagnostici: timeout, errori di trasporto/frame
        e status di errore vengono conteggiati qui.
        """
        timeout_ms = (
            response_timeout_ms if response_timeout_ms is not None else self.response_timeout_ms
        )
        timeout_s = timeout_ms / 1000.0 if timeout_ms is not None else None
        try:
            with self._t.temporary_timeout(timeout_s):
                self._flush()
                self._send(cmd, data)
                self.diag.commands_sent += 1
                resp = self._recv_frame()
        except P.SilionTimeoutError as e:
            self.diag.timeouts += 1
            self.diag.record_error(e)
            raise
        except P.SilionFrameError as e:
            self.diag.frame_errors += 1
            self.diag.record_error(e)
            raise
        except TransportError as e:
            self.diag.transport_errors += 1
            err = P.SilionTransportError(str(e))
            self.diag.record_error(err)
            raise err from e
        if resp.cmd != cmd:
            self._raise_frame_error(f"Cmd mismatch: sent 0x{cmd:02X} recv 0x{resp.cmd:02X}")
        if resp.status == 0x0000:
            self.diag.responses_ok += 1
        elif resp.status == 0x0400:
            self.diag.no_tag_events += 1
        else:
            self.diag.status_errors += 1
            self.diag.record_error(f"cmd 0x{cmd:02X} -> status 0x{resp.status:04X}")
        return resp

    def _extended_command(
        self, subcmd: int, subdata: bytes = b"", response_timeout_ms: int | None = None
    ) -> P.ExtendedResponse:
        """Invia un comando nel formato esteso e verifica la risposta.

        Gemello di `_command` per i comandi a due byte di opcode (0xAA48 e
        parenti). La contabilita' diagnostica e' la stessa: gli errori di questi
        comandi devono comparire negli stessi contatori, altrimenti l'health
        check racconterebbe solo meta' della storia.
        """
        timeout_ms = (
            response_timeout_ms if response_timeout_ms is not None else self.response_timeout_ms
        )
        timeout_s = timeout_ms / 1000.0 if timeout_ms is not None else None
        try:
            with self._t.temporary_timeout(timeout_s):
                self._flush()
                pkt = P.build_extended_packet(subcmd, subdata)
                log.debug("TX %s", pkt.hex(" ").upper())
                self._t.write(pkt)
                self.diag.commands_sent += 1
                resp = self._recv_extended_frame()
        except P.SilionTimeoutError as e:
            self.diag.timeouts += 1
            self.diag.record_error(e)
            raise
        except P.SilionFrameError as e:
            self.diag.frame_errors += 1
            self.diag.record_error(e)
            raise
        except TransportError as e:
            self.diag.transport_errors += 1
            err = P.SilionTransportError(str(e))
            self.diag.record_error(err)
            raise err from e
        if resp.subcmd != subcmd:
            self._raise_frame_error(
                f"Subcmd mismatch: sent 0x{subcmd:04X} recv 0x{resp.subcmd:04X}"
            )
        if resp.status == 0x0000:
            self.diag.responses_ok += 1
        elif resp.status == 0x0400:
            self.diag.no_tag_events += 1
        else:
            self.diag.status_errors += 1
            self.diag.record_error(f"subcmd 0x{subcmd:04X} -> status 0x{resp.status:04X}")
        return resp

    # ------------------------------------------------------------------ #
    # Comandi
    # ------------------------------------------------------------------ #
    def boot_firmware(self) -> dict:
        """0x04 - passa in App firmware. Obbligatorio al power-on."""
        resp = self._command(P.CMD_BOOT_FIRMWARE, response_timeout_ms=self.boot_timeout_ms)
        check_status(resp.status)
        d = resp.data
        if len(d) < 20:
            self._raise_frame_error(f"Boot response too short: {len(d)} bytes ({d.hex().upper()})")
        info = {
            "bootloader_version": d[0:4].hex().upper(),
            "hardware_version": d[4:8].hex().upper(),
            "firmware_date": d[8:12].hex().upper(),
            "firmware_version": d[12:16].hex().upper(),
            "supported_protocol": d[16:20].hex().upper(),
        }
        self.firmware_info = info
        checkpoint(
            log,
            "boot_firmware",
            firmware=info["firmware_version"],
            hardware=info["hardware_version"],
        )
        log.info("Boot firmware OK: %s", info)
        return info

    # ---------------- Parametri di protocollo Gen2 (0x9B / 0x6B) ---------
    def _set_gen2_param(self, parameter: int, option: int | None, value: int | None) -> None:
        data = bytearray([P.PROTOCOL_GEN2, parameter])
        if option is not None:
            data.append(option)
        if value is not None:
            data.append(value)
        resp = self._command(P.CMD_SET_PROTOCOL_CONFIG, bytes(data))
        check_status(resp.status)

    def set_gen2_session(self, session: int) -> None:
        """0x9B/0x00 - sessione Gen2 (S0..S3, default S0).

        Conta quando si legge piu' volte la stessa popolazione: in S0 il flag di
        inventario decade subito e i tag piu' forti continuano a rispondere,
        affamando i deboli. S2 e S3 lo mantengono per un tempo persistente, cosi'
        ogni passata da spazio anche ai tag che rispondono male.
        """
        if session not in (0, 1, 2, 3):
            raise ValueError(f"session out of range 0..3: {session}")
        self._set_gen2_param(P.GEN2_PARAM_SESSION, None, session)
        checkpoint(log, "set_gen2_session", session=session)

    def set_gen2_target(self, target: int, *, dynamic: bool = False) -> None:
        """0x9B/0x01 - target A/B, statico oppure con ribaltamento automatico.

        Con `dynamic=True` il modulo inventaria finche' trova tag, poi ribalta il
        target da solo: e' il modo per far rispondere ogni tag una volta per
        passata. Il manuale avverte che ha effetto **solo** su 0x22 e 0xAA48;
        gli altri comandi usano il target di partenza.
        """
        if target not in (0, 1):
            raise ValueError(f"target must be 0 (A / A-B) or 1 (B / B-A): {target}")
        option = P.GEN2_TARGET_DYNAMIC if dynamic else P.GEN2_TARGET_STATIC
        self._set_gen2_param(P.GEN2_PARAM_TARGET, option, target)
        checkpoint(log, "set_gen2_target", target=target, dynamic=dynamic)

    def set_gen2_q(self, q: int | None = None) -> None:
        """0x9B/0x12 - parametro Q dell'anti-collisione.

        `q=None` lascia il Q dinamico (default di fabbrica), che il modulo adatta
        alla popolazione. Un Q fisso serve soprattutto a rendere ripetibili le
        misure di collaudo, dove la variabilita' del Q dinamico confonderebbe il
        confronto fra configurazioni.
        """
        if q is None:
            self._set_gen2_param(P.GEN2_PARAM_Q, P.GEN2_Q_DYNAMIC, None)
        else:
            if not 0 <= q <= 0x0F:
                raise ValueError(f"q out of range 0..15: {q}")
            self._set_gen2_param(P.GEN2_PARAM_Q, P.GEN2_Q_STATIC, q)
        checkpoint(log, "set_gen2_q", q="dinamico" if q is None else q)

    def set_gen2_rf_mode(self, rf_mode: int) -> None:
        """0x9B/0x02 - modalita' RF (sensibilita' contro velocita').

        `P.RF_MODE_MAX_SENSITIVITY` (0x71) guadagna 5 dB sul default: e' la leva
        piu' diretta per i tag schermati da liquido o coperti da altri tag.

        Attenzione: se il modulo non supporta la modalita' richiesta risponde
        comunque con stato 0x0000 ma **ripiega sulla modalita' 7**. L'unico modo
        di sapere cosa e' stato applicato davvero e' rileggerlo con 0x6B, ed e'
        quello che fa `get_gen2_param`.
        """
        if not 0 <= rf_mode <= 0xFF:
            raise ValueError(f"rf_mode out of range 0..255: {rf_mode}")
        self._set_gen2_param(P.GEN2_PARAM_RF_MODE, None, rf_mode)
        checkpoint(
            log,
            "set_gen2_rf_mode",
            rf_mode=f"0x{rf_mode:02X}",
            sensitivity_dbm=P.RF_MODE_SENSITIVITY_DBM.get(rf_mode),
        )

    def get_gen2_param(self, parameter: int) -> bytes:
        """0x6B - rilegge un parametro di protocollo Gen2.

        Ritorna i byte che seguono `protocollo | parametro` nella risposta:
        eventuale Option e Value, la cui presenza dipende dal parametro.
        """
        resp = self._command(P.CMD_GET_PROTOCOL_CONFIG, bytes([P.PROTOCOL_GEN2, parameter]))
        check_status(resp.status)
        if len(resp.data) < 2 or resp.data[0] != P.PROTOCOL_GEN2 or resp.data[1] != parameter:
            self._raise_frame_error(
                f"0x6B: risposta per un parametro diverso da quello chiesto "
                f"({resp.data[:2].hex().upper()})"
            )
        return resp.data[2:]

    # ---------------- Configurazione lettore (0x95) ----------------------
    def set_antenna_dwell_time(self, dwell_ms: int) -> None:
        """0x95/0x02 - tempo di permanenza su ciascuna antenna (default 4000 ms).

        Il modulo passa all'antenna successiva quando scade questo tempo oppure
        quando smette di trovare tag nuovi. Abbassarlo rende le passate piu'
        rapide e quindi piu' numerose a parita' di tempo.
        """
        if not 20 <= dwell_ms <= 60000:
            raise ValueError(f"dwell_ms out of range 20..60000: {dwell_ms}")
        resp = self._command(
            P.CMD_SET_READER_CONFIG,
            bytes([P.READER_OPT_ANTENNA_DWELL]) + dwell_ms.to_bytes(4, "big"),
        )
        check_status(resp.status)
        checkpoint(log, "set_antenna_dwell_time", dwell_ms=dwell_ms)

    def set_duty_cycle(self, full_power_ms: int, period_ms: int) -> None:
        """0x95/0x11 - limita il ciclo di lavoro per contenere il riscaldamento.

        Serve nelle letture lunghe a piena potenza. `full_power_ms` e' il tempo a
        pieno carico prima che il duty cycle entri in azione, `period_ms` il
        periodo. Richiede firmware >= 20240606.
        """
        for nome, valore in (("full_power_ms", full_power_ms), ("period_ms", period_ms)):
            if not 0 <= valore <= 0xFFFF:
                raise ValueError(f"{nome} out of range 0..65535: {valore}")
        resp = self._command(
            P.CMD_SET_READER_CONFIG,
            bytes([P.READER_OPT_DUTY_CYCLE])
            + full_power_ms.to_bytes(2, "big")
            + period_ms.to_bytes(2, "big"),
        )
        check_status(resp.status)
        checkpoint(log, "set_duty_cycle", full_power_ms=full_power_ms, period_ms=period_ms)

    def set_power_mode(self, power_mode: int = P.POWER_MODE_RESPONSIVE) -> None:
        """0x98 - modalita' di consumo del modulo.

        Nelle modalita' 2 e 3 il modulo spegne la radio dopo ogni comando e ci
        mette 80-200 ms a riaccenderla: su una sequenza di inventory ripetuti si
        perde piu' tempo ad accendere che a leggere. Per la lettura di una
        scatola va tenuta 0 o 1.
        """
        if not 0 <= power_mode <= 3:
            raise ValueError(f"power_mode out of range 0..3: {power_mode}")
        resp = self._command(P.CMD_SET_POWER_MODE, bytes([power_mode]))
        check_status(resp.status)
        if power_mode not in P.POWER_MODES_RESPONSIVE:
            log.warning(
                "Power mode %d: la radio si spegne fra un comando e l'altro, "
                "la lettura ripetuta ne risente",
                power_mode,
            )
        checkpoint(log, "set_power_mode", power_mode=power_mode)

    def set_rssi_report_mode(self, mode: int = P.RSSI_MODE_MAX) -> None:
        """0x9A/0x06 - riporta il massimo RSSI osservato invece dell'ultimo.

        Su piu' letture dello stesso tag l'ultimo valore e' rumoroso, il massimo
        e' stabile: serve a confrontare i tag fra loro per stimarne la salute.
        """
        if mode not in (P.RSSI_MODE_LAST, P.RSSI_MODE_MAX):
            raise ValueError(f"mode must be 0 (ultimo) or 1 (massimo): {mode}")
        resp = self._command(
            P.CMD_SET_UNIQUE_CONFIG, bytes([P.UNIQUE_OPT, P.UNIQUE_KEY_RSSI_MODE, mode])
        )
        check_status(resp.status)
        checkpoint(log, "set_rssi_report_mode", mode=mode)

    def set_rssi_filter(self, threshold_dbm: int | None) -> None:
        """0xAA5B - soglia sotto la quale i tag non vengono riportati.

        `None` disattiva il filtro, ed e' quello che serve quando l'obiettivo e'
        leggere **tutto**: un filtro ereditato dalla configurazione di fabbrica
        scarterebbe in silenzio proprio i tag piu' difficili, cioe' quelli per
        cui esiste tutto il resto di questo lavoro.
        """
        if threshold_dbm is None:
            subdata = bytes([0x01, 0x00, 0x00, 0x00])
        else:
            if not -128 <= threshold_dbm <= -1:
                raise ValueError(f"soglia RSSI attesa fra -128 e -1 dBm: {threshold_dbm}")
            subdata = bytes([0x01, P.RSSI_FILTER_MARKER, threshold_dbm & 0xFF, 0x00])
        resp = self._extended_command(P.SUBCMD_RSSI_FILTER, subdata)
        check_status(resp.status)
        checkpoint(log, "set_rssi_filter", threshold_dbm=threshold_dbm)

    def get_rssi_filter(self) -> int | None:
        """0xAA5B - soglia RSSI attualmente impostata, `None` se disattivata."""
        resp = self._extended_command(P.SUBCMD_RSSI_FILTER, P.RSSI_FILTER_QUERY)
        check_status(resp.status)
        if len(resp.data) < 4:
            self._raise_frame_error(
                f"0xAA5B: attesi 4 byte di stato, ricevuti {len(resp.data)}"
            )
        if resp.data[1] != P.RSSI_FILTER_MARKER:
            return None
        return int.from_bytes(resp.data[2:3], "big", signed=True)

    # ---------------- Inventory asincrono a tag densi (0xAA58/0xAA59) ----
    def start_dense_inventory(
        self,
        *,
        metadata_flags: int = 0x0007,
        search_flags: int = 0x0000,
        dense: bool = True,
    ) -> None:
        """0xAA58 - avvia l'inventory asincrono in modalita' tag densi.

        Il manuale la descrive per «ambienti dove i tag sono impilati e difficili
        da leggere»: e' il caso di una scatola piena di contenitori.

        Due cose da sapere prima di usarla. In questa modalita' sessione, target,
        Q e modalita' RF **non sono impostabili**: li gestisce il modulo, quindi
        va confrontata con la taratura manuale, non aggiunta ad essa. Ed e'
        **asincrona**: da qui in poi il modulo spinge i tag da solo finche' non
        si chiama `stop_dense_inventory`.

        Il manuale la dichiara supportata in CE ma non in FCC.
        """
        if not 0 <= metadata_flags <= 0xFFFF:
            raise ValueError(f"metadata_flags fuori range 0..0xFFFF: {metadata_flags}")
        if not 0 <= search_flags <= 0xFFFF:
            raise ValueError(f"search_flags fuori range 0..0xFFFF: {search_flags}")
        # ExConfigData: 20 byte di cui solo il primo impostabile.
        # 0x00 = tag densi, 0x01 = pochi tag e facili.
        subdata = bytearray([0x00 if dense else 0x01] + [0x00] * 19)
        subdata += metadata_flags.to_bytes(2, "big")
        subdata += bytes([0x00])  # Option: nessun filtro (non supportato qui)
        subdata += search_flags.to_bytes(2, "big")
        resp = self._extended_command(P.SUBCMD_DENSE_INVENTORY_START, bytes(subdata))
        check_status(resp.status)
        checkpoint(log, "start_dense_inventory", dense=dense, metadata_flags=metadata_flags)

    def stop_dense_inventory(self) -> None:
        """0xAA59 - ferma l'inventory asincrono."""
        resp = self._extended_command(P.SUBCMD_DENSE_INVENTORY_STOP)
        check_status(resp.status)
        checkpoint(log, "stop_dense_inventory")

    def collect_uploaded_tags(
        self,
        duration_s: float,
        *,
        metadata_flags: int = 0x0007,
        stop_event: Any = None,
    ) -> list[Tag]:
        """Raccoglie i tag che il modulo spinge, fino allo scadere del tempo.

        A differenza di tutto il resto del driver qui non c'e' una domanda a cui
        corrisponde una risposta: si legge quello che arriva. Un timeout del
        trasporto **non e' un errore** — significa solo che in quel momento non
        e' arrivato niente — ed e' la ragione per cui il ciclo lo ignora invece
        di propagarlo.
        """
        if duration_s <= 0:
            raise ValueError("duration_s deve essere maggiore di zero")
        scadenza = time.monotonic() + duration_s
        raccolti: list[Tag] = []
        while time.monotonic() < scadenza:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                frame = self._recv_raw_frame()
            except P.SilionTimeoutError:
                continue          # silenzio radio: normale in ascolto
            except P.SilionFrameError as exc:
                self.diag.frame_errors += 1
                self.diag.record_error(exc)
                log.warning("Frame scartato durante l'ascolto: %s", exc)
                continue
            if P.is_extended_frame(frame):
                continue          # eco di un comando di controllo, non un tag
            try:
                pacchetto = P.parse_async_upload(frame)
                if pacchetto.is_heartbeat:
                    continue
                tag, _ = parse_tag_record(pacchetto.record, 0, pacchetto.metadata_flags)
            except (P.SilionFrameError, ValueError, IndexError) as exc:
                self.diag.frame_errors += 1
                log.warning("Pacchetto auto-caricato non interpretabile: %s", exc)
                continue
            raccolti.append(tag)
        checkpoint(
            log,
            "collect_uploaded_tags",
            duration_s=duration_s,
            tags=len(raccolti),
            epcs=len({t.epc for t in raccolti}),
        )
        return raccolti

    def dense_inventory(
        self,
        duration_s: float = 2.0,
        *,
        metadata_flags: int = 0x0007,
        stop_event: Any = None,
    ) -> list[Tag]:
        """Comodo: avvia la modalita' densa, ascolta, ferma.

        Lo stop e' in `finally`: lasciare il modulo a trasmettere dopo un errore
        significherebbe trovarselo che parla da solo al comando successivo.
        """
        self.start_dense_inventory(metadata_flags=metadata_flags)
        try:
            return self.collect_uploaded_tags(
                duration_s, metadata_flags=metadata_flags, stop_event=stop_event
            )
        finally:
            try:
                self.stop_dense_inventory()
            except (P.SilionFrameError, SilionError):
                log.exception("Stop dell'inventory asincrono non riuscito")

    # ---------------- Diagnostica antenna (0xAA4A) -----------------------
    def measure_standing_wave(
        self,
        antenna: int,
        *,
        band: int = 0x08,
        frequencies_khz: list[int] | None = None,
        timeout_ms: int = 30000,
    ) -> list[dict]:
        """0xAA4A - misura il return loss dell'antenna, frequenza per frequenza.

        E' l'unico modo di sapere se un'antenna e' davvero adattata alla banda in
        cui la si sta usando, invece di dedurlo dal datasheet. Il manuale indica
        VSWR < 7 come soglia di accettabilita': oltre, l'antenna e' disadattata,
        danneggiata, mal collegata o assente.

        `frequencies_khz=None` misura tutte le frequenze della banda indicata.
        Il valore predefinito e' CE_LOW/EU (0x08): sui moduli certificati per
        l'Europa il firmware rifiuta le altre bande con lo stato 0x010B.
        La potenza di prova non e' impostabile: il modulo usa sempre 20 dBm, e il
        campo esiste solo per compatibilita'. Il test puo' durare decine di
        secondi, da cui il timeout generoso.
        """
        _check_antenna(antenna)
        elenco = list(frequencies_khz or [])
        if len(elenco) > 0xFF:
            raise ValueError("al massimo 255 frequenze per misura")
        subdata = bytearray()
        # Il campo potenza esiste nel frame ma il manuale lo marca non valido:
        # il modulo misura sempre a 20 dBm. Si invia lo stesso valore dell'esempio
        # ufficiale, cosi' il frame e' confrontabile byte per byte con il manuale.
        subdata += P.STANDING_WAVE_TEST_POWER_CDBM.to_bytes(2, "big")
        subdata += bytes([antenna, band & 0xFF, len(elenco)])
        for frequenza in elenco:
            if not 0 <= frequenza <= 0xFFFFFF:
                raise ValueError(f"frequenza fuori intervallo (kHz su 3 byte): {frequenza}")
            subdata += frequenza.to_bytes(3, "big")

        resp = self._extended_command(
            P.SUBCMD_STANDING_WAVE,
            bytes(subdata),
            response_timeout_ms=self._operation_response_timeout(timeout_ms),
        )
        check_status(resp.status)

        corpo = resp.data[5:]  # i primi 5 byte ripetono i parametri inviati
        if len(corpo) % 4:
            self._raise_frame_error(
                f"0xAA4A: {len(corpo)} byte di misure, non multipli di 4"
            )
        misure = []
        for offset in range(0, len(corpo), 4):
            frequenza = int.from_bytes(corpo[offset : offset + 3], "big")
            grezzo = corpo[offset + 3]
            vswr = P.vswr_from_return_loss(grezzo)
            misure.append(
                {
                    "frequency_khz": frequenza,
                    "return_loss_db": grezzo / 10.0,
                    "vswr": vswr,
                    "ok": vswr < P.VSWR_ALERT_THRESHOLD,
                }
            )
        peggiore = max((m["vswr"] for m in misure), default=None)
        checkpoint(
            log,
            "measure_standing_wave",
            antenna=antenna,
            points=len(misure),
            worst_vswr=None if peggiore is None else round(peggiore, 2),
        )
        return misure

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
                if not 0 < p <= self._max_power_cdbm:
                    raise ValueError(f"{name} out of range 1..{self._max_power_cdbm} cdBm: {p}")
                if p > _STD_MAX_POWER_CDBM:
                    log.warning(
                        "%s=%d cdBm supera 30 dBm: valido solo su "
                        "versione full-band (fuori limiti ETSI in EU)",
                        name,
                        p,
                    )
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
        d = resp.data
        if not d or d[0] != 0x05:
            self._raise_frame_error(
                f"Get antenna response senza Option 0x05: {d.hex().upper() or '-'}"
            )
        if (len(d) - 1) % 2:
            self._raise_frame_error(
                f"Get antenna response con coppia incompleta: {d.hex().upper()}"
            )
        connected = []
        for i in range(1, len(d), 2):
            ant_id, state = d[i], d[i + 1]
            if ant_id not in _ANTENNA_IDS or state not in (0, 1):
                self._raise_frame_error(
                    f"Get antenna response non valida: antenna={ant_id} state={state}"
                )
            if state:
                connected.append(ant_id)
        log.info("Connected antennas: %s", connected)
        return connected

    # ------------------------------------------------------------------ #
    # Inventory
    # ------------------------------------------------------------------ #
    def sync_inventory(
        self,
        timeout_ms: int = 1000,
        search_flags: int = 0x0000,
        embedded_read: list[tuple[int, int, int]] | None = None,
    ) -> int:
        """0x22 - inventario sincrono, ritorna numero di tag archiviati.

        `embedded_read` innesta una lettura di memoria nell'inventory: ogni tag
        trovato riporta anche il contenuto delle banche richieste, come lista di
        `(bank, address_words, word_count)`. E' cio' che permette di leggere il
        payload di **tutti** i tag in un giro solo, invece che uno per volta.

        Il manuale garantisce che se la lettura innestata fallisce l'EPC viene
        comunque riportato: l'embedded read non peggiora la completezza
        dell'inventario, al massimo lascia il dato vuoto.

        Per ricevere i dati serve poi `get_tag_buffer` con il flag
        `META_TAG_DATA` acceso.
        """
        _check_timeout(timeout_ms)
        if not 0 <= search_flags <= 0xFFFF:
            raise ValueError(f"search_flags fuori range 0..0xFFFF: {search_flags}")
        coda = b""
        if embedded_read:
            search_flags |= P.SEARCH_FLAG_EMBEDDED_DATA
            coda = P.build_embedded_read(embedded_read)
        data = bytes(
            [
                0x00,
                (search_flags >> 8) & 0xFF,
                search_flags & 0xFF,
                (timeout_ms >> 8) & 0xFF,
                timeout_ms & 0xFF,
            ]
        ) + coda
        resp = self._command(
            P.CMD_SYNCHRONOUS_INVENTORY,
            data,
            response_timeout_ms=self._operation_response_timeout(timeout_ms),
        )
        check_status(resp.status)
        d = resp.data
        if len(d) < 4:
            self._raise_frame_error(f"Sync inventory response too short: {d.hex().upper() or '-'}")
        if d[0] != 0x00:
            self._raise_frame_error(f"Sync inventory Option inattesa 0x{d[0]:02X}")
        flags = (d[1] << 8) | d[2]
        extended_count = bool(flags & 0x10)
        expected_len = 7 if extended_count else 4
        if len(d) != expected_len:
            self._raise_frame_error(
                f"Sync inventory response length {len(d)}, expected {expected_len}: "
                f"{d.hex().upper()}"
            )
        count = int.from_bytes(d[3:7], "big") if extended_count else d[3]
        log.info("Sync inventory: %d tags", count)
        return count

    def get_tag_buffer(self, metadata_flags: int = 0x0007, option: int = 0x00) -> list[Tag]:
        """0x29 - recupera i tag archiviati dall'ultimo 0x22."""
        data = bytes(
            [
                (metadata_flags >> 8) & 0xFF,
                metadata_flags & 0xFF,
                option,
            ]
        )
        resp = self._command(P.CMD_GET_TAG_BUFFER, data)
        check_status(resp.status)
        try:
            return parse_tag_buffer(resp.data, metadata_flags)
        except P.SilionFrameError as e:
            self.diag.frame_errors += 1
            self.diag.record_error(e)
            raise

    def inventory(
        self,
        timeout_ms: int = 1000,
        metadata_flags: int = 0x0007,
        embedded_read: list[tuple[int, int, int]] | None = None,
    ) -> list[Tag]:
        """Comodo: inventario sincrono + recupero buffer.

        Con `embedded_read` il flag `META_TAG_DATA` viene acceso da solo: chiedere
        la lettura innestata e poi dimenticare di richiederne i dati sarebbe un
        errore silenzioso, con i tag che tornano senza `embedded_data` e nessuna
        spiegazione.
        """
        if embedded_read:
            metadata_flags |= P.META_TAG_DATA
        count = self.sync_inventory(timeout_ms=timeout_ms, embedded_read=embedded_read)
        tags = self.get_tag_buffer(metadata_flags=metadata_flags)
        checkpoint(
            log,
            "inventory",
            stored=count,
            parsed=len(tags),
            epcs=len({t.epc for t in tags}),
            embedded=bool(embedded_read),
        )
        return tags

    # ------------------------------------------------------------------ #
    # Tag access (read / write)
    # ------------------------------------------------------------------ #
    def read_tag_data(
        self,
        bank: int,
        address: int,
        word_count: int,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
        select_epc: bytes | None = None,
    ) -> bytes:
        """0x28 - legge word_count parole (16 bit) dalla banca.

        Senza `select_epc` usa Option 0x05 (password, nessun filtro) e agisce sul
        **primo tag che risponde**: va bene solo con un tag in campo.

        Con `select_epc` il comando punta quell'EPC preciso anche in mezzo ad
        altri. E' il filtro Select, ed e' cio' che permette di leggere un
        contenitore scelto senza doverlo isolare fisicamente.
        """
        _check_bank(bank)
        if not 0 <= address <= 0xFFFFFFFF:
            raise ValueError(f"address out of range 0..0xFFFFFFFF: {address}")
        if not 1 <= word_count <= _MAX_READ_WORDS:
            raise ValueError(f"word_count out of range 1..{_MAX_READ_WORDS}: {word_count}")
        _check_timeout(timeout_ms)
        if len(access_password) != 4:
            raise ValueError("access_password must be 4 bytes")
        option, singulation = P.build_tag_singulation(
            P.SELECT_PASSWORD_ONLY if select_epc is None else P.SELECT_BY_EPC_ID,
            access_password,
            select_data=b"" if select_epc is None else bytes(select_epc),
        )
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([option])
        data += bytes([bank])
        data += address.to_bytes(4, "big")
        data += bytes([word_count])
        data += singulation
        resp = self._command(
            P.CMD_READ_TAG_DATA,
            bytes(data),
            response_timeout_ms=self._operation_response_timeout(timeout_ms),
        )
        check_status(resp.status)
        # Nessuna di queste opzioni richiede metadati: dopo l'eco dell'option
        # devono seguire esattamente le word richieste. L'eco va confrontata con
        # l'option **inviata**, non con un valore fisso: cambia se si usa il
        # filtro Select.
        expected_len = 1 + word_count * 2
        if len(resp.data) != expected_len or resp.data[0] != option:
            ricevuta = (
                f"option 0x{resp.data[0]:02X}" if resp.data else "risposta vuota"
            )
            self._raise_frame_error(
                f"Read response non valida: attesi {expected_len} byte con "
                f"option 0x{option:02X}, ricevuti {len(resp.data)} byte "
                f"({ricevuta}: {resp.data.hex().upper() or '-'})"
            )
        return bytes(resp.data[1:])

    def write_tag_data(
        self,
        bank: int,
        address: int,
        write_data: bytes,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
        select_epc: bytes | None = None,
    ) -> None:
        """0x24 - scrive dati (lunghezza multipla di 2, max 64 byte).

        Senza `select_epc` colpisce il primo tag che risponde; con il filtro
        Select colpisce soltanto quell'EPC.
        """
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
        option, singulation = P.build_tag_singulation(
            P.SELECT_PASSWORD_ONLY if select_epc is None else P.SELECT_BY_EPC_ID,
            access_password,
            select_data=b"" if select_epc is None else bytes(select_epc),
        )
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([option])
        data += address.to_bytes(4, "big")
        data += bytes([bank])
        data += singulation
        data += write_data
        resp = self._command(
            P.CMD_WRITE_TAG_DATA,
            bytes(data),
            response_timeout_ms=self._operation_response_timeout(timeout_ms),
        )
        check_status(resp.status)

    def write_tag_epc(
        self, epc: bytes, access_password: bytes = b"\x00\x00\x00\x00", timeout_ms: int = 1000
    ) -> None:
        """0x23 - scrive l'EPC (aggiorna automaticamente il PC)."""
        if not epc or len(epc) % 2 != 0 or len(epc) > 62:
            raise ValueError(f"epc must be non-empty, even-length, max 62 bytes (got {len(epc)})")
        _check_timeout(timeout_ms)
        if len(access_password) != 4:
            raise ValueError("access_password must be 4 bytes")
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([0x05])  # Option: password, no select
        data += access_password
        data += epc
        resp = self._command(
            P.CMD_WRITE_TAG_EPC,
            bytes(data),
            response_timeout_ms=self._operation_response_timeout(timeout_ms),
        )
        check_status(resp.status)

    def lock_tag(
        self,
        mask: int,
        action: int,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
    ) -> None:
        """0x25 - blocca o sblocca le banche di memoria del primo tag che risponde.

        `mask` e `action` si compongono con `protocol.build_lock_bits`, che
        conosce la disposizione dei bit della Figura 6 del manuale. Solo i 10 bit
        bassi sono validi in entrambi i campi.

        Attenzione: le operazioni permanenti (`permalock`, `permaunlock`) **non
        sono annullabili**, su nessun tag e con nessuna password.

        A differenza di read/write questo comando **non ammette Option 0x05**
        (manuale EX10 2024-12 §6.3): si usa Option 0x00, e la password di accesso
        e' comunque sempre presente nel payload.
        """
        for nome, valore in (("mask", mask), ("action", action)):
            if not isinstance(valore, int) or isinstance(valore, bool):
                raise TypeError(f"{nome} must be an integer")
            if not 0 <= valore <= 0x03FF:
                raise ValueError(f"{nome} out of range 0..0x03FF (only the low 10 bits are valid)")
        if action & ~mask:
            raise ValueError(
                "action sets bits that mask leaves at zero: those bits would be ignored"
            )
        _check_timeout(timeout_ms)
        if len(access_password) != 4:
            raise ValueError("access_password must be 4 bytes")
        data = bytearray()
        data += timeout_ms.to_bytes(2, "big")
        data += bytes([0x00])  # Option: nessun filtro (0x05 vietato per 0x25)
        data += access_password
        data += mask.to_bytes(2, "big")
        data += action.to_bytes(2, "big")
        resp = self._command(
            P.CMD_LOCK_TAG,
            bytes(data),
            response_timeout_ms=self._operation_response_timeout(timeout_ms),
        )
        check_status(resp.status)

    # ------------------------------------------------------------------ #
    # Helper Step 1: read/write provando su ogni antenna
    # ------------------------------------------------------------------ #
    def read_try_all_antennas(
        self,
        antennas: list[int],
        bank: int,
        address: int,
        word_count: int,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
        select_epc: bytes | None = None,
    ) -> dict:
        """Prova la lettura su ciascuna antenna; ritorna dict {ant: data|error}.

        Con `select_epc` la lettura punta quel tag preciso anche in mezzo ad
        altri: e' cio' che permette di leggere un contenitore dentro una scatola
        piena senza tirarlo fuori.
        """
        results = {}
        for ant in antennas:
            try:
                self.set_antenna_for_access(ant, ant)
                data = self.read_tag_data(
                    bank, address, word_count, access_password, timeout_ms, select_epc
                )
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
        checkpoint(
            log, "read_try_all_antennas", ok=ok_ants, fail=[a for a in antennas if a not in ok_ants]
        )
        return results

    def write_try_all_antennas(
        self,
        antennas: list[int],
        bank: int,
        address: int,
        write_data: bytes,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
    ) -> dict:
        """Prova la scrittura su ciascuna antenna; ritorna dict {ant: ok|error}."""
        results = {}
        for ant in antennas:
            try:
                self.set_antenna_for_access(ant, ant)
                self.write_tag_data(bank, address, write_data, access_password, timeout_ms)
                results[ant] = {"ok": True}
            except SilionError as e:
                results[ant] = {"ok": False, "error": str(e)}
            except P.SilionFrameError as e:
                log.warning("Ant %d: errore trasporto/frame in scrittura: %s", ant, e)
                results[ant] = {"ok": False, "error": f"frame/timeout: {e}"}
        ok_ants = [a for a, r in results.items() if r["ok"]]
        checkpoint(
            log,
            "write_try_all_antennas",
            ok=ok_ants,
            fail=[a for a in antennas if a not in ok_ants],
        )
        return results

    def write_epc_try_all_antennas(
        self,
        antennas: list[int],
        epc: bytes,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
    ) -> dict:
        """Prova il cambio EPC in ordine e si ferma al primo successo."""
        results = {}
        for ant in antennas:
            try:
                self.set_antenna_for_access(ant, ant)
                self.write_tag_epc(epc, access_password, timeout_ms)
                results[ant] = {"ok": True}
                break
            except SilionError as exc:
                results[ant] = {"ok": False, "error": str(exc)}
            except P.SilionFrameError as exc:
                log.warning("Ant %d: errore trasporto/frame in scrittura EPC: %s", ant, exc)
                results[ant] = {"ok": False, "error": f"frame/timeout: {exc}"}
        ok_ants = [antenna for antenna, result in results.items() if result["ok"]]
        checkpoint(
            log,
            "write_epc_try_all_antennas",
            ok=ok_ants,
            fail=[antenna for antenna in results if antenna not in ok_ants],
        )
        return results

    def lock_try_all_antennas(
        self,
        antennas: list[int],
        mask: int,
        action: int,
        access_password: bytes = b"\x00\x00\x00\x00",
        timeout_ms: int = 1000,
    ) -> dict:
        """Prova il lock in ordine e si ferma al primo successo.

        Come per il cambio EPC ci si ferma appena una antenna riesce: ripetere un
        lock gia' andato a buon fine non aggiungerebbe nulla, e su un tag appena
        bloccato i tentativi successivi fallirebbero comunque.
        """
        results = {}
        for ant in antennas:
            try:
                self.set_antenna_for_access(ant, ant)
                self.lock_tag(mask, action, access_password, timeout_ms)
                results[ant] = {"ok": True}
                break
            except SilionError as exc:
                results[ant] = {"ok": False, "error": str(exc)}
            except P.SilionFrameError as exc:
                log.warning("Ant %d: errore trasporto/frame nel lock: %s", ant, exc)
                results[ant] = {"ok": False, "error": f"frame/timeout: {exc}"}
        ok_ants = [antenna for antenna, result in results.items() if result["ok"]]
        checkpoint(
            log,
            "lock_try_all_antennas",
            ok=ok_ants,
            fail=[antenna for antenna in results if antenna not in ok_ants],
            mask=f"0x{mask:04X}",
            action=f"0x{action:04X}",
        )
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
        checkpoint(
            log, "health_check", ok=report["ok"], antennas=report.get("antennas_connected", "-")
        )
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
    reader_cfg = cfg.get("reader", {}) or {}
    return SIM7200Reader(
        t,
        boot_timeout_ms=int(reader_cfg.get("boot_timeout_ms", 3000)),
        response_timeout_ms=int(reader_cfg["response_timeout_ms"])
        if reader_cfg.get("response_timeout_ms") is not None
        else None,
        max_power_dbm=int(reader_cfg.get("max_power_dbm", 30)),
    )
