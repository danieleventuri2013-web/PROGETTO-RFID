"""Prove della distinta v2 firmata e cifrata per il destinatario."""

from __future__ import annotations

import datetime as dt
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.x509.oid import NameOID

from lims.crypto import PayloadAuthenticationError, UnknownKeyError
from lims.manifest import MANIFEST_SCHEMA_V2, Manifest, ManifestEntry
from lims.secure_manifest import open_secure_manifest, seal_secure_manifest


def _certificati(codice: str = "UNITA-A"):
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
    station_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, codice)])
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
    return ca_cert, station_key, station_cert, recipient_key, recipient_cert


def _distinta() -> Manifest:
    identificativo = str(uuid.uuid4())
    return Manifest(
        schema=MANIFEST_SCHEMA_V2,
        shipment_id=71,
        destination="Ospedale centrale",
        manifest_uuid=identificativo,
        source_code="UNITA-A",
        destination_code="OSPEDALE-1",
        entries=[
            ManifestEntry(
                epc="0100A5000F12060101DEADB1",
                tid="E28011902000500000000001",
                accession_id=987654,
                container_index=1,
                container_total=1,
                codice_fiscale="MRTMTT25D09F205Z",
                display_name="ROSSI MARIO",
            )
        ],
    )


def _busta(codice: str = "UNITA-A"):
    ca, station_key, station_cert, recipient_key, recipient_cert = _certificati(codice)
    distinta = _distinta()
    blob = seal_secure_manifest(
        distinta,
        manifest_uuid=distinta.manifest_uuid,
        source_code="UNITA-A",
        destination_code="OSPEDALE-1",
        recipient_certificate=recipient_cert,
        signing_key=station_key,
        signer_certificate=station_cert,
    )
    return blob, ca, recipient_key, recipient_cert


def test_giro_completo_v2_e_dati_sanitari_non_in_chiaro() -> None:
    blob, ca, recipient_key, recipient_cert = _busta()
    assert b"MRTMTT25D09F205Z" not in blob
    assert b"ROSSI MARIO" not in blob

    verificata = open_secure_manifest(
        blob,
        recipient_private_key=recipient_key,
        recipient_certificate=recipient_cert,
        trusted_cas=[ca],
        expected_destination_code="OSPEDALE-1",
    )
    assert verificata.manifest.entries[0].tid == "E28011902000500000000001"
    assert verificata.header.item_count == 1
    assert len(verificata.sha256) == 64


def test_manomissione_firma_e_destinazione_sono_rilevate() -> None:
    blob, ca, recipient_key, recipient_cert = _busta()
    alterata = bytearray(blob)
    alterata[-1] ^= 1
    try:
        open_secure_manifest(
            bytes(alterata),
            recipient_private_key=recipient_key,
            recipient_certificate=recipient_cert,
            trusted_cas=[ca],
        )
    except PayloadAuthenticationError:
        pass
    else:
        raise AssertionError("la distinta alterata e' stata accettata")

    try:
        open_secure_manifest(
            blob,
            recipient_private_key=recipient_key,
            recipient_certificate=recipient_cert,
            trusted_cas=[ca],
            expected_destination_code="OSPEDALE-2",
        )
    except PayloadAuthenticationError:
        pass
    else:
        raise AssertionError("la distinta destinata ad altro ospedale e' stata accettata")


def test_certificato_sbagliato_e_identita_mittente_non_coerente_falliscono() -> None:
    blob, ca, recipient_key, _ = _busta()
    _, _, _, _, altro_recipient = _certificati()
    try:
        open_secure_manifest(
            blob,
            recipient_private_key=recipient_key,
            recipient_certificate=altro_recipient,
            trusted_cas=[ca],
        )
    except UnknownKeyError:
        pass
    else:
        raise AssertionError("il certificato destinatario sbagliato e' stato accettato")

    try:
        _busta("UNITA-DIVERSA")
    except PayloadAuthenticationError:
        pass
    else:
        raise AssertionError("identita' incoerente accettata durante la firma")


def test_seriale_revocato_e_ca_non_attendibile_falliscono() -> None:
    blob, ca, recipient_key, recipient_cert = _busta()
    verificata = open_secure_manifest(
        blob,
        recipient_private_key=recipient_key,
        recipient_certificate=recipient_cert,
        trusted_cas=[ca],
    )
    try:
        open_secure_manifest(
            blob,
            recipient_private_key=recipient_key,
            recipient_certificate=recipient_cert,
            trusted_cas=[ca],
            revoked_serials=[verificata.signer_certificate.serial_number],
        )
    except PayloadAuthenticationError:
        pass
    else:
        raise AssertionError("un certificato revocato e' stato accettato")

    altra_ca, *_ = _certificati("ALTRA-UNITA")
    try:
        open_secure_manifest(
            blob,
            recipient_private_key=recipient_key,
            recipient_certificate=recipient_cert,
            trusted_cas=[altra_ca],
        )
    except PayloadAuthenticationError:
        pass
    else:
        raise AssertionError("una CA non attendibile e' stata accettata")
