"""Test del confine service headless, senza GUI e senza hardware."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion import protocol as P
from rfid_silion.service import (
    AntennaPower,
    EpcGenerationRequest,
    EventRequest,
    InventoryRequest,
    MemoryBank,
    ReaderSettings,
    ReadRequest,
    RFIDBackend,
    RFIDService,
    RFIDServiceBinding,
    ServiceState,
    UnsafeWriteError,
    WriteEpcRequest,
    WriteRequest,
    barcode_to_epc_hex,
    discover_serial_ports,
    epc_hex_to_barcode,
)
from rfid_silion.tags import Tag


class FakeDiag:
    def snapshot(self) -> dict:
        return {"commands_sent": 7}


class FakeReader:
    def __init__(self, *, boot_error: Exception | None = None):
        self.boot_error = boot_error
        self.opened = False
        self.closed = False
        self.firmware_info = None
        self.diag = FakeDiag()
        self.region = None
        self.powers = []
        self.inventory_antennas = []
        self.inventory_set_calls = 0
        self.tags = [
            Tag(
                epc="E2000017221101441890ABCD",
                pc=0x3000,
                crc=0x1234,
                read_count=2,
                rssi=-42,
                antenna_id=1,
                embedded_data=b"\x12\x34",
            )
        ]
        self.write_calls = []
        self.write_epc_calls = []
        self.lock_calls = []
        self.read_ok = True
        self.read_selects = []
        self.write_ok = True

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def boot_firmware(self) -> dict:
        if self.boot_error is not None:
            raise self.boot_error
        self.firmware_info = {
            "firmware_version": "01020304",
            "hardware_version": "05060708",
        }
        return self.firmware_info

    def health_check(self, check_antennas: bool = True) -> dict:
        return {
            "ok": True,
            "transport": "fake",
            "antennas_connected": [1, 2, 3] if check_antennas else [],
            "counters": self.diag.snapshot(),
        }

    def set_region(self, region: int) -> None:
        self.region = region

    def set_antennas_power(self, powers: list[tuple[int, int, int]]) -> None:
        self.powers = list(powers)

    def set_antennas_for_inventory(self, pairs: list[tuple[int, int]]) -> None:
        self.inventory_set_calls += 1
        self.inventory_antennas = list(pairs)

    def inventory(self, timeout_ms: int, metadata_flags: int) -> list[Tag]:
        assert timeout_ms > 0
        assert metadata_flags >= 0
        return list(self.tags)

    def read_try_all_antennas(
        self, antennas, bank, address, words, password, timeout_ms, select_epc=None
    ):
        assert bank == P.BANK_USER
        assert address == 0 and words == 2 and len(password) == 4 and timeout_ms == 1000
        self.read_selects.append(select_epc)
        if self.read_ok:
            return {antenna: {"ok": True, "data": b"\x12\x34"} for antenna in antennas}
        return {antenna: {"ok": False, "error": "nessun tag"} for antenna in antennas}

    def write_try_all_antennas(self, antennas, bank, address, data, password, timeout_ms):
        self.write_calls.append((antennas, bank, address, data, password, timeout_ms))
        if self.write_ok:
            return {antenna: {"ok": True} for antenna in antennas}
        return {antenna: {"ok": False, "error": "write fallita"} for antenna in antennas}

    def lock_try_all_antennas(self, antennas, mask, action, password, timeout_ms):
        self.lock_calls.append((tuple(antennas), mask, action, password, timeout_ms))
        if not self.write_ok:
            return {antenna: {"ok": False, "error": "lock fallito"} for antenna in antennas}
        return {antennas[0]: {"ok": True}}

    def write_epc_try_all_antennas(
        self, antennas, epc, password, timeout_ms, select_epc=None
    ):
        self.write_epc_calls.append((antennas, epc, password, timeout_ms, select_epc))
        if not self.write_ok:
            return {antenna: {"ok": False, "error": "write EPC fallita"} for antenna in antennas}
        self.tags[0].epc = epc.hex().upper()
        return {antennas[0]: {"ok": True}}


def make_service(reader: FakeReader | None = None) -> tuple[RFIDService, FakeReader]:
    fake = reader or FakeReader()
    service = RFIDService({"serial": {"port": "FAKE"}}, reader_factory=lambda _cfg: fake)
    return service, fake


def test_lifecycle_events_and_json_contract():
    service, reader = make_service()
    observed = []
    unsubscribe = service.subscribe(observed.append)

    started = service.start()
    assert started.ok is True
    assert started.state is ServiceState.READY
    assert started.data["transport"] == "fake"
    json.dumps(started.to_dict())
    assert service.start().data["already_started"] is True
    assert service.snapshot()["diagnostics"] == {"commands_sent": 7}

    sequences = [event.sequence for event in observed]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert any(event.kind == "state.changed" for event in observed)
    unsubscribe()

    stopped = service.stop()
    assert stopped.ok is True and service.state is ServiceState.STOPPED
    assert reader.closed is True
    json.dumps(service.recent_events())


def test_serial_port_discovery_is_json_safe_and_sorted():
    from types import SimpleNamespace

    from serial.tools import list_ports

    original = list_ports.comports
    list_ports.comports = lambda: [
        SimpleNamespace(device="COM9", description="USB RFID", hwid="ID9"),
        SimpleNamespace(device="COM2", description=None, hwid=None),
    ]
    try:
        ports = discover_serial_ports()
    finally:
        list_ports.comports = original
    assert [item["device"] for item in ports] == ["COM2", "COM9"]
    assert ports[0]["description"] == ""
    json.dumps(ports)


def test_event_polling_reports_retention_gaps_without_creating_events():
    reader = FakeReader()
    service = RFIDService(
        {"serial": {"port": "FAKE"}},
        reader_factory=lambda _cfg: reader,
        event_history_size=3,
    )
    assert service.start().ok
    assert service.inventory(InventoryRequest(antennas=(1,))).ok

    last_before_poll = service.snapshot()["last_event_sequence"]
    batch = service.events(EventRequest(after_sequence=0))
    assert batch.ok is True
    assert batch.data["history_truncated"] is True
    assert batch.data["first_available_sequence"] == batch.data["events"][0]["sequence"]
    assert batch.data["last_sequence"] == last_before_poll
    assert batch.data["next_after_sequence"] == batch.data["events"][-1]["sequence"]
    assert service.snapshot()["last_event_sequence"] == last_before_poll
    json.dumps(batch.to_dict())

    next_batch = service.events(EventRequest(batch.data["next_after_sequence"]))
    assert next_batch.ok is True
    assert next_batch.data["events"] == []
    assert next_batch.data["history_truncated"] is False

    invalid = service.events({"after_sequence": -1})
    assert invalid.ok is False
    assert invalid.error["type"] == "ValueError"


def test_configure_and_inventory_are_transport_agnostic():
    service, reader = make_service()
    assert service.start().ok
    settings = ReaderSettings(
        region=0x08,
        powers=(AntennaPower(1, 1800, 1900), AntennaPower(2, 2000, 2000)),
    )
    assert service.configure(settings).ok
    assert reader.region == 0x08
    assert reader.powers == [(1, 1800, 1900), (2, 2000, 2000)]

    request = InventoryRequest(antennas=(1, 2), timeout_ms=750)
    first = service.inventory(request)
    second = service.inventory(request)
    assert first.ok and second.ok
    assert reader.inventory_set_calls == 1
    assert first.data["tags"][0]["embedded_data"] == "1234"
    assert first.data["unique_epcs"] == [reader.tags[0].epc]
    assert service.snapshot()["observed_epcs"] == [reader.tags[0].epc]
    assert any(event["kind"] == "inventory.tags" for event in service.recent_events())


def test_read_verify_and_guarded_write():
    service, reader = make_service()
    assert service.start().ok
    read_request = ReadRequest(bank=MemoryBank.USER, address=0, word_count=2, antennas=(1, 2))
    read_response = service.read(read_request)
    assert read_response.ok
    assert read_response.data["results"]["1"]["data"] == "1234"
    assert service.verify(read_request, "1234").ok

    write_request = WriteRequest(
        bank=MemoryBank.USER,
        address=0,
        data_hex="1234",
        expected_epc=reader.tags[0].epc.lower(),
        antennas=(1, 2),
    )
    blocked = service.write(write_request)
    assert blocked.ok is False
    assert blocked.error["type"] == UnsafeWriteError.__name__

    assert service.inventory(InventoryRequest(antennas=(1, 2))).ok
    written = service.write(write_request)
    assert written.ok is True
    assert reader.write_calls[0][3] == b"\x12\x34"


def test_generate_and_write_epc_are_service_operations_with_fresh_target_guard():
    service, reader = make_service()
    first = service.generate_epc(EpcGenerationRequest(byte_length=12))
    second = service.generate_epc({"byte_length": 12})
    assert first.ok and second.ok
    assert len(first.data["epc"]) == 24
    assert first.data["epc"] != second.data["epc"]
    assert first.data["random_bits"] == 96
    assert first.data["uniqueness_scope"] == "service_session"
    assert service.snapshot()["generated_epc_candidates"] == 2

    assert service.start().ok
    previous_epc = reader.tags[0].epc
    target_epc = first.data["epc"]
    request = WriteEpcRequest(
        new_epc=target_epc,
        expected_epc=previous_epc,
        antennas=(1, 2, 3),
    )
    blocked = service.write_epc(request)
    assert blocked.ok is False
    assert blocked.error["type"] == UnsafeWriteError.__name__

    assert service.inventory(InventoryRequest()).ok
    changed = service.write_epc(request)
    assert changed.ok is True
    assert changed.data["previous_epc"] == previous_epc
    assert changed.data["new_epc"] == target_epc
    assert changed.data["success_antenna"] == 1
    assert changed.data["verification_required"] is True
    assert reader.write_epc_calls[0][1].hex().upper() == target_epc
    # Il filtro Select porta l'EPC atteso fino al comando: la guardia lo pretende
    # gia' come unico osservato, quindi non c'e' ragione di colpire «il primo
    # tag che risponde».
    assert reader.write_epc_calls[0][4].hex().upper() == previous_epc
    assert service.snapshot()["observed_epcs"] == []
    assert any(event["kind"] == "tag.epc.changed" for event in service.recent_events())

    stale_retry = service.write_epc(request)
    assert stale_retry.ok is False
    verified = service.inventory(InventoryRequest())
    assert verified.ok is True
    assert verified.data["unique_epcs"] == [target_epc]


def test_barcode_epc_codec_is_fixed_96_bit_and_reversible():
    epc = barcode_to_epc_hex("CPOE932-2")
    assert epc == "43504F453933322D32000000"
    assert len(epc) == 24
    assert epc_hex_to_barcode(epc) == "CPOE932-2"
    assert epc_hex_to_barcode(barcode_to_epc_hex("ABCDEFGHIJKL")) == "ABCDEFGHIJKL"


def test_barcode_epc_codec_rejects_lossy_or_ambiguous_values():
    invalid_encode = ("", "ABCDEFGHIJKLM", "caffè", "A\x00B")
    for value in invalid_encode:
        try:
            barcode_to_epc_hex(value)
            raise AssertionError(f"barcode non valido accettato: {value!r}")
        except ValueError:
            pass
    for epc in ("AA", "41FF" + "00" * 10, "410042" + "00" * 9):
        try:
            epc_hex_to_barcode(epc)
            raise AssertionError(f"EPC barcode non valido accettato: {epc}")
        except ValueError:
            pass


def test_epc_dtos_reject_unsafe_lengths_and_equal_values():
    invalid = [
        lambda: EpcGenerationRequest(byte_length=11),
        lambda: EpcGenerationRequest(byte_length=12, prefix_hex="AA" * 5),
        lambda: WriteEpcRequest(new_epc="AA", expected_epc="BBBB"),
        lambda: WriteEpcRequest(new_epc="AABB", expected_epc="AABB"),
        lambda: WriteEpcRequest(new_epc="GGGG", expected_epc="AABB"),
    ]
    for build in invalid:
        try:
            build()
            raise AssertionError("ValueError atteso")
        except ValueError:
            pass


def test_read_passes_the_select_filter_down_to_the_driver():
    """Senza filtro la lettura colpisce il primo tag che risponde.

    Con piu' tag in campo — una scatola piena — quello e' il modo di attribuire
    il payload al contenitore sbagliato. Il filtro deve arrivare al driver come
    byte, non fermarsi al DTO.
    """
    service, reader = make_service()
    assert service.start().ok

    service.read(ReadRequest(bank=MemoryBank.USER, address=0, word_count=2, antennas=(1,)))
    assert reader.read_selects[-1] is None

    service.read(
        ReadRequest(
            bank=MemoryBank.USER,
            address=0,
            word_count=2,
            antennas=(1,),
            select_epc="0100010000000101032e62f3",
        )
    )
    assert reader.read_selects[-1] == bytes.fromhex("0100010000000101032E62F3")

    # Un EPC non esadecimale e' un errore di chiamata, non una lettura senza filtro.
    rifiutata = service.read(
        ReadRequest(bank=MemoryBank.USER, address=0, word_count=2, select_epc="non-esadecimale")
    )
    assert rifiutata.ok is False


def test_failed_read_and_write_preserve_per_antenna_results():
    service, reader = make_service()
    assert service.start().ok
    read_request = ReadRequest(bank=MemoryBank.USER, address=0, word_count=2, antennas=(1, 2))
    reader.read_ok = False
    failed_read = service.read(read_request)
    assert failed_read.ok is False
    assert set(failed_read.data["results"]) == {"1", "2"}

    reader.read_ok = True
    assert service.inventory(InventoryRequest(antennas=(1, 2))).ok
    reader.write_ok = False
    failed_write = service.write(
        WriteRequest(
            bank=MemoryBank.USER,
            address=0,
            data_hex="1234",
            expected_epc=reader.tags[0].epc,
            antennas=(1, 2),
        )
    )
    assert failed_write.ok is False
    assert set(failed_write.data["results"]) == {"1", "2"}
    json.dumps(failed_write.to_dict())


def test_operations_before_start_return_serializable_errors():
    service, _reader = make_service()
    response = service.health()
    assert response.ok is False
    assert response.state is ServiceState.STOPPED
    assert response.error["type"] == "ServiceStateError"
    json.dumps(response.to_dict())


def test_start_failure_closes_reader_and_enters_error_state():
    reader = FakeReader(boot_error=RuntimeError("boot fallito"))
    service, _reader = make_service(reader)
    response = service.start()
    assert response.ok is False
    assert response.state is ServiceState.ERROR
    assert reader.closed is True
    assert service.snapshot()["ready"] is False
    assert service.stop().ok is True


def test_replace_config_only_when_stopped():
    captured = []
    reader = FakeReader()
    service = RFIDService(
        {"serial": {"port": "OLD"}},
        reader_factory=lambda cfg: captured.append(cfg) or reader,
    )
    assert service.replace_config({"tcp": {"host": "reader.local"}}).ok
    assert service.start().ok
    assert captured == [{"tcp": {"host": "reader.local"}}]
    assert service.replace_config({"serial": {"port": "NEW"}}).ok is False


def test_owned_binding_starts_and_stops_its_backend():
    service, reader = make_service()
    binding = RFIDServiceBinding(service, owns_lifecycle=True)
    assert isinstance(service, RFIDBackend)
    assert binding.attach({"serial": {"port": "OWNED"}}).ok
    assert binding.attached is True
    detached = binding.detach()
    assert detached.ok is True
    assert detached.data["already_stopped"] is False
    assert binding.attached is False
    assert reader.closed is True


def test_shared_binding_never_stops_the_framework_backend():
    service, reader = make_service()
    assert service.start().ok
    binding = RFIDServiceBinding(service, owns_lifecycle=False)
    attached = binding.attach({"serial": {"port": "IGNORED_WHILE_READY"}})
    assert attached.ok is True
    assert attached.data["already_started"] is True

    detached = binding.detach()
    assert detached.ok is True
    assert detached.operation == "detach"
    assert detached.data == {"service_stopped": False, "ownership": "shared"}
    assert service.ready is True
    assert reader.closed is False


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print(f"{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
