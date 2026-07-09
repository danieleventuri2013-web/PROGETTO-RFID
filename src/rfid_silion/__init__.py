"""Pacchetto di pilotaggio lettore RFID Silion SIM7200 (protocollo seriale binario).

Implementazione nativa del protocollo documentato in
MANUALI/Communication_Protocol_Doc__20210716 (rif. autorevole: ULTIMI MANUALI/
EX10 Module Communication Prorocol-2024-12), indipendente dalle DLL ModuleAPI.
"""
__version__ = "0.2.0"

from .protocol import (
    HEADER,
    crc16,
    build_packet,
    parse_response,
    SilionFrameError,
    SilionTimeoutError,
)
from .errors import SilionError, NoTagError, status_to_exception, check_status
from .transports import (
    Transport,
    SerialTransport,
    TcpTransport,
    TransportNotOpenError,
)
from .tags import Tag, parse_tag_buffer
from .reader import SIM7200Reader, reader_from_config
from .diagnostics import (
    DiagCounters,
    checkpoint,
    debug_mode_enabled,
    setup_logging,
)

__all__ = [
    "__version__",
    "HEADER",
    "crc16",
    "build_packet",
    "parse_response",
    "SilionFrameError",
    "SilionTimeoutError",
    "SilionError",
    "NoTagError",
    "status_to_exception",
    "check_status",
    "Transport",
    "SerialTransport",
    "TcpTransport",
    "TransportNotOpenError",
    "Tag",
    "parse_tag_buffer",
    "SIM7200Reader",
    "reader_from_config",
    "DiagCounters",
    "checkpoint",
    "debug_mode_enabled",
    "setup_logging",
]
