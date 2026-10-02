"""Riempire la scatola un campione per volta.

E' la parte del flusso che il sigillo non copre: il sigillo **verifica** un
elenco a scatola chiusa, questo **costruisce** l'elenco mentre la scatola e'
aperta. Le promesse:

* un campione riconosciuto entra e viene annunciato **una volta sola**: il bip
  che suona a ogni giro smette di voler dire qualcosa;
* un tag mai scritto viene chiamato per nome — «campione non inizializzato» —
  invece di essere ignorato o contato;
* un contenitore gia' in un'altra scatola non entra in questa;
* un tag che sparisce per un giro **non esce dall'elenco**: le soglie sono
  asimmetriche apposta, perche' perdere una riga fa credere all'operatore di
  dover aggiungere un campione che c'e' gia';
* una lettura fallita non svuota l'elenco: «non so» non e' «non c'e'».
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag

from lims.codec import build_box_epc, build_epc
from lims.db import LimsDatabase
from lims.model import Case, Patient, Sex, Shipment, Specimen
from lims.riempimento import (
    Esito,
    PoliticaRiempimento,
    SessioneRiempimento,
    politica_da_config,
    potenza_da_config,
)

CF_UNO = "MRTMTT25D09F205Z"
CF_DUE = "RSSMRA80A01H501U"


# ---------------------------------------------------------------------------
# Attrezzatura
# ---------------------------------------------------------------------------
def _archivio() -> LimsDatabase:
    return LimsDatabase(":memory:")


def _contenitore(db: LimsDatabase, codice_fiscale: str, cognome: str, epc: str) -> int:
    """Un contenitore gia' scritto, come dopo una sessione di accettazione."""
    patient_id = db.upsert_patient(
        Patient(codice_fiscale=codice_fiscale, cognome=cognome, nome="Prova", sesso=Sex.MALE)
    )
    accession_id = db.next_accession_id()
    case_id = db.create_case(
        Case(accession_id=accession_id, patient_id=patient_id, data_prelievo=None)
    )
    specimen_id = db.add_specimen(
        Specimen(case_id=case_id, descrizione="pezzo operatorio", material_code=1)
    )
    container_id = db.plan_containers(specimen_id, 1)[0]
    db.assign_epc(container_id, epc, tid="E280119020005000000000" + f"{container_id:02X}")
    db.mark_provisioned(container_id)
    return container_id


def _epc(indice: int) -> str:
    """Un EPC di contenitore ben formato, come lo produce il codec."""
    return build_epc(1, indice, 1, 1).hex().upper()


def _epc_scatola() -> str:
    return build_box_epc(1, 7).hex().upper()


def _tag(epc: str, **extra) -> SimulatedTag:
    return SimulatedTag(bytes.fromhex(epc), user_bytes=64, **extra)


def _sessione(backend, db, **extra) -> SessioneRiempimento:
    politica = extra.pop(
        "politica", PoliticaRiempimento(cicli_per_giro=2, cicli_per_entrare=2, cicli_per_uscire=4)
    )
    return SessioneRiempimento(
        backend, db, antenne=(1, 2), politica=politica, **extra
    )


def _esiti(giro: dict) -> list[str]:
    return [evento["esito"] for evento in giro["eventi"]]


# ---------------------------------------------------------------------------
# Riconoscere
# ---------------------------------------------------------------------------
def test_un_campione_riconosciuto_entra_e_si_annuncia_una_volta_sola():
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)

    primo = sessione.passa()
    assert _esiti(primo) == [Esito.ENTRATO.value], primo
    evento = primo["eventi"][0]
    # Chi e', non solo un EPC: e' quello che l'operatore legge sullo schermo.
    assert evento["paziente"] == "DELLA VALLE PROVA"
    assert evento["codice_fiscale"] == CF_UNO
    assert evento["descrizione"] == "pezzo operatorio"
    assert evento["etichetta"] == "1/1"
    assert evento["anomalia"] is False
    assert primo["quanti"] == 1

    # Al giro dopo e' ancora li', ma non si riannuncia: un bip per giro non
    # direbbe piu' niente.
    secondo = sessione.passa()
    assert secondo["eventi"] == []
    assert secondo["quanti"] == 1


def test_i_campioni_entrano_uno_alla_volta_come_al_banco():
    db = _archivio()
    epc = [_epc(indice) for indice in (1, 2, 3)]
    for indice, valore in enumerate(epc, start=1):
        _contenitore(db, CF_UNO if indice < 3 else CF_DUE, f"Paziente{indice}", valore)
    backend = FakeTagBackend([], antennas=(1, 2))
    sessione = _sessione(backend, db)

    contati = []
    for valore in epc:
        backend.tags.append(_tag(valore))
        giro = sessione.passa()
        assert _esiti(giro) == [Esito.ENTRATO.value], giro
        contati.append(giro["quanti"])
    assert contati == [1, 2, 3]
    assert sorted(sessione.epc_dentro()) == sorted(epc)


def test_serve_piu_di_una_lettura_per_entrare():
    """La quantita' si conferma con letture ripetute, non con una."""
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([], antennas=(1, 2))
    # Un tag difficile: risponde a un ciclo su due.
    backend.tags.append(_tag(epc, visible_every=2))
    sessione = _sessione(
        backend,
        db,
        politica=PoliticaRiempimento(
            cicli_per_giro=1, cicli_per_entrare=2, cicli_per_uscire=4, pausa_s=0
        ),
    )
    primo = sessione.passa()
    assert primo["eventi"] == [], "una lettura sola non basta a dichiararlo dentro"
    # Continuando a leggere, prima o poi si conferma.
    for _ in range(6):
        giro = sessione.passa()
        if giro["eventi"]:
            assert _esiti(giro) == [Esito.ENTRATO.value]
            break
    else:
        raise AssertionError("il tag difficile non e' mai entrato")


def test_un_tag_lontano_che_ammicca_non_entra_mai():
    """La controprova del test precedente.

    Accumulare le letture serve a riconoscere il tag schermato dentro la
    scatola, non ad accogliere quello sul tavolo accanto che ogni tanto
    risponde. Il conteggio si azzera quando il tag resta assente abbastanza a
    lungo da essere dichiarato fuori.
    """
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([_tag(epc, visible_every=8)], antennas=(1, 2))
    sessione = _sessione(
        backend,
        db,
        politica=PoliticaRiempimento(
            cicli_per_giro=1, cicli_per_entrare=2, cicli_per_uscire=3, pausa_s=0
        ),
    )
    for _ in range(24):
        assert sessione.passa()["quanti"] == 0


# ---------------------------------------------------------------------------
# Le anomalie
# ---------------------------------------------------------------------------
def test_un_tag_mai_scritto_e_un_campione_non_inizializzato():
    """Il caso che il flusso di lavoro chiede di segnalare a voce alta."""
    db = _archivio()
    backend = FakeTagBackend([_tag("AAAAAAAAAAAAAAAAAAAA0001")], antennas=(1, 2))
    sessione = _sessione(backend, db)

    giro = sessione.passa()
    assert _esiti(giro) == [Esito.NON_INIZIALIZZATO.value], giro
    evento = giro["eventi"][0]
    assert evento["anomalia"] is True
    assert "non e' stato preparato" in evento["dettaglio"]
    # E non si conta: nella scatola non c'e' un campione, c'e' un problema.
    assert giro["quanti"] == 0
    assert len(giro["anomalie"]) == 1


def test_un_contenitore_di_un_altra_spedizione_non_entra():
    db = _archivio()
    epc = _epc(1)
    container_id = _contenitore(db, CF_UNO, "Della Valle", epc)
    altra = db.create_shipment(Shipment(destinazione="Ospedale Terni", data=None))
    db.add_to_shipment(altra, [container_id])

    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)
    giro = sessione.passa()
    assert _esiti(giro) == [Esito.ALTRA_SPEDIZIONE.value], giro
    assert "gia' in un'altra scatola" in giro["eventi"][0]["dettaglio"]
    # Ma si sa comunque di chi e': serve per andarlo a cercare.
    assert giro["eventi"][0]["paziente"] == "DELLA VALLE PROVA"
    assert giro["quanti"] == 0


def test_i_contenitori_gia_in_questa_scatola_non_sono_anomalie():
    """Dopo un riavvio la scatola e' piena: sono i nostri, non intrusi."""
    db = _archivio()
    epc = _epc(1)
    container_id = _contenitore(db, CF_UNO, "Della Valle", epc)
    nostra = db.create_shipment(Shipment(destinazione="Ospedale Foligno", data=None))
    db.add_to_shipment(nostra, [container_id])

    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db, contenitori_della_scatola=[container_id])
    giro = sessione.passa()
    assert _esiti(giro) == [Esito.ENTRATO.value], giro
    assert giro["quanti"] == 1


def test_un_contenitore_annullato_viene_segnalato():
    db = _archivio()
    epc = _epc(1)
    container_id = _contenitore(db, CF_UNO, "Della Valle", epc)
    db.void_container(container_id, reason="contenitore rotto")

    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    giro = _sessione(backend, db).passa()
    assert _esiti(giro) == [Esito.ANNULLATO.value], giro
    assert giro["quanti"] == 0


def test_il_tag_del_coperchio_identifica_la_scatola_e_non_si_conta():
    db = _archivio()
    epc = _epc_scatola()
    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)

    giro = sessione.passa()
    assert _esiti(giro) == [Esito.SCATOLA.value], giro
    assert giro["quanti"] == 0
    assert giro["anomalie"] == [], "il coperchio non e' un intruso"
    assert sessione.box_epc == epc


# ---------------------------------------------------------------------------
# Uscire dall'elenco
# ---------------------------------------------------------------------------
def test_un_tag_che_sparisce_per_un_giro_non_esce_dall_elenco():
    """Perdere una riga fa credere di dover aggiungere un campione che c'e'."""
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    tag = _tag(epc)
    backend = FakeTagBackend([tag], antennas=(1, 2))
    sessione = _sessione(backend, db)
    assert _esiti(sessione.passa()) == [Esito.ENTRATO.value]

    backend.tags.clear()  # un giro senza vederlo: schermato da un altro vasetto
    giro = sessione.passa()
    assert giro["eventi"] == [], "un solo giro mancato non lo toglie"
    assert giro["quanti"] == 1

    backend.tags.append(tag)
    assert sessione.passa()["quanti"] == 1


def test_un_campione_tolto_davvero_esce_dall_elenco():
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)
    sessione.passa()

    backend.tags.clear()
    for _ in range(5):
        giro = sessione.passa()
        if giro["eventi"]:
            break
    assert _esiti(giro) == [Esito.USCITO.value], giro
    assert giro["quanti"] == 0


def test_le_soglie_invertite_vengono_rifiutate():
    """Con le soglie al contrario l'elenco lampeggerebbe a ogni giro."""
    try:
        PoliticaRiempimento(cicli_per_entrare=4, cicli_per_uscire=2)
        raise AssertionError("ValueError attesa")
    except ValueError as exc:
        assert "lampeggia" in str(exc)


# ---------------------------------------------------------------------------
# Robustezza
# ---------------------------------------------------------------------------
def test_una_lettura_fallita_non_svuota_l_elenco():
    """«Non so» non e' «non c'e'»."""
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)
    sessione.passa()
    assert sessione.riepilogo()["quanti"] == 1

    class _Muto:
        """Un lettore che non risponde piu'."""

        def inventory(self, request):
            from rfid_silion.service import ServiceResponse, ServiceState

            return ServiceResponse(
                operation="inventory",
                ok=False,
                state=ServiceState.READY,
                error={"type": "SilionTimeoutError", "message": "nessuna risposta"},
            )

        def configure(self, settings):
            raise AssertionError("la radio e' gia' pronta")

    sessione.backend = _Muto()
    giro = sessione.passa()
    assert giro["errore"], "il guasto va dichiarato"
    assert giro["eventi"] == []
    assert giro["quanti"] == 1, "l'elenco resta quello che era"


def test_dimenticare_un_tag_lo_toglie_dalla_memoria():
    """Serve alla correzione a mano: tolto e dimenticato, se no rientra da solo."""
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)
    sessione.passa()
    assert sessione.dimentica(epc) is True
    assert sessione.riepilogo()["quanti"] == 0
    assert sessione.dimentica(epc) is False


def test_declassare_toglie_dal_conteggio_ma_lascia_il_motivo():
    db = _archivio()
    epc = _epc(1)
    _contenitore(db, CF_UNO, "Della Valle", epc)
    backend = FakeTagBackend([_tag(epc)], antennas=(1, 2))
    sessione = _sessione(backend, db)
    sessione.passa()

    sessione.declassa(epc, Esito.ALTRA_SPEDIZIONE, "preso da un'altra scatola")
    riepilogo = sessione.riepilogo()
    assert riepilogo["quanti"] == 0
    assert len(riepilogo["anomalie"]) == 1
    assert riepilogo["anomalie"][0]["dettaglio"] == "preso da un'altra scatola"


def test_senza_antenne_la_sessione_non_parte():
    try:
        SessioneRiempimento(FakeTagBackend([]), _archivio(), antenne=())
        raise AssertionError("ValueError attesa")
    except ValueError as exc:
        assert "almeno un'antenna" in str(exc)


# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
def test_la_potenza_del_riempimento_e_la_piu_bassa_del_sigillo():
    """A scatola aperta il tavolo accanto e' pieno: meno portata, meno vicini."""
    assert potenza_da_config({"seal_powers_cdbm": [2000, 2500, 2900]}) == 2000
    assert potenza_da_config({}) is None
    # Una potenza dichiarata esplicitamente vince su tutto.
    assert (
        potenza_da_config(
            {"seal_powers_cdbm": [2000, 2900], "riempimento": {"potenza_cdbm": 1500}}
        )
        == 1500
    )


def test_le_soglie_si_leggono_dalla_configurazione():
    politica = politica_da_config(
        {"riempimento": {"cicli_per_giro": 4, "cicli_per_entrare": 3, "cicli_per_uscire": 9}}
    )
    assert (politica.cicli_per_giro, politica.cicli_per_entrare) == (4, 3)
    assert politica.cicli_per_uscire == 9
    # Senza sezione, i valori prudenti di partenza.
    predefinita = politica_da_config({})
    assert predefinita.cicli_per_uscire > predefinita.cicli_per_entrare


def test_la_potenza_si_applica_una_volta_sola():
    db = _archivio()
    backend = FakeTagBackend([], antennas=(1, 2))
    sessione = _sessione(backend, db, potenza_cdbm=1800)
    sessione.passa()
    sessione.passa()
    assert backend.configure_calls == 1, "riconfigurare a ogni giro sarebbe tempo sprecato"
    assert backend.read_power_cdbm == 1800


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
