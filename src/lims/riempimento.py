"""Riempire la scatola un campione per volta, sapendo cosa entra.

Il sigillo (`lims.sealing`) **verifica** un elenco noto a scatola chiusa. Questo
modulo fa la cosa opposta e complementare: **costruisce** quell'elenco mentre la
scatola e' ancora aperta, riconoscendo ogni campione nel momento in cui
l'operatore lo appoggia dentro.

I due si tengono per mano e nessuno dei due basta da solo:

* il riempimento sa **cosa e' entrato**, ma a coperchio aperto — quello che
  vede potrebbe stare accanto alla scatola invece che dentro;
* il sigillo certifica **cosa c'e' dentro** a scatola chiusa, ma solo rispetto
  a un elenco che qualcuno deve avergli dato.

Da qui le scelte di questo modulo:

**La quantita' si conferma con letture ripetute, non con una.** Un tag puo'
sparire per un giro perche' un altro contenitore gli e' finito davanti o perche'
il liquido lo scherma: dichiarare un campione «uscito» al primo giro mancato
farebbe lampeggiare l'elenco proprio mentre l'operatore ci guarda. Ogni giro e'
un piccolo gruppo di cicli, e le due soglie sono **asimmetriche**:

* per **entrare** bastano poche letture, perche' il bip deve arrivare subito
  dopo il gesto — se arriva tre secondi dopo, l'operatore ha gia' messo il
  campione successivo e non sa piu' a quale si riferisce;
* per **uscire** ne servono di piu'. Togliere un campione dall'elenco e' la
  direzione pericolosa: un elenco che perde una riga fa credere all'operatore
  di doverne aggiungere un altro. Se invece resta di troppo, e' il sigillo a
  scoprirlo, e il sigillo non si sbaglia.

**La potenza e' bassa di proposito.** Qui un falso positivo non e' un fastidio:
aggiunge alla spedizione un campione che sta sul tavolo accanto e non nella
scatola. Il sigillo poi lo cercherebbe invano e la spedizione non partirebbe —
l'errore si scopre, ma tardi. Meno portata significa meno vicini di casa.

**Il tag non riconosciuto e' un risultato, non un errore.** «Campione non
inizializzato» e' esattamente cio' che l'operatore deve vedere quando infila
nella scatola un contenitore il cui tag non e' mai stato scritto: e' il caso che
il flusso di lavoro chiede di segnalare a voce alta.

Il modulo parla solo con `RFIDBackend` e con l'archivio in lettura: non conosce
frame, trasporti, ne' HTTP.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Protocol, Sequence

from .codec import epc_kind
from .model import ContainerState
from .responses import ServiceCallError, inventory_tags, raise_for_status

__all__ = [
    "Esito",
    "EventoRiempimento",
    "PoliticaRiempimento",
    "SessioneRiempimento",
]

log = logging.getLogger("lims.riempimento")


class Esito(str, Enum):
    """Cosa e' successo a un tag fra un giro di letture e il successivo."""

    ENTRATO = "entrato"
    USCITO = "uscito"
    #: Tag mai scritto da questo sistema: il contenitore non e' stato preparato.
    NON_INIZIALIZZATO = "non_inizializzato"
    #: Contenitore vero, ma gia' in un'altra scatola o gia' partito.
    ALTRA_SPEDIZIONE = "altra_spedizione"
    #: Contenitore annullato: non doveva piu' esistere.
    ANNULLATO = "annullato"
    #: Il tag sul coperchio, che identifica la scatola e non si conta.
    SCATOLA = "scatola"
    #: L'operatore ha detto che quel contenitore non e' nella scatola. Resta
    #: nel campo (e' li' sul tavolo), ma non conta e non rientra da solo.
    ESCLUSO = "escluso"

    @property
    def anomalia(self) -> bool:
        """Vero se l'operatore deve fermarsi a guardare."""
        return self in (
            Esito.NON_INIZIALIZZATO,
            Esito.ALTRA_SPEDIZIONE,
            Esito.ANNULLATO,
        )


def _adesso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class _Backend(Protocol):
    def inventory(self, request: Any) -> Any: ...
    def configure(self, settings: Any) -> Any: ...


@dataclass
class PoliticaRiempimento:
    """Le soglie con cui si decide che un campione e' dentro o non c'e' piu'.

    Sono **valori prudenti di partenza, non misure**: nessun tag e' ancora stato
    provato su questo banco. Vanno tarati contro la campagna di misura
    (`lims.campaign`) prima di considerarli buoni.
    """

    #: Cicli di inventory in ogni giro. Piu' di uno perche' e' la ripetizione a
    #: dare la conferma; pochi perche' il giro deve stare sotto il secondo.
    cicli_per_giro: int = 3
    #: Cicli in cui un tag deve comparire prima di entrare nell'elenco.
    cicli_per_entrare: int = 2
    #: Cicli consecutivi senza vederlo prima di toglierlo. Piu' alto della
    #: soglia di ingresso: vedi il ragionamento in testa al modulo.
    cicli_per_uscire: int = 6
    #: Millisecondi per ciclo di inventory.
    timeout_ms: int = 300
    #: Pausa fra un ciclo e l'altro dentro lo stesso giro.
    pausa_s: float = 0.02

    def __post_init__(self) -> None:
        if self.cicli_per_giro < 1:
            raise ValueError("cicli_per_giro deve essere almeno 1")
        if self.cicli_per_entrare < 1:
            raise ValueError("cicli_per_entrare deve essere almeno 1")
        if self.cicli_per_uscire < 1:
            raise ValueError("cicli_per_uscire deve essere almeno 1")
        if self.cicli_per_uscire <= self.cicli_per_entrare:
            # Non e' pignoleria: con le soglie invertite un tag difficile
            # entrerebbe e uscirebbe dall'elenco a ogni giro.
            raise ValueError(
                "cicli_per_uscire deve superare cicli_per_entrare, altrimenti "
                "l'elenco lampeggia"
            )


@dataclass
class EventoRiempimento:
    """Un campione entrato, uscito, o un tag che non doveva essere li'."""

    esito: Esito
    epc: str
    container_id: int | None = None
    etichetta: str = ""
    paziente: str = ""
    codice_fiscale: str = ""
    descrizione: str = ""
    accettazione: int | None = None
    antenne: tuple[int, ...] = ()
    rssi: int | None = None
    dettaglio: str = ""
    quando: str = field(default_factory=_adesso)

    @property
    def anomalia(self) -> bool:
        return self.esito.anomalia

    def describe(self) -> dict[str, Any]:
        return {
            "esito": self.esito.value,
            "anomalia": self.anomalia,
            "epc": self.epc,
            "container_id": self.container_id,
            "etichetta": self.etichetta,
            "paziente": self.paziente,
            "codice_fiscale": self.codice_fiscale,
            "descrizione": self.descrizione,
            "accettazione": self.accettazione,
            "antenne": list(self.antenne),
            "rssi": self.rssi,
            "dettaglio": self.dettaglio,
            "quando": self.quando,
        }


@dataclass
class _StatoTag:
    """Quello che la sessione ricorda di un tag fra un giro e l'altro."""

    epc: str
    visti: int = 0
    mancati: int = 0
    dentro: bool = False
    esito: Esito | None = None
    antenne: set[int] = field(default_factory=set)
    rssi: int | None = None
    evento: EventoRiempimento | None = None


class SessioneRiempimento:
    """Sorveglia la scatola aperta e riconosce i campioni che ci entrano.

    Non scrive niente nell'archivio: dice cosa e' successo, e chi la usa decide
    cosa farne. Cosi' resta provabile senza spedizioni e senza database di
    scrittura.
    """

    def __init__(
        self,
        backend: _Backend,
        db: Any,
        *,
        antenne: Sequence[int],
        politica: PoliticaRiempimento | None = None,
        potenza_cdbm: int | None = None,
        region: int = 0x08,
        write_power_cdbm: int = 2000,
        contenitori_della_scatola: Iterable[int] = (),
    ):
        self.backend = backend
        self.db = db
        self.antenne = tuple(int(a) for a in antenne)
        if not self.antenne:
            raise ValueError("serve almeno un'antenna di lettura")
        self.politica = politica or PoliticaRiempimento()
        self.potenza_cdbm = potenza_cdbm
        self.region = region
        self.write_power_cdbm = write_power_cdbm
        #: I contenitori gia' assegnati a questa scatola. Serve a distinguere
        #: «e' dei nostri» da «e' di un'altra spedizione» quando lo stato del
        #: contenitore e' gia' `packed`.
        self.nostri: set[int] = {int(c) for c in contenitori_della_scatola}
        self.tag: dict[str, _StatoTag] = {}
        self.box_epc = ""
        self._radio_pronta = False

    # -- radio ---------------------------------------------------------------
    def prepara(self) -> None:
        """Imposta la potenza del riempimento. Una volta sola per sessione."""
        if self._radio_pronta or self.potenza_cdbm is None:
            self._radio_pronta = True
            return
        from rfid_silion.service import AntennaPower, ReaderSettings

        raise_for_status(
            self.backend.configure(
                ReaderSettings(
                    region=self.region,
                    powers=tuple(
                        AntennaPower(
                            antenna_id=antenna,
                            read_power_cdbm=self.potenza_cdbm,
                            write_power_cdbm=self.write_power_cdbm,
                        )
                        for antenna in self.antenne
                    ),
                )
            ),
            "potenza di riempimento",
        )
        self._radio_pronta = True

    def _un_ciclo(self) -> dict[str, dict[str, Any]]:
        from rfid_silion.service import InventoryRequest

        risposta = raise_for_status(
            self.backend.inventory(
                InventoryRequest(
                    antennas=self.antenne, timeout_ms=self.politica.timeout_ms
                )
            ),
            "lettura della scatola",
        )
        visti: dict[str, dict[str, Any]] = {}
        for lettura in inventory_tags(risposta):
            epc = str(lettura.get("epc", "")).strip().upper()
            if not epc:
                continue
            voce = visti.setdefault(epc, {"antenne": set(), "rssi": None})
            if lettura.get("antenna_id") is not None:
                voce["antenne"].add(int(lettura["antenna_id"]))
            rssi = lettura.get("rssi")
            if rssi is not None:
                voce["rssi"] = max(voce["rssi"], int(rssi)) if voce["rssi"] is not None else int(rssi)
        return visti

    # -- un giro -------------------------------------------------------------
    def passa(self) -> dict[str, Any]:
        """Un giro di letture. Restituisce solo cio' che e' **cambiato**.

        Gli eventi sono le transizioni, non lo stato: un campione gia' dentro
        non viene riannunciato a ogni giro, altrimenti il bip suonerebbe senza
        sosta e smetterebbe di voler dire qualcosa.
        """
        self.prepara()
        conteggio: dict[str, int] = {}
        raccolta: dict[str, dict[str, Any]] = {}
        errore = ""
        try:
            for indice in range(self.politica.cicli_per_giro):
                for epc, dati in self._un_ciclo().items():
                    conteggio[epc] = conteggio.get(epc, 0) + 1
                    voce = raccolta.setdefault(epc, {"antenne": set(), "rssi": None})
                    voce["antenne"] |= dati["antenne"]
                    if dati["rssi"] is not None:
                        voce["rssi"] = (
                            max(voce["rssi"], dati["rssi"])
                            if voce["rssi"] is not None
                            else dati["rssi"]
                        )
                if indice + 1 < self.politica.cicli_per_giro and self.politica.pausa_s:
                    time.sleep(self.politica.pausa_s)
        except (ServiceCallError, ValueError) as exc:
            # Un giro fallito non deve svuotare l'elenco: senza letture non si
            # sa niente, e «non so» non e' «non c'e'».
            log.warning("Giro di riempimento non riuscito: %s", exc)
            return {**self.riepilogo(), "eventi": [], "errore": str(exc)}

        eventi: list[EventoRiempimento] = []
        cicli = self.politica.cicli_per_giro

        for epc, volte in conteggio.items():
            stato = self.tag.setdefault(epc, _StatoTag(epc=epc))
            stato.visti += volte
            stato.mancati = 0
            stato.antenne |= raccolta[epc]["antenne"]
            if raccolta[epc]["rssi"] is not None:
                stato.rssi = raccolta[epc]["rssi"]
            if not stato.dentro and stato.visti >= self.politica.cicli_per_entrare:
                stato.dentro = True
                evento = self._classifica(stato)
                stato.esito, stato.evento = evento.esito, evento
                eventi.append(evento)

        for epc, stato in self.tag.items():
            if epc in conteggio:
                continue
            stato.mancati += cicli
            if stato.mancati < self.politica.cicli_per_uscire:
                # Un giro mancato non azzera niente: un tag schermato dal
                # vasetto davanti alterna presenza e assenza, e azzerare il
                # conteggio a ogni buco significherebbe non riconoscerlo mai —
                # cioe' fallire proprio sui tag difficili, che sono quelli per
                # cui le letture ripetute esistono.
                continue
            stato.visti = 0
            if stato.dentro:
                era_contato = stato.esito == Esito.ENTRATO
                stato.dentro = False
                if era_contato:
                    # Si annuncia l'uscita solo di cio' che era contato: un tag
                    # estraneo che si allontana non e' una notizia.
                    stato.esito = Esito.USCITO
                    eventi.append(self._evento(stato, Esito.USCITO))
                else:
                    # Sparito dal campo: la prossima volta che compare va
                    # riclassificato da capo, compreso un escluso che
                    # l'operatore decide poi di mettere dentro davvero.
                    stato.esito = None
                    stato.evento = None

        if eventi:
            log.info(
                "Riempimento: %s",
                ", ".join(f"{e.esito.value} {e.epc[:12]}" for e in eventi),
            )
        # Un giro restituisce sia cio' che e' cambiato sia lo stato completo:
        # chi lo chiama ha bisogno di entrambi, e ricomporli fuori significa
        # ricalcolare due volte la stessa cosa.
        return {
            **self.riepilogo(),
            "eventi": [evento.describe() for evento in eventi],
            "errore": errore,
        }

    # -- classificazione -----------------------------------------------------
    def _classifica(self, stato: _StatoTag) -> EventoRiempimento:
        """Di chi e' questo tag, e ha diritto di stare in questa scatola?"""
        if epc_kind(stato.epc) == "box":
            if not self.box_epc:
                self.box_epc = stato.epc
            return self._evento(
                stato, Esito.SCATOLA, dettaglio="tag del coperchio: identifica la scatola"
            )

        record = None
        try:
            record = self.db.find_container_by_epc(stato.epc)
        except Exception:  # noqa: BLE001
            log.exception("Ricerca del contenitore %s non riuscita", stato.epc)

        if record is None:
            return self._evento(
                stato,
                Esito.NON_INIZIALIZZATO,
                dettaglio="questo contenitore non e' stato preparato: il tag non "
                "risulta scritto da questa postazione",
            )

        comune = {
            "container_id": record.container_id,
            "etichetta": record.label,
            "paziente": record.display_name,
            "codice_fiscale": record.codice_fiscale,
            "descrizione": record.descrizione,
            "accettazione": record.accession_id,
        }

        if record.state == ContainerState.VOIDED:
            return self._evento(
                stato, Esito.ANNULLATO, dettaglio="contenitore annullato", **comune
            )
        if record.state in (ContainerState.SHIPPED, ContainerState.RECEIVED):
            return self._evento(
                stato,
                Esito.ALTRA_SPEDIZIONE,
                dettaglio="questo contenitore risulta gia' partito",
                **comune,
            )
        if record.state == ContainerState.PACKED and record.container_id not in self.nostri:
            return self._evento(
                stato,
                Esito.ALTRA_SPEDIZIONE,
                dettaglio="questo contenitore e' gia' in un'altra scatola",
                **comune,
            )
        if record.state == ContainerState.PLANNED:
            return self._evento(
                stato,
                Esito.NON_INIZIALIZZATO,
                dettaglio="il tag risulta assegnato ma non ancora scritto",
                **comune,
            )
        return self._evento(stato, Esito.ENTRATO, **comune)

    def _evento(self, stato: _StatoTag, esito: Esito, **campi: Any) -> EventoRiempimento:
        return EventoRiempimento(
            esito=esito,
            epc=stato.epc,
            antenne=tuple(sorted(stato.antenne)),
            rssi=stato.rssi,
            **campi,
        )

    # -- lo stato adesso -----------------------------------------------------
    def dentro(self) -> list[dict[str, Any]]:
        """I campioni riconosciuti che stanno nella scatola, in ordine di arrivo."""
        return [
            stato.evento.describe()
            for stato in self.tag.values()
            if stato.dentro and stato.esito == Esito.ENTRATO and stato.evento is not None
        ]

    def anomalie(self) -> list[dict[str, Any]]:
        """Cio' che e' nel campo e non ci dovrebbe essere."""
        return [
            stato.evento.describe()
            for stato in self.tag.values()
            if stato.dentro and stato.evento is not None and stato.evento.anomalia
        ]

    def epc_dentro(self) -> list[str]:
        return [
            stato.epc
            for stato in self.tag.values()
            if stato.dentro and stato.esito == Esito.ENTRATO
        ]

    def declassa(self, epc: str, esito: Esito, dettaglio: str) -> None:
        """Cambia l'esito di un tag gia' annunciato.

        Capita quando l'archivio rifiuta l'aggiunta alla scatola per una
        ragione che la sola lettura non poteva conoscere — per esempio un
        contenitore finito in un'altra spedizione fra un giro e l'altro. Il
        campione resta nel campo, ma smette di contare come «dentro».
        """
        stato = self.tag.get(str(epc).strip().upper())
        if stato is None or stato.evento is None:
            return
        stato.esito = esito
        stato.evento.esito = esito
        stato.evento.dettaglio = dettaglio

    def escludi(self, epc: str, dettaglio: str = "") -> bool:
        """L'operatore dichiara che quel contenitore non e' nella scatola.

        Non basta dimenticarlo: il tag e' ancora nel campo — sta sul tavolo
        accanto, ed e' proprio per questo che era stato letto — quindi al giro
        successivo rientrerebbe da solo, e la correzione non reggerebbe un
        secondo. Resta invece **escluso** finche' non lascia davvero il campo:
        se poi l'operatore lo mette dentro per davvero, sparisce e ricompare, e
        allora viene riconosciuto da capo.
        """
        stato = self.tag.get(str(epc).strip().upper())
        if stato is None:
            return False
        stato.esito = Esito.ESCLUSO
        stato.evento = self._evento(
            stato,
            Esito.ESCLUSO,
            dettaglio=dettaglio or "escluso dall'operatore: non e' in questa scatola",
        )
        return True

    def dimentica(self, epc: str) -> bool:
        """Cancella ogni memoria di un tag. Usato dai test e dalle correzioni
        in cui il contenitore viene fisicamente portato via."""
        return self.tag.pop(str(epc).strip().upper(), None) is not None

    def esclusi(self) -> list[dict[str, Any]]:
        """Cio' che l'operatore ha detto di non contare, e che e' ancora li'."""
        return [
            stato.evento.describe()
            for stato in self.tag.values()
            if stato.dentro and stato.esito == Esito.ESCLUSO and stato.evento is not None
        ]

    def riepilogo(self) -> dict[str, Any]:
        dentro = self.dentro()
        return {
            "dentro": dentro,
            "quanti": len(dentro),
            "anomalie": self.anomalie(),
            "esclusi": self.esclusi(),
            "box_epc": self.box_epc,
            "epc": self.epc_dentro(),
        }


def politica_da_config(lims_cfg: Any) -> PoliticaRiempimento:
    """Costruisce la politica dalla sezione `lims:` di config.yaml."""
    sezione = dict(lims_cfg or {})
    riempimento = dict(sezione.get("riempimento", {}) or {})
    return PoliticaRiempimento(
        cicli_per_giro=int(riempimento.get("cicli_per_giro", 3)),
        cicli_per_entrare=int(riempimento.get("cicli_per_entrare", 2)),
        cicli_per_uscire=int(riempimento.get("cicli_per_uscire", 6)),
        timeout_ms=int(riempimento.get("timeout_ms", 300)),
    )


def potenza_da_config(lims_cfg: Any) -> int | None:
    """La potenza del riempimento: la piu' bassa fra quelle del sigillo.

    Il sigillo sale di potenza apposta, per non perdere niente a scatola
    chiusa. Qui vale il contrario: la scatola e' aperta e il tavolo accanto e'
    pieno di contenitori di altre spedizioni. Meno portata, meno vicini.
    """
    sezione = dict(lims_cfg or {})
    esplicita = (sezione.get("riempimento", {}) or {}).get("potenza_cdbm")
    if esplicita:
        return int(esplicita)
    potenze = list(sezione.get("seal_powers_cdbm") or [])
    return min(int(p) for p in potenze) if potenze else None


#: Firma della richiamata usata dal livello superiore per raccontare i giri.
Osservatore = Callable[[dict[str, Any]], None]
