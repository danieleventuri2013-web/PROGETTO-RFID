"""Host locale JSON Lines per ``RFIDService``.

Ogni riga letta da stdin contiene una richiesta JSON-RPC 2.0; ogni risposta
viene scritta come una singola riga JSON su stdout. Log e diagnostica usano
stderr/file, cosi' il canale dati resta sempre machine-readable.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

import yaml

from .diagnostics import setup_logging
from .rpc import INVALID_REQUEST, PARSE_ERROR, RFIDRPCDispatcher, error_response
from .service import RFIDService

log = logging.getLogger("rfid_silion.service_host")


def dispatch_json_value(value: Any, dispatcher: RFIDRPCDispatcher) -> Any:
    """Gestisce richiesta singola o batch JSON-RPC."""
    if isinstance(value, list):
        if not value:
            return error_response(None, INVALID_REQUEST, "batch JSON-RPC vuoto")
        responses = [dispatcher.dispatch(request) for request in value]
        filtered = [response for response in responses if response is not None]
        return filtered or None
    return dispatcher.dispatch(value)


def dispatch_json_line(line: str, dispatcher: RFIDRPCDispatcher) -> Any:
    """Decodifica e gestisce una riga, traducendo gli errori di parsing."""
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        return error_response(
            None,
            PARSE_ERROR,
            "JSON non valido",
            {"line": exc.lineno, "column": exc.colno},
        )
    return dispatch_json_value(value, dispatcher)


def run_jsonl_stream(
    input_stream: TextIO,
    output_stream: TextIO,
    dispatcher: RFIDRPCDispatcher,
) -> int:
    """Esegue il loop JSONL fino a EOF; utile anche nei test con ``StringIO``."""
    for raw_line in input_stream:
        line = raw_line.strip()
        if not line:
            continue
        response = dispatch_json_line(line, dispatcher)
        if response is None:
            continue
        output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        output_stream.write("\n")
        output_stream.flush()
    return 0


def load_config(path: Path) -> dict:
    """Carica una configurazione YAML e verifica che la radice sia un mapping."""
    with path.open("r", encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}
    if not isinstance(config, dict):
        raise ValueError("la radice della configurazione deve essere un oggetto YAML")
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Service locale RFID SIM7200 via JSON-RPC 2.0 su stdin/stdout"
    )
    parser.add_argument("--config", required=True, type=Path, help="percorso config YAML")
    parser.add_argument("--debug", action="store_true", help="abilita dump frame TX/RX nei log")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"Errore configurazione service: {exc}", file=sys.stderr)
        return 2

    logging_cfg = config.get("logging", {}) or {}
    setup_logging(
        level=str(logging_cfg.get("level", "INFO")),
        log_dir=logging_cfg.get("dir", "logs"),
        file_prefix="service",
        force_debug=args.debug,
    )
    service = RFIDService(config)
    dispatcher = RFIDRPCDispatcher(service)
    log.info("Service RFID pronto sul canale JSONL; API %s", service.snapshot()["api_version"])
    try:
        return run_jsonl_stream(sys.stdin, sys.stdout, dispatcher)
    except KeyboardInterrupt:
        log.info("Service RFID interrotto")
        return 130
    finally:
        response = service.stop()
        if not response.ok:
            log.error("Arresto service fallito: %s", response.error)


if __name__ == "__main__":
    raise SystemExit(main())
