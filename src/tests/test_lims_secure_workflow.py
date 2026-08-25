"""Percorso completo della distinta v2, dal mittente al destinatario."""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID
from fake_backend import FakeTagBackend

from lims.model import Case, Patient, Shipment, ShipmentState, Specimen
from lims.pec import PecSendResult
from webui.workflow import Workflow, WorkflowError


def _materiale(tmp: Path) -> dict[str, Path]:
    adesso = dt.datetime.now(dt.timezone.utc)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CA circuito RFID")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(adesso - dt.timedelta(days=1))
        .not_valid_after(adesso + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, algorithm=None)
    )
    station_key = ed25519.Ed25519PrivateKey.generate()
    station_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "UNITA-A")])
    station_cert = (
        x509.CertificateBuilder()
        .subject_name(station_name)
        .issuer_name(ca_name)
        .public_key(station_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(adesso - dt.timedelta(days=1))
        .not_valid_after(adesso + dt.timedelta(days=365))
        .sign(ca_key, algorithm=None)
    )
    recipient_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    recipient_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OSPEDALE-1")])
    recipient_cert = (
        x509.CertificateBuilder()
        .subject_name(recipient_name)
        .issuer_name(ca_name)
        .public_key(recipient_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(adesso - dt.timedelta(days=1))
        .not_valid_after(adesso + dt.timedelta(days=365))
        .sign(ca_key, algorithm=None)
    )
    percorsi = {
        "ca": tmp / "ca.pem",
        "station_cert": tmp / "station.pem",
        "station_key": tmp / "station-private.pem",
        "recipient_cert": tmp / "recipient.pem",
        "recipient_key": tmp / "recipient-private.pem",
    }
    percorsi["ca"].write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    percorsi["station_cert"].write_bytes(
        station_cert.public_bytes(serialization.Encoding.PEM)
    )
    percorsi["station_key"].write_bytes(
        station_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    percorsi["recipient_cert"].write_bytes(
        recipient_cert.public_bytes(serialization.Encoding.PEM)
    )
    percorsi["recipient_key"].write_bytes(
        recipient_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return percorsi


def _base_config(tmp: Path) -> dict:
    return {
        "reader": {"region": 0x08},
        "antennas": [{"id": 1, "read_power": 2000, "write_power": 2000}],
        "tag_access": {},
        "lims": {
            "lab_id": 1,
            "database": str(tmp / "lims.db"),
            "keyring": str(tmp / "keys.json"),
            "manifest_archive": str(tmp / "archivio"),
            "read_antennas": [1],
            "write_antennas": [1],
        },
    }


class _PecFinta:
    def send_manifest(self, **dati):
        assert dati["blob"]
        return PecSendResult("<distinta@pec>", b"messaggio PEC integrale")


def test_percorso_v2_blocca_la_partenza_fino_alla_consegna_e_si_importa() -> None:
    with tempfile.TemporaryDirectory() as cartella:
        tmp = Path(cartella)
        chiavi = _materiale(tmp)
        config = _base_config(tmp)
        config.update(
            {
                "laboratorio": {"nome": "Unita A", "codice": "UNITA-A"},
                "destinatari": [
                    {
                        "nome": "Ospedale 1",
                        "codice": "OSPEDALE-1",
                        "pec": "ospedale@pec.example",
                        "encryption_certificate": str(chiavi["recipient_cert"]),
                    }
                ],
                "security": {
                    "station_certificate": str(chiavi["station_cert"]),
                    "station_private_key": str(chiavi["station_key"]),
                },
                "pec": {
                    "enabled": True,
                    "sender": "unita-a@pec.example",
                    "username": "unita-a@pec.example",
                    "smtp_host": "smtp.pec.example",
                },
            }
        )
        mittente = Workflow(config, FakeTagBackend([]), config_path=tmp / "mittente.yaml")
        mittente.imposta_operatore("TEST")
        try:
            paziente = mittente.db.upsert_patient(
                Patient(
                    codice_fiscale="MRTMTT25D09F205Z",
                    cognome="Rossi",
                    nome="Mario",
                )
            )
            caso = mittente.db.create_case(
                Case(accession_id=123456, patient_id=paziente, data_prelievo=date.today())
            )
            reperto = mittente.db.add_specimen(Specimen(case_id=caso, material_code=1))
            contenitore = mittente.db.plan_containers(reperto, 1)[0]
            epc = "0100010001E2400001010001"
            mittente.db.assign_epc(contenitore, epc)
            mittente.db.mark_provisioned(contenitore, "E28011902000500000000001")
            mittente.shipment_id = mittente.db.create_shipment(
                Shipment(destinazione="Ospedale 1", data=date.today())
            )
            mittente.db.add_to_shipment(mittente.shipment_id, [contenitore])
            sigillo = {
                "ok": True,
                "expected": [epc],
                "found": [epc],
                "missing": [],
                "unexpected": [],
            }
            mittente.db.record_shipment_sealing(
                mittente.shipment_id, ok=True, operator="TEST", record=sigillo
            )
            mittente.db.set_shipment_state(mittente.shipment_id, ShipmentState.SEALED)
            mittente._pec_transport = lambda: _PecFinta()  # type: ignore[method-assign]

            stato = mittente.invia_distinta_pec()
            assert stato["consegna_pec"]["stato"] == "smtp_accepted"
            blob, _ = mittente.esporta_distinta()
            assert blob == bytes(mittente.db.outbound_manifest(mittente.shipment_id)["encrypted_blob"])
            try:
                mittente.conferma_invio()
            except WorkflowError:
                pass
            else:
                raise AssertionError("la partenza senza consegna PEC e' stata autorizzata")

            uscita = mittente.db.outbound_manifest(mittente.shipment_id)
            mittente.db.record_pec_receipt(
                uscita["id"],
                receipt_type="avvenuta-consegna",
                message_id="<distinta@pec>",
                raw_eml=b"ricevuta integrale",
            )
            assert mittente.conferma_invio()["stato"] == "sent"
        finally:
            mittente.chiudi()

        ricezione_tmp = tmp / "ricezione"
        config_ricezione = _base_config(ricezione_tmp)
        config_ricezione.update(
            {
                "laboratorio": {"nome": "Ospedale 1", "codice": "OSPEDALE-1"},
                "security": {
                    "recipient_certificate": str(chiavi["recipient_cert"]),
                    "recipient_private_key": str(chiavi["recipient_key"]),
                    "trusted_ca": str(chiavi["ca"]),
                },
            }
        )
        destinatario = Workflow(
            config_ricezione,
            FakeTagBackend([]),
            config_path=tmp / "destinatario.yaml",
        )
        destinatario.imposta_operatore("RICEVENTE")
        try:
            importata = destinatario.importa_distinta(blob)
            assert importata["verifica_documento"]["ok"] is True
            assert importata["verifica_documento"]["mittente"] == "UNITA-A"
            assert importata["contenitori"][0]["paziente"] == "ROSSI MARIO"
        finally:
            destinatario.chiudi()

