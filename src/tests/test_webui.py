"""Il server dell'interfaccia operativa, provato via HTTP e senza hardware.

I test parlano al server come ci parla il browser: richieste vere su una porta
vera, contro un backend simulato. Cosi' quello che si verifica e' il contratto
che il frontend usera' davvero, non una funzione interna che gli assomiglia.

Le promesse che questi test tengono ferme:

* senza token non si arriva all'hardware;
* due operazioni radio insieme non partono: la seconda riceve `409`;
* i passi della scrittura escono sul flusso eventi **mentre** accade, non dopo;
* `/rpc` e' davvero il dispatcher esistente, non una seconda implementazione;
* il percorso completo — accettazione, scrittura, sigillo, distinta, ricezione —
  regge da un capo all'altro;
* e i casi che non devono mai passare: sigillo dichiarato valido con un
  contenitore mancante, scrittura su un tag gia' scritto, distinta manomessa.
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag

from webui.server import WebUIServer

CF_PAZIENTE = "MRTMTT25D09F205Z"
TID_BASE = bytes.fromhex("E2801190200050A1B2C300")


# ---------------------------------------------------------------------------
# Attrezzatura
# ---------------------------------------------------------------------------
def _tag_vergine(indice: int) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=TID_BASE + bytes([indice]),
        user_bytes=64,
    )


def _config(tmp: Path) -> dict:
    return {
        "reader": {"region": 0x08},
        "antennas": [
            {"id": 1, "read_power": 2900, "write_power": 2000},
            {"id": 2, "read_power": 2900, "write_power": 2000},
            {"id": 3, "read_power": 2000, "write_power": 2000},
        ],
        "geometry": {
            "volume_mm": [440, 220, 220],
            "antenna_size_mm": [220, 220, 24],
            "antenna_positions_mm": [
                {"id": 1, "center": [-110, 0, 0], "normal": [0, 0, 1]},
                {"id": 2, "center": [110, 0, 0], "normal": [0, 0, 1]},
                {"id": 3, "center": [520, 0, 0], "normal": [0, 0, 1]},
            ],
        },
        "tag_access": {"access_password_hex": "00000000", "timeout_ms": 1000},
        "lims": {
            "lab_id": 1,
            "database": str(tmp / "lims.db"),
            "keyring": str(tmp / "keys.json"),
            "user_memory_bytes": 64,
            "write_antennas": [3],
            "read_antennas": [1, 2],
            "seal_min_antennas": 1,
            "seal_powers_cdbm": [2000, 2900],
        },
        "webui": {"host": "127.0.0.1", "port": 0},
    }


class _Postazione:
    """Server avviato su una porta libera, con backend simulato."""

    def __init__(self, tmp: Path, tags: list[SimulatedTag] | None = None):
        self.backend = FakeTagBackend(tags if tags is not None else [], antennas=(1, 2, 3))
        self.server = WebUIServer(_config(tmp), self.backend, host="127.0.0.1", port=0)
        self.server.workflow.imposta_operatore("TEST")
        self.server.start_background()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"

    def __enter__(self) -> "_Postazione":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()

    # -- chiamate ---------------------------------------------------------
    def post(self, percorso: str, dati: dict | None = None, *, token: str | None = ...):
        corpo = json.dumps(dati or {}).encode("utf-8")
        richiesta = urllib.request.Request(
            f"{self.base}{percorso}", data=corpo, method="POST"
        )
        richiesta.add_header("Content-Type", "application/json")
        if token is ...:
            token = self.server.token
        if token is not None:
            richiesta.add_header("X-RFID-Token", token)
        try:
            with urllib.request.urlopen(richiesta, timeout=10) as risposta:
                grezzo = risposta.read()
                return risposta.status, (json.loads(grezzo) if grezzo else None)
        except urllib.error.HTTPError as errore:
            grezzo = errore.read()
            return errore.code, (json.loads(grezzo) if grezzo else None)

    def scarica(self, percorso: str) -> tuple[int, bytes]:
        richiesta = urllib.request.Request(f"{self.base}{percorso}", data=b"{}", method="POST")
        richiesta.add_header("Content-Type", "application/json")
        richiesta.add_header("X-RFID-Token", self.server.token)
        try:
            with urllib.request.urlopen(richiesta, timeout=10) as risposta:
                return risposta.status, risposta.read()
        except urllib.error.HTTPError as errore:
            return errore.code, errore.read()

    def get(self, percorso: str, *, token: str | None = ...):
        url = f"{self.base}{percorso}"
        richiesta = urllib.request.Request(url, method="GET")
        if token is ...:
            token = self.server.token
        if token is not None:
            richiesta.add_header("X-RFID-Token", token)
        try:
            with urllib.request.urlopen(richiesta, timeout=10) as risposta:
                return risposta.status, risposta.read()
        except urllib.error.HTTPError as errore:
            return errore.code, errore.read()


def _tmp(nome: str) -> Path:
    import tempfile

    cartella = Path(tempfile.mkdtemp(prefix=f"webui_{nome}_"))
    return cartella


def _accetta(posto: _Postazione, *, totale: int) -> dict:
    stato, dati = posto.post(
        "/api/registra",
        {
            "codice_fiscale": CF_PAZIENTE,
            "cognome": "Della Valle",
            "nome": "Gianfranco",
            "sesso": "M",
            "contenitori": totale,
            "reparto": "Chirurgia generale",
            "descrizione": "pezzo operatorio",
            "material_code": 1,
            "fixative_code": 1,
            "site_code": 1,
            "avvertenze": ["INFECTIOUS"],
        },
    )
    assert stato == 200, dati
    return dati


def _scrivi_tutti(posto: _Postazione, tags: list[SimulatedTag]) -> list[dict]:
    """Scrive i tag uno per volta, come sul banco vero.

    La postazione di scrittura ne accetta uno solo: mettere tre contenitori sul
    piatto insieme e' proprio l'errore che la guardia deve fermare, quindi il
    test non puo' simularlo altrimenti.
    """
    posto.post("/api/conferma_conteggio")
    tutti = list(posto.backend.tags)
    esiti = []
    try:
        for tag in tags:
            posto.backend.tags[:] = [tag]
            stato, dati = posto.post("/api/scrivi")
            assert stato == 200, dati
            esiti.append(dati)
    finally:
        posto.backend.tags[:] = tutti
    return esiti


def _prepara(posto: _Postazione, destinazione: str):
    stato, coda = posto.post("/api/coda_spedizione")
    assert stato == 200, coda
    ids = [
        voce["container_id"]
        for gruppo in coda["gruppi"]
        for voce in gruppo["contenitori"]
    ]
    return posto.post(
        "/api/prepara_spedizione",
        {"destinazione": destinazione, "container_ids": ids},
    )


# ---------------------------------------------------------------------------
# Accesso
# ---------------------------------------------------------------------------
def test_senza_token_non_si_arriva_allhardware():
    with _Postazione(_tmp("token")) as posto:
        stato, dati = posto.post("/api/stato", token=None)
        assert stato == 401
        assert "token" in dati["errore"]

        stato, _ = posto.post("/api/stato", token="quello-sbagliato")
        assert stato == 401

        # Con il token giusto la stessa richiesta passa: il rifiuto era il token,
        # non la rotta.
        stato, _ = posto.post("/api/stato")
        assert stato == 200


def test_token_accettato_anche_nellindirizzo():
    """Il browser apre la pagina con `?t=…`: il flusso SSE non ha header propri."""
    with _Postazione(_tmp("query")) as posto:
        stato, _ = posto.post(f"/api/stato?t={posto.server.token}", token=None)
        assert stato == 200


def test_due_istanze_sulla_stessa_porta_non_convivono():
    """Due interfacce sulla stessa porta sono peggio di una che non parte.

    `HTTPServer` imposta `SO_REUSEADDR` e su Windows quel flag lascia che un
    secondo processo si leghi a una porta gia' occupata: restano due server
    vivi, ognuno col suo token, e il browser ne prende uno a caso. L'indirizzo
    stampato dall'altro viene rifiutato, e sembra un problema di token.
    """
    from webui.server import PortaOccupataError

    cartella = _tmp("porta_occupata")
    primo = WebUIServer(_config(cartella), FakeTagBackend([]), host="127.0.0.1", port=0)
    primo.start_background()
    try:
        secondo = WebUIServer(
            _config(cartella), FakeTagBackend([]), host="127.0.0.1", port=primo.port
        )
        try:
            secondo.apri()
            raise AssertionError("la seconda istanza non doveva prendere la porta")
        except PortaOccupataError as exc:
            assert str(primo.port) in str(exc)
        finally:
            secondo.workflow.chiudi()
    finally:
        primo.shutdown()


def test_il_token_puo_essere_fisso():
    """Su una postazione dedicata serve un indirizzo che non cambia."""
    cartella = _tmp("token_fisso")
    server = WebUIServer(
        _config(cartella),
        FakeTagBackend([]),
        host="127.0.0.1",
        port=0,
        token="token-di-collaudo",
    )
    try:
        assert server.token == "token-di-collaudo"
        assert "t=token-di-collaudo" in server.url
    finally:
        server.workflow.chiudi()


def test_la_pagina_si_carica_anche_col_token_sbagliato():
    """La pagina non e' segreta: e' il *comando* che va protetto.

    Serve che si carichi comunque, altrimenti l'operatore vedrebbe solo un
    errore del browser e non saprebbe che il problema e' il token scaduto:
    e' la pagina stessa a doverglielo dire.
    """
    with _Postazione(_tmp("pagina")) as posto:
        stato, corpo = posto.get("/", token=None)
        assert stato == 200
        assert b"sbarramento" in corpo, "manca la schermata che spiega il token"

        stato, dati = posto.post("/api/descrivi", token="di-ieri")
        assert stato == 401


def test_un_metodo_non_previsto_risponde_in_modo_leggibile():
    """La libreria standard risponderebbe con una pagina 501 in inglese.

    I browser mandano OPTIONS da soli (estensioni, service worker) e quel 501
    nudo sembra un guasto del lettore.
    """
    import http.client

    with _Postazione(_tmp("metodo")) as posto:
        connessione = http.client.HTTPConnection("127.0.0.1", posto.server.port, timeout=5)
        connessione.request("OPTIONS", "/")
        risposta = connessione.getresponse()
        corpo = json.loads(risposta.read())
        assert risposta.status == 405
        assert "OPTIONS" in corpo["errore"]
        assert corpo["spiegazione"]


def test_i_file_statici_non_escono_dalla_cartella():
    with _Postazione(_tmp("statici")) as posto:
        stato, _ = posto.get("/../server.py")
        assert stato in (403, 404)


def test_operazione_sconosciuta_e_404():
    with _Postazione(_tmp("ignota")) as posto:
        stato, dati = posto.post("/api/vola")
        assert stato == 404
        assert "sconosciuta" in dati["errore"]


# ---------------------------------------------------------------------------
# Postazione tavoletta
# ---------------------------------------------------------------------------
def test_il_manifesto_non_si_scarica_senza_token():
    """Dentro il manifesto c'e' il token, in `start_url`.

    E' quello che rende funzionante il collegamento salvato sulla schermata
    Home della tavoletta — e per la stessa ragione chi scarica il manifesto ha
    in mano il comando del lettore. Percio' non e' un file statico come gli
    altri: la rotta chiede il token esattamente come le API.
    """
    with _Postazione(_tmp("manifesto")) as posto:
        stato, _ = posto.get("/manifest.webmanifest", token=None)
        assert stato == 401, "il manifesto non deve uscire senza token"

        stato, corpo = posto.get("/manifest.webmanifest")
        assert stato == 200
        manifesto = json.loads(corpo)
        assert posto.server.token in manifesto["start_url"]
        assert "modo=tavoletta" in manifesto["start_url"], (
            "il collegamento salvato deve aprirsi gia' in modalita' tavoletta"
        )
        assert manifesto["display"] == "standalone"
        assert manifesto["icons"], "senza icone Android non installa niente"


def test_le_icone_sono_png_veri():
    """Disegnate in Python, quindi vanno provate: un PNG rotto non si vede.

    Android le scarica per conto suo, senza il token e senza la pagina: se
    fossero protette il collegamento si installerebbe senza icona.
    """
    with _Postazione(_tmp("icone")) as posto:
        for lato in (192, 512):
            stato, corpo = posto.get(f"/icona-{lato}.png", token=None)
            assert stato == 200, f"icona {lato} non servita"
            assert corpo[:8] == b"\x89PNG\r\n\x1a\n", "non e' un PNG"
            # L'IHDR e' il primo pezzo e porta le misure: 8 byte di firma,
            # 4 di lunghezza, 4 di tipo, poi larghezza e altezza.
            larghezza = int.from_bytes(corpo[16:20], "big")
            altezza = int.from_bytes(corpo[20:24], "big")
            assert (larghezza, altezza) == (lato, lato), (
                f"il PNG dichiara {larghezza}x{altezza} invece di {lato}x{lato}"
            )

        stato, _ = posto.get("/icona-999.png", token=None)
        assert stato == 404


def test_l_indirizzo_per_la_tavoletta_esiste_solo_ascoltando_fuori():
    """Sul loopback la tavoletta non arriva, e dirlo e' meta' del lavoro.

    L'indirizzo giusto lo conosce solo il server: il browser vede quello con
    cui e' arrivato, che sul PC e' 127.0.0.1 — da un altro apparecchio non
    porta da nessuna parte.
    """
    with _Postazione(_tmp("indirizzi")) as posto:
        stato, dati = posto.post("/api/indirizzi")
        assert stato == 200
        assert dati["rete"] == [], "in loopback non c'e' nessun indirizzo di rete"
        assert dati["locale"].startswith("http://127.0.0.1:")
        assert dati["token_fisso"] is False


def test_ascoltando_su_tutte_le_schede_l_indirizzo_non_e_0_0_0_0():
    """`http://0.0.0.0:8770` non e' un indirizzo che un browser sappia aprire.

    Stamparlo manderebbe l'operatore a sbattere: al suo posto il loopback per
    questa macchina, e gli IP veri per la tavoletta.
    """
    tmp = _tmp("ovunque")
    server = WebUIServer(
        _config(tmp),
        FakeTagBackend([], antennas=(1, 2, 3)),
        host="0.0.0.0",
        port=8770,
        token="fisso-per-la-tavoletta",
    )
    try:
        assert server.url.startswith("http://127.0.0.1:8770/")
        indirizzi = server.indirizzi()
        assert indirizzi["token_fisso"] is True
        for indirizzo in indirizzi["rete"]:
            assert not indirizzo.startswith("http://0.0.0.0")
            assert not indirizzo.startswith("http://127.")
            assert "modo=tavoletta" in indirizzo
            assert "fisso-per-la-tavoletta" in indirizzo
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Mutua esclusione
# ---------------------------------------------------------------------------
def test_una_sola_operazione_radio_alla_volta():
    """La seconda operazione non aspetta: viene rifiutata con 409.

    Metterla in coda sarebbe peggio che rifiutarla — l'operatore crederebbe di
    aver avviato qualcosa che parte fra dieci secondi, su un tag che nel
    frattempo ha tolto dal piatto.
    """
    cartella = _tmp("concorrenza")
    with _Postazione(cartella, [_tag_vergine(1)]) as posto:
        _accetta(posto, totale=1)
        posto.post("/api/conferma_conteggio")

        # Si rallenta l'inventory per tenere occupata la radio.
        originale = posto.backend.inventory
        rilascia = threading.Event()

        def lento(request):
            rilascia.wait(timeout=5)
            return originale(request)

        posto.backend.inventory = lento
        esiti: list = []

        def scrivi() -> None:
            esiti.append(posto.post("/api/scrivi"))

        thread = threading.Thread(target=scrivi)
        thread.start()
        # Si attende che il lock sia davvero preso, senza dormire a caso.
        scadenza = time.monotonic() + 5
        while not posto.server.busy and time.monotonic() < scadenza:
            time.sleep(0.01)
        assert posto.server.busy

        stato, dati = posto.post("/api/sorveglia")
        assert stato == 409
        assert dati["occupato"] is True

        # Le operazioni che non toccano la radio restano disponibili: senza,
        # l'interfaccia si bloccherebbe proprio mentre ha da aggiornarsi.
        stato, _ = posto.post("/api/stato_accettazione")
        assert stato == 200

        rilascia.set()
        thread.join(timeout=10)
        assert esiti and esiti[0][0] == 200


# ---------------------------------------------------------------------------
# Eventi
# ---------------------------------------------------------------------------
def _leggi_eventi(posto: _Postazione, quanti: int, *, timeout: float = 10.0) -> list[dict]:
    """Apre il flusso SSE e raccoglie `quanti` eventi."""
    richiesta = urllib.request.Request(
        f"{posto.base}/api/eventi?t={posto.server.token}", method="GET"
    )
    raccolti: list[dict] = []
    flusso = urllib.request.urlopen(richiesta, timeout=timeout)
    try:
        scadenza = time.monotonic() + timeout
        while len(raccolti) < quanti and time.monotonic() < scadenza:
            riga = flusso.readline()
            if not riga:
                break
            testo = riga.decode("utf-8").strip()
            if testo.startswith("data: "):
                raccolti.append(json.loads(testo[6:]))
    finally:
        flusso.close()
    return raccolti


def test_i_passi_della_scrittura_escono_mentre_accade():
    """La scena si muove su eventi veri: se il passo non arriva, non e' successo."""
    cartella = _tmp("eventi")
    with _Postazione(cartella, [_tag_vergine(1)]) as posto:
        _accetta(posto, totale=1)
        posto.post("/api/conferma_conteggio")

        raccolti: list[dict] = []
        pronto = threading.Event()

        def ascolta() -> None:
            pronto.set()
            raccolti.extend(_leggi_eventi(posto, 4))

        ascoltatore = threading.Thread(target=ascolta, daemon=True)
        ascoltatore.start()
        pronto.wait(timeout=5)
        # Il flusso deve essere registrato prima di far partire la scrittura.
        scadenza = time.monotonic() + 5
        while posto.server.events.subscriber_count == 0 and time.monotonic() < scadenza:
            time.sleep(0.01)

        stato, _ = posto.post("/api/scrivi")
        assert stato == 200
        ascoltatore.join(timeout=10)

        tipi = [evento["kind"] for evento in raccolti]
        assert tipi[0] == "scrittura"
        fasi = [evento["data"].get("fase") for evento in raccolti]
        assert fasi[0] == "avvio"
        assert "passo" in fasi
        testi = [
            evento["data"].get("testo", "") for evento in raccolti if evento["data"].get("testo")
        ]
        assert testi, "nessun passo riportato"


def test_un_consumatore_lento_perde_eventi_ma_lo_dichiara():
    """Meglio un buco dichiarato che uno stato inventato."""
    with _Postazione(_tmp("lento")) as posto:
        posto.server.events._max_queue = 2
        coda = posto.server.events.subscribe()
        for indice in range(5):
            posto.server.events.publish("prova", {"n": indice})
        eventi = []
        while not coda.empty():
            eventi.append(coda.get_nowait())
        assert len(eventi) == 2
        assert eventi[-1].get("persi") is True


# ---------------------------------------------------------------------------
# Delega al dispatcher RPC
# ---------------------------------------------------------------------------
def test_rpc_delega_al_dispatcher_esistente():
    """`/rpc` non reinterpreta niente: passa il corpo al dispatcher e basta.

    Compresa la negoziazione di versione, che quindi non va riscritta qui.
    """
    from rfid_silion.service import SERVICE_API_VERSION
    from tests.test_service import make_service

    servizio, lettore = make_service()
    cartella = _tmp("rpc")
    server = WebUIServer(_config(cartella), servizio, host="127.0.0.1", port=0)
    server.start_background()
    try:
        base = f"http://127.0.0.1:{server.port}"

        def chiama(corpo: dict) -> dict:
            richiesta = urllib.request.Request(
                f"{base}/rpc", data=json.dumps(corpo).encode("utf-8"), method="POST"
            )
            richiesta.add_header("Content-Type", "application/json")
            richiesta.add_header("X-RFID-Token", server.token)
            with urllib.request.urlopen(richiesta, timeout=10) as risposta:
                return json.loads(risposta.read())

        descrizione = chiama({"jsonrpc": "2.0", "id": "d", "method": "rfid.describe"})
        assert descrizione["result"]["data"]["service_api_version"] == SERVICE_API_VERSION

        avvio = chiama(
            {
                "jsonrpc": "2.0",
                "id": "s",
                "api_version": SERVICE_API_VERSION,
                "method": "rfid.start",
            }
        )
        assert avvio["result"]["ok"] is True

        inventario = chiama(
            {
                "jsonrpc": "2.0",
                "id": "i",
                "api_version": SERVICE_API_VERSION,
                "method": "rfid.inventory",
                "params": {"antennas": [1, 2], "timeout_ms": 500},
            }
        )
        assert inventario["result"]["data"]["unique_epcs"] == [lettore.tags[0].epc]

        # La versione incompatibile viene rifiutata dal dispatcher, non dal server.
        incompatibile = chiama(
            {
                "jsonrpc": "2.0",
                "id": "v",
                "api_version": "9.0",
                "method": "rfid.start",
            }
        )
        assert incompatibile["error"]["data"]["supported"] == SERVICE_API_VERSION
    finally:
        server.shutdown()


def test_rpc_senza_contratto_di_servizio_lo_dice():
    """Con un backend che non e' un servizio, `/rpc` risponde invece di rompersi."""
    with _Postazione(_tmp("rpc_assente")) as posto:
        # Il backend simulato non espone `events`: non e' un servizio completo.
        posto.server.dispatcher = None
        stato, dati = posto.post("/rpc", {"jsonrpc": "2.0", "id": 1, "method": "rfid.describe"})
        assert stato == 200
        assert dati["error"]["code"] == -32601


# ---------------------------------------------------------------------------
# Percorso completo
# ---------------------------------------------------------------------------
def test_percorso_completo_da_accettazione_a_ricezione():
    cartella = _tmp("percorso")
    tags = [_tag_vergine(indice) for indice in range(1, 4)]
    with _Postazione(cartella, tags) as posto:
        posto.post("/api/operatore", {"nome": "dvent"})
        dati = _accetta(posto, totale=3)
        assert dati["totale"] == 3
        assert dati["conteggio_confermato"] is False

        # Senza conferma del conteggio non si scrive niente.
        stato, errore = posto.post("/api/scrivi")
        assert stato == 400
        assert "conferma" in errore["errore"]

        esiti = _scrivi_tutti(posto, tags)
        assert esiti[-1]["scritti"] == 3
        assert all(esito["scrittura"]["ok"] for esito in esiti)

        stato, spedizione = _prepara(posto, "Lab B")
        assert stato == 200
        assert spedizione["attesi"] == 3

        stato, sigillo = posto.post("/api/sigilla")
        assert stato == 200
        assert sigillo["sigillo"]["ok"] is True
        assert sigillo["sigillo"]["trovati"] == 3
        assert sigillo["sigillo"]["attesi"] == 3

        stato, blob = posto.scarica("/api/distinta")
        assert stato == 200
        assert blob.startswith(b"RFIDLIMS-MANIFEST")
        stato, invio = posto.post("/api/conferma_invio")
        assert stato == 200 and invio["stato"] == "sent"

        # Lato destinatario: stessa chiave, stessa cartella.
        with _Postazione(cartella, tags) as arrivo:
            stato, distinta = arrivo.post(
                "/api/importa_distinta",
                {"contenuto_base64": base64.b64encode(blob).decode("ascii")},
            )
            assert stato == 200
            assert distinta["attesi"] == 3
            assert distinta["contenitori"][0]["paziente"].startswith("DELLA VALLE")

            stato, lettura = arrivo.post("/api/leggi_volume")
            assert stato == 200
            assert lettura["riconciliazione"]["ok"] is True
            assert lettura["riconciliazione"]["arrivati"] == 3
            stato, ricevuta = arrivo.post("/api/conferma_ricezione")
            assert stato == 200 and ricevuta["stato"] == "received"

            # A scatola ancora chiusa si sa cosa c'e' dentro, contenitore per
            # contenitore: e' il motivo per cui i dati viaggiano nel tag invece
            # che in un file a parte.
            osservazioni = lettura["rilievo"]["osservazioni"]
            assert len(osservazioni) == 3
            assert all(voce["stato"] == "decodificato" for voce in osservazioni)
            assert [voce["contenitore"] for voce in osservazioni] == ["1/3", "2/3", "3/3"]
            assert all(
                voce["campione"]["paziente"].startswith("DELLA VALLE")
                for voce in osservazioni
            )
            # E l'avvertenza sanitaria arriva prima che qualcuno apra il coperchio.
            assert all(
                "INFECTIOUS" in voce["campione"]["avvertenze"]
                for voce in osservazioni
            )


def test_un_riavvio_riprende_l_accettazione_incompleta():
    cartella = _tmp("ripresa")
    tags = [_tag_vergine(1), _tag_vergine(2)]
    with _Postazione(cartella, tags) as posto:
        _accetta(posto, totale=2)
        _scrivi_tutti(posto, tags[:1])

    with _Postazione(cartella, tags) as ripartita:
        stato, flussi = ripartita.post("/api/riprendi_workflow")
        assert stato == 200
        accettazione = flussi["accettazione"]
        assert accettazione["accession_id"] == 1
        assert accettazione["scritti"] == 1
        assert accettazione["totale"] == 2
        assert accettazione["prossimo"]["campione"]["avvertenze"] == ["INFECTIOUS"]


def test_un_riavvio_conserva_la_prova_integrale_del_sigillo():
    cartella = _tmp("ripresa_sigillo")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        _accetta(posto, totale=1)
        _scrivi_tutti(posto, [tag])
        _prepara(posto, "Lab B")
        stato, esito = posto.post("/api/sigilla")
        assert stato == 200 and esito["sigillo"]["ok"] is True
        passate = esito["sigillo"]["passes"]

    with _Postazione(cartella, [tag]) as ripartita:
        stato, flussi = ripartita.post("/api/riprendi_workflow")
        assert stato == 200
        sigillo = flussi["spedizione"]["sigillo"]
        assert sigillo["ok"] is True
        assert sigillo["expected"] == [tag.epc_hex]
        assert sigillo["passes"] == passate

        stato, blob = ripartita.scarica("/api/distinta")
        assert stato == 200
        assert blob.startswith(b"RFIDLIMS-MANIFEST")


def test_il_sigillo_ripristina_potenze_e_gen2():
    cartella = _tmp("ripristino_radio")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        _accetta(posto, totale=1)
        _scrivi_tutti(posto, [tag])
        _prepara(posto, "Lab B")
        posto.backend.gen2.update(
            {"session": 3, "target": 1, "target_dynamic": False, "rf_mode": 0x71}
        )
        precedente = dict(posto.backend.gen2)

        stato, esito = posto.post("/api/sigilla")
        assert stato == 200 and esito["sigillo"]["ok"] is True
        assert posto.backend.gen2 == precedente
        assert posto.backend.read_power_cdbm == 2900


def test_variazione_del_numero_di_contenitori_in_corso_dopera():
    cartella = _tmp("conteggio")
    with _Postazione(cartella, [_tag_vergine(indice) for indice in range(1, 5)]) as posto:
        _accetta(posto, totale=3)
        stato, dati = posto.post("/api/correggi_conteggio", {"totale": 4})
        assert stato == 200
        assert dati["totale"] == 4
        assert len(dati["contenitori"]) == 4


# ---------------------------------------------------------------------------
# Cio' che non deve mai passare
# ---------------------------------------------------------------------------
def test_mai_un_sigillo_valido_se_manca_un_contenitore():
    cartella = _tmp("mancante")
    tags = [_tag_vergine(indice) for indice in range(1, 4)]
    with _Postazione(cartella, tags) as posto:
        _accetta(posto, totale=3)
        _scrivi_tutti(posto, tags)
        _prepara(posto, "Lab B")

        # Un contenitore esce dal campo: il sigillo deve accorgersene e dire
        # quale, con nome e paziente.
        rimosso = posto.backend.tags.pop()
        stato, esito = posto.post("/api/sigilla")
        assert stato == 200
        sigillo = esito["sigillo"]
        assert sigillo["ok"] is False
        assert sigillo["trovati"] == 2
        assert sigillo["attesi"] == 3
        assert len(sigillo["missing"]) == 1
        assert esito["mancanti_descritti"][0]["paziente"].startswith("DELLA VALLE")
        assert rimosso.epc_hex not in sigillo["found"]


def test_un_tag_gia_scritto_non_si_riscrive():
    cartella = _tmp("riscrittura")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        _accetta(posto, totale=1)
        _scrivi_tutti(posto, [tag])

        # Seconda accettazione, stesso tag ancora sul piatto.
        posto.post("/api/nuova_accettazione")
        _accetta(posto, totale=1)
        posto.post("/api/conferma_conteggio")
        stato, esito = posto.post("/api/scrivi")
        assert stato == 200
        assert esito["scrittura"]["ok"] is False
        assert "gia" in esito["scrittura"]["error"].lower().replace("'", "")


def test_una_distinta_manomessa_non_si_apre():
    cartella = _tmp("manomessa")
    tags = [_tag_vergine(indice) for indice in range(1, 3)]
    with _Postazione(cartella, tags) as posto:
        _accetta(posto, totale=2)
        _scrivi_tutti(posto, tags)
        _prepara(posto, "Lab B")
        posto.post("/api/sigilla")
        _, blob = posto.scarica("/api/distinta")

        guasto = bytearray(blob)
        guasto[-1] ^= 0xFF
        stato, errore = posto.post(
            "/api/importa_distinta",
            {"contenuto_base64": base64.b64encode(bytes(guasto)).decode("ascii")},
        )
        assert stato == 400
        assert "autentica" in errore["errore"]


def test_un_file_qualsiasi_non_e_una_distinta():
    with _Postazione(_tmp("estranea")) as posto:
        stato, errore = posto.post(
            "/api/importa_distinta",
            {"contenuto_base64": base64.b64encode(b"ciao come va").decode("ascii")},
        )
        assert stato == 400
        assert "sistema" in errore["errore"] or "distinta" in errore["errore"]


def test_descrivi_da_al_frontend_il_banco_vero():
    """La scena deve assomigliare al montaggio, non a un montaggio ideale."""
    with _Postazione(_tmp("descrivi")) as posto:
        stato, dati = posto.post("/api/descrivi")
        assert stato == 200
        posizioni = dati["antenne"]["posizioni"]
        assert len(posizioni) == 3
        # Tutte a pavimento: nessuna verticale, sul prototipo.
        assert all(voce["normal"] == [0, 0, 1] for voce in posizioni)
        assert dati["capienza_payload"] > 0
        assert dati["codebook"]["materiali"]


# ---------------------------------------------------------------------------
# Strumenti e calibrazione
# ---------------------------------------------------------------------------
def test_le_potenze_si_impostano_in_dbm_e_arrivano_in_centi_dbm():
    """L'interfaccia parla dBm, il protocollo centi-dBm: la conversione sta qui.

    Sbagliarla di un fattore cento significherebbe trasmettere a 0,2 dBm o
    chiedere 2000 dBm: nel primo caso non legge niente, nel secondo il comando
    viene rifiutato. Vale la pena di un test.
    """
    with _Postazione(_tmp("potenze")) as posto:
        stato, dati = posto.post("/api/descrivi")
        assert stato == 200
        potenze = dati["antenne"]["potenze"]
        assert potenze and all(5 <= voce["lettura"] <= 30 for voce in potenze)

        stato, _ = posto.post(
            "/api/potenze", {"antenne": [{"id": 1, "lettura": 27, "scrittura": 21}]}
        )
        assert stato == 200
        applicate = posto.backend.read_power_cdbm
        assert applicate == 2700

        # E la configurazione in memoria si aggiorna, altrimenti l'operazione
        # successiva rimetterebbe di nascosto i valori del file.
        stato, dati = posto.post("/api/descrivi")
        prima = next(v for v in dati["antenne"]["potenze"] if v["id"] == 1)
        assert prima["lettura"] == 27 and prima["scrittura"] == 21


def test_gen2_lascia_stare_i_campi_vuoti():
    """Un campo vuoto significa «non toccare», e zero non e' vuoto."""
    with _Postazione(_tmp("gen2")) as posto:
        stato, _ = posto.post("/api/gen2", {"session": "", "target": "", "q": "", "rf_mode": ""})
        assert stato == 400  # nessun parametro da applicare

        stato, _ = posto.post("/api/gen2", {"session": "0", "rf_mode": "113"})
        assert stato == 200
        assert posto.backend.gen2.get("session") == 0
        assert posto.backend.gen2.get("rf_mode") == 0x71

        stato, errore = posto.post("/api/gen2", {"session": "9"})
        assert stato == 400
        assert "session" in errore["errore"]


def test_la_campagna_pretende_i_tag_dentro():
    """Senza contenuto dichiarato non c'e' niente da cercare."""
    with _Postazione(_tmp("campagna")) as posto:
        stato, errore = posto.post("/api/campagna", {})
        assert stato == 400
        assert "rilevato" in errore["errore"]


def test_un_epc_non_puo_essere_dentro_e_fuori():
    """Dichiararlo due volte renderebbe la misura insensata."""
    tag = _tag_vergine(1)
    with _Postazione(_tmp("controllo"), [tag]) as posto:
        stato, dentro = posto.post("/api/rileva_controllo", {"posizione": "dentro"})
        assert stato == 200 and dentro["dentro"] == 1

        # Lo stesso tag rilevato «fuori»: esce da «dentro», dove non era.
        stato, fuori = posto.post("/api/rileva_controllo", {"posizione": "fuori"})
        assert stato == 200
        assert fuori["fuori"] == 1 and fuori["dentro"] == 0

        stato, errore = posto.post("/api/rileva_controllo", {"posizione": "altrove"})
        assert stato == 400


def test_il_registro_racconta_le_scritture():
    cartella = _tmp("registro")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        posto.post("/api/operatore", {"nome": "dvent"})
        _accetta(posto, totale=1)
        _scrivi_tutti(posto, [tag])

        stato, registro = posto.post("/api/registro", {"limite": 50})
        assert stato == 200
        operazioni = [voce["operation"] for voce in registro["eventi"]]
        assert "provision" in operazioni
        assert any(voce["operator"] == "dvent" for voce in registro["eventi"])

        stato, parco = posto.post("/api/parco_tag")
        assert stato == 200
        assert parco["per_stato"]["assigned"] == 1


# ---------------------------------------------------------------------------
# Archivio pazienti
# ---------------------------------------------------------------------------
def test_lo_storico_dice_cosa_dove_quando_e_chi():
    """La domanda che arriva da fuori mesi dopo, in una risposta sola."""
    cartella = _tmp("archivio")
    tags = [_tag_vergine(indice) for indice in range(1, 4)]
    with _Postazione(cartella, tags) as posto:
        posto.post("/api/operatore", {"nome": "DV"})
        _accetta(posto, totale=3)
        _scrivi_tutti(posto, tags)
        _prepara(posto, "Ospedale B")
        posto.post("/api/sigilla")
        posto.scarica("/api/distinta")
        posto.post("/api/conferma_invio")

        # Ricerca: cognome, codice fiscale e numero di accettazione portano
        # tutti allo stesso paziente.
        for query in ("Della", CF_PAZIENTE, "1"):
            stato, risposta = posto.post("/api/cerca_paziente", {"query": query})
            assert stato == 200, query
            assert len(risposta["risultati"]) == 1, query
            assert risposta["risultati"][0]["codice_fiscale"] == CF_PAZIENTE

        paziente_id = risposta["risultati"][0]["id"]
        stato, storico = posto.post("/api/storico_paziente", {"patient_id": paziente_id})
        assert stato == 200
        assert len(storico["accettazioni"]) == 1

        riassunto = storico["accettazioni"][0]["riassunto"]
        assert riassunto["pezzi"] == 3
        assert riassunto["scritti"] == 3
        assert riassunto["spediti"] == 3
        assert riassunto["completo"] is True

        spedizione = riassunto["spedizioni"][0]
        assert spedizione["destinazione"] == "Ospedale B"
        assert spedizione["pezzi"] == 3
        assert spedizione["supervisore"] == "DV"
        assert spedizione["sigillo_ok"] == 1
        # Data e ora della partenza, non solo la data.
        assert spedizione["inviata"] and "T" in spedizione["inviata"]


def test_lo_storico_non_dichiara_completo_cio_che_non_lo_e():
    """Se il sigillo non ha trovato tutto, lo storico non lo nasconde."""
    cartella = _tmp("archivio_ko")
    tags = [_tag_vergine(indice) for indice in range(1, 4)]
    with _Postazione(cartella, tags) as posto:
        posto.post("/api/operatore", {"nome": "DV"})
        _accetta(posto, totale=3)
        _scrivi_tutti(posto, tags)
        _prepara(posto, "Ospedale B")
        posto.backend.tags.pop()  # un contenitore non c'e' piu'
        stato, sigillo = posto.post("/api/sigilla")
        assert sigillo["sigillo"]["ok"] is False

        _, risposta = posto.post("/api/cerca_paziente", {"query": CF_PAZIENTE})
        _, storico = posto.post(
            "/api/storico_paziente", {"patient_id": risposta["risultati"][0]["id"]}
        )
        riassunto = storico["accettazioni"][0]["riassunto"]
        assert riassunto["completo"] is False
        assert riassunto["spedizioni"][0]["sigillo_ok"] == 0
        assert "mancanti 1" in riassunto["spedizioni"][0]["sigillo_dettaglio"]


def test_la_traccia_di_un_contenitore_risale_al_paziente():
    """Con un EPC in mano si deve poter risalire a tutto, per un controllo esterno."""
    cartella = _tmp("traccia")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        posto.post("/api/operatore", {"nome": "DV"})
        _accetta(posto, totale=1)
        esiti = _scrivi_tutti(posto, [tag])
        epc = esiti[0]["scrittura"]["epc"]

        stato, traccia = posto.post("/api/traccia_contenitore", {"epc": epc})
        assert stato == 200
        assert traccia["contenitore"]["codice_fiscale"] == CF_PAZIENTE
        assert traccia["contenitore"]["etichetta"] == "1/1"
        assert [evento["operation"] for evento in traccia["eventi"]] == ["provision"]
        assert traccia["eventi"][0]["operator"] == "DV"


def test_annullare_l_accettazione_non_cancella_i_tag_gia_scritti():
    """Un tag scritto esiste nel mondo fisico: fingere di cancellarlo perde un campione."""
    cartella = _tmp("annullo")
    tags = [_tag_vergine(indice) for indice in range(1, 4)]
    with _Postazione(cartella, tags) as posto:
        _accetta(posto, totale=3)
        _scrivi_tutti(posto, tags[:1])  # solo il primo

        stato, esito = posto.post("/api/annulla_accettazione", {})
        assert stato == 200
        assert esito["annullati"] == 2
        assert len(esito["gia_scritti"]) == 1
        assert esito["gia_scritti"][0]["etichetta"] == "1/3"

        # La postazione e' tornata libera.
        stato, dopo = posto.post("/api/stato_accettazione")
        assert dopo["accession_id"] is None
        assert dopo["contenitori"] == []

        # Il contenitore scritto e' ancora spedibile: non e' sparito.
        stato, spedizione = _prepara(posto, "Ospedale B")
        assert stato == 200
        assert spedizione["attesi"] == 1

        stato, errore = posto.post("/api/annulla_accettazione", {})
        assert stato == 400
        assert "nessuna accettazione" in errore["errore"]


# ---------------------------------------------------------------------------
# Anagrafiche
# ---------------------------------------------------------------------------
def test_operatori_e_destinatari_si_configurano():
    with _Postazione(_tmp("anagrafiche")) as posto:
        stato, dati = posto.post(
            "/api/anagrafiche",
            {
                "laboratorio": {"nome": "Anatomia patologica A", "codice": "AP-A"},
                "operatori": [
                    {"codice": "DV", "cognome": "Venturi", "nome": "Daniele"},
                    {"cognome": "Bianchi", "nome": "Anna"},
                ],
                "destinatari": [
                    {"nome": "Ospedale B", "codice": "B", "email": "ap@ospedaleb.it"}
                ],
            },
        )
        assert stato == 200
        # Sigla se c'e', altrimenti cognome: e' quello che si vede a schermo.
        assert [voce["etichetta"] for voce in dati["operatori"]] == ["DV", "Bianchi"]
        assert dati["laboratorio"]["insegna"] == "AP-A"
        assert dati["destinatari"][0]["email"] == "ap@ospedaleb.it"


def test_un_operatore_fuori_elenco_viene_rifiutato():
    """Il registro deve poter dire chi ha scritto un tag, a distanza di mesi."""
    with _Postazione(_tmp("operatore_ignoto")) as posto:
        # Senza elenco configurato si accetta qualunque nome: e' il caso di chi
        # non ha ancora compilato le impostazioni.
        stato, _ = posto.post("/api/operatore", {"nome": "chiunque"})
        assert stato == 200

        posto.post("/api/anagrafiche", {"operatori": [{"codice": "DV", "cognome": "Venturi"}]})
        stato, errore = posto.post("/api/operatore", {"nome": "Rossi"})
        assert stato == 400
        assert "sconosciuto" in errore["errore"]

        stato, _ = posto.post("/api/operatore", {"nome": "DV"})
        assert stato == 200


def test_senza_operatore_non_si_apre_la_catena_di_custodia():
    with _Postazione(_tmp("operatore_obbligatorio")) as posto:
        posto.post("/api/operatore", {"nome": ""})
        stato, errore = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_PAZIENTE,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "contenitori": 1,
            },
        )
        assert stato == 400
        assert "operatore" in errore["errore"]


def test_due_operatori_indistinguibili_vengono_rifiutati():
    """Due voci uguali nel menu sono una trappola: se ne sceglie una a caso."""
    with _Postazione(_tmp("omonimi")) as posto:
        stato, errore = posto.post(
            "/api/anagrafiche",
            {"operatori": [{"cognome": "Rossi"}, {"cognome": "Rossi", "nome": "Ada"}]},
        )
        assert stato == 400
        assert "stessa identita" in errore["errore"]


def test_una_email_sbagliata_si_vede_subito():
    with _Postazione(_tmp("email")) as posto:
        stato, errore = posto.post(
            "/api/anagrafiche",
            {"destinatari": [{"nome": "Ospedale B", "email": "ap.ospedaleb.it"}]},
        )
        assert stato == 400
        assert "email" in errore["errore"]


def test_la_spedizione_pretende_un_destinatario_configurato():
    cartella = _tmp("destinatario")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        _accetta(posto, totale=1)
        _scrivi_tutti(posto, [tag])

        stato, errore = posto.post("/api/prepara_spedizione", {"destinazione": ""})
        assert stato == 400
        assert "destinatario" in errore["errore"]

        posto.post("/api/anagrafiche", {"destinatari": [{"nome": "Ospedale B"}]})
        stato, errore = posto.post("/api/prepara_spedizione", {"destinazione": "Ospedale Z"})
        assert stato == 400
        assert "sconosciuto" in errore["errore"]

        stato, spedizione = _prepara(posto, "Ospedale B")
        assert stato == 200
        assert spedizione["destinazione"] == "Ospedale B"


def test_la_bozza_email_dichiara_che_l_allegato_va_messo_a_mano():
    """Una pagina web non puo' allegare un file: non fingere che lo faccia."""
    cartella = _tmp("bozza")
    tag = _tag_vergine(1)
    with _Postazione(cartella, [tag]) as posto:
        posto.post("/api/operatore", {"nome": "DV"})
        posto.post(
            "/api/anagrafiche",
            {
                "laboratorio": {"nome": "Anatomia A", "email_referente": "capo@lab-a.it"},
                "destinatari": [{"nome": "Ospedale B", "email": "ap@ospedaleb.it"}],
            },
        )
        _accetta(posto, totale=1)
        _scrivi_tutti(posto, [tag])
        _prepara(posto, "Ospedale B")
        posto.post("/api/sigilla")
        posto.scarica("/api/distinta")

        stato, bozza = posto.post("/api/bozza_email")
        assert stato == 200
        assert bozza["a"] == "ap@ospedaleb.it"
        assert bozza["cc"] == "capo@lab-a.it"
        assert bozza["allegato_manuale"] is True
        assert bozza["nome_file"].endswith(".rfidman")
        assert "Anatomia A" in bozza["oggetto"]
        # Nessun dato di paziente nel corpo: viaggia cifrato, non in chiaro
        # dentro una mail.
        assert "MRTMTT25D09F205Z" not in bozza["corpo"]
        assert "DELLA VALLE" not in bozza["corpo"].upper()


# ---------------------------------------------------------------------------
# Impostazioni
# ---------------------------------------------------------------------------
def test_le_impostazioni_dicono_quale_trasporto_e_attivo():
    with _Postazione(_tmp("imp")) as posto:
        stato, dati = posto.post("/api/impostazioni")
        assert stato == 200
        # La configurazione di prova non ha ne' `serial` ne' `tcp`: il default
        # resta il seriale, che e' come si lavora al banco.
        assert dati["trasporto_attivo"] == "seriale"
        assert dati["tcp"]["port"] == 8080
        assert dati["lettore"]["region"] == 0x08
        # Senza percorso di configurazione il salvataggio va dichiarato
        # impossibile, non lasciato fallire dopo.
        assert dati["salvabile"] is False

        stato, errore = posto.post("/api/salva_impostazioni", {})
        assert stato == 400
        assert "configurazione" in errore["errore"]


def test_il_trasporto_inattivo_viene_parcheggiato_non_lasciato():
    """Il trasporto si sceglie per presenza della chiave.

    Lasciare `serial` e `tcp` insieme nel file significherebbe sceglierne uno a
    caso al prossimo avvio: la sezione che non si usa va rinominata.
    """
    cartella = _tmp("parcheggio")
    posto = _Postazione(cartella)
    try:
        posto.server.workflow.config["serial"] = {"port": "COM5", "baudrate": 115200}
        costruita = posto.server.workflow._config_trasporto(
            {"trasporto": "tcp", "host": "192.168.1.100", "port_tcp": 8080}
        )
        assert "tcp" in costruita
        assert "serial" not in costruita
        assert costruita["serial_disabled"]["port"] == "COM5"

        # E viceversa.
        posto.server.workflow.config = costruita
        indietro = posto.server.workflow._config_trasporto(
            {"trasporto": "seriale", "port": "COM7"}
        )
        assert indietro["serial"]["port"] == "COM7"
        assert "tcp" not in indietro
        assert indietro["tcp_disabled"]["host"] == "192.168.1.100"
    finally:
        posto.server.shutdown()


def test_il_collegamento_senza_porta_viene_rifiutato_subito():
    with _Postazione(_tmp("senza_porta")) as posto:
        stato, errore = posto.post("/api/applica_collegamento", {"trasporto": "seriale"})
        assert stato == 400
        assert "porta" in errore["errore"]

        stato, errore = posto.post("/api/applica_collegamento", {"trasporto": "tcp"})
        assert stato == 400
        assert "indirizzo" in errore["errore"]

        stato, errore = posto.post("/api/applica_collegamento", {"trasporto": "piccioni"})
        assert stato == 400


def test_il_salvataggio_riscrive_il_file_e_conserva_il_resto():
    """Salvare non deve perdere le sezioni che l'interfaccia non tocca."""
    import yaml

    cartella = _tmp("salva")
    percorso = cartella / "config.yaml"
    configurazione = _config(cartella)
    configurazione["serial"] = {"port": "COM5", "baudrate": 115200}
    configurazione["una_sezione_che_non_conosciamo"] = {"valore": 42}
    percorso.write_text(yaml.safe_dump(configurazione), encoding="utf-8")

    backend = FakeTagBackend([], antennas=(1, 2))
    server = WebUIServer(
        configurazione, backend, host="127.0.0.1", port=0, config_path=percorso
    )
    server.start_background()
    try:
        richiesta = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/salva_impostazioni",
            data=json.dumps({"trasporto": "tcp", "host": "10.0.0.9", "port_tcp": 9090}).encode(),
            method="POST",
        )
        richiesta.add_header("Content-Type", "application/json")
        richiesta.add_header("X-RFID-Token", server.token)
        with urllib.request.urlopen(richiesta, timeout=10) as risposta:
            assert risposta.status == 200

        salvata = yaml.safe_load(percorso.read_text(encoding="utf-8"))
        assert salvata["tcp"] == {"host": "10.0.0.9", "port": 9090, "timeout_s": 2.0}
        assert "serial" not in salvata
        assert salvata["serial_disabled"]["port"] == "COM5"
        # La sezione ignota e' ancora li'.
        assert salvata["una_sezione_che_non_conosciamo"] == {"valore": 42}
    finally:
        server.shutdown()


def test_le_porte_seriali_si_possono_elencare_sempre():
    """Anche senza lettore collegato la risposta e' una lista, non un errore."""
    with _Postazione(_tmp("porte")) as posto:
        stato, dati = posto.post("/api/porte_seriali")
        assert stato == 200
        assert isinstance(dati["porte"], list)
        for porta in dati["porte"]:
            assert porta["device"]
            assert isinstance(porta["probabile"], bool)


def test_le_avanzate_rifiutano_una_regione_impossibile():
    with _Postazione(_tmp("avanzate")) as posto:
        stato, errore = posto.post("/api/avanzate", {"region": 999})
        assert stato == 400
        assert "byte" in errore["errore"]

        stato, errore = posto.post("/api/avanzate", {})
        assert stato == 400
        assert "nessun parametro" in errore["errore"]


def test_il_duty_cycle_va_impostato_a_coppie():
    """Una finestra senza periodo non significa niente: va detto, non ignorato."""
    from rfid_silion.service import ReaderTuning

    try:
        ReaderTuning(duty_cycle_full_ms=100)
        raise AssertionError("ValueError atteso")
    except ValueError as exc:
        assert "periodo" in str(exc)

    try:
        ReaderTuning(duty_cycle_full_ms=500, duty_cycle_period_ms=100)
        raise AssertionError("ValueError atteso")
    except ValueError as exc:
        assert "superare" in str(exc)

    assert ReaderTuning(duty_cycle_full_ms=100, duty_cycle_period_ms=400).duty_cycle_full_ms == 100


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print(f"{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
