"""Colli multipli, allegati immutabili, ricezione e SMTP simulato, senza invii reali."""
from __future__ import annotations

import base64
import io
import os
import smtplib
import sqlite3
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import date
from email import policy
from email.parser import BytesParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend
from test_lims_secure_workflow import _base_config

from lims import mail_dispatch as mail
from lims.codec import build_box_epc
from lims.db import _MIGRATIONS, LimsDatabase
from lims.model import Case, Patient, Shipment, ShipmentState, Specimen
from lims.tagio import FieldSurvey, TagObservation
from webui.workflow import Workflow, WorkflowError


@contextmanager
def _pair():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config = _base_config(root)
        config["laboratorio"] = {"nome": "Presidio A"}
        config["destinatari"] = [{"nome": "Distretto B", "email": "ricezione@example.test"}]
        config["email"] = {"sender": "magazzino@example.test"}
        src = Workflow(config, FakeTagBackend([]), config_path=root / "config.yaml")
        src.imposta_operatore("TEST")
        other = _base_config(root / "ricevente")
        other["laboratorio"] = {"nome": "Distretto B"}
        dst = Workflow(other, FakeTagBackend([]))
        dst.keyring = src.keyring
        dst.imposta_operatore("TEST")
        try:
            yield src, dst
        finally:
            src.chiudi()
            dst.chiudi()


def _collo(w, n, box=None):
    pid = w.db.upsert_patient(Patient(codice_fiscale="", cognome="COGNOMESEGRETO", nome="NOMESEGRETO"))
    case = w.db.create_case(Case(accession_id=n, patient_id=pid, data_prelievo=date.today()))
    specimen = w.db.add_specimen(Specimen(case_id=case, material_code=1))
    cid = w.db.plan_containers(specimen, 1)[0]
    epc = f"010001{n:08X}0101AABBCC"
    w.db.assign_epc(cid, epc)
    w.db.mark_provisioned(cid, f"E28011902000500000{n:06X}")
    sid = w.db.create_shipment(Shipment(destinazione="Distretto B", data=date.today()))
    w.db.add_to_shipment(sid, [cid])
    box = box or build_box_epc(1, n, random_suffix=b"\x01\x02\x03\x04").hex().upper()
    w.db.record_shipment_sealing(sid, ok=True, operator="TEST",
        record={"ok": True, "expected": [epc], "found": [epc], "missing": [], "unexpected": [], "box_epc": box})
    w.db.set_shipment_state(sid, ShipmentState.SEALED)
    return sid, epc, box


def _expect(fn, text=""):
    try:
        fn()
    except (WorkflowError, mail.MailError) as exc:
        assert text in str(exc), str(exc)
    else:
        raise AssertionError("operazione non rifiutata")


def _import_zip(src, dst, dispatch):
    file = src.file_email(dispatch["id"], "zip")
    return dst.importa_distinte([file])


def _survey(w, epcs, error=""):
    data = FieldSurvey(observations=[TagObservation(epc=e) for e in epcs], error=error)
    w._tagio = lambda **kw: SimpleNamespace(survey_field=lambda **args: data)


def test_email_due_colli_allegati_immutabili_e_senza_dati_nel_testo():
    with _pair() as (src, dst):
        a, b = _collo(src, 1), _collo(src, 2)
        src.shipment_id = a[0]
        batch = src.prepara_invio_email([a[0], b[0]])
        assert src.shipment_id == a[0]
        assert len(batch["allegati"]) == 2
        assert "SEGRETO" not in batch["body"] + batch["subject"]
        output = src.file_email(batch["id"])
        msg = BytesParser(policy=policy.default).parsebytes(base64.b64decode(output["contenuto_base64"]))
        assert msg["From"] == "magazzino@example.test"
        for sid, attachment in zip((a[0], b[0]), msg.iter_attachments(), strict=True):
            stored = src.db.outbound_manifest(sid)
            assert attachment.get_payload(decode=True) == bytes(stored["encrypted_blob"])
            assert attachment.get_filename() == stored["filename"]
        again = src.prepara_invio_email([a[0], b[0]])
        assert [x["nome"] for x in again["allegati"]] == [x["nome"] for x in batch["allegati"]]
        assert src.bozza_email()["nome_file"] == batch["allegati"][0]["nome"]


def test_importazione_cumulativa_duplicati_selezione_e_collo_estraneo():
    with _pair() as (src, dst):
        a, b = _collo(src, 1), _collo(src, 2)
        batch = src.prepara_invio_email([a[0], b[0]])
        imported = _import_zip(src, dst, batch)
        assert len(imported["distinte"]) == 2
        assert dst.inbound_id is None
        assert all(r["esito"] == "già presente" for r in _import_zip(src, dst, batch)["risultati"])
        _survey(dst, [b[1], b[2]])
        result = dst.riconosci_collo()
        assert result["riconciliazione"]["ok"]
        assert result["distinta"]["box_epc"] == b[2]
        active = dst.inbound_id
        _import_zip(src, dst, batch)
        assert dst.inbound_id == active
        _survey(dst, [a[1], a[2]])
        _expect(dst.leggi_volume, "non corrisponde")
        _expect(dst.conferma_ricezione, "nuova lettura")
        _survey(dst, [b[1], b[2]])
        dst.leggi_volume()
        dst.conferma_ricezione()
        _survey(dst, [b[1], b[2]])
        _expect(dst.riconosci_collo, "nessuna distinta")
        _survey(dst, [a[1], a[2]])
        assert dst.riconosci_collo()["riconciliazione"]["ok"]


def test_letture_ambigue_non_scelgono_la_distinta():
    with _pair() as (src, dst):
        a, b = _collo(src, 1), _collo(src, 2)
        _import_zip(src, dst, src.prepara_invio_email([a[0], b[0]]))
        _survey(dst, [a[2], b[2]])
        _expect(dst.riconosci_collo, "più colli")
        assert dst.inbound_id is None
        _survey(dst, [a[1]])
        _expect(dst.riconosci_collo, "nessun tag")
        c = _collo(src, 3, box=a[2])
        _import_zip(src, dst, src.prepara_invio_email([c[0]]))
        _survey(dst, [a[2]])
        _expect(dst.riconosci_collo, "più distinte")


def test_errore_radio_non_diventa_riconciliazione_valida():
    with _pair() as (src, dst):
        a = _collo(src, 1)
        _import_zip(src, dst, src.prepara_invio_email([a[0]]))
        _survey(dst, [a[1], a[2]], "timeout")
        _expect(dst.riconosci_collo, "non riuscita")
        assert not dst.db.connection.execute("SELECT * FROM inbound_reconciliations").fetchall()


def test_destinazioni_diverse_e_casella_assente_bloccano_preparazione():
    with _pair() as (src, dst):
        a, b = _collo(src, 1), _collo(src, 2)
        with src.db.connection:
            src.db.connection.execute("UPDATE shipments SET destinazione='Altro' WHERE id=?", (b[0],))
        _expect(lambda: src.prepara_invio_email([a[0], b[0]]), "stesso destinatario")
        src.config["email"] = {}
        _expect(lambda: src.prepara_invio_email([a[0]]), "indirizzo email")


def test_nuova_lettura_fallita_invalida_il_confronto_anche_dopo_riavvio():
    with _pair() as (src, dst):
        a = _collo(src, 1)
        _import_zip(src, dst, src.prepara_invio_email([a[0]]))
        _survey(dst, [a[1], a[2]])
        dst.riconosci_collo()
        assert dst.stato_ricezione()["lettura_valida"]
        _survey(dst, [], "timeout")
        _expect(dst.leggi_volume, "non riuscita")
        _expect(dst.conferma_ricezione, "nuova lettura")
        config = dst.config
        # Il circuito deve mantenere la stessa chiave anche sul disco.
        dst.keyring.save(config["lims"]["keyring"])
        dst.chiudi()
        restored = Workflow(config, FakeTagBackend([]))
        try:
            restored.imposta_operatore("TEST")
            assert restored.inbound_id == dst.inbound_id
            assert not restored.stato_ricezione()["lettura_valida"]
            _expect(restored.conferma_ricezione, "nuova lettura")
        finally:
            restored.chiudi()


def test_file_corrotto_non_blocca_gli_altri_e_sede_errata_rifiutata():
    with _pair() as (src, dst):
        a = _collo(src, 1)
        batch = src.prepara_invio_email([a[0]])
        files = [src.file_email(batch["id"], "zip"), {"nome": "rotto.rfidman", "contenuto_base64": "cm90dG8="}]
        result = dst.importa_distinte(files)
        assert {r["esito"] for r in result["risultati"]} == {"importata", "rifiutata"}
        dst.config["laboratorio"]["nome"] = "Altro"
        assert _import_zip(src, dst, batch)["risultati"][0]["esito"] == "rifiutata"


def test_zip_senza_estrazione_e_con_limiti():
    for name, content in [("../fuori.rfidman", b"x"), ("segreto.txt", b"x"), ("grande.rfidman", b"x" * (mail.MAX_FILE_BYTES + 1))]:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(name, content)
        _expect(lambda stream=stream: mail.unpack_files("invio.zip", stream.getvalue()))


def test_esito_smtp_incerto_impedisce_il_reinvio():
    with _pair() as (src, dst):
        a = _collo(src, 1)
        batch = src.prepara_invio_email([a[0]])
        with patch.object(mail, "send_message", side_effect=mail.SendUncertain("esito incerto")) as send:
            _expect(lambda: src.invia_email(batch["id"]), "incerto")
            _expect(lambda: src.invia_email(batch["id"]), "incerto")
            assert send.call_count == 1
        assert src.dettaglio_email(batch["id"])["state"] == "send_unknown"


def test_preparazione_senza_invio_e_accettazione_smtp_separata_dalla_partenza():
    with _pair() as (src, dst):
        a = _collo(src, 1)
        with patch.object(mail, "send_message") as send:
            batch = src.prepara_invio_email([a[0]])
            assert not send.called
            assert src.invia_email(batch["id"])["state"] == "smtp_accepted"
            _expect(lambda: src.invia_email(batch["id"]))
            assert send.call_count == 1
        assert src.db.shipment_row(a[0])["state"] == "exported"


def test_smtp_tls_password_fuori_yaml_e_quit_dopo_accettazione():
    order = []
    class SMTP:
        def __init__(self, *a, **kw): order.append("connect")
        def ehlo(self): order.append("ehlo")
        def starttls(self, **kw): order.append("tls")
        def login(self, user, password):
            assert password == "password-finta"
            order.append("login")
        def sendmail(self, sender, recipients, raw):
            assert b"X-Unsent" not in raw
            assert recipients == ["ricezione@example.test"]
            order.append("send")
            return {}
        def quit(self): raise OSError("rete chiusa dopo accettazione")
        def close(self): pass
    cfg = {"enabled": True, "sender": "magazzino@example.test", "username": "magazzino@example.test"}
    raw, _ = mail.build_message(cfg["sender"], "ricezione@example.test", "Oggetto", "Testo", [("a.rfidman", b"cifrato")])
    with patch.dict(os.environ, {"RFID_MAIL_PASSWORD": "password-finta"}), patch.object(smtplib, "SMTP", SMTP):
        mail.send_message(cfg, raw, cfg["sender"], "ricezione@example.test")
    assert order.index("tls") < order.index("login") < order.index("send")


def test_migrazione_nove_rollback_e_riapertura():
    db = LimsDatabase.__new__(LimsDatabase)
    db._conn = sqlite3.connect(":memory:")
    original = _MIGRATIONS[9]
    try:
        for version in range(1, 9):
            migration = _MIGRATIONS[version]
            if callable(migration):
                migration(db.connection)
            else:
                db.connection.executescript(migration)
            db.connection.execute(f"PRAGMA user_version={version}")
            db.connection.commit()
        db.connection.execute("INSERT INTO inbound_shipments (origin_lab_id,origin_shipment_id,manifest_hash,encrypted_blob,imported_at) VALUES (1,42,'hash',X'1234','oggi')")
        db.connection.commit()
        _MIGRATIONS[9] = original.replace("CREATE TABLE mail_dispatches", "ERRORE CREATE TABLE mail_dispatches")
        try:
            db._migrate()
            raise AssertionError("migrazione errata accettata")
        except sqlite3.OperationalError:
            pass
        assert db.schema_version == 8
        assert "box_epc" not in [r[1] for r in db.connection.execute("PRAGMA table_info(inbound_shipments)")]
        _MIGRATIONS[9] = original
        db._migrate()
        assert db.schema_version == 10
        assert db.connection.execute("SELECT encrypted_blob FROM inbound_shipments").fetchone()[0] == bytes.fromhex("1234")
        db._migrate()
    finally:
        _MIGRATIONS[9] = original
        db.connection.close()


def _run_all():
    import logging
    import traceback
    logging.disable(logging.CRITICAL)
    failed = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            print("FAIL", fn.__name__)
            traceback.print_exc()
    print(f"{len(tests)-failed}/{len(tests)} test superati")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(_run_all())
