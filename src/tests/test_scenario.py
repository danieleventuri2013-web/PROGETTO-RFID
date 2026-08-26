"""Rimontare il banco da una sessione vera.

E' la meta' utile del diario: se da un file di log non si riesce a ricostruire
il campo, il diario resta un registro da leggere invece di un banco da
riaccendere. Le promesse:

* dopo una scrittura vera, il tag ricostruito ha **lo stesso EPC finale**, il
  suo TID e il contenuto della memoria USER — cioe' e' lo stesso tag, non uno
  che gli somiglia;
* un cambio di EPC non sdoppia il tag in due;
* un tag che nel campo vero si leggeva a fatica resta difficile anche qui,
  altrimenti la simulazione sarebbe piu' gentile della realta' proprio dove
  serve severita';
* il banco di prova esiste **solo** con il lettore simulato.
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

from rfid_silion.diario import BackendTracciato, Diario, leggi_diario
from rfid_silion.scenario import (
    BancoSimulato,
    Scenario,
    TagRicostruito,
    banco_da_diario,
    carica_scenario,
    scenario_vuoto,
)
from rfid_silion.service import InventoryRequest
from webui.server import WebUIServer
from webui.workflow import Workflow

CF_PAZIENTE = "MRTMTT25D09F205Z"


def _tmp(nome: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"scenario_{nome}_"))


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
        },
        "webui": {"host": "127.0.0.1", "port": 0},
    }


def _tag_vergine(indice: int) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=bytes.fromhex("E2801190200050A1B2C300") + bytes([indice]),
        user_bytes=64,
    )


def _sessione_con_una_scrittura(cartella: Path) -> tuple[Diario, FakeTagBackend]:
    """Una accettazione e una scrittura vere, con il diario acceso."""
    diario = Diario(cartella / "diario")
    finto = FakeTagBackend([_tag_vergine(1)], antennas=(1, 2, 3))
    flusso = Workflow(_config(cartella), BackendTracciato(finto, diario))
    flusso.imposta_operatore("TEST")
    flusso.registra_accettazione(
        {
            "codice_fiscale": CF_PAZIENTE,
            "cognome": "Della Valle",
            "nome": "Gianfranco",
            "sesso": "M",
            "contenitori": 1,
            "descrizione": "pezzo operatorio",
        }
    )
    flusso.conferma_conteggio()
    esito = flusso.scrivi_prossimo()
    assert esito["scrittura"]["ok"], esito["scrittura"]
    flusso.chiudi()
    diario.chiudi()
    return diario, finto


# ---------------------------------------------------------------------------
# Ricostruzione dal diario
# ---------------------------------------------------------------------------
def test_dopo_una_scrittura_vera_il_tag_torna_intero():
    cartella = _tmp("scrittura")
    diario, finto = _sessione_con_una_scrittura(cartella)
    vero = finto.tags[0]

    scenario = banco_da_diario(diario.percorso)
    assert len(scenario.tag) == 1, [voce.epc for voce in scenario.tag]
    ricostruito = scenario.tag[0]

    # Stesso EPC finale: la catena del cambio di EPC e' stata seguita.
    assert ricostruito.epc == vero.epc_hex
    assert ricostruito.tid == vero.tid.hex().upper()
    # E il payload scritto nella memoria USER, che e' cio' che rende il tag
    # autosufficiente: senza, il banco rimonterebbe un tag muto.
    assert ricostruito.user, "la memoria USER scritta deve essere ricostruita"
    assert bytes(vero.user).startswith(ricostruito.user)


def test_il_banco_ricostruito_risponde_come_quello_vero():
    cartella = _tmp("rimontato")
    diario, finto = _sessione_con_una_scrittura(cartella)

    scenario = banco_da_diario(diario.percorso)
    # La scrittura usa la sola antenna 3: il tag non ha mai avuto occasione di
    # rispondere alle antenne di lettura, quindi non deve risultarne sordo.
    assert scenario.tag[0].antenne == {3}
    banco = BancoSimulato(scenario, antenne=(1, 2, 3))
    banco.metti(finto.tags[0].epc_hex)
    risposta = banco.backend.inventory(InventoryRequest(antennas=(1, 2), timeout_ms=400))
    assert risposta.ok
    assert finto.tags[0].epc_hex in risposta.data["unique_epcs"]

    # E la memoria USER si rilegge, come sul tag vero.
    from rfid_silion.service import MemoryBank, ReadRequest

    lettura = banco.backend.read(
        ReadRequest(antennas=(1,), bank=MemoryBank.USER, address=0, word_count=4)
    )
    assert lettura.ok
    letto = next(iter(lettura.data["results"].values()))["data"]
    assert letto == bytes(finto.tags[0].user[:8]).hex().upper()


def test_un_cambio_di_epc_non_sdoppia_il_tag():
    """Senza seguire la catena si avrebbero due tag dove ce n'era uno."""
    cartella = _tmp("catena")
    diario, _ = _sessione_con_una_scrittura(cartella)
    record = [
        voce
        for voce in leggi_diario(diario.percorso)
        if voce.get("nome") == "write_epc"
    ]
    assert record, "la scrittura deve aver cambiato l'EPC, altrimenti il test non prova niente"
    assert len(banco_da_diario(diario.percorso).tag) == 1


def test_un_tag_difficile_resta_difficile():
    """Il tasso di rilevamento diventa la regola di visibilita' del simulato."""
    facile = TagRicostruito(epc="AA" * 12, letture=10, cicli=10)
    saltuario = TagRicostruito(epc="BB" * 12, letture=5, cicli=10)
    ostico = TagRicostruito(epc="CC" * 12, letture=2, cicli=10)
    assert facile.simulato().visible_every == 1
    assert saltuario.simulato().visible_every == 2
    assert ostico.simulato().visible_every == 3


def test_un_tag_sordo_da_un_lato_lo_resta():
    """Interrogato dalle antenne 1 e 2, ha risposto solo alla 2."""
    voce = TagRicostruito(
        epc="DD" * 12,
        antenne={2},
        occasioni={1, 2},
        letture=10,
        cicli=10,
        rssi=[-70, -40, -55],
    )
    tag = voce.simulato()
    assert tag.visible_from == {2}
    # La mediana, non la media: un picco isolato non deve spostare il valore.
    assert tag.rssi == -55


def test_un_antenna_mai_interrogata_non_diventa_un_muro():
    """Il caso vero: un tag scritto alla postazione, dove si usa la sola 3.

    Dichiararlo sordo alle antenne di lettura lo renderebbe introvabile in un
    sigillo che nella realta' lo troverebbe: la simulazione sarebbe piu' severa
    del banco, che e' un modo diverso ma altrettanto inutile di sbagliare.
    """
    voce = TagRicostruito(epc="EE" * 12, antenne={3}, occasioni={3}, letture=5, cicli=5)
    assert voce.simulato().visible_from is None


def test_un_diario_senza_letture_non_inventa_tag():
    cartella = _tmp("muto")
    diario = Diario(cartella)
    diario.scrivi("api", "registra", {"cognome": "Rossi"})
    diario.chiudi()
    assert banco_da_diario(diario.percorso).tag == []


# ---------------------------------------------------------------------------
# Il banco
# ---------------------------------------------------------------------------
def test_il_banco_mette_e_toglie_dal_campo():
    scenario = scenario_vuoto(3)
    banco = BancoSimulato(scenario)
    assert banco.elenco()["campo"] == []
    assert len(banco.elenco()["magazzino"]) == 3

    primo = scenario.tag[0].epc
    banco.metti(primo)
    assert [voce["epc"] for voce in banco.elenco()["campo"]] == [primo]
    assert len(banco.elenco()["magazzino"]) == 2

    # Appoggiarlo due volte non lo duplica: sul banco vero il contenitore e' uno.
    banco.metti(primo)
    assert len(banco.elenco()["campo"]) == 1

    banco.togli(primo)
    assert banco.elenco()["campo"] == []
    assert len(banco.elenco()["magazzino"]) == 3


def test_il_banco_riconosce_un_tag_dopo_il_cambio_di_epc():
    """Dopo una scrittura il tag ha un EPC nuovo, ma e' sempre quello."""
    scenario = scenario_vuoto(1)
    banco = BancoSimulato(scenario)
    vecchio = scenario.tag[0].epc
    banco.metti(vecchio)
    banco.backend.tags[0].epc = bytes.fromhex("0100010000000301020304")
    nuovo = banco.backend.tags[0].epc_hex

    assert [voce["epc"] for voce in banco.elenco()["campo"]] == [nuovo]
    banco.togli(nuovo)  # col nome nuovo
    assert banco.elenco()["campo"] == []
    banco.metti(vecchio)  # e anche col nome vecchio
    assert len(banco.elenco()["campo"]) == 1


def test_un_tag_che_non_esiste_lo_dice():
    banco = BancoSimulato(scenario_vuoto(1))
    try:
        banco.metti("FFFFFFFFFFFFFFFFFFFFFFFF")
        raise AssertionError("KeyError attesa")
    except KeyError as exc:
        assert "nessun tag" in str(exc)


def test_i_tag_vergini_partono_fuori_dal_campo():
    """Sul banco vero i contenitori si appoggiano uno per volta."""
    scenario = scenario_vuoto(6)
    assert len(scenario.tag) == 6
    assert scenario.nel_campo == set()


# ---------------------------------------------------------------------------
# Scenario scritto a mano
# ---------------------------------------------------------------------------
def test_uno_scenario_yaml_si_legge():
    cartella = _tmp("yaml")
    file = cartella / "banco.yaml"
    file.write_text(
        "tag:\n"
        '  - epc: "0100010000000301020304"\n'
        '    tid: "E28011902000000000000001"\n'
        "    antenne: [1]\n"
        "    rssi: -62\n"
        "    visibile_ogni: 3\n"
        "    nel_campo: true\n"
        '  - epc: "0100010000000301020305"\n'
        "    nel_campo: false\n",
        encoding="utf-8",
    )
    scenario = carica_scenario(file)
    assert len(scenario.tag) == 2
    assert scenario.nel_campo == {"0100010000000301020304"}
    banco = BancoSimulato(scenario)
    campo = banco.elenco()["campo"]
    assert len(campo) == 1
    assert campo[0]["antenne"] == [1]
    assert campo[0]["rssi"] == -62
    assert campo[0]["visibile_ogni"] == 3


def test_carica_scenario_accetta_anche_un_diario():
    cartella = _tmp("misto")
    diario, _ = _sessione_con_una_scrittura(cartella)
    assert carica_scenario(diario.percorso).tag


# ---------------------------------------------------------------------------
# La rotta del banco
# ---------------------------------------------------------------------------
class _Postazione:
    def __init__(self, tmp: Path, banco=None):
        backend = banco.backend if banco is not None else FakeTagBackend([], antennas=(1, 2, 3))
        self.server = WebUIServer(
            _config(tmp), backend, host="127.0.0.1", port=0, banco=banco
        )
        self.server.workflow.imposta_operatore("TEST")
        self.server.start_background()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
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


def test_il_banco_di_prova_non_esiste_col_lettore_vero():
    """Nessuna postazione vera deve poter far comparire un campione."""
    with _Postazione(_tmp("vero")) as posto:
        stato, dati = posto.post("/api/simulazione", {"azione": "elenco"})
        assert stato == 404, dati
        stato, descrizione = posto.post("/api/descrivi")
        assert descrizione["banco_di_prova"] is False


def test_col_lettore_simulato_il_banco_si_comanda():
    scenario = scenario_vuoto(2)
    banco = BancoSimulato(scenario)
    with _Postazione(_tmp("simulato"), banco) as posto:
        stato, descrizione = posto.post("/api/descrivi")
        assert descrizione["banco_di_prova"] is True

        stato, dati = posto.post("/api/simulazione", {"azione": "elenco"})
        assert stato == 200 and len(dati["magazzino"]) == 2

        epc = scenario.tag[0].epc
        stato, dati = posto.post("/api/simulazione", {"azione": "metti", "epc": epc})
        assert stato == 200 and [v["epc"] for v in dati["campo"]] == [epc]

        # E la sorveglianza del piatto lo vede: e' il punto di tutto.
        stato, guardato = posto.post("/api/sorveglia")
        assert stato == 200 and guardato["stato"] == "pronto", guardato

        stato, dati = posto.post("/api/simulazione", {"azione": "svuota"})
        assert stato == 200 and dati["campo"] == []
        stato, guardato = posto.post("/api/sorveglia")
        assert guardato["stato"] == "vuoto"


def test_il_banco_rifiuta_una_azione_che_non_esiste():
    with _Postazione(_tmp("azione"), BancoSimulato(scenario_vuoto(1))) as posto:
        stato, dati = posto.post("/api/simulazione", {"azione": "esplodi"})
        assert stato == 400
        assert "sconosciuta" in dati["errore"]

        stato, dati = posto.post("/api/simulazione", {"azione": "metti", "epc": "00"})
        assert stato == 400
        assert "nessun tag" in dati["errore"]


def test_uno_scenario_vuoto_si_descrive():
    scenario = Scenario(origine="di prova")
    assert scenario.descrivi() == {"origine": "di prova", "tag": 0, "nel_campo": 0}


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
