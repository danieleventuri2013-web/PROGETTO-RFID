"""Orchestrazione delle operazioni sul tag: scrittura di un contenitore e lettura di un carico.

Sta sopra `RFIDBackend` e non conosce ne' frame ne' porte seriali. L'ordine dei
passi non e' arbitrario: e' imposto dalle guardie del servizio, che vanno
assecondate e non aggirate.

**Scrittura** (`provision`): l'inventory iniziale deve vedere un tag solo, poi si
legge il TID, si scrive il nuovo EPC, si rifa' l'inventory (il cambio di EPC
invalida la guardia, `service.py:867`), si scrive il payload sigillato e infine
lo si rilegge per verifica. Il payload e' sigillato **dopo** il cambio di EPC,
perche' EPC e TID sono entrambi dati autenticati.

**Lettura** (`survey_field`): qui c'e' un limite del driver da conoscere. I
comandi di lettura usano l'opzione 0x05, che agisce sul *primo tag che risponde*:
con piu' tag nel campo non si puo' scegliere quale interrogare, perche' il filtro
Select non e' implementato (e' lo Step 2 di `PIANO_PROGETTO.md:227`). Di
conseguenza il payload cifrato si legge solo a tag singolo.

Questo non impedisce il controllo che serve di piu' in ricezione: **l'EPC e' in
chiaro e contiene numerazione e accettazione**, quindi i contenitori mancanti si
individuano da un solo inventory, senza decifrare nulla e senza possedere la chiave.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from .codec import (
    EpcInfo,
    TagPayload,
    build_epc,
    pack_payload,
    parse_epc,
    unpack_payload,
)
from .crypto import (
    CryptoError,
    Keyring,
    PayloadAuthenticationError,
    UnknownKeyError,
    max_plaintext_bytes,
    open_sealed,
    seal,
)
from .profiler import TID_AAD_BYTES, TID_AAD_WORDS
from .responses import (
    ServiceCallError,
    first_read,
    inventory_epcs,
    inventory_tags,
    raise_for_status,
)

__all__ = [
    "MODALITA_PAYLOAD",
    "MODALITA_SOLO_EPC",
    "MAX_WRITE_BYTES",
    "FieldSurvey",
    "MissingContainer",
    "ProvisionResult",
    "TagIO",
    "TagObservation",
]

log = logging.getLogger("lims.tagio")

#: Sul tag va lo pseudonimo **e** il campione cifrato. E' la modalita' normale:
#: il tag e' autosufficiente e il payload e' legato al TID, quindi un chip
#: copiato non passa l'autenticazione.
MODALITA_PAYLOAD = "payload"
#: Sul tag va **solo** lo pseudonimo. Per i chip senza USER memory utilizzabile.
#: Si perde il legame payload-TID, cioe' la difesa anti-clonazione; restano il
#: registro perpetuo degli EPC e la distinta firmata. I dati del paziente
#: viaggiano sulla carta.
MODALITA_SOLO_EPC = "solo_epc"

# Limite del comando 0x24: oltre 64 byte la scrittura va spezzata in blocchi
# (`reader.py:429`). La lettura ne consente 192 per volta (`reader.py:29`).
MAX_WRITE_BYTES = 64
MAX_READ_WORDS = 96


class _Backend(Protocol):
    def inventory(self, request: Any) -> Any: ...
    def read(self, request: Any) -> Any: ...
    def write(self, request: Any) -> Any: ...
    def write_epc(self, request: Any) -> Any: ...
    def lock(self, request: Any) -> Any: ...
    def verify(self, request: Any, expected_data_hex: str) -> Any: ...


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ProvisionResult:
    """Esito della scrittura di un contenitore."""

    ok: bool = False
    epc: str = ""
    previous_epc: str = ""
    tid: str = ""
    revision: int = 0
    payload_bytes: int = 0
    blocks_written: int = 0
    antenna: int | None = None
    error: str = ""
    steps: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "epc": self.epc,
            "previous_epc": self.previous_epc,
            "tid": self.tid,
            "revision": self.revision,
            "payload_bytes": self.payload_bytes,
            "blocks_written": self.blocks_written,
            "antenna": self.antenna,
            "error": self.error,
            "steps": list(self.steps),
            "timestamp": self.timestamp,
        }


@dataclass
class TagObservation:
    """Un tag visto nel campo, con quanto si e' riusciti a ricavarne."""

    epc: str
    status: str = "solo_epc"
    detail: str = ""
    antenna: int | None = None
    rssi: int | None = None
    epc_info: EpcInfo | None = None
    payload: TagPayload | None = None
    tid: str = ""

    @property
    def foreign(self) -> bool:
        return self.status == "estraneo"

    @property
    def readable(self) -> bool:
        return self.payload is not None

    def describe(self) -> dict[str, Any]:
        documento: dict[str, Any] = {
            "epc": self.epc,
            "stato": self.status,
            "dettaglio": self.detail,
            "antenna": self.antenna,
            "rssi": self.rssi,
        }
        if self.epc_info is not None:
            documento["accettazione"] = self.epc_info.accession_id
            documento["contenitore"] = self.epc_info.label
            documento["laboratorio"] = self.epc_info.lab_id
        if self.payload is not None:
            documento["campione"] = self.payload.describe()
        return documento


@dataclass(frozen=True)
class MissingContainer:
    """Un contenitore atteso in base alla numerazione ma non trovato nel campo."""

    accession_id: int
    index: int
    total: int

    @property
    def label(self) -> str:
        return f"accettazione {self.accession_id}, contenitore {self.index}/{self.total}"


@dataclass
class FieldSurvey:
    """Fotografia di quanto c'e' nel volume di lettura."""

    observations: list[TagObservation] = field(default_factory=list)
    missing: list[MissingContainer] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def known(self) -> list[TagObservation]:
        return [item for item in self.observations if not item.foreign]

    @property
    def foreign(self) -> list[TagObservation]:
        return [item for item in self.observations if item.foreign]

    @property
    def complete(self) -> bool:
        """Vero se ogni serie di contenitori vista e' completa."""
        return not self.missing and not self.error

    def by_accession(self) -> dict[int, list[TagObservation]]:
        gruppi: dict[int, list[TagObservation]] = {}
        for osservazione in self.known:
            if osservazione.epc_info is None:
                continue
            gruppi.setdefault(osservazione.epc_info.accession_id, []).append(osservazione)
        for elenco in gruppi.values():
            elenco.sort(key=lambda item: item.epc_info.container_index)  # type: ignore[union-attr]
        return gruppi

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "timestamp": self.timestamp,
            "tag_totali": len(self.observations),
            "tag_estranei": len(self.foreign),
            "completo": self.complete,
            "mancanti": [item.label for item in self.missing],
            "osservazioni": [item.describe() for item in self.observations],
        }


class TagIO:
    """Operazioni di alto livello sul tag, sopra il contratto del servizio."""

    def __init__(
        self,
        backend: _Backend,
        keyring: Keyring,
        *,
        lab_id: int = 0,
        antennas: tuple[int, ...] = (1, 2, 3),
        access_password_hex: str = "00000000",
        timeout_ms: int = 1000,
        user_memory_bytes: int = 64,
        db: Any = None,
        operator: str = "",
        modalita: str = MODALITA_PAYLOAD,
    ):
        self.backend = backend
        self.keyring = keyring
        self.lab_id = lab_id
        self.antennas = tuple(antennas)
        self.access_password_hex = access_password_hex
        self.timeout_ms = timeout_ms
        self.user_memory_bytes = user_memory_bytes
        self.db = db
        self.operator = operator
        if modalita not in (MODALITA_PAYLOAD, MODALITA_SOLO_EPC):
            raise ValueError(f"modalita' di scrittura sconosciuta: {modalita}")
        self.modalita = modalita

    @property
    def solo_epc(self) -> bool:
        """Vero se sul tag va scritto soltanto lo pseudonimo.

        Serve per i chip senza USER memory utilizzabile. Ha senso **da quando la
        distinta stampata porta i dati del paziente sulla carta**: prima, un tag
        senza payload avrebbe lasciato il laboratorio destinatario senza nessun
        modo di sapere cosa aveva in mano.
        """
        return self.modalita == MODALITA_SOLO_EPC

    # -- costruzione delle richieste --------------------------------------
    def _read_request(self, bank: int, address: int, words: int, select_epc: str = ""):
        from rfid_silion.service import ReadRequest

        return ReadRequest(
            bank=bank,
            address=address,
            word_count=words,
            antennas=self.antennas,
            access_password_hex=self.access_password_hex,
            timeout_ms=self.timeout_ms,
            select_epc=select_epc,
        )

    def _inventory(self):
        from rfid_silion.service import InventoryRequest

        return self.backend.inventory(
            InventoryRequest(antennas=self.antennas, timeout_ms=self.timeout_ms)
        )

    @property
    def payload_capacity(self) -> int:
        """Byte di payload in chiaro che entrano nella USER memory configurata."""
        return max_plaintext_bytes(self.user_memory_bytes)

    # -- registrazione ------------------------------------------------------
    def _log(self, operation: str, *, ok: bool, **campi: Any) -> None:
        if self.db is None:
            return
        try:
            self.db.log_event(operation, ok=ok, operator=self.operator, **campi)
        except Exception:  # noqa: BLE001
            # La traccia non deve mai far fallire l'operazione sul tag.
            log.exception("Registrazione dell'evento %s non riuscita", operation)

    # -- scrittura ---------------------------------------------------------
    def provision(
        self,
        payload: TagPayload,
        *,
        revision: int = 0,
        container_id: int | None = None,
        key_id: int | None = None,
        authorized_rewrite: bool = False,
        on_step: Callable[[str], None] | None = None,
    ) -> ProvisionResult:
        """Scrive pseudonimo e payload sigillato sull'unico tag presente nel campo.

        **Scrittura unica.** Un tag gia' scritto non si riscrive: il contenitore
        e' partito, o e' stato scartato, o il chip e' difettoso — in tutti e tre
        i casi riscriverlo significa sovrascrivere i dati di un altro paziente.
        Se qualcosa e' andato storto prima della spedizione, la strada e'
        annullare il contenitore e rifarlo con un tag nuovo
        (`LimsDatabase.replace_container`), non correggere quello vecchio.

        `authorized_rewrite=True` forza la riscrittura ed esiste per l'assistenza
        tecnica e per il collaudo. Viene registrato nella traccia, perche' una
        deroga che non lascia segno non e' una deroga: e' un buco.

        `on_step` riceve ogni passo **mentre accade**. Serve a un'interfaccia che
        segua la scrittura invece di ricostruirla dopo: un'animazione mossa da un
        timer racconterebbe all'operatore qualcosa che magari non e' successo.
        """
        from rfid_silion.service import MemoryBank, WriteEpcRequest

        esito = ProvisionResult(revision=revision)
        key_id = self.keyring.default_key_id if key_id is None else key_id

        def passo(testo: str) -> None:
            esito.steps.append(testo)
            if on_step is not None:
                try:
                    on_step(testo)
                except Exception:  # noqa: BLE001
                    # Un ascoltatore che si rompe non deve interrompere una
                    # scrittura gia' avviata sul tag.
                    log.exception("Callback on_step fallita")

        try:
            chiave = self.keyring.require(key_id)

            # 1. un solo tag nel campo: con l'opzione 0x05 la scrittura
            #    colpirebbe altrimenti un contenitore a caso.
            inventario = raise_for_status(self._inventory(), "inventory iniziale")
            epcs = inventory_epcs(inventario)
            if len(epcs) != 1:
                raise ServiceCallError(
                    "nella postazione di scrittura deve esserci un solo tag, "
                    f"ne sono stati visti {len(epcs)}"
                )
            esito.previous_epc = epcs[0]
            passo(f"tag riconosciuto: {esito.previous_epc}")

            # 2. TID: identita' di fabbrica su cui ancorare l'autenticazione.
            lettura_tid = raise_for_status(
                self.backend.read(self._read_request(MemoryBank.TID, 0, TID_AAD_WORDS)),
                "lettura TID",
            )
            antenna, tid_bytes = first_read(lettura_tid)
            if len(tid_bytes) < TID_AAD_BYTES:
                raise ServiceCallError(
                    f"TID di {len(tid_bytes)} byte: ne servono {TID_AAD_BYTES} per legare "
                    "il payload al chip"
                )
            tid_bytes = tid_bytes[:TID_AAD_BYTES]
            esito.tid = tid_bytes.hex().upper()
            esito.antenna = antenna
            passo(f"TID letto su antenna {antenna}: {esito.tid}")

            # Si valida lo spazio PRIMA di cambiare l'EPC. In precedenza il
            # controllo avveniva dopo: con una configurazione troppo prudente
            # il tag restava con il nuovo EPC ma senza payload, cioe' a meta'
            # della procedura mostrata all'operatore.
            chiaro = pack_payload(payload, max_bytes=self.payload_capacity)

            # Scrittura unica: il chip si interroga PRIMA di toccarlo.
            # Un archivio che non tiene il registro dei tag equivale a non averlo:
            # la guardia non si puo' applicare, e va detto invece di fallire con
            # un errore di attributo a meta' operazione.
            interroga = getattr(self.db, "get_tag", None) if self.db is not None else None
            if interroga is None and self.db is not None:
                log.warning(
                    "L'archivio non espone get_tag: impossibile verificare che il "
                    "tag non sia gia' stato scritto"
                )
            if interroga is not None:
                gia_noto = interroga(esito.tid)
                stato = (gia_noto or {}).get("state")
                if gia_noto is not None and stato != "free":
                    if not authorized_rewrite:
                        raise ServiceCallError(
                            f"il tag {esito.tid} risulta gia' scritto (stato '{stato}'): "
                            "riscriverlo sovrascriverebbe i dati di un altro campione. "
                            "Annullare il contenitore e usarne uno nuovo, oppure "
                            "autorizzare esplicitamente la riscrittura."
                        )
                    self._log(
                        "authorized_rewrite",
                        ok=True,
                        tid=esito.tid,
                        detail=f"riscrittura autorizzata di un tag in stato '{stato}'",
                    )
                    passo(f"riscrittura autorizzata (stato precedente: {stato})")

            # 3. nuovo pseudonimo, diverso da quello attuale.
            nuovo_epc = build_epc(
                self.lab_id,
                payload.accession_id,
                payload.container_index,
                payload.container_total,
            )
            while nuovo_epc.hex().upper() == esito.previous_epc:
                nuovo_epc = build_epc(
                    self.lab_id,
                    payload.accession_id,
                    payload.container_index,
                    payload.container_total,
                )
            esito.epc = nuovo_epc.hex().upper()

            if self.db is not None and container_id is not None:
                # Prenota l'EPC prima di toccare il tag: un duplicato scoperto
                # dopo la scrittura lascerebbe due contenitori indistinguibili.
                self.db.assign_epc(container_id, esito.epc, esito.tid, revision)

            # 4. cambio EPC (azzera la guardia del servizio).
            raise_for_status(
                self.backend.write_epc(
                    WriteEpcRequest(
                        new_epc=esito.epc,
                        expected_epc=esito.previous_epc,
                        antennas=self.antennas,
                        access_password_hex=self.access_password_hex,
                        timeout_ms=self.timeout_ms,
                    )
                ),
                "cambio EPC",
            )
            passo(f"EPC scritto: {esito.epc}")

            # 5. nuovo inventory: riarma la guardia e conferma il cambio.
            conferma = raise_for_status(self._inventory(), "inventory di conferma")
            visti = inventory_epcs(conferma)
            if visti != [esito.epc]:
                raise ServiceCallError(
                    f"dopo il cambio EPC il campo mostra {visti or 'nessun tag'} "
                    f"invece del solo {esito.epc}"
                )
            passo("nuovo EPC confermato")

            if self.solo_epc:
                # 6-8 non hanno oggetto: questo chip non ha una USER memory in
                # cui mettere il campione. Il contenitore resta identificato
                # dall'EPC, e cosa contiene lo dice la distinta.
                passo("solo EPC: questo tag non porta il campione")
                esito.payload_bytes = 0
                esito.blocks_written = 0
                if self.db is not None and container_id is not None:
                    self.db.mark_provisioned(container_id, esito.tid, revision)
                    self.db.assign_tag(
                        esito.tid,
                        container_id,
                        esito.epc,
                        revision,
                        allow_rewrite=authorized_rewrite,
                    )
                    self.db.clear_tag_failures(esito.tid)
                esito.ok = True
                self._log(
                    "provision",
                    ok=True,
                    epc=esito.epc,
                    tid=esito.tid,
                    container_id=container_id,
                    antenna=antenna,
                    detail="solo EPC (nessuna USER memory)",
                )
                log.info("Contenitore scritto in sola identita': EPC %s", esito.epc)
                return esito

            # 6. sigillo: EPC e TID sono dati autenticati, quindi si sigilla ora.
            sigillato = seal(
                chiaro,
                epc=nuovo_epc,
                tid=tid_bytes,
                key=chiave,
                key_id=key_id,
                revision=revision,
            )
            if len(sigillato) > self.user_memory_bytes:
                raise ServiceCallError(
                    f"payload di {len(sigillato)} byte: non entra nei "
                    f"{self.user_memory_bytes} byte di USER memory dichiarati"
                )
            esito.payload_bytes = len(sigillato)

            # 7. scrittura, eventualmente in piu' blocchi da 64 byte.
            esito.blocks_written = self._write_blocks(sigillato, esito.epc)
            blocchi = "blocco" if esito.blocks_written == 1 else "blocchi"
            passo(f"payload scritto: {len(sigillato)} byte in {esito.blocks_written} {blocchi}")

            # 8. rilettura di verifica: senza questa la scrittura resta un'ipotesi.
            self._verify_blocks(sigillato)
            passo("payload riletto e verificato")

            if self.db is not None and container_id is not None:
                self.db.mark_provisioned(container_id, esito.tid, revision)
                # Il chip entra nel registro solo ORA, a scrittura verificata:
                # un tag marcato come usato per una scrittura poi fallita
                # sarebbe buttato via per niente.
                self.db.assign_tag(
                    esito.tid,
                    container_id,
                    esito.epc,
                    revision,
                    allow_rewrite=authorized_rewrite,
                )
                self.db.clear_tag_failures(esito.tid)

            esito.ok = True
            self._log(
                "provision",
                ok=True,
                epc=esito.epc,
                tid=esito.tid,
                container_id=container_id,
                antenna=antenna,
                detail=f"{esito.payload_bytes} byte",
            )
            log.info("Contenitore scritto: EPC %s, %d byte", esito.epc, esito.payload_bytes)
        except (ServiceCallError, CryptoError, ValueError) as exc:
            esito.ok = False
            esito.error = str(exc)
            self._log(
                "provision",
                ok=False,
                epc=esito.epc or esito.previous_epc,
                tid=esito.tid,
                container_id=container_id,
                detail=str(exc),
            )
            log.warning("Scrittura del contenitore non riuscita: %s", exc)
        return esito

    def _write_blocks(self, data: bytes, expected_epc: str) -> int:
        """Scrive il payload in blocchi da 64 byte a indirizzi crescenti."""
        from rfid_silion.service import MemoryBank, WriteRequest

        scritti = 0
        for offset in range(0, len(data), MAX_WRITE_BYTES):
            blocco = data[offset : offset + MAX_WRITE_BYTES]
            raise_for_status(
                self.backend.write(
                    WriteRequest(
                        bank=MemoryBank.USER,
                        address=offset // 2,
                        data_hex=blocco.hex().upper(),
                        expected_epc=expected_epc,
                        antennas=self.antennas,
                        access_password_hex=self.access_password_hex,
                        timeout_ms=self.timeout_ms,
                    )
                ),
                f"scrittura del blocco a word {offset // 2}",
            )
            scritti += 1
        return scritti

    def _verify_blocks(self, expected: bytes) -> None:
        """Rilegge e confronta, a blocchi se il payload supera una lettura sola."""
        from rfid_silion.service import MemoryBank

        limite = MAX_READ_WORDS * 2
        for offset in range(0, len(expected), limite):
            atteso = expected[offset : offset + limite]
            richiesta = self._read_request(MemoryBank.USER, offset // 2, len(atteso) // 2)
            raise_for_status(
                self.backend.verify(richiesta, atteso.hex().upper()),
                f"verifica del blocco a word {offset // 2}",
            )

    # -- blindatura del tag -------------------------------------------------
    def set_access_password(self, new_password_hex: str, *, expected_epc: str) -> ProvisionResult:
        """Scrive la password di accesso nella banca RESERVED.

        Il layout della banca RESERVED e' fisso: le prime due word sono la kill
        password, le due successive quella di accesso. Si scrive quindi a
        `address=2` — la kill password resta a zero, intoccata.

        Finche' la password resta `00000000` chiunque puo' riscrivere il tag: il
        lock da solo non basta, perche' si sblocca con la password.
        """
        from rfid_silion.service import MemoryBank, WriteRequest

        esito = ProvisionResult(epc=expected_epc.strip().upper())
        try:
            nuova = bytes.fromhex("".join(new_password_hex.split()))
            if len(nuova) != 4:
                raise ValueError("la password di accesso deve essere di 4 byte")
            raise_for_status(
                self.backend.write(
                    WriteRequest(
                        bank=MemoryBank.RESERVED,
                        address=2,          # word 2..3 = access password
                        data_hex=nuova.hex().upper(),
                        expected_epc=esito.epc,
                        antennas=self.antennas,
                        access_password_hex=self.access_password_hex,
                        timeout_ms=self.timeout_ms,
                    )
                ),
                "scrittura della password di accesso",
            )
            esito.ok = True
            esito.steps.append("password di accesso impostata")
            # Da adesso il tag risponde solo alla nuova password.
            self.access_password_hex = nuova.hex().upper()
            self._log("set_access_password", ok=True, epc=esito.epc)
        except (ServiceCallError, ValueError) as exc:
            esito.error = str(exc)
            self._log("set_access_password", ok=False, epc=esito.epc, detail=str(exc))
        return esito

    def lock_container(
        self,
        *,
        expected_epc: str,
        permanent: bool = False,
        unlock: bool = False,
        targets: Iterable[str] = ("epc", "user"),
    ) -> ProvisionResult:
        """Blocca o sblocca EPC e USER memory del contenitore.

        Con `permanent=False` il blocco e' revocabile conoscendo la password di
        accesso: e' quello che serve in laboratorio, dove un contenitore puo'
        dover essere ri-etichettato. `permanent=True` e' definitivo e va usato
        solo se si accetta di buttare il tag in caso di errore.

        `unlock=True` fa l'operazione inversa, ed e' il primo passo della rimessa
        in circolo: senza, la cancellazione della memoria verrebbe rifiutata.
        """
        from rfid_silion.service import LockRequest

        if unlock and permanent:
            raise ValueError("uno sblocco permanente rende la banca non piu' proteggibile")
        modo = "unlock" if unlock else ("permalock" if permanent else "lock")
        esito = ProvisionResult(epc=expected_epc.strip().upper())
        try:
            raise_for_status(
                self.backend.lock(
                    LockRequest(
                        targets={bersaglio: modo for bersaglio in targets},
                        expected_epc=esito.epc,
                        antennas=self.antennas,
                        access_password_hex=self.access_password_hex,
                        timeout_ms=self.timeout_ms,
                        allow_permanent=permanent,
                    )
                ),
                "lock del contenitore",
            )
            esito.ok = True
            esito.steps.append(f"banche bloccate ({modo}): {', '.join(targets)}")
            self._log("lock", ok=True, epc=esito.epc, detail=modo)
        except (ServiceCallError, ValueError) as exc:
            esito.error = str(exc)
            self._log("lock", ok=False, epc=esito.epc, detail=str(exc))
        return esito

    # -- guasti prima della spedizione ---------------------------------------
    def wipe_voided(
        self,
        *,
        expected_epc: str,
        tid: str = "",
        neutral_epc: bytes | None = None,
    ) -> ProvisionResult:
        """Neutralizza un tag di un contenitore **annullato**.

        Non e' una rimessa in circolo: il tag non si riusa. Serve al caso in cui
        il contenitore vada scartato ma il tag resti in giro — su un contenitore
        rotto da buttare, o su un'etichetta staccata — e non debba piu' rispondere
        come se appartenesse alla spedizione.

        Cancella la USER memory e azzera l'EPC. La cancellazione non e' pulizia
        formale: quei byte contengono i dati di un paziente, e un contenitore
        rotto finisce nei rifiuti, non in cassaforte.

        Richiede che il contenitore sia gia' stato annullato in archivio: e'
        `LimsDatabase.void_container` a decidere, non questo metodo.
        """
        from rfid_silion.service import WriteEpcRequest

        esito = ProvisionResult(previous_epc=expected_epc.strip().upper(), tid=tid.strip().upper())
        try:
            sblocco = self.lock_container(
                expected_epc=esito.previous_epc, unlock=True, targets=("epc", "user")
            )
            if sblocco.ok:
                esito.steps.append("banche sbloccate")
            else:
                # Un tag mai bloccato risponde comunque: non e' un errore.
                log.info("Sblocco non necessario o non riuscito: %s", sblocco.error)

            raise_for_status(self._inventory(), "inventory prima della cancellazione")
            esito.blocks_written = self._write_blocks(
                bytes(self.user_memory_bytes), esito.previous_epc
            )
            esito.steps.append(f"USER memory cancellata ({self.user_memory_bytes} byte)")

            nuovo_epc = neutral_epc or bytes(12)
            raise_for_status(self._inventory(), "inventory prima del cambio EPC")
            raise_for_status(
                self.backend.write_epc(
                    WriteEpcRequest(
                        new_epc=nuovo_epc.hex().upper(),
                        expected_epc=esito.previous_epc,
                        antennas=self.antennas,
                        access_password_hex=self.access_password_hex,
                        timeout_ms=self.timeout_ms,
                    )
                ),
                "azzeramento dell'EPC",
            )
            esito.epc = nuovo_epc.hex().upper()
            esito.steps.append("EPC azzerato")
            esito.ok = True
            self._log("wipe_voided", ok=True, epc=esito.previous_epc, tid=esito.tid)
        except (ServiceCallError, ValueError) as exc:
            esito.error = str(exc)
            self._log(
                "wipe_voided", ok=False, epc=esito.previous_epc, tid=esito.tid, detail=str(exc)
            )
            log.warning("Neutralizzazione del tag annullato non riuscita: %s", exc)
        return esito

    # -- lettura -----------------------------------------------------------
    def survey_field(self, expected_epcs: Iterable[str] | None = None) -> FieldSurvey:
        """Osserva tutti i tag nel campo e segnala i contenitori mancanti.

        `expected_epcs` e' la distinta di cio' che dovrebbe esserci. **Va passata
        ogni volta che si conosce**: senza, i mancanti si deducono dalla sola
        numerazione degli EPC letti, e di un'accettazione da tre contenitori di
        cui non se ne legge *nessuno* non resterebbe traccia — l'assenza totale
        sarebbe indistinguibile dal non essere mai partiti.

        Il payload di ogni tag viene letto isolandolo con il **filtro Select**
        sul suo EPC: e' cio' che permette di sapere cosa c'e' dentro una scatola
        piena senza aprirla e senza estrarre i contenitori uno a uno. Il
        controllo di completezza funziona comunque anche se un payload non si
        legge, perche' si basa sull'EPC.
        """
        rilievo = FieldSurvey()
        try:
            inventario = raise_for_status(self._inventory(), "inventory")
        except ServiceCallError as exc:
            rilievo.error = str(exc)
            return rilievo

        migliori: dict[str, dict[str, Any]] = {}
        for tag in inventory_tags(inventario):
            epc = str(tag.get("epc", "")).upper()
            if not epc:
                continue
            precedente = migliori.get(epc)
            if precedente is None or (tag.get("rssi") or -999) > (precedente.get("rssi") or -999):
                migliori[epc] = tag

        for epc in sorted(migliori):
            tag = migliori[epc]
            osservazione = TagObservation(
                epc=epc,
                antenna=tag.get("antenna_id"),
                rssi=tag.get("rssi"),
            )
            try:
                osservazione.epc_info = parse_epc(epc)
            except ValueError as exc:
                osservazione.status = "estraneo"
                osservazione.detail = str(exc)
                rilievo.observations.append(osservazione)
                continue

            self._load_payload(osservazione, isolate=len(migliori) > 1)
            rilievo.observations.append(osservazione)

        rilievo.missing = self._missing_containers(rilievo.observations, expected_epcs)
        self._log(
            "survey",
            ok=True,
            detail=f"{len(rilievo.observations)} tag, {len(rilievo.missing)} mancanti",
        )
        return rilievo

    def _load_payload(self, observation: TagObservation, *, isolate: bool = False) -> None:
        """Legge TID e USER di un tag e ne decifra il payload.

        Con `isolate` la lettura passa dal filtro Select sull'EPC osservato:
        obbligatorio quando nel campo c'e' piu' di un tag, perche' senza filtro
        il comando colpisce il primo che risponde — e il payload finirebbe
        attribuito al contenitore sbagliato, che e' l'errore peggiore possibile
        in una catena di custodia.
        """
        from rfid_silion.service import MemoryBank

        select = observation.epc if isolate else ""
        if self.solo_epc:
            # Un tag senza payload non e' un tag rotto. Chiamarlo «illeggibile»
            # manderebbe l'operatore a cercare un guasto che non c'e', e a
            # buttare via un chip che funziona.
            observation.status = "solo_epc"
            observation.detail = "questo circuito non porta il campione: sta sulla distinta"
            try:
                lettura_tid = raise_for_status(
                    self.backend.read(
                        self._read_request(MemoryBank.TID, 0, TID_AAD_WORDS, select)
                    ),
                    "lettura TID",
                )
                _, tid_bytes = first_read(lettura_tid)
                observation.tid = tid_bytes[:TID_AAD_BYTES].hex().upper()
            except ServiceCallError as exc:
                observation.detail = f"TID non letto: {exc}"
            if observation.epc_info is None:
                observation.status = "estraneo"
            return
        try:
            lettura_tid = raise_for_status(
                self.backend.read(
                    self._read_request(MemoryBank.TID, 0, TID_AAD_WORDS, select)
                ),
                "lettura TID",
            )
            _, tid_bytes = first_read(lettura_tid)
            tid_bytes = tid_bytes[:TID_AAD_BYTES]
            observation.tid = tid_bytes.hex().upper()

            parole = min(MAX_READ_WORDS, max(1, self.user_memory_bytes // 2))
            lettura = raise_for_status(
                self.backend.read(self._read_request(MemoryBank.USER, 0, parole, select)),
                "lettura USER",
            )
            _, dati = first_read(lettura)
        except ServiceCallError as exc:
            observation.status = "illeggibile"
            observation.detail = str(exc)
            return

        if observation.epc_info is None:
            observation.status = "estraneo"
            return
        try:
            chiaro, meta = open_sealed(
                dati, epc=observation.epc, tid=tid_bytes, keys=self.keyring
            )
            observation.payload = unpack_payload(chiaro, observation.epc_info)
            observation.status = "decodificato"
            observation.detail = f"chiave {meta.key_id}, revisione {meta.revision}"
        except UnknownKeyError as exc:
            observation.status = "chiave_mancante"
            observation.detail = str(exc)
        except PayloadAuthenticationError as exc:
            observation.status = "non_autenticato"
            observation.detail = str(exc)
        except (CryptoError, ValueError) as exc:
            observation.status = "illeggibile"
            observation.detail = str(exc)

    @staticmethod
    def _missing_containers(
        observations: Iterable[TagObservation],
        expected_epcs: Iterable[str] | None = None,
    ) -> list[MissingContainer]:
        """Elenca i contenitori attesi ma non trovati.

        Con `expected_epcs` il confronto e' con la distinta: e' il modo corretto,
        perche' copre anche il caso in cui di un'accettazione non arrivi
        nemmeno un contenitore.

        Senza, si ripiega sulla deduzione dalla numerazione degli EPC letti —
        utile perche' non richiede la chiave di cifratura ne' alcun archivio (e'
        il motivo per cui la numerazione sta nell'EPC in chiaro), ma cieca su
        cio' che non ha visto affatto.
        """
        presenti: dict[int, set[int]] = {}
        letti: set[str] = set()
        for osservazione in observations:
            letti.add(osservazione.epc)
            info = osservazione.epc_info
            if info is not None:
                presenti.setdefault(info.accession_id, set()).add(info.container_index)

        if expected_epcs is not None:
            mancanti: list[MissingContainer] = []
            for epc in sorted({str(e).strip().upper() for e in expected_epcs if e}):
                if epc in letti:
                    continue
                try:
                    info = parse_epc(epc)
                except ValueError:
                    continue  # una distinta con EPC estranei non e' affar nostro
                mancanti.append(
                    MissingContainer(info.accession_id, info.container_index, info.container_total)
                )
            return mancanti

        attesi: dict[int, int] = {}
        for osservazione in observations:
            info = osservazione.epc_info
            if info is not None:
                attesi[info.accession_id] = max(
                    attesi.get(info.accession_id, 0), info.container_total
                )

        dedotti: list[MissingContainer] = []
        for accession_id, totale in sorted(attesi.items()):
            trovati = presenti.get(accession_id, set())
            for indice in range(1, totale + 1):
                if indice not in trovati:
                    dedotti.append(MissingContainer(accession_id, indice, totale))
        return dedotti
