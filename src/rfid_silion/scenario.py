"""Rimette in piedi un banco a partire da una sessione vera (o da un file scritto a mano).

Il diario conserva il dato grezzo di ogni lettura: quali EPC, da quale antenna,
con che RSSI, e cosa e' stato letto o scritto nei banchi di memoria. Con quel
materiale si puo' ricostruire un `FakeTagBackend` che si comporta come il campo
di quel giorno — e allora l'interfaccia operativa si puo' far girare per intero
senza lettore, ma **sui tag veri** invece che su tag inventati.

Due sorgenti:

* un **diario** (`logs/diario/*.jsonl`) — si estrae quello che la radio ha
  visto davvero;
* uno **scenario YAML** scritto a mano — per costruire il caso che serve
  provare e che non e' ancora capitato.

Il banco tiene due posti distinti, e la distinzione e' tutto il punto:

* il **campo**, cioe' cio' che il lettore vede adesso;
* il **magazzino**, cioe' i tag che esistono ma sono fuori portata.

Spostare un tag dal magazzino al campo e' l'equivalente simulato di appoggiare
un campione sull'antenna. Senza quella distinzione non si potrebbe provare
niente di cio' che accade *mentre* qualcosa cambia — il riempimento della
scatola, la sorveglianza del piatto — che e' esattamente la parte nuova.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .diario import leggi_diario
from .simulazione import FakeTagBackend, SimulatedTag

__all__ = [
    "BancoSimulato",
    "Scenario",
    "TagRicostruito",
    "banco_da_diario",
    "carica_scenario",
    "scenario_da_record",
    "scenario_vuoto",
]

log = logging.getLogger("rfid.scenario")

#: Banchi Gen2, come in `protocol.py`. Qui servono solo per rileggere cosa
#: diceva una richiesta di lettura registrata nel diario.
_BANK_EPC = 1
_BANK_TID = 2
_BANK_USER = 3

#: Quanto e' grande la memoria USER quando il diario non lo dice. E' il valore
#: misurato sul tag in uso (`config.yaml`, `lims.user_memory_bytes`), non un
#: numero tondo scelto a caso.
USER_BYTE_PREDEFINITI = 86

#: RSSI di ripiego per un tag di cui non si e' mai registrata la forza.
_RSSI_PREDEFINITO = -50


@dataclass
class TagRicostruito:
    """Quel che si e' potuto sapere di un tag rileggendo il diario."""

    epc: str
    tid: str = ""
    user: bytes = b""
    user_bytes: int = USER_BYTE_PREDEFINITI
    #: Le antenne da cui il tag ha davvero risposto.
    antenne: set[int] = field(default_factory=set)
    #: Le antenne che hanno avuto **l'occasione** di sentirlo, cioe' quelle
    #: interrogate in un ciclo in cui il tag era nel campo. La differenza fra
    #: le due e' l'unica cosa che autorizza a dire «da li' non risponde».
    occasioni: set[int] = field(default_factory=set)
    rssi: list[int] = field(default_factory=list)
    letture: int = 0
    cicli: int = 0
    #: `None` = decidilo dalle occasioni mancate (e' il caso del diario).
    #: `True` = rispondi solo alle antenne elencate, perche' lo ha dichiarato
    #: chi ha scritto lo scenario a mano.
    limita_antenne: bool | None = None

    @property
    def rssi_tipico(self) -> int:
        """La mediana, non la media: un singolo picco non deve spostarla."""
        if not self.rssi:
            return _RSSI_PREDEFINITO
        ordinati = sorted(self.rssi)
        return ordinati[len(ordinati) // 2]

    @property
    def tasso(self) -> float:
        return self.letture / self.cicli if self.cicli else 0.0

    def simulato(self) -> SimulatedTag:
        """Il tag come lo vedra' il banco.

        Le antenne che l'hanno visto diventano quelle da cui risponde: un tag
        che nel campo vero rispondeva solo all'antenna 1 continua a farlo qui,
        e un algoritmo multi-passata che si appoggiava a quella asimmetria
        continua a essere messo alla prova.
        """
        # Un tag visto in meno di una lettura su tre e' un tag difficile: lo si
        # rende difficile anche qui, altrimenti la simulazione sarebbe piu'
        # gentile della realta' proprio dove conta.
        ogni = 1
        if 0 < self.tasso < 0.34:
            ogni = 3
        elif 0.34 <= self.tasso < 0.67:
            ogni = 2
        # Si limita la visibilita' solo se c'e' stata una vera occasione
        # mancata: un tag scritto alla postazione, dove si interroga la sola
        # antenna 3, non ha mai avuto modo di rispondere alle antenne di
        # lettura, e dichiararlo sordo a quelle lo renderebbe introvabile in un
        # sigillo che nella realta' lo troverebbe.
        limita = (
            bool(self.occasioni - self.antenne)
            if self.limita_antenne is None
            else self.limita_antenne
        )
        return SimulatedTag(
            bytes.fromhex(self.epc),
            tid=bytes.fromhex(self.tid) if self.tid else bytes(12),
            user_bytes=max(self.user_bytes, len(self.user)),
            user_initial=self.user,
            visible_from=set(self.antenne) if (limita and self.antenne) else None,
            visible_every=ogni,
            rssi=self.rssi_tipico,
        )


@dataclass
class Scenario:
    """Un banco pronto da montare: i tag e chi di loro sta nel campo."""

    tag: list[TagRicostruito] = field(default_factory=list)
    nel_campo: set[str] = field(default_factory=set)
    origine: str = ""

    def descrivi(self) -> dict[str, Any]:
        return {
            "origine": self.origine,
            "tag": len(self.tag),
            "nel_campo": len(self.nel_campo),
        }


def _hex(valore: Any) -> str:
    return str(valore or "").strip().upper()


def banco_da_diario(percorso: str | Path) -> Scenario:
    """Ricostruisce lo scenario dalle letture registrate in un diario."""
    scenario = scenario_da_record(leggi_diario(percorso))
    scenario.origine = str(percorso)
    log.info(
        "Scenario da %s: %d tag, %d nel campo",
        percorso,
        len(scenario.tag),
        len(scenario.nel_campo),
    )
    return scenario


def scenario_da_record(record: Iterable[Mapping[str, Any]]) -> Scenario:
    """Ricostruisce lo scenario da record di diario gia' letti.

    Segue anche i cambi di EPC: quando un tag viene provvisionato il suo
    identificativo cambia, e senza seguirlo si finirebbe con due tag dove nella
    realta' ce n'era uno — che e' l'errore in cui cade chiunque conti gli EPC
    distinti di un log.
    """
    conosciuti: dict[str, TagRicostruito] = {}
    # `vecchio EPC -> nuovo EPC`, per non perdere l'identita' dopo una
    # scrittura dell'EPC.
    successore: dict[str, str] = {}
    ultimo_solo: str = ""

    def risolvi(epc: str) -> str:
        """L'EPC attuale di un tag, seguendo la catena dei cambi."""
        visti = set()
        while epc in successore and epc not in visti:
            visti.add(epc)
            epc = successore[epc]
        return epc

    def voce(epc: str) -> TagRicostruito:
        epc = risolvi(epc)
        if epc not in conosciuti:
            conosciuti[epc] = TagRicostruito(epc=epc)
        return conosciuti[epc]

    for voce_diario in record:
        if voce_diario.get("canale") != "radio":
            continue
        nome = voce_diario.get("nome")
        dati = voce_diario.get("dati") or {}
        corpo = dati.get("dati") or {}
        # `RFIDService` riecheggia la richiesta dentro i dati, il banco
        # simulato no: il diario la mette da parte e qui si guarda in entrambi
        # i posti.
        richiesta = corpo.get("request") or dati.get("richiesta") or {}

        if nome == "inventory":
            tag = corpo.get("tags") or []
            visti_nel_ciclo: set[str] = set()
            for lettura in tag:
                if not isinstance(lettura, Mapping):
                    continue
                epc = _hex(lettura.get("epc"))
                if not epc:
                    continue
                elemento = voce(epc)
                visti_nel_ciclo.add(elemento.epc)
                if lettura.get("antenna_id") is not None:
                    elemento.antenne.add(int(lettura["antenna_id"]))
                if lettura.get("rssi") is not None:
                    elemento.rssi.append(int(lettura["rssi"]))
            interrogate = {int(a) for a in richiesta.get("antennas", []) or []}
            for elemento in conosciuti.values():
                elemento.cicli += 1
                if elemento.epc in visti_nel_ciclo:
                    elemento.letture += 1
                    # L'occasione conta solo se il tag era nel campo: un ciclo
                    # in cui non ha risposto da nessuna antenna non distingue
                    # «lontano dal lettore» da «schermato da quel lato».
                    elemento.occasioni |= interrogate
            unici = [_hex(e) for e in corpo.get("unique_epcs") or []]
            ultimo_solo = risolvi(unici[0]) if len(unici) == 1 else ""

        elif nome in ("read", "verify") and dati.get("ok"):
            epc = _hex(richiesta.get("select_epc")) or ultimo_solo
            if not epc:
                continue
            contenuto = _primo_risultato(corpo.get("results"))
            if contenuto is None:
                continue
            elemento = voce(epc)
            banca = int(richiesta.get("bank", 0))
            indirizzo = int(richiesta.get("address", 0))
            if banca == _BANK_TID and indirizzo == 0:
                elemento.tid = contenuto.hex().upper()
            elif banca == _BANK_USER:
                elemento.user = _innesta(elemento.user, indirizzo * 2, contenuto)

        elif nome == "write" and dati.get("ok"):
            epc = _hex(richiesta.get("expected_epc"))
            if not epc or int(richiesta.get("bank", 0)) != _BANK_USER:
                continue
            try:
                scritto = bytes.fromhex(str(richiesta.get("data_hex", "")))
            except ValueError:
                continue
            elemento = voce(epc)
            elemento.user = _innesta(elemento.user, int(richiesta.get("address", 0)) * 2, scritto)

        elif nome == "write_epc" and dati.get("ok"):
            vecchio = _hex(richiesta.get("expected_epc"))
            nuovo = _hex(richiesta.get("new_epc"))
            if not vecchio or not nuovo or vecchio == nuovo:
                continue
            attuale = risolvi(vecchio)
            elemento = conosciuti.pop(attuale, TagRicostruito(epc=attuale))
            elemento.epc = nuovo
            conosciuti[nuovo] = elemento
            successore[attuale] = nuovo
            ultimo_solo = ""

    scenario = Scenario(tag=sorted(conosciuti.values(), key=lambda voce: voce.epc))
    # Nel campo si mette cio' che alla fine della sessione era ancora letto in
    # modo affidabile. Il resto resta in magazzino, pronto a essere appoggiato.
    scenario.nel_campo = {
        elemento.epc for elemento in scenario.tag if elemento.tasso >= 0.5
    }
    return scenario


def _primo_risultato(risultati: Any) -> bytes | None:
    """I byte letti dalla prima antenna che ha risposto."""
    if not isinstance(risultati, Mapping):
        return None
    for esito in risultati.values():
        if isinstance(esito, Mapping) and esito.get("ok") and esito.get("data"):
            try:
                return bytes.fromhex(str(esito["data"]))
            except ValueError:
                return None
    return None


def _innesta(base: bytes, offset: int, pezzo: bytes) -> bytes:
    """Sovrascrive `pezzo` dentro `base` alla posizione data, allungando se serve."""
    buffer = bytearray(base)
    if len(buffer) < offset + len(pezzo):
        buffer.extend(bytes(offset + len(pezzo) - len(buffer)))
    buffer[offset : offset + len(pezzo)] = pezzo
    return bytes(buffer)


def carica_scenario(percorso: str | Path) -> Scenario:
    """Legge uno scenario scritto a mano (YAML) oppure un diario (.jsonl).

    Formato YAML::

        tag:
          - epc: "0100010000000301020304"
            tid: "E28011..."          # facoltativo
            user_byte: 86             # facoltativo
            rssi: -45                 # facoltativo
            antenne: [1, 2]           # da quali antenne risponde
            visibile_ogni: 2          # 1 = sempre, 3 = un ciclo su tre
            nel_campo: true           # se no, resta in magazzino
    """
    file = Path(percorso)
    if not file.exists():
        raise FileNotFoundError(f"scenario non trovato: {file}")
    if file.suffix.lower() == ".jsonl":
        return banco_da_diario(file)

    import yaml

    documento = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    scenario = Scenario(origine=str(file))
    for voce in documento.get("tag", []) or []:
        epc = _hex(voce.get("epc"))
        if not epc:
            continue
        elemento = TagRicostruito(
            epc=epc,
            tid=_hex(voce.get("tid")),
            user_bytes=int(voce.get("user_byte", USER_BYTE_PREDEFINITI)),
            antenne=set(int(a) for a in voce.get("antenne", []) or []),
            rssi=[int(voce.get("rssi", _RSSI_PREDEFINITO))],
        )
        # Scritto a mano, `antenne` significa proprio «solo queste»: chi
        # compila il file lo sta dichiarando, non deducendo da un log.
        elemento.limita_antenne = bool(elemento.antenne)
        # `visibile_ogni` si esprime qui come tasso, che e' la stessa cosa
        # detta dal verso da cui la guarda chi legge un diario.
        ogni = max(1, int(voce.get("visibile_ogni", 1)))
        elemento.cicli, elemento.letture = ogni, 1
        scenario.tag.append(elemento)
        if voce.get("nel_campo", True):
            scenario.nel_campo.add(epc)
    return scenario


def scenario_vuoto(quanti: int = 6, *, user_byte: int = USER_BYTE_PREDEFINITI) -> Scenario:
    """Tag vergini di fabbrica, per provare l'accettazione da zero.

    Un tag mai scritto ha un EPC di fabbrica e memoria USER a zero: e'
    esattamente la condizione in cui arrivano dal fornitore, ed e' la sola in
    cui la scrittura ha senso.
    """
    # Il numero di serie cambia a ogni banco: due sessioni di prova devono
    # dare tag **diversi**. Con TID fissi la seconda sessione troverebbe tutti
    # i tag gia' assegnati nell'archivio e la scrittura verrebbe rifiutata —
    # giustamente, perche' un tag si scrive una volta sola, ma il banco
    # diventerebbe inservibile dopo il primo giro.
    serie = secrets.randbelow(0xFFFFFF)
    scenario = Scenario(origine=f"{quanti} tag vergini (serie {serie:06X})")
    for indice in range(1, quanti + 1):
        scenario.tag.append(
            TagRicostruito(
                epc=f"E2801190{serie:06X}{indice:010X}"[:24],
                tid=f"E28011902000{serie:06X}{indice:06X}"[:24],
                user_bytes=user_byte,
                antenne=set(),
                rssi=[_RSSI_PREDEFINITO],
                letture=1,
                cicli=1,
            )
        )
    # Nessuno nel campo: sul banco vero i contenitori si appoggiano uno per
    # volta, ed e' cosi' che va provato.
    return scenario


class BancoSimulato:
    """Il banco: un `FakeTagBackend` piu' il magazzino dei tag fuori portata.

    Esiste per una ragione sola — poter dire «adesso l'operatore appoggia
    questo campione» senza avere il campione. Il backend sottostante resta
    quello dei test, quindi tutto cio' che vale li' vale anche qui.
    """

    def __init__(self, scenario: Scenario, *, antenne: Iterable[int] = (1, 2, 3)):
        self.scenario = scenario
        self._simulati: dict[str, SimulatedTag] = {
            elemento.epc: elemento.simulato() for elemento in scenario.tag
        }
        nel_campo = [
            tag for epc, tag in self._simulati.items() if epc in scenario.nel_campo
        ]
        self.backend = FakeTagBackend(nel_campo, antennas=tuple(int(a) for a in antenne))

    # -- il campo ------------------------------------------------------------
    def _indice(self, epc: str) -> str:
        """L'EPC come lo conosce il banco adesso.

        Serve perche' un tag scritto cambia EPC: chi lo aveva messo nel campo
        continua a chiamarlo col nome vecchio, e il banco deve riconoscerlo lo
        stesso invece di dire che non esiste.
        """
        cercato = _hex(epc)
        if cercato in self._simulati:
            return cercato
        for chiave, tag in self._simulati.items():
            if tag.epc_hex == cercato:
                return chiave
        return ""

    def metti(self, epc: str) -> dict[str, Any]:
        """Appoggia un tag sull'antenna."""
        chiave = self._indice(epc)
        if not chiave:
            raise KeyError(f"nessun tag {epc} in questo scenario")
        tag = self._simulati[chiave]
        if tag not in self.backend.tags:
            self.backend.tags.append(tag)
        return self.elenco()

    def togli(self, epc: str) -> dict[str, Any]:
        """Toglie un tag dal campo. Resta nello scenario, solo fuori portata."""
        chiave = self._indice(epc)
        if not chiave:
            raise KeyError(f"nessun tag {epc} in questo scenario")
        tag = self._simulati[chiave]
        self.backend.tags[:] = [voce for voce in self.backend.tags if voce is not tag]
        return self.elenco()

    def svuota(self) -> dict[str, Any]:
        self.backend.tags.clear()
        return self.elenco()

    def elenco(self) -> dict[str, Any]:
        """Cosa c'e' nel campo e cosa aspetta in magazzino."""
        nel_campo = {tag.epc_hex for tag in self.backend.tags}
        return {
            "origine": self.scenario.origine,
            "campo": [self._descrivi(tag) for tag in self.backend.tags],
            "magazzino": [
                self._descrivi(tag)
                for tag in self._simulati.values()
                if tag.epc_hex not in nel_campo
            ],
        }

    @staticmethod
    def _descrivi(tag: SimulatedTag) -> dict[str, Any]:
        return {
            "epc": tag.epc_hex,
            "tid": tag.tid.hex().upper(),
            "rssi": tag.rssi,
            "antenne": sorted(tag.visible_from) if tag.visible_from else [],
            "visibile_ogni": tag.visible_every,
            "scritto": tag.write_count > 0,
        }
