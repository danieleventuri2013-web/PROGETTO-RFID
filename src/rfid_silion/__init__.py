"""Pacchetto di pilotaggio lettore RFID Silion SIM7200 (protocollo seriale binario).

Implementazione nativa del protocollo documentato in
MANUALI/Communication_Protocol_Doc__20210716 (rif. autorevole: ULTIMI MANUALI/
EX10 Module Communication Prorocol-2024-12), indipendente dalle DLL ModuleAPI.
"""

__version__ = "0.3.0"

from .client import RFIDClientError, RFIDProcessClient
from .diagnostics import (
    DiagCounters,
    checkpoint,
    debug_mode_enabled,
    setup_logging,
)
from .errors import NoTagError, SilionError, check_status, status_to_exception
from .protocol import (
    HEADER,
    SilionFrameError,
    SilionTimeoutError,
    SilionTransportError,
    build_lock_bits,
    build_packet,
    crc16,
    parse_response,
)
from .reader import SIM7200Reader, reader_from_config
from .rpc import RPC_PROTOCOL_VERSION, RFIDRPCDispatcher
from .service import (
    SERVICE_API_VERSION,
    AntennaDiagnosticsRequest,
    AntennaPower,
    EpcGenerationRequest,
    EventRequest,
    Gen2Settings,
    InventoryRequest,
    LockMode,
    LockRequest,
    LockTarget,
    MemoryBank,
    ReaderSettings,
    ReaderTuning,
    ReadRequest,
    RFIDBackend,
    RFIDService,
    RFIDServiceBinding,
    ServiceEvent,
    ServiceResponse,
    ServiceState,
    ServiceStateError,
    UnsafeWriteError,
    WriteEpcRequest,
    WriteRequest,
    discover_serial_ports,
)
from .tags import (
    Tag,
    TagReadAccumulator,
    TagReadSummary,
    parse_tag_buffer,
    summarize_tag_reads,
)
from .transports import (
    SerialTransport,
    TcpTransport,
    Transport,
    TransportDisconnectedError,
    TransportError,
    TransportNotOpenError,
)

__all__ = [
    "__version__",
    "HEADER",
    "crc16",
    "build_packet",
    "build_lock_bits",
    "parse_response",
    "SilionFrameError",
    "SilionTimeoutError",
    "SilionTransportError",
    "SilionError",
    "NoTagError",
    "status_to_exception",
    "check_status",
    "Transport",
    "SerialTransport",
    "TcpTransport",
    "TransportNotOpenError",
    "TransportError",
    "TransportDisconnectedError",
    "Tag",
    "TagReadAccumulator",
    "TagReadSummary",
    "parse_tag_buffer",
    "summarize_tag_reads",
    "SIM7200Reader",
    "reader_from_config",
    "RPC_PROTOCOL_VERSION",
    "RFIDRPCDispatcher",
    "RFIDClientError",
    "RFIDProcessClient",
    "SERVICE_API_VERSION",
    "AntennaDiagnosticsRequest",
    "AntennaPower",
    "EpcGenerationRequest",
    "Gen2Settings",
    "ReaderSettings",
    "ReaderTuning",
    "InventoryRequest",
    "EventRequest",
    "LockMode",
    "LockRequest",
    "LockTarget",
    "MemoryBank",
    "ReadRequest",
    "RFIDBackend",
    "WriteEpcRequest",
    "WriteRequest",
    "ServiceState",
    "ServiceStateError",
    "UnsafeWriteError",
    "ServiceResponse",
    "ServiceEvent",
    "RFIDService",
    "RFIDServiceBinding",
    "discover_serial_ports",
    "DiagCounters",
    "checkpoint",
    "debug_mode_enabled",
    "setup_logging",
]
