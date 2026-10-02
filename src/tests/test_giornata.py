"""La giornata operativa, esercitata senza lettore fisico."""

from __future__ import annotations

import copy
import datetime as dt
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_operativita import _server
from test_strumenti import CF_UNO, _tag

from lims.acceptance import data_italiana, giorno_italiano
from lims.codec import TagPayload, build_epc, pack_payload, parse_epc, unpack_payload
from lims.db import _MIGRATIONS, LimsDatabase, LimsDatabaseError
from lims.manifest import build_manifest, open_manifest, seal_manifest
from lims.model import Patient
from webui.server import WebUIServer


def salva(s, **extra):
    dati = {
        "nome": "Anna",
        "cognome": "Rossi",
        "reperti": [{"descrizione": "Biopsia", "contenitori": 1}],
        **extra,
    }
    status, r = s.call("salva_bozza", dati)
    assert status == 200, r
    return r


def test_omonimi_senza_cf_restano_distinti_e_salvare_non_usa_radio():
    s = _server()
    try:
        s.backend.calls.clear()
        a, b = salva(s, reperti=[]), salva(s)
        assert a["patient_id"] != b["patient_id"]
        assert s.backend.calls == []
        _, g = s.call("accettazioni_giorno", {})
        assert len(g["pazienti"]) == 2
        assert g["contatori"] == {
            "pazienti": 2,
            "campioni": 1,
            "contenitori": 1,
            "verificati": 0,
            "da_scrivere": 1,
        }
        assert a["accettazioni"][0]["stato"] == "da_preparare"
    finally:
        s.shutdown()


def test_cf_duplicato_non_modifica_anagrafica_o_crea_lotti():
    s = _server()
    try:
        a = salva(s, codice_fiscale=CF_UNO)
        status, errore = s.call(
            "salva_bozza", {"nome": "Altro", "cognome": "Nome", "codice_fiscale": CF_UNO}
        )
        assert status == 400 and "già presente" in errore["errore"]
        assert s.workflow.db.connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
        assert s.workflow.db.get_patient(a["patient_id"]).nome == "ANNA"
    finally:
        s.shutdown()


def test_modifica_bozza_rifiuta_versione_vecchia_e_cambio_paziente():
    s = _server()
    try:
        a, b = salva(s, reperti=[]), salva(s, nome="Carlo")
        versione = a["accettazioni"][0]["edit_version"]
        dati = {
            "patient_id": a["patient_id"],
            "accession_id": a["accession_id"],
            "edit_version": versione,
        }
        salva(s, **dati)
        assert s.call("salva_bozza", {"nome": "Anna", "cognome": "Rossi", **dati})[0] == 400
        dati.update(patient_id=b["patient_id"], edit_version=versione + 1)
        assert s.call("salva_bozza", {"nome": "Anna", "cognome": "Rossi", **dati})[0] == 400
    finally:
        s.shutdown()


def test_coda_flessibile_progressi_e_ripresa_dopo_riavvio():
    s = _server(prototype_mode=True)
    altro = None
    try:
        a = salva(
            s, reperti=[{"descrizione": "Colon", "contenitori": 2}, {"descrizione": "Linfonodo"}]
        )
        b = salva(s, nome="Carlo")
        assert s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})[0] == 200
        assert not s.workflow.conteggio_confermato
        assert s.call("conferma_conteggio", {})[0] == 200
        _, scritto = s.call("scrivi", {"accession_id": a["accession_id"]})
        assert scritto["scrittura"]["ok"], scritto
        assert s.call("seleziona_accettazione", {"accession_id": b["accession_id"]})[0] == 200
        assert s.call("scrivi", {"accession_id": a["accession_id"]})[0] == 400
        assert s.call("conferma_conteggio", {"accession_id": a["accession_id"]})[0] == 400
        assert s.call("scrivi", {"accession_id": b["accession_id"]})[1]["scrittura"]["ok"]
        altro = WebUIServer(copy.deepcopy(s.workflow.config), s.backend, host="127.0.0.1", port=0)
        altro.workflow.imposta_operatore("TEST")
        _, ripresa = altro.call("seleziona_accettazione", {"accession_id": a["accession_id"]})
        assert (
            ripresa["scritti"] == 1
            and ripresa["prossimo"]["index"] == 2
            and ripresa["conteggio_confermato"]
        )
        _, g = altro.call("accettazioni_giorno", {})
        assert g["contatori"]["verificati"] == 2
        assert {p["stato"] for p in g["pazienti"]} == {"parziale", "completato"}
    finally:
        if altro:
            altro.shutdown()
        s.shutdown()


def test_tentativo_perso_non_diventa_verificato_e_congela_lotto():
    s = _server()
    try:
        a = salva(s)
        s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})
        originale = s.backend.write_epc

        def persa(r):
            originale(r)
            return s.backend._ko("write_epc", "risposta persa")

        s.backend.write_epc = persa
        assert not s.call("scrivi", {})[1]["scrittura"]["ok"]
        _, d = s.call("dettaglio_accettazione", {"patient_id": a["patient_id"]})
        c = d["accettazioni"][0]
        assert not c["modificabile"] and c["verificati"] == 0 and c["errori"] == 1
        assert c["reperti"][0]["contenitori"][0]["epc"]
        assert s.call("correggi_conteggio", {"totale": 2})[0] == 400
        status, _ = s.call(
            "salva_bozza",
            {
                "nome": "Anna",
                "cognome": "Rossi",
                "patient_id": a["patient_id"],
                "accession_id": a["accession_id"],
                "edit_version": c["edit_version"],
            },
        )
        assert status == 400
        nuovo = salva(s, patient_id=a["patient_id"], reperti=[{"descrizione": "Nuovo campione"}])
        assert nuovo["accession_id"] != a["accession_id"]
        assert len(nuovo["accettazioni"]) == 2
    finally:
        s.shutdown()


def test_giornata_usa_registrazione_locale_e_mostra_arretrati():
    assert data_italiana("2026-03-29T22:30:00+00:00") == "2026-03-30"
    assert data_italiana("2026-10-25T22:30:00+00:00") == "2026-10-25"
    s = _server()
    try:
        a = salva(s, data_prelievo="2020-01-01", reperti=[])
        b = salva(s, nome="Carlo")
        oggi = giorno_italiano()
        ieri = (dt.date.fromisoformat(oggi) - dt.timedelta(days=1)).isoformat()
        s.workflow.db.connection.execute(
            "UPDATE cases SET created_at=? WHERE accession_id=?",
            (ieri + "T12:00:00+02:00", a["accession_id"]),
        )
        s.workflow.db.connection.commit()
        _, g = s.call("accettazioni_giorno", {"data": oggi})
        assert [p["id"] for p in g["pazienti"]] == [b["patient_id"]]
        assert [p["id"] for p in g["arretrati"]] == [a["patient_id"]]
        assert s.call("annulla_bozza", {"accession_id": a["accession_id"]})[0] == 200
        assert s.call("accettazioni_giorno", {"data": oggi})[1]["arretrati"] == []
    finally:
        s.shutdown()


def test_payload_senza_cf_v2_conserva_formato_v1_e_capacita():
    base = dict(accession_id=1, container_index=1, container_total=1, display_name="ANNA ROSSI")
    epc = parse_epc(build_epc(1, 1, 1, 1))
    for cf, versione in (("", 2), (CF_UNO, 1)):
        packed = pack_payload(TagPayload(codice_fiscale=cf, **base), max_bytes=44)
        assert packed[0] == versione and len(packed) <= 44
        assert unpack_payload(packed, epc).codice_fiscale == cf


def test_scrittura_e_lettura_user_senza_cf():
    s = _server()
    try:
        s.backend.tags = [_tag(1, user_bytes=64)]
        status, r = s.call(
            "salva_operativita",
            {
                "user_memory_bytes": 64,
                "memorie": {"read_user": True, "write_user": True, "read_tid": True},
            },
        )
        assert status == 200, r
        a = salva(s)
        s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})
        _, r = s.call("scrivi", {})
        assert r["scrittura"]["ok"], r
        ril = s.workflow._tagio(scrittura=False).survey_field()
        assert ril.ok and ril.observations[0].payload.codice_fiscale == ""
        record = s.workflow.db.get_container(r["contenitori"][0]["container_id"])
        assert record.codice_fiscale == ""
    finally:
        s.shutdown()


def test_migrazione_v7_conserva_id_collegamenti_e_ammette_cf_assente():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "archivio.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        for v in range(1, 8):
            m = _MIGRATIONS[v]
            if callable(m):
                m(conn)
            else:
                conn.executescript(m)
            conn.execute(f"PRAGMA user_version={v}")
            conn.commit()
        conn.execute(
            "INSERT INTO patients(id,codice_fiscale,cognome,nome,sesso,created_at) VALUES(7,?,'ROSSI','ANNA','X','2026-01-01')",
            (CF_UNO,),
        )
        conn.execute(
            "INSERT INTO cases(id,accession_id,patient_id,created_at) VALUES(4,99,7,'2026-01-01')"
        )
        conn.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='patients'")
        conn.commit()
        conn.close()
        with LimsDatabase(path) as db:
            assert db.get_patient(7).codice_fiscale == CF_UNO
            assert db.find_case(99).patient_id == 7
            assert not db.connection.execute("PRAGMA foreign_key_check").fetchall()
            x = db.upsert_patient(Patient("", "Rossi", "Anna"))
            y = db.upsert_patient(Patient("", "Rossi", "Anna"))
            assert x != y and x > 100


def test_mutazioni_giornata_non_si_sovrappongono_alla_radio():
    s = _server()
    try:
        s._acquire("scrivi")
        try:
            for nome in (
                "salva_bozza",
                "seleziona_accettazione",
                "sospendi_accettazione",
                "annulla_bozza",
            ):
                assert s.call(nome, {})[0] == 409
            assert s.call("accettazioni_giorno", {})[0] == 200
        finally:
            s._release()
    finally:
        s.shutdown()


def test_modificare_bozza_selezionata_invalida_solo_la_selezione():
    s = _server()
    try:
        a = salva(s)
        s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})
        s.backend.calls.clear()
        b = salva(
            s,
            patient_id=a["patient_id"],
            accession_id=a["accession_id"],
            edit_version=a["accettazioni"][0]["edit_version"],
            reperti=[
                {"descrizione": "Nuovo", "contenitori": 2, "avvertenze": ["BIOBANK", "URGENT"]}
            ],
        )
        assert s.workflow.accession_id is None and s.backend.calls == []
        assert not s.workflow.db.connection.execute("PRAGMA foreign_key_check").fetchall()
        c = b["accettazioni"][0]
        assert c["totale"] == 2 and c["reperti"][0]["flags"] == 5
        assert s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})[0] == 200
        assert not s.workflow.conteggio_confermato
    finally:
        s.shutdown()


def test_etichetta_distinta_e_ricezione_senza_cf():
    s = _server()
    try:
        a = salva(s)
        s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})
        status, r = s.call("scrivi", {})
        assert status == 200 and r["scrittura"]["ok"], r
        cid = r["contenitori"][0]["container_id"]
        status, etichetta = s.call("etichetta", {"container_id": cid})
        assert status == 200 and "ROSSI" in str(etichetta), etichetta
        spedizione = s.workflow.prepara_spedizione("Ospedale Foligno", [cid])
        manifesto = build_manifest(s.workflow.db, s.workflow.shipment_id, lab_id=1)
        assert manifesto.entries[0].codice_fiscale == ""
        blob = seal_manifest(manifesto, s.workflow.keyring)
        assert open_manifest(blob, s.workflow.keyring).entries[0].display_name == "ROSSI ANNA"
        ricevuta = s.workflow.importa_distinta(blob)
        assert ricevuta and spedizione
    finally:
        s.shutdown()


def test_dettaglio_inesistente_e_data_errata_sono_errori_operatore():
    s = _server()
    try:
        assert s.call("dettaglio_accettazione", {"patient_id": 9999})[0] == 400
        assert s.call("accettazioni_giorno", {"data": "non una data"})[0] == 400
    finally:
        s.shutdown()


def test_anagrafica_con_tag_avviati_non_cambia_dal_percorso_legacy():
    s = _server()
    try:
        a = salva(s, codice_fiscale=CF_UNO)
        s.call("seleziona_accettazione", {"accession_id": a["accession_id"]})
        assert s.call("scrivi", {})[1]["scrittura"]["ok"]
        try:
            s.workflow.db.upsert_patient(Patient(CF_UNO, "Diverso", "Nome"))
            raise AssertionError("anagrafica congelata modificabile")
        except LimsDatabaseError:
            pass
        assert s.workflow.db.get_patient(a["patient_id"]).nome == "ANNA"
    finally:
        s.shutdown()


def test_migrazione_interrotta_ritorna_integralmente_alla_versione_precedente():
    db = LimsDatabase.__new__(LimsDatabase)
    db._conn = sqlite3.connect(":memory:")
    script = _MIGRATIONS[8]
    try:
        for versione in range(1, 8):
            migrazione = _MIGRATIONS[versione]
            if callable(migrazione):
                migrazione(db.connection)
            else:
                db.connection.executescript(migrazione)
            db.connection.execute(f"PRAGMA user_version={versione}")
            db.connection.commit()
        db.connection.execute("PRAGMA foreign_keys=ON")
        # Guasto dopo la sostituzione della tabella e prima del commit.
        _MIGRATIONS[8] = script.replace("COMMIT;", "SELECT * FROM tabella_inesistente;")
        try:
            db._migrate()
            raise AssertionError("guasto di migrazione non rilevato")
        except sqlite3.OperationalError:
            pass
        assert db.schema_version == 7
        assert db.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert not db.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='patients_v8'"
        ).fetchall()
        assert "frozen_at" not in [r[1] for r in db.connection.execute("PRAGMA table_info(cases)")]
        _MIGRATIONS[8] = script
        db._migrate()
        from lims.db import SCHEMA_VERSION
        assert db.schema_version == SCHEMA_VERSION
    finally:
        _MIGRATIONS[8] = script
        db.connection.close()


def _run_all():
    import logging
    import traceback

    logging.disable(logging.CRITICAL)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print("PASS", test.__name__)
        except Exception:
            print("FAIL", test.__name__)
            traceback.print_exc()
    print(f"{passed}/{len(tests)} test superati")
    return int(passed != len(tests))


if __name__ == "__main__":
    sys.exit(_run_all())
