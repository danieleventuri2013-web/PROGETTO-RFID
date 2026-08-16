"""Backend RFID simulato per i test di `lims`, senza hardware.

Non e' un test: e' l'attrezzatura condivisa da `test_lims_profiler` e
`test_lims_tagio`. Riproduce le regole che contano del confine reale
(`docs/CONTRATTO_SERVICE.md`), perche' e' contro quelle che il codice va provato:

* i limiti del driver: lettura fino a 96 word, scrittura fino a 64 byte di
  lunghezza pari (`reader.py:29,421-430`);
* la guardia sulla scrittura: consentita solo se l'ultimo inventory ha visto
  **esattamente** l'EPC atteso (`service.py:797-835`);
* l'azzeramento dell'EPC osservato dopo un cambio di EPC (`service.py:867`),
  che obbliga a un nuovo inventory prima di poter riscrivere;
* il fallimento della lettura quando nessuna antenna risponde.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion.service import (
    InventoryRequest,
    MemoryBank,
    ReadRequest,
    ServiceResponse,
    ServiceState,
    WriteEpcRequest,
    WriteRequest,
)

_MAX_READ_WORDS = 96
_MAX_WRITE_BYTES = 64


class SimulatedTag:
    """Un tag Gen2 con banchi di memoria di dimensione configurabile."""

    def __init__(
        self,
        epc: bytes,
        *,
        tid: bytes = bytes.fromhex("E2801190200050A1B2C3D4E5"),
        user_bytes: int = 64,
        user_initial: bytes = b"",
        visible_from: set[int] | None = None,
        min_read_power_cdbm: int = 0,
        visible_every: int = 1,
        rssi: int = -45,
    ):
        # Regole di visibilita': servono a simulare i tag difficili, quelli
        # schermati dal liquido o coperti da altri, che si leggono solo da certe
        # antenne o solo alzando la potenza. Senza di esse una strategia
        # multi-passata sembrerebbe funzionare anche se non serve a niente.
        self.visible_from = set(visible_from) if visible_from else None
        self.min_read_power_cdbm = min_read_power_cdbm
        self.visible_every = max(1, visible_every)
        self.rssi = rssi
        self.epc = bytes(epc)
        self.tid = bytes(tid)
        self.user = bytearray(user_bytes)
        self.user[: len(user_initial)] = user_initial[:user_bytes]
        self.write_count = 0
        # RESERVED: word 0..1 kill password, word 2..3 access password.
        self.reserved = bytearray(8)
        self.locked: dict[str, str] = {}

    def bank(self, bank: int) -> bytes:
        if bank == MemoryBank.TID:
            return self.tid
        if bank == MemoryBank.USER:
            return bytes(self.user)
        if bank == MemoryBank.EPC:
            # PC word fittizia seguita dall'EPC, come sul chip reale.
            return b"\x30\x00" + self.epc
        return bytes(self.reserved)

    @property
    def epc_hex(self) -> str:
        return self.epc.hex().upper()


class FakeTagBackend:
    """Implementazione di `RFIDBackend` sufficiente per `lims`."""

    def __init__(self, tags: list[SimulatedTag] | None = None, *, antennas: tuple[int, ...] = (1, 2)):
        self.tags = list(tags or [])
        self.antennas = antennas
        self.observed_epcs: frozenset[str] = frozenset()
        self.calls: list[str] = []
        self.read_failures = 0        # quante prossime letture far fallire
        self.write_failures = 0
        self.inventory_calls = 0
        self.read_calls = 0
        # Stato radio corrente, impostato da `configure` / `configure_gen2`:
        # decide quali tag rispondono all'inventory successivo.
        self.read_power_cdbm = 3000
        self.gen2: dict[str, Any] = {}
        self.configure_calls = 0
        self.started = False
        self.listeners: list[Any] = []

    def configure(self, settings) -> ServiceResponse:
        from rfid_silion.service import ReaderSettings

        if isinstance(settings, Mapping):
            settings = ReaderSettings.from_mapping(settings)
        self.calls.append("configure")
        self.configure_calls += 1
        if settings.powers:
            self.read_power_cdbm = max(p.read_power_cdbm for p in settings.powers)
        return self._ok("configure", {"region": settings.region})

    def configure_gen2(self, settings) -> ServiceResponse:
        from rfid_silion.service import Gen2Settings

        if isinstance(settings, Mapping):
            settings = Gen2Settings.from_mapping(settings)
        self.calls.append("configure_gen2")
        applicati = {
            chiave: valore
            for chiave, valore in (
                ("session", settings.session),
                ("target", settings.target),
                ("rf_mode", settings.rf_mode),
            )
            if valore is not None
        }
        self.gen2.update(applicati)
        return self._ok("configure_gen2", {"applied": applicati})

    def _visible(self, tag: SimulatedTag, antenna: int) -> bool:
        """Il tag risponde a questa antenna, a questa potenza, in questo ciclo?"""
        if tag.visible_from is not None and antenna not in tag.visible_from:
            return False
        if self.read_power_cdbm < tag.min_read_power_cdbm:
            return False
        return self.inventory_calls % tag.visible_every == 0

    # -- utilita' per i test ----------------------------------------------
    @property
    def only_tag(self) -> SimulatedTag:
        if len(self.tags) != 1:
            raise AssertionError(f"nel campo ci sono {len(self.tags)} tag, non uno")
        return self.tags[0]

    # -- ciclo di vita ------------------------------------------------------
    # Non serve a `lims`, che riceve un lettore gia' avviato, ma serve a
    # `webui`, che il lettore lo avvia lui: senza questi metodi la schermata di
    # connessione resterebbe l'unica parte non provabile senza hardware.
    @property
    def state(self) -> ServiceState:
        return ServiceState.READY if self.started else ServiceState.STOPPED

    @property
    def ready(self) -> bool:
        return self.started

    def start(self) -> ServiceResponse:
        self.calls.append("start")
        self.started = True
        return self._ok("start", {"version": "fake-1.0"})

    def stop(self) -> ServiceResponse:
        self.calls.append("stop")
        self.started = False
        return ServiceResponse(operation="stop", ok=True, state=ServiceState.STOPPED, data={})

    def health(self) -> ServiceResponse:
        self.calls.append("health")
        return self._ok("health", {"ok": self.started, "antennas": list(self.antennas)})

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "api_version": "1.2",
            "antennas": list(self.antennas),
            "tags_in_field": len(self.tags),
        }

    def subscribe(self, listener) -> Any:
        self.listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self.listeners:
                self.listeners.remove(listener)

        return unsubscribe

    def _ok(self, operation: str, data: Mapping[str, Any]) -> ServiceResponse:
        return ServiceResponse(operation=operation, ok=True, state=ServiceState.READY, data=data)

    def _ko(self, operation: str, message: str, data: Mapping[str, Any] | None = None):
        return ServiceResponse(
            operation=operation,
            ok=False,
            state=ServiceState.READY,
            data=data or {},
            error={"type": "RuntimeError", "message": message},
        )

    # -- operazioni del contratto -----------------------------------------
    def inventory(self, request: InventoryRequest | Mapping[str, Any]) -> ServiceResponse:
        if isinstance(request, Mapping):
            request = InventoryRequest.from_mapping(request)
        self.calls.append("inventory")
        self.inventory_calls += 1

        elenco = []
        for tag in self.tags:
            for antenna in request.antennas:
                if self._visible(tag, antenna):
                    elenco.append(
                        {
                            "epc": tag.epc_hex,
                            "pc": 0x3000,
                            "crc": 0x1234,
                            "read_count": 1,
                            "rssi": tag.rssi,
                            "antenna_id": antenna,
                        }
                    )
        self.observed_epcs = frozenset(record["epc"] for record in elenco)
        return self._ok(
            "inventory",
            {"tags": elenco, "unique_epcs": sorted(self.observed_epcs)},
        )

    def read(self, request: ReadRequest | Mapping[str, Any]) -> ServiceResponse:
        if isinstance(request, Mapping):
            request = ReadRequest.from_mapping(request)
        self.calls.append("read")
        self.read_calls += 1

        if not 1 <= request.word_count <= _MAX_READ_WORDS:
            return self._ko("read", f"word_count fuori intervallo: {request.word_count}")
        if self.read_failures > 0:
            self.read_failures -= 1
            return self._ko("read", "lettura fallita su tutte le antenne")
        if not self.tags:
            return self._ko("read", "nessun tag nel campo")

        if request.select_epc:
            # Filtro Select: il comando punta quell'EPC preciso. Un EPC che non
            # e' nel campo non risponde, esattamente come sul chip vero.
            tag = next(
                (item for item in self.tags if item.epc_hex == request.select_epc), None
            )
            if tag is None:
                return self._ko("read", f"nessun tag con EPC {request.select_epc}")
        else:
            # Opzione 0x05: agisce sul primo tag che risponde.
            tag = self.tags[0]
        contenuto = tag.bank(request.bank)
        inizio = request.address * 2
        fine = inizio + request.word_count * 2
        if fine > len(contenuto):
            return self._ko(
                "read",
                f"lettura oltre la fine della banca {request.bank}: "
                f"richiesti {fine} byte, disponibili {len(contenuto)}",
                {"results": {str(a): {"ok": False, "error": "memory overrun"} for a in request.antennas}},
            )
        dati = contenuto[inizio:fine].hex().upper()
        return self._ok(
            "read",
            {"results": {str(a): {"ok": True, "data": dati} for a in request.antennas}},
        )

    def _guard(self, operation: str, expected_epc: str) -> ServiceResponse | None:
        atteso = expected_epc.strip().upper()
        if not atteso:
            return self._ko(operation, "expected_epc obbligatorio")
        if self.observed_epcs != frozenset({atteso}):
            return self._ko(
                operation,
                "scrittura bloccata: l'ultimo inventory non contiene solo l'EPC "
                f"atteso {atteso}",
            )
        return None

    def write(self, request: WriteRequest | Mapping[str, Any]) -> ServiceResponse:
        if isinstance(request, Mapping):
            request = WriteRequest.from_mapping(request)
        self.calls.append("write")

        bloccato = self._guard("write", request.expected_epc)
        if bloccato is not None:
            return bloccato
        dati = bytes.fromhex(request.data_hex)
        if not dati or len(dati) % 2 or len(dati) > _MAX_WRITE_BYTES:
            return self._ko(
                "write",
                f"lunghezza dati non valida: {len(dati)} byte "
                f"(serve pari e non oltre {_MAX_WRITE_BYTES})",
            )
        if self.write_failures > 0:
            self.write_failures -= 1
            return self._ko("write", "scrittura fallita su tutte le antenne")

        tag = self.only_tag
        inizio = request.address * 2
        if request.bank == MemoryBank.USER:
            destinazione = tag.user
            nome = "user"
        elif request.bank == MemoryBank.RESERVED:
            destinazione = tag.reserved
            nome = "access_password"
        else:
            return self._ko("write", f"banca non scrivibile in questa simulazione: {request.bank}")
        if tag.locked.get(nome) in ("lock", "permalock"):
            return self._ko("write", f"banca {nome} bloccata")
        if inizio + len(dati) > len(destinazione):
            return self._ko("write", f"scrittura oltre la fine della banca {nome}")
        destinazione[inizio : inizio + len(dati)] = dati
        if request.bank == MemoryBank.USER:
            tag.write_count += 1
        return self._ok(
            "write",
            {"results": {str(a): {"ok": True} for a in request.antennas}},
        )

    def lock(self, request) -> ServiceResponse:
        from rfid_silion.service import LockRequest

        if isinstance(request, Mapping):
            request = LockRequest.from_mapping(request)
        self.calls.append("lock")

        bloccato = self._guard("lock", request.expected_epc)
        if bloccato is not None:
            return bloccato
        permanenti = request.permanent_targets
        if permanenti and not request.allow_permanent:
            return self._ko("lock", "operazione permanente senza allow_permanent")
        self.only_tag.locked.update(request.targets)
        return self._ok(
            "lock",
            {
                "results": {str(a): {"ok": True} for a in request.antennas},
                "permanent": list(permanenti),
            },
        )

    def write_epc(self, request: WriteEpcRequest | Mapping[str, Any]) -> ServiceResponse:
        if isinstance(request, Mapping):
            request = WriteEpcRequest.from_mapping(request)
        self.calls.append("write_epc")

        bloccato = self._guard("write_epc", request.expected_epc)
        if bloccato is not None:
            return bloccato
        if self.write_failures > 0:
            self.write_failures -= 1
            return self._ko("write_epc", "cambio EPC fallito su tutte le antenne")

        self.only_tag.epc = bytes.fromhex(request.new_epc)
        # Come il servizio reale: l'EPC osservato non e' piu' valido, serve un
        # nuovo inventory prima di qualunque altra scrittura.
        self.observed_epcs = frozenset()
        return self._ok(
            "write_epc",
            {
                "results": {str(a): {"ok": True} for a in request.antennas},
                "verification_required": True,
            },
        )

    def verify(
        self,
        request: ReadRequest | Mapping[str, Any],
        expected_data_hex: str,
    ) -> ServiceResponse:
        self.calls.append("verify")
        risposta = self.read(request)
        if not risposta.ok:
            return risposta
        atteso = expected_data_hex.strip().upper()
        risultati = {
            antenna: {**esito, "match": esito.get("data") == atteso}
            for antenna, esito in risposta.data["results"].items()
        }
        corrispondenze = [esito["match"] for esito in risultati.values()]
        if not corrispondenze or not all(corrispondenze):
            return self._ko("verify", "i dati riletti non coincidono", {"results": risultati})
        return self._ok("verify", {"results": risultati})
