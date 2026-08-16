"""Test della scrittura unica, dell'annullamento e della sostituzione.

Il tag **non torna indietro**: parte col contenitore e resta al laboratorio
destinatario, dove il campione va conservato intatto. Quindi niente riuso, e due
cose devono valere sempre:

* un tag gia' scritto non si riscrive, salvo autorizzazione esplicita;
* un EPC non torna mai disponibile, nemmeno se il contenitore viene annullato.

Quello che invece succede davvero: un contenitore si rompe, o un tag non
funziona. Il campione passa a un contenitore nuovo, e la numerazione «2 di 3»
resta quella.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag
from lims.codec import SpecimenFlags, TagPayload
from lims.crypto import KEY_SIZE, Keyring
from lims.db import (
    SCHEMA_VERSION,
    DuplicateEpcError,
    LimsDatabase,
    LimsDatabaseError,
)
from lims.model import Case, Patient, Specimen, TagState
from lims.tagio import TagIO

CF_A = "MRTMTT25D09F205Z"
CF_B = "RSSMRA85M01H501Q"
CHIAVE = bytes(range(KEY_SIZE))
TID = "E2801190200050A1B2C3D4E5"
VERGINE = bytes.fromhex("AAAAAAAAAAAAAAAAAAAAAAAA")


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _contenitore(db: LimsDatabase, cf: str, accession_id: int) -> int:
    patient_id = db.upsert_patient(Patient(codice_fiscale=cf, cognome="Rossi", nome="Mario"))
    case_id = db.create_case(Case(accession_id=accession_id, patient_id=patient_id))
    specimen_id = db.add_specimen(Specimen(case_id=case_id, material_code=1))
    return db.plan_containers(specimen_id, 1)[0]


# --------------------------------------------------------------------------
# Schema e migrazione
# --------------------------------------------------------------------------
def test_schema_alla_versione_corrente() -> None:
    with LimsDatabase() as db:
        assert db.schema_version == SCHEMA_VERSION
        tabelle = {
            row[0]
            for row in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"tags", "tag_assignments"} <= tabelle
        colonne = {riga[1] for riga in db.connection.execute("PRAGMA table_info(shipments)")}
        assert {"operator", "sealed_at", "sent_at", "sealing_ok"} <= colonne


def test_migrazione_da_v1_conserva_i_dati() -> None:
    """Un archivio in esercizio non puo' perdere nulla nell'aggiornamento."""
    import sqlite3

    from lims import db as db_module

    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "v1.db"

        # Un archivio alla versione 1 **vero**: solo lo schema di allora. Se lo
        # si costruisse con quello corrente e poi si riscrivesse `user_version`,
        # il test proverebbe una migrazione su un database che ha gia' le
        # colonne nuove — cioe' non proverebbe niente.
        grezzo = sqlite3.connect(percorso)
        grezzo.executescript(db_module._SCHEMA)
        # Un contenitore gia' scritto, com'era registrato allora: le tabelle
        # `tags` e `tag_assignments` ancora non esistevano.
        adesso = "2026-01-15T09:00:00+01:00"
        grezzo.execute(
            "INSERT INTO patients (codice_fiscale, cognome, nome, sesso, created_at)"
            " VALUES (?,?,?,?,?)",
            (CF_A, "ROSSI", "MARIO", "M", adesso),
        )
        grezzo.execute(
            "INSERT INTO cases (accession_id, patient_id, created_at) VALUES (?,?,?)",
            (100, 1, adesso),
        )
        grezzo.execute(
            "INSERT INTO specimens (case_id, created_at) VALUES (?,?)", (1, adesso)
        )
        grezzo.execute(
            "INSERT INTO containers"
            " (specimen_id, idx, total, epc, tid, state, revision, provisioned_at, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (1, 1, 1, "0100A5000064010100AABB", TID, "provisioned", 0, adesso, adesso),
        )
        grezzo.execute("PRAGMA user_version = 1")
        grezzo.commit()
        grezzo.close()

        # Riapertura: la migrazione porta alla versione corrente e recupera i
        # contenitori gia' scritti nel nuovo modello ad asset.
        with LimsDatabase(percorso) as db:
            assert db.schema_version == SCHEMA_VERSION
            record = db.find_container_by_epc("0100A5000064010100AABB")
            assert record is not None
            assert record.codice_fiscale == CF_A
            # Il contenitore gia' scritto entra nel nuovo modello ad asset.
            tag = db.get_tag(TID)
            assert tag is not None and tag["state"] == TagState.ASSIGNED.value
            assert db.active_assignment(TID) is not None


def test_database_di_versione_futura_ancora_rifiutato() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "futuro.db"
        with LimsDatabase(percorso) as db:
            db.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
            db.connection.commit()
        _expect(LimsDatabaseError, lambda: LimsDatabase(percorso), "versione futura")


# --------------------------------------------------------------------------
# Parco tag
# --------------------------------------------------------------------------
def test_censimento_e_stato_iniziale() -> None:
    with LimsDatabase() as db:
        db.register_tag(TID, user_memory_bytes=64, model="Alien Higgs-3", reference_rssi=-42)
        tag = db.get_tag(TID)
        assert tag["state"] == TagState.FREE.value
        assert tag["user_memory_bytes"] == 64
        assert tag["reference_rssi"] == -42
        assert db.tags_by_state(TagState.FREE)[0]["tid"] == TID


def test_assegnazione_registra_il_chip_sul_contenitore() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        db.register_tag(TID, user_memory_bytes=64)
        db.assign_tag(TID, container_id, "0100A5000064010100AABB")

        assert db.get_tag(TID)["state"] == TagState.ASSIGNED.value
        assert db.get_tag(TID)["write_count"] == 1
        assert db.active_assignment(TID)["epc"] == "0100A5000064010100AABB"
        assert db.find_container_by_tid(TID).codice_fiscale == CF_A


def test_un_tag_gia_scritto_non_si_riassegna() -> None:
    """Scrittura unica: il tag e' partito, o e' stato scartato, o e' difettoso."""
    with LimsDatabase() as db:
        primo = _contenitore(db, CF_A, 100)
        secondo = _contenitore(db, CF_B, 200)
        db.register_tag(TID)
        db.assign_tag(TID, primo, "0100A5000064010100AABB")

        _expect(
            LimsDatabaseError,
            lambda: db.assign_tag(TID, secondo, "0100A50000C8010100CCDD"),
            "riassegnazione di un tag gia' scritto",
        )


def test_un_epc_non_torna_mai_disponibile() -> None:
    """Nemmeno l'annullamento libera un EPC: confonderebbe due campioni."""
    with LimsDatabase() as db:
        primo = _contenitore(db, CF_A, 100)
        secondo = _contenitore(db, CF_B, 200)
        db.register_tag(TID)
        altro = db.register_tag("E28011902000509999888877")
        db.assign_tag(TID, primo, "0100A5000064010100AABB")
        db.void_container(primo, reason="contenitore rotto")

        _expect(
            DuplicateEpcError,
            lambda: db.assign_tag(altro, secondo, "0100A5000064010100AABB"),
            "riuso di un EPC annullato",
        )


# --------------------------------------------------------------------------
# Annullamento e sostituzione: i casi che succedono davvero
# --------------------------------------------------------------------------
def test_annullamento_richiede_una_motivazione() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        _expect(
            ValueError,
            lambda: db.void_container(container_id, reason="   "),
            "annullamento senza motivo",
        )


def test_contenitore_rotto_viene_sostituito_alla_stessa_posizione() -> None:
    with LimsDatabase() as db:
        patient_id = db.upsert_patient(
            Patient(codice_fiscale=CF_A, cognome="Rossi", nome="Mario")
        )
        case_id = db.create_case(Case(accession_id=100, patient_id=patient_id))
        specimen_id = db.add_specimen(Specimen(case_id=case_id, material_code=1))
        contenitori = db.plan_containers(specimen_id, 3)

        db.register_tag(TID)
        db.assign_tag(TID, contenitori[1], "0100A5000064020300AABB")

        sostituto = db.replace_container(contenitori[1], reason="contenitore rotto")

        attivi = db.active_containers_for_accession(100)
        assert [r.label for r in attivi] == ["1/3", "2/3", "3/3"], "la serie resta completa"
        assert sostituto in [r.container_id for r in attivi]
        assert contenitori[1] not in [r.container_id for r in attivi]

        riga = db.connection.execute(
            "SELECT idx, total, replaces, state FROM containers WHERE id=?", (sostituto,)
        ).fetchone()
        assert riga["idx"] == 2 and riga["total"] == 3, "la numerazione non cambia"
        assert riga["replaces"] == contenitori[1]

        annullato = db.connection.execute(
            "SELECT state, voided_reason FROM containers WHERE id=?", (contenitori[1],)
        ).fetchone()
        assert annullato["state"] == "voided"
        assert annullato["voided_reason"] == "contenitore rotto"


def test_tag_guasto_finisce_in_quarantena_non_fra_gli_annullati() -> None:
    # La distinzione serve: un contenitore rotto non dice nulla sul chip, un tag
    # che non ha funzionato non va rimesso in gioco.
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        db.register_tag(TID)
        db.assign_tag(TID, container_id, "0100A5000064010100AABB")

        db.void_container(container_id, reason="tag non risponde", tag_faulty=True)
        assert db.get_tag(TID)["state"] == TagState.QUARANTINE.value

        with LimsDatabase() as altro_db:
            altro = _contenitore(altro_db, CF_A, 100)
            altro_db.register_tag(TID)
            altro_db.assign_tag(TID, altro, "0100A5000064010100AABB")
            altro_db.void_container(altro, reason="vetro incrinato")
            assert altro_db.get_tag(TID)["state"] == TagState.VOIDED.value


def test_lo_storico_dell_assegnazione_resta_dopo_l_annullamento() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        db.register_tag(TID)
        db.assign_tag(TID, container_id, "0100A5000064010100AABB")
        db.void_container(container_id, reason="contenitore rotto")

        storico = db.assignment_history(TID)
        assert len(storico) == 1
        assert storico[0]["released_at"] is not None
        assert storico[0]["release_reason"] == "contenitore rotto"
        assert storico[0]["epc"] == "0100A5000064010100AABB"


def test_tag_spedito_marcato_come_partito() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        db.register_tag(TID)
        db.assign_tag(TID, container_id, "0100A5000064010100AABB")
        db.mark_tag_shipped(TID)
        assert db.get_tag(TID)["state"] == TagState.SHIPPED.value


# --------------------------------------------------------------------------
# Salute del tag
# --------------------------------------------------------------------------
def test_guasti_ripetuti_mettono_il_tag_in_quarantena() -> None:
    with LimsDatabase() as db:
        db.register_tag(TID)
        for _ in range(2):
            db.record_tag_failure(TID)
        assert db.get_tag(TID)["state"] == TagState.FREE.value, "due guasti non bastano"

        db.record_tag_failure(TID)
        assert db.get_tag(TID)["state"] == TagState.QUARANTINE.value


def test_un_tag_in_quarantena_non_viene_assegnato() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        db.register_tag(TID)
        db.set_tag_state(TID, TagState.QUARANTINE)
        _expect(
            LimsDatabaseError,
            lambda: db.assign_tag(TID, container_id, "0100A5000064010100AABB"),
            "assegnazione di un tag in quarantena",
        )


def test_un_operazione_riuscita_azzera_i_guasti() -> None:
    with LimsDatabase() as db:
        db.register_tag(TID)
        db.record_tag_failure(TID)
        db.clear_tag_failures(TID)
        assert db.get_tag(TID)["failure_count"] == 0


def test_i_guasti_di_un_tag_in_quarantena_non_si_azzerano_da_soli() -> None:
    # Uscire dalla quarantena deve essere una decisione, non un effetto collaterale.
    with LimsDatabase() as db:
        db.register_tag(TID)
        for _ in range(3):
            db.record_tag_failure(TID)
        db.clear_tag_failures(TID)
        assert db.get_tag(TID)["failure_count"] == 3
        assert db.get_tag(TID)["state"] == TagState.QUARANTINE.value


# --------------------------------------------------------------------------
# Rimessa in circolo sul tag fisico
# --------------------------------------------------------------------------
def _payload(cf: str) -> TagPayload:
    return TagPayload(
        codice_fiscale=cf,
        accession_id=100,
        container_index=1,
        container_total=1,
        display_name="ROSSI MARIO",
        data_prelievo=date(2026, 8, 15),
        material_code=1,
        flags=SpecimenFlags.NONE,
    )


def test_neutralizzare_un_annullato_cancella_i_dati_del_paziente() -> None:
    """Un contenitore rotto finisce nei rifiuti: non deve portarsi via dati leggibili."""
    backend = FakeTagBackend(
        [SimulatedTag(VERGINE, tid=bytes.fromhex(TID), user_bytes=64)]
    )
    tagio = TagIO(backend, Keyring({0: CHIAVE}), lab_id=0x00A5, antennas=(1, 2))

    scritto = tagio.provision(_payload(CF_A))
    assert scritto.ok is True, scritto.error
    assert any(backend.only_tag.user), "dopo la scrittura la memoria contiene qualcosa"

    rilascio = tagio.wipe_voided(expected_epc=scritto.epc, tid=scritto.tid)
    assert rilascio.ok is True, rilascio.error
    assert not any(backend.only_tag.user), "la USER memory deve restare a zero"
    assert backend.only_tag.epc == bytes(12), "l'EPC deve tornare neutro"


def test_la_neutralizzazione_sblocca_prima_di_cancellare() -> None:
    backend = FakeTagBackend(
        [SimulatedTag(VERGINE, tid=bytes.fromhex(TID), user_bytes=64)]
    )
    tagio = TagIO(backend, Keyring({0: CHIAVE}), lab_id=0x00A5, antennas=(1, 2))
    scritto = tagio.provision(_payload(CF_A))
    tagio.lock_container(expected_epc=scritto.epc)
    assert backend.only_tag.locked == {"epc": "lock", "user": "lock"}

    rilascio = tagio.wipe_voided(expected_epc=scritto.epc, tid=scritto.tid)
    assert rilascio.ok is True, rilascio.error
    assert backend.only_tag.locked == {"epc": "unlock", "user": "unlock"}


def test_provision_registra_il_chip_e_impedisce_la_riscrittura() -> None:
    """Scrittura unica applicata sul campo, non solo in archivio."""
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        backend = FakeTagBackend(
            [SimulatedTag(VERGINE, tid=bytes.fromhex(TID), user_bytes=64)]
        )
        tagio = TagIO(
            backend, Keyring({0: CHIAVE}), lab_id=0x00A5, antennas=(1, 2), db=db
        )
        scritto = tagio.provision(_payload(CF_A), container_id=container_id)
        assert scritto.ok is True, scritto.error
        assert db.get_tag(scritto.tid)["state"] == TagState.ASSIGNED.value

        # Un secondo tentativo sullo stesso chip deve essere rifiutato prima di
        # toccare il tag.
        secondo = _contenitore(db, CF_B, 200)
        scritture = backend.only_tag.write_count
        rifiutato = tagio.provision(_payload(CF_B), container_id=secondo)
        assert rifiutato.ok is False
        assert "gia' scritto" in rifiutato.error
        assert backend.only_tag.write_count == scritture, "il tag non doveva essere toccato"


def test_riscrittura_autorizzata_lascia_traccia() -> None:
    """La deroga esiste per l'assistenza, ma non deve essere silenziosa."""
    with LimsDatabase() as db:
        container_id = _contenitore(db, CF_A, 100)
        backend = FakeTagBackend(
            [SimulatedTag(VERGINE, tid=bytes.fromhex(TID), user_bytes=64)]
        )
        tagio = TagIO(
            backend, Keyring({0: CHIAVE}), lab_id=0x00A5, antennas=(1, 2), db=db,
            operator="assistenza",
        )
        primo = tagio.provision(_payload(CF_A), container_id=container_id)
        assert primo.ok is True, primo.error

        db.void_container(container_id, reason="prova di collaudo")
        secondo_contenitore = _contenitore(db, CF_B, 200)
        forzato = tagio.provision(
            _payload(CF_B), container_id=secondo_contenitore, authorized_rewrite=True
        )
        assert forzato.ok is True, forzato.error
        assert any("riscrittura autorizzata" in passo for passo in forzato.steps)

        operazioni = [evento["operation"] for evento in db.recent_events(20)]
        assert "authorized_rewrite" in operazioni, "la deroga deve restare nella traccia"


def test_uno_sblocco_permanente_e_rifiutato() -> None:
    # Sarebbe irreversibile nel senso opposto: la banca non tornerebbe piu'
    # proteggibile.
    backend = FakeTagBackend([SimulatedTag(VERGINE, user_bytes=64)])
    tagio = TagIO(backend, Keyring({0: CHIAVE}))
    _expect(
        ValueError,
        lambda: tagio.lock_container(expected_epc="AABB", unlock=True, permanent=True),
        "sblocco permanente",
    )


# --------------------------------------------------------------------------
# Variazione del numero di contenitori in corso d'opera
# --------------------------------------------------------------------------
def _reperto(db: LimsDatabase, totale: int) -> tuple[int, list[int]]:
    patient_id = db.upsert_patient(Patient(codice_fiscale=CF_A, cognome="Rossi", nome="Mario"))
    case_id = db.create_case(Case(accession_id=100, patient_id=patient_id))
    specimen_id = db.add_specimen(Specimen(case_id=case_id, material_code=1))
    return specimen_id, db.plan_containers(specimen_id, totale)


def test_aumentare_i_contenitori_prima_di_scrivere() -> None:
    # Registrati 3, contandoli fisicamente sono 4.
    with LimsDatabase() as db:
        specimen_id, _ = _reperto(db, 3)
        esito = db.adjust_container_count(specimen_id, 4)

        assert esito.new_total == 4
        assert len(esito.added) == 1 and esito.removed == ()
        assert esito.has_stale_tags is False
        attivi = db.active_containers_for_accession(100)
        assert [r.label for r in attivi] == ["1/4", "2/4", "3/4", "4/4"]


def test_ridurre_i_contenitori_prima_di_scrivere() -> None:
    with LimsDatabase() as db:
        specimen_id, _ = _reperto(db, 4)
        esito = db.adjust_container_count(specimen_id, 2)

        assert len(esito.removed) == 2
        attivi = db.active_containers_for_accession(100)
        assert [r.label for r in attivi] == ["1/2", "2/2"]


def test_i_tag_gia_scritti_conservano_il_vecchio_totale_e_vengono_segnalati() -> None:
    """Il totale sta dentro il chip, e il chip non si riscrive."""
    with LimsDatabase() as db:
        specimen_id, contenitori = _reperto(db, 3)
        db.register_tag(TID)
        db.assign_tag(TID, contenitori[0], "0100A5000064010300AABB")

        esito = db.adjust_container_count(specimen_id, 4)

        assert esito.has_stale_tags is True
        assert esito.stale == ((contenitori[0], 1, 3),)
        assert "1/3" in esito.describe()["tag_con_totale_superato"][0]

        # I non scritti si allineano, lo scritto no.
        righe = {
            r["idx"]: r["total"]
            for r in db.connection.execute(
                "SELECT idx, total FROM containers WHERE specimen_id=? AND state<>'voided'",
                (specimen_id,),
            )
        }
        assert righe[1] == 3, "il contenitore gia' scritto porta il totale del chip"
        assert righe[2] == righe[3] == righe[4] == 4


def test_non_si_scende_sotto_i_contenitori_gia_scritti() -> None:
    # Quei campioni esistono: vanno annullati uno per uno con una motivazione,
    # non fatti sparire abbassando un conteggio.
    with LimsDatabase() as db:
        specimen_id, contenitori = _reperto(db, 3)
        db.register_tag(TID)
        db.assign_tag(TID, contenitori[2], "0100A5000064030300AABB")

        _expect(
            LimsDatabaseError,
            lambda: db.adjust_container_count(specimen_id, 2),
            "riduzione sotto un contenitore gia' scritto",
        )


def test_variazione_valida_gli_estremi() -> None:
    with LimsDatabase() as db:
        specimen_id, _ = _reperto(db, 2)
        _expect(ValueError, lambda: db.adjust_container_count(specimen_id, 0), "zero contenitori")
        _expect(ValueError, lambda: db.adjust_container_count(specimen_id, 256), "oltre 255")
        _expect(
            LimsDatabaseError,
            lambda: db.adjust_container_count(9999, 2),
            "reperto inesistente",
        )


def test_variazione_ripetuta_e_stabile() -> None:
    with LimsDatabase() as db:
        specimen_id, _ = _reperto(db, 3)
        db.adjust_container_count(specimen_id, 5)
        db.adjust_container_count(specimen_id, 2)
        esito = db.adjust_container_count(specimen_id, 3)
        attivi = db.active_containers_for_accession(100)
        assert [r.label for r in attivi] == ["1/3", "2/3", "3/3"]
        assert esito.new_total == 3


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
