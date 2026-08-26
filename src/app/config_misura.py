"""Riporta nella configurazione quello che la profilazione ha misurato.

Il numero che conta e' `lims.user_memory_bytes`: quanti byte di USER memory
hanno davvero i tag in uso. Non si deduce dal datasheet — i datasheet dichiarano
«fino a» e i lotti variano — e se e' sbagliato la scrittura fallisce a meta' sul
primo campione vero. Finora andava trascritto a mano dal report; qui lo scrive
la profilazione stessa.

**Due decisioni non ovvie.**

1. *Si scrive il tag peggiore dello stesso chip, non l'ultimo.* La soglia deve
   reggere il tag meno capiente del lotto: se il primo tag misura 64 byte e il
   secondo 128, scrivere 128 renderebbe illeggibile il primo. Percio' il valore
   scende liberamente e **non sale** senza `--forza`. Un modello di chip diverso
   apre invece una nuova serie: mescolare il minimo del vecchio modello con i
   dati identificativi del nuovo produrrebbe una configurazione incoerente.

2. *Il file si modifica riga per riga, non si riscrive.* `config.yaml` e' pieno
   di commenti che spiegano ogni parametro: `yaml.safe_dump` li cancellerebbe
   tutti (e' quello che succede oggi con «Salva come predefinito»). Qui si
   toccano soltanto le righe interessate, e tutto il resto — commenti, ordine,
   spaziatura, fine riga CRLF — resta identico byte per byte.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

__all__ = ["CHIAVE_MODALITA", "Esito", "Misura", "aggiorna_testo", "scrivi_misura"]

#: La chiave che il codice legge davvero.
CHIAVE = "user_memory_bytes"
#: La chiave che registra da dove viene quel numero.
REGISTRO = "tag_misurato"
#: Cosa si riesce a scrivere su questi tag. La profilazione la misura, quindi
#: la scrive qui invece di lasciarla al ricordo di chi ha letto il report.
CHIAVE_MODALITA = "modalita_scrittura"

_COMMENTO_MODALITA = (
    "# Cosa si scrive sul tag: «payload» (pseudonimo + campione cifrato) oppure",
    "# «solo_epc» per i chip senza USER memory utilizzabile. In «solo_epc» i dati",
    "# del paziente viaggiano sulla distinta stampata e si perde il legame",
    "# anti-clonazione fra campione e numero di serie del chip.",
)

_COMMENTO = (
    "# Scritto da `python run.py tag-profile`: cosa e' stato misurato, su quanti",
    "# tag e quando. Il codice non lo legge, e' la prova di dove viene il numero",
    "# qui sopra. `minimo_byte` e' il tag peggiore incontrato: la soglia deve",
    "# reggere anche quello, quindi scende da sola e non sale senza --forza.",
)


@dataclass(frozen=True)
class Misura:
    """Cosa ha visto la profilazione su un singolo tag."""

    user_bytes: int
    tid_serializzato: bool
    chip: str = ""
    epc: str = ""


@dataclass(frozen=True)
class Esito:
    """Cosa e' stato fatto al file, e perche'."""

    scritto: bool
    motivo: str
    prima: int | None = None
    dopo: int | None = None
    tag_provati: int = 0


def scrivi_misura(
    percorso: str | Path,
    misura: Misura | None,
    *,
    forza: bool = False,
    adesso: dt.datetime | None = None,
    modalita_scrittura: str | None = None,
) -> Esito:
    """Aggiorna il file di configurazione. Scrittura in due tempi.

    `misura=None` con `modalita_scrittura` cambia solo la modalita': serve
    quando l'operatore decide di lavorare in sola identita' senza che sia
    arrivata una misura nuova.
    """
    percorso = Path(percorso)
    with open(percorso, "r", encoding="utf-8", newline="") as f:
        testo = f.read()

    nuovo, esito = aggiorna_testo(
        testo, misura, forza=forza, adesso=adesso, modalita_scrittura=modalita_scrittura
    )
    if not esito.scritto:
        return esito

    # Un'interruzione a meta' lascerebbe il laboratorio senza configurazione
    # invece che con quella vecchia: stessa cautela di `salva_impostazioni`.
    provvisorio = percorso.with_suffix(percorso.suffix + ".tmp")
    with open(provvisorio, "w", encoding="utf-8", newline="") as f:
        f.write(nuovo)
    provvisorio.replace(percorso)
    return esito


def aggiorna_testo(
    testo: str,
    misura: Misura | None,
    *,
    forza: bool = False,
    adesso: dt.datetime | None = None,
    modalita_scrittura: str | None = None,
) -> tuple[str, Esito]:
    """Versione pura: testo in, testo fuori. Tutta la logica sta qui."""
    if misura is None:
        if modalita_scrittura is None:
            return testo, Esito(False, "niente da scrivere")
        nuovo, cambiata = _scrivi_modalita(testo, modalita_scrittura)
        return nuovo, Esito(
            scritto=cambiata,
            motivo=f"modalita' di scrittura impostata a «{modalita_scrittura}»"
            if cambiata
            else f"la modalita' era gia' «{modalita_scrittura}»",
        )
    if misura.user_bytes <= 0:
        # Zero byte non e' una misura della memoria e non va scritto come
        # soglia. Ma e' esattamente il caso in cui la **modalita'** conta: un
        # tag senza USER memory si lavora in sola identita', e perdere quella
        # riga insieme al resto lascerebbe la configurazione a dire che il
        # campione ci sta.
        if modalita_scrittura is not None:
            nuovo, cambiata = _scrivi_modalita(testo, modalita_scrittura)
            return nuovo, Esito(
                scritto=cambiata,
                motivo=(
                    f"nessuna USER memory leggibile: la soglia resta com'era e si "
                    f"lavora in modalita' «{modalita_scrittura}»"
                )
                if cambiata
                else f"la modalita' era gia' «{modalita_scrittura}»",
            )
        return testo, Esito(False, "misura non valida: nessun byte di USER memory letto")

    dati = yaml.safe_load(testo) or {}
    lims = dati.get("lims") or {}
    attuale = lims.get(CHIAVE)
    registro = lims.get(REGISTRO) or {}
    provati = int(registro.get("tag_provati") or 0)
    minimo_noto = registro.get("minimo_byte")
    chip_noto = str(registro.get("chip") or "").strip()
    chip_misurato = str(misura.chip or "").strip()
    chip_cambiato = bool(chip_noto and chip_misurato and chip_noto != chip_misurato)

    if forza or not provati or minimo_noto is None or chip_cambiato:
        minimo = misura.user_bytes
        provati = 1
        if forza:
            motivo = f"misura imposta: la soglia riparte da {minimo} byte su questo tag"
        elif chip_cambiato:
            motivo = (
                f"chip cambiato da {chip_noto} a {chip_misurato}: "
                f"nuova serie avviata da {minimo} byte"
            )
        else:
            motivo = f"prima misura registrata: {minimo} byte"
    else:
        provati += 1
        minimo = min(int(minimo_noto), misura.user_bytes)
        if misura.user_bytes > minimo:
            motivo = (
                f"questo tag ha {misura.user_bytes} byte, ma un tag gia' misurato "
                f"ne aveva {minimo}: resta il peggiore. Con --forza si riparte da capo"
            )
        elif misura.user_bytes < int(minimo_noto):
            motivo = (
                f"questo tag ha meno memoria dei precedenti "
                f"({misura.user_bytes} contro {minimo_noto}): la soglia scende"
            )
        else:
            motivo = f"conferma la misura precedente: {minimo} byte su {provati} tag"

    righe = testo.splitlines(keepends=True)
    fine = _fine_riga(righe)
    inizio_lims, fine_lims = _sezione(righe, "lims")
    if inizio_lims is None:
        return testo, Esito(False, "nel file di configurazione non c'e' una sezione «lims»")

    riga_chiave, rientro = _riga_chiave(righe, inizio_lims, fine_lims, CHIAVE)
    if riga_chiave is None:
        return testo, Esito(
            False, f"nella sezione «lims» non c'e' la riga «{CHIAVE}»: aggiungila a mano"
        )

    righe[riga_chiave] = f"{rientro}{CHIAVE}: {minimo}{fine}"

    blocco = _righe_registro(rientro, fine, misura, minimo, provati, adesso or dt.datetime.now())
    riga_registro, _ = _riga_chiave(righe, inizio_lims, fine_lims, REGISTRO)
    if riga_registro is None:
        # Non c'era: si infila subito sotto il valore che documenta, commento
        # compreso. Cosi' anche una configurazione vecchia si aggiorna da sola.
        preambolo = [f"{rientro}{testo_commento}{fine}" for testo_commento in _COMMENTO]
        righe[riga_chiave + 1 : riga_chiave + 1] = preambolo + blocco
    else:
        righe[riga_registro : _fine_blocco(righe, riga_registro, rientro)] = blocco

    risultato = "".join(righe)
    if modalita_scrittura is not None:
        risultato, cambiata = _scrivi_modalita(risultato, modalita_scrittura)
        if cambiata:
            motivo += f"; modalita' di scrittura «{modalita_scrittura}»"

    return risultato, Esito(
        scritto=True,
        motivo=motivo,
        prima=None if attuale is None else int(attuale),
        dopo=minimo,
        tag_provati=provati,
    )


def _scrivi_modalita(testo: str, modalita: str) -> tuple[str, bool]:
    """Imposta `lims.modalita_scrittura`, aggiungendo la riga se non c'e'.

    Stessa disciplina del resto del modulo: si tocca una riga sola e tutto il
    contorno — commenti, ordine, fine riga — resta identico byte per byte.
    """
    dati = yaml.safe_load(testo) or {}
    if (dati.get("lims") or {}).get(CHIAVE_MODALITA) == modalita:
        return testo, False

    righe = testo.splitlines(keepends=True)
    fine = _fine_riga(righe)
    inizio_lims, fine_lims = _sezione(righe, "lims")
    if inizio_lims is None:
        return testo, False

    riga, rientro = _riga_chiave(righe, inizio_lims, fine_lims, CHIAVE_MODALITA)
    if riga is not None:
        righe[riga] = f"{rientro}{CHIAVE_MODALITA}: {modalita}{fine}"
        return "".join(righe), True

    # Non c'era: si mette subito sotto `user_memory_bytes`, che e' il valore di
    # cui parla, con il suo commento. Se manca anche quello, in testa a `lims`.
    ancora, rientro_ancora = _riga_chiave(righe, inizio_lims, fine_lims, CHIAVE)
    posizione = (ancora + 1) if ancora is not None else (inizio_lims + 1)
    rientro = rientro_ancora if ancora is not None else "  "
    blocco = [f"{rientro}{riga_commento}{fine}" for riga_commento in _COMMENTO_MODALITA]
    blocco.append(f"{rientro}{CHIAVE_MODALITA}: {modalita}{fine}")
    righe[posizione:posizione] = blocco
    return "".join(righe), True


# ---------------------------------------------------------------------------
# Chirurgia sul testo
# ---------------------------------------------------------------------------
def _fine_riga(righe: list[str]) -> str:
    """Il fine riga del file, per non mescolare CRLF e LF nello stesso file."""
    for riga in righe:
        if riga.endswith("\r\n"):
            return "\r\n"
        if riga.endswith("\n"):
            return "\n"
    return "\n"


def _sezione(righe: list[str], nome: str) -> tuple[int | None, int]:
    """Estremi di una sezione di primo livello: `nome:` fino alla successiva."""
    inizio: int | None = None
    for indice, riga in enumerate(righe):
        if inizio is None:
            if re.match(rf"^{re.escape(nome)}\s*:", riga):
                inizio = indice
            continue
        nudo = riga.rstrip("\r\n")
        if not nudo.strip() or nudo.lstrip().startswith("#"):
            continue
        if not nudo[:1].isspace():
            return inizio, indice
    return inizio, len(righe)


def _riga_chiave(
    righe: list[str], inizio: int, fine: int, chiave: str
) -> tuple[int | None, str]:
    for indice in range(inizio, min(fine, len(righe))):
        trovato = re.match(rf"^(\s+){re.escape(chiave)}\s*:", righe[indice])
        if trovato:
            return indice, trovato.group(1)
    return None, ""


def _fine_blocco(righe: list[str], inizio: int, rientro: str) -> int:
    """Ultima riga del blocco che comincia a `inizio`, esclusa.

    Le righe vuote in coda non fanno parte del blocco: mangiarle attaccherebbe
    la sezione seguente a questa a ogni riscrittura.
    """
    ultimo = inizio
    indice = inizio + 1
    while indice < len(righe):
        nudo = righe[indice].rstrip("\r\n")
        if nudo.strip():
            if len(nudo) - len(nudo.lstrip()) <= len(rientro):
                break
            ultimo = indice
        indice += 1
    return ultimo + 1


def _cita(valore: str) -> str:
    """Stringa YAML fra apici: dentro, un apice si raddoppia."""
    return "'" + str(valore).replace("'", "''") + "'"


def _righe_registro(
    rientro: str,
    fine: str,
    misura: Misura,
    minimo: int,
    provati: int,
    adesso: dt.datetime,
) -> list[str]:
    voci = [
        f"{rientro}{REGISTRO}:",
        f"{rientro}  chip: {_cita(misura.chip)}",
        f"{rientro}  tid_serializzato: {'true' if misura.tid_serializzato else 'false'}",
        f"{rientro}  minimo_byte: {minimo}",
        f"{rientro}  ultimo_byte: {misura.user_bytes}",
        f"{rientro}  tag_provati: {provati}",
        f"{rientro}  ultimo_epc: {_cita(misura.epc)}",
        f"{rientro}  aggiornato: {_cita(adesso.isoformat(timespec='seconds'))}",
    ]
    return [voce + fine for voce in voci]
