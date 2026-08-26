"""Gli strumenti di misura, provati dove servono.

Tre pannelli su cinque non rispondevano alla domanda per cui esistono. Le
promesse che questi test tengono ferme:

* l'adattamento si misura **anche fuori dalla banda**, e la risposta dice *dove*
  l'antenna e' accordata: e' quello che distingue un'antenna cattiva da una
  buona ma accordata altrove;
* se il firmware rifiuta la banda, la regione si puo' commutare — e viene
  **rimessa a posto anche se la misura fallisce a meta'**;
* un minimo sul bordo della spazzata **non e' una risonanza**, e non va
  spacciato per tale;
* la modalita' RF sostituita in silenzio dal modulo si vede, perche' si rilegge;
* un tag senza USER memory non blocca niente: si scrive il solo EPC e il
  campione arriva a destinazione sulla distinta;
* passare alla modalita' ridotta non e' mai automatico.
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

from app.config_misura import Misura, aggiorna_testo
from lims.tagio import MODALITA_PAYLOAD, MODALITA_SOLO_EPC
from webui.server import WebUIServer

CF_UNO = "MRTMTT25D09F205Z"
CF_DUE = "RSSMRA80A01H501U"


# ---------------------------------------------------------------------------
# Attrezzatura
# ---------------------------------------------------------------------------
def _tmp(nome: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"strumenti_{nome}_"))


def _config(tmp: Path, **lims) -> dict:
    base = {
        "lab_id": 1,
        "database": str(tmp / "lims.db"),
        "keyring": str(tmp / "keys.json"),
        "user_memory_bytes": 64,
        "write_antennas": [3],
        "read_antennas": [1, 2],
        "seal_min_antennas": 1,
        "seal_powers_cdbm": [2000, 2900],
    }
    base.update(lims)
    return {
        "reader": {"region": 0x08},
        "antennas": [
            {"id": 1, "read_power": 2900, "write_power": 2000},
            {"id": 2, "read_power": 2900, "write_power": 2000},
            {"id": 3, "read_power": 2000, "write_power": 2000},
        ],
        "tag_access": {"access_password_hex": "00000000", "timeout_ms": 1000},
        "lims": base,
        "destinatari": [{"nome": "Ospedale Foligno", "codice": "OF", "attivo": True}],
        "webui": {"host": "127.0.0.1", "port": 0},
        "diario": {"attivo": False},
    }


def _tag(indice: int, *, user_bytes: int = 64) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=bytes.fromhex("E2801190200050A1B2C300") + bytes([indice]),
        user_bytes=user_bytes,
    )


class _Postazione:
    def __init__(self, tmp: Path, tag: list[SimulatedTag] | None = None, **lims):
        self.backend = FakeTagBackend(tag if tag is not None else [], antennas=(1, 2, 3))
        self.server = WebUIServer(
            _config(tmp, **lims), self.backend, host="127.0.0.1", port=0
        )
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
            with urllib.request.urlopen(richiesta, timeout=20) as risposta:
                grezzo = risposta.read()
                return risposta.status, (json.loads(grezzo) if grezzo else None)
        except urllib.error.HTTPError as errore:
            grezzo = errore.read()
            return errore.code, (json.loads(grezzo) if grezzo else None)

    def scrivi_su(self, tag: SimulatedTag):
        tutti = list(self.backend.tags)
        self.backend.tags[:] = [tag]
        try:
            return self.post("/api/scrivi")
        finally:
            self.backend.tags[:] = tutti

    def trascrivi(self, cf: str, cognome: str):
        return self.post(
            "/api/registra",
            {
                "codice_fiscale": cf,
                "cognome": cognome,
                "nome": "Prova",
                "sesso": "M",
                "descrizione": "pezzo operatorio",
            },
        )


# ---------------------------------------------------------------------------
# Che cosa e' collegato davvero
# ---------------------------------------------------------------------------
def test_il_censimento_si_fa_da_solo_al_collegamento():
    """Non e' un pannello da ricordarsi di aprire: e' la prima cosa che si sa."""
    with _Postazione(_tmp("censimento")) as posto:
        assert posto.server.workflow.hardware["rilevato"] is False
        stato, esito = posto.post("/api/connetti")
        assert stato == 200, esito
        hardware = esito["hardware"]
        assert hardware["rilevato"] is True
        assert hardware["antenne_collegate"] == [1, 2, 3]
        assert hardware["seriale"]
        assert hardware["banda_configurata"] == 0x08
        # E resta memorizzato: gli altri pannelli non lo richiedono di nuovo.
        _, descrizione = posto.post("/api/descrivi")
        assert descrizione["hardware"]["rilevato"] is True


def test_le_bande_accettate_hanno_un_nome_e_i_loro_MHz():
    """«0x08» non dice niente a nessuno; «CE_LOW 865-867 MHz» sì."""
    with _Postazione(_tmp("bande")) as posto:
        _, esito = posto.post("/api/connetti")
        nomi = esito["hardware"]["bande_nomi"]
        assert any("CE_LOW" in n and "865-867" in n for n in nomi), nomi
        assert any("North America" in n for n in nomi), nomi
        assert esito["hardware"]["banda_configurata_nome"].startswith("CE_LOW")


def test_un_modulo_monoregione_dichiara_di_non_poter_spazzare():
    """EX10 2024-12 §2.2 e §10.1: non e' un limite del programma, e' il firmware."""
    with _Postazione(_tmp("monoregione")) as posto:
        posto.backend.regioni_simulate = {0x08}
        _, esito = posto.post("/api/connetti")
        hardware = esito["hardware"]
        assert hardware["multibanda"] is False
        assert hardware["spazzata_larga"] is False
        assert hardware["frequenze_singole"] is False
        assert "0x010B" in hardware["motivo_limite"]
        assert "analizzatore" in hardware["motivo_limite"]


def test_su_un_modulo_monoregione_la_spazzata_larga_si_rifiuta_spiegando():
    """Provarci darebbe un errore criptico e farebbe cercare un guasto."""
    with _Postazione(_tmp("rifiuto_larga")) as posto:
        posto.backend.regioni_simulate = {0x08}
        posto.post("/api/connetti")
        stato, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "da_khz": 850_000, "a_khz": 960_000, "passo_khz": 2000},
        )
        assert stato == 200, esito
        assert esito["non_supportata"] is True
        assert "analizzatore" in esito["messaggio"]
        # La banda configurata invece si misura eccome.
        _, in_banda = posto.post("/api/diagnostica_antenna", {"antenna": 1})
        assert in_banda["data"]["measurements"]


def test_su_un_modulo_multibanda_la_spazzata_larga_si_fa():
    with _Postazione(_tmp("multibanda")) as posto:
        posto.post("/api/connetti")
        stato, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "da_khz": 850_000, "a_khz": 960_000, "passo_khz": 2000},
        )
        assert stato == 200, esito
        assert "non_supportata" not in esito
        assert esito["data"]["measurements"]


def test_un_censimento_fallito_non_fa_fallire_il_collegamento():
    """Si lavora anche senza sapere la certificazione — senza fingere di saperla."""
    with _Postazione(_tmp("censimento_ko")) as posto:
        def rompi():
            raise RuntimeError("il modulo non risponde a 0x71")

        posto.backend.identify = rompi
        stato, esito = posto.post("/api/connetti")
        assert stato == 200, esito
        assert esito["hardware"]["rilevato"] is False
        assert "0x71" in esito["hardware"]["motivo"]


def test_senza_censimento_non_si_vieta_niente():
    """Nel dubbio si lascia provare: e' il modulo a dire di no, non noi."""
    with _Postazione(_tmp("nel_dubbio")) as posto:
        assert posto.server.workflow.hardware["rilevato"] is False
        stato, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "da_khz": 850_000, "a_khz": 960_000, "passo_khz": 2000},
        )
        assert stato == 200, esito
        assert "non_supportata" not in esito


def test_la_curva_simulata_riproduce_i_punti_del_datasheet():
    """Il banco deve mentire il meno possibile.

    La prima versione era una parabola con un minimo aguzzo a 915 MHz che
    saliva a 4,5 ai bordi. Il datasheet SLP1027 dice che l'antenna e' piatta
    fra 1,16 e 1,24 su 902-928: simulare un picco insegnava a cercare una forma
    che questa antenna non ha.
    """
    from rfid_silion.simulazione import _vswr_modello

    # I punti pubblicati, misurati in camera anecoica.
    for frequenza_khz, atteso in ((902_000, 1.24), (915_000, 1.16), (922_000, 1.20)):
        misurato = _vswr_modello(frequenza_khz, 915_000)
        assert abs(misurato - atteso) < 0.03, (frequenza_khz, misurato, atteso)
    # Su tutta la banda specificata resta ben adattata, come dice il datasheet.
    for frequenza_khz in range(902_000, 929_000, 1_000):
        assert _vswr_modello(frequenza_khz, 915_000) < 1.3


def test_sotto_i_900_MHz_la_curva_simulata_e_estrapolazione():
    """Il costruttore non pubblica niente li' sotto: e' il motivo della misura.

    Il modello prevede circa 1,8 a 866 MHz, ma resta una previsione da un polo
    singolo. Il test fissa che la simulazione sia *plausibile* — degradata ma
    non assurda — non che quel numero sia vero.
    """
    from rfid_silion.simulazione import _vswr_modello

    in_banda = _vswr_modello(866_000, 915_000)
    assert 1.4 < in_banda < 3.0, in_banda
    # Peggiora allontanandosi, come deve fare un adattamento.
    assert _vswr_modello(860_000, 915_000) > in_banda


def test_la_formula_del_vswr_e_quella_del_manuale():
    """L'esempio ufficiale: VL=0x78 -> VSWR 1.67. Se cambia, e' un errore nostro."""
    from rfid_silion import protocol as P

    assert round(P.vswr_from_return_loss(0x78), 2) == 1.67
    assert P.VSWR_ALERT_THRESHOLD == 7.0  # «It should usually be less than 7»


def test_i_nomi_delle_bande_vengono_dall_appendice_del_manuale():
    from rfid_silion import protocol as P

    assert P.nome_regione(0x01).startswith("North America 902-928")
    assert P.nome_regione(0x08).startswith("CE_LOW (Europa) 865-867")
    assert P.nome_regione(0xFF).startswith("Banda intera 860-960")
    # Un codice che il manuale non elenca non diventa un nome inventato.
    assert "sconosciuta" in P.nome_regione(0x99)


# ---------------------------------------------------------------------------
# Adattamento delle antenne
# ---------------------------------------------------------------------------
def test_la_banda_configurata_si_misura_come_sempre():
    with _Postazione(_tmp("banda")) as posto:
        stato, esito = posto.post("/api/diagnostica_antenna", {"antenna": 1})
        assert stato == 200, esito
        dati = esito["data"]
        assert dati["measurements"], "il modulo deve restituire misure"
        assert all(
            865_000 <= m["frequency_khz"] <= 868_000 for m in dati["measurements"]
        )
        assert dati["in_banda_eu"]["punti"] == len(dati["measurements"])


def test_una_spazzata_larga_trova_dove_l_antenna_e_accordata():
    """La domanda vera: cattiva, o buona ma accordata altrove?"""
    with _Postazione(_tmp("larga")) as posto:
        stato, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "da_khz": 860_000, "a_khz": 960_000, "passo_khz": 2000},
        )
        assert stato == 200, esito
        risonanza = esito["data"]["risonanza"]
        # Le SLP1027 simulate risuonano a 915 MHz, come quelle vere dovrebbero.
        assert 905_000 <= risonanza["frequency_khz"] <= 925_000, risonanza
        assert risonanza["al_bordo"] is False
        assert risonanza["scarto_da_eu_khz"] > 30_000, "sono ben sopra la banda EU"
        # E in banda EU il verdetto resta quello che conta per l'esercizio.
        assert esito["data"]["in_banda_eu"]["vswr_peggiore"] > risonanza["vswr"]


def test_la_banda_utile_e_il_numero_confrontabile_col_datasheet():
    """Il datasheet dichiara «VSWR <= 1,3 su 902-928 MHz»: si risponde in kind.

    Il minimo, su un pannello a banda larga, e' piatto e la sua posizione la
    sposta il rumore; l'estensione della banda utile no.
    """
    with _Postazione(_tmp("larghezza")) as posto:
        _, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "da_khz": 840_000, "a_khz": 990_000, "passo_khz": 2000},
        )
        larghezza = esito["data"]["larghezza_banda"]
        assert larghezza["trovata"] is True
        assert larghezza["da_khz"] < larghezza["a_khz"]
        assert larghezza["larghezza_khz"] > 0
        # Con la curva del datasheet la banda utile arriva sotto gli 866 MHz.
        assert larghezza["copre_eu"] is True, larghezza
        assert larghezza["al_bordo"] is False, "la spazzata era abbastanza larga"


def test_una_banda_utile_che_esce_dalla_spazzata_lo_dice():
    """Dire «larga 3 MHz» quando continua oltre sarebbe una misura falsa."""
    with _Postazione(_tmp("larghezza_bordo")) as posto:
        _, esito = posto.post("/api/diagnostica_antenna", {"antenna": 1})
        larghezza = esito["data"]["larghezza_banda"]
        assert larghezza["trovata"] is True
        assert larghezza["al_bordo"] is True


def test_un_minimo_sul_bordo_non_e_una_risonanza():
    """Dirlo lo stesso porterebbe al fornitore un numero che non esiste."""
    with _Postazione(_tmp("bordo")) as posto:
        # La sola banda EU: il minimo cade all'estremo, perche' la risonanza
        # vera sta 50 MHz piu' in la'.
        _, esito = posto.post("/api/diagnostica_antenna", {"antenna": 1})
        assert esito["data"]["risonanza"]["al_bordo"] is True


def test_la_condizione_della_misura_viaggia_con_la_misura():
    """Tre curve diverse possono essere tre antenne o tre situazioni.

    Il VSWR e' l'impedenza vista al connettore, e dipende da tutto quello che
    sta nel campo vicino — a 866 MHz una manciata di centimetri. Senza sapere
    cosa c'era sull'antenna, la curva di un mese fa non si sa piu' leggere.
    """
    with _Postazione(_tmp("condizione")) as posto:
        stato, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "nota": "a vuoto, in posizione di lavoro"},
        )
        assert stato == 200, esito
        assert esito["nota"] == "a vuoto, in posizione di lavoro"

        # Anche quando il modulo ha dovuto cambiare regione per misurare.
        posto.backend.rifiuta_bande_diverse = True
        _, commutata = posto.post(
            "/api/diagnostica_antenna",
            {
                "antenna": 1,
                "banda": 0xFF,
                "consenti_cambio_regione": True,
                "nota": "scatola piena sopra",
            },
        )
        assert commutata["nota"] == "scatola piena sopra"
        assert commutata["regione_commutata"] is True


def test_una_nota_lunghissima_viene_accorciata():
    """Va nel diario e in una legenda: un tema non ci sta e non serve."""
    with _Postazione(_tmp("nota_lunga")) as posto:
        _, esito = posto.post(
            "/api/diagnostica_antenna", {"antenna": 1, "nota": "x" * 500}
        )
        assert len(esito["nota"]) == 200


def test_senza_nota_la_misura_si_fa_lo_stesso():
    with _Postazione(_tmp("senza_nota")) as posto:
        stato, esito = posto.post("/api/diagnostica_antenna", {"antenna": 1})
        assert stato == 200
        assert esito["nota"] == ""
        assert esito["data"]["measurements"]


def test_le_antenne_si_confrontano_fra_loro():
    """Due antenne uguali con curve diverse indicano un cavo, non un progetto."""
    with _Postazione(_tmp("confronto")) as posto:
        peggiori = []
        for antenna in (1, 2, 3):
            _, esito = posto.post("/api/diagnostica_antenna", {"antenna": antenna})
            peggiori.append(esito["data"]["worst_vswr"])
        assert len(set(peggiori)) == 3, "le tre antenne devono essere distinguibili"


def test_una_banda_rifiutata_non_e_un_errore_ma_una_domanda():
    with _Postazione(_tmp("rifiuto")) as posto:
        posto.backend.rifiuta_bande_diverse = True
        stato, esito = posto.post("/api/diagnostica_antenna", {"antenna": 1, "banda": 0xFF})
        assert stato == 200, esito
        assert esito["rifiutata"] is True
        assert esito["regione_attuale"] == 0x08
        assert esito["banda_chiesta"] == 0xFF
        assert "0x010B" in esito["messaggio"]


def test_col_consenso_la_regione_si_commuta_e_torna_com_era():
    with _Postazione(_tmp("commuta")) as posto:
        posto.backend.rifiuta_bande_diverse = True
        stato, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "banda": 0xFF, "consenti_cambio_regione": True},
        )
        assert stato == 200, esito
        assert esito["regione_commutata"] is True
        assert esito["data"]["measurements"]
        # E soprattutto: il modulo e' tornato sulla sua regione.
        assert posto.backend.region == 0x08


def test_la_regione_torna_com_era_anche_se_la_misura_fallisce():
    """Una misura interrotta non deve lasciare la postazione fuori banda."""
    with _Postazione(_tmp("fallita")) as posto:
        posto.backend.rifiuta_bande_diverse = True
        # L'antenna 4 non esiste: la misura fallisce dopo il cambio di regione.
        stato, errore = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 4, "banda": 0xFF, "consenti_cambio_regione": True},
        )
        assert stato == 400, errore
        assert posto.backend.region == 0x08, "la regione va ripristinata comunque"


def test_troppe_frequenze_allargano_il_passo_invece_di_troncare():
    """Il comando ne accetta 255: una curva che finisce a meta' non si confronta."""
    with _Postazione(_tmp("passo")) as posto:
        _, esito = posto.post(
            "/api/diagnostica_antenna",
            {"antenna": 1, "da_khz": 800_000, "a_khz": 1_000_000, "passo_khz": 100},
        )
        misure = esito["data"]["measurements"]
        assert len(misure) <= 255
        assert min(m["frequency_khz"] for m in misure) == 800_000
        assert max(m["frequency_khz"] for m in misure) >= 999_000, "l'intervallo resta intero"


# ---------------------------------------------------------------------------
# Parametri Gen2
# ---------------------------------------------------------------------------
def test_il_consigliato_applica_e_spiega():
    with _Postazione(_tmp("gen2")) as posto:
        stato, esito = posto.post("/api/gen2_consigliato")
        assert stato == 200, esito
        applicati = esito["data"]["applied"]
        assert applicati["session"] == 2
        assert applicati["target_dynamic"] is True
        assert applicati["q_dynamic"] is True
        assert applicati["rf_mode"] == 0x71
        # Ogni valore ha una ragione, e la ragione arriva all'interfaccia.
        assert set(esito["motivi"]) == {"session", "target", "q", "rf_mode"}
        assert esito["rf_sostituita"] is False


def test_la_modalita_rf_sostituita_in_silenzio_si_vede():
    """Il manuale lo annuncia e senza rilettura non se ne accorge nessuno."""
    with _Postazione(_tmp("rf")) as posto:
        posto.backend.rf_mode_supportate = {0x6B}
        stato, esito = posto.post("/api/gen2_consigliato")
        assert stato == 200
        assert esito["rf_sostituita"] is True
        assert "0x71" in esito["avviso"] and "0x6B" in esito["avviso"]
        assert esito["riletti"]["rf_mode"] == 0x6B


def test_anche_una_applicazione_a_mano_rilegge():
    with _Postazione(_tmp("rilettura")) as posto:
        stato, esito = posto.post("/api/gen2", {"session": 1})
        assert stato == 200, esito
        assert esito["riletti"]["session"] == 1


def test_la_prova_di_lettura_misura_quello_che_serve():
    tag = [_tag(1), _tag(2)]
    with _Postazione(_tmp("prova"), tag) as posto:
        stato, esito = posto.post("/api/prova_lettura", {"cicli": 4})
        assert stato == 200, esito
        assert esito["cicli"] == 4
        assert esito["tag"] == 2
        assert esito["letture"] > 0
        assert esito["letture_al_secondo"] > 0
        assert esito["rssi"]["minimo"] <= esito["rssi"]["massimo"]
        assert len(esito["dettaglio"]) == 2
        assert all(voce["tasso"] > 0 for voce in esito["dettaglio"])
        # Porta con se' la configurazione con cui e' stata fatta: una misura
        # senza le sue condizioni non si confronta con niente.
        assert "session" in esito["gen2"]


def test_un_tag_difficile_si_vede_dal_tasso():
    facile = _tag(1)
    difficile = SimulatedTag(
        bytes.fromhex("BBBBBBBBBBBBBBBBBBBB0002"), user_bytes=64, visible_every=3
    )
    with _Postazione(_tmp("difficile"), [facile, difficile]) as posto:
        _, esito = posto.post("/api/prova_lettura", {"cicli": 9})
        tassi = {voce["epc"][:4]: voce["tasso"] for voce in esito["dettaglio"]}
        assert tassi["AAAA"] > tassi.get("BBBB", 0), tassi


def test_i_cicli_fuori_intervallo_vengono_rifiutati():
    with _Postazione(_tmp("cicli")) as posto:
        for cicli in (0, 100):
            stato, errore = posto.post("/api/prova_lettura", {"cicli": cicli})
            assert stato == 400, cicli
            assert "fra 1 e 40" in errore["errore"]


# ---------------------------------------------------------------------------
# Profilazione e modalita' di scrittura
# ---------------------------------------------------------------------------
def test_un_tag_senza_user_memory_propone_la_modalita_ridotta():
    vuoto = _tag(1, user_bytes=0)
    with _Postazione(_tmp("proposta"), [vuoto]) as posto:
        stato, esito = posto.post("/api/profila_tag")
        assert stato == 200, esito
        assert esito["profilo"]["suitable"] is False
        proposta = esito["proposta"]
        assert proposta["modalita_scrittura"] == MODALITA_SOLO_EPC
        assert proposta["modalita_attuale"] == MODALITA_PAYLOAD
        assert proposta["cambia"] is True
        assert any("non ci sta" in motivo for motivo in proposta["motivi"])
        assert any("copiato" in motivo for motivo in proposta["motivi"])


def test_un_tag_capiente_non_propone_niente_di_strano():
    with _Postazione(_tmp("capiente"), [_tag(1, user_bytes=64)]) as posto:
        _, esito = posto.post("/api/profila_tag")
        assert esito["proposta"]["modalita_scrittura"] == MODALITA_PAYLOAD


def test_la_modalita_ridotta_non_si_attiva_da_sola():
    """Una difesa che decade in silenzio e' una difesa che nessuno ha deciso."""
    with _Postazione(_tmp("mai_sola"), [_tag(1, user_bytes=0)]) as posto:
        posto.post("/api/profila_tag")
        _, descrizione = posto.post("/api/descrivi")
        assert descrizione["modalita_scrittura"] == MODALITA_PAYLOAD


def test_in_solo_epc_la_user_memory_non_viene_toccata():
    tag = _tag(1, user_bytes=64)
    with _Postazione(_tmp("solo_epc"), [tag], modalita_scrittura=MODALITA_SOLO_EPC) as posto:
        posto.trascrivi(CF_UNO, "Della Valle")
        stato, esito = posto.scrivi_su(tag)
        assert stato == 200 and esito["scrittura"]["ok"], esito
        assert esito["scrittura"]["payload_bytes"] == 0
        assert esito["scrittura"]["blocks_written"] == 0
        assert esito["scrittura"]["epc"], "l'EPC si scrive comunque"
        assert esito["scrittura"]["tid"], "e il TID si legge comunque"
        # Il chip non e' stato toccato oltre l'EPC.
        assert tag.write_count == 0
        assert bytes(tag.user) == bytes(64)
        assert any("solo EPC" in passo for passo in esito["scrittura"]["steps"])


def test_in_solo_epc_il_giro_arriva_fino_alla_distinta():
    """La modalita' ha senso da quando il foglio porta i dati del paziente."""
    tag = [_tag(1, user_bytes=0), _tag(2, user_bytes=0)]
    with _Postazione(_tmp("giro"), tag, modalita_scrittura=MODALITA_SOLO_EPC) as posto:
        for indice, (cf, cognome) in enumerate(
            ((CF_UNO, "Della Valle"), (CF_DUE, "Rossi"))
        ):
            posto.trascrivi(cf, cognome)
            stato, esito = posto.scrivi_su(tag[indice])
            assert stato == 200 and esito["scrittura"]["ok"], esito
            posto.post("/api/nuova_accettazione")

        posto.backend.tags.clear()
        posto.post("/api/avvia_riempimento", {"destinazione": "Ospedale Foligno"})
        posto.backend.tags[:] = list(tag)
        stato, giro = posto.post("/api/sorveglia_scatola")
        assert giro["quanti"] == 2, giro
        posto.post("/api/chiudi_riempimento")
        stato, sigillo = posto.post("/api/sigilla")
        assert sigillo["sigillo"]["ok"] is True

        stato, foglio = posto.post("/api/distinta_stampabile")
        assert stato == 200, foglio
        # I dati del paziente ci sono: viaggiano sulla carta invece che nel chip.
        assert sorted(riga["paziente"] for riga in foglio["righe"]) == [
            "DELLA VALLE PROVA",
            "ROSSI PROVA",
        ]


def test_in_solo_epc_un_tag_letto_non_e_illeggibile():
    """Chiamarlo rotto manderebbe a cercare un guasto che non c'e'."""
    tag = _tag(1, user_bytes=0)
    with _Postazione(_tmp("lettura"), [tag], modalita_scrittura=MODALITA_SOLO_EPC) as posto:
        posto.trascrivi(CF_UNO, "Della Valle")
        posto.scrivi_su(tag)
        rilievo = posto.server.workflow._tagio(scrittura=False).survey_field()
        osservazioni = rilievo.to_dict()["osservazioni"]
        assert [o["stato"] for o in osservazioni] == ["solo_epc"], osservazioni
        assert "distinta" in osservazioni[0]["dettaglio"]


def test_una_modalita_sconosciuta_non_degrada_in_silenzio():
    with _Postazione(_tmp("ignota"), modalita_scrittura="fantasia") as posto:
        _, descrizione = posto.post("/api/descrivi")
        assert descrizione["modalita_scrittura"] == MODALITA_PAYLOAD


# ---------------------------------------------------------------------------
# Scrittura in configurazione
# ---------------------------------------------------------------------------
_CONFIG_MINIMA = """lims:
  lab_id: 1
  # quanto misurato sui tag
  user_memory_bytes: 86
  write_antennas:
  - 3
"""


def test_la_modalita_si_scrive_conservando_i_commenti():
    nuovo, esito = aggiorna_testo(_CONFIG_MINIMA, None, modalita_scrittura=MODALITA_SOLO_EPC)
    assert esito.scritto is True
    assert "modalita_scrittura: solo_epc" in nuovo
    assert "# quanto misurato sui tag" in nuovo, "i commenti restano"
    assert "user_memory_bytes: 86" in nuovo, "il resto non si tocca"
    # E si torna indietro senza duplicare il commento.
    ritorno, _ = aggiorna_testo(nuovo, None, modalita_scrittura=MODALITA_PAYLOAD)
    assert "modalita_scrittura: payload" in ritorno
    assert ritorno.count("Cosa si scrive sul tag") == 1


def test_una_misura_a_zero_non_abbassa_la_soglia_ma_scrive_la_modalita():
    """Zero byte non e' una misura della memoria: e' il caso della modalita'."""
    nuovo, esito = aggiorna_testo(
        _CONFIG_MINIMA,
        Misura(user_bytes=0, tid_serializzato=True),
        modalita_scrittura=MODALITA_SOLO_EPC,
    )
    assert esito.scritto is True
    assert "user_memory_bytes: 86" in nuovo, "la soglia resta com'era"
    assert "modalita_scrittura: solo_epc" in nuovo
    assert "nessuna USER memory" in esito.motivo


def test_senza_modalita_una_misura_a_zero_non_scrive_niente():
    nuovo, esito = aggiorna_testo(_CONFIG_MINIMA, Misura(user_bytes=0, tid_serializzato=True))
    assert esito.scritto is False
    assert nuovo == _CONFIG_MINIMA


# ---------------------------------------------------------------------------
# Salute e registro
# ---------------------------------------------------------------------------
def test_la_salute_dice_quali_antenne_sono_collegate():
    with _Postazione(_tmp("salute")) as posto:
        posto.post("/api/connetti")
        stato, esito = posto.post("/api/salute")
        assert stato == 200, esito
        dati = esito["data"]
        assert dati["antennas_connected"] == [1, 2, 3]
        assert "counters" in dati and "timeouts" in dati["counters"]
        assert dati["firmware_info"]


def test_una_antenna_scollegata_si_vede():
    with _Postazione(_tmp("scollegata")) as posto:
        posto.backend.antenne_scollegate = {2}
        posto.post("/api/connetti")
        _, esito = posto.post("/api/salute")
        assert esito["data"]["antennas_connected"] == [1, 3]


def test_il_registro_delle_misure_rilegge_il_diario():
    from rfid_silion.diario import BackendTracciato, Diario

    cartella = _tmp("registro")
    diario = Diario(cartella / "diario")
    try:
        backend = BackendTracciato(FakeTagBackend([], antennas=(1, 2, 3)), diario)
        config = _config(cartella)
        config["diario"] = {"attivo": True, "dir": str(cartella / "diario")}
        server = WebUIServer(config, backend, host="127.0.0.1", port=0, diario=diario)
        server.workflow.imposta_operatore("TEST")
        try:
            server.call("diagnostica_antenna", {"antenna": 1})
            server.call("prova_lettura", {"cicli": 2})
            stato, esito = server.call("registro_misure", {"limite": 10})
            assert stato == 200, esito
            generi = [voce["genere"] for voce in esito["misure"]]
            assert "adattamento" in generi, generi
            assert "prova di lettura" in generi, generi
            # Una misura sola per adattamento: il canale radio porta lo stesso
            # dato senza l'interpretazione, e mostrarlo due volte confonderebbe.
            assert generi.count("adattamento") == 1, generi
            misura = next(v for v in esito["misure"] if v["genere"] == "adattamento")
            assert misura["antenna"] == 1
            assert misura["punti"] > 0
            assert misura["risonanza"]
        finally:
            server.shutdown()
    finally:
        diario.chiudi()


def test_senza_diario_il_registro_non_si_inventa_niente():
    """Nessun diario e' un caso normale, non un errore da mostrare a schermo."""
    cartella = _tmp("senza_diario")
    config = _config(cartella)
    config["diario"] = {"attivo": False, "dir": str(cartella / "mai_scritto")}
    server = WebUIServer(config, FakeTagBackend([]), host="127.0.0.1", port=0)
    server.workflow.imposta_operatore("TEST")
    try:
        stato, esito = server.call("registro_misure", {})
        assert stato == 200, esito
        assert esito["misure"] == []
        assert "mai_scritto" in esito["cartella"]
    finally:
        server.shutdown()


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
