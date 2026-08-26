"""Un lettore RFID simulato: stessa superficie, nessun hardware.

Riproduce le regole che contano del confine reale (`docs/CONTRATTO_SERVICE.md`),
perche' e' contro quelle che il codice va provato:

* i limiti del driver: lettura fino a 96 word, scrittura fino a 64 byte di
  lunghezza pari (`reader.py:29,421-430`);
* la guardia sulla scrittura: consentita solo se l'ultimo inventory ha visto
  **esattamente** l'EPC atteso (`service.py:797-835`);
* l'azzeramento dell'EPC osservato dopo un cambio di EPC (`service.py:867`),
  che obbliga a un nuovo inventory prima di poter riscrivere;
* il fallimento della lettura quando nessuna antenna risponde.

Nasce come attrezzatura dei test e li serve ancora (`tests/fake_backend.py` lo
ri-esporta), ma **non e' piu' solo roba da test**: e' anche il banco su cui gira
l'interfaccia operativa quando il lettore non c'e', seminato con i tag di una
sessione vera (`rfid_silion.scenario`, `python run.py webui --simulato`). Da qui
la promozione fuori da `tests/`: un programma che si avvia in produzione non
importa dalla cartella dei test.

Non implementa tutto il contratto — mancano `events`, `generate_epc`,
`antenna_diagnostics` — e la mancanza e' voluta: cosi' i percorsi che
pretendono un servizio completo continuano a distinguersi.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from .service import (
    InventoryRequest,
    MemoryBank,
    ReadRequest,
    ServiceResponse,
    ServiceState,
    WriteEpcRequest,
    WriteRequest,
)

__all__ = ["FakeTagBackend", "SimulatedTag"]

# ---------------------------------------------------------------------------
# La curva d'antenna simulata, ricavata dal datasheet invece che inventata
#
# La prima versione di questo modello era una parabola con un minimo netto a
# 915 MHz che saliva a VSWR 4,5 ai bordi. Il datasheet SLP1027 dice un'altra
# cosa: misurata in camera anecoica, l'antenna e' **piatta e ben adattata** su
# tutta la banda in cui e' specificata —
#
#     902 MHz: 1,24    915 MHz: 1,16    922 MHz: 1,20    928 MHz: 1,17
#
# cioe' S11 fra -20,6 e -21,8 dB. Non e' un risonatore stretto, e' un pannello
# a banda larga. Simulare un minimo aguzzo insegnava all'operatore a cercare
# una forma che questa antenna non ha.
#
# Il modello e' quello standard a risonatore singolo:
#
#     |G|^2 = ((b-1)^2 + x^2) / ((b+1)^2 + x^2),  x = Q (f/f0 - f0/f)
#
# con `b` fissato dal VSWR minimo (1,16 a 915 MHz) e `Q` calcolato per passare
# dal punto a 902 MHz (1,24). Ne esce Q ~= 5,9: un'antenna a basso Q, come ci
# si aspetta da un pannello RFID largo 220 mm.
#
# ATTENZIONE — sotto i 900 MHz questa curva e' **estrapolazione, non dato**:
# il costruttore non pubblica niente li' sotto, ed e' esattamente il motivo per
# cui la misura vera va fatta. Il modello prevede circa VSWR 1,8 a 866 MHz, ma
# un adattamento reale puo' degradare piu' in fretta di un polo singolo. Quel
# numero lo da' solo l'hardware.
#
# Il modello e' simmetrico attorno a f0, la curva vera no: a 928 MHz il
# datasheet da' 1,17 e il modello 1,24. Un polo singolo non cattura
# l'asimmetria, e non vale la pena aggiungerne un secondo per un banco di
# prova — basta sapere che la simulazione e' un po' pessimista in alto.
# ---------------------------------------------------------------------------
#: Centro della banda in cui l'antenna e' specificata (datasheet SLP1027).
RISONANZA_KHZ = 915_000
#: VSWR nel punto migliore, dal datasheet.
VSWR_RISONANZA = 1.16
#: Coefficiente di accoppiamento. Per un risonatore singolo sovraccoppiato
#: |G|min = (b-1)/(b+1), da cui VSWR minimo = b: coincidono.
_BETA = VSWR_RISONANZA
#: Q ricavato dal punto pubblicato a 902 MHz (VSWR 1,24). Vedi sopra.
_Q_ANTENNA = 5.86


def _vswr_modello(frequenza_khz: float, centro_khz: float) -> float:
    """VSWR del risonatore singolo tarato sui punti del datasheet."""
    f = frequenza_khz / centro_khz
    x = _Q_ANTENNA * (f - 1.0 / f)
    num = (_BETA - 1.0) ** 2 + x * x
    den = (_BETA + 1.0) ** 2 + x * x
    gamma = (num / den) ** 0.5
    if gamma >= 0.999:
        return 20.0
    return min((1 + gamma) / (1 - gamma), 20.0)

_MAX_READ_WORDS = 96
_MAX_WRITE_BYTES = 64


def _return_loss(vswr: float) -> float:
    """Il return loss in dB che corrisponde a un VSWR.

    E' l'inversa di `protocol.vswr_from_return_loss`: il modulo vero misura il
    return loss e da li' si ricava il VSWR, qui si parte dal VSWR voluto e si
    torna indietro, cosi' la catena di conversione viene esercitata per intero
    invece di essere scavalcata.
    """
    if vswr <= 1.0:
        return 25.5
    rapporto = (vswr + 1.0) / (vswr - 1.0)
    return min(25.5, 20.0 * math.log10(rapporto))


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
        self.gen2: dict[str, Any] = {
            "session": 0,
            "target": 0,
            "target_dynamic": False,
            "q": None,
            "q_dynamic": True,
            "rf_mode": 0x6B,
        }
        self.configure_calls = 0
        self.diagnostics_calls = 0
        #: La regione impostata, che `configure` aggiorna. Serve a riprodurre il
        #: comportamento del firmware certificato: misurare fuori dalla propria
        #: regione viene rifiutato, e smette di esserlo appena la regione cambia.
        self.region = 0x08
        #: Se vero il modulo rifiuta ogni banda diversa dalla regione corrente,
        #: come un firmware certificato per una sola regione. Spento per
        #: predefinito: e' il comportamento da accendere per provare la strada
        #: del cambio di regione.
        self.rifiuta_bande_diverse = False
        #: Le modalita' RF che il modulo sa fare davvero. `None` = tutte. Con un
        #: elenco, una modalita' fuori elenco viene **accettata e poi
        #: sostituita in silenzio**, che e' il comportamento che il manuale
        #: annuncia e che senza rilettura non si scopre mai.
        self.rf_mode_supportate: set[int] | None = None
        self.rf_mode_ripiego = 0x6B
        #: Antenne che il modulo dichiara non collegate, per provare l'avviso
        #: che oggi e' la causa piu' comune di «non legge».
        self.antenne_scollegate: set[int] = set()
        #: Le bande che il modulo simulato dichiara di accettare. Il valore
        #: predefinito imita un modulo certificato Cina (America, Cina, Europa,
        #: banda intera); `{0x08}` imita un modulo CE, che la spazzata larga
        #: non la puo' fare.
        self.regioni_simulate: set[int] = {0x01, 0x06, 0x08, 0xFF}
        #: Sposta la risonanza di tutte le antenne, per provare uno scenario
        #: diverso da quello atteso.
        self.scarto_risonanza_khz = 0
        self.started = False
        self.listeners: list[Any] = []

    def configure(self, settings) -> ServiceResponse:
        from .service import ReaderSettings

        if isinstance(settings, Mapping):
            settings = ReaderSettings.from_mapping(settings)
        self.calls.append("configure")
        self.configure_calls += 1
        self.region = int(settings.region)
        if settings.powers:
            self.read_power_cdbm = max(p.read_power_cdbm for p in settings.powers)
        return self._ok("configure", {"region": settings.region})

    def configure_gen2(self, settings) -> ServiceResponse:
        from .service import Gen2Settings

        if isinstance(settings, Mapping):
            settings = Gen2Settings.from_mapping(settings)
        self.calls.append("configure_gen2")
        applicati = {
            chiave: valore
            for chiave, valore in (
                ("session", settings.session),
                ("target", settings.target),
                ("q", settings.q),
                ("rf_mode", settings.rf_mode),
            )
            if valore is not None
        }
        if settings.target is not None:
            applicati["target_dynamic"] = settings.target_dynamic
        if settings.q_dynamic:
            applicati["q"] = None
            applicati["q_dynamic"] = True
        elif settings.q is not None:
            applicati["q_dynamic"] = False
        if (
            self.rf_mode_supportate is not None
            and applicati.get("rf_mode") is not None
            and applicati["rf_mode"] not in self.rf_mode_supportate
        ):
            # Accettata e sostituita, senza dirlo: e' il caso che rende
            # necessaria la rilettura.
            applicati["rf_mode"] = self.rf_mode_ripiego
        self.gen2.update(applicati)
        return self._ok("configure_gen2", {"applied": applicati})

    def read_gen2_settings(self) -> ServiceResponse:
        self.calls.append("read_gen2_settings")
        return self._ok("read_gen2_settings", {"settings": dict(self.gen2)})

    # -- adattamento delle antenne (0xAA4A) --------------------------------
    def antenna_diagnostics(self, request) -> ServiceResponse:
        """Misura di onda stazionaria simulata, con una risonanza fuori banda EU.

        Serve a poter guardare la parte piu' importante degli strumenti senza
        avere le antenne in mano: la curva ha il minimo a 915 MHz e peggiora
        scendendo verso gli 866, che e' esattamente cio' che ci si aspetta dalle
        SLP1027 e cio' che la misura vera dovra' confermare o smentire.

        `bande_rifiutate` riproduce l'altro comportamento che conta: un modulo
        certificato EU che respinge le altre bande. Senza, non si potrebbe
        provare la strada del cambio di regione.
        """
        from .service import AntennaDiagnosticsRequest

        if isinstance(request, Mapping):
            request = AntennaDiagnosticsRequest.from_mapping(request)
        self.calls.append("antenna_diagnostics")
        self.diagnostics_calls += 1

        if self.rifiuta_bande_diverse and request.band != self.region:
            return self._ko(
                "antenna_diagnostics",
                f"banda 0x{request.band:02X} rifiutata dal modulo (stato 0x010B): "
                f"la regione impostata e' 0x{self.region:02X}",
            )
        if request.antenna not in self.antennas:
            return self._ko(
                "antenna_diagnostics", f"antenna {request.antenna} non collegata"
            )

        frequenze = list(request.frequencies_khz) or self._banda(request.band)
        # Ogni antenna risuona un po' piu' in la' della precedente: due antenne
        # identiche che danno curve identiche renderebbero inutile il confronto,
        # che e' proprio il modo in cui si scopre un cavo difettoso.
        centro = RISONANZA_KHZ + (request.antenna - 1) * 3_000 + self.scarto_risonanza_khz
        misure = []
        for frequenza in frequenze:
            vswr = _vswr_modello(frequenza, centro)
            misure.append(
                {
                    "frequency_khz": int(frequenza),
                    "return_loss_db": round(_return_loss(vswr), 1),
                    "vswr": round(vswr, 2),
                    "ok": vswr < 7.0,
                }
            )
        peggiore = max((m["vswr"] for m in misure), default=None)
        return self._ok(
            "antenna_diagnostics",
            {
                "request": {
                    "antenna": request.antenna,
                    "band": request.band,
                    "frequencies_khz": list(request.frequencies_khz),
                },
                "measurements": misure,
                "worst_vswr": peggiore,
                "threshold": 7.0,
                "ok": peggiore is not None and peggiore < 7.0,
            },
        )

    @staticmethod
    def _banda(codice: int) -> list[int]:
        """Le frequenze che il modulo spazzerebbe per una banda, ogni 500 kHz."""
        estremi = {0x08: (865_000, 868_000)}.get(codice, (840_000, 960_000))
        inizio, fine = estremi
        passo = 500 if fine - inizio <= 10_000 else 2_000
        return list(range(inizio, fine + 1, passo))

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

    def identify(self) -> ServiceResponse:
        """Il censimento, su un banco che finge un modulo certificato Cina.

        La scelta non e' neutra: un modulo Cina accetta tutte le bande, quindi
        sul banco la spazzata larga funziona. Sul modulo vero potrebbe non
        essere cosi', ed e' esattamente per questo che la schermata esiste —
        `regioni_simulate` permette di provare anche il caso opposto.
        """
        self.calls.append("identify")
        return self._ok(
            "identify",
            {
                "transport": {"description": "banco simulato"},
                "firmware_info": {"version": "fake-1.0", "model": "SIM7200 simulato"},
                "regions_available": sorted(self.regioni_simulate),
                "serial_number": "5349-4D55-4C41-544F",
                "temperature_c": 41,
                "antennas_connected": [
                    a for a in self.antennas if a not in self.antenne_scollegate
                ],
            },
        )

    def health(self, check_antennas: bool = True) -> ServiceResponse:
        """Stesse chiavi di `reader.health_check`, altrimenti non prova niente.

        Un banco che risponde con nomi diversi da quelli veri fa sembrare
        funzionante un'interfaccia che sull'hardware leggerebbe campi vuoti.
        """
        self.calls.append("health")
        return self._ok(
            "health",
            {
                "ok": self.started,
                "booted": self.started,
                "transport": {"description": "banco simulato"},
                "firmware_info": {"version": "fake-1.0", "model": "SIM7200 simulato"},
                # `antenne_scollegate` permette di provare il caso che conta:
                # un'antenna in configurazione che non risulta collegata.
                "antennas_connected": [
                    a for a in self.antennas if a not in self.antenne_scollegate
                ]
                if check_antennas
                else None,
                "counters": {
                    "commands_sent": len(self.calls),
                    "responses_ok": len(self.calls),
                    "no_tag_events": 0,
                    "status_errors": 0,
                    "frame_errors": 0,
                    "timeouts": 0,
                    "transport_errors": 0,
                    "bytes_discarded": 0,
                    "last_error": None,
                    "last_error_at": None,
                },
            },
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "api_version": "1.4",
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
        from .service import LockRequest

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
