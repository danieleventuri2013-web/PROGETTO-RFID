"""Pacchetto di pilotaggio lettore RFID Silion SIM7200 (protocollo seriale binario).

Implementazione nativa del protocollo documentato in
MANUALI/Communication_Protocol_Doc__20210716, indipendente dalle DLL ModuleAPI.
"""
from .protocol import (
    HEADER,
    crc16,
    build_packet,
    parse_response,
    SilionFrameError,
)
from .errors import SilionError, status_to_exception, check_status
from .transports import Transport, SerialTransport, TcpTransport

__all__ = [
    "HEADER",
    "crc16",
    "build_packet",
    "parse_response",
    "SilionFrameError",
    "SilionError",
    "status_to_exception",
    "check_status",
    "Transport",
    "SerialTransport",
    "TcpTransport",
]
