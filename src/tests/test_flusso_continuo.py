"""Il flusso descritto in `flusso-di-lavoro.txt`, provato via HTTP.

I campioni arrivano gia' etichettati, in ordine sparso, e si trascrivono uno
alla volta. Da questo discendono le promesse che questi test tengono ferme:

* **un contenitore solo non fa domande**: la conferma del numero si chiede da
  due in su, dove il totale finisce nel chip e diventa irreversibile;
* che lo stesso paziente abbia gia' altri campioni si scopre solo alla
  trascrizione successiva, e allora il sistema **lo dice** — senza bloccare
  niente, perche' due campioni dello stesso paziente sono normali;
* il **residuo da spedire** viaggia con ogni risposta che lo cambia: e' il
  numero che impedisce di finire la giornata con un campione scritto e mai
  partito;
* si possono scrivere **piu' tag di quanti ne entrino in una scatola**: una
  scatola sigillata non impedisce di riempire la successiva, ma una ancora
  aperta si'.
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

from webui.server import WebUIServer

#: Codici fiscali validi: il carattere di controllo viene verificato davvero,
#: quindi non se ne possono inventare.
CF_UNO = "MRTMTT25D09F205Z"
CF_DUE = "RSSMRA80A01H501U"


def _tmp(nome: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"flusso_{nome}_"))


def _config(tmp: Path) -> dict:
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
            "seal_min_antennas": 1,
            "seal_powers_cdbm": [2000, 2900],
        },
        "destinatari": [
            {"nome": "Ospedale Foligno", "codice": "OF", "attivo": True},
            {"nome": "Ospedale Terni", "codice": "OT", "attivo": True},
        ],
        "webui": {"host": "127.0.0.1", "port": 0},
        "diario": {"attivo": False},
    }


def _tag(indice: int) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=bytes.fromhex("E2801190200050A1B2C300") + bytes([indice]),
        user_bytes=64,
    )


class _Postazione:
    def __init__(self, tmp: Path, tag: list[SimulatedTag] | None = None):
        self.backend = FakeTagBackend(tag if tag is not None else [], antennas=(1, 2, 3))
        self.server = WebUIServer(_config(tmp), self.backend, host="127.0.0.1", port=0)
        self.server.workflow.imposta_operatore("TEST")
        self.server.start_background()

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
            with urllib.request.urlopen(richiesta, timeout=15) as risposta:
                grezzo = risposta.read()
                return risposta.status, (json.loads(grezzo) if grezzo else None)
        except urllib.error.HTTPError as errore:
            grezzo = errore.read()
            return errore.code, (json.loads(grezzo) if grezzo else None)

    # -- gesti del banco --------------------------------------------------
    def trascrivi(self, codice_fiscale: str, cognome: str, *, contenitori: int = 1, **extra):
        """Una trascrizione, come dall'etichetta del reparto."""
        dati = {
            "codice_fiscale": codice_fiscale,
            "cognome": cognome,
            "nome": "Prova",
            "sesso": "M",
            "contenitori": contenitori,
            "descrizione": "pezzo operatorio",
            "reparto": "Chirurgia generale",
            "material_code": 1,
            "fixative_code": 1,
            "site_code": 1,
        }
        dati.update(extra)
        return self.post("/api/registra", dati)

    def scrivi_su(self, tag: SimulatedTag):
        """Appoggia un tag sulla postazione e scrive: uno per volta, come al banco."""
        tutti = list(self.backend.tags)
        self.backend.tags[:] = [tag]
        try:
            return self.post("/api/scrivi")
        finally:
            self.backend.tags[:] = tutti


def _scrivi_campione(posto: _Postazione, cf: str, cognome: str, tag: SimulatedTag) -> str:
    """Trascrivi, scrivi il tag, chiudi l'accettazione. Restituisce l'EPC."""
    stato, _ = posto.trascrivi(cf, cognome)
    assert stato == 200
    stato, esito = posto.scrivi_su(tag)
    assert stato == 200 and esito["scrittura"]["ok"], esito
    posto.post("/api/nuova_accettazione")
    return esito["scrittura"]["epc"]


# ---------------------------------------------------------------------------
# Trascrizione
# ---------------------------------------------------------------------------
def test_un_contenitore_solo_non_chiede_la_conferma():
    """Con un vasetto in mano, «li hai contati?» non ha oggetto."""
    with _Postazione(_tmp("uno")) as posto:
        stato, dati = posto.trascrivi(CF_UNO, "Della Valle", contenitori=1)
        assert stato == 200
        assert dati["conteggio_confermato"] is True
        # E si puo' scrivere subito, senza passare da `conferma_conteggio`.
        stato, esito = posto.scrivi_su(_tag(1))
        assert stato == 200 and esito["scrittura"]["ok"], esito


def test_da_due_in_su_la_conferma_resta_obbligatoria():
    """Li' il totale entra nel chip e non si corregge piu'."""
    with _Postazione(_tmp("tre")) as posto:
        stato, dati = posto.trascrivi(CF_UNO, "Della Valle", contenitori=3)
        assert stato == 200
        assert dati["conteggio_confermato"] is False

        stato, errore = posto.scrivi_su(_tag(1))
        assert stato == 400
        assert "conferma del numero" in errore["errore"]

        posto.post("/api/conferma_conteggio")
        stato, esito = posto.scrivi_su(_tag(1))
        assert stato == 200 and esito["scrittura"]["ok"]


def test_la_conferma_implicita_resta_nel_registro():
    """Chi ha dichiarato cosa deve restare scritto, anche quando e' ovvio."""
    with _Postazione(_tmp("registro")) as posto:
        posto.trascrivi(CF_UNO, "Della Valle", contenitori=1)
        _, registro = posto.post("/api/registro", {"limite": 20})
        conferme = [
            voce for voce in registro["eventi"] if voce["operation"] == "count_confirm"
        ]
        assert conferme, registro["eventi"]
        assert "conferma implicita" in conferme[0]["detail"]
        assert conferme[0]["operator"] == "TEST"


def test_il_secondo_campione_dello_stesso_paziente_viene_segnalato():
    """Lo si scopre solo adesso: prima non c'era modo di saperlo."""
    with _Postazione(_tmp("bis")) as posto:
        stato, primo = posto.trascrivi(CF_UNO, "Della Valle")
        assert primo["paziente_gia_noto"]["contenitori"] == 0
        prima_accettazione = primo["accession_id"]
        posto.scrivi_su(_tag(1))
        posto.post("/api/nuova_accettazione")

        stato, secondo = posto.trascrivi(CF_UNO, "Della Valle")
        assert stato == 200
        nota = secondo["paziente_gia_noto"]
        assert nota["contenitori"] == 1
        assert nota["accettazioni"] == [prima_accettazione]
        assert nota["scritti"] == 1
        # Ma non blocca: il campione si scrive lo stesso.
        stato, esito = posto.scrivi_su(_tag(2))
        assert stato == 200 and esito["scrittura"]["ok"]


def test_un_paziente_diverso_non_viene_segnalato():
    with _Postazione(_tmp("diverso")) as posto:
        posto.trascrivi(CF_UNO, "Della Valle")
        posto.scrivi_su(_tag(1))
        posto.post("/api/nuova_accettazione")
        _, secondo = posto.trascrivi(CF_DUE, "Rossi")
        assert secondo["paziente_gia_noto"]["contenitori"] == 0


def test_un_contenitore_annullato_non_conta_fra_gli_altri():
    """Un contenitore annullato non esiste piu': segnalarlo sarebbe un falso."""
    with _Postazione(_tmp("annullato")) as posto:
        posto.trascrivi(CF_UNO, "Della Valle", contenitori=2)
        posto.post("/api/annulla_accettazione", {"motivo": "vassoio sbagliato"})
        _, secondo = posto.trascrivi(CF_UNO, "Della Valle")
        assert secondo["paziente_gia_noto"]["contenitori"] == 0


# ---------------------------------------------------------------------------
# Il residuo da spedire
# ---------------------------------------------------------------------------
def test_il_residuo_viaggia_con_le_risposte_che_lo_cambiano():
    with _Postazione(_tmp("residuo")) as posto:
        _, dati = posto.trascrivi(CF_UNO, "Della Valle")
        assert dati["residuo_da_spedire"] == 0

        _, esito = posto.scrivi_su(_tag(1))
        assert esito["residuo_da_spedire"] == 1, "dopo la scrittura c'e' un campione in attesa"

        _, spedizione = posto.post("/api/stato_spedizione")
        assert spedizione["residuo_da_spedire"] == 1


def test_il_residuo_si_azzera_quando_i_campioni_entrano_in_una_spedizione():
    with _Postazione(_tmp("azzera")) as posto:
        _scrivi_campione(posto, CF_UNO, "Della Valle", _tag(1))
        _scrivi_campione(posto, CF_DUE, "Rossi", _tag(2))
        _, coda = posto.post("/api/coda_spedizione")
        assert coda["totale"] == 2

        ids = [v["container_id"] for g in coda["gruppi"] for v in g["contenitori"]]
        _, spedizione = posto.post(
            "/api/prepara_spedizione",
            {"destinazione": "Ospedale Foligno", "container_ids": ids},
        )
        assert spedizione["residuo_da_spedire"] == 0


# ---------------------------------------------------------------------------
# Piu' tag di quanti ne entrino in una scatola
# ---------------------------------------------------------------------------
def _prepara(posto: _Postazione, destinazione: str, epcs: list[str]):
    _, coda = posto.post("/api/coda_spedizione")
    per_epc = {
        v["epc"]: v["container_id"] for g in coda["gruppi"] for v in g["contenitori"]
    }
    ids = [per_epc[epc] for epc in epcs if epc in per_epc]
    return posto.post(
        "/api/prepara_spedizione", {"destinazione": destinazione, "container_ids": ids}
    )


def test_una_scatola_ancora_aperta_blocca_la_successiva():
    """Una scatola aperta e' una scatola che si sta riempiendo adesso."""
    with _Postazione(_tmp("aperta")) as posto:
        primo = _scrivi_campione(posto, CF_UNO, "Della Valle", _tag(1))
        secondo = _scrivi_campione(posto, CF_DUE, "Rossi", _tag(2))

        stato, _ = _prepara(posto, "Ospedale Foligno", [primo])
        assert stato == 200
        stato, errore = _prepara(posto, "Ospedale Terni", [secondo])
        assert stato == 400
        assert "ancora aperta" in errore["errore"]


def test_una_scatola_sigillata_non_blocca_la_successiva():
    """E' il caso normale: sessanta tag scritti, venti per scatola."""
    cartella = _tmp("due_scatole")
    tag = [_tag(indice) for indice in range(1, 3)]
    with _Postazione(cartella, tag) as posto:
        primo = _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        secondo = _scrivi_campione(posto, CF_DUE, "Rossi", tag[1])

        # Prima scatola: dentro c'e' solo il primo campione.
        assert _prepara(posto, "Ospedale Foligno", [primo])[0] == 200
        posto.backend.tags[:] = [tag[0]]
        stato, sigillo = posto.post("/api/sigilla")
        assert stato == 200 and sigillo["sigillo"]["ok"], sigillo["sigillo"]
        prima_spedizione = sigillo["shipment_id"]

        # Seconda scatola, subito: la prima aspetta il corriere.
        stato, seconda = _prepara(posto, "Ospedale Terni", [secondo])
        assert stato == 200, seconda
        assert seconda["shipment_id"] != prima_spedizione
        assert seconda["destinazione"] == "Ospedale Terni"
        assert [v["epc"] for v in seconda["contenitori"]] == [secondo]


def test_le_spedizioni_aperte_dicono_cosa_manca():
    """Senza questo elenco la scatola precedente sparirebbe dall'interfaccia."""
    cartella = _tmp("elenco")
    tag = [_tag(indice) for indice in range(1, 3)]
    with _Postazione(cartella, tag) as posto:
        primo = _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        secondo = _scrivi_campione(posto, CF_DUE, "Rossi", tag[1])

        _prepara(posto, "Ospedale Foligno", [primo])
        posto.backend.tags[:] = [tag[0]]
        _, sigillo = posto.post("/api/sigilla")
        prima = sigillo["shipment_id"]
        _prepara(posto, "Ospedale Terni", [secondo])

        stato, elenco = posto.post("/api/spedizioni_aperte")
        assert stato == 200
        per_id = {voce["shipment_id"]: voce for voce in elenco["spedizioni"]}
        assert len(per_id) == 2
        assert per_id[prima]["stato"] == "sealed"
        assert per_id[prima]["da_fare"] == "distinta da mandare"
        assert per_id[prima]["pezzi"] == 1
        assert elenco["attiva"] != prima, "quella corrente e' la seconda scatola"

        altra = next(voce for voce in elenco["spedizioni"] if voce["shipment_id"] != prima)
        assert altra["da_fare"] == "da sigillare"


def test_si_torna_su_una_spedizione_precedente():
    cartella = _tmp("riapri")
    tag = [_tag(indice) for indice in range(1, 3)]
    with _Postazione(cartella, tag) as posto:
        primo = _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        secondo = _scrivi_campione(posto, CF_DUE, "Rossi", tag[1])
        _prepara(posto, "Ospedale Foligno", [primo])
        posto.backend.tags[:] = [tag[0]]
        _, sigillo = posto.post("/api/sigilla")
        prima = sigillo["shipment_id"]
        _prepara(posto, "Ospedale Terni", [secondo])

        stato, tornata = posto.post("/api/riapri_spedizione", {"shipment_id": prima})
        assert stato == 200
        assert tornata["shipment_id"] == prima
        assert tornata["destinazione"] == "Ospedale Foligno"
        # Lo stato non cambia: «riaprire» qui vuol dire tornare a guardarla,
        # non poterci rimettere le mani dentro.
        assert tornata["stato"] == "sealed"
        # E il sigillo gia' fatto e' ancora li'.
        assert tornata["sigillo"]["ok"] is True


def test_una_spedizione_annullata_non_si_riapre():
    with _Postazione(_tmp("annullata")) as posto:
        primo = _scrivi_campione(posto, CF_UNO, "Della Valle", _tag(1))
        _, spedizione = _prepara(posto, "Ospedale Foligno", [primo])
        identificativo = spedizione["shipment_id"]
        posto.post("/api/annulla_spedizione")

        stato, errore = posto.post("/api/riapri_spedizione", {"shipment_id": identificativo})
        assert stato == 400
        assert "annullata" in errore["errore"]


# ---------------------------------------------------------------------------
# Riempire la scatola un campione per volta
# ---------------------------------------------------------------------------
def test_i_campioni_entrano_uno_per_volta_e_la_scatola_si_compone_da_sola():
    """Il gesto e' appoggiare il campione: nessun pulsante dice «l'ho messo»."""
    cartella = _tmp("riempimento")
    tag = [_tag(indice) for indice in range(1, 4)]
    with _Postazione(cartella, tag) as posto:
        epc = [
            _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0]),
            _scrivi_campione(posto, CF_DUE, "Rossi", tag[1]),
        ]
        posto.backend.tags.clear()

        stato, avvio = posto.post(
            "/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"}
        )
        assert stato == 200, avvio
        assert avvio["riempimento"]["quanti"] == 0
        scatola = avvio["shipment_id"]

        # Primo campione dentro.
        posto.backend.tags.append(tag[0])
        stato, giro = posto.post("/api/sorveglia_scatola")
        assert stato == 200, giro
        assert [e["esito"] for e in giro["eventi"]] == ["entrato"]
        assert giro["eventi"][0]["paziente"] == "DELLA VALLE PROVA"
        assert giro["quanti"] == 1

        # Secondo campione: il primo non si riannuncia.
        posto.backend.tags.append(tag[1])
        stato, giro = posto.post("/api/sorveglia_scatola")
        assert [e["esito"] for e in giro["eventi"]] == ["entrato"]
        assert giro["quanti"] == 2

        # E la spedizione contiene esattamente cio' che e' entrato.
        stato, spedizione = posto.post("/api/stato_spedizione")
        assert spedizione["shipment_id"] == scatola
        assert sorted(v["epc"] for v in spedizione["contenitori"]) == sorted(epc)
        assert spedizione["residuo_da_spedire"] == 0


def test_un_campione_non_inizializzato_viene_dichiarato():
    """Il punto 4 del flusso: il tag mai scritto va detto a voce alta."""
    cartella = _tmp("vergine")
    tag = [_tag(1), _tag(2)]
    with _Postazione(cartella, tag) as posto:
        _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})

        # Nella scatola finisce un contenitore mai preparato.
        posto.backend.tags.append(tag[1])
        stato, giro = posto.post("/api/sorveglia_scatola")
        assert stato == 200
        assert [e["esito"] for e in giro["eventi"]] == ["non_inizializzato"]
        assert giro["eventi"][0]["anomalia"] is True
        assert giro["quanti"] == 0, "un problema non e' un campione"
        assert len(giro["anomalie"]) == 1


def test_il_riempimento_racconta_ogni_ingresso_sul_flusso_eventi():
    """La scena e il bip si muovono su questi, non su un timer."""
    import queue

    cartella = _tmp("eventi")
    tag = [_tag(1)]
    with _Postazione(cartella, tag) as posto:
        _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        posto.backend.tags.clear()
        coda = posto.server.events.subscribe()
        try:
            posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
            posto.backend.tags.append(tag[0])
            posto.post("/api/sorveglia_scatola")

            fasi = []
            while True:
                try:
                    evento = coda.get(timeout=0.5)
                except queue.Empty:
                    break
                if evento["kind"] == "riempimento":
                    fasi.append(evento["data"])
        finally:
            posto.server.events.unsubscribe(coda)

    assert [f["fase"] for f in fasi] == ["avvio", "tag", "conteggio"], fasi
    assert fasi[1]["esito"] == "entrato"
    assert fasi[1]["paziente"] == "DELLA VALLE PROVA"
    assert fasi[2]["quanti"] == 1


def test_un_campione_aggiunto_per_sbaglio_si_toglie_e_non_rientra():
    """Capita quando la lettura arriva a un contenitore appoggiato accanto."""
    cartella = _tmp("correzione")
    tag = [_tag(1), _tag(2)]
    with _Postazione(cartella, tag) as posto:
        primo = _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        _scrivi_campione(posto, CF_DUE, "Rossi", tag[1])
        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})

        posto.backend.tags[:] = [tag[0], tag[1]]
        stato, giro = posto.post("/api/sorveglia_scatola")
        assert giro["quanti"] == 2

        stato, corretto = posto.post("/api/togli_dalla_scatola", {"epc": primo})
        assert stato == 200, corretto
        assert corretto["quanti"] == 1
        assert primo not in corretto["epc"]
        # Torna disponibile per un'altra scatola.
        assert corretto["residuo_da_spedire"] == 1

        # E al giro dopo non rientra da solo, anche se e' ancora nel campo.
        stato, giro = posto.post("/api/sorveglia_scatola")
        assert giro["quanti"] == 1, giro


def test_la_scatola_vuota_non_si_chiude():
    with _Postazione(_tmp("vuota")) as posto:
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        stato, errore = posto.post("/api/chiudi_riempimento")
        assert stato == 400
        assert "vuota" in errore["errore"]


def test_dal_riempimento_si_passa_al_sigillo_senza_scegliere_niente():
    """L'elenco atteso e' quello che e' entrato: nessuna coda da spuntare."""
    cartella = _tmp("al_sigillo")
    tag = [_tag(1), _tag(2)]
    with _Postazione(cartella, tag) as posto:
        epc = [
            _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0]),
            _scrivi_campione(posto, CF_DUE, "Rossi", tag[1]),
        ]
        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        posto.backend.tags[:] = list(tag)
        posto.post("/api/sorveglia_scatola")

        stato, chiusa = posto.post("/api/chiudi_riempimento")
        assert stato == 200
        assert chiusa["attesi"] == 2

        stato, sigillo = posto.post("/api/sigilla")
        assert stato == 200, sigillo
        assert sigillo["sigillo"]["ok"] is True
        assert sorted(sigillo["sigillo"]["found"]) == sorted(epc)


def test_il_sigillo_scopre_il_campione_che_non_e_davvero_nella_scatola():
    """Il riempimento vede a coperchio aperto; il sigillo certifica chiuso.

    E' la ragione per cui servono entrambi: se un contenitore era appoggiato
    accanto invece che dentro, il riempimento lo conta e il sigillo no.
    """
    cartella = _tmp("bugia")
    tag = [_tag(1), _tag(2)]
    with _Postazione(cartella, tag) as posto:
        _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        fuori = _scrivi_campione(posto, CF_DUE, "Rossi", tag[1])
        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        posto.backend.tags[:] = list(tag)
        posto.post("/api/sorveglia_scatola")
        posto.post("/api/chiudi_riempimento")

        # Il coperchio si chiude e quel contenitore resta fuori.
        posto.backend.tags[:] = [tag[0]]
        stato, sigillo = posto.post("/api/sigilla")
        assert stato == 200
        assert sigillo["sigillo"]["ok"] is False
        assert sigillo["sigillo"]["missing"] == [fuori]
        assert sigillo["mancanti_descritti"][0]["paziente"] == "ROSSI PROVA"


def test_dopo_un_riavvio_i_campioni_gia_dentro_non_sono_intrusi():
    """Il contenuto e' nell'archivio, la sessione no: va ricostruita."""
    cartella = _tmp("riavvio")
    tag = [_tag(1)]
    with _Postazione(cartella, tag) as posto:
        _scrivi_campione(posto, CF_UNO, "Della Valle", tag[0])
        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        posto.backend.tags.append(tag[0])
        posto.post("/api/sorveglia_scatola")

        # Il riavvio: la sessione in memoria sparisce, la scatola no.
        posto.server.workflow.riempimento = None

        stato, giro = posto.post("/api/sorveglia_scatola")
        assert stato == 200, giro
        assert giro["anomalie"] == [], "sono i nostri, non intrusi"
        assert giro["quanti"] == 1


def test_una_scatola_aperta_impedisce_di_aprirne_un_altra():
    with _Postazione(_tmp("due_aperte")) as posto:
        assert posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})[0] == 200
        stato, errore = posto.post(
            "/api/avvia_riempimento", {"destinazione": "Ospedale Terni"}
        )
        assert stato == 400
        assert "ancora aperta" in errore["errore"]


def test_il_riempimento_pretende_un_destinatario_configurato():
    with _Postazione(_tmp("destinatario")) as posto:
        stato, errore = posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Chissa"})
        assert stato == 400
        assert "sconosciuto" in errore["errore"]

        stato, errore = posto.post("/api/avvia_riempimento", {"destinazione": ""})
        assert stato == 400
        assert "destinatario" in errore["errore"]


def test_sorvegliare_senza_scatola_aperta_lo_dice():
    with _Postazione(_tmp("senza")) as posto:
        stato, errore = posto.post("/api/sorveglia_scatola")
        assert stato == 400
        assert "aprire prima" in errore["errore"]


# ---------------------------------------------------------------------------
# La distinta stampata e il suo QR
# ---------------------------------------------------------------------------
def _scatola_sigillata(posto: _Postazione, tag: list) -> dict:
    """Tre campioni scritti, messi in scatola, sigillati. Restituisce il foglio."""
    posto.trascrivi(
        CF_UNO, "Della Valle", data_nascita="1957-03-12", ora_prelievo="14:30", sesso="M"
    )
    posto.scrivi_su(tag[0])
    posto.post("/api/nuova_accettazione")
    posto.trascrivi(
        CF_DUE, "Rossi", data_nascita="12/07/1980", ora_prelievo="0915", sesso="M"
    )
    posto.scrivi_su(tag[1])
    posto.post("/api/nuova_accettazione")

    posto.backend.tags.clear()
    posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
    posto.backend.tags[:] = list(tag)
    posto.post("/api/sorveglia_scatola")
    posto.post("/api/chiudi_riempimento")
    stato, sigillo = posto.post("/api/sigilla")
    assert stato == 200 and sigillo["sigillo"]["ok"], sigillo
    stato, foglio = posto.post("/api/distinta_stampabile")
    assert stato == 200, foglio
    return foglio


def test_il_foglio_porta_tutto_quello_che_serve_a_destinazione():
    """I due archivi non comunicano: cio' che non e' sul foglio, li' non esiste."""
    tag = [_tag(1), _tag(2)]
    with _Postazione(_tmp("foglio"), tag) as posto:
        foglio = _scatola_sigillata(posto, tag)

        assert foglio["totale"] == 2
        assert foglio["mittente"]["insegna"]
        assert foglio["destinatario"]["nome"] == "Ospedale Foligno"
        assert foglio["operatore"] == "TEST"
        assert foglio["sigillo"]["ok"] is True
        assert foglio["sigillo"]["prova_chiusura"] == "parola dell'operatore"

        per_cf = {riga["codice_fiscale"]: riga for riga in foglio["righe"]}
        uno = per_cf[CF_UNO]
        # Identificativo, nome, sesso, nascita, data e ora del prelievo, campione.
        assert uno["paziente"] == "DELLA VALLE PROVA"
        assert uno["sesso"] == "M"
        assert uno["data_nascita"] == "12/03/1957"
        assert uno["ora_prelievo"] == "14:30"
        assert uno["descrizione"] == "pezzo operatorio"
        assert uno["etichetta"] == "1/1"
        assert len(uno["epc"]) == 24
        # Le date si stampano all'italiana, non in ISO.
        assert "/" in uno["data_prelievo"]
        assert per_cf[CF_DUE]["ora_prelievo"] == "09:15"


def test_il_qr_del_foglio_si_rilegge_e_ricostruisce_l_elenco():
    """La strada che non passa da nessun file: si legge il codice e basta."""
    tag = [_tag(1), _tag(2)]
    with _Postazione(_tmp("qr"), tag) as posto:
        foglio = _scatola_sigillata(posto, tag)
        assert len(foglio["codici"]) == 1, "due campioni stanno in un codice solo"
        codice = foglio["codici"][0]
        assert codice["svg"].startswith("<svg")
        assert "mm" in codice["svg"], "il QR si stampa, quindi si misura in millimetri"

        stato, letta = posto.post(
            "/api/leggi_qr_distinta", {"scansioni": [codice["testo"]]}
        )
        assert stato == 200, letta
        assert letta["attesi"] == 2
        assert letta["firma_verificata"] is True
        riletti = {riga["codice_fiscale"]: riga for riga in letta["righe"]}
        assert riletti[CF_UNO]["paziente"] == "DELLA VALLE PROVA"
        assert riletti[CF_UNO]["ora_prelievo"] == "14:30"
        assert riletti[CF_UNO]["data_nascita"] == "12/03/1957"
        # E gli EPC, che sono cio' che poi si confronta con la scatola.
        assert sorted(r["epc"] for r in letta["righe"]) == sorted(
            r["epc"] for r in foglio["righe"]
        )


def test_un_qr_di_un_altro_sistema_viene_rifiutato():
    with _Postazione(_tmp("qr_estraneo")) as posto:
        stato, errore = posto.post(
            "/api/leggi_qr_distinta", {"scansioni": ["https://esempio.it/qualcosa"]}
        )
        assert stato == 400
        assert "non e' una distinta di questo sistema" in errore["errore"]


def test_un_qr_manomesso_non_passa():
    """La firma serve a questo: un codice rifatto da qualcun altro non entra."""
    tag = [_tag(1), _tag(2)]
    with _Postazione(_tmp("qr_falso"), tag) as posto:
        foglio = _scatola_sigillata(posto, tag)
        testo = foglio["codici"][0]["testo"]
        # Si cambia un carattere del corpo, come farebbe una copia mal fatta.
        guasto = testo[:30] + ("A" if testo[30] != "A" else "B") + testo[31:]
        stato, errore = posto.post("/api/leggi_qr_distinta", {"scansioni": [guasto]})
        assert stato == 400
        assert errore["errore"], "un codice manomesso deve essere respinto"


def test_il_foglio_si_stampa_solo_di_una_spedizione_esistente():
    with _Postazione(_tmp("senza_spedizione")) as posto:
        stato, errore = posto.post("/api/distinta_stampabile")
        assert stato == 400
        assert "nessuna spedizione" in errore["errore"]


def test_l_ora_del_prelievo_si_accetta_in_tutte_le_grafie():
    with _Postazione(_tmp("ore")) as posto:
        for scritta, attesa in (("14:30", "14:30"), ("1430", "14:30"), ("930", "09:30")):
            stato, _ = posto.trascrivi(CF_UNO, "Della Valle", ora_prelievo=scritta)
            assert stato == 200, scritta
            posto.post("/api/annulla_accettazione", {"motivo": "prova"})

        stato, errore = posto.trascrivi(CF_UNO, "Della Valle", ora_prelievo="99:99")
        assert stato == 400
        assert "ora non riconosciuta" in errore["errore"]


def test_una_data_di_nascita_sbagliata_lo_dice_subito():
    with _Postazione(_tmp("nascita")) as posto:
        stato, errore = posto.trascrivi(CF_UNO, "Della Valle", data_nascita="32 marzo")
        assert stato == 400
        assert "data non riconosciuta" in errore["errore"]

        # Vuoto invece va bene: sull'etichetta del reparto a volte non c'e'.
        stato, _ = posto.trascrivi(CF_UNO, "Della Valle", data_nascita="")
        assert stato == 200


def test_il_campione_prelevato_ieri_conserva_la_sua_data():
    """La data viene dall'etichetta, non dall'orologio della postazione."""
    tag = _tag(1)
    with _Postazione(_tmp("ieri"), [tag]) as posto:
        posto.trascrivi(CF_UNO, "Della Valle", data_prelievo="2026-08-25", ora_prelievo="17:45")
        posto.scrivi_su(tag)
        posto.post("/api/nuova_accettazione")
        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        # Lo stesso oggetto: dopo la scrittura porta il suo EPC nuovo, e un tag
        # ricreato da zero sarebbe un contenitore diverso, mai preparato.
        posto.backend.tags[:] = [tag]
        posto.post("/api/sorveglia_scatola")
        posto.post("/api/chiudi_riempimento")
        posto.post("/api/sigilla")
        _, foglio = posto.post("/api/distinta_stampabile")
        assert foglio["righe"][0]["data_prelievo"] == "25/08/2026"
        assert foglio["righe"][0]["ora_prelievo"] == "17:45"


# ---------------------------------------------------------------------------
# Piu' reperti nella stessa accettazione
# ---------------------------------------------------------------------------
#: Quattro campioni presi allo stesso paziente nella stessa seduta: stesso nome
#: sopra, ma descrizione, materiale, fissativo, sede e avvertenze diversi. E' il
#: caso normale, non l'eccezione.
QUATTRO_REPERTI = [
    {
        "descrizione": "pezzo operatorio, colon",
        "material_code": 1,
        "fixative_code": 1,
        "site_code": 3,
        "avvertenze": ["INFECTIOUS"],
        "contenitori": 2,
    },
    {
        "descrizione": "linfonodo sentinella",
        "material_code": 2,
        "fixative_code": 2,
        "site_code": 7,
        "contenitori": 1,
    },
    {
        "descrizione": "margine di resezione, a fresco",
        "material_code": 3,
        "fixative_code": 0,
        "site_code": 3,
        "avvertenze": ["URGENT"],
        "contenitori": 1,
    },
]


def test_quattro_campioni_dello_stesso_paziente_possono_essere_diversi():
    """Stesso paziente, quattro reperti: ognuno col suo campione nel chip."""
    with _Postazione(_tmp("reperti")) as posto:
        stato, dati = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "sesso": "M",
                "reparto": "Chirurgia generale",
                "reperti": QUATTRO_REPERTI,
            },
        )
        assert stato == 200, dati
        assert dati["totale"] == 4
        assert dati["reperti"] == 3, "tre reperti distinti, quattro contenitori"

        descrizioni = [voce["descrizione"] for voce in dati["contenitori"]]
        assert descrizioni == [
            "pezzo operatorio, colon",
            "pezzo operatorio, colon",
            "linfonodo sentinella",
            "margine di resezione, a fresco",
        ], descrizioni


def test_la_numerazione_e_dell_accettazione_non_del_reperto():
    """Due vasetti di colon e uno di linfonodo fanno 1/3, 2/3, 3/3.

    Non 1/2, 2/2 e 1/1: sarebbero tre etichette in cui il paziente si perde, e
    l'EPC porta gia' l'indice riferito all'accettazione.
    """
    with _Postazione(_tmp("numerazione")) as posto:
        stato, dati = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": [
                    {"descrizione": "colon", "material_code": 1, "contenitori": 2},
                    {"descrizione": "linfonodo", "material_code": 2, "contenitori": 1},
                ],
            },
        )
        assert stato == 200
        assert [voce["etichetta"] for voce in dati["contenitori"]] == ["1/3", "2/3", "3/3"]


def test_ogni_tag_porta_i_codici_del_proprio_reperto():
    """Scriverli tutti uguali sarebbe un errore che a destinazione non si corregge."""
    tag = [_tag(indice) for indice in range(1, 5)]
    with _Postazione(_tmp("codici"), tag) as posto:
        posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": QUATTRO_REPERTI,
            },
        )
        posto.post("/api/conferma_conteggio")

        # Il campione che la postazione mostra prima di ogni scrittura deve
        # cambiare insieme al contenitore.
        visti = []
        for indice in range(4):
            _, stato_prima = posto.post("/api/stato_accettazione")
            prossimo = stato_prima["prossimo"]
            visti.append((prossimo["descrizione"], prossimo["campione"]))
            esito, dati = posto.scrivi_su(tag[indice])
            assert esito == 200 and dati["scrittura"]["ok"], dati

        assert [d for d, _ in visti] == [
            "pezzo operatorio, colon",
            "pezzo operatorio, colon",
            "linfonodo sentinella",
            "margine di resezione, a fresco",
        ]
        # E i codici scritti nel chip sono diversi fra reperti diversi.
        materiali = [campione["materiale"] for _, campione in visti]
        assert materiali[0] == materiali[1]
        assert len(set(materiali)) == 3, materiali
        # Le avvertenze seguono il reperto, non l'accettazione.
        assert visti[0][1]["avvertenze"] != visti[2][1]["avvertenze"]


def test_i_reperti_diversi_arrivano_distinti_sulla_distinta():
    """Il foglio e' l'unica cosa che il destinatario riceve: deve dirlo."""
    tag = [_tag(indice) for indice in range(1, 5)]
    with _Postazione(_tmp("distinta_reperti"), tag) as posto:
        posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "ora_prelievo": "10:15",
                "reperti": QUATTRO_REPERTI,
            },
        )
        posto.post("/api/conferma_conteggio")
        for indice in range(4):
            posto.scrivi_su(tag[indice])
        posto.post("/api/nuova_accettazione")

        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        posto.backend.tags[:] = list(tag)
        posto.post("/api/sorveglia_scatola")
        posto.post("/api/chiudi_riempimento")
        posto.post("/api/sigilla")

        stato, foglio = posto.post("/api/distinta_stampabile")
        assert stato == 200, foglio
        assert foglio["totale"] == 4
        descrizioni = sorted(riga["descrizione"] for riga in foglio["righe"])
        assert descrizioni == sorted(
            [
                "pezzo operatorio, colon",
                "pezzo operatorio, colon",
                "linfonodo sentinella",
                "margine di resezione, a fresco",
            ]
        ), descrizioni
        # Tutte righe dello stesso paziente, ma con campioni diversi.
        assert {riga["paziente"] for riga in foglio["righe"]} == {"DELLA VALLE GIANFRANCO"}
        assert len({riga["materiale"] for riga in foglio["righe"]}) == 3

        # E il QR li riporta tutti e quattro, distinti.
        stato, letta = posto.post(
            "/api/leggi_qr_distinta",
            {"scansioni": [c["testo"] for c in foglio["codici"]]},
        )
        assert stato == 200
        assert sorted(riga["descrizione"] for riga in letta["righe"]) == descrizioni


def test_senza_conteggio_ogni_campione_e_un_contenitore():
    """E' la forma che manda l'interfaccia: nessun numero da digitare.

    Quanti campioni abbia un paziente non si sa prima di averli in mano, e un
    numero da solo non direbbe comunque *quali* sono. Se ne serve un altro lo
    si aggiunge, con la sua descrizione e i suoi codici.
    """
    with _Postazione(_tmp("senza_conteggio")) as posto:
        stato, dati = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": [
                    {"descrizione": "pezzo operatorio, colon", "material_code": 1},
                    {"descrizione": "linfonodo sentinella", "material_code": 2},
                    {"descrizione": "margine di resezione", "material_code": 3},
                ],
            },
        )
        assert stato == 200, dati
        assert dati["totale"] == 3
        assert dati["reperti"] == 3
        assert [voce["etichetta"] for voce in dati["contenitori"]] == ["1/3", "2/3", "3/3"]
        assert [voce["descrizione"] for voce in dati["contenitori"]] == [
            "pezzo operatorio, colon",
            "linfonodo sentinella",
            "margine di resezione",
        ]


def test_un_campione_solo_resta_il_caso_semplice():
    """Un contenitore, nessuna domanda: la conferma del conteggio non serve."""
    with _Postazione(_tmp("uno_solo"), [_tag(1)]) as posto:
        stato, dati = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": [{"descrizione": "biopsia gastrica", "material_code": 1}],
            },
        )
        assert stato == 200
        assert dati["totale"] == 1
        assert dati["conteggio_confermato"] is True
        assert dati["contenitori"][0]["etichetta"] == "1/1"
        stato, esito = posto.scrivi_su(_tag(1))
        assert stato == 200 and esito["scrittura"]["ok"], esito


def test_la_forma_piatta_continua_a_funzionare():
    """N vasetti dello stesso reperto resta il caso piu' frequente."""
    with _Postazione(_tmp("piatta")) as posto:
        stato, dati = posto.trascrivi(CF_UNO, "Della Valle", contenitori=3)
        assert stato == 200
        assert dati["totale"] == 3
        assert dati["reperti"] == 1
        assert [voce["etichetta"] for voce in dati["contenitori"]] == ["1/3", "2/3", "3/3"]
        assert {voce["descrizione"] for voce in dati["contenitori"]} == {"pezzo operatorio"}


def test_con_piu_reperti_il_conteggio_non_si_corregge_alla_cieca():
    """«Quanti sono in tutto» non dice piu' quale reperto cambiare."""
    with _Postazione(_tmp("conteggio")) as posto:
        posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": [
                    {"descrizione": "colon", "material_code": 1, "contenitori": 1},
                    {"descrizione": "linfonodo", "material_code": 2, "contenitori": 1},
                ],
            },
        )
        stato, errore = posto.post("/api/correggi_conteggio", {"totale": 3})
        assert stato == 400
        assert "piu' reperti" in errore["errore"]

    # Con un reperto solo continua a funzionare come prima.
    with _Postazione(_tmp("conteggio_uno")) as posto:
        posto.trascrivi(CF_UNO, "Della Valle", contenitori=2)
        stato, dati = posto.post("/api/correggi_conteggio", {"totale": 4})
        assert stato == 200, dati
        assert dati["totale"] == 4


def test_dopo_un_riavvio_ogni_contenitore_conserva_il_suo_reperto():
    """Il payload si ricostruisce da ogni contenitore, non dal primo."""
    cartella = _tmp("riavvio_reperti")
    with _Postazione(cartella) as posto:
        posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": QUATTRO_REPERTI,
            },
        )
        prima = [
            (voce["etichetta"], voce["descrizione"])
            for voce in posto.post("/api/stato_accettazione")[1]["contenitori"]
        ]

    # Riavvio: stessa cartella, workflow nuovo.
    with _Postazione(cartella) as posto:
        stato, dopo = posto.post("/api/stato_accettazione")
        assert stato == 200
        assert [(v["etichetta"], v["descrizione"]) for v in dopo["contenitori"]] == prima
        assert dopo["reperti"] == 3
        # E il campione che verrebbe scritto adesso e' quello giusto.
        assert dopo["prossimo"]["descrizione"] == "pezzo operatorio, colon"


def test_un_reperto_senza_descrizione_non_blocca_niente():
    """Sull'etichetta del reparto a volte non c'e': un campo vuoto e' un dato."""
    with _Postazione(_tmp("vuoto_reperto")) as posto:
        stato, dati = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": [{"contenitori": 1}, {"descrizione": "colon", "contenitori": 1}],
            },
        )
        assert stato == 200
        assert dati["totale"] == 2


def test_un_avvertenza_inventata_viene_rifiutata():
    with _Postazione(_tmp("avvertenza")) as posto:
        stato, errore = posto.post(
            "/api/registra",
            {
                "codice_fiscale": CF_UNO,
                "cognome": "Della Valle",
                "nome": "Gianfranco",
                "reperti": [{"descrizione": "colon", "avvertenze": ["ESPLOSIVO"]}],
            },
        )
        assert stato == 400
        assert "avvertenza sconosciuta" in errore["errore"]


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
