"""Confine applicativo headless per integrare il sottosistema RFID come servizio.

Questo modulo e' intenzionalmente indipendente da Tkinter e da qualunque
framework web. Espone contratti versionati e JSON-safe sopra SIM7200Reader;
la GUI e i futuri adapter HTTP/IPC devono dipendere da questo livello, non dal
protocollo o dai trasporti.
"""

from __future__ import annotations

import copy
import datetime as dt
import logging
import secrets
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum, IntEnum
from typing import Any, Protocol, runtime_checkable

from . import protocol as P
from .errors import NoTagError
from .reader import SIM7200Reader, reader_from_config

log = logging.getLogger("rfid_silion.service")

# 1.1 aggiunge `lock`; 1.2 aggiunge le leve radio (`configure_gen2`,
# `tune_reader`, `antenna_diagnostics`). Tutte aggiunte puramente additive: i
# client che dichiarano una minor version precedente restano serviti, come
# stabilisce la compatibilita' gestita in `rpc.py`.
SERVICE_API_VERSION = "1.4"


class ServiceState(str, Enum):
    """Stati pubblici del lifecycle del servizio."""

    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    ERROR = "error"


class MemoryBank(IntEnum):
    """Banche EPC Gen2 esposte dal contratto senza dipendere dal protocollo."""

    RESERVED = 0
    EPC = 1
    TID = 2
    USER = 3


class ServiceStateError(RuntimeError):
    """Operazione richiesta quando il servizio non e' pronto."""


class UnsafeWriteError(RuntimeError):
    """Scrittura rifiutata perche' il target EPC non e' verificato."""


def discover_serial_ports() -> list[dict[str, str]]:
    """Elenca le porte seriali locali per la GUI di configurazione."""
    from serial.tools import list_ports

    return [
        {
            "device": str(port.device),
            "description": str(port.description or ""),
            "hwid": str(port.hwid or ""),
        }
        for port in sorted(list_ports.comports(), key=lambda item: str(item.device))
    ]


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _json_safe(value: Any) -> Any:
    """Converte ricorsivamente DTO e valori del driver in tipi JSON-safe."""
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex().upper()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return value


def _parse_hex(value: str, field_name: str, *, exact_bytes: int | None = None) -> bytes:
    try:
        parsed = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} non e' un valore esadecimale valido") from exc
    if exact_bytes is not None and len(parsed) != exact_bytes:
        raise ValueError(f"{field_name} deve contenere esattamente {exact_bytes} byte")
    return parsed


def _normalize_epc_hex(value: str, field_name: str) -> str:
    normalized = "".join(str(value).split()).upper()
    parsed = _parse_hex(normalized, field_name)
    if not parsed or len(parsed) % 2 != 0 or len(parsed) > 62:
        raise ValueError(f"{field_name} deve contenere 2..62 byte ed avere un numero pari di byte")
    return normalized


def barcode_to_epc_hex(barcode: str, byte_length: int = 12) -> str:
    """Codifica un barcode ASCII in un EPC a lunghezza fissa con padding nullo."""
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 2:
        raise ValueError("byte_length deve essere un intero di almeno 2")
    if not isinstance(barcode, str) or not barcode:
        raise ValueError("barcode obbligatorio")
    if "\x00" in barcode:
        raise ValueError("il barcode non puo' contenere il carattere nullo")
    try:
        payload = barcode.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("il barcode deve contenere solo caratteri ASCII") from exc
    if len(payload) > byte_length:
        raise ValueError(
            f"barcode troppo lungo: {len(payload)} byte, massimo {byte_length}"
        )
    return payload.ljust(byte_length, b"\x00").hex().upper()


def epc_hex_to_barcode(epc_hex: str, byte_length: int = 12) -> str:
    """Decodifica un EPC ASCII eliminando esclusivamente il padding nullo finale."""
    normalized = "".join(str(epc_hex).split()).upper()
    payload = _parse_hex(normalized, "epc_hex", exact_bytes=byte_length)
    barcode_bytes = payload.rstrip(b"\x00")
    if b"\x00" in barcode_bytes:
        raise ValueError("EPC barcode contiene padding nullo interno")
    try:
        return barcode_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("EPC barcode non contiene una stringa ASCII valida") from exc


def _normalize_antennas(antennas: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(int(antenna) for antenna in antennas)
    if not normalized:
        raise ValueError("selezionare almeno un'antenna")
    if len(set(normalized)) != len(normalized):
        raise ValueError("la lista antenne contiene duplicati")
    invalid = [antenna for antenna in normalized if antenna not in (1, 2, 3, 4)]
    if invalid:
        raise ValueError(f"antenne non valide: {invalid}")
    return normalized


@dataclass(frozen=True)
class AntennaPower:
    antenna_id: int
    read_power_cdbm: int
    write_power_cdbm: int


@dataclass(frozen=True)
class ReaderSettings:
    region: int = 0x08
    powers: tuple[AntennaPower, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "powers", tuple(self.powers))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReaderSettings":
        powers = tuple(
            item if isinstance(item, AntennaPower) else AntennaPower(**item)
            for item in value.get("powers", ())
        )
        return cls(region=int(value.get("region", 0x08)), powers=powers)


@dataclass(frozen=True)
class InventoryRequest:
    antennas: tuple[int, ...] = (1, 2, 3)
    timeout_ms: int = 1000
    metadata_flags: int = 0x0007

    def __post_init__(self) -> None:
        object.__setattr__(self, "antennas", _normalize_antennas(self.antennas))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "InventoryRequest":
        return cls(
            antennas=tuple(value.get("antennas", (1, 2, 3))),
            timeout_ms=int(value.get("timeout_ms", 1000)),
            metadata_flags=int(value.get("metadata_flags", 0x0007)),
        )


@dataclass(frozen=True)
class ReadRequest:
    bank: int
    address: int
    word_count: int
    antennas: tuple[int, ...] = (1, 2, 3)
    access_password_hex: str = "00000000"
    timeout_ms: int = 1000
    #: EPC da isolare con il filtro Select. Vuoto = nessun filtro, e la lettura
    #: colpisce il primo tag che risponde: corretto solo con un tag in campo.
    #: Con piu' tag e' l'unico modo di scegliere quale interrogare.
    select_epc: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "antennas", _normalize_antennas(self.antennas))
        object.__setattr__(self, "select_epc", self.select_epc.strip().upper())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReadRequest":
        return cls(
            bank=int(value["bank"]),
            address=int(value.get("address", 0)),
            word_count=int(value["word_count"]),
            antennas=tuple(value.get("antennas", (1, 2, 3))),
            access_password_hex=str(value.get("access_password_hex", "00000000")),
            timeout_ms=int(value.get("timeout_ms", 1000)),
            select_epc=str(value.get("select_epc", "")),
        )


@dataclass(frozen=True)
class WriteRequest:
    bank: int
    address: int
    data_hex: str
    expected_epc: str
    antennas: tuple[int, ...] = (1, 2, 3)
    access_password_hex: str = "00000000"
    timeout_ms: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "antennas", _normalize_antennas(self.antennas))
        object.__setattr__(self, "expected_epc", self.expected_epc.strip().upper())

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WriteRequest":
        return cls(
            bank=int(value["bank"]),
            address=int(value.get("address", 0)),
            data_hex=str(value["data_hex"]),
            expected_epc=str(value["expected_epc"]),
            antennas=tuple(value.get("antennas", (1, 2, 3))),
            access_password_hex=str(value.get("access_password_hex", "00000000")),
            timeout_ms=int(value.get("timeout_ms", 1000)),
        )


@dataclass(frozen=True)
class EpcGenerationRequest:
    """Parametri per creare un EPC casuale senza dipendere dalla GUI."""

    byte_length: int = 12
    prefix_hex: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.byte_length, bool) or not isinstance(self.byte_length, int):
            raise ValueError("byte_length deve essere un intero")
        if not 2 <= self.byte_length <= 62 or self.byte_length % 2:
            raise ValueError("byte_length deve essere pari e compreso tra 2 e 62")
        prefix = "".join(self.prefix_hex.split()).upper()
        prefix_bytes = _parse_hex(prefix, "prefix_hex") if prefix else b""
        if len(prefix_bytes) > self.byte_length - 8:
            raise ValueError("prefix_hex deve lasciare almeno 8 byte casuali")
        object.__setattr__(self, "prefix_hex", prefix)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EpcGenerationRequest":
        return cls(
            byte_length=int(value.get("byte_length", 12)),
            prefix_hex=str(value.get("prefix_hex", "")),
        )


@dataclass(frozen=True)
class WriteEpcRequest:
    """Cambio EPC protetto dall'identita' rilevata nell'ultimo inventory."""

    new_epc: str
    expected_epc: str
    antennas: tuple[int, ...] = (1, 2, 3)
    access_password_hex: str = "00000000"
    timeout_ms: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "new_epc", _normalize_epc_hex(self.new_epc, "new_epc"))
        object.__setattr__(
            self,
            "expected_epc",
            _normalize_epc_hex(self.expected_epc, "expected_epc"),
        )
        object.__setattr__(self, "antennas", _normalize_antennas(self.antennas))
        if self.new_epc == self.expected_epc:
            raise ValueError("new_epc deve essere diverso da expected_epc")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "WriteEpcRequest":
        return cls(
            new_epc=str(value["new_epc"]),
            expected_epc=str(value["expected_epc"]),
            antennas=tuple(value.get("antennas", (1, 2, 3))),
            access_password_hex=str(value.get("access_password_hex", "00000000")),
            timeout_ms=int(value.get("timeout_ms", 1000)),
        )


@dataclass(frozen=True)
class Gen2Settings:
    """Parametri radio Gen2 (comando 0x9B).

    Ogni campo a `None` viene lasciato com'e': cosi' una passata di lettura puo'
    cambiare una sola leva senza doverle riaffermare tutte, ed e' anche il modo
    per non toccare per sbaglio un parametro che l'operatore ha tarato a mano.
    """

    session: int | None = None
    target: int | None = None
    target_dynamic: bool = False
    q: int | None = None
    q_dynamic: bool = False
    rf_mode: int | None = None

    def __post_init__(self) -> None:
        if self.session is not None and self.session not in (0, 1, 2, 3):
            raise ValueError("session deve essere compresa fra 0 e 3")
        if self.target is not None and self.target not in (0, 1):
            raise ValueError("target deve essere 0 (A / A-B) o 1 (B / B-A)")
        if self.q is not None and not 0 <= self.q <= 15:
            raise ValueError("q deve essere compreso fra 0 e 15")
        if self.q is not None and self.q_dynamic:
            raise ValueError("q_dynamic esclude un valore fisso di q")
        if self.rf_mode is not None and not 0 <= self.rf_mode <= 0xFF:
            raise ValueError("rf_mode deve stare in un byte")

    @property
    def touches_anything(self) -> bool:
        return any(
            (
                self.session is not None,
                self.target is not None,
                self.q is not None,
                self.q_dynamic,
                self.rf_mode is not None,
            )
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Gen2Settings":
        def _int_or_none(chiave: str) -> int | None:
            grezzo = value.get(chiave)
            return None if grezzo is None else int(grezzo)

        return cls(
            session=_int_or_none("session"),
            target=_int_or_none("target"),
            target_dynamic=bool(value.get("target_dynamic", False)),
            q=_int_or_none("q"),
            q_dynamic=bool(value.get("q_dynamic", False)),
            rf_mode=_int_or_none("rf_mode"),
        )


@dataclass(frozen=True)
class ReaderTuning:
    """Impostazioni che non cambiano cosa si legge, ma se lo si legge.

    Lasciate al valore di fabbrica sabotano in silenzio la lettura ripetuta: il
    risparmio energetico spegne la radio fra un comando e l'altro, e un filtro
    RSSI ereditato scarta proprio i tag piu' deboli.
    """

    power_mode: int | None = None
    rssi_filter_dbm: int | None = None
    disable_rssi_filter: bool = False
    antenna_dwell_ms: int | None = None
    max_rssi_reporting: bool | None = None
    #: Ciclo di lavoro del trasmettitore: quanto resta a piena potenza dentro un
    #: periodo. Serve a contenere il riscaldamento nelle sessioni lunghe, ed e'
    #: la leva con cui si rispetta un limite di occupazione del canale. I due
    #: valori si impostano insieme: uno solo non ha significato.
    duty_cycle_full_ms: int | None = None
    duty_cycle_period_ms: int | None = None

    def __post_init__(self) -> None:
        if self.power_mode is not None and not 0 <= self.power_mode <= 3:
            raise ValueError("power_mode deve essere compreso fra 0 e 3")
        if self.rssi_filter_dbm is not None and self.disable_rssi_filter:
            raise ValueError("disable_rssi_filter esclude una soglia esplicita")
        if self.antenna_dwell_ms is not None and not 20 <= self.antenna_dwell_ms <= 60000:
            raise ValueError("antenna_dwell_ms deve essere compreso fra 20 e 60000")
        pieno, periodo = self.duty_cycle_full_ms, self.duty_cycle_period_ms
        if (pieno is None) != (periodo is None):
            raise ValueError("duty cycle: servono sia la finestra piena sia il periodo")
        if pieno is not None and periodo is not None and pieno > periodo:
            raise ValueError("duty cycle: la finestra piena non puo' superare il periodo")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ReaderTuning":
        def _int_or_none(chiave: str) -> int | None:
            grezzo = value.get(chiave)
            return None if grezzo is None else int(grezzo)

        massimo = value.get("max_rssi_reporting")
        return cls(
            power_mode=_int_or_none("power_mode"),
            rssi_filter_dbm=_int_or_none("rssi_filter_dbm"),
            disable_rssi_filter=bool(value.get("disable_rssi_filter", False)),
            antenna_dwell_ms=_int_or_none("antenna_dwell_ms"),
            max_rssi_reporting=None if massimo is None else bool(massimo),
            duty_cycle_full_ms=_int_or_none("duty_cycle_full_ms"),
            duty_cycle_period_ms=_int_or_none("duty_cycle_period_ms"),
        )


@dataclass(frozen=True)
class AntennaDiagnosticsRequest:
    """Misura di onda stazionaria su un'antenna (comando 0xAA4A)."""

    antenna: int
    band: int = 0x08
    frequencies_khz: tuple[int, ...] = ()
    timeout_ms: int = 30000

    def __post_init__(self) -> None:
        if self.antenna not in (1, 2, 3, 4):
            raise ValueError(f"antenna non valida: {self.antenna}")
        object.__setattr__(self, "frequencies_khz", tuple(int(f) for f in self.frequencies_khz))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AntennaDiagnosticsRequest":
        return cls(
            antenna=int(value["antenna"]),
            band=int(value.get("band", 0x08)),
            frequencies_khz=tuple(value.get("frequencies_khz", ())),
            timeout_ms=int(value.get("timeout_ms", 30000)),
        )


class LockTarget(str, Enum):
    """Cio' che il comando 0x25 puo' bloccare, oltre alle banche di memoria."""

    KILL_PASSWORD = "kill_password"
    ACCESS_PASSWORD = "access_password"
    EPC = "epc"
    TID = "tid"
    USER = "user"


class LockMode(str, Enum):
    """Le cinque operazioni previste dal manuale EX10 §6.3.

    `PERMALOCK` e `PERMAUNLOCK` sono **irreversibili**: nessuna password le
    annulla, su nessun tag.
    """

    NO_ACTION = P.LOCK_NO_ACTION
    LOCK = P.LOCK_LOCK
    UNLOCK = P.LOCK_UNLOCK
    PERMALOCK = P.LOCK_PERMALOCK
    PERMAUNLOCK = P.LOCK_PERMAUNLOCK


@dataclass(frozen=True)
class LockRequest:
    """Blocco o sblocco delle banche di un tag.

    `allow_permanent` deve valere `True` perche' un'operazione permanente venga
    eseguita: e' un consenso esplicito richiesto proprio perche' non esiste modo
    di tornare indietro.
    """

    targets: Mapping[str, str]
    expected_epc: str
    antennas: tuple[int, ...] = (1, 2, 3)
    access_password_hex: str = "00000000"
    timeout_ms: int = 1000
    allow_permanent: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "antennas", _normalize_antennas(self.antennas))
        object.__setattr__(self, "expected_epc", self.expected_epc.strip().upper())
        # I membri enum vanno riconosciuti come tali: `str(LockTarget.USER)` da'
        # "LockTarget.USER", non "user", quindi passarli al costruttore fallirebbe.
        def _valore(item: Any, enum_cls: type[Enum]) -> str:
            if isinstance(item, enum_cls):
                return str(item.value)
            return str(enum_cls(str(item)).value)

        normalizzati = {
            _valore(chiave, LockTarget): _valore(valore, LockMode)
            for chiave, valore in dict(self.targets).items()
        }
        if not normalizzati:
            raise ValueError("indicare almeno una banca da bloccare o sbloccare")
        object.__setattr__(self, "targets", normalizzati)

    @property
    def permanent_targets(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                chiave
                for chiave, modo in self.targets.items()
                if modo in P.LOCK_PERMANENT_OPERATIONS
            )
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LockRequest":
        return cls(
            targets=dict(value["targets"]),
            expected_epc=str(value["expected_epc"]),
            antennas=tuple(value.get("antennas", (1, 2, 3))),
            access_password_hex=str(value.get("access_password_hex", "00000000")),
            timeout_ms=int(value.get("timeout_ms", 1000)),
            allow_permanent=bool(value.get("allow_permanent", False)),
        )


@dataclass(frozen=True)
class EventRequest:
    """Cursore per il polling affidabile della cronologia eventi."""

    after_sequence: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.after_sequence, bool) or not isinstance(self.after_sequence, int):
            raise ValueError("after_sequence deve essere un intero")
        if self.after_sequence < 0:
            raise ValueError("after_sequence non puo' essere negativo")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EventRequest":
        raw = value.get("after_sequence", 0)
        if isinstance(raw, bool):
            raise ValueError("after_sequence deve essere un intero")
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("after_sequence deve essere un intero") from exc
        if isinstance(raw, float) and not raw.is_integer():
            raise ValueError("after_sequence deve essere un intero")
        return cls(after_sequence=parsed)


@dataclass(frozen=True)
class ServiceResponse:
    """Risposta stabile del confine service, sempre serializzabile in JSON."""

    operation: str
    ok: bool
    state: ServiceState
    data: Mapping[str, Any] = field(default_factory=dict)
    error: Mapping[str, Any] | None = None
    timestamp: str = field(default_factory=_now)
    api_version: str = SERVICE_API_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)


@dataclass(frozen=True)
class ServiceEvent:
    """Evento sequenziale consumabile da GUI, framework o adapter esterni."""

    sequence: int
    kind: str
    state: ServiceState
    data: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)
    api_version: str = SERVICE_API_VERSION

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)


@runtime_checkable
class RFIDBackend(Protocol):
    """Interfaccia strutturale usata da GUI, framework e adapter.

    Consente di sostituire ``RFIDService`` con un client RPC mantenendo gli
    stessi DTO e impedisce alla GUI di dipendere dall'implementazione hardware.
    """

    @property
    def state(self) -> ServiceState: ...

    @property
    def ready(self) -> bool: ...

    def replace_config(self, config: Mapping[str, Any]) -> ServiceResponse: ...

    def start(self) -> ServiceResponse: ...

    def stop(self) -> ServiceResponse: ...

    def configure(self, settings: ReaderSettings | Mapping[str, Any]) -> ServiceResponse: ...

    def generate_epc(
        self,
        request: EpcGenerationRequest | Mapping[str, Any] | None = None,
    ) -> ServiceResponse: ...

    def inventory(self, request: InventoryRequest | Mapping[str, Any]) -> ServiceResponse: ...

    def read(self, request: ReadRequest | Mapping[str, Any]) -> ServiceResponse: ...

    def write(self, request: WriteRequest | Mapping[str, Any]) -> ServiceResponse: ...

    def write_epc(self, request: WriteEpcRequest | Mapping[str, Any]) -> ServiceResponse: ...

    def lock(self, request: LockRequest | Mapping[str, Any]) -> ServiceResponse: ...

    def configure_gen2(self, settings: Gen2Settings | Mapping[str, Any]) -> ServiceResponse: ...

    def read_gen2_settings(self) -> ServiceResponse: ...

    def tune_reader(self, settings: ReaderTuning | Mapping[str, Any]) -> ServiceResponse: ...

    def antenna_diagnostics(
        self, request: AntennaDiagnosticsRequest | Mapping[str, Any]
    ) -> ServiceResponse: ...

    def verify(
        self,
        request: ReadRequest | Mapping[str, Any],
        expected_data_hex: str,
    ) -> ServiceResponse: ...

    def health(self, check_antennas: bool = True) -> ServiceResponse: ...

    def identify(self) -> ServiceResponse: ...

    def snapshot(self) -> dict[str, Any]: ...

    def events(
        self,
        request: EventRequest | Mapping[str, Any] | None = None,
    ) -> ServiceResponse: ...

    def recent_events(self, after_sequence: int = 0) -> list[dict[str, Any]]: ...


class RFIDServiceBinding:
    """Associa un client a un backend dichiarandone la proprietà lifecycle.

    Un binding ``owned`` chiude il backend al detach. Un binding ``shared``
    scollega soltanto il client: il framework resta l'unico proprietario del
    trasporto e la GUI non può liberare o riaprire COM/socket autonomamente.
    """

    def __init__(self, backend: RFIDBackend, *, owns_lifecycle: bool):
        self.backend = backend
        self.owns_lifecycle = bool(owns_lifecycle)
        self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self, config: Mapping[str, Any] | None = None) -> ServiceResponse:
        """Configura da fermo e avvia/idempotentemente collega il backend."""
        if not self.backend.ready and config is not None:
            configured = self.backend.replace_config(config)
            if not configured.ok:
                return configured
        response = self.backend.start()
        self._attached = response.ok
        return response

    def detach(self) -> ServiceResponse:
        """Chiude solo un backend posseduto; quello condiviso resta attivo."""
        self._attached = False
        if self.owns_lifecycle:
            return self.backend.stop()
        return ServiceResponse(
            operation="detach",
            ok=True,
            state=self.backend.state,
            data={"service_stopped": False, "ownership": "shared"},
        )


ReaderFactory = Callable[[dict[str, Any]], SIM7200Reader]
EventListener = Callable[[ServiceEvent], None]


class RFIDService:
    """Black box sincrona sopra driver, trasporto e protocollo Silion.

    Le chiamate sono serializzate con un lock. Il livello chiamante decide se
    eseguirle su un worker, in un processo service o direttamente. Nessun
    metodo dipende da una GUI.
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        reader_factory: ReaderFactory = reader_from_config,
        event_history_size: int = 1000,
    ):
        if event_history_size < 1:
            raise ValueError("event_history_size deve essere almeno 1")
        self._config = copy.deepcopy(dict(config))
        self._reader_factory = reader_factory
        self._reader: SIM7200Reader | None = None
        self._state = ServiceState.STOPPED
        self._lock = threading.RLock()
        self._listeners: list[EventListener] = []
        self._events: deque[ServiceEvent] = deque(maxlen=event_history_size)
        self._event_sequence = 0
        self._inventory_antennas: tuple[int, ...] | None = None
        self._observed_epcs: frozenset[str] = frozenset()
        self._generated_epcs: set[str] = set()

    @property
    def state(self) -> ServiceState:
        with self._lock:
            return self._state

    @property
    def ready(self) -> bool:
        return self.state is ServiceState.READY

    def subscribe(self, listener: EventListener) -> Callable[[], None]:
        """Registra un listener e restituisce una funzione di unsubscribe."""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def events(
        self,
        request: EventRequest | Mapping[str, Any] | None = None,
    ) -> ServiceResponse:
        """Legge la cronologia senza generare nuovi eventi di polling."""
        try:
            query = (
                EventRequest()
                if request is None
                else request
                if isinstance(request, EventRequest)
                else EventRequest.from_mapping(request)
            )
        except (TypeError, ValueError) as exc:
            return ServiceResponse(
                operation="events",
                ok=False,
                state=self.state,
                error={"type": type(exc).__name__, "message": str(exc)},
            )

        with self._lock:
            retained = tuple(self._events)
            first_available = retained[0].sequence if retained else self._event_sequence + 1
            selected = [
                event.to_dict() for event in retained if event.sequence > query.after_sequence
            ]
            cursor = int(selected[-1]["sequence"]) if selected else query.after_sequence
            return ServiceResponse(
                operation="events",
                ok=True,
                state=self._state,
                data={
                    "after_sequence": query.after_sequence,
                    "next_after_sequence": cursor,
                    "first_available_sequence": first_available,
                    "last_sequence": self._event_sequence,
                    "history_truncated": query.after_sequence < first_available - 1,
                    "events": selected,
                },
            )

    def recent_events(self, after_sequence: int = 0) -> list[dict[str, Any]]:
        """Scorciatoia compatibile; per i metadati di retention usare events()."""
        response = self.events(EventRequest(after_sequence=after_sequence))
        return [dict(event) for event in response.data["events"]]

    def _emit(self, kind: str, data: Mapping[str, Any] | None = None) -> ServiceEvent:
        with self._lock:
            self._event_sequence += 1
            event = ServiceEvent(
                sequence=self._event_sequence,
                kind=kind,
                state=self._state,
                data=data or {},
            )
            self._events.append(event)
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                log.exception("Listener evento service fallito: %s", kind)
        return event

    def _set_state(self, state: ServiceState) -> None:
        with self._lock:
            previous = self._state
            self._state = state
        if previous is not state:
            self._emit("state.changed", {"previous": previous.value, "current": state.value})

    def _success(self, operation: str, data: Mapping[str, Any] | None = None) -> ServiceResponse:
        response = ServiceResponse(operation=operation, ok=True, state=self.state, data=data or {})
        self._emit("operation.completed", {"response": response.to_dict()})
        return response

    def _failure(
        self,
        operation: str,
        exc: Exception,
        data: Mapping[str, Any] | None = None,
    ) -> ServiceResponse:
        response = ServiceResponse(
            operation=operation,
            ok=False,
            state=self.state,
            data=data or {},
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        self._emit("operation.failed", {"response": response.to_dict()})
        return response

    def _require_reader(self) -> SIM7200Reader:
        if self._state is not ServiceState.READY or self._reader is None:
            raise ServiceStateError(f"servizio non pronto: stato={self._state.value}")
        return self._reader

    def replace_config(self, config: Mapping[str, Any]) -> ServiceResponse:
        """Sostituisce la config soltanto quando il servizio non e' attivo."""
        operation = "replace_config"
        try:
            with self._lock:
                if self._reader is not None or self._state not in (
                    ServiceState.STOPPED,
                    ServiceState.ERROR,
                ):
                    raise ServiceStateError("arrestare il servizio prima di sostituire la config")
                self._config = copy.deepcopy(dict(config))
                self._state = ServiceState.STOPPED
                self._inventory_antennas = None
                self._observed_epcs = frozenset()
            return self._success(operation)
        except Exception as exc:
            return self._failure(operation, exc)

    def start(self) -> ServiceResponse:
        """Apre il trasporto ed esegue il boot firmware; chiamata idempotente."""
        with self._lock:
            if self._state is ServiceState.READY and self._reader is not None:
                return self._success(
                    "start",
                    {"already_started": True, "firmware_info": self._reader.firmware_info},
                )
            self._set_state(ServiceState.STARTING)
            reader: SIM7200Reader | None = None
            try:
                reader = self._reader_factory(copy.deepcopy(self._config))
                reader.open()
                firmware_info = reader.boot_firmware()
                self._reader = reader
                self._inventory_antennas = None
                self._observed_epcs = frozenset()
                self._set_state(ServiceState.READY)
                transport = reader.health_check(check_antennas=False)["transport"]
                return self._success(
                    "start",
                    {
                        "already_started": False,
                        "firmware_info": firmware_info,
                        "transport": transport,
                    },
                )
            except Exception as exc:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:
                        log.exception("Chiusura reader fallita dopo errore di avvio")
                self._reader = None
                self._set_state(ServiceState.ERROR)
                return self._failure("start", exc)

    def stop(self) -> ServiceResponse:
        """Chiude il trasporto e azzera lo stato volatile; chiamata idempotente."""
        with self._lock:
            if self._reader is None and self._state is ServiceState.STOPPED:
                return self._success("stop", {"already_stopped": True})
            self._set_state(ServiceState.STOPPING)
            reader, self._reader = self._reader, None
            self._inventory_antennas = None
            self._observed_epcs = frozenset()
            try:
                if reader is not None:
                    reader.close()
            except Exception as exc:
                self._set_state(ServiceState.ERROR)
                return self._failure("stop", exc)
            self._set_state(ServiceState.STOPPED)
            return self._success("stop", {"already_stopped": False})

    def configure(self, settings: ReaderSettings | Mapping[str, Any]) -> ServiceResponse:
        """Applica regione e potenze senza esporre i comandi del protocollo."""
        operation = "configure"
        try:
            if isinstance(settings, Mapping):
                settings = ReaderSettings.from_mapping(settings)
            powers = [
                (item.antenna_id, item.read_power_cdbm, item.write_power_cdbm)
                for item in settings.powers
            ]
            with self._lock:
                reader = self._require_reader()
                reader.set_region(settings.region)
                if powers:
                    reader.set_antennas_power(powers)
            return self._success(operation, {"settings": _json_safe(settings)})
        except Exception as exc:
            return self._failure(operation, exc)

    def generate_epc(
        self,
        request: EpcGenerationRequest | Mapping[str, Any] | None = None,
    ) -> ServiceResponse:
        """Genera un EPC casuale; non esegue I/O sul lettore."""
        operation = "generate_epc"
        try:
            if request is None:
                request = EpcGenerationRequest()
            elif isinstance(request, Mapping):
                request = EpcGenerationRequest.from_mapping(request)
            prefix = bytes.fromhex(request.prefix_hex)
            random_length = request.byte_length - len(prefix)
            with self._lock:
                unavailable = self._observed_epcs | self._generated_epcs
                for _attempt in range(10):
                    candidate = (prefix + secrets.token_bytes(random_length)).hex().upper()
                    if candidate not in unavailable:
                        self._generated_epcs.add(candidate)
                        break
                else:
                    raise RuntimeError("impossibile generare un EPC non osservato")
            return self._success(
                operation,
                {
                    "epc": candidate,
                    "byte_length": request.byte_length,
                    "prefix_hex": request.prefix_hex,
                    "random_bits": random_length * 8,
                    "guarantee": "unico nella sessione; unicita' globale da confermare nel framework",
                    "uniqueness_scope": "service_session",
                },
            )
        except Exception as exc:
            return self._failure(operation, exc)

    def inventory(self, request: InventoryRequest | Mapping[str, Any]) -> ServiceResponse:
        """Esegue un ciclo inventory e restituisce un batch di tag JSON-safe."""
        operation = "inventory"
        try:
            if isinstance(request, Mapping):
                request = InventoryRequest.from_mapping(request)
            with self._lock:
                reader = self._require_reader()
                if request.antennas != self._inventory_antennas:
                    reader.set_antennas_for_inventory(
                        [(antenna, antenna) for antenna in request.antennas]
                    )
                    self._inventory_antennas = request.antennas
                try:
                    tags = reader.inventory(
                        timeout_ms=request.timeout_ms,
                        metadata_flags=request.metadata_flags,
                    )
                except NoTagError:
                    tags = []
                self._observed_epcs = frozenset(tag.epc.upper() for tag in tags)
            data = {
                "request": _json_safe(request),
                "tags": [_json_safe(tag) for tag in tags],
                "unique_epcs": sorted(self._observed_epcs),
            }
            self._emit("inventory.tags", data)
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def read(self, request: ReadRequest | Mapping[str, Any]) -> ServiceResponse:
        """Legge una banca provando le antenne richieste."""
        operation = "read"
        try:
            if isinstance(request, Mapping):
                request = ReadRequest.from_mapping(request)
            password = _parse_hex(
                request.access_password_hex,
                "access_password_hex",
                exact_bytes=4,
            )
            select = (
                _parse_hex(request.select_epc, "select_epc") if request.select_epc else None
            )
            with self._lock:
                reader = self._require_reader()
                results = reader.read_try_all_antennas(
                    list(request.antennas),
                    request.bank,
                    request.address,
                    request.word_count,
                    password,
                    request.timeout_ms,
                    select,
                )
            data = {"request": _json_safe(request), "results": _json_safe(results)}
            if not any(result.get("ok") for result in results.values()):
                return self._failure(
                    operation,
                    RuntimeError(f"lettura fallita su tutte le antenne: {data['results']}"),
                    data,
                )
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def write(self, request: WriteRequest | Mapping[str, Any]) -> ServiceResponse:
        """Scrive soltanto se l'ultimo inventory ha visto un unico EPC atteso."""
        operation = "write"
        try:
            if isinstance(request, Mapping):
                request = WriteRequest.from_mapping(request)
            password = _parse_hex(
                request.access_password_hex,
                "access_password_hex",
                exact_bytes=4,
            )
            data_bytes = _parse_hex(request.data_hex, "data_hex")
            if not request.expected_epc:
                raise UnsafeWriteError("expected_epc obbligatorio")
            with self._lock:
                reader = self._require_reader()
                if self._observed_epcs != frozenset({request.expected_epc}):
                    raise UnsafeWriteError(
                        "scrittura bloccata: l'ultimo inventory non contiene "
                        f"solo l'EPC atteso {request.expected_epc}"
                    )
                results = reader.write_try_all_antennas(
                    list(request.antennas),
                    request.bank,
                    request.address,
                    data_bytes,
                    password,
                    request.timeout_ms,
                )
            data = {"request": _json_safe(request), "results": _json_safe(results)}
            if not any(result.get("ok") for result in results.values()):
                return self._failure(
                    operation,
                    RuntimeError(f"scrittura fallita su tutte le antenne: {data['results']}"),
                    data,
                )
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def configure_gen2(self, settings: Gen2Settings | Mapping[str, Any]) -> ServiceResponse:
        """Applica i parametri radio Gen2 e li **rilegge** per conferma.

        La rilettura non e' zelo: il manuale avverte che una modalita' RF non
        supportata viene accettata con stato di successo, mentre il modulo
        ripiega su un'altra. Senza il riscontro si crederebbe di leggere a
        -93 dBm mentre si legge a -88.
        """
        operation = "configure_gen2"
        try:
            if isinstance(settings, Mapping):
                settings = Gen2Settings.from_mapping(settings)
            applicati: dict[str, Any] = {}
            with self._lock:
                reader = self._require_reader()
                if settings.session is not None:
                    reader.set_gen2_session(settings.session)
                    applicati["session"] = settings.session
                if settings.target is not None:
                    reader.set_gen2_target(settings.target, dynamic=settings.target_dynamic)
                    applicati["target"] = settings.target
                    applicati["target_dynamic"] = settings.target_dynamic
                if settings.q_dynamic:
                    reader.set_gen2_q(None)
                    applicati["q"] = "dynamic"
                elif settings.q is not None:
                    reader.set_gen2_q(settings.q)
                    applicati["q"] = settings.q
                if settings.rf_mode is not None:
                    reader.set_gen2_rf_mode(settings.rf_mode)
                    applicati["rf_mode"] = settings.rf_mode
                    riletto = reader.get_gen2_param(P.GEN2_PARAM_RF_MODE)
                    applicati["rf_mode_readback"] = riletto.hex().upper()
                    if riletto and riletto[-1] != settings.rf_mode:
                        return self._failure(
                            operation,
                            RuntimeError(
                                "il modulo ha ripiegato su un'altra modalita' RF: chiesta "
                                f"0x{settings.rf_mode:02X}, applicata 0x{riletto[-1]:02X}"
                            ),
                            {"requested": _json_safe(settings), "applied": applicati},
                        )
            data = {
                "request": _json_safe(settings),
                "applied": applicati,
                "sensitivity_dbm": P.RF_MODE_SENSITIVITY_DBM.get(settings.rf_mode),
            }
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def read_gen2_settings(self) -> ServiceResponse:
        """Legge l'assetto Gen2 completo per poterlo ripristinare dopo una prova."""
        operation = "read_gen2_settings"
        try:
            with self._lock:
                reader = self._require_reader()
                sessione = reader.get_gen2_param(P.GEN2_PARAM_SESSION)
                target = reader.get_gen2_param(P.GEN2_PARAM_TARGET)
                q = reader.get_gen2_param(P.GEN2_PARAM_Q)
                rf_mode = reader.get_gen2_param(P.GEN2_PARAM_RF_MODE)
            if not sessione or len(target) < 2 or not q or not rf_mode:
                raise RuntimeError("risposta Gen2 incompleta dal lettore")
            q_dinamico = q[0] == P.GEN2_Q_DYNAMIC
            impostazioni = {
                "session": sessione[-1],
                "target": target[-1],
                "target_dynamic": target[0] == P.GEN2_TARGET_DYNAMIC,
                "q": None if q_dinamico else q[-1],
                "q_dynamic": q_dinamico,
                "rf_mode": rf_mode[-1],
            }
            return self._success(operation, {"settings": impostazioni})
        except Exception as exc:
            return self._failure(operation, exc)

    def tune_reader(self, settings: ReaderTuning | Mapping[str, Any]) -> ServiceResponse:
        """Applica le impostazioni che condizionano la lettura ripetuta."""
        operation = "tune_reader"
        try:
            if isinstance(settings, Mapping):
                settings = ReaderTuning.from_mapping(settings)
            applicati: dict[str, Any] = {}
            with self._lock:
                reader = self._require_reader()
                if settings.power_mode is not None:
                    reader.set_power_mode(settings.power_mode)
                    applicati["power_mode"] = settings.power_mode
                if settings.antenna_dwell_ms is not None:
                    reader.set_antenna_dwell_time(settings.antenna_dwell_ms)
                    applicati["antenna_dwell_ms"] = settings.antenna_dwell_ms
                if settings.max_rssi_reporting is not None:
                    reader.set_rssi_report_mode(
                        P.RSSI_MODE_MAX if settings.max_rssi_reporting else P.RSSI_MODE_LAST
                    )
                    applicati["max_rssi_reporting"] = settings.max_rssi_reporting
                if settings.disable_rssi_filter:
                    reader.set_rssi_filter(None)
                    applicati["rssi_filter_dbm"] = None
                elif settings.rssi_filter_dbm is not None:
                    reader.set_rssi_filter(settings.rssi_filter_dbm)
                    applicati["rssi_filter_dbm"] = settings.rssi_filter_dbm
                if settings.duty_cycle_full_ms is not None:
                    reader.set_duty_cycle(
                        settings.duty_cycle_full_ms, settings.duty_cycle_period_ms
                    )
                    applicati["duty_cycle_full_ms"] = settings.duty_cycle_full_ms
                    applicati["duty_cycle_period_ms"] = settings.duty_cycle_period_ms
            return self._success(
                operation, {"request": _json_safe(settings), "applied": applicati}
            )
        except Exception as exc:
            return self._failure(operation, exc)

    def antenna_diagnostics(
        self, request: AntennaDiagnosticsRequest | Mapping[str, Any]
    ) -> ServiceResponse:
        """Misura l'adattamento dell'antenna, frequenza per frequenza (0xAA4A).

        Serve a sapere se un'antenna e' adattata alla banda in cui la si sta
        usando davvero, invece di fidarsi delle curve del datasheet.
        """
        operation = "antenna_diagnostics"
        try:
            if isinstance(request, Mapping):
                request = AntennaDiagnosticsRequest.from_mapping(request)
            with self._lock:
                reader = self._require_reader()
                misure = reader.measure_standing_wave(
                    request.antenna,
                    band=request.band,
                    frequencies_khz=list(request.frequencies_khz) or None,
                    timeout_ms=request.timeout_ms,
                )
            peggiore = max((m["vswr"] for m in misure), default=None)
            data = {
                "request": _json_safe(request),
                "measurements": misure,
                "worst_vswr": peggiore,
                "threshold": P.VSWR_ALERT_THRESHOLD,
                "ok": peggiore is not None and peggiore < P.VSWR_ALERT_THRESHOLD,
            }
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def lock(self, request: LockRequest | Mapping[str, Any]) -> ServiceResponse:
        """Blocca o sblocca le banche del tag, con le stesse cautele della scrittura.

        Vale la guardia dell'EPC atteso: con l'opzione senza filtro il comando
        colpisce il primo tag che risponde, e un lock dato al tag sbagliato non
        e' sempre rimediabile.

        Le operazioni permanenti richiedono `allow_permanent=True`. Non e' una
        formalita': dopo un permalock la banca non torna piu' scrivibile, e un
        contenitore da riscrivere andrebbe buttato.
        """
        operation = "lock"
        try:
            if isinstance(request, Mapping):
                request = LockRequest.from_mapping(request)
            password = _parse_hex(
                request.access_password_hex,
                "access_password_hex",
                exact_bytes=4,
            )
            if not request.expected_epc:
                raise UnsafeWriteError("expected_epc obbligatorio")
            permanenti = request.permanent_targets
            if permanenti and not request.allow_permanent:
                raise UnsafeWriteError(
                    "operazione permanente e irreversibile su "
                    f"{', '.join(permanenti)}: richiede allow_permanent=true"
                )
            mask, action = P.build_lock_bits(dict(request.targets))
            with self._lock:
                reader = self._require_reader()
                if self._observed_epcs != frozenset({request.expected_epc}):
                    raise UnsafeWriteError(
                        "lock bloccato: l'ultimo inventory non contiene "
                        f"solo l'EPC atteso {request.expected_epc}"
                    )
                results = reader.lock_try_all_antennas(
                    list(request.antennas),
                    mask,
                    action,
                    password,
                    request.timeout_ms,
                )
            data = {
                "request": _json_safe(request),
                "results": _json_safe(results),
                "mask": f"0x{mask:04X}",
                "action": f"0x{action:04X}",
                "permanent": list(permanenti),
            }
            if not any(result.get("ok") for result in results.values()):
                return self._failure(
                    operation,
                    RuntimeError(f"lock fallito su tutte le antenne: {data['results']}"),
                    data,
                )
            self._emit("tag.locked", data)
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def write_epc(self, request: WriteEpcRequest | Mapping[str, Any]) -> ServiceResponse:
        """Cambia EPC solo dopo inventory con un unico tag atteso."""
        operation = "write_epc"
        try:
            if isinstance(request, Mapping):
                request = WriteEpcRequest.from_mapping(request)
            password = _parse_hex(
                request.access_password_hex,
                "access_password_hex",
                exact_bytes=4,
            )
            new_epc_bytes = bytes.fromhex(request.new_epc)
            with self._lock:
                reader = self._require_reader()
                if self._observed_epcs != frozenset({request.expected_epc}):
                    raise UnsafeWriteError(
                        "cambio EPC bloccato: l'ultimo inventory non contiene "
                        f"solo l'EPC atteso {request.expected_epc}"
                    )
                results = reader.write_epc_try_all_antennas(
                    list(request.antennas),
                    new_epc_bytes,
                    password,
                    request.timeout_ms,
                )
                success_antenna = next(
                    (antenna for antenna, result in results.items() if result.get("ok")),
                    None,
                )
                if success_antenna is not None:
                    self._observed_epcs = frozenset()
            data = {
                "request": _json_safe(request),
                "previous_epc": request.expected_epc,
                "new_epc": request.new_epc,
                "success_antenna": success_antenna,
                "verification_required": True,
                "results": _json_safe(results),
            }
            if success_antenna is None:
                return self._failure(
                    operation,
                    RuntimeError(f"scrittura EPC fallita: {data['results']}"),
                    data,
                )
            self._emit(
                "tag.epc.changed",
                {
                    "previous_epc": request.expected_epc,
                    "new_epc": request.new_epc,
                    "antenna": success_antenna,
                    "verified": False,
                },
            )
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def verify(
        self,
        request: ReadRequest | Mapping[str, Any],
        expected_data_hex: str,
    ) -> ServiceResponse:
        """Rilegge una banca e confronta ogni esito con i dati attesi."""
        operation = "verify"
        try:
            response = self.read(request)
            if not response.ok:
                message = response.error["message"] if response.error else "lettura fallita"
                return self._failure(operation, RuntimeError(str(message)), response.data)
            expected = _parse_hex(expected_data_hex, "expected_data_hex").hex().upper()
            results = copy.deepcopy(dict(response.data["results"]))
            matches = []
            for result in results.values():
                if result.get("ok"):
                    result["match"] = result.get("data", "").upper() == expected
                    matches.append(bool(result["match"]))
            data = {"expected_data_hex": expected, "results": results}
            if not matches or not all(matches):
                return self._failure(
                    operation,
                    RuntimeError(f"verifica dati fallita: {results}"),
                    data,
                )
            return self._success(operation, data)
        except Exception as exc:
            return self._failure(operation, exc)

    def health(self, check_antennas: bool = True) -> ServiceResponse:
        """Espone l'health check del reader nel contratto del servizio."""
        operation = "health"
        report: dict[str, Any] | None = None
        try:
            with self._lock:
                report = self._require_reader().health_check(check_antennas=check_antennas)
            if not report.get("ok", False):
                raise RuntimeError(f"health check fallito: {report}")
            return self._success(operation, {"report": report})
        except Exception as exc:
            return self._failure(operation, exc, {"report": report} if report is not None else None)

    def identify(self) -> ServiceResponse:
        """Che cosa c'e' davvero attaccato: modulo, certificazione, antenne.

        Non e' una diagnosi, e' un censimento — e serve prima di ogni misura,
        perche' **quali misure siano possibili dipende dall'esemplare**. La
        certificazione del modulo (EX10 2024-12 §2.2) decide se si possono
        selezionare bande diverse dalla propria e frequenze singole: senza
        saperlo si finisce per proporre all'operatore una procedura che il suo
        firmware rifiutera'.

        Ogni interrogazione e' isolata: un modulo che non implementa `0x72`
        (la serie SIMX600 non ha il sensore) non deve far fallire il resto.
        """
        operation = "identify"
        try:
            with self._lock:
                reader = self._require_reader()
                dati: dict[str, Any] = {
                    "transport": reader._t.describe(),
                    "firmware_info": reader.firmware_info,
                }
                for chiave, azione in (
                    ("regions_available", reader.get_available_regions),
                    ("serial_number", reader.get_serial_number),
                    ("temperature_c", reader.get_module_temperature),
                    ("antennas_connected", reader.get_antenna_connection),
                ):
                    try:
                        dati[chiave] = azione()
                    except Exception as exc:  # noqa: BLE001 - il censimento continua
                        dati[chiave] = None
                        dati.setdefault("non_disponibili", {})[chiave] = str(exc)
            return self._success(operation, dati)
        except Exception as exc:
            return self._failure(operation, exc)

    def snapshot(self) -> dict[str, Any]:
        """Stato non sensibile utile a framework, monitor e diagnostica."""
        with self._lock:
            diagnostics = self._reader.diag.snapshot() if self._reader is not None else None
            return {
                "api_version": SERVICE_API_VERSION,
                "state": self._state.value,
                "ready": self._state is ServiceState.READY,
                "observed_epcs": sorted(self._observed_epcs),
                "generated_epc_candidates": len(self._generated_epcs),
                "inventory_antennas": list(self._inventory_antennas or ()),
                "diagnostics": diagnostics,
                "last_event_sequence": self._event_sequence,
            }

    def __enter__(self) -> "RFIDService":
        response = self.start()
        if not response.ok:
            message = response.error["message"] if response.error else "avvio fallito"
            raise ServiceStateError(str(message))
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()
