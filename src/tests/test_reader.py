"""Unit test del reader con trasporto finto (senza hardware).

Copre: ciclo comando/risposta, resync sull'header 0xFF, timeout, contatori
diagnostici (DiagCounters), validazione input dei comandi e health_check().
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion import protocol as P
from rfid_silion.reader import SIM7200Reader
from rfid_silion.transports import Transport
from rfid_silion.errors import SilionError, NoTagError


def make_response(cmd: int, status: int = 0x0000, data: bytes = b"") -> bytes:
    """Costruisce un frame di risposta Reader->Host valido (con CRC)."""
    body = bytes([len(data), cmd, (status >> 8) & 0xFF, status & 0xFF]) + data
    crc = P.crc16(body)
    return bytes([P.HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


class FakeTransport(Transport):
    """Trasporto in memoria: serve i byte precaricati in `rx`, registra `tx`."""

    def __init__(self, rx: bytes = b""):
        self.rx = bytearray(rx)
        self.tx = bytearray()

    def open(self):
        pass

    def close(self):
        pass

    def write(self, data: bytes):
        self.tx.extend(data)

    def read(self, n: int) -> bytes:
        chunk = bytes(self.rx[:n])
        del self.rx[:n]
        return chunk          # b"" quando esaurito = timeout

    def flush_input(self):
        pass                  # non svuota rx: i test precaricano la risposta


def test_command_roundtrip_and_counters():
    """get_antenna_connection: TX corretto, parsing risposta, contatori ok."""
    data = bytes([0x05, 0x01, 0x01, 0x02, 0x01, 0x03, 0x00])  # ant1 e ant2 connesse
    t = FakeTransport(make_response(P.CMD_GET_ANTENNA_PORTS, 0x0000, data))
    r = SIM7200Reader(t)
    connected = r.get_antenna_connection()
    assert connected == [1, 2], connected
    assert bytes(t.tx) == P.build_packet(P.CMD_GET_ANTENNA_PORTS, bytes([0x05]))
    assert r.diag.commands_sent == 1 and r.diag.responses_ok == 1
    assert r.diag.timeouts == 0 and r.diag.frame_errors == 0


def test_resync_skips_noise_before_header():
    """Byte spuri prima dell'header 0xFF: scartati e conteggiati, frame ok."""
    frame = make_response(P.CMD_SET_CURRENT_REGION)
    t = FakeTransport(b"\x00\xAB\xCD" + frame)
    r = SIM7200Reader(t)
    r.set_region(0x08)   # non deve sollevare
    assert r.diag.bytes_discarded == 3
    assert r.diag.responses_ok == 1


def test_timeout_raises_and_counts():
    """Nessuna risposta -> SilionTimeoutError e contatore timeouts."""
    r = SIM7200Reader(FakeTransport(b""))
    try:
        r.set_region(0x08)
        assert False, "atteso SilionTimeoutError"
    except P.SilionTimeoutError:
        pass
    assert r.diag.timeouts == 1
    assert r.diag.last_error is not None


def test_status_error_counted():
    """Status 0x0101 (bootloader): eccezione SilionError e contatore."""
    t = FakeTransport(make_response(P.CMD_SET_CURRENT_REGION, 0x0101))
    r = SIM7200Reader(t)
    try:
        r.set_region(0x08)
        assert False, "atteso SilionError"
    except SilionError as e:
        assert e.status == 0x0101
    assert r.diag.status_errors == 1 and r.diag.no_tag_events == 0


def test_no_tag_counted_separately():
    """Status 0x0400 (no tag): NoTagError, conteggiato come evento non-guasto."""
    t = FakeTransport(make_response(P.CMD_SYNCHRONOUS_INVENTORY, 0x0400))
    r = SIM7200Reader(t)
    try:
        r.sync_inventory(timeout_ms=100)
        assert False, "atteso NoTagError"
    except NoTagError:
        pass
    assert r.diag.no_tag_events == 1 and r.diag.status_errors == 0


def test_cmd_mismatch_is_frame_error():
    """Risposta con cmd diverso da quello inviato -> SilionFrameError."""
    t = FakeTransport(make_response(P.CMD_BOOT_FIRMWARE, 0x0000, bytes(20)))
    r = SIM7200Reader(t)
    try:
        r.set_region(0x08)
        assert False, "atteso SilionFrameError"
    except P.SilionTimeoutError:
        assert False, "cmd mismatch, non timeout"
    except P.SilionFrameError:
        pass
    assert r.diag.frame_errors == 1


def test_validation_power_range():
    """Potenze fuori range o antenna inesistente -> ValueError (prima dell'I/O)."""
    r = SIM7200Reader(FakeTransport())
    for bad in [(1, 0, 2000), (1, 2000, 4000), (5, 2000, 2000)]:
        try:
            r.set_antennas_power([bad])
            assert False, f"atteso ValueError per {bad}"
        except ValueError:
            pass
    try:
        r.set_antennas_power([])
        assert False, "atteso ValueError per lista vuota"
    except ValueError:
        pass


def test_validation_read_write_params():
    """Parametri read/write non validi -> ValueError (prima dell'I/O)."""
    r = SIM7200Reader(FakeTransport())
    cases = [
        lambda: r.read_tag_data(P.BANK_USER, 0, 0),          # word_count 0
        lambda: r.read_tag_data(P.BANK_USER, 0, 97),         # > 96 word
        lambda: r.read_tag_data(0x07, 0, 2),                 # banca inesistente
        lambda: r.write_tag_data(P.BANK_USER, 0, b"\x01"),   # lunghezza dispari
        lambda: r.write_tag_data(P.BANK_USER, 0, b""),       # vuoto
        lambda: r.write_tag_data(P.BANK_USER, 0, b"\x00" * 66),  # > 64 byte
        lambda: r.write_tag_epc(b"\x01\x02\x03"),            # EPC dispari
        lambda: r.read_tag_data(P.BANK_USER, 0, 2, b"\x00"),  # pwd != 4 byte
        lambda: r.set_antenna_for_access(0, 1),              # antenna 0
        lambda: r.sync_inventory(timeout_ms=0),              # timeout 0
    ]
    for i, fn in enumerate(cases):
        try:
            fn()
            assert False, f"case {i}: atteso ValueError"
        except ValueError:
            pass


def test_boot_short_response_raises():
    """Risposta di boot troppo corta -> SilionFrameError esplicito."""
    t = FakeTransport(make_response(P.CMD_BOOT_FIRMWARE, 0x0000, bytes(8)))
    r = SIM7200Reader(t)
    try:
        r.boot_firmware()
        assert False, "atteso SilionFrameError"
    except P.SilionFrameError:
        pass
    assert r.firmware_info is None


def test_health_check_report():
    """health_check: report con trasporto, antenne connesse e contatori."""
    data = bytes([0x05, 0x01, 0x01, 0x02, 0x00, 0x03, 0x00])  # solo ant1
    t = FakeTransport(make_response(P.CMD_GET_ANTENNA_PORTS, 0x0000, data))
    r = SIM7200Reader(t)
    rep = r.health_check()
    assert rep["ok"] is True
    assert rep["antennas_connected"] == [1]
    assert rep["booted"] is False and rep["firmware_info"] is None
    assert rep["counters"]["commands_sent"] == 1
    assert rep["transport"] == "FakeTransport"


def test_health_check_reports_failure():
    """health_check con lettore muto: ok=False e errore antenne registrato."""
    r = SIM7200Reader(FakeTransport(b""))
    rep = r.health_check()
    assert rep["ok"] is False
    assert "antennas_error" in rep
    assert rep["counters"]["timeouts"] == 1


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
