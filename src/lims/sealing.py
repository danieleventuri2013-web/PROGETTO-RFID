"""Sigillo di una scatola: certificare cosa contiene al momento della chiusura.

E' il cuore del requisito operativo. Due errori opposti, da evitare entrambi:

* **falso negativo** — un contenitore c'e' ma non viene letto. Si combatte con
  passate ripetute che cambiano le condizioni radio, perche' un tag perso in una
  configurazione si ritrova spesso in un'altra;
* **falso positivo** — un contenitore non c'e' ma viene contato, perche' a piena
  potenza il lettore vede anche il tavolo accanto. Si combatte anzitutto con la
  **verifica a insieme chiuso**: si sa quali EPC devono esserci, e quello che non
  e' sulla distinta finisce fra i "fuori distinta", mai nel conteggio.

Da qui la scelta di fondo: **non e' una scoperta, e' una verifica**. Cercare 34
EPC noti e' un problema molto piu' facile che elencare "tutto quello che c'e'", e
permette un criterio di arresto onesto — o si trovano tutti, o si dice quali
mancano. Il sistema non dichiara mai completo un insieme che non lo e'.

Il modulo parla solo con `RFIDBackend`: nessuna conoscenza di frame o trasporti.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Protocol, Sequence

from .codec import epc_kind, parse_box_epc
from .responses import ServiceCallError, inventory_tags, raise_for_status

__all__ = [
    "ClosureProof",
    "ReadPass",
    "SealingPolicy",
    "SealingRecord",
    "SealingSession",
    "TagEvidence",
    "default_passes",
]

log = logging.getLogger("lims.sealing")


class ClosureProof(str, Enum):
    """Come si e' stabilito che il contenitore era chiuso.

    Il campo esiste da subito anche se oggi si usa solo `OPERATOR`: quando
    arrivera' il sensore sugli agganci, i sigilli gia' registrati resteranno
    distinguibili da quelli con prova oggettiva.
    """

    OPERATOR = "operator"   # dichiarazione dell'operatore
    SENSOR = "sensor"       # sensore fisico sugli agganci (GPI 0x66)
    NONE = "none"


class _Backend(Protocol):
    def inventory(self, request: Any) -> Any: ...
    def configure(self, settings: Any) -> Any: ...
    def configure_gen2(self, settings: Any) -> Any: ...


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ReadPass:
    """Una passata di lettura con la sua configurazione radio.

    Cambiare condizione fra una passata e l'altra serve a **decorrelare i
    fallimenti**: un tag schermato a una potenza o da una certa antenna spesso
    risponde a un'altra. Ripetere venti volte la stessa passata identica aggiunge
    molto meno di quattro passate diverse.
    """

    antennas: tuple[int, ...]
    read_power_cdbm: int | None = None
    session: int | None = None
    target: int | None = None
    target_dynamic: bool = False
    rf_mode: int | None = None
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "antennas", tuple(int(a) for a in self.antennas))
        if not self.antennas:
            raise ValueError("una passata deve usare almeno un'antenna")

    def describe(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "antennas": list(self.antennas),
            "read_power_cdbm": self.read_power_cdbm,
            "session": self.session,
            "target": self.target,
            "target_dynamic": self.target_dynamic,
            "rf_mode": None if self.rf_mode is None else f"0x{self.rf_mode:02X}",
        }


def default_passes(
    antennas: Sequence[int],
    *,
    powers_cdbm: Sequence[int] = (2000, 2500, 3000),
    high_sensitivity_rf_mode: int | None = 0x71,
) -> list[ReadPass]:
    """Strategia predefinita: variare una condizione per volta.

    Alterna potenza, sottoinsiemi di antenne e sessione Gen2. Le potenze basse
    non sono uno spreco: a piena potenza i tag vicini saturano il ricevitore e
    mascherano quelli lontani, quindi una passata debole a volte trova cio' che
    una forte perde.

    L'ultima passata usa la modalita' RF ad alta sensibilita' (-93 dBm contro
    -88): e' lenta, e ha senso come rete di sicurezza sui tag piu' difficili.
    """
    antenne = tuple(int(a) for a in antennas)
    if not antenne:
        raise ValueError("serve almeno un'antenna")

    passate: list[ReadPass] = []
    for potenza in powers_cdbm:
        passate.append(
            ReadPass(
                antennas=antenne,
                read_power_cdbm=potenza,
                session=2,
                target=0,
                target_dynamic=True,
                label=f"tutte le antenne, {potenza / 100:.0f} dBm, S2 A-B",
            )
        )
        # Una passata per singola antenna a ogni livello di potenza: serve anche
        # a sapere QUALE antenna vede un tag, informazione che poi usa il
        # criterio di consenso geometrico.
        if len(antenne) > 1:
            for antenna in antenne:
                passate.append(
                    ReadPass(
                        antennas=(antenna,),
                        read_power_cdbm=potenza,
                        session=0,
                        label=f"antenna {antenna}, {potenza / 100:.0f} dBm, S0",
                    )
                )

    if high_sensitivity_rf_mode is not None:
        passate.append(
            ReadPass(
                antennas=antenne,
                read_power_cdbm=max(powers_cdbm),
                session=2,
                target=0,
                target_dynamic=True,
                rf_mode=high_sensitivity_rf_mode,
                label="alta sensibilita' (-93 dBm)",
            )
        )
    return passate


@dataclass
class SealingPolicy:
    """Regole con cui si decide se un tag e' dentro e se il sigillo e' valido."""

    #: Antenne distinte che devono aver visto il tag. Con due antenne a pavimento
    #: sotto il contenitore piu' una verticale, chiedere due conferme scarta i
    #: tag appoggiati fuori, che tipicamente ne raggiungono una sola.
    min_antennas: int = 1
    #: Frazione minima di passate in cui il tag deve essere comparso.
    min_detection_rate: float = 0.0
    #: Passate consecutive con insieme invariato prima di dichiarare il sigillo.
    stable_passes: int = 2
    max_passes: int = 30
    max_seconds: float = 120.0
    #: Se falso, si accettano anche EPC non presenti in distinta. Da tenere vero:
    #: e' la difesa principale contro i tag che stanno fuori dalla scatola.
    require_expected_set: bool = True
    #: Pausa fra una passata e l'altra, per non saturare il canale.
    pause_s: float = 0.05

    def __post_init__(self) -> None:
        if self.min_antennas < 1:
            raise ValueError("min_antennas deve essere almeno 1")
        if not 0.0 <= self.min_detection_rate <= 1.0:
            raise ValueError("min_detection_rate deve stare fra 0 e 1")
        if self.stable_passes < 1:
            raise ValueError("stable_passes deve essere almeno 1")
        if self.max_passes < 1:
            raise ValueError("max_passes deve essere almeno 1")


@dataclass(frozen=True)
class TagEvidence:
    """Cosa si e' osservato di un singolo tag durante il sigillo."""

    epc: str
    observations: int
    cycles: int
    detection_rate: float
    antennas: tuple[int, ...]
    best_rssi: int | None
    accepted: bool
    reason: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "epc": self.epc,
            "osservazioni": self.observations,
            "passate": self.cycles,
            "tasso_rilevamento": round(self.detection_rate, 3),
            "antenne": list(self.antennas),
            "rssi_migliore": self.best_rssi,
            "accettato": self.accepted,
            "motivo": self.reason,
        }


@dataclass
class SealingRecord:
    """Esito del sigillo, con l'evidenza che lo giustifica.

    E' il documento che a distanza di mesi risponde a "come facevamo a sapere che
    dentro c'erano quei 34 contenitori": non solo il verdetto, ma quante passate
    sono state fatte, con che configurazione, e con quale forza ogni tag ha
    risposto.
    """

    ok: bool = False
    expected: tuple[str, ...] = ()
    found: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    passes_run: int = 0
    passes: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[TagEvidence] = field(default_factory=list)
    closure_proof: ClosureProof = ClosureProof.NONE
    #: Scatola su cui si e' fatto il sigillo, letta dal tag sul coperchio.
    #: Vuoto se il coperchio non ha tag o non e' stato visto.
    box_epc: str = ""
    box_id: int | None = None
    operator: str = ""
    stop_reason: str = ""
    error: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    def __post_init__(self) -> None:
        # Accetta sia il membro enum sia la stringa, cosi' un record ricaricato
        # da JSON si comporta come uno appena creato.
        if not isinstance(self.closure_proof, ClosureProof):
            self.closure_proof = ClosureProof(str(self.closure_proof))

    @property
    def complete(self) -> bool:
        return self.ok and not self.missing

    @property
    def counts(self) -> tuple[int, int]:
        """`(trovati, attesi)` — il numero che l'operatore confronta a colpo d'occhio."""
        return len(self.found), len(self.expected)

    def to_dict(self) -> dict[str, Any]:
        trovati, attesi = self.counts
        return {
            "ok": self.ok,
            "completo": self.complete,
            "trovati": trovati,
            "attesi": attesi,
            "expected": list(self.expected),
            "found": list(self.found),
            "missing": list(self.missing),
            "unexpected": list(self.unexpected),
            "rejected": list(self.rejected),
            "passes_run": self.passes_run,
            "passes": list(self.passes),
            "evidence": [item.describe() for item in self.evidence],
            "closure_proof": self.closure_proof.value,
            "box_epc": self.box_epc,
            "box_id": self.box_id,
            "operator": self.operator,
            "stop_reason": self.stop_reason,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class SealingSession:
    """Esegue le passate di lettura e certifica il contenuto della scatola."""

    def __init__(
        self,
        backend: _Backend,
        expected_epcs: Iterable[str],
        *,
        passes: Sequence[ReadPass],
        policy: SealingPolicy | None = None,
        region: int = 0x08,
        write_power_cdbm: int = 2000,
        timeout_ms: int = 1000,
        max_unique_epcs: int = 1000,
        operator: str = "",
        db: Any = None,
    ):
        self.backend = backend
        self.expected = tuple(sorted({str(epc).strip().upper() for epc in expected_epcs if epc}))
        self.passes = list(passes)
        if not self.passes:
            raise ValueError("serve almeno una passata di lettura")
        self.policy = policy or SealingPolicy()
        self.region = region
        self.write_power_cdbm = write_power_cdbm
        self.timeout_ms = timeout_ms
        self.max_unique_epcs = max_unique_epcs
        self.operator = operator
        self.db = db

    # -- applicazione della configurazione di una passata ------------------
    def _apply(self, passata: ReadPass) -> None:
        from rfid_silion.service import AntennaPower, Gen2Settings, ReaderSettings

        if passata.read_power_cdbm is not None:
            raise_for_status(
                self.backend.configure(
                    ReaderSettings(
                        region=self.region,
                        powers=tuple(
                            AntennaPower(
                                antenna_id=antenna,
                                read_power_cdbm=passata.read_power_cdbm,
                                write_power_cdbm=self.write_power_cdbm,
                            )
                            for antenna in passata.antennas
                        ),
                    )
                ),
                f"potenza della passata «{passata.label}»",
            )

        impostazioni = Gen2Settings(
            session=passata.session,
            target=passata.target,
            target_dynamic=passata.target_dynamic,
            rf_mode=passata.rf_mode,
        )
        if impostazioni.touches_anything:
            raise_for_status(
                self.backend.configure_gen2(impostazioni),
                f"parametri Gen2 della passata «{passata.label}»",
            )

    def _inventory(self, passata: ReadPass):
        from rfid_silion.service import InventoryRequest

        return raise_for_status(
            self.backend.inventory(
                InventoryRequest(antennas=passata.antennas, timeout_ms=self.timeout_ms)
            ),
            f"inventory della passata «{passata.label}»",
        )

    # -- valutazione --------------------------------------------------------
    def _evaluate(self, accumulatore) -> tuple[list[TagEvidence], set[str]]:
        """Applica la politica e separa i tag accettati da quelli scartati."""
        evidenze: list[TagEvidence] = []
        accettati: set[str] = set()
        for riga in accumulatore.summaries():
            motivo = ""
            accettato = True
            if epc_kind(riga.epc) == "box":
                # Il tag del coperchio e' atteso nel campo e non e' un intruso:
                # identifica la scatola, non un contenitore da contare.
                accettato, motivo = False, "tag della scatola"
            elif self.policy.require_expected_set and riga.epc not in self.expected:
                accettato, motivo = False, "fuori distinta"
            elif len(riga.antennas) < self.policy.min_antennas:
                accettato, motivo = (
                    False,
                    f"visto da {len(riga.antennas)} antenna/e, ne servono "
                    f"{self.policy.min_antennas}",
                )
            elif riga.detection_rate < self.policy.min_detection_rate:
                accettato, motivo = (
                    False,
                    f"tasso di rilevamento {riga.detection_rate:.2f} sotto la soglia "
                    f"{self.policy.min_detection_rate:.2f}",
                )
            if accettato:
                accettati.add(riga.epc)
            evidenze.append(
                TagEvidence(
                    epc=riga.epc,
                    observations=riga.observations,
                    cycles=riga.cycles,
                    detection_rate=riga.detection_rate,
                    antennas=riga.antennas,
                    best_rssi=riga.best_rssi,
                    accepted=accettato,
                    reason=motivo,
                )
            )
        return evidenze, accettati

    # -- esecuzione ---------------------------------------------------------
    def run(
        self,
        *,
        closure_proof: ClosureProof = ClosureProof.NONE,
        stop_event: Any = None,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> SealingRecord:
        """Esegue le passate fino al criterio di arresto.

        `on_progress(passata, trovati, attesi)` viene chiamato dopo ogni passata:
        serve all'interfaccia per mostrare il conteggio che sale, senza che
        questo modulo conosca la GUI.
        """
        from rfid_silion.tags import Tag, TagReadAccumulator

        record = SealingRecord(
            expected=self.expected, closure_proof=closure_proof, operator=self.operator
        )
        accumulatore = TagReadAccumulator(max_unique_epcs=self.max_unique_epcs)
        scadenza = time.monotonic() + self.policy.max_seconds
        stabili = 0
        precedente: set[str] = set()
        accettati: set[str] = set()
        evidenze: list[TagEvidence] = []

        try:
            for numero in range(1, self.policy.max_passes + 1):
                if stop_event is not None and stop_event.is_set():
                    record.stop_reason = "interrotto dall'operatore"
                    break
                if time.monotonic() > scadenza:
                    record.stop_reason = "tempo massimo raggiunto"
                    break

                passata = self.passes[(numero - 1) % len(self.passes)]
                self._apply(passata)
                risposta = self._inventory(passata)
                letti = [
                    Tag(
                        epc=str(tag.get("epc", "")).upper(),
                        pc=int(tag.get("pc") or 0),
                        crc=int(tag.get("crc") or 0),
                        read_count=tag.get("read_count"),
                        rssi=tag.get("rssi"),
                        antenna_id=tag.get("antenna_id"),
                    )
                    for tag in inventory_tags(risposta)
                    if tag.get("epc")
                ]
                accumulatore.add(letti)
                record.passes_run = numero
                record.passes.append({**passata.describe(), "pass": numero, "tags": len(letti)})

                evidenze, accettati = self._evaluate(accumulatore)
                if on_progress is not None:
                    on_progress(numero, len(accettati & set(self.expected)), len(self.expected))

                stabili = stabili + 1 if accettati == precedente else 0
                precedente = set(accettati)

                mancanti = set(self.expected) - accettati
                if not mancanti and stabili >= self.policy.stable_passes:
                    record.stop_reason = (
                        f"tutti i contenitori attesi trovati e stabili per "
                        f"{self.policy.stable_passes} passate"
                    )
                    break
                if self.policy.pause_s:
                    time.sleep(self.policy.pause_s)
            else:
                record.stop_reason = "numero massimo di passate raggiunto"
        except (ServiceCallError, ValueError) as exc:
            record.error = str(exc)
            record.stop_reason = "errore durante la lettura"
            log.warning("Sigillo interrotto: %s", exc)

        # Identita' della scatola, se il coperchio porta un tag.
        scatole = [item.epc for item in evidenze if item.reason == "tag della scatola"]
        if scatole:
            record.box_epc = sorted(scatole)[0]
            try:
                record.box_id = parse_box_epc(record.box_epc).box_id
            except ValueError:
                record.box_id = None
            if len(scatole) > 1:
                # Due coperchi nel campo: o ce ne sono due scatole, o una e' di
                # un'altra spedizione. In entrambi i casi il sigillo non regge.
                record.error = (
                    f"nel campo ci sono {len(scatole)} tag di scatola: "
                    f"{', '.join(sorted(scatole))}"
                )

        record.evidence = sorted(evidenze, key=lambda item: item.epc)
        record.found = tuple(sorted(accettati & set(self.expected)))
        record.missing = tuple(sorted(set(self.expected) - accettati))
        record.unexpected = tuple(
            sorted(item.epc for item in evidenze if item.reason == "fuori distinta")
        )
        record.rejected = tuple(
            sorted(
                item.epc
                for item in evidenze
                if not item.accepted
                and item.reason not in ("fuori distinta", "tag della scatola")
            )
        )

        # Il sigillo vale solo se tutto torna: nessun mancante, nessuna perdita
        # per eviction dell'accumulatore, nessun errore. La cautela sull'eviction
        # non e' teorica: se l'accumulatore avesse scartato EPC per limite di
        # capienza, l'insieme "trovato" sarebbe incompleto senza accorgersene.
        record.ok = (
            not record.error
            and not record.missing
            and bool(self.expected)
            and accumulatore.evicted_epcs == 0
        )
        if accumulatore.evicted_epcs:
            record.error = (
                f"{accumulatore.evicted_epcs} EPC scartati per limite di capienza "
                "dell'accumulatore: alzare max_unique_epcs"
            )
        record.finished_at = _now()

        self._log(record)
        log.info(
            "Sigillo: %d/%d contenitori in %d passate (%s)",
            *record.counts,
            record.passes_run,
            record.stop_reason,
        )
        return record

    def _log(self, record: SealingRecord) -> None:
        if self.db is None:
            return
        try:
            trovati, attesi = record.counts
            self.db.log_event(
                "sealing",
                ok=record.ok,
                operator=self.operator,
                detail=(
                    f"{trovati}/{attesi} in {record.passes_run} passate; "
                    f"mancanti={len(record.missing)} fuori_distinta={len(record.unexpected)}; "
                    f"prova={record.closure_proof.value}"
                ),
            )
        except Exception:  # noqa: BLE001
            log.exception("Registrazione del sigillo non riuscita")
