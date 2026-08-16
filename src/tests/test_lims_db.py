"""Test dell'archivio SQLite: schema, vincoli, registro EPC e traccia operazioni."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims.db import (
    SCHEMA_VERSION,
    DuplicateEpcError,
    LimsDatabase,
    LimsDatabaseError,
    NotFoundError,
)
from lims.model import (
    Case,
    ContainerState,
    Patient,
    Sex,
    Shipment,
    ShipmentState,
    Specimen,
)

CF_VALIDO = "MRTMTT25D09F205Z"
CF_ALTRO = "RSSMRA85M01H501" + "Q"


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _paziente(cf: str = CF_VALIDO) -> Patient:
    return Patient(
        codice_fiscale=cf,
        cognome="Rossi",
        nome="Mario",
        data_nascita=date(1985, 4, 9),
        sesso=Sex.MALE,
    )


def _archivio_popolato(db: LimsDatabase, *, totale: int = 3) -> tuple[int, list[int]]:
    """Crea paziente, accettazione, reperto e `totale` contenitori pianificati."""
    patient_id = db.upsert_patient(_paziente())
    accession_id = db.next_accession_id()
    case_id = db.create_case(
        Case(
            accession_id=accession_id,
            patient_id=patient_id,
            data_prelievo=date(2026, 8, 15),
            reparto="Chirurgia",
            medico="Dott. Bianchi",
        )
    )
    specimen_id = db.add_specimen(
        Specimen(
            case_id=case_id,
            descrizione="Nodulo quadrante supero-esterno",
            material_code=1,
            site_code=2,
            fixative_code=1,
        )
    )
    return accession_id, db.plan_containers(specimen_id, totale)


# --------------------------------------------------------------------------
# Schema e migrazioni
# --------------------------------------------------------------------------
def test_schema_creato_alla_prima_apertura() -> None:
    with LimsDatabase() as db:
        assert db.schema_version == SCHEMA_VERSION


def test_riapertura_non_ricrea_lo_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "sotto" / "lims.db"
        with LimsDatabase(percorso) as db:
            patient_id = db.upsert_patient(_paziente())
        with LimsDatabase(percorso) as db:
            assert db.schema_version == SCHEMA_VERSION
            assert db.get_patient(patient_id).codice_fiscale == CF_VALIDO


def test_database_di_versione_futura_rifiutato() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "futuro.db"
        with LimsDatabase(percorso) as db:
            db.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            db.connection.commit()
        _expect(
            LimsDatabaseError,
            lambda: LimsDatabase(percorso),
            "un archivio scritto da una versione piu' recente non va aperto alla cieca",
        )


def test_chiavi_esterne_attive() -> None:
    with LimsDatabase() as db:
        _expect(
            sqlite3.IntegrityError,
            lambda: db.add_specimen(Specimen(case_id=999, descrizione="orfano")),
            "un reperto senza accettazione non deve essere accettato",
        )


# --------------------------------------------------------------------------
# Pazienti
# --------------------------------------------------------------------------
def test_upsert_paziente_inserisce_e_aggiorna() -> None:
    with LimsDatabase() as db:
        primo = db.upsert_patient(_paziente())
        secondo = db.upsert_patient(
            Patient(codice_fiscale=CF_VALIDO, cognome="Rossi", nome="Mario Luigi")
        )
        assert primo == secondo, "il codice fiscale e' la chiave naturale"
        assert db.get_patient(primo).nome == "MARIO LUIGI"


def test_find_patient_e_get_patient() -> None:
    with LimsDatabase() as db:
        patient_id = db.upsert_patient(_paziente())
        trovato = db.find_patient(" mrt mtt25d09f205z ")
        assert trovato is not None and trovato.id == patient_id
        assert trovato.data_nascita == date(1985, 4, 9)
        assert trovato.sesso == Sex.MALE
        assert db.find_patient(CF_ALTRO) is None
        _expect(NotFoundError, lambda: db.get_patient(999), "paziente inesistente")


# --------------------------------------------------------------------------
# Accettazioni e contenitori
# --------------------------------------------------------------------------
def test_next_accession_id_progressivo() -> None:
    with LimsDatabase() as db:
        assert db.next_accession_id() == 1
        patient_id = db.upsert_patient(_paziente())
        db.create_case(Case(accession_id=1, patient_id=patient_id))
        assert db.next_accession_id() == 2


def test_accettazione_duplicata_rifiutata() -> None:
    with LimsDatabase() as db:
        patient_id = db.upsert_patient(_paziente())
        db.create_case(Case(accession_id=7, patient_id=patient_id))
        _expect(
            LimsDatabaseError,
            lambda: db.create_case(Case(accession_id=7, patient_id=patient_id)),
            "due accettazioni con lo stesso numero",
        )


def test_plan_containers_crea_la_serie_completa() -> None:
    with LimsDatabase() as db:
        accession_id, container_ids = _archivio_popolato(db, totale=3)
        assert len(container_ids) == 3
        record = db.containers_for_accession(accession_id)
        assert [item.label for item in record] == ["1/3", "2/3", "3/3"]
        assert all(item.state == ContainerState.PLANNED for item in record)


def test_plan_containers_rifiuta_totali_fuori_intervallo() -> None:
    with LimsDatabase() as db:
        _archivio_popolato(db, totale=1)
        specimen_id = db.connection.execute("SELECT id FROM specimens").fetchone()["id"]
        _expect(ValueError, lambda: db.plan_containers(specimen_id, 0), "zero contenitori")
        _expect(ValueError, lambda: db.plan_containers(specimen_id, 256), "oltre 255")


def test_plan_containers_e_atomico() -> None:
    # Ripianificare lo stesso reperto viola UNIQUE(specimen_id, idx): non deve
    # restare mezza serie di contenitori.
    with LimsDatabase() as db:
        _archivio_popolato(db, totale=2)
        specimen_id = db.connection.execute("SELECT id FROM specimens").fetchone()["id"]
        _expect(
            LimsDatabaseError,
            lambda: db.plan_containers(specimen_id, 2),
            "ripianificazione dello stesso reperto",
        )
        quanti = db.connection.execute("SELECT COUNT(*) AS n FROM containers").fetchone()["n"]
        assert quanti == 2, f"contenitori residui dopo il fallimento: {quanti}"


# --------------------------------------------------------------------------
# Registro di unicita' degli EPC
# --------------------------------------------------------------------------
def test_assign_epc_e_ricerca_per_epc() -> None:
    with LimsDatabase() as db:
        accession_id, container_ids = _archivio_popolato(db)
        db.assign_epc(container_ids[1], "0100a5000f1206020 3deadbe".replace(" ", ""))
        record = db.find_container_by_epc("0100A5000F12060203DEADBE")
        assert record is not None
        assert record.container_id == container_ids[1]
        assert record.label == "2/3"
        assert record.display_name == "ROSSI MARIO"
        assert record.codice_fiscale == CF_VALIDO
        assert record.accession_id == accession_id
        assert record.material_code == 1 and record.site_code == 2
        assert record.data_prelievo == date(2026, 8, 15)


def test_epc_duplicato_rifiutato() -> None:
    """E' la garanzia che RFIDService.generate_epc non puo' dare da solo."""
    with LimsDatabase() as db:
        _, container_ids = _archivio_popolato(db)
        db.assign_epc(container_ids[0], "AABBCCDDEEFF001122334455")
        _expect(
            DuplicateEpcError,
            lambda: db.assign_epc(container_ids[1], "AABBCCDDEEFF001122334455"),
            "due contenitori con lo stesso EPC sarebbero indistinguibili in campo",
        )


def test_epc_exists() -> None:
    with LimsDatabase() as db:
        _, container_ids = _archivio_popolato(db)
        assert db.epc_exists("AABB00112233445566778899") is False
        db.assign_epc(container_ids[0], "AABB00112233445566778899")
        assert db.epc_exists("aabb00112233445566778899") is True


def test_epc_sconosciuto_non_trovato() -> None:
    with LimsDatabase() as db:
        _archivio_popolato(db)
        assert db.find_container_by_epc("FFFFFFFFFFFFFFFFFFFFFFFF") is None


def test_mark_provisioned_registra_tid_e_stato() -> None:
    with LimsDatabase() as db:
        _, container_ids = _archivio_popolato(db)
        db.assign_epc(container_ids[0], "0100A5000F1206010311AABB")
        db.mark_provisioned(container_ids[0], tid="e2801190200050a1", revision=1)
        record = db.get_container(container_ids[0])
        assert record.state == ContainerState.PROVISIONED
        assert record.tid == "E2801190200050A1"
        assert record.revision == 1


# --------------------------------------------------------------------------
# Spedizioni
# --------------------------------------------------------------------------
def test_spedizione_completa() -> None:
    with LimsDatabase() as db:
        _, container_ids = _archivio_popolato(db)
        for offset, container_id in enumerate(container_ids):
            db.assign_epc(container_id, f"0100A5000F12060{offset}03AABBC{offset}")
        shipment_id = db.create_shipment(
            Shipment(destinazione="Laboratorio Centrale", data=date(2026, 8, 16))
        )
        assert db.add_to_shipment(shipment_id, container_ids) == 3
        contenuto = db.shipment_contents(shipment_id)
        assert [item.label for item in contenuto] == ["1/3", "2/3", "3/3"]
        assert all(item.state == ContainerState.SHIPPED for item in contenuto)

        db.set_shipment_state(shipment_id, ShipmentState.SENT)
        stato = db.connection.execute(
            "SELECT state FROM shipments WHERE id=?", (shipment_id,)
        ).fetchone()["state"]
        assert stato == "sent"


def test_aggiunta_ripetuta_alla_spedizione_e_idempotente() -> None:
    with LimsDatabase() as db:
        _, container_ids = _archivio_popolato(db, totale=1)
        shipment_id = db.create_shipment(Shipment(destinazione="Lab"))
        db.add_to_shipment(shipment_id, container_ids)
        db.add_to_shipment(shipment_id, container_ids)
        assert len(db.shipment_contents(shipment_id)) == 1


# --------------------------------------------------------------------------
# Traccia delle operazioni
# --------------------------------------------------------------------------
def test_log_event_e_consultazione() -> None:
    with LimsDatabase() as db:
        _, container_ids = _archivio_popolato(db, totale=1)
        epc = "0100A5000F1206010111AABB"
        db.assign_epc(container_ids[0], epc)
        db.log_event(
            "write_user",
            ok=True,
            epc=epc,
            tid="E2801190",
            container_id=container_ids[0],
            antenna=2,
            rssi=-45,
            operator="dvent",
        )
        db.log_event("verify", ok=False, epc=epc, detail="mismatch", operator="dvent")

        eventi = db.events_for_epc(epc.lower())
        assert len(eventi) == 2
        assert eventi[0]["operation"] == "write_user"
        assert eventi[0]["ok"] == 1 and eventi[0]["antenna"] == 2 and eventi[0]["rssi"] == -45
        assert eventi[1]["ok"] == 0 and eventi[1]["detail"] == "mismatch"
        assert all(evento["operator"] == "dvent" for evento in eventi)


def test_recent_events_ordine_decrescente() -> None:
    with LimsDatabase() as db:
        for indice in range(5):
            db.log_event("inventory", ok=True, detail=str(indice))
        recenti = list(db.recent_events(limit=3))
        assert [evento["detail"] for evento in recenti] == ["4", "3", "2"]


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
