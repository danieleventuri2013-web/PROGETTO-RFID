"""Campagna di misura: tarare la lettura su dati, non a sentimento.

Il problema ha due facce opposte e la configurazione migliore le deve
soddisfare **entrambe**:

* leggere tutto quello che sta **dentro** la scatola (nessun falso negativo);
* non leggere niente di quello che sta **fuori** (nessun falso positivo).

La configurazione che legge di piu' in assoluto e' anche quella che legge il
tavolo accanto: alzare la potenza finche' "si vede tutto" e' il modo classico di
costruire un sistema che conta contenitori che non ci sono. Per questo la
campagna misura due tassi insieme e sceglie il massimo *a fughe nulle*.

Metodo: si dichiara l'insieme dei tag **dentro** e si posizionano di proposito
alcuni tag di controllo **fuori**, alle distanze che si vogliono escludere
(sul banco accanto, sotto il piano, nella scatola successiva). Poi si spazza la
griglia di configurazioni e si misura.

Lo stesso strumento serve in prototipazione e al collaudo dal cliente, con la
stessa procedura e lo stesso formato di report: e' cio' che rende confrontabili
due installazioni diverse.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .responses import ServiceCallError, inventory_tags, raise_for_status
from .sealing import ReadPass

__all__ = [
    "CampaignConfig",
    "CampaignReport",
    "ConfigurationResult",
    "ReadCampaign",
    "grid_passes",
]

log = logging.getLogger("lims.campaign")


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def grid_passes(
    antennas: Sequence[int],
    *,
    powers_cdbm: Sequence[int] = (1500, 2000, 2500, 3000),
    sessions: Sequence[int] = (0, 2),
    rf_modes: Sequence[int | None] = (None, 0x71),
    include_single_antenna: bool = True,
) -> list[ReadPass]:
    """Griglia di configurazioni da provare, una per combinazione.

    Le potenze partono basse di proposito: serve trovare la **piu' bassa** che
    legge tutto il contenuto, non la piu' alta che legge qualcosa in piu'. E' la
    potenza a definire fisicamente il volume di lettura.
    """
    antenne = tuple(int(a) for a in antennas)
    if not antenne:
        raise ValueError("serve almeno un'antenna")

    combinazioni: list[tuple[int, ...]] = [antenne]
    if include_single_antenna and len(antenne) > 1:
        combinazioni += [(a,) for a in antenne]

    griglia: list[ReadPass] = []
    for gruppo in combinazioni:
        for potenza in powers_cdbm:
            for sessione in sessions:
                for rf_mode in rf_modes:
                    etichetta = (
                        f"ant={'+'.join(str(a) for a in gruppo)} "
                        f"{potenza / 100:.0f}dBm S{sessione}"
                        f"{' RF=0x%02X' % rf_mode if rf_mode is not None else ''}"
                    )
                    griglia.append(
                        ReadPass(
                            antennas=gruppo,
                            read_power_cdbm=potenza,
                            session=sessione,
                            target=0,
                            target_dynamic=True,
                            rf_mode=rf_mode,
                            label=etichetta,
                        )
                    )
    return griglia


@dataclass
class CampaignConfig:
    """Parametri della campagna."""

    #: Cicli di inventory per ogni configurazione. Piu' cicli, stima piu' stabile.
    cycles_per_configuration: int = 10
    timeout_ms: int = 500
    pause_s: float = 0.02
    region: int = 0x08
    write_power_cdbm: int = 2000
    #: Geometria dichiarata del contenitore, in millimetri. Non entra nel calcolo:
    #: viene riportata nel report perche' un risultato senza le dimensioni a cui
    #: si riferisce non e' confrontabile con nulla.
    container_mm: tuple[int, int, int] | None = None
    notes: str = ""


@dataclass(frozen=True)
class ConfigurationResult:
    """Esito di una singola configurazione della griglia."""

    label: str
    configuration: dict[str, Any]
    cycles: int
    inside_expected: int
    inside_found: int
    inside_detection_rate: float
    outside_expected: int
    outside_leaked: int
    outside_leak_rate: float
    missing_inside: tuple[str, ...]
    leaked_outside: tuple[str, ...]
    per_tag_detection: dict[str, float]
    elapsed_s: float

    @property
    def clean(self) -> bool:
        """Nessuna fuga: nessun tag esterno e' stato letto neppure una volta."""
        return self.outside_leaked == 0

    @property
    def complete(self) -> bool:
        return self.inside_found == self.inside_expected and self.inside_expected > 0

    @property
    def usable(self) -> bool:
        """Configurazione accettabile: legge tutto dentro e niente fuori."""
        return self.complete and self.clean

    def describe(self) -> dict[str, Any]:
        return {
            "configurazione": self.label,
            "dettaglio": self.configuration,
            "cicli": self.cycles,
            "dentro_trovati": f"{self.inside_found}/{self.inside_expected}",
            "dentro_tasso": round(self.inside_detection_rate, 3),
            "fuori_letti": f"{self.outside_leaked}/{self.outside_expected}",
            "fuori_tasso": round(self.outside_leak_rate, 3),
            "utilizzabile": self.usable,
            "mancanti": list(self.missing_inside),
            "fughe": list(self.leaked_outside),
            "secondi": round(self.elapsed_s, 2),
        }


@dataclass
class CampaignReport:
    """Report completo, pensato per essere archiviato e riconfrontato."""

    results: list[ConfigurationResult] = field(default_factory=list)
    inside_epcs: tuple[str, ...] = ()
    outside_epcs: tuple[str, ...] = ()
    container_mm: tuple[int, int, int] | None = None
    notes: str = ""
    error: str = ""
    started_at: str = field(default_factory=_now)
    finished_at: str = ""

    @property
    def usable(self) -> list[ConfigurationResult]:
        return [item for item in self.results if item.usable]

    def best(self) -> ConfigurationResult | None:
        """La configurazione consigliata.

        Fra quelle che leggono tutto dentro senza fughe si sceglie la **potenza
        piu' bassa**: margine di rumore piu' ampio, volume di lettura piu' netto,
        meno riscaldamento. A parita' di potenza vince quella piu' veloce.
        """
        candidate = self.usable
        if not candidate:
            return None
        return min(
            candidate,
            key=lambda item: (
                item.configuration.get("read_power_cdbm") or 0,
                item.elapsed_s,
            ),
        )

    def hardest_tags(self, limit: int = 5) -> list[tuple[str, float]]:
        """I tag piu' difficili, mediati su tutte le configurazioni.

        Servono a distinguere un problema di taratura da un problema fisico: se
        lo stesso tag e' l'ultimo in ogni configurazione, il rimedio non e' la
        potenza ma la sua posizione nella scatola — o il tag stesso.
        """
        somme: dict[str, list[float]] = {}
        for risultato in self.results:
            for epc, tasso in risultato.per_tag_detection.items():
                somme.setdefault(epc, []).append(tasso)
        medie = [(epc, sum(v) / len(v)) for epc, v in somme.items() if v]
        return sorted(medie, key=lambda item: item[1])[:limit]

    def to_dict(self) -> dict[str, Any]:
        migliore = self.best()
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "container_mm": list(self.container_mm) if self.container_mm else None,
            "notes": self.notes,
            "error": self.error,
            "dentro": list(self.inside_epcs),
            "fuori": list(self.outside_epcs),
            "configurazioni_provate": len(self.results),
            "configurazioni_utilizzabili": len(self.usable),
            "consigliata": migliore.describe() if migliore else None,
            "tag_piu_difficili": [
                {"epc": epc, "tasso_medio": round(tasso, 3)}
                for epc, tasso in self.hardest_tags()
            ],
            "risultati": [item.describe() for item in self.results],
        }


class ReadCampaign:
    """Esegue la griglia di configurazioni e ne misura la resa."""

    def __init__(
        self,
        backend: Any,
        *,
        inside_epcs: Iterable[str],
        outside_epcs: Iterable[str] = (),
        config: CampaignConfig | None = None,
    ):
        self.backend = backend
        self.inside = tuple(sorted({str(e).strip().upper() for e in inside_epcs if e}))
        self.outside = tuple(sorted({str(e).strip().upper() for e in outside_epcs if e}))
        if not self.inside:
            raise ValueError("serve almeno un tag dichiarato dentro il contenitore")
        sovrapposti = set(self.inside) & set(self.outside)
        if sovrapposti:
            raise ValueError(
                f"gli stessi EPC sono dichiarati dentro e fuori: {sorted(sovrapposti)}"
            )
        self.config = config or CampaignConfig()

    def _apply(self, passata: ReadPass) -> None:
        from rfid_silion.service import AntennaPower, Gen2Settings, ReaderSettings

        if passata.read_power_cdbm is not None:
            raise_for_status(
                self.backend.configure(
                    ReaderSettings(
                        region=self.config.region,
                        powers=tuple(
                            AntennaPower(
                                antenna_id=antenna,
                                read_power_cdbm=passata.read_power_cdbm,
                                write_power_cdbm=self.config.write_power_cdbm,
                            )
                            for antenna in passata.antennas
                        ),
                    )
                ),
                f"potenza per «{passata.label}»",
            )
        impostazioni = Gen2Settings(
            session=passata.session,
            target=passata.target,
            target_dynamic=passata.target_dynamic,
            rf_mode=passata.rf_mode,
        )
        if impostazioni.touches_anything:
            raise_for_status(
                self.backend.configure_gen2(impostazioni), f"Gen2 per «{passata.label}»"
            )

    def _measure(self, passata: ReadPass) -> ConfigurationResult:
        from rfid_silion.service import InventoryRequest

        conteggi: dict[str, int] = {}
        inizio = time.monotonic()
        for _ in range(self.config.cycles_per_configuration):
            risposta = raise_for_status(
                self.backend.inventory(
                    InventoryRequest(
                        antennas=passata.antennas, timeout_ms=self.config.timeout_ms
                    )
                ),
                f"inventory per «{passata.label}»",
            )
            visti = {str(tag.get("epc", "")).upper() for tag in inventory_tags(risposta)}
            for epc in visti:
                if epc:
                    conteggi[epc] = conteggi.get(epc, 0) + 1
            if self.config.pause_s:
                time.sleep(self.config.pause_s)
        durata = time.monotonic() - inizio

        cicli = self.config.cycles_per_configuration
        per_tag = {epc: conteggi.get(epc, 0) / cicli for epc in self.inside}
        trovati = [epc for epc in self.inside if conteggi.get(epc)]
        fughe = [epc for epc in self.outside if conteggi.get(epc)]

        return ConfigurationResult(
            label=passata.label,
            configuration=passata.describe(),
            cycles=cicli,
            inside_expected=len(self.inside),
            inside_found=len(trovati),
            inside_detection_rate=(
                sum(per_tag.values()) / len(per_tag) if per_tag else 0.0
            ),
            outside_expected=len(self.outside),
            outside_leaked=len(fughe),
            outside_leak_rate=(
                sum(conteggi.get(epc, 0) for epc in self.outside) / (cicli * len(self.outside))
                if self.outside
                else 0.0
            ),
            missing_inside=tuple(epc for epc in self.inside if not conteggi.get(epc)),
            leaked_outside=tuple(fughe),
            per_tag_detection=per_tag,
            elapsed_s=durata,
        )

    def run(
        self,
        passes: Sequence[ReadPass],
        *,
        stop_event: Any = None,
        on_progress: Callable[[int, int, ConfigurationResult], None] | None = None,
    ) -> CampaignReport:
        """Misura ogni configurazione della griglia."""
        report = CampaignReport(
            inside_epcs=self.inside,
            outside_epcs=self.outside,
            container_mm=self.config.container_mm,
            notes=self.config.notes,
        )
        try:
            for indice, passata in enumerate(passes, start=1):
                if stop_event is not None and stop_event.is_set():
                    report.error = "campagna interrotta dall'operatore"
                    break
                self._apply(passata)
                risultato = self._measure(passata)
                report.results.append(risultato)
                log.info(
                    "%s -> dentro %d/%d, fuori %d/%d",
                    passata.label,
                    risultato.inside_found,
                    risultato.inside_expected,
                    risultato.outside_leaked,
                    risultato.outside_expected,
                )
                if on_progress is not None:
                    on_progress(indice, len(passes), risultato)
        except (ServiceCallError, ValueError) as exc:
            report.error = str(exc)
            log.warning("Campagna interrotta: %s", exc)
        report.finished_at = _now()
        return report
