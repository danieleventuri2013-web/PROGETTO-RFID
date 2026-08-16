"""Client Python per il processo service RFID JSONL.

Il framework usa questa classe come ``RFIDBackend`` senza conoscere JSON-RPC,
seriale, TCP o protocollo Silion. Un solo subprocess possiede l'hardware; la
stessa istanza client puo' essere passata alla GUI con ownership condivisa.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .rpc import RPC_PROTOCOL_VERSION
from .service import (
    SERVICE_API_VERSION,
    AntennaDiagnosticsRequest,
    EpcGenerationRequest,
    EventRequest,
    Gen2Settings,
    InventoryRequest,
    LockRequest,
    ReaderSettings,
    ReaderTuning,
    ReadRequest,
    ServiceResponse,
    ServiceState,
    WriteEpcRequest,
    WriteRequest,
)

log = logging.getLogger("rfid_silion.client")


class RFIDClientError(RuntimeError):
    """Errore del canale verso il processo service."""


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex().upper()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return value


def _params(value: Any) -> dict[str, Any]:
    converted = _json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("i parametri RPC devono essere un mapping o una dataclass")
    return converted


class RFIDProcessClient:
    """Backend sincrono che controlla un host JSONL in un subprocess.

    Le richieste sono serializzate: il protocollo JSON Lines mantiene una sola
    risposta in volo. Due thread drenano stdout/stderr per evitare deadlock; le
    attese hanno sempre un timeout configurabile.
    """

    def __init__(
        self,
        config_path: str | Path,
        *,
        command: Sequence[str] | None = None,
        cwd: str | Path | None = None,
        timeout_s: float = 30.0,
        stderr_history_size: int = 200,
    ):
        if timeout_s <= 0:
            raise ValueError("timeout_s deve essere maggiore di zero")
        if stderr_history_size < 1:
            raise ValueError("stderr_history_size deve essere almeno 1")
        self.config_path = Path(config_path).resolve()
        self.command = (
            tuple(command)
            if command is not None
            else (
                sys.executable,
                "-m",
                "rfid_silion.service_host",
                "--config",
                str(self.config_path),
            )
        )
        if not self.command:
            raise ValueError("command non puo' essere vuoto")
        self.cwd = Path(cwd).resolve() if cwd is not None else None
        self.timeout_s = float(timeout_s)
        self._process: subprocess.Popen[str] | None = None
        self._state = ServiceState.STOPPED
        self._request_sequence = 0
        self._request_lock = threading.RLock()
        self._stdout_queue: queue.Queue[str | None] = queue.Queue()
        self._stderr_history: deque[str] = deque(maxlen=stderr_history_size)
        self._stderr_lock = threading.Lock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None

    @property
    def state(self) -> ServiceState:
        with self._request_lock:
            if self._process is not None and self._process.poll() is not None:
                if self._state is not ServiceState.STOPPED:
                    self._state = ServiceState.ERROR
            return self._state

    @property
    def ready(self) -> bool:
        return self.state is ServiceState.READY

    @property
    def is_open(self) -> bool:
        with self._request_lock:
            return self._process is not None and self._process.poll() is None

    @property
    def pid(self) -> int | None:
        with self._request_lock:
            return self._process.pid if self._process is not None else None

    def open(self) -> dict[str, Any]:
        """Avvia il solo processo e negozia l'API, senza fare boot hardware."""
        with self._request_lock:
            if self._process is not None and self._process.poll() is None:
                return self._describe_locked()
            self._stdout_queue = queue.Queue()
            with self._stderr_lock:
                self._stderr_history.clear()
            try:
                self._process = subprocess.Popen(
                    self.command,
                    cwd=str(self.cwd) if self.cwd is not None else None,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except OSError as exc:
                self._state = ServiceState.ERROR
                raise RFIDClientError(f"impossibile avviare il service: {exc}") from exc
            self._start_drain_threads_locked()
            try:
                return self._describe_locked()
            except Exception:
                self._terminate_locked()
                self._state = ServiceState.ERROR
                raise

    def describe(self) -> dict[str, Any]:
        with self._request_lock:
            self._ensure_open_locked()
            return self._describe_locked()

    def _describe_locked(self) -> dict[str, Any]:
        envelope = self._rpc_locked("rfid.describe", {}, include_api_version=False)
        response = self._response_from_envelope("describe", envelope)
        if not response.ok:
            raise RFIDClientError(str(response.error))
        data = dict(response.data)
        if data.get("service_api_version") != SERVICE_API_VERSION:
            raise RFIDClientError(
                "API service incompatibile: "
                f"{data.get('service_api_version')} != {SERVICE_API_VERSION}"
            )
        return data

    def start(self) -> ServiceResponse:
        return self._call_service("rfid.start")

    def stop(self) -> ServiceResponse:
        with self._request_lock:
            if self._process is None or self._process.poll() is not None:
                self._state = ServiceState.STOPPED
                return ServiceResponse(
                    operation="stop",
                    ok=True,
                    state=ServiceState.STOPPED,
                    data={"already_stopped": True, "process_open": False},
                )
        return self._call_service("rfid.stop")

    def replace_config(self, config: Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.replace_config", {"config": _json_value(config)})

    def configure(self, settings: ReaderSettings | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.configure", _params(settings))

    def generate_epc(
        self,
        request: EpcGenerationRequest | Mapping[str, Any] | None = None,
    ) -> ServiceResponse:
        return self._call_service(
            "rfid.generate_epc",
            _params(request or EpcGenerationRequest()),
        )

    def inventory(self, request: InventoryRequest | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.inventory", _params(request))

    def read(self, request: ReadRequest | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.read", _params(request))

    def write(self, request: WriteRequest | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.write", _params(request))

    def write_epc(self, request: WriteEpcRequest | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.write_epc", _params(request))

    def lock(self, request: LockRequest | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.lock", _params(request))

    def configure_gen2(self, settings: Gen2Settings | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.configure_gen2", _params(settings))

    def tune_reader(self, settings: ReaderTuning | Mapping[str, Any]) -> ServiceResponse:
        return self._call_service("rfid.tune_reader", _params(settings))

    def antenna_diagnostics(
        self, request: AntennaDiagnosticsRequest | Mapping[str, Any]
    ) -> ServiceResponse:
        return self._call_service("rfid.antenna_diagnostics", _params(request))

    def verify(
        self,
        request: ReadRequest | Mapping[str, Any],
        expected_data_hex: str,
    ) -> ServiceResponse:
        return self._call_service(
            "rfid.verify",
            {"request": _params(request), "expected_data_hex": expected_data_hex},
        )

    def health(self, check_antennas: bool = True) -> ServiceResponse:
        return self._call_service("rfid.health", {"check_antennas": check_antennas})

    def snapshot(self) -> dict[str, Any]:
        response = self._call_service("rfid.snapshot")
        if response.ok:
            return dict(response.data["snapshot"])
        return {
            "api_version": SERVICE_API_VERSION,
            "state": self.state.value,
            "ready": False,
            "client_error": dict(response.error or {}),
        }

    def events(
        self,
        request: EventRequest | Mapping[str, Any] | None = None,
    ) -> ServiceResponse:
        return self._call_service("rfid.events", _params(request or EventRequest()))

    def recent_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        response = self.events(EventRequest(after_sequence=after_sequence))
        if not response.ok:
            message = (
                response.error.get("message", "lettura eventi fallita")
                if response.error
                else "lettura eventi fallita"
            )
            raise RFIDClientError(str(message))
        return [dict(event) for event in response.data.get("events", [])]

    def stderr_tail(self) -> list[str]:
        """Ultime righe stderr del processo, utili per assistenza e report."""
        with self._stderr_lock:
            return list(self._stderr_history)

    def close(self) -> None:
        """Ferma hardware e subprocess; sicuro da richiamare più volte."""
        with self._request_lock:
            process = self._process
            if process is None:
                self._state = ServiceState.STOPPED
                return
            if process.poll() is None and self._state is not ServiceState.STOPPED:
                try:
                    envelope = self._rpc_locked("rfid.stop", {})
                    self._response_from_envelope("stop", envelope)
                except RFIDClientError:
                    log.warning("Stop remoto fallito durante close", exc_info=True)
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=self.timeout_s)
            except subprocess.TimeoutExpired:
                self._terminate_locked()
            self._process = None
            self._state = ServiceState.STOPPED
        self._join_drain_threads()

    def _call_service(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> ServiceResponse:
        operation = method.removeprefix("rfid.")
        try:
            with self._request_lock:
                self._ensure_open_locked()
                envelope = self._rpc_locked(method, dict(params or {}))
                return self._response_from_envelope(operation, envelope)
        except (OSError, RFIDClientError, queue.Empty) as exc:
            if not self.is_open:
                self._state = ServiceState.ERROR
            return self._client_failure(operation, exc)

    def _rpc_locked(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        include_api_version: bool = True,
    ) -> dict[str, Any]:
        process = self._process
        if process is None or process.poll() is not None:
            raise RFIDClientError("processo service non attivo")
        if process.stdin is None:
            raise RFIDClientError("stdin del processo service non disponibile")
        self._request_sequence += 1
        request_id = f"python-{self._request_sequence}"
        request: dict[str, Any] = {
            "jsonrpc": RPC_PROTOCOL_VERSION,
            "id": request_id,
            "method": method,
            "params": _json_value(params),
        }
        if include_api_version:
            request["api_version"] = SERVICE_API_VERSION
        try:
            process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
            process.stdin.write("\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RFIDClientError(f"scrittura verso service fallita: {exc}") from exc
        try:
            raw = self._stdout_queue.get(timeout=self.timeout_s)
        except queue.Empty as exc:
            raise RFIDClientError(
                f"timeout risposta service dopo {self.timeout_s:g} secondi"
            ) from exc
        if raw is None:
            raise RFIDClientError(
                f"processo service terminato (exit={process.poll()}): {self.stderr_tail()}"
            )
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RFIDClientError(f"risposta JSON non valida: {raw!r}") from exc
        if not isinstance(envelope, dict) or envelope.get("id") != request_id:
            raise RFIDClientError(f"risposta RPC non correlata alla richiesta {request_id}")
        return envelope

    def _response_from_envelope(
        self,
        operation: str,
        envelope: Mapping[str, Any],
    ) -> ServiceResponse:
        rpc_error = envelope.get("error")
        if isinstance(rpc_error, Mapping):
            return ServiceResponse(
                operation=operation,
                ok=False,
                state=self.state,
                error={
                    "type": "RPCError",
                    "message": str(rpc_error.get("message", "errore RPC")),
                    "code": rpc_error.get("code"),
                    "data": _json_value(rpc_error.get("data")),
                },
            )
        raw = envelope.get("result")
        if not isinstance(raw, Mapping):
            raise RFIDClientError("risposta RPC senza result valido")
        try:
            state = ServiceState(str(raw["state"]))
            response = ServiceResponse(
                operation=str(raw.get("operation", operation)),
                ok=bool(raw["ok"]),
                state=state,
                data=dict(raw.get("data") or {}),
                error=dict(raw["error"]) if isinstance(raw.get("error"), Mapping) else None,
                timestamp=str(raw["timestamp"]),
                api_version=str(raw["api_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RFIDClientError(f"ServiceResponse remoto non valido: {raw}") from exc
        if response.api_version != SERVICE_API_VERSION:
            raise RFIDClientError(f"versione risposta incompatibile: {response.api_version}")
        self._state = response.state
        return response

    def _client_failure(self, operation: str, exc: Exception) -> ServiceResponse:
        return ServiceResponse(
            operation=operation,
            ok=False,
            state=self.state,
            error={"type": type(exc).__name__, "message": str(exc)},
        )

    def _ensure_open_locked(self) -> None:
        if self._process is None or self._process.poll() is not None:
            self.open()

    def _start_drain_threads_locked(self) -> None:
        process = self._process
        if process is None or process.stdout is None or process.stderr is None:
            raise RFIDClientError("pipe del processo service non disponibili")

        def drain_stdout() -> None:
            try:
                for line in process.stdout:
                    stripped = line.strip()
                    if stripped:
                        self._stdout_queue.put(stripped)
            finally:
                self._stdout_queue.put(None)

        def drain_stderr() -> None:
            for line in process.stderr:
                stripped = line.rstrip()
                if stripped:
                    with self._stderr_lock:
                        self._stderr_history.append(stripped)
                    log.debug("service stderr: %s", stripped)

        self._stdout_thread = threading.Thread(
            target=drain_stdout,
            name="rfid-service-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=drain_stderr,
            name="rfid-service-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _terminate_locked(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=min(self.timeout_s, 5.0))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)

    def _join_drain_threads(self) -> None:
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=1.0)
        self._stdout_thread = None
        self._stderr_thread = None

    def __enter__(self) -> "RFIDProcessClient":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
