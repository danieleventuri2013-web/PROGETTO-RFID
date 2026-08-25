"""Busta v2 della distinta: cifratura per il destinatario e firma del mittente.

La distinta v1 usa una chiave simmetrica condivisa. Resta leggibile per
compatibilita', ma non scala bene quando molte unita' inviano a pochi ospedali:
ogni mittente finirebbe per possedere un segreto capace di aprire anche file
altrui. La v2 usa invece una chiave casuale AES-256-GCM per ogni file, avvolta
con la chiave pubblica RSA del destinatario. Il mittente firma l'intera busta
con Ed25519 e allega il proprio certificato X.509.

Il formato e' volutamente piccolo e deterministico nella struttura::

    MAGIC | 02 | header_len(4) | header_json |
    wrapped_len(2) | wrapped_key | nonce(12) |
    cipher_len(4) | ciphertext | cert_len(4) | signer_cert_der |
    signature(64)

SHA-256 non e' dentro il file, per evitare una dipendenza circolare: viene
calcolato sui byte finali, firma compresa, e conservato ai due estremi.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from cryptography import x509
from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

from .crypto import PayloadAuthenticationError, PayloadFormatError, UnknownKeyError
from .manifest import MANIFEST_MAGIC, Manifest

__all__ = [
    "MANIFEST_SCHEMA_V2",
    "SecureManifestHeader",
    "VerifiedManifest",
    "certificate_fingerprint",
    "load_certificate",
    "load_private_key",
    "open_secure_manifest",
    "peek_secure_header",
    "seal_secure_manifest",
]

MANIFEST_SCHEMA_V2 = 2
_DOMAIN = b"RFID-LIMS-manifest-v2|"
_NONCE_SIZE = 12
_SIGNATURE_SIZE = 64
_MAX_FILE_SIZE = 16 * 1024 * 1024


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _take(blob: bytes, offset: int, length: int, nome: str) -> tuple[bytes, int]:
    if length < 0 or offset + length > len(blob):
        raise PayloadFormatError(f"distinta v2 troncata nel campo {nome}")
    return blob[offset : offset + length], offset + length


def _u16(value: int) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError("campo della distinta troppo lungo")
    return value.to_bytes(2, "big")


def _u32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise ValueError("campo della distinta troppo lungo")
    return value.to_bytes(4, "big")


def certificate_fingerprint(certificato: x509.Certificate) -> str:
    """Impronta SHA-256 del certificato, in esadecimale maiuscolo."""
    return certificato.fingerprint(hashes.SHA256()).hex().upper()


def load_certificate(value: str | Path | bytes) -> x509.Certificate:
    dati = Path(value).read_bytes() if isinstance(value, (str, Path)) else bytes(value)
    try:
        return x509.load_pem_x509_certificate(dati)
    except ValueError:
        try:
            return x509.load_der_x509_certificate(dati)
        except ValueError as exc:
            raise PayloadFormatError("certificato X.509 non leggibile") from exc


def load_private_key(value: str | Path | bytes, password: str | bytes | None = None) -> Any:
    dati = Path(value).read_bytes() if isinstance(value, (str, Path)) else bytes(value)
    segreto = password.encode("utf-8") if isinstance(password, str) else password
    try:
        return serialization.load_pem_private_key(dati, password=segreto)
    except (TypeError, ValueError):
        try:
            return serialization.load_der_private_key(dati, password=segreto)
        except (TypeError, ValueError) as exc:
            raise UnknownKeyError("chiave privata non leggibile o password errata") from exc


@dataclass(frozen=True)
class SecureManifestHeader:
    manifest_uuid: str
    source_code: str
    destination_code: str
    shipment_id: int
    created_at: str
    item_count: int
    recipient_fingerprint: str
    signer_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithms": {
                "content": "A256GCM",
                "key_wrap": "RSA-OAEP-256",
                "signature": "Ed25519",
            },
            "created_at": self.created_at,
            "destination_code": self.destination_code,
            "item_count": self.item_count,
            "manifest_uuid": self.manifest_uuid,
            "recipient_fingerprint": self.recipient_fingerprint,
            "shipment_id": self.shipment_id,
            "signer_fingerprint": self.signer_fingerprint,
            "source_code": self.source_code,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SecureManifestHeader":
        algoritmi = value.get("algorithms") or {}
        attesi = {
            "content": "A256GCM",
            "key_wrap": "RSA-OAEP-256",
            "signature": "Ed25519",
        }
        if dict(algoritmi) != attesi:
            raise PayloadFormatError("algoritmi della distinta v2 non supportati")
        try:
            header = cls(
                manifest_uuid=str(value["manifest_uuid"]),
                source_code=str(value["source_code"]),
                destination_code=str(value["destination_code"]),
                shipment_id=int(value["shipment_id"]),
                created_at=str(value["created_at"]),
                item_count=int(value["item_count"]),
                recipient_fingerprint=str(value["recipient_fingerprint"]).upper(),
                signer_fingerprint=str(value["signer_fingerprint"]).upper(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PayloadFormatError("intestazione della distinta v2 incompleta") from exc
        try:
            uuid.UUID(header.manifest_uuid)
        except ValueError as exc:
            raise PayloadFormatError("identificativo UUID della distinta non valido") from exc
        if (
            not header.source_code.strip()
            or not header.destination_code.strip()
            or header.shipment_id <= 0
            or header.item_count <= 0
        ):
            raise PayloadFormatError("intestazione della distinta v2 non coerente")
        return header


@dataclass(frozen=True)
class VerifiedManifest:
    manifest: Manifest
    header: SecureManifestHeader
    sha256: str
    signer_certificate: x509.Certificate


@dataclass(frozen=True)
class _Envelope:
    header: SecureManifestHeader
    header_bytes: bytes
    wrapped_key: bytes
    nonce: bytes
    ciphertext: bytes
    signer_der: bytes
    signature: bytes
    signed_bytes: bytes


def _parse(blob: bytes) -> _Envelope:
    if len(blob) > _MAX_FILE_SIZE:
        raise PayloadFormatError("distinta troppo grande")
    prefisso = MANIFEST_MAGIC + bytes([MANIFEST_SCHEMA_V2])
    if not blob.startswith(prefisso):
        raise PayloadFormatError("il file non e' una distinta v2")
    offset = len(prefisso)
    raw, offset = _take(blob, offset, 4, "lunghezza intestazione")
    header_len = int.from_bytes(raw, "big")
    header_bytes, offset = _take(blob, offset, header_len, "intestazione")
    raw, offset = _take(blob, offset, 2, "lunghezza chiave")
    wrapped_len = int.from_bytes(raw, "big")
    wrapped_key, offset = _take(blob, offset, wrapped_len, "chiave cifrata")
    nonce, offset = _take(blob, offset, _NONCE_SIZE, "nonce")
    raw, offset = _take(blob, offset, 4, "lunghezza contenuto")
    cipher_len = int.from_bytes(raw, "big")
    ciphertext, offset = _take(blob, offset, cipher_len, "contenuto cifrato")
    raw, offset = _take(blob, offset, 4, "lunghezza certificato")
    cert_len = int.from_bytes(raw, "big")
    signer_der, offset = _take(blob, offset, cert_len, "certificato mittente")
    signed_bytes = blob[:offset]
    signature, offset = _take(blob, offset, _SIGNATURE_SIZE, "firma")
    if offset != len(blob):
        raise PayloadFormatError("dati inattesi dopo la firma della distinta")
    try:
        mapping = json.loads(header_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadFormatError("intestazione JSON della distinta non valida") from exc
    if _canonical(mapping) != header_bytes:
        raise PayloadFormatError("intestazione della distinta non canonica")
    return _Envelope(
        SecureManifestHeader.from_mapping(mapping),
        header_bytes,
        wrapped_key,
        nonce,
        ciphertext,
        signer_der,
        signature,
        signed_bytes,
    )


def peek_secure_header(blob: bytes) -> SecureManifestHeader:
    """Legge i soli metadati non sanitari senza decifrare il documento."""
    return _parse(bytes(blob)).header


def seal_secure_manifest(
    manifest: Manifest,
    *,
    manifest_uuid: str,
    source_code: str,
    destination_code: str,
    recipient_certificate: x509.Certificate,
    signing_key: ed25519.Ed25519PrivateKey,
    signer_certificate: x509.Certificate,
) -> bytes:
    """Crea una distinta v2 cifrata per un ospedale e firmata dalla postazione."""
    destinatario = recipient_certificate.public_key()
    if not isinstance(destinatario, rsa.RSAPublicKey):
        raise ValueError("il certificato del destinatario deve contenere una chiave RSA")
    firmatario = signer_certificate.public_key()
    if not isinstance(firmatario, ed25519.Ed25519PublicKey):
        raise ValueError("il certificato della postazione deve contenere una chiave Ed25519")
    if signing_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) != firmatario.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw):
        raise ValueError("chiave privata e certificato della postazione non coincidono")
    if not manifest.entries:
        raise ValueError("la distinta non contiene campioni")
    _verify_station_identity(signer_certificate, source_code)
    adesso = dt.datetime.now(dt.timezone.utc)
    if not (
        signer_certificate.not_valid_before_utc
        <= adesso
        <= signer_certificate.not_valid_after_utc
    ):
        raise ValueError("il certificato della postazione e' scaduto o non valido")
    if not (
        recipient_certificate.not_valid_before_utc
        <= adesso
        <= recipient_certificate.not_valid_after_utc
    ):
        raise ValueError("il certificato del destinatario e' scaduto o non valido")

    header = SecureManifestHeader(
        manifest_uuid=str(manifest_uuid),
        source_code=str(source_code).strip(),
        destination_code=str(destination_code).strip(),
        shipment_id=int(manifest.shipment_id or 0),
        created_at=manifest.created_at,
        item_count=len(manifest.entries),
        recipient_fingerprint=certificate_fingerprint(recipient_certificate),
        signer_fingerprint=certificate_fingerprint(signer_certificate),
    )
    header_bytes = _canonical(header.to_dict())
    data_key = os.urandom(32)
    wrapped_key = destinatario.encrypt(
        data_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=_DOMAIN,
        ),
    )
    nonce = os.urandom(_NONCE_SIZE)
    aad = _DOMAIN + header_bytes + wrapped_key
    plaintext = _canonical(manifest.to_dict())
    ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
    signer_der = signer_certificate.public_bytes(serialization.Encoding.DER)
    prefix = b"".join(
        (
            MANIFEST_MAGIC,
            bytes([MANIFEST_SCHEMA_V2]),
            _u32(len(header_bytes)),
            header_bytes,
            _u16(len(wrapped_key)),
            wrapped_key,
            nonce,
            _u32(len(ciphertext)),
            ciphertext,
            _u32(len(signer_der)),
            signer_der,
        )
    )
    return prefix + signing_key.sign(_DOMAIN + prefix)


def _verify_certificate(
    certificato: x509.Certificate,
    trusted_cas: Iterable[x509.Certificate],
    revoked_serials: Iterable[int],
) -> None:
    adesso = dt.datetime.now(dt.timezone.utc)
    if not certificato.not_valid_before_utc <= adesso <= certificato.not_valid_after_utc:
        raise PayloadAuthenticationError("certificato della postazione scaduto o non valido")
    if certificato.serial_number in {int(v) for v in revoked_serials}:
        raise PayloadAuthenticationError("certificato della postazione revocato")
    for autorita in trusted_cas:
        if not autorita.not_valid_before_utc <= adesso <= autorita.not_valid_after_utc:
            continue
        try:
            vincoli = autorita.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
        except x509.ExtensionNotFound:
            continue
        if not vincoli.ca:
            continue
        try:
            certificato.verify_directly_issued_by(autorita)
            return
        except (ValueError, TypeError, InvalidSignature):
            continue
    raise PayloadAuthenticationError("certificato della postazione non attendibile")


def _verify_station_identity(
    certificato: x509.Certificate, source_code: str
) -> None:
    """Lega il codice unita' firmato all'identita' del certificato X.509."""
    identita = {
        attributo.value.strip().upper()
        for oid in (NameOID.ORGANIZATIONAL_UNIT_NAME, NameOID.COMMON_NAME)
        for attributo in certificato.subject.get_attributes_for_oid(oid)
    }
    if source_code.strip().upper() not in identita:
        raise PayloadAuthenticationError(
            "il certificato mittente non identifica l'unita' dichiarata"
        )


def open_secure_manifest(
    blob: bytes,
    *,
    recipient_private_key: rsa.RSAPrivateKey,
    recipient_certificate: x509.Certificate,
    trusted_cas: Iterable[x509.Certificate],
    revoked_serials: Iterable[int] = (),
    expected_destination_code: str = "",
) -> VerifiedManifest:
    """Verifica provenienza e integrita', poi decifra una distinta v2."""
    busta = _parse(bytes(blob))
    certificato_mittente = load_certificate(busta.signer_der)
    if certificate_fingerprint(certificato_mittente) != busta.header.signer_fingerprint:
        raise PayloadAuthenticationError("impronta del certificato mittente non coerente")
    if certificate_fingerprint(recipient_certificate) != busta.header.recipient_fingerprint:
        raise UnknownKeyError("la distinta e' cifrata per un altro destinatario")
    if expected_destination_code and (
        busta.header.destination_code.strip().upper()
        != expected_destination_code.strip().upper()
    ):
        raise PayloadAuthenticationError("la distinta e' indirizzata a un altro laboratorio")
    _verify_certificate(certificato_mittente, trusted_cas, revoked_serials)
    _verify_station_identity(certificato_mittente, busta.header.source_code)
    firmatario = certificato_mittente.public_key()
    if not isinstance(firmatario, ed25519.Ed25519PublicKey):
        raise PayloadAuthenticationError("certificato mittente privo di chiave Ed25519")
    try:
        firmatario.verify(busta.signature, _DOMAIN + busta.signed_bytes)
    except InvalidSignature as exc:
        raise PayloadAuthenticationError("firma della distinta non valida") from exc
    try:
        data_key = recipient_private_key.decrypt(
            busta.wrapped_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=_DOMAIN,
            ),
        )
        plaintext = AESGCM(data_key).decrypt(
            busta.nonce,
            busta.ciphertext,
            _DOMAIN + busta.header_bytes + busta.wrapped_key,
        )
    except (ValueError, InvalidTag) as exc:
        raise PayloadAuthenticationError("decifratura o autenticazione della distinta fallita") from exc
    try:
        mapping = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadFormatError("contenuto JSON della distinta non valido") from exc
    manifest = Manifest.from_dict(mapping)
    if len(manifest.entries) != busta.header.item_count:
        raise PayloadAuthenticationError("conteggio della distinta non coerente")
    if int(manifest.shipment_id or 0) != busta.header.shipment_id:
        raise PayloadAuthenticationError("numero di spedizione non coerente")
    if manifest.manifest_uuid and manifest.manifest_uuid != busta.header.manifest_uuid:
        raise PayloadAuthenticationError("identificativo della distinta non coerente")
    if manifest.source_code.strip().upper() != busta.header.source_code.strip().upper():
        raise PayloadAuthenticationError("codice del mittente non coerente")
    if (
        manifest.destination_code.strip().upper()
        != busta.header.destination_code.strip().upper()
    ):
        raise PayloadAuthenticationError("codice del destinatario non coerente")
    return VerifiedManifest(
        manifest=manifest,
        header=busta.header,
        sha256=hashlib.sha256(blob).hexdigest(),
        signer_certificate=certificato_mittente,
    )
