"""Distinta di spedizione cifrata e riconciliazione all'arrivo.

Il tag e' autosufficiente, quindi la distinta non serve a far arrivare i dati:
serve come **riscontro indipendente**. Se all'arrivo la scatola e i tag dicono
una cosa e la distinta un'altra, c'e' un problema da guardare — ed e' proprio
quello il valore di avere due fonti invece di una.

Serve anche come prova documentale di cosa e' stato spedito, con il record del
sigillo allegato: passate eseguite, configurazione radio, risposta di ogni tag.

E' cifrata con la stessa infrastruttura del payload (`lims.crypto`, chiave del
circuito, `key_id` per la rotazione) perche' contiene gli stessi dati sanitari.
A differenza del tag pero' non c'e' vincolo di spazio, quindi il nonce e'
casuale ed esplicito: nessuna acrobazia per risparmiare byte.

Il canale resta libero: allegato email, cartella condivisa, chiavetta.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .crypto import (
    KEY_SIZE,
    Keyring,
    PayloadAuthenticationError,
    PayloadFormatError,
    UnknownKeyError,
)

__all__ = [
    "MANIFEST_MAGIC",
    "MANIFEST_SCHEMA_V1",
    "MANIFEST_SCHEMA_V2",
    "Manifest",
    "ManifestEntry",
    "Reconciliation",
    "build_manifest",
    "open_manifest",
    "reconcile",
    "seal_manifest",
]

log = logging.getLogger("lims.manifest")

MANIFEST_MAGIC = b"RFIDLIMS-MANIFEST"
MANIFEST_SCHEMA_V1 = 1
MANIFEST_SCHEMA_V2 = 2
_NONCE_SIZE = 12
_DOMAIN = b"RFID-LIMS-manifest-v1"


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ManifestEntry:
    """Un contenitore nella distinta."""

    epc: str
    accession_id: int
    container_index: int
    container_total: int
    codice_fiscale: str = ""
    display_name: str = ""
    tid: str = ""
    external_ref: str = ""
    material_code: int = 0
    fixative_code: int = 0
    site_code: int = 0
    data_prelievo: str | None = None
    descrizione: str = ""
    flags: int = 0

    @property
    def label(self) -> str:
        return f"{self.container_index}/{self.container_total}"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ManifestEntry":
        noti = {campo: value.get(campo) for campo in cls.__dataclass_fields__ if campo in value}
        return cls(**noti)  # type: ignore[arg-type]


@dataclass
class Manifest:
    """Cosa e' stato spedito, da chi, a chi, e con quale prova di sigillo."""

    schema: int = MANIFEST_SCHEMA_V1
    lab_id: int = 0
    shipment_id: int | None = None
    destination: str = ""
    created_at: str = field(default_factory=_now)
    operator: str = ""
    entries: list[ManifestEntry] = field(default_factory=list)
    sealing: dict[str, Any] | None = None
    box_epc: str = ""
    notes: str = ""
    manifest_uuid: str = ""
    source_code: str = ""
    destination_code: str = ""
    visual_check: dict[str, Any] | None = None

    @property
    def epcs(self) -> tuple[str, ...]:
        return tuple(sorted(voce.epc for voce in self.entries))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "lab_id": self.lab_id,
            "shipment_id": self.shipment_id,
            "destination": self.destination,
            "created_at": self.created_at,
            "operator": self.operator,
            "box_epc": self.box_epc,
            "notes": self.notes,
            "manifest_uuid": self.manifest_uuid,
            "source_code": self.source_code,
            "destination_code": self.destination_code,
            "entries": [asdict(voce) for voce in self.entries],
            "sealing": self.sealing,
            "visual_check": self.visual_check,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Manifest":
        schema = int(value.get("schema", 0))
        if schema not in (MANIFEST_SCHEMA_V1, MANIFEST_SCHEMA_V2):
            raise PayloadFormatError(f"versione distinta non supportata: {schema}")
        return cls(
            schema=schema,
            lab_id=int(value.get("lab_id", 0)),
            shipment_id=value.get("shipment_id"),
            destination=str(value.get("destination", "")),
            created_at=str(value.get("created_at", "")),
            operator=str(value.get("operator", "")),
            box_epc=str(value.get("box_epc", "")),
            notes=str(value.get("notes", "")),
            manifest_uuid=str(value.get("manifest_uuid", "")),
            source_code=str(value.get("source_code", "")),
            destination_code=str(value.get("destination_code", "")),
            entries=[ManifestEntry.from_mapping(voce) for voce in value.get("entries", [])],
            sealing=value.get("sealing"),
            visual_check=value.get("visual_check"),
        )


def build_manifest(
    db: Any,
    shipment_id: int,
    *,
    lab_id: int = 0,
    operator: str = "",
    sealing: Any = None,
    box_epc: str = "",
    external_refs: Mapping[int, str] | None = None,
    notes: str = "",
) -> Manifest:
    """Compone la distinta dai contenitori effettivamente in spedizione.

    Prende solo i contenitori registrati nella spedizione: quelli annullati non
    ci sono, ed e' giusto che non compaiano — sono campioni che non partono.
    """
    contenuto = db.shipment_contents(shipment_id)
    spedizione = db.connection.execute(
        "SELECT destinazione FROM shipments WHERE id=?", (shipment_id,)
    ).fetchone()
    riferimenti = dict(external_refs or {})

    voci = [
        ManifestEntry(
            epc=record.epc,
            tid=record.tid,
            accession_id=record.accession_id,
            container_index=record.index,
            container_total=record.total,
            codice_fiscale=record.codice_fiscale,
            display_name=record.display_name,
            external_ref=riferimenti.get(record.accession_id, record.external_ref),
            material_code=record.material_code,
            fixative_code=record.fixative_code,
            site_code=record.site_code,
            data_prelievo=record.data_prelievo.isoformat() if record.data_prelievo else None,
            descrizione=record.descrizione,
            flags=record.flags,
        )
        for record in contenuto
        if record.epc
    ]
    return Manifest(
        lab_id=lab_id,
        shipment_id=shipment_id,
        destination=str(spedizione["destinazione"]) if spedizione else "",
        operator=operator,
        entries=voci,
        sealing=sealing.to_dict() if hasattr(sealing, "to_dict") else sealing,
        box_epc=box_epc.strip().upper(),
        notes=notes,
    )


# --------------------------------------------------------------------------
# Sigillo del documento
# --------------------------------------------------------------------------
# Formato: MAGIC | schema(1) | key_id(1) | nonce(12) | ciphertext+tag
#
# A differenza del payload sul tag qui lo spazio non e' un problema, quindi il
# nonce e' casuale ed esplicito invece che derivato: e' la scelta piu' semplice e
# quella che non puo' ripetersi per errore.
def seal_manifest(manifest: Manifest, keyring: Keyring, key_id: int | None = None) -> bytes:
    """Cifra e autentica la distinta."""
    key_id = keyring.default_key_id if key_id is None else key_id
    chiave = keyring.require(key_id)
    if len(chiave) != KEY_SIZE:
        raise ValueError(f"la chiave deve essere di {KEY_SIZE} byte")
    if not 0 <= key_id <= 0xFF:
        raise ValueError("key_id fuori intervallo 0..255")

    documento = json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    nonce = secrets.token_bytes(_NONCE_SIZE)
    intestazione = MANIFEST_MAGIC + bytes([MANIFEST_SCHEMA_V1, key_id])
    # L'intestazione entra nei dati autenticati: cambiarne il key_id per far
    # provare un'altra chiave deve far fallire l'apertura, non riuscirla.
    aad = _DOMAIN + intestazione
    return intestazione + nonce + AESGCM(chiave).encrypt(nonce, documento, aad)


def open_manifest(blob: bytes, keyring: Keyring) -> Manifest:
    """Verifica, decifra e ricostruisce la distinta.

    Distingue i tre esiti che all'operatore servono distinti: file estraneo,
    chiave mancante, contenuto manomesso.
    """
    testa = len(MANIFEST_MAGIC) + 2
    if len(blob) < testa + _NONCE_SIZE + 16:
        raise PayloadFormatError("file troppo corto per essere una distinta")
    if not blob.startswith(MANIFEST_MAGIC):
        raise PayloadFormatError("il file non e' una distinta di questo sistema")
    schema = blob[len(MANIFEST_MAGIC)]
    if schema != MANIFEST_SCHEMA_V1:
        raise PayloadFormatError(f"versione distinta non supportata: {schema}")
    key_id = blob[len(MANIFEST_MAGIC) + 1]
    chiave = keyring.get(key_id)
    if chiave is None:
        raise UnknownKeyError(
            f"distinta cifrata con la chiave {key_id}, non disponibile in questo "
            "laboratorio: va concordata con il mittente"
        )

    nonce = blob[testa : testa + _NONCE_SIZE]
    corpo = blob[testa + _NONCE_SIZE :]
    aad = _DOMAIN + blob[:testa]
    try:
        chiaro = AESGCM(chiave).decrypt(nonce, corpo, aad)
    except InvalidTag as exc:
        raise PayloadAuthenticationError(
            "autenticazione della distinta fallita: chiave errata o file manomesso"
        ) from exc
    return Manifest.from_dict(json.loads(chiaro.decode("utf-8")))


# --------------------------------------------------------------------------
# Riconciliazione
# --------------------------------------------------------------------------
@dataclass
class Reconciliation:
    """Confronto fra cio' che e' stato spedito e cio' che e' arrivato."""

    expected: tuple[str, ...] = ()
    arrived: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    details: dict[str, ManifestEntry] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unexpected

    @property
    def counts(self) -> tuple[int, int]:
        return len(self.arrived), len(self.expected)

    def describe_missing(self) -> list[str]:
        """I mancanti con nome e paziente: un EPC da solo non dice niente a nessuno."""
        righe = []
        for epc in self.missing:
            voce = self.details.get(epc)
            if voce is None:
                righe.append(epc)
            else:
                righe.append(
                    f"accettazione {voce.accession_id}, contenitore {voce.label} — "
                    f"{voce.display_name} ({voce.codice_fiscale})"
                )
        return righe

    def to_dict(self) -> dict[str, Any]:
        arrivati, attesi = self.counts
        return {
            "ok": self.ok,
            "arrivati": arrivati,
            "attesi": attesi,
            "mancanti": list(self.missing),
            "mancanti_descritti": self.describe_missing(),
            "inattesi": list(self.unexpected),
        }


def reconcile(manifest: Manifest, observed_epcs: Iterable[str]) -> Reconciliation:
    """Confronta la distinta con quanto letto all'arrivo.

    Gli **inattesi** contano quanto i mancanti: un contenitore in piu' nella
    scatola e' un errore di spedizione, non un bonus.
    """
    attesi = {voce.epc.strip().upper() for voce in manifest.entries if voce.epc}
    letti = {str(epc).strip().upper() for epc in observed_epcs if epc}
    return Reconciliation(
        expected=tuple(sorted(attesi)),
        arrived=tuple(sorted(attesi & letti)),
        missing=tuple(sorted(attesi - letti)),
        unexpected=tuple(sorted(letti - attesi)),
        details={voce.epc.strip().upper(): voce for voce in manifest.entries if voce.epc},
    )
