"""Server locale dell'interfaccia operativa.

Solo libreria standard: `http.server` basta e non aggiunge dipendenze a un
laboratorio isolato. Il server non conosce l'RFID — inoltra a due contratti che
esistono gia' e sono gia' collaudati:

* `POST /rpc` → `RFIDRPCDispatcher`, il contratto del servizio (Strumenti);
* `POST /api/<operazione>` → `webui.workflow.Workflow`, il flusso di laboratorio;
* `GET /api/eventi` → SSE: passi della scrittura, avanzamento del sigillo, eventi
  del servizio.

**Una operazione radio alla volta.** Non e' prudenza generica: la guardia di
scrittura di `RFIDService` usa lo stato dell'ultimo inventory, quindi due
operazioni sovrapposte si corromperebbero a vicenda. Chi arriva mentre e'
occupato riceve `409`, non una coda.

**Solo loopback e solo con token.** L'hardware non deve essere raggiungibile
dalla rete del laboratorio, e nemmeno da un altro utente della stessa macchina.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import queue
import secrets
import socket
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from rfid_silion.diario import Diario
from rfid_silion.rpc import RFIDRPCDispatcher

from . import icone
from .workflow import Workflow, WorkflowError

__all__ = ["WebUIServer", "EventBus", "STATIC_DIR", "indirizzi_locali"]

log = logging.getLogger("webui.server")

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: Host che significano «solo questo computer» e «tutte le schede di rete».
_SOLO_QUI = frozenset({"127.0.0.1", "localhost", "::1"})
_OVUNQUE = frozenset({"0.0.0.0", "", "::"})

#: Oltre questa soglia una richiesta viene rifiutata senza leggerla: il corpo
#: piu' grande e' una distinta, che sta in poche decine di kilobyte.
MAX_BODY_BYTES = 4 * 1024 * 1024

#: Operazioni che non finiscono nel diario come record `api`. `traccia`
#: scriverebbe un record per ogni lotto di record; `stato` e' una lettura di
#: stato che non cambia niente e comparirebbe a decine.
_API_SILENZIOSE = frozenset({"traccia", "stato"})

#: Nomi di campo il cui valore non entra mai nel diario, da qualunque canale
#: arrivi: il PIN della deroga alla partenza, la password di accesso ai tag, il
#: token della postazione. Del gesto resta traccia, del segreto no.
_CHIAVI_SEGRETE = ("password", "pin", "token", "passphrase", "segreto", "credenzial")

#: Quanti eventi dell'interfaccia si accettano in un solo lotto. La pagina ne
#: manda una decina per volta: oltre questa soglia non e' piu' il browser che
#: racconta, e' qualcuno che riempie il disco.
_MAX_EVENTI_UI = 200

#: Ogni quanto il flusso SSE manda un commento di tenuta. Serve a scoprire un
#: browser chiuso senza aspettare il timeout del sistema operativo.
HEARTBEAT_S = 15.0

#: Tipi MIME dichiarati esplicitamente. Su Windows `mimetypes` legge il registro
#: di sistema, dove `.js` puo' essere associato a `text/plain`: il browser
#: rifiuterebbe il modulo e la pagina resterebbe muta senza dire perche'.
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class EventBus:
    """Distribuisce gli eventi ai browser collegati.

    Ogni iscritto ha la sua coda limitata: un consumatore lento perde eventi
    invece di bloccare la scrittura di un tag. La perdita e' dichiarata
    (`persi`), cosi' l'interfaccia sa di doversi risincronizzare invece di
    mostrare uno stato inventato — stesso principio di `history_truncated` nel
    contratto del servizio.
    """

    def __init__(self, *, max_queue: int = 200, diario: Diario | None = None):
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._max_queue = max_queue
        self._sequence = 0
        # Il diario registra gli eventi *emessi*, non quelli consegnati: cosi'
        # nel file resta la scena come il server l'ha raccontata, anche se un
        # browser lento ne ha persi per strada.
        self.diario = diario or Diario.spento()

    def subscribe(self) -> queue.Queue:
        coda: queue.Queue = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.append(coda)
        return coda

    def unsubscribe(self, coda: queue.Queue) -> None:
        with self._lock:
            if coda in self._subscribers:
                self._subscribers.remove(coda)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, kind: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._sequence += 1
            evento = {
                "sequence": self._sequence,
                "kind": kind,
                "data": dict(data or {}),
                "timestamp": time.time(),
            }
            iscritti = list(self._subscribers)
        self.diario.scrivi("evento", kind, evento["data"])
        for coda in iscritti:
            try:
                coda.put_nowait(evento)
            except queue.Full:
                # Si scarta il piu' vecchio e si segnala il buco.
                try:
                    coda.get_nowait()
                    coda.put_nowait({**evento, "persi": True})
                except (queue.Empty, queue.Full):  # pragma: no cover - corsa rara
                    pass
        return evento


class _Busy(RuntimeError):
    """Un'altra operazione radio e' gia' in corso."""


def indirizzi_locali() -> list[str]:
    """Gli indirizzi IPv4 con cui questa macchina si fa trovare sulla rete.

    Serve quando il server ascolta su tutte le schede: `0.0.0.0` non e' un
    indirizzo da scrivere sulla tavoletta, e su un portatile ce ne sono spesso
    tre o quattro (Wi-Fi, cavo, macchine virtuali) fra cui uno solo e' quello
    giusto. Il primo della lista e' quello con cui il sistema uscirebbe verso
    la rete, che e' la scelta giusta in quasi tutti i casi.
    """
    trovati: list[str] = []
    try:
        # Nessun pacchetto parte davvero: su UDP `connect` sceglie soltanto la
        # scheda. 192.0.2.0/24 e' la rete riservata agli esempi, quindi non c'e'
        # rischio di disturbare qualcuno anche se qualcosa partisse.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sonda:
            sonda.connect(("192.0.2.1", 9))
            trovati.append(sonda.getsockname()[0])
    except OSError:
        pass
    try:
        trovati.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass

    unici: list[str] = []
    for indirizzo in trovati:
        if indirizzo.startswith("127.") or indirizzo in unici:
            continue
        unici.append(indirizzo)
    return unici


class PortaOccupataError(RuntimeError):
    """La porta e' gia' usata da un'altra istanza dell'interfaccia."""


class _ServerEsclusivo(ThreadingHTTPServer):
    """Server che si rifiuta di condividere la porta.

    `HTTPServer` imposta `SO_REUSEADDR`, e **su Windows quel flag permette a un
    secondo processo di legarsi a una porta gia' occupata** invece di fallire.
    Il risultato e' due interfacce vive sulla stessa porta, ognuna con il suo
    token: il browser finisce su una delle due a caso e l'indirizzo stampato
    dall'altra viene rifiutato — un guasto che sembra un problema di token e non
    lo e'.

    Qui la porta e' esclusiva: la seconda istanza fallisce subito e lo dice.
    """

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self) -> None:
        esclusiva = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if esclusiva is not None:  # solo Windows
            self.socket.setsockopt(socket.SOL_SOCKET, esclusiva, 1)
        super().server_bind()


class WebUIServer:
    """Compone flusso, dispatcher RPC e HTTP in un'unica postazione."""

    def __init__(
        self,
        config: Mapping[str, Any],
        backend: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8770,
        token: str | None = None,
        static_dir: Path | None = None,
        workflow: Workflow | None = None,
        config_path: str | Path | None = None,
        diario: Diario | None = None,
        banco: Any = None,
    ):
        self.config = dict(config)
        self.backend = backend
        # Il diario e' sempre presente, eventualmente spento: cosi' nessuna
        # chiamata deve chiedersi se esiste.
        self.diario = diario or Diario.spento()
        # Gli eventi del browser si possono spegnere da soli, lasciando accesi
        # quelli della radio: sono le due meta' del diario e non sempre
        # servono insieme.
        self.diario_interfaccia = bool(
            (self.config.get("diario", {}) or {}).get("interfaccia", True)
        )
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        # Un token fissato in configurazione rende l'indirizzo stabile fra un
        # avvio e l'altro: e' la condizione perche' un collegamento salvato
        # sulla tavoletta continui a funzionare domani.
        self.token_fisso = bool(token)
        # Il banco di prova esiste solo quando il lettore e' simulato. Finche'
        # e' `None` la rotta non viene nemmeno registrata: nessuna postazione
        # vera deve poter far comparire un campione che non esiste.
        self.banco = banco
        self.static_dir = Path(static_dir or STATIC_DIR)
        self.events = EventBus(diario=self.diario)
        self.workflow = workflow or Workflow(self.config, backend, config_path=config_path)
        self.dispatcher = RFIDRPCDispatcher(backend) if _is_service(backend) else None
        self._radio_lock = threading.Lock()
        self._operazione_corrente = ""
        self._stop_event = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._operations = self._build_operations()
        self._subscribe_service_events()

    # -- eventi del servizio -------------------------------------------------
    def _subscribe_service_events(self) -> None:
        subscribe = getattr(self.backend, "subscribe", None)
        if subscribe is None:
            return

        def inoltra(event: Any) -> None:
            try:
                self.events.publish("servizio", event.to_dict())
            except Exception:  # noqa: BLE001
                log.exception("Inoltro evento di servizio non riuscito")

        self._unsubscribe = subscribe(inoltra)

    # -- mutua esclusione radio ---------------------------------------------
    def _acquire(self, nome: str) -> None:
        if not self._radio_lock.acquire(blocking=False):
            raise _Busy(self._operazione_corrente or "operazione in corso")
        self._operazione_corrente = nome

    def _release(self) -> None:
        self._operazione_corrente = ""
        self._radio_lock.release()

    @property
    def busy(self) -> bool:
        return bool(self._operazione_corrente)

    # -- indirizzi -----------------------------------------------------------
    @property
    def url(self) -> str:
        """L'indirizzo da aprire **su questo computer**.

        Con `host: 0.0.0.0` il server ascolta su tutte le schede di rete, ma
        `http://0.0.0.0:...` non e' un indirizzo che un browser sappia aprire:
        li' si stampa il loopback, che funziona sempre. Gli indirizzi per gli
        altri apparecchi stanno in `indirizzi()`.
        """
        host = "127.0.0.1" if self.host in _OVUNQUE else self.host
        return f"http://{host}:{self.port}/?t={self.token}"

    def indirizzi(self) -> dict[str, Any]:
        """Da dove e' raggiungibile questa postazione.

        Serve alla schermata Impostazioni: l'indirizzo per la tavoletta lo
        conosce solo il server, perche' il browser vede l'indirizzo con cui e'
        arrivato — sul PC il loopback, che dalla tavoletta non porta da nessuna
        parte.
        """
        rete: list[str] = []
        if self.host not in _SOLO_QUI:
            candidati = indirizzi_locali() if self.host in _OVUNQUE else [self.host]
            rete = [
                f"http://{indirizzo}:{self.port}/?modo=tavoletta&t={self.token}"
                for indirizzo in candidati
            ]
        return {
            "locale": self.url,
            "rete": rete,
            "host": self.host,
            "token_fisso": self.token_fisso,
        }

    def manifesto(self) -> dict[str, Any]:
        """Manifesto dell'applicazione web, per la schermata Home di Android.

        `start_url` porta il token: e' cio' che rende il collegamento salvato
        una postazione funzionante invece di una pagina che si apre e dice di
        no. Per la stessa ragione il manifesto non e' un file statico — chi lo
        scarica ha in mano il comando del lettore — e la rotta chiede il token
        come le altre.

        `display: standalone` toglie la barra degli indirizzi: sul banco non
        serve, e una barra in meno sono 60px di altezza in piu' proprio dove
        l'altezza e' la misura scarsa.
        """
        return {
            "name": "Tracciabilità campioni",
            "short_name": "Campioni",
            "description": "Accettazione, sigillo e ricezione dei campioni istologici.",
            "lang": "it",
            "dir": "ltr",
            "start_url": f"/?modo=tavoletta&t={self.token}",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#f3f1f7",
            "theme_color": "#3b2a63",
            "icons": [
                {
                    "src": f"/icona-{lato}.png",
                    "sizes": f"{lato}x{lato}",
                    "type": "image/png",
                    # «any maskable»: Android ritaglia l'icona nella forma di
                    # sistema (cerchio, goccia, quadrato stondato). Il vetrino
                    # sta dentro la zona sicura, quindi lo stesso file va bene
                    # ritagliato e intero.
                    "purpose": "any maskable",
                }
                for lato in icone.LATI
            ],
        }

    # -- tabella delle operazioni -------------------------------------------
    def _build_operations(self) -> dict[str, tuple[Callable[..., Any], bool]]:
        """`nome → (funzione, richiede_la_radio)`.

        Il secondo elemento non e' un dettaglio: le operazioni che non toccano
        la radio devono restare disponibili *mentre* una scrittura e' in corso,
        altrimenti l'interfaccia si blocca proprio quando ha piu' bisogno di
        aggiornarsi.
        """
        f = self.workflow
        operazioni: dict[str, tuple[Callable[..., Any], bool]] = {
            # Il flag del banco lo aggiunge il server: `Workflow` non deve
            # sapere se sotto c'e' un lettore vero o simulato.
            "descrivi": (
                lambda _d: {**f.descrivi(), "banco_di_prova": self.banco is not None},
                False,
            ),
            # Il diario dell'interfaccia: mai radio, cosi' resta raggiungibile
            # anche mentre il lettore e' impegnato in un sigillo.
            "traccia": (lambda d: self.traccia(d), False),
            "indirizzi": (lambda _d: self.indirizzi(), False),
            "riprendi_workflow": (lambda _d: f.riprendi_workflow(), False),
            "operatore": (lambda d: f.imposta_operatore(d.get("nome", "")), False),
            "stato": (lambda _d: f.stato(), False),
            "connetti": (lambda _d: self._connetti(), True),
            "disconnetti": (lambda _d: f.disconnetti(), True),
            "registra": (lambda d: f.registra_accettazione(d), False),
            "stato_accettazione": (lambda _d: f.stato_accettazione(), False),
            "conferma_conteggio": (lambda _d: f.conferma_conteggio(), False),
            "correggi_conteggio": (lambda d: f.correggi_conteggio(d.get("totale", 1)), False),
            "sorveglia": (lambda _d: f.sorveglia_piatto(), True),
            "scrivi": (lambda d: self._scrivi(d), True),
            "annulla_accettazione": (
                lambda d: f.annulla_accettazione(d.get("motivo", "")),
                False,
            ),
            "nuova_accettazione": (lambda _d: f.nuova_accettazione(), False),
            "annulla_contenitore": (
                lambda d: f.annulla_contenitore(
                    d.get("container_id"),
                    d.get("motivo", ""),
                    tag_guasto=bool(d.get("tag_guasto", False)),
                ),
                False,
            ),
            "coda_spedizione": (lambda _d: f.coda_spedizione(), False),
            "prepara_spedizione": (
                lambda d: f.prepara_spedizione(
                    d.get("destinazione", ""), d.get("container_ids", [])
                ),
                False,
            ),
            "stato_spedizione": (lambda _d: f.stato_spedizione(), False),
            "spedizioni_aperte": (lambda d: f.spedizioni_aperte(d.get("limite", 20)), False),
            "riapri_spedizione": (
                lambda d: f.riapri_spedizione(d.get("shipment_id")),
                False,
            ),
            "annulla_spedizione": (lambda _d: f.annulla_spedizione(), False),
            # Riempimento: la scatola aperta sulle antenne di lettura.
            "avvia_riempimento": (
                lambda d: self._avvia_riempimento(d.get("destinazione", "")),
                True,
            ),
            "sorveglia_scatola": (lambda _d: self._sorveglia_scatola(), True),
            "togli_dalla_scatola": (
                lambda d: f.togli_dalla_scatola(d.get("epc", "")),
                False,
            ),
            "chiudi_riempimento": (lambda _d: f.chiudi_riempimento(), False),
            "sigilla": (lambda _d: self._sigilla(), True),
            "distinta_stampabile": (lambda _d: f.distinta_stampabile(), False),
            "leggi_qr_distinta": (lambda d: f.leggi_qr_distinta(d.get("scansioni", [])), False),
            "invia_distinta_pec": (lambda _d: f.invia_distinta_pec(), False),
            "aggiorna_ricevute_pec": (lambda _d: f.aggiorna_ricevute_pec(), False),
            "conferma_invio": (
                lambda d: f.conferma_invio(
                    motivo_deroga=d.get("motivo_deroga", ""),
                    pin=d.get("pin", ""),
                ),
                False,
            ),
            "importa_distinta": (lambda d: self._importa_distinta(d), False),
            "stato_ricezione": (lambda _d: f.stato_ricezione(), False),
            "leggi_volume": (lambda _d: self._leggi_volume(), True),
            "stato_riscontro": (lambda _d: f.stato_riscontro(), False),
            "importa_riscontro": (lambda d: self._importa_riscontro(d), False),
            "riepilogo_transito": (
                lambda d: f.riepilogo_transito(
                    d.get("destinazione", ""), d.get("dal", ""), d.get("al", "")
                ),
                False,
            ),
            "conferma_ricezione": (
                lambda d: f.conferma_ricezione(d.get("motivo_non_conformita", "")),
                False,
            ),
            "etichetta": (lambda d: f.etichetta(d.get("container_id")), False),
            "stampa_etichetta": (lambda d: f.stampa_etichetta(d.get("container_id")), False),
            # -- strumenti e calibrazione --------------------------------
            "salute": (lambda _d: f.salute(), True),
            "potenze": (lambda d: f.imposta_potenze(d), True),
            "gen2": (lambda d: f.imposta_gen2(d), True),
            "gen2_consigliato": (lambda _d: f.gen2_consigliato(), True),
            "prova_lettura": (lambda d: f.prova_lettura(d.get("cicli", 5)), True),
            "diagnostica_antenna": (
                lambda d: f.diagnostica_antenna(
                    d.get("antenna", 1),
                    banda=d.get("banda"),
                    da_khz=d.get("da_khz"),
                    a_khz=d.get("a_khz"),
                    passo_khz=d.get("passo_khz", 1000),
                    consenti_cambio_regione=bool(d.get("consenti_cambio_regione", False)),
                    nota=str(d.get("nota", "")),
                ),
                True,
            ),
            "profila_tag": (lambda _d: self._profila(), True),
            "applica_profilo": (lambda d: f.applica_profilo(d), False),
            "registro_misure": (lambda d: f.registro_misure(d.get("limite", 40)), False),
            "inventario_campagna": (lambda d: self._inventario_campagna(d), True),
            "rileva_controllo": (
                lambda d: f.rileva_controllo(d.get("posizione", "dentro")),
                True,
            ),
            "campagna": (lambda d: self._campagna(d), True),
            "cerca_paziente": (
                lambda d: f.cerca_paziente(
                    d.get("query", ""),
                    offset=int(d.get("offset", 0) or 0),
                    ordine=str(d.get("ordine", "recenti")),
                ),
                False,
            ),
            "storico_paziente": (lambda d: f.storico_paziente(d.get("patient_id")), False),
            "traccia_contenitore": (lambda d: f.traccia_contenitore(d.get("epc", "")), False),
            "registro": (lambda d: f.registro(d.get("limite", 100)), False),
            "parco_tag": (lambda _d: f.parco_tag(), False),
            # -- impostazioni ---------------------------------------------
            "anagrafiche": (lambda d: f.imposta_anagrafiche(d), False),
            "bozza_email": (lambda _d: f.bozza_email(), False),
            "porte_seriali": (lambda _d: f.porte_seriali(), False),
            "impostazioni": (lambda _d: f.impostazioni(), False),
            # Cambiare trasporto ferma e riapre il lettore: e' un'operazione
            # radio a tutti gli effetti.
            "applica_collegamento": (lambda d: self._collegamento(d), True),
            "avanzate": (lambda d: f.imposta_avanzate(d), True),
            "salva_impostazioni": (lambda d: f.salva_impostazioni(d), False),
        }
        if self.banco is not None:
            # Muovere un tag nel campo simulato cambia cio' che il lettore
            # vede: e' un'operazione radio a tutti gli effetti, e va serializzata
            # con le altre.
            operazioni["simulazione"] = (lambda d: self._simulazione(d), True)
        return operazioni

    # -- operazioni che pubblicano eventi -----------------------------------
    def _connetti(self) -> dict[str, Any]:
        esito = self.workflow.connetti()
        self.events.publish("lettore", esito)
        return esito

    def _scrivi(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Scrive un tag raccontando i passi mentre accadono.

        La scena animata si muove su questi eventi e su nessun timer: se il
        passo non e' arrivato, non e' successo.
        """
        self.events.publish("scrittura", {"fase": "avvio"})

        def passo(testo: str) -> None:
            self.events.publish("scrittura", {"fase": "passo", "testo": testo})

        esito = self.workflow.scrivi_prossimo(
            on_step=passo,
            authorized_rewrite=bool(dati.get("authorized_rewrite", False)),
        )
        self.events.publish(
            "scrittura",
            {
                "fase": "fine",
                "ok": bool(esito["scrittura"]["ok"]),
                "scritti": esito["scritti"],
                "totale": esito["totale"],
            },
        )
        return esito

    def _avvia_riempimento(self, destinazione: str) -> dict[str, Any]:
        esito = self.workflow.avvia_riempimento(destinazione)
        self.events.publish(
            "riempimento",
            {
                "fase": "avvio",
                "shipment_id": esito.get("shipment_id"),
                "destinazione": esito.get("destinazione", ""),
            },
        )
        return esito

    def _sorveglia_scatola(self) -> dict[str, Any]:
        """Un giro di letture, con gli eventi che muovono la scena.

        Ogni ingresso e ogni uscita esce di qui nel momento in cui accade: la
        pagina fa il bip su questo, non su un timer. Un giro che non cambia
        niente non pubblica niente — il silenzio e' informazione anche lui.
        """
        esito = self.workflow.sorveglia_scatola()
        for evento in esito.get("eventi", []):
            self.events.publish("riempimento", {"fase": "tag", **evento})
        if esito.get("eventi"):
            self.events.publish(
                "riempimento",
                {"fase": "conteggio", "quanti": esito.get("quanti", 0)},
            )
        return esito

    def _sigilla(self) -> dict[str, Any]:
        self.events.publish("sigillo", {"fase": "avvio"})

        def avanzamento(passata: int, trovati: int, attesi: int) -> None:
            self.events.publish(
                "sigillo",
                {"fase": "passata", "passata": passata, "trovati": trovati, "attesi": attesi},
            )

        esito = self.workflow.sigilla(
            on_progress=avanzamento, stop_event=self._stop_event_sigillo()
        )
        sigillo = esito.get("sigillo") or {}
        self.events.publish(
            "sigillo",
            {
                "fase": "fine",
                "ok": bool(sigillo.get("ok")),
                "trovati": sigillo.get("trovati", 0),
                "attesi": sigillo.get("attesi", 0),
            },
        )
        return esito

    def _stop_event_sigillo(self) -> threading.Event:
        # Ripulito a ogni sigillo: un'interruzione vale per la sessione che la
        # riceve, non per la successiva.
        self._stop_event = threading.Event()
        return self._stop_event

    def _collegamento(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        esito = self.workflow.applica_collegamento(dati)
        self.events.publish("lettore", esito)
        return esito

    def _profila(self) -> dict[str, Any]:
        self.events.publish("profilazione", {"fase": "avvio"})
        esito = self.workflow.profila_tag()
        self.events.publish(
            "profilazione",
            {"fase": "fine", "adatto": bool(esito["profilo"].get("suitable"))},
        )
        return esito

    def _campagna(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """La griglia e' lunga: l'avanzamento esce configurazione per configurazione."""
        self.events.publish("campagna", {"fase": "avvio"})

        def avanzamento(indice: int, totale: int, risultato: Any) -> None:
            self.events.publish(
                "campagna",
                {
                    "fase": "configurazione",
                    "indice": indice,
                    "totale": totale,
                    "risultato": risultato.describe(),
                },
            )

        esito = self.workflow.esegui_campagna(
            dati, on_progress=avanzamento, stop_event=self._stop_event_sigillo()
        )
        self.events.publish("campagna", {"fase": "fine", "configurazioni": len(esito.get("risultati", []))})
        return esito

    def _inventario_campagna(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Fotografia iniziale: pubblica l'avanzamento dei circa venti cicli."""
        cicli = int(dati.get("cicli", 20))
        self.events.publish("campagna", {"fase": "inventario_avvio", "totale": cicli})

        def avanzamento(indice: int, totale: int, trovati: int) -> None:
            self.events.publish(
                "campagna",
                {
                    "fase": "inventario_ciclo",
                    "indice": indice,
                    "totale": totale,
                    "trovati": trovati,
                },
            )

        esito = self.workflow.inventario_campagna(cicli, on_progress=avanzamento)
        self.events.publish(
            "campagna",
            {"fase": "inventario_fine", "trovati": len(esito.get("tag", []))},
        )
        return esito

    def _importa_riscontro(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        import base64
        import binascii

        try:
            contenuto = base64.b64decode(str(dati.get("contenuto_base64", "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkflowError(f"file non leggibile: {exc}") from exc
        esito = self.workflow.importa_riscontro(contenuto)
        self.events.publish(
            "arrivo",
            {"shipment_id": esito.get("shipment_id"), "ok": bool(esito.get("ok"))},
        )
        return esito

    def _importa_distinta(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        import base64
        import binascii

        blob = dati.get("contenuto_base64", "")
        try:
            contenuto = base64.b64decode(str(blob), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkflowError(f"file non leggibile: {exc}") from exc
        return self.workflow.importa_distinta(contenuto)

    def _simulazione(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Il banco di prova: appoggia e togli campioni dal campo simulato.

        E' l'unico modo di provare cio' che accade *mentre* qualcosa cambia —
        il riempimento della scatola, la sorveglianza del piatto — senza avere
        in mano trenta contenitori e un lettore acceso.
        """
        azione = str(dati.get("azione", "elenco")).strip().lower()
        epc = str(dati.get("epc", "")).strip().upper()
        try:
            if azione == "metti":
                return self.banco.metti(epc)
            if azione == "togli":
                return self.banco.togli(epc)
            if azione == "svuota":
                return self.banco.svuota()
            if azione == "elenco":
                return self.banco.elenco()
        except KeyError as exc:
            raise WorkflowError(str(exc).strip("'")) from exc
        raise WorkflowError(f"azione sconosciuta per il banco di prova: {azione}")

    def _leggi_volume(self) -> dict[str, Any]:
        self.events.publish("ricezione", {"fase": "lettura"})
        esito = self.workflow.leggi_volume()
        riconciliazione = esito.get("riconciliazione") or {}
        self.events.publish(
            "ricezione",
            {
                "fase": "fine",
                "ok": bool(riconciliazione.get("ok")),
                "arrivati": riconciliazione.get("arrivati", 0),
                "attesi": riconciliazione.get("attesi", 0),
            },
        )
        return esito

    # -- esecuzione di una operazione ---------------------------------------
    def call(self, nome: str, dati: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        voce = self._operations.get(nome)
        if voce is None:
            return HTTPStatus.NOT_FOUND, {"errore": f"operazione sconosciuta: {nome}"}
        funzione, radio = voce
        if not radio:
            return self._esegui(nome, funzione, dati)
        try:
            self._acquire(nome)
        except _Busy as exc:
            # Vale la pena registrarlo: un 409 dice che l'operatore ha premuto
            # mentre la radio era occupata, ed e' un fatto sul flusso, non un
            # guasto.
            self.diario.scrivi(
                "api", nome, {"stato": int(HTTPStatus.CONFLICT), "occupato_da": str(exc)}
            )
            return HTTPStatus.CONFLICT, {
                "errore": f"il lettore sta gia' eseguendo: {exc}",
                "occupato": True,
            }
        try:
            return self._esegui(nome, funzione, dati)
        finally:
            self._release()

    def _esegui(
        self, nome: str, funzione: Callable[..., Any], dati: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        avvio = time.perf_counter()
        try:
            stato, risposta = HTTPStatus.OK, funzione(dati)
        except WorkflowError as exc:
            # Errore previsto: e' un messaggio pensato per l'operatore.
            stato, risposta = HTTPStatus.BAD_REQUEST, {"errore": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("Operazione non riuscita")
            stato, risposta = HTTPStatus.INTERNAL_SERVER_ERROR, {
                "errore": f"{type(exc).__name__}: {exc}",
                "imprevisto": True,
            }
        if nome not in _API_SILENZIOSE:
            self.diario.scrivi(
                "api",
                nome,
                {
                    "stato": int(stato),
                    "richiesta": _ripulisci(dati),
                    "risposta": _ripulisci(risposta),
                },
                durata_ms=(time.perf_counter() - avvio) * 1000,
            )
        return stato, risposta

    def traccia(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Accoglie un lotto di eventi dal browser e li mette nel diario.

        Non tocca la radio, quindi resta viva **durante** un sigillo lungo: e'
        proprio allora che serve sapere cosa ha premuto l'operatore. Il
        contenuto arriva dalla pagina e non viene interpretato: qui si annota,
        non si obbedisce.
        """
        if not self.diario_interfaccia:
            return {"registrati": 0, "diario": False}
        eventi = dati.get("eventi")
        if not isinstance(eventi, (list, tuple)):
            raise WorkflowError("«eventi» deve essere un elenco")
        if len(eventi) > _MAX_EVENTI_UI:
            raise WorkflowError(
                f"troppi eventi in un solo lotto: al massimo {_MAX_EVENTI_UI}"
            )
        scritti = 0
        for evento in eventi:
            if not isinstance(evento, Mapping):
                continue
            nome = str(evento.get("nome", "")).strip()[:80] or "senza-nome"
            self.diario.scrivi("ui", nome, _senza_segreti(evento))
            scritti += 1
        return {"registrati": scritti, "diario": bool(self.diario.attivo)}

    def rpc(self, richiesta: Any) -> Any:
        """Inoltra al dispatcher del servizio, senza reinterpretare niente."""
        if self.dispatcher is None:
            return {
                "jsonrpc": "2.0",
                "id": richiesta.get("id") if isinstance(richiesta, Mapping) else None,
                "error": {
                    "code": -32601,
                    "message": "questo backend non espone il contratto JSON-RPC",
                },
            }
        try:
            self._acquire("rpc")
        except _Busy as exc:
            return {
                "jsonrpc": "2.0",
                "id": richiesta.get("id") if isinstance(richiesta, Mapping) else None,
                "error": {"code": -32000, "message": f"lettore occupato: {exc}"},
            }
        try:
            return self.dispatcher.dispatch(richiesta)
        finally:
            self._release()

    def interrompi(self) -> dict[str, Any]:
        """Chiede al sigillo in corso di fermarsi al prossimo controllo."""
        self._stop_event.set()
        return {"interruzione_richiesta": True}

    # -- ciclo di vita HTTP --------------------------------------------------
    def apri(self) -> None:
        """Prende la porta. Va chiamata prima di annunciare l'indirizzo.

        Solleva `PortaOccupataError` se un'altra istanza e' gia' in ascolto,
        cosi' nessuno stampa un indirizzo che poi non funziona.
        """
        if self._httpd is not None:
            return
        try:
            self._httpd = _ServerEsclusivo((self.host, self.port), _make_handler(self))
        except OSError as exc:
            raise PortaOccupataError(
                f"la porta {self.port} e' gia' usata: l'interfaccia operativa e' "
                "probabilmente gia' aperta in un'altra finestra"
            ) from exc
        # La porta effettiva puo' differire se si chiede 0.
        self.port = self._httpd.server_address[1]
        self.diario.nota(
            "sessione_avviata",
            host=self.host,
            porta=self.port,
            token_fisso=self.token_fisso,
            backend=type(self.backend).__name__,
        )

    def serve_forever(self) -> None:
        self.apri()
        log.info("Interfaccia operativa su %s", self.url)
        try:
            self._httpd.serve_forever(poll_interval=0.25)
        finally:
            self._httpd.server_close()

    def start_background(self) -> threading.Thread:
        # Si apre nel thread chiamante: cosi' una porta occupata solleva
        # l'errore a chi ha chiesto l'avvio, invece di morire in un thread.
        self.apri()
        pronto = threading.Event()

        def esegui() -> None:
            pronto.set()
            try:
                self._httpd.serve_forever(poll_interval=0.1)
            finally:
                self._httpd.server_close()

        thread = threading.Thread(target=esegui, name="webui", daemon=True)
        thread.start()
        pronto.wait(timeout=5.0)
        return thread

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._httpd is not None:
            self._httpd.shutdown()
        if self._unsubscribe is not None:
            self._unsubscribe()
        self.workflow.chiudi()
        self.diario.nota("sessione_chiusa")
        self.diario.chiudi()


def _riservato(nome: Any) -> bool:
    minuscola = str(nome).lower()
    return any(segreto in minuscola for segreto in _CHIAVI_SEGRETE)


def _ripulisci(valore: Any, profondita: int = 0) -> Any:
    """Toglie i segreti da una struttura diretta al diario.

    Vale per richiesta e risposta di ogni operazione: `conferma_invio` porta il
    PIN del responsabile, `avanzate` la password di accesso ai tag. Il resto
    passa intatto — il diario serve proprio a rileggere cosa e' stato chiesto.
    """
    if profondita > 8:
        return valore
    if isinstance(valore, Mapping):
        return {
            str(chiave): (
                "(non registrato)"
                if _riservato(chiave)
                else _ripulisci(contenuto, profondita + 1)
            )
            for chiave, contenuto in valore.items()
        }
    if isinstance(valore, (list, tuple)):
        return [_ripulisci(voce, profondita + 1) for voce in valore]
    return valore


def _senza_segreti(evento: Mapping[str, Any]) -> dict[str, Any]:
    """L'evento del browser ripulito di cio' che non deve essere conservato.

    Sapere *che* un PIN e' stato digitato serve a ricostruire il flusso; sapere
    quale, no. La pagina gia' non guarda i campi password, ma il diario non
    deve dipendere dalla buona educazione del browser: la regola vale anche se
    qualcuno chiama la rotta a mano.

    Un evento su un campo riservato si riconosce dal nome della chiave
    (`pin`, `password`, …) oppure dall'identificativo del campo, perche' la
    pagina manda `{"id": "pin-deroga", "valore": "…"}` e li' la chiave e'
    soltanto «valore».
    """
    identita_riservata = _riservato(evento.get("id", "")) or _riservato(
        evento.get("name", "")
    )
    ripulito: dict[str, Any] = {}
    for chiave, valore in evento.items():
        if chiave == "nome":
            continue
        if _riservato(chiave) or (identita_riservata and chiave in ("valore", "testo")):
            ripulito[str(chiave)] = "(non registrato)"
        else:
            ripulito[str(chiave)] = valore
    return ripulito


def _is_service(backend: Any) -> bool:
    """Il dispatcher RPC pretende il contratto completo del servizio."""
    return all(
        hasattr(backend, nome)
        for nome in ("start", "stop", "inventory", "snapshot", "events", "health")
    )


def _make_handler(server: WebUIServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "RFIDLims"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        # -- utilita' ---------------------------------------------------------
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("%s - %s", self.address_string(), format % args)

        def _token_valido(self, query: Mapping[str, list[str]]) -> bool:
            fornito = self.headers.get("X-RFID-Token", "")
            if not fornito:
                fornito = (query.get("t") or [""])[0]
            return secrets.compare_digest(fornito, server.token)

        def _rispondi(self, stato: int, corpo: bytes, tipo: str) -> None:
            self.send_response(stato)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            # L'interfaccia gira in locale e non deve poter essere inclusa
            # altrove ne' caricare niente da fuori.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corpo)

        def _json(self, stato: int, dati: Any) -> None:
            corpo = json.dumps(dati, ensure_ascii=False, default=str).encode("utf-8")
            self._rispondi(stato, corpo, "application/json; charset=utf-8")

        def _leggi_corpo(self) -> Any:
            lunghezza = int(self.headers.get("Content-Length") or 0)
            if lunghezza > MAX_BODY_BYTES:
                raise ValueError("corpo della richiesta troppo grande")
            if lunghezza <= 0:
                return {}
            grezzo = self.rfile.read(lunghezza)
            return json.loads(grezzo.decode("utf-8"))

        # -- rotte ------------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parti = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parti.query)
            percorso = parti.path

            if percorso == "/api/eventi":
                if not self._token_valido(query):
                    self._json(HTTPStatus.UNAUTHORIZED, {"errore": "token mancante o errato"})
                    return
                self._stream_eventi()
                return

            if percorso.startswith("/api/") or percorso == "/rpc":
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"errore": "usare POST"})
                return

            # Il manifesto contiene il token dentro `start_url`: chi lo legge
            # comanda il lettore, quindi vale la stessa regola delle API. La
            # pagina se lo chiede da sola col token che ha gia' (`app.js`).
            if percorso == "/manifest.webmanifest":
                if not self._token_valido(query):
                    self._json(HTTPStatus.UNAUTHORIZED, {"errore": "token mancante o errato"})
                    return
                corpo = json.dumps(server.manifesto(), ensure_ascii=False).encode("utf-8")
                self._rispondi(HTTPStatus.OK, corpo, "application/manifest+json; charset=utf-8")
                return

            # Le icone invece sono pubbliche: sono pixel, non dicono niente, e
            # Android le scarica per conto suo mentre installa il collegamento.
            lato = icone.lato_dal_percorso(percorso)
            if lato is not None:
                self._rispondi(HTTPStatus.OK, icone.icona(lato), "image/png")
                return

            self._servi_statico(percorso)

        # `BaseHTTPRequestHandler` risponde a qualunque altro metodo con una
        # pagina HTML 501 in inglese, senza dire cosa fare. Il browser ne manda
        # (OPTIONS da estensioni o service worker), e quel 501 nudo sembra un
        # guasto del lettore: meglio un errore che si spiega.
        def _metodo_non_previsto(self) -> None:
            self._json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {
                    "errore": f"metodo {self.command} non previsto",
                    "spiegazione": "questa interfaccia usa solo GET per le pagine e POST per i comandi",
                },
            )

        do_OPTIONS = _metodo_non_previsto
        do_PUT = _metodo_non_previsto
        do_DELETE = _metodo_non_previsto
        do_PATCH = _metodo_non_previsto

        def do_HEAD(self) -> None:  # noqa: N802
            # Solo sui file: una HEAD su `/api/eventi` aprirebbe un flusso che
            # nessuno legge.
            percorso = urllib.parse.urlsplit(self.path).path
            if percorso.startswith("/api/") or percorso == "/rpc":
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"errore": "usare POST"})
                return
            self._servi_statico(percorso)

        def do_POST(self) -> None:  # noqa: N802
            parti = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parti.query)
            if not self._token_valido(query):
                self._json(HTTPStatus.UNAUTHORIZED, {"errore": "token mancante o errato"})
                return
            try:
                corpo = self._leggi_corpo()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"errore": f"corpo non valido: {exc}"})
                return

            if parti.path == "/rpc":
                risposta = server.rpc(corpo)
                if risposta is None:  # notification JSON-RPC
                    self._rispondi(HTTPStatus.NO_CONTENT, b"", "application/json")
                else:
                    self._json(HTTPStatus.OK, risposta)
                return

            if parti.path == "/api/interrompi":
                self._json(HTTPStatus.OK, server.interrompi())
                return

            # I tre file che escono da qui. Il corpo lo produce il flusso, il
            # server aggiunge solo l'involucro HTTP.
            scarichi = {
                "/api/distinta": (
                    server.workflow.esporta_distinta,
                    "application/octet-stream",
                ),
                "/api/riscontro": (
                    server.workflow.esporta_riscontro,
                    "application/octet-stream",
                ),
                "/api/riepilogo": (
                    lambda: server.workflow.esporta_riepilogo(
                        corpo.get("destinazione", ""),
                        corpo.get("dal", ""),
                        corpo.get("al", ""),
                    ),
                    "text/csv; charset=utf-8",
                ),
            }
            if parti.path in scarichi:
                produci, tipo = scarichi[parti.path]
                self._scarica(produci, tipo)
                return

            if not parti.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"errore": "percorso sconosciuto"})
                return

            nome = parti.path[len("/api/") :]
            if not isinstance(corpo, Mapping):
                self._json(HTTPStatus.BAD_REQUEST, {"errore": "il corpo deve essere un oggetto"})
                return
            stato, dati = server.call(nome, corpo)
            self._json(stato, dati)

        # -- flussi -----------------------------------------------------------
        def _scarica(self, produci: Callable[[], tuple[bytes, str]], tipo: str) -> None:
            """Un file come allegato: la distinta, il verbale, il riepilogo."""
            try:
                blob, nome = produci()
            except WorkflowError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"errore": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("Produzione del file da scaricare non riuscita")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"errore": str(exc)})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Disposition", f'attachment; filename="{nome}"')
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        def _stream_eventi(self) -> None:
            coda = server.events.subscribe()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    try:
                        evento = coda.get(timeout=HEARTBEAT_S)
                    except queue.Empty:
                        self.wfile.write(b": battito\n\n")
                        self.wfile.flush()
                        continue
                    riga = json.dumps(evento, ensure_ascii=False, default=str)
                    self.wfile.write(
                        f"id: {evento['sequence']}\nevent: {evento['kind']}\n"
                        f"data: {riga}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                log.debug("Flusso eventi chiuso dal browser")
            finally:
                server.events.unsubscribe(coda)

        def _servi_statico(self, percorso: str) -> None:
            nome = "index.html" if percorso in ("", "/") else percorso.lstrip("/")
            file = (server.static_dir / nome).resolve()
            try:
                file.relative_to(server.static_dir.resolve())
            except ValueError:
                # Tentativo di uscire dalla cartella statica.
                self._json(HTTPStatus.FORBIDDEN, {"errore": "percorso non consentito"})
                return
            if not file.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"errore": "file non trovato"})
                return
            tipo = _MIME.get(file.suffix.lower())
            if tipo is None:
                indovinato, _ = mimetypes.guess_type(str(file))
                tipo = indovinato or "application/octet-stream"
            corpo = file.read_bytes()
            self._rispondi(HTTPStatus.OK, corpo, tipo)

    return Handler
