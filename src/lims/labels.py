"""Etichette stampate per i contenitori: generazione ZPL e invio alla stampante.

L'etichetta non e' un duplicato del tag: e' l'unica parte leggibile a occhio
nudo. Se il tag si rompe, si stacca o il lettore non c'e', l'etichetta e' quello
che resta per capire di chi e' il campione — e al laboratorio destinatario ne
viene appiccicata una seconda, secondo il loro schema.

Per questo il contenuto e' scelto per essere utile **senza** RFID: paziente,
codice fiscale, accettazione, «n di N», materiale, avvertenze. Il Data Matrix
riporta l'EPC, cioe' il legame con il tag, e permette di ritrovare il record
anche con un semplice lettore di codici a barre.

Il generatore produce ZPL (Zebra), ma passa da un'interfaccia astratta: cambiare
marca di stampante non deve toccare il resto del programma. `FileLabelPrinter`
permette di provare tutto senza hardware.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .codec import SpecimenFlags, describe_fixative, describe_material, describe_site

__all__ = [
    "FileLabelPrinter",
    "LabelContent",
    "LabelPrinter",
    "LabelTemplate",
    "NetworkLabelPrinter",
    "ZPL_DEFAULT_PORT",
    "render_zpl",
]

log = logging.getLogger("lims.labels")

#: Porta di stampa RAW, standard di fatto per le stampanti di rete.
ZPL_DEFAULT_PORT = 9100

# Avvertenze da stampare in evidenza. Solo quelle che cambiano il comportamento
# di chi apre il contenitore: un elenco lungo non lo legge nessuno.
_ETICHETTE_FLAG = {
    SpecimenFlags.URGENT: "URGENTE",
    SpecimenFlags.INFECTIOUS: "RISCHIO BIOLOGICO",
    SpecimenFlags.FROZEN: "CONGELATO",
    SpecimenFlags.BIOBANK: "BIOBANCA",
}


def _zpl_escape(value: str) -> str:
    """Neutralizza i caratteri che ZPL interpreta come comandi.

    `^` e `~` iniziano un comando: lasciarli passare in un cognome
    significherebbe stampare un'etichetta storta, o non stamparla affatto.
    """
    return str(value).replace("^", " ").replace("~", " ")


@dataclass(frozen=True)
class LabelContent:
    """Cosa va stampato su un'etichetta di contenitore."""

    display_name: str
    codice_fiscale: str
    accession_id: int
    container_index: int
    container_total: int
    epc: str
    external_ref: str = ""
    material_code: int = 0
    fixative_code: int = 0
    site_code: int = 0
    data_prelievo: date | None = None
    flags: SpecimenFlags = SpecimenFlags.NONE
    lab_name: str = ""

    @property
    def label(self) -> str:
        return f"{self.container_index}/{self.container_total}"

    @property
    def warnings(self) -> list[str]:
        return [testo for flag, testo in _ETICHETTE_FLAG.items() if flag and flag in self.flags]


@dataclass(frozen=True)
class LabelTemplate:
    """Geometria dell'etichetta, in millimetri e punti per pollice."""

    width_mm: float = 50.0
    height_mm: float = 30.0
    dpi: int = 203
    margin_mm: float = 2.0
    darkness: int | None = None
    #: Copie stampate per ogni contenitore. Due sono comode quando una va sul
    #: contenitore e una sulla richiesta cartacea.
    copies: int = 1

    def __post_init__(self) -> None:
        if self.width_mm <= 0 or self.height_mm <= 0:
            raise ValueError("le dimensioni dell'etichetta devono essere positive")
        if self.dpi not in (203, 300, 600):
            raise ValueError("dpi supportati: 203, 300, 600")
        if self.copies < 1:
            raise ValueError("copies deve essere almeno 1")

    def dots(self, mm: float) -> int:
        """Converte millimetri in punti stampante."""
        return int(round(mm * self.dpi / 25.4))

    @property
    def width_dots(self) -> int:
        return self.dots(self.width_mm)

    @property
    def height_dots(self) -> int:
        return self.dots(self.height_mm)


def render_zpl(content: LabelContent, template: LabelTemplate | None = None) -> str:
    """Compone l'etichetta in ZPL.

    Il testo e' dichiarato in UTF-8 (`^CI28`): senza, i caratteri accentati di un
    cognome verrebbero stampati come simboli casuali.
    """
    template = template or LabelTemplate()
    margine = template.dots(template.margin_mm)
    larghezza = template.width_dots
    utile = larghezza - 2 * margine

    # Il Data Matrix va in alto a destra, quadrato, con il lato pari a circa un
    # terzo dell'altezza: leggibile da un lettore palmare senza rubare spazio al
    # testo, che e' la parte che serve a un umano.
    lato_dm = min(template.dots(template.height_mm / 2.4), utile // 3)
    dm_x = larghezza - margine - lato_dm
    modulo_dm = max(3, lato_dm // 22)

    grande = template.dots(3.6)
    medio = template.dots(2.6)
    piccolo = template.dots(2.0)

    righe: list[str] = ["^XA", "^CI28", f"^PW{larghezza}", f"^LL{template.height_dots}", "^LH0,0"]
    if template.darkness is not None:
        righe.append(f"^MD{template.darkness}")

    y = margine
    righe.append(
        f"^FO{margine},{y}^A0N,{grande},{grande}^FB{utile - lato_dm - 8},1,0,L^FD"
        f"{_zpl_escape(content.display_name)}^FS"
    )
    # Data Matrix con l'EPC: e' il ponte fra etichetta e tag.
    righe.append(f"^FO{dm_x},{y}^BXN,{modulo_dm},200^FD{_zpl_escape(content.epc)}^FS")

    y += grande + 6
    righe.append(f"^FO{margine},{y}^A0N,{medio},{medio}^FD{_zpl_escape(content.codice_fiscale)}^FS")

    y += medio + 6
    riferimento = f"  rif. {_zpl_escape(content.external_ref)}" if content.external_ref else ""
    righe.append(
        f"^FO{margine},{y}^A0N,{medio},{medio}^FDAcc. {content.accession_id}"
        f"{riferimento}^FS"
    )

    y += medio + 6
    # «n di N» in grande: e' il dato che l'operatore confronta a colpo d'occhio
    # con i contenitori che ha in mano.
    righe.append(
        f"^FO{margine},{y}^A0N,{grande},{grande}^FD{content.label}^FS"
    )
    materiale = describe_material(content.material_code)
    righe.append(
        f"^FO{margine + template.dots(12)},{y}^A0N,{medio},{medio}"
        f"^FB{utile - template.dots(12)},2,0,L^FD{_zpl_escape(materiale)}^FS"
    )

    y += grande + 6
    dettagli = [describe_fixative(content.fixative_code)]
    if content.site_code:
        dettagli.append(describe_site(content.site_code))
    if content.data_prelievo:
        dettagli.append(content.data_prelievo.strftime("%d/%m/%Y"))
    righe.append(
        f"^FO{margine},{y}^A0N,{piccolo},{piccolo}^FB{utile},2,0,L^FD"
        f"{_zpl_escape(' - '.join(dettagli))}^FS"
    )

    if content.warnings:
        y += piccolo + 6
        # In negativo (^FR) per essere impossibile da ignorare: chi apre il
        # contenitore deve vedere il rischio biologico prima di toccarlo.
        righe.append(
            f"^FO{margine},{y}^GB{utile},{medio + 8},{medio + 8}^FS"
            f"^FO{margine + 4},{y + 4}^A0N,{medio},{medio}^FR^FD"
            f"{_zpl_escape(' - '.join(content.warnings))}^FS"
        )

    if content.lab_name:
        righe.append(
            f"^FO{margine},{template.height_dots - margine - piccolo}"
            f"^A0N,{piccolo},{piccolo}^FD{_zpl_escape(content.lab_name)}^FS"
        )

    if template.copies > 1:
        righe.append(f"^PQ{template.copies}")
    righe.append("^XZ")
    return "\n".join(righe)


class LabelPrinter(Protocol):
    """Confine verso la stampante: cambiare marca non deve toccare il resto."""

    def send(self, payload: str) -> None: ...


@dataclass
class NetworkLabelPrinter:
    """Stampante di rete in modalita' RAW (porta 9100)."""

    host: str
    port: int = ZPL_DEFAULT_PORT
    timeout_s: float = 5.0

    def send(self, payload: str) -> None:
        with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as presa:
            presa.sendall(payload.encode("utf-8"))
        log.info("Etichetta inviata a %s:%d (%d byte)", self.host, self.port, len(payload))


@dataclass
class FileLabelPrinter:
    """Scrive lo ZPL su file invece di stamparlo.

    Serve per l'anteprima, per i test e per il collaudo prima che la stampante
    arrivi. Il file si puo' inviare a mano alla stampante, o incollare in uno dei
    visualizzatori ZPL online.
    """

    path: Path | str
    append: bool = False
    printed: list[str] = field(default_factory=list)

    def send(self, payload: str) -> None:
        percorso = Path(self.path)
        percorso.parent.mkdir(parents=True, exist_ok=True)
        with open(percorso, "a" if self.append else "w", encoding="utf-8") as f:
            f.write(payload + "\n")
        self.printed.append(payload)


def print_container_label(
    printer: LabelPrinter,
    content: LabelContent,
    template: LabelTemplate | None = None,
) -> str:
    """Compone e invia l'etichetta; ritorna lo ZPL prodotto, utile per la traccia."""
    zpl = render_zpl(content, template)
    printer.send(zpl)
    return zpl


def content_from_record(
    record: Any,
    *,
    external_ref: str = "",
    flags: SpecimenFlags = SpecimenFlags.NONE,
    lab_name: str = "",
) -> LabelContent:
    """Costruisce il contenuto dell'etichetta da un `ContainerRecord` d'archivio."""
    return LabelContent(
        display_name=record.display_name,
        codice_fiscale=record.codice_fiscale,
        accession_id=record.accession_id,
        container_index=record.index,
        container_total=record.total,
        epc=record.epc,
        external_ref=external_ref,
        material_code=record.material_code,
        fixative_code=record.fixative_code,
        site_code=record.site_code,
        data_prelievo=record.data_prelievo,
        flags=flags,
        lab_name=lab_name,
    )
