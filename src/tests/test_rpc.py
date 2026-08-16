"""Test JSON-RPC/JSONL del service, senza hardware."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion.rpc import (
    API_VERSION_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    RFIDRPCDispatcher,
)
from rfid_silion.service import SERVICE_API_VERSION, MemoryBank
from rfid_silion.service_host import dispatch_json_line, run_jsonl_stream
from tests.test_service import make_service


def request(method: str, params: dict | None = None, request_id=1, **extra) -> dict:
    value = {
        "jsonrpc": "2.0",
        "id": request_id,
        "api_version": SERVICE_API_VERSION,
        "method": method,
        "params": params or {},
    }
    value.update(extra)
    return value


def test_describe_negotiates_versions_and_capabilities():
    service, _reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)

    response = dispatcher.dispatch({"jsonrpc": "2.0", "id": "discovery", "method": "rfid.describe"})
    result = response["result"]
    assert result["ok"] is True
    assert result["data"]["service_api_version"] == SERVICE_API_VERSION
    assert "rfid.inventory" in result["data"]["methods"]
    assert "rfid.generate_epc" in result["data"]["methods"]
    assert "rfid.write_epc" in result["data"]["methods"]
    json.dumps(response)

    mismatch = request("rfid.start", api_version="9.0")
    response = dispatcher.dispatch(mismatch)
    assert response["error"]["code"] == API_VERSION_MISMATCH
    assert response["error"]["data"]["supported"] == SERVICE_API_VERSION


def test_rpc_lifecycle_inventory_snapshot_and_events():
    service, reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)

    started = dispatcher.dispatch(request("rfid.start", request_id="start"))
    assert started["result"]["ok"] is True
    inventory = dispatcher.dispatch(
        request(
            "rfid.inventory",
            {"antennas": [1, 2], "timeout_ms": 800, "metadata_flags": 7},
            request_id="inventory",
        )
    )
    assert inventory["result"]["data"]["unique_epcs"] == [reader.tags[0].epc]

    snapshot = dispatcher.dispatch(request("rfid.snapshot", request_id="snapshot"))
    assert snapshot["result"]["data"]["snapshot"]["ready"] is True
    events = dispatcher.dispatch(request("rfid.events", {"after_sequence": 0}, request_id="events"))
    assert events["result"]["data"]["events"]
    assert events["result"]["data"]["history_truncated"] is False
    assert (
        events["result"]["data"]["next_after_sequence"] == events["result"]["data"]["last_sequence"]
    )
    json.dumps(events)


def test_rpc_read_verify_and_write_use_json_dtos():
    service, reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)
    assert dispatcher.dispatch(request("rfid.start"))["result"]["ok"]
    assert dispatcher.dispatch(request("rfid.inventory", {"antennas": [1, 2]}))["result"]["ok"]

    read_params = {
        "bank": MemoryBank.USER,
        "address": 0,
        "word_count": 2,
        "antennas": [1, 2],
        "access_password_hex": "00000000",
        "timeout_ms": 1000,
    }
    read = dispatcher.dispatch(request("rfid.read", read_params))
    assert read["result"]["data"]["results"]["1"]["data"] == "1234"
    verify = dispatcher.dispatch(
        request(
            "rfid.verify",
            {"request": read_params, "expected_data_hex": "1234"},
        )
    )
    assert verify["result"]["ok"] is True

    write = dispatcher.dispatch(
        request(
            "rfid.write",
            {
                "bank": MemoryBank.USER,
                "address": 0,
                "data_hex": "1234",
                "expected_epc": reader.tags[0].epc,
                "antennas": [1, 2],
            },
        )
    )
    assert write["result"]["ok"] is True

    generated = dispatcher.dispatch(request("rfid.generate_epc", {"byte_length": 12}))
    new_epc = generated["result"]["data"]["epc"]
    changed = dispatcher.dispatch(
        request(
            "rfid.write_epc",
            {
                "new_epc": new_epc,
                "expected_epc": reader.tags[0].epc,
                "antennas": [1, 2, 3],
                "access_password_hex": "00000000",
            },
        )
    )
    assert changed["result"]["ok"] is True
    assert changed["result"]["data"]["new_epc"] == new_epc


def test_rpc_rejects_invalid_requests_methods_and_params():
    service, _reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)

    invalid = dispatcher.dispatch({"jsonrpc": "1.0", "id": 1, "method": "rfid.start"})
    assert invalid["error"]["code"] == INVALID_REQUEST
    missing = dispatcher.dispatch(request("rfid.unknown"))
    assert missing["error"]["code"] == METHOD_NOT_FOUND
    bad_params = dispatcher.dispatch(request("rfid.inventory", {"unknown": True}))
    assert bad_params["error"]["code"] == INVALID_PARAMS
    bad_health = dispatcher.dispatch(request("rfid.health", {"check_antennas": "yes"}))
    assert bad_health["error"]["code"] == INVALID_PARAMS
    bad_events = dispatcher.dispatch(request("rfid.events", {"after_sequence": True}))
    assert bad_events["result"]["ok"] is False
    assert bad_events["result"]["error"]["type"] == "ValueError"


def test_notifications_execute_without_response():
    service, _reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)
    notification = request("rfid.start")
    notification.pop("id")
    assert dispatcher.dispatch(notification) is None
    assert service.ready is True


def test_jsonl_stream_supports_parse_errors_batches_and_notifications():
    service, _reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)
    notification = request("rfid.start")
    notification.pop("id")
    batch = [
        request("rfid.describe", request_id="describe"),
        notification,
        request("rfid.snapshot", request_id="snapshot"),
    ]
    source = io.StringIO("{bad json}\n" + json.dumps(batch) + "\n\n")
    target = io.StringIO()

    assert run_jsonl_stream(source, target, dispatcher) == 0
    lines = [json.loads(line) for line in target.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == PARSE_ERROR
    assert len(lines[1]) == 2
    assert lines[1][0]["id"] == "describe"
    assert lines[1][1]["result"]["data"]["snapshot"]["ready"] is True

    empty_batch = dispatch_json_line("[]", dispatcher)
    assert empty_batch["error"]["code"] == INVALID_REQUEST


def test_hardware_smoke_jsonl_is_valid_read_only_and_complete():
    root = Path(__file__).resolve().parents[2]
    lines = (
        (root / "tools" / "service_hardware_smoke.jsonl").read_text(encoding="utf-8").splitlines()
    )
    requests = [json.loads(line) for line in lines if line.strip()]
    methods = [item["method"] for item in requests]
    assert methods == [
        "rfid.describe",
        "rfid.start",
        "rfid.configure",
        "rfid.health",
        "rfid.inventory",
        "rfid.snapshot",
        "rfid.events",
        "rfid.stop",
    ]
    assert "rfid.write" not in methods

    service, _reader = make_service()
    dispatcher = RFIDRPCDispatcher(service)
    responses = [dispatcher.dispatch(item) for item in requests]
    assert all(response is not None for response in responses)
    assert all(response["result"]["ok"] for response in responses)
    assert responses[-2]["result"]["data"]["history_truncated"] is False
    assert service.state.value == "stopped"
    json.dumps(responses)


def test_service_process_discovery_keeps_stdout_machine_readable():
    root = Path(__file__).resolve().parents[2]
    probe = {"jsonrpc": "2.0", "id": "probe", "method": "rfid.describe"}
    completed = subprocess.run(
        [sys.executable, str(root / "run.py"), "service"],
        input=json.dumps(probe) + "\n",
        capture_output=True,
        text=True,
        timeout=20,
        cwd=root,
        check=False,
    )
    assert completed.returncode == 0
    stdout_lines = completed.stdout.splitlines()
    assert len(stdout_lines) == 1
    response = json.loads(stdout_lines[0])
    assert response["id"] == "probe"
    assert response["result"]["data"]["service_api_version"] == SERVICE_API_VERSION
    assert "Service RFID pronto" not in completed.stdout
    assert "Service RFID pronto" in completed.stderr


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
