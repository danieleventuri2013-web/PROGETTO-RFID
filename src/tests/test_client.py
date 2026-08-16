"""Test del client Python verso il processo service, senza hardware."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion import (
    SERVICE_API_VERSION,
    EpcGenerationRequest,
    EventRequest,
    ReaderSettings,
    RFIDBackend,
    RFIDClientError,
    RFIDProcessClient,
    ServiceState,
    WriteEpcRequest,
)

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "src" / "app" / "config.yaml"


def _service_command() -> list[str]:
    return [
        sys.executable,
        str(ROOT / "run.py"),
        "service",
        "--config",
        str(CONFIG_PATH),
    ]


def test_process_client_discovery_contract_and_clean_close():
    client = RFIDProcessClient(
        CONFIG_PATH,
        command=_service_command(),
        cwd=ROOT,
        timeout_s=20,
    )
    assert isinstance(client, RFIDBackend)
    try:
        description = client.open()
        assert client.is_open is True
        assert client.pid is not None
        assert description["service_api_version"] == SERVICE_API_VERSION
        assert "rfid.inventory" in description["methods"]
        assert "rfid.lock" in description["methods"]

        snapshot = client.snapshot()
        assert snapshot["state"] == "stopped"
        assert snapshot["ready"] is False
        event_batch = client.events(EventRequest())
        assert event_batch.ok is True
        assert event_batch.data["events"] == []
        assert event_batch.data["history_truncated"] is False
        assert client.recent_events() == []

        generated = client.generate_epc(EpcGenerationRequest(byte_length=12))
        assert generated.ok is True
        assert len(generated.data["epc"]) == 24
        epc_not_ready = client.write_epc(
            WriteEpcRequest(
                new_epc=generated.data["epc"],
                expected_epc="E2000017221101441890ABCD",
            )
        )
        assert epc_not_ready.ok is False
        assert epc_not_ready.state is ServiceState.STOPPED

        replaced = client.replace_config({"serial": {"port": "PORTA_TEST"}})
        assert replaced.ok is True
        assert replaced.state is ServiceState.STOPPED

        not_ready = client.configure(ReaderSettings())
        assert not_ready.ok is False
        assert not_ready.state is ServiceState.STOPPED

        stopped = client.stop()
        assert stopped.ok is True
        assert stopped.state is ServiceState.STOPPED
    finally:
        client.close()

    assert client.is_open is False
    assert client.state is ServiceState.STOPPED


def test_process_client_reports_process_start_failure_and_can_close():
    client = RFIDProcessClient(
        CONFIG_PATH,
        command=[sys.executable, "-c", "raise SystemExit(7)"],
        timeout_s=5,
    )
    try:
        try:
            client.open()
        except RFIDClientError as exc:
            assert "terminato" in str(exc)
        else:
            raise AssertionError("RFIDClientError atteso")
        assert client.is_open is False
        assert client.state is ServiceState.ERROR
    finally:
        client.close()

    assert client.state is ServiceState.STOPPED


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
