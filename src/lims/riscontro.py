"""Il verbale di riscontro: come e' andata, detto da chi ha aperto la scatola.

Fino a qui il mittente sa una cosa sola: di aver spedito. Le ricevute PEC
provano che il *documento* e' arrivato, non che le provette ci siano. Se un
campione si perde per strada, al laboratorio che l'ha mandato non lo dice
nessuno — e mesi dopo, quando qualcuno chiede conto di quel campione,
l'archivio del mittente si ferma alla parola «spedito».

Questo modulo chiude il giro. Il destinatario, dopo aver letto la scatola e
confermato la ricezione, esporta un **verbale**: cosa era atteso, cosa e'
arrivato, cosa mancava, chi ha controllato e quando. Il mittente lo importa e
la spedizione risulta *arrivata*. Solo allora il riepilogo del periodo puo'
dire «tutto a buon fine» senza mentire.

Il verbale usa la stessa infrastruttura della distinta e per le stesse ragioni:

* **cifrato**, perche' contiene gli stessi dati sanitari della distinta —
  quali campioni, di quali pazienti;
* **firmato**, perche' un verbale che dichiara «tutto arrivato» ha valore solo
  se si sa da chi viene: senza firma, chiunque potrebbe chiudere una
  spedizione altrui;
* **con i certificati se ci sono, con la chiave del circuito altrimenti**,
  esattamente come `lims.manifest` e `lims.secure_manifest`. Non si aggiunge
  una terza infrastruttura di chiavi per un terzo documento.

Il canale resta libero: allegato, chiavetta, cartella condivisa. E' un file, e
l'unica cosa che conta e' che arrivi.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

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
    "RISCONTRO_MAGIA",
    "RISCONTRO_SCHEMA_V1",
    "Riscontro",
    "apri_riscontro",
    "costruisci_riscontro",
    "sigilla_riscontro",
]

log = logging.getLogger("lims.riscontro")

RISCONTRO_MAGIA = b"RFIDLIMS-RISCONTRO"
RISCONTRO_SCHEMA_V1 = 1
_NONCE = 12
_DOMINIO = b"RFID-LIMS-riscontro-v1"


def _adesso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Riscontro:
    """Cosa e' arrivato davvero, e cosa no."""

    schema: int = RISCONTRO_SCHEMA_V1
    #: Numero che la spedizione ha nell'archivio del **mittente**: e' la chiave
    #: con cui lui la ritrovera'.
    shipment_id: int | None = None
    #: Numero che la stessa spedizione ha nell'archivio del destinatario. Non
    #: coincide, e serve quando i due si parlano al telefono.
    inbound_id: int | None = None
    manifest_uuid: str = ""
    lab_id: int = 0
    origine: str = ""
    destinazione: str = ""
    operatore: str = ""
    #: Quando la scatola e' stata letta, e quando la ricezione e' stata chiusa.
    letta_il: str = ""
    confermata_il: str = field(default_factory=_adesso)
    attesi: list[str] = field(default_factory=list)
    arrivati: list[str] = field(default_factory=list)
    mancanti: list[str] = field(default_factory=list)
    inattesi: list[str] = field(default_factory=list)
    non_conformita: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        """«Tutto a buon fine» ha un significato preciso e nessuna sfumatura.

        Nessun mancante, nessun inatteso, e almeno un campione: una scatola
        vuota che nessuno cerca non e' una spedizione riuscita.
        """
        return bool(self.attesi) and not self.mancanti and not self.inattesi

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}

    @classmethod
    def from_dict(cls, valore: Mapping[str, Any]) -> "Riscontro":
        schema = int(valore.get("schema", 0))
        if schema != RISCONTRO_SCHEMA_V1:
            raise PayloadFormatError(f"versione del verbale non supportata: {schema}")
        noti = {
            campo: valore[campo]
            for campo in cls.__dataclass_fields__
            if campo in valore
        }
        return cls(**noti)  # type: ignore[arg-type]

    def descrivi(self) -> dict[str, Any]:
        """Come lo legge un operatore, non un programma."""
        return {
            "shipment_id": self.shipment_id,
            "inbound_id": self.inbound_id,
            "manifest_uuid": self.manifest_uuid,
            "destinazione": self.destinazione,
            "operatore": self.operatore,
            "confermata_il": self.confermata_il,
            "attesi": len(self.attesi),
            "arrivati": len(self.arrivati),
            "mancanti": len(self.mancanti),
            "inattesi": len(self.inattesi),
            "ok": self.ok,
            "non_conformita": self.non_conformita,
            "epc_mancanti": list(self.mancanti),
            "epc_inattesi": list(self.inattesi),
        }


def costruisci_riscontro(
    db: Any,
    inbound_id: int,
    *,
    operatore: str,
    lab_id: int = 0,
    destinazione: str = "",
    note: str = "",
) -> Riscontro:
    """Il verbale a partire da cio' che l'archivio del destinatario ha registrato.

    Non si inventa niente: l'ultima riconciliazione e' gia' li', con la sua
    data e il suo operatore. Qui la si mette in una forma che possa viaggiare.
    """
    riga = db.inbound_row(int(inbound_id))
    riconciliazione = db.latest_inbound_reconciliation(int(inbound_id))
    if riconciliazione is None:
        raise ValueError(
            "questa ricezione non e' mai stata letta: non c'e' niente da certificare"
        )
    if riga["state"] != "received":
        raise ValueError("confermare la ricezione prima di esportare il verbale")

    return Riscontro(
        shipment_id=riga["origin_shipment_id"],
        inbound_id=int(inbound_id),
        manifest_uuid=str(riga["manifest_uuid"] or ""),
        lab_id=int(lab_id),
        origine=str(riga["destination"] or ""),
        destinazione=str(destinazione or riga["destination"] or ""),
        operatore=str(riga["confirmed_by"] or operatore),
        letta_il=str(riconciliazione.get("ts", "")),
        confermata_il=str(riga["confirmed_at"] or _adesso()),
        attesi=list(riconciliazione.get("expected", [])),
        arrivati=list(riconciliazione.get("arrived", [])),
        mancanti=list(riconciliazione.get("missing", [])),
        inattesi=list(riconciliazione.get("unexpected", [])),
        non_conformita=str(riga["nonconformity_reason"] or ""),
        note=note,
    )


# --------------------------------------------------------------------------
# Cifratura: la stessa della distinta, per le stesse ragioni
# --------------------------------------------------------------------------
def sigilla_riscontro(riscontro: Riscontro, keyring: Keyring, *, key_id: int | None = None) -> bytes:
    """Cifra il verbale con la chiave del circuito."""
    identificativo = keyring.default_key_id if key_id is None else int(key_id)
    chiave = keyring.require(identificativo)
    if len(chiave) != KEY_SIZE:
        raise ValueError(f"la chiave deve essere di {KEY_SIZE} byte")
    nonce = secrets.token_bytes(_NONCE)
    testa = (
        RISCONTRO_MAGIA
        + bytes([RISCONTRO_SCHEMA_V1, identificativo])
        + nonce
    )
    corpo = json.dumps(riscontro.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    cifrato = AESGCM(chiave).encrypt(nonce, corpo, _DOMINIO + testa)
    return testa + cifrato


def apri_riscontro(blob: bytes, keyring: Keyring) -> Riscontro:
    """Decifra e verifica un verbale ricevuto."""
    grezzo = bytes(blob)
    minimo = len(RISCONTRO_MAGIA) + 2 + _NONCE
    if not grezzo.startswith(RISCONTRO_MAGIA) or len(grezzo) <= minimo:
        raise PayloadFormatError("non e' un verbale di riscontro di questo sistema")
    schema = grezzo[len(RISCONTRO_MAGIA)]
    if schema != RISCONTRO_SCHEMA_V1:
        raise PayloadFormatError(f"versione del verbale non supportata: {schema}")
    identificativo = grezzo[len(RISCONTRO_MAGIA) + 1]
    chiave = keyring.get(identificativo)
    if chiave is None:
        raise UnknownKeyError(
            f"chiave {identificativo} non presente nel portachiavi: va concordata "
            "con il laboratorio che ha ricevuto"
        )
    nonce = grezzo[len(RISCONTRO_MAGIA) + 2 : minimo]
    testa = grezzo[:minimo]
    try:
        corpo = AESGCM(chiave).decrypt(nonce, grezzo[minimo:], _DOMINIO + testa)
    except InvalidTag as exc:
        raise PayloadAuthenticationError(
            "verbale non autentico o manomesso"
        ) from exc
    try:
        return Riscontro.from_dict(json.loads(corpo.decode("utf-8")))
    except (ValueError, TypeError) as exc:
        raise PayloadFormatError(f"contenuto del verbale illeggibile: {exc}") from exc
