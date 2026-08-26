"""Rilegge un diario di prototipazione e lo racconta.

Un file JSON Lines da qualche migliaio di righe non si legge a occhio. Qui ci
sono le due domande che si fanno davvero dopo una sessione:

* **cosa e' successo** — la sequenza, in italiano, con i tempi
  (``--timeline``);
* **com'e' andata** — quante operazioni, quali sono fallite, quali tag ha
  visto la radio e con che forza (il modo predefinito);
* e infine **rifacciamola** — da una sessione vera si estrae uno scenario che
  rimette in piedi lo stesso campo senza lettore (``--scenario``).

Non c'e' hardware di mezzo: si legge un file. E' pensato apposta per il momento
in cui il lettore non e' collegato e l'unica cosa che resta della prova di ieri
e' il diario.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion.diario import leggi_diario  # noqa: E402
from rfid_silion.scenario import scenario_da_record  # noqa: E402

DIR_PREDEFINITA = Path("logs/diario")


def trova_diario(indicato: str | None) -> Path:
    """Il file da leggere: quello indicato, altrimenti l'ultimo scritto.

    `ultimo.txt` viene aggiornato a ogni apertura, quindi risponde anche
    mentre la sessione e' ancora in corso; se manca si ripiega sul file piu'
    recente, che e' la stessa risposta per altra via.
    """
    if indicato:
        percorso = Path(indicato)
        if percorso.is_dir():
            return trova_diario(str(percorso / "ultimo.txt"))
        if percorso.name == "ultimo.txt" and percorso.exists():
            nome = percorso.read_text(encoding="utf-8").strip()
            return percorso.parent / nome
        if not percorso.exists():
            raise SystemExit(f"diario non trovato: {percorso}")
        return percorso

    segnalibro = DIR_PREDEFINITA / "ultimo.txt"
    if segnalibro.exists():
        candidato = DIR_PREDEFINITA / segnalibro.read_text(encoding="utf-8").strip()
        if candidato.exists():
            return candidato
    file = sorted(DIR_PREDEFINITA.glob("diario_*.jsonl"))
    if not file:
        raise SystemExit(
            f"nessun diario in {DIR_PREDEFINITA}. "
            "Si scrive da solo quando parte l'interfaccia operativa."
        )
    return file[-1]


def _ora(record: dict[str, Any]) -> str:
    """Solo l'orario: la data e' quella del file e si ripeterebbe su ogni riga."""
    return str(record.get("t", ""))[11:23]


def _riga_timeline(record: dict[str, Any]) -> str:
    canale = record.get("canale", "?")
    nome = record.get("nome", "?")
    dati = record.get("dati") or {}
    durata = record.get("durata_ms")
    coda = f"  [{durata:.0f} ms]" if isinstance(durata, (int, float)) else ""

    if canale == "ui" and nome == "clic":
        dettaglio = f"«{dati.get('testo', '')}»"
        if dati.get("disabilitato"):
            dettaglio += " (disabilitato)"
    elif canale == "ui" and nome == "campo":
        dettaglio = f"{dati.get('id', '')} = {dati.get('valore', '')!r}"
    elif canale == "ui" and nome == "schermata":
        dettaglio = f"{dati.get('da', '')} -> {dati.get('a', '')}"
    elif canale == "ui" and nome == "scena":
        dettaglio = f"{dati.get('stato', '')}: {dati.get('testo', '')}"
    elif canale == "api":
        stato = dati.get("stato", "")
        errore = (dati.get("risposta") or {}).get("errore", "")
        dettaglio = f"HTTP {stato}" + (f" — {errore}" if errore else "")
    elif canale == "radio":
        tag = ((dati.get("dati") or {}).get("tags")) or []
        esito = "ok" if dati.get("ok") else "FALLITA"
        dettaglio = esito + (f", {len(tag)} tag" if tag else "")
        if dati.get("errore"):
            dettaglio += f" — {dati['errore'].get('message', dati['errore'])}"
    else:
        dettaglio = ", ".join(f"{k}={v}" for k, v in list(dati.items())[:4])
    return f"{_ora(record)}  {canale:<7} {nome:<24} {dettaglio}{coda}"


def timeline(record: Iterable[dict[str, Any]]) -> None:
    for voce in record:
        print(_riga_timeline(voce))


def riassunto(record: list[dict[str, Any]], percorso: Path) -> None:
    per_canale: Counter = Counter()
    operazioni: Counter = Counter()
    clic: Counter = Counter()
    schermate: Counter = Counter()
    errori: list[str] = []
    durate: defaultdict[str, list[float]] = defaultdict(list)

    inventari = 0

    for voce in record:
        canale = voce.get("canale", "?")
        nome = str(voce.get("nome", "?"))
        dati = voce.get("dati") or {}
        per_canale[canale] += 1
        durata = voce.get("durata_ms")
        if isinstance(durata, (int, float)):
            durate[f"{canale}:{nome}"].append(float(durata))

        if canale == "api":
            operazioni[nome] += 1
            stato = dati.get("stato", 0)
            if isinstance(stato, int) and stato >= 400:
                messaggio = (dati.get("risposta") or {}).get("errore", "")
                errori.append(f"{_ora(voce)}  api {nome} -> {stato}: {messaggio}")
        elif canale == "ui":
            if nome == "clic":
                clic[str(dati.get("testo") or dati.get("id") or "?")] += 1
            elif nome == "schermata":
                schermate[str(dati.get("a", ""))] += 1
        elif canale == "radio":
            if not dati.get("ok", True):
                messaggio = dati.get("errore") or {}
                if isinstance(messaggio, dict):
                    messaggio = messaggio.get("message", "")
                errori.append(f"{_ora(voce)}  radio {nome} -> {messaggio}")
            if isinstance((dati.get("dati") or {}).get("tags"), list):
                inventari += 1

    print(f"Diario: {percorso}")
    print(f"Record: {len(record)}")
    if record:
        print(f"Da {record[0].get('t', '?')} a {record[-1].get('t', '?')}")
    print()

    print("Per canale")
    for canale, quanti in per_canale.most_common():
        print(f"  {canale:<8} {quanti:>6}")
    print()

    if operazioni:
        print("Operazioni piu' usate")
        for nome, quanti in operazioni.most_common(12):
            tempi = durate.get(f"api:{nome}", [])
            medio = f"  media {sum(tempi) / len(tempi):.0f} ms" if tempi else ""
            print(f"  {nome:<26} {quanti:>5}{medio}")
        print()

    if clic:
        print("Tasti piu' premuti")
        for testo, quanti in clic.most_common(10):
            print(f"  {testo[:50]:<52} {quanti:>4}")
        print()

    if schermate:
        print("Schermate aperte: " + ", ".join(f"{k} x{v}" for k, v in schermate.most_common()))
        print()

    # I tag li conta la ricostruzione, non un conteggio a parte: contare gli EPC
    # distinti di un log darebbe due tag dove ce n'era uno che ha cambiato EPC
    # durante la scrittura, ed e' l'errore piu' facile da fare qui.
    scenario = scenario_da_record(record)
    if scenario.tag:
        print(f"Tag visti dalla radio ({inventari} inventory)")
        print(f"  {'EPC':<26} {'letture':>9} {'antenne':>9} {'RSSI':>6}  memoria")
        for voce in sorted(scenario.tag, key=lambda v: (-v.letture, v.epc)):
            note = " ".join(str(a) for a in sorted(voce.antenne)) or "-"
            memoria = f"USER {len(voce.user)} byte" if voce.user else ""
            if voce.tid:
                memoria = (memoria + "  TID " + voce.tid[:12] + "...").strip()
            letture = f"{voce.letture}/{voce.cicli}"
            print(
                f"  {voce.epc:<26} {letture:>9} {note:>9} "
                f"{voce.rssi_tipico:>6}  {memoria}"
            )
        print()
    elif inventari:
        print(f"{inventari} inventory, nessun tag in campo\n")

    if errori:
        print(f"Errori ({len(errori)})")
        for riga in errori[:25]:
            print(f"  {riga}")
        if len(errori) > 25:
            print(f"  …e altri {len(errori) - 25}")
    else:
        print("Nessun errore registrato.")


def esporta_scenario(percorso: Path, destinazione: Path) -> int:
    """Scrive uno scenario YAML con i tag visti in questa sessione.

    Il file e' fatto per essere **modificato a mano**: si parte da quello che e'
    successo davvero e si cambia il caso che si vuole provare — un tag che
    smette di rispondere, uno che arriva da un'altra spedizione, uno mai
    scritto. E' la ragione per cui non si passa direttamente il diario a
    `--simulato`.
    """
    from rfid_silion.scenario import banco_da_diario

    scenario = banco_da_diario(percorso)
    if not scenario.tag:
        print(f"In {percorso.name} non ci sono letture della radio: niente da estrarre.")
        return 1

    righe = [
        "# Scenario per il banco simulato.",
        f"# Estratto da {percorso.name}.",
        "#",
        "# Si avvia con:  python run.py webui --simulato " + str(destinazione),
        "#",
        "# `antenne` vuoto significa «risponde da tutte». `visibile_ogni: 3`",
        "# significa un ciclo su tre, cioe' un tag difficile.",
        "tag:",
    ]
    for voce in scenario.tag:
        tag = voce.simulato()
        righe.append(f'  - epc: "{voce.epc}"')
        if voce.tid:
            righe.append(f'    tid: "{voce.tid}"')
        righe.append(f"    user_byte: {max(voce.user_bytes, len(voce.user))}")
        righe.append(f"    rssi: {voce.rssi_tipico}")
        antenne = sorted(tag.visible_from) if tag.visible_from else []
        righe.append(f"    antenne: [{', '.join(str(a) for a in antenne)}]")
        righe.append(f"    visibile_ogni: {tag.visible_every}")
        righe.append(f"    nel_campo: {'true' if voce.epc in scenario.nel_campo else 'false'}")
        righe.append(
            f"    # visto {voce.letture} volte su {voce.cicli} cicli"
            + (f", memoria USER: {len(voce.user)} byte" if voce.user else "")
        )
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    destinazione.write_text("\n".join(righe) + "\n", encoding="utf-8")
    print(f"Scenario con {len(scenario.tag)} tag scritto in {destinazione}")
    print(f"Per usarlo:  python run.py webui --simulato {destinazione}")
    return 0


def _console_indulgente() -> None:
    """Non far cadere il rapporto per un carattere che la console non sa scrivere.

    Su Windows la console usa cp1252: un nome con un carattere fuori tabella
    farebbe terminare il programma con `UnicodeEncodeError` a meta' rapporto, e
    perderebbe proprio la sessione che si stava cercando di capire. Meglio un
    punto interrogativo al posto di una lettera.
    """
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    _console_indulgente()
    parser = argparse.ArgumentParser(
        prog="run.py diario",
        description="Rilegge un diario di prototipazione.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        help="file .jsonl o cartella; senza argomento si usa l'ultima sessione",
    )
    parser.add_argument("--timeline", action="store_true", help="la sequenza, riga per riga")
    parser.add_argument(
        "--canale",
        choices=["ui", "api", "radio", "evento", "nota"],
        help="mostra un canale solo",
    )
    parser.add_argument("--epc", help="solo i record che nominano questo EPC")
    parser.add_argument(
        "--scenario",
        metavar="FILE.yaml",
        help="estrae i tag visti in uno scenario per il banco simulato",
    )
    argomenti = parser.parse_args(argv)

    percorso = trova_diario(argomenti.file)
    if argomenti.scenario:
        return esporta_scenario(percorso, Path(argomenti.scenario))

    record = list(leggi_diario(percorso))

    if argomenti.canale:
        record = [voce for voce in record if voce.get("canale") == argomenti.canale]
    if argomenti.epc:
        cercato = argomenti.epc.strip().upper()
        record = [voce for voce in record if cercato in str(voce.get("dati", "")).upper()]

    if argomenti.timeline:
        timeline(record)
    else:
        riassunto(record, percorso)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
