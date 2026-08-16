"""Test dei trasporti seriale e TCP senza hardware reale."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import patch

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion.transports import (
    SerialTransport,
    TcpTransport,
    TransportDisconnectedError,
    TransportError,
    TransportNotOpenError,
)


class FakeSocket:
    """Socket minimale programmabile per verificare I/O e timeout."""

    def __init__(self, reads: list[bytes | BaseException] | None = None):
        self.reads = list(reads or [])
        self.sent: list[bytes] = []
        self.timeouts: list[float] = []
        self.closed = False
        self.write_error: OSError | None = None

    def recv(self, _n: int) -> bytes:
        if not self.reads:
            raise BlockingIOError()
        result = self.reads.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def sendall(self, data: bytes) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.sent.append(data)

    def settimeout(self, value: float) -> None:
        self.timeouts.append(value)

    def close(self) -> None:
        self.closed = True


class FakeSerial:
    """Porta seriale minimale compatibile con SerialTransport."""

    def __init__(self):
        self.is_open = True
        self.timeout = 2.0
        self.written = b""
        self.flushed = False
        self.closed = False
        self.read_error = False

    def write(self, data: bytes) -> None:
        self.written += data

    def read(self, n: int) -> bytes:
        if self.read_error:
            raise serial.SerialException("guasto")
        return b"ok"[:n]

    def reset_input_buffer(self) -> None:
        self.flushed = True

    def close(self) -> None:
        self.closed = True
        self.is_open = False


def test_tcp_io_timeout_flush_and_close():
    sock = FakeSocket([b"abc", socket.timeout(), b"stale", BlockingIOError()])
    transport = TcpTransport("127.0.0.1", 8080, timeout_s=2.0)
    transport._sock = sock

    transport.write(b"cmd")
    assert sock.sent == [b"cmd"]
    assert transport.read(3) == b"abc"
    assert transport.read(1) == b""

    transport.flush_input()
    assert sock.timeouts[-2:] == [0, 2.0]

    with transport.temporary_timeout(5.0):
        assert transport.timeout_s == 5.0
    assert transport.timeout_s == 2.0
    assert transport.describe() == "tcp 127.0.0.1:8080"

    transport.close()
    assert sock.closed is True
    assert transport._sock is None


def test_tcp_distinguishes_disconnect_and_write_error():
    transport = TcpTransport("reader.local")
    transport._sock = FakeSocket([b""])
    try:
        transport.read(1)
        raise AssertionError("chiusura TCP accettata come timeout")
    except TransportDisconnectedError:
        pass

    transport._sock = FakeSocket([b""])
    try:
        transport.flush_input()
        raise AssertionError("chiusura TCP ignorata durante il flush")
    except TransportDisconnectedError:
        pass

    failing = FakeSocket()
    failing.write_error = OSError("connessione persa")
    transport._sock = failing
    try:
        transport.write(b"x")
        raise AssertionError("errore di scrittura TCP non propagato")
    except TransportDisconnectedError:
        pass


def test_serial_io_timeout_close_and_errors():
    port = FakeSerial()
    transport = SerialTransport("COM_TEST")
    transport._ser = port

    transport.write(b"cmd")
    assert port.written == b"cmd"
    assert transport.read(2) == b"ok"
    transport.flush_input()
    assert port.flushed is True

    transport.set_timeout(0.5)
    assert transport.timeout_s == 0.5
    assert port.timeout == 0.5
    assert transport.describe() == "serial COM_TEST@115200"

    port.read_error = True
    try:
        transport.read(1)
        raise AssertionError("SerialException non normalizzata")
    except TransportError:
        pass

    transport.close()
    assert port.closed is True


def test_closed_transports_raise_clear_error():
    for transport, operation in (
        (TcpTransport("reader.local"), lambda item: item.read(1)),
        (SerialTransport("COM_TEST"), lambda item: item.write(b"x")),
    ):
        try:
            operation(transport)
            raise AssertionError("operazione consentita su trasporto chiuso")
        except TransportNotOpenError:
            pass

    with patch(
        "rfid_silion.transports.socket.create_connection",
        side_effect=OSError("host irraggiungibile"),
    ):
        try:
            TcpTransport("reader.local").open()
            raise AssertionError("errore apertura TCP non normalizzato")
        except TransportDisconnectedError:
            pass

    with patch(
        "rfid_silion.transports.serial.Serial",
        side_effect=serial.SerialException("porta assente"),
    ):
        try:
            SerialTransport("COM_TEST").open()
            raise AssertionError("errore apertura seriale non normalizzato")
        except TransportError:
            pass


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
