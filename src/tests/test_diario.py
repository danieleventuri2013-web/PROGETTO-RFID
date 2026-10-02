"""Il diario di prototipazione, provato dove verra' usato davvero.

Il diario esiste per rispondere a «cosa e' successo l'ultima volta» quando il
lettore non c'e'. Le promesse che questi test tengono ferme:

* di un inventory resta **il dato grezzo** — EPC, RSSI, antenna, conteggio di
  letture — perche' e' quello che serve per rimettere in piedi la stessa scena
  senza hardware, non un riassunto;
* il canale del browser (`/api/traccia`) **non tocca la radio**, quindi risponde
  anche mentre un sigillo e' in corso: e' proprio allora che serve sapere cosa
  ha premuto l'operatore;
* con il pseudonimo attivo il codice fiscale non compare da nessuna parte, e lo
  stesso paziente ha lo stesso codice anche in una sessione diversa;
* un diario che non riesce a scrivere **non ferma il lavoro**: si spegne e basta;
* le password non finiscono nel file nemmeno per sbaglio.
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag

from rfid_silion.diario import BackendTracciato, Diario, diario_da_config, leggi_diario
from rfid_silion.service import InventoryRequest
from webui.server import WebUIServer

CF_PAZIENTE = "MRTMTT25D09F205Z"


# ---------------------------------------------------------------------------
# Attrezzatura
# ---------------------------------------------------------------------------
def _tmp(nome: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"diario_{nome}_"))


def _config(tmp: Path, **diario: object) -> dict:
    return {
        "reader": {"region": 0x08},
        "antennas": [
            {"id": 1, "read_power": 2900, "write_power": 2000},
            {"id": 2, "read_power": 2900, "write_power": 2000},
            {"id": 3, "read_power": 2000, "write_power": 2000},
        ],
        "tag_access": {"access_password_hex": "00000000", "timeout_ms": 1000},
        "lims": {
            "lab_id": 1,
            "database": str(tmp / "lims.db"),
            "keyring": str(tmp / "keys.json"),
            "user_memory_bytes": 64,
            "write_antennas": [3],
            "read_antennas": [1, 2],
        },
        "webui": {"host": "127.0.0.1", "port": 0},
        "diario": {"attivo": True, "dir": str(tmp / "diario"), **diario},
    }


class _Postazione:
    """Server vero su una porta libera, con backend simulato e diario acceso."""

    def __init__(self, tmp: Path, tags: list[SimulatedTag] | None = None, **diario: object):
        config = _config(tmp, **diario)
        self.diario = diario_da_config(config)
        self.backend = BackendTracciato(
            FakeTagBackend(tags if tags is not None else [], antennas=(1, 2, 3)),
            self.diario,
        )
        self.server = WebUIServer(
            config, self.backend, host="127.0.0.1", port=0, diario=self.diario
        )
        self.server.workflow.imposta_operatore("TEST")
        self.server.start_background()
        assert self.server.call("connetti", {})[0] == 200

    def __enter__(self) -> "_Postazione":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()

    def post(self, percorso: str, dati: dict | None = None):
        richiesta = urllib.request.Request(
            f"http://127.0.0.1:{self.server.port}{percorso}",
            data=json.dumps(dati or {}).encode("utf-8"),
            method="POST",
        )
        richiesta.add_header("Content-Type", "application/json")
        richiesta.add_header("X-RFID-Token", self.server.token)
        try:
            with urllib.request.urlopen(richiesta, timeout=10) as risposta:
                grezzo = risposta.read()
                return risposta.status, (json.loads(grezzo) if grezzo else None)
        except urllib.error.HTTPError as errore:
            grezzo = errore.read()
            return errore.code, (json.loads(grezzo) if grezzo else None)

    def record(self) -> list[dict]:
        """I record scritti finora, riletti dal file come farebbe un umano."""
        return list(leggi_diario(self.diario.percorso))


def _tag(indice: int) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=bytes.fromhex("E2801190200050A1B2C300") + bytes([indice]),
        user_bytes=64,
    )


# ---------------------------------------------------------------------------
# Il dato grezzo della radio
# ---------------------------------------------------------------------------
def test_di_un_inventory_resta_il_dato_grezzo_non_un_riassunto():
    """Senza RSSI e antenna il diario non servirebbe a rifare la scena."""
    cartella = _tmp("radio")
    diario = Diario(cartella)
    backend = BackendTracciato(FakeTagBackend([_tag(1), _tag(2)], antennas=(1, 2)), diario)

    risposta = backend.inventory(InventoryRequest(antennas=(1, 2), timeout_ms=400))
    assert risposta.ok
    diario.chiudi()

    letture = [r for r in leggi_diario(diario.percorso) if r["canale"] == "radio"]
    assert len(letture) == 1, letture
    voce = letture[0]
    assert voce["nome"] == "inventory"
    assert voce["dati"]["ok"] is True
    assert "durata_ms" in voce

    tag = voce["dati"]["dati"]["tags"]
    assert tag, "l'elenco dei tag deve restare per intero"
    for lettura in tag:
        assert lettura["epc"]
        assert lettura["rssi"] is not None
        assert lettura["antenna_id"] in (1, 2)
        assert lettura["read_count"] is not None
    # Anche l'insieme degli EPC unici, che e' cio' che guarda la guardia di
    # scrittura: senza, non si potrebbe ricostruire perche' una scrittura e'
    # stata rifiutata.
    assert voce["dati"]["dati"]["unique_epcs"]


def test_una_chiamata_alla_radio_fallita_resta_col_suo_motivo():
    cartella = _tmp("errore")
    diario = Diario(cartella)
    finto = FakeTagBackend([], antennas=(1, 2))
    finto.read_failures = 1
    backend = BackendTracciato(finto, diario)

    from rfid_silion.service import MemoryBank, ReadRequest

    risposta = backend.read(
        ReadRequest(antennas=(1,), bank=MemoryBank.TID, address=0, word_count=6)
    )
    assert not risposta.ok
    diario.chiudi()

    letture = [r for r in leggi_diario(diario.percorso) if r["nome"] == "read"]
    assert letture and letture[0]["dati"]["ok"] is False
    assert letture[0]["dati"].get("errore"), "un errore senza motivo non serve a niente"


def test_il_backend_tracciato_resta_trasparente():
    """Avvolgere non deve cambiare cosa il backend sembra essere."""
    from rfid_silion.service import RFIDService
    from webui.server import _is_service

    servizio = RFIDService({"serial": {"port": "COM99"}, "reader": {"region": 8}})
    avvolto = BackendTracciato(servizio, Diario.spento())
    assert _is_service(avvolto) is _is_service(servizio) is True
    assert avvolto.state == servizio.state
    assert avvolto.ready == servizio.ready
    assert avvolto.backend is servizio
    # Un metodo che il backend non ha deve restare assente, altrimenti chi
    # verifica il contratto con `hasattr` verrebbe ingannato.
    assert not hasattr(BackendTracciato(FakeTagBackend([]), Diario.spento()), "inesistente")


# ---------------------------------------------------------------------------
# Il canale del browser
# ---------------------------------------------------------------------------
def test_gli_eventi_del_browser_finiscono_nel_diario():
    with _Postazione(_tmp("ui")) as posto:
        stato, dati = posto.post(
            "/api/traccia",
            {
                "eventi": [
                    {"nome": "clic", "id": "scrivi", "testo": "Scrivi il tag"},
                    {"nome": "campo", "id": "cf", "valore": CF_PAZIENTE},
                ]
            },
        )
        assert stato == 200, dati
        assert dati["registrati"] == 2

        interfaccia = [r for r in posto.record() if r["canale"] == "ui"]
        assert [r["nome"] for r in interfaccia] == ["clic", "campo"]
        assert interfaccia[0]["dati"]["id"] == "scrivi"
        assert interfaccia[1]["dati"]["valore"] == CF_PAZIENTE


def test_il_diario_del_browser_non_tocca_la_radio():
    """Deve rispondere anche mentre il lettore e' occupato.

    E' la ragione per cui `traccia` non e' un'operazione radio: se si bloccasse
    durante un sigillo si perderebbe proprio la parte di sessione in cui
    l'operatore sta facendo qualcosa.
    """
    with _Postazione(_tmp("occupato")) as posto:
        posto.server._acquire("sigilla")
        try:
            stato, dati = posto.post("/api/traccia", {"eventi": [{"nome": "clic"}]})
            assert stato == 200, dati
            # Controprova: un'operazione radio nello stesso momento riceve 409.
            occupato, _ = posto.post("/api/sorveglia")
            assert occupato == 409
        finally:
            posto.server._release()


def test_i_fotogrammi_webcam_non_finiscono_nel_diario():
    from unittest.mock import patch

    with _Postazione(_tmp("webcam")) as posto:
        before = len(posto.record())
        with patch("webui.qr_camera.decodifica_fotogramma", return_value={"codici": []}):
            code, _ = posto.post("/api/decodifica_qr_camera", {"immagine_base64": "FOTOGRAMMA_PRIVATO"})
            assert code == 200
        assert len(posto.record()) == before
        assert "FOTOGRAMMA_PRIVATO" not in posto.diario.percorso.read_text(encoding="utf-8")


def test_un_pin_non_finisce_nel_diario():
    """La pagina gia' non lo guarda; il server non ci fa affidamento."""
    with _Postazione(_tmp("segreti")) as posto:
        stato, _ = posto.post(
            "/api/traccia",
            {
                "eventi": [
                    {"nome": "campo", "id": "pin-deroga", "valore": "1234"},
                    {"nome": "credenziale", "password": "sissignore"},
                    {"nome": "campo", "id": "cf", "valore": CF_PAZIENTE},
                ]
            },
        )
        assert stato == 200
        testo = posto.diario.percorso.read_text(encoding="utf-8")
        assert "1234" not in testo
        assert "sissignore" not in testo
        # Ma il fatto che quel campo sia stato compilato resta: serve a
        # ricostruire il flusso.
        assert "pin-deroga" in testo
        assert CF_PAZIENTE in testo


def test_il_pin_della_deroga_non_finisce_nel_diario_nemmeno_dall_api():
    """`conferma_invio` porta il PIN del responsabile nel corpo della richiesta."""
    with _Postazione(_tmp("deroga")) as posto:
        posto.post(
            "/api/conferma_invio",
            {"motivo_deroga": "corriere gia' partito", "pin": "999111"},
        )
        testo = posto.diario.percorso.read_text(encoding="utf-8")
        assert "999111" not in testo
        # Il motivo invece serve: e' la ragione per cui e' stata autorizzata.
        assert "corriere gia' partito" in testo


def test_un_lotto_smisurato_viene_rifiutato():
    with _Postazione(_tmp("lotto")) as posto:
        stato, dati = posto.post(
            "/api/traccia", {"eventi": [{"nome": "clic"} for _ in range(500)]}
        )
        assert stato == 400
        assert "troppi eventi" in dati["errore"]

        stato, dati = posto.post("/api/traccia", {"eventi": "non un elenco"})
        assert stato == 400


def test_l_interruttore_spegne_il_browser_e_lascia_la_radio():
    with _Postazione(_tmp("meta"), [_tag(1)], interfaccia=False) as posto:
        stato, dati = posto.post("/api/traccia", {"eventi": [{"nome": "clic"}]})
        assert stato == 200
        assert dati == {"registrati": 0, "diario": False}
        assert posto.server.workflow.descrivi()["diario"]["interfaccia"] is False

        posto.post("/api/sorveglia")
        canali = {r["canale"] for r in posto.record()}
        assert "ui" not in canali
        assert "radio" in canali, "la radio deve restare tracciata"


# ---------------------------------------------------------------------------
# Le operazioni e gli eventi del server
# ---------------------------------------------------------------------------
def test_una_operazione_lascia_richiesta_risposta_e_durata():
    with _Postazione(_tmp("api")) as posto:
        stato, _ = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_PAZIENTE,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "sesso": "M",
                "contenitori": 1,
                "descrizione": "pezzo operatorio",
            },
        )
        assert stato == 200

        api = [r for r in posto.record() if r["canale"] == "api" and r["nome"] == "registra"]
        assert len(api) == 1
        voce = api[0]
        assert voce["dati"]["stato"] == 200
        assert voce["dati"]["richiesta"]["codice_fiscale"] == CF_PAZIENTE
        assert voce["dati"]["risposta"]["accession_id"]
        assert voce["durata_ms"] >= 0


def test_un_errore_dell_operatore_resta_scritto():
    with _Postazione(_tmp("rifiuto")) as posto:
        stato, _ = posto.post("/api/registra", {"codice_fiscale": "non valido"})
        assert stato == 400
        api = [r for r in posto.record() if r["nome"] == "registra"]
        assert api[-1]["dati"]["stato"] == 400
        assert api[-1]["dati"]["risposta"]["errore"]


def test_gli_eventi_trasmessi_al_browser_restano_anche_nel_file():
    with _Postazione(_tmp("sse")) as posto:
        posto.server.events.publish("scrittura", {"fase": "avvio"})
        eventi = [r for r in posto.record() if r["canale"] == "evento"]
        assert eventi and eventi[-1]["nome"] == "scrittura"
        assert eventi[-1]["dati"]["fase"] == "avvio"


# ---------------------------------------------------------------------------
# Riservatezza
# ---------------------------------------------------------------------------
def test_il_pseudonimo_nasconde_il_paziente_e_resta_confrontabile():
    cartella = _tmp("pseudonimo")
    primo = Diario(cartella, pseudonimo=True)
    primo.scrivi("api", "registra", {"codice_fiscale": CF_PAZIENTE, "cognome": "Rossi"})
    primo.chiudi()

    # Sessione diversa, stesso paziente: il codice deve coincidere, altrimenti
    # non si puo' seguire un caso nei giorni.
    secondo = Diario(cartella, pseudonimo=True)
    secondo.scrivi("api", "registra", {"codice_fiscale": CF_PAZIENTE, "cognome": "Rossi"})
    secondo.chiudi()

    uno = list(leggi_diario(primo.percorso))[0]["dati"]
    due = list(leggi_diario(secondo.percorso))[0]["dati"]
    assert primo.percorso != secondo.percorso
    assert uno["codice_fiscale"].startswith("PZ-")
    assert uno["codice_fiscale"] == due["codice_fiscale"]
    assert uno["cognome"] != "Rossi"
    assert CF_PAZIENTE not in primo.percorso.read_text(encoding="utf-8")


def test_il_pseudonimo_lascia_leggibile_il_campione():
    """Anonimizzare la descrizione del reperto renderebbe il diario inutile."""
    cartella = _tmp("reperto")
    diario = Diario(cartella, pseudonimo=True)
    diario.scrivi(
        "api",
        "registra",
        {"cognome": "Rossi", "descrizione": "pezzo operatorio", "material_code": 3},
    )
    diario.chiudi()
    dati = list(leggi_diario(diario.percorso))[0]["dati"]
    assert dati["descrizione"] == "pezzo operatorio"
    assert dati["material_code"] == 3


# ---------------------------------------------------------------------------
# Robustezza
# ---------------------------------------------------------------------------
def test_un_diario_che_non_scrive_non_ferma_il_lavoro():
    import logging

    diario = Diario(_tmp("guasto"))
    diario._file.close()  # come un disco pieno o una chiavetta staccata
    # La traccia di stack e' voluta e finisce nel log: qui si zittisce solo
    # perche' non sporchi l'uscita dei test.
    logging.getLogger("rfid.diario").setLevel(logging.CRITICAL)
    try:
        diario.scrivi("api", "registra", {"cognome": "Rossi"})  # non deve sollevare
    finally:
        logging.getLogger("rfid.diario").setLevel(logging.NOTSET)
    # E dopo il primo fallimento si spegne, invece di ritentare a ogni record.
    assert diario.attivo is False
    diario.scrivi("api", "registra", {"cognome": "Rossi"})
    diario.chiudi()


def test_il_diario_spento_non_apre_nessun_file():
    diario = Diario.spento()
    diario.scrivi("api", "qualcosa", {"a": 1})
    diario.nota("altro", b=2)
    assert diario.percorso is None
    assert diario.attivo is False


def test_la_rotazione_apre_un_secondo_file():
    cartella = _tmp("rotazione")
    diario = Diario(cartella, max_mb=0.001)  # ~1 kB
    primo = diario.percorso
    for indice in range(200):
        diario.scrivi("nota", "riempimento", {"indice": indice, "zeppa": "x" * 40})
    assert diario.percorso != primo, "superata la soglia va aperto un file nuovo"
    assert primo.exists() and diario.percorso.exists()
    diario.chiudi()


def test_le_righe_rotte_non_fanno_perdere_la_sessione():
    """L'ultima riga di un file interrotto da Ctrl+C e' spesso monca."""
    cartella = _tmp("monco")
    diario = Diario(cartella)
    diario.scrivi("nota", "buona", {"a": 1})
    diario.chiudi()
    with diario.percorso.open("a", encoding="utf-8") as file:
        file.write('{"t": "adesso", "canale": "nota"')  # troncata

    record = list(leggi_diario(diario.percorso))
    assert len(record) == 1 and record[0]["nome"] == "buona"


def test_un_blob_enorme_non_gonfia_il_file():
    cartella = _tmp("blob")
    diario = Diario(cartella)
    diario.scrivi("api", "importa_distinta", {"contenuto_base64": "A" * 200_000})
    diario.chiudi()
    testo = diario.percorso.read_text(encoding="utf-8")
    assert len(testo) < 2000
    assert "in tutto 200000 caratteri" in testo


# ---------------------------------------------------------------------------
# Rilettura
# ---------------------------------------------------------------------------
def test_il_riassunto_racconta_la_sessione():
    from app import diario_cli

    with _Postazione(_tmp("riassunto"), [_tag(1)]) as posto:
        posto.post("/api/traccia", {"eventi": [{"nome": "clic", "testo": "Collega"}]})
        posto.post("/api/sorveglia")
        posto.post("/api/registra", {"codice_fiscale": "sbagliato"})
        record = posto.record()

    # Il riassunto e' testo per una persona: si verifica che non esploda e che
    # nomini le tre cose che deve nominare.
    import io
    from contextlib import redirect_stdout

    uscita = io.StringIO()
    with redirect_stdout(uscita):
        diario_cli.riassunto(record, Path("diario_di_prova.jsonl"))
    testo = uscita.getvalue()
    assert "Per canale" in testo
    assert "Tag visti dalla radio" in testo
    assert "Errori" in testo
    assert "AAAAAAAAAAAAAAAAAAAA0001" in testo, "l'EPC visto deve comparire"

    uscita = io.StringIO()
    with redirect_stdout(uscita):
        diario_cli.timeline(record)
    assert "sorveglia" in uscita.getvalue()


def test_si_trova_da_solo_l_ultimo_diario():
    from app import diario_cli

    cartella = _tmp("ultimo")
    diario = Diario(cartella)
    diario.scrivi("nota", "prova", {})
    diario.chiudi()
    assert diario_cli.trova_diario(str(cartella)) == diario.percorso
    assert diario_cli.trova_diario(str(diario.percorso)) == diario.percorso


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
