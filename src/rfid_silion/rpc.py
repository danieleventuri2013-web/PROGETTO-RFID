"""Adapter JSON-RPC 2.0 per il confine applicativo RFID.

Il dispatcher non esegue I/O e non conosce seriale, TCP o Tkinter. Traduce
richieste JSON in chiamate a :class:`RFIDService`; puo' quindi essere riusato
da un host stdio, HTTP, socket locale o da test in-process.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .service import (
    SERVICE_API_VERSION,
    RFIDService,
    ServiceResponse,
)

log = logging.getLogger("rfid_silion.rpc")

RPC_PROTOCOL_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
API_VERSION_MISMATCH = -32001


class RPCFault(Exception):
    """Errore del livello RPC, distinto dagli errori operativi del service."""

    def __init__(self, code: int, message: str, data: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = dict(data) if data is not None else None


def error_response(
    request_id: Any,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Costruisce una risposta errore JSON-RPC 2.0 serializzabile."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = dict(data)
    return {
        "jsonrpc": RPC_PROTOCOL_VERSION,
        "id": request_id,
        "error": error,
    }


def _api_version_compatible(requested: Any) -> bool:
    """Verifica la compatibilita' fra la versione chiesta dal client e questa.

    Il contratto promette che in 1.x si aggiungono soltanto campi e metodi
    (`docs/CONTRATTO_SERVICE.md`): un client che chiede una minor version
    **precedente** va servito, perche' il service offre tutto quello che il
    client conosce e in piu' qualcosa. Il contrario no: chi chiede una minor
    version successiva potrebbe usare metodi che qui non esistono, e va respinto
    con chiarezza invece di fallire piu' avanti su un metodo sconosciuto.
    """
    if not isinstance(requested, str):
        return False
    try:
        major, minor = (int(parte) for parte in requested.split("."))
        major_supportato, minor_supportato = (
            int(parte) for parte in SERVICE_API_VERSION.split(".")
        )
    except ValueError:
        return False
    return major == major_supportato and minor <= minor_supportato


class RFIDRPCDispatcher:
    """Traduce richieste JSON-RPC nel contratto pubblico di ``RFIDService``."""

    METHODS = (
        "rfid.describe",
        "rfid.start",
        "rfid.stop",
        "rfid.replace_config",
        "rfid.configure",
        "rfid.generate_epc",
        "rfid.inventory",
        "rfid.read",
        "rfid.write",
        "rfid.write_epc",
        "rfid.lock",
        "rfid.configure_gen2",
        "rfid.read_gen2_settings",
        "rfid.tune_reader",
        "rfid.antenna_diagnostics",
        "rfid.verify",
        "rfid.health",
        "rfid.identify",
        "rfid.snapshot",
        "rfid.events",
    )

    def __init__(self, service: RFIDService):
        self.service = service

    def dispatch(self, request: Any) -> dict[str, Any] | None:
        """Gestisce una richiesta; ``None`` indica una notification JSON-RPC."""
        if not isinstance(request, Mapping):
            return error_response(None, INVALID_REQUEST, "richiesta JSON-RPC non valida")

        notification = "id" not in request
        request_id = request.get("id")
        try:
            self._validate_request(request)
            method = str(request["method"])
            params = request.get("params", {})
            if not isinstance(params, Mapping):
                raise RPCFault(INVALID_PARAMS, "params deve essere un oggetto JSON")
            if method != "rfid.describe":
                requested_api = request.get("api_version")
                if not _api_version_compatible(requested_api):
                    raise RPCFault(
                        API_VERSION_MISMATCH,
                        "versione API service non compatibile",
                        {
                            "requested": requested_api,
                            "supported": SERVICE_API_VERSION,
                        },
                    )
            response = self._invoke(method, params)
            if notification:
                return None
            return {
                "jsonrpc": RPC_PROTOCOL_VERSION,
                "id": request_id,
                "result": response.to_dict(),
            }
        except RPCFault as exc:
            if notification:
                return None
            return error_response(request_id, exc.code, exc.message, exc.data)
        except (KeyError, TypeError, ValueError) as exc:
            if notification:
                return None
            return error_response(request_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # confine: non propagare dettagli interni al client
            log.exception("Errore interno dispatcher RPC")
            if notification:
                return None
            return error_response(
                request_id,
                INTERNAL_ERROR,
                "errore interno del service RFID",
                {"type": type(exc).__name__},
            )

    @staticmethod
    def _validate_request(request: Mapping[str, Any]) -> None:
        if request.get("jsonrpc") != RPC_PROTOCOL_VERSION:
            raise RPCFault(INVALID_REQUEST, "jsonrpc deve essere '2.0'")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            raise RPCFault(INVALID_REQUEST, "method obbligatorio e deve essere una stringa")
        if "id" in request:
            request_id = request["id"]
            if isinstance(request_id, bool) or not isinstance(
                request_id, (str, int, float, type(None))
            ):
                raise RPCFault(INVALID_REQUEST, "id deve essere stringa, numero o null")

    def _invoke(self, method: str, params: Mapping[str, Any]) -> ServiceResponse:
        if method not in self.METHODS:
            raise RPCFault(METHOD_NOT_FOUND, f"metodo non trovato: {method}")

        if method == "rfid.describe":
            self._require_empty(params)
            return self._local_response(
                "describe",
                {
                    "service_api_version": SERVICE_API_VERSION,
                    "rpc_protocol_version": RPC_PROTOCOL_VERSION,
                    "methods": list(self.METHODS),
                },
            )
        if method == "rfid.start":
            self._require_empty(params)
            return self.service.start()
        if method == "rfid.stop":
            self._require_empty(params)
            return self.service.stop()
        if method == "rfid.replace_config":
            self._reject_unknown(params, {"config"})
            config = params["config"]
            if not isinstance(config, Mapping):
                raise ValueError("config deve essere un oggetto JSON")
            return self.service.replace_config(config)
        if method == "rfid.configure":
            self._reject_unknown(params, {"region", "powers"})
            return self.service.configure(params)
        if method == "rfid.generate_epc":
            self._reject_unknown(params, {"byte_length", "prefix_hex"})
            return self.service.generate_epc(params)
        if method == "rfid.inventory":
            self._reject_unknown(params, {"antennas", "timeout_ms", "metadata_flags"})
            return self.service.inventory(params)
        if method == "rfid.read":
            self._reject_unknown(
                params,
                {
                    "bank",
                    "address",
                    "word_count",
                    "antennas",
                    "access_password_hex",
                    "timeout_ms",
                },
            )
            return self.service.read(params)
        if method == "rfid.write":
            self._reject_unknown(
                params,
                {
                    "bank",
                    "address",
                    "data_hex",
                    "expected_epc",
                    "antennas",
                    "access_password_hex",
                    "timeout_ms",
                },
            )
            return self.service.write(params)
        if method == "rfid.write_epc":
            self._reject_unknown(
                params,
                {
                    "new_epc",
                    "expected_epc",
                    "antennas",
                    "access_password_hex",
                    "timeout_ms",
                },
            )
            return self.service.write_epc(params)
        if method == "rfid.lock":
            self._reject_unknown(
                params,
                {
                    "targets",
                    "expected_epc",
                    "antennas",
                    "access_password_hex",
                    "timeout_ms",
                    "allow_permanent",
                },
            )
            if not isinstance(params.get("targets"), Mapping):
                raise ValueError("targets deve essere un oggetto JSON")
            return self.service.lock(params)
        if method == "rfid.configure_gen2":
            self._reject_unknown(
                params,
                {"session", "target", "target_dynamic", "q", "q_dynamic", "rf_mode"},
            )
            return self.service.configure_gen2(params)
        if method == "rfid.read_gen2_settings":
            self._reject_unknown(params, set())
            return self.service.read_gen2_settings()
        if method == "rfid.tune_reader":
            self._reject_unknown(
                params,
                {
                    "power_mode",
                    "rssi_filter_dbm",
                    "disable_rssi_filter",
                    "antenna_dwell_ms",
                    "max_rssi_reporting",
                },
            )
            return self.service.tune_reader(params)
        if method == "rfid.antenna_diagnostics":
            self._reject_unknown(params, {"antenna", "band", "frequencies_khz", "timeout_ms"})
            return self.service.antenna_diagnostics(params)
        if method == "rfid.verify":
            self._reject_unknown(params, {"request", "expected_data_hex"})
            request = params["request"]
            if not isinstance(request, Mapping):
                raise ValueError("request deve essere un oggetto JSON")
            expected_data_hex = params["expected_data_hex"]
            if not isinstance(expected_data_hex, str):
                raise ValueError("expected_data_hex deve essere una stringa")
            self._reject_unknown(
                request,
                {
                    "bank",
                    "address",
                    "word_count",
                    "antennas",
                    "access_password_hex",
                    "timeout_ms",
                },
            )
            return self.service.verify(request, expected_data_hex)
        if method == "rfid.health":
            check_antennas = params.get("check_antennas", True)
            if not isinstance(check_antennas, bool):
                raise ValueError("check_antennas deve essere booleano")
            unknown = set(params) - {"check_antennas"}
            if unknown:
                raise ValueError(f"parametri non riconosciuti: {sorted(unknown)}")
            return self.service.health(check_antennas=check_antennas)
        if method == "rfid.identify":
            if params:
                raise ValueError(f"parametri non riconosciuti: {sorted(params)}")
            return self.service.identify()
        if method == "rfid.snapshot":
            self._require_empty(params)
            return self._local_response("snapshot", {"snapshot": self.service.snapshot()})
        if method == "rfid.events":
            self._reject_unknown(params, {"after_sequence"})
            return self.service.events(params)
        raise RPCFault(METHOD_NOT_FOUND, f"metodo non trovato: {method}")

    def _local_response(self, operation: str, data: Mapping[str, Any]) -> ServiceResponse:
        return ServiceResponse(
            operation=operation,
            ok=True,
            state=self.service.state,
            data=data,
        )

    @staticmethod
    def _require_empty(params: Mapping[str, Any]) -> None:
        if params:
            raise ValueError(f"il metodo non accetta parametri: {sorted(params)}")

    @staticmethod
    def _reject_unknown(params: Mapping[str, Any], allowed: set[str]) -> None:
        unknown = set(params) - allowed
        if unknown:
            raise ValueError(f"parametri non riconosciuti: {sorted(unknown)}")
