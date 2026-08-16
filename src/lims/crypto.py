"""Sigillo crittografico del payload scritto nella USER memory del tag.

Il requisito operativo e' che il tag viaggi da solo, senza rete e senza database
di riscontro. Ne discende che i dati del paziente stanno fisicamente sul tag, e
che un tag UHF e' leggibile da chiunque abbia un lettore a qualche metro, senza
contatto e senza lasciare traccia. Il payload va quindi cifrato.

Schema adottato: **AES-256-GCM**, con due scelte che meritano una spiegazione.

*Il nonce non viene memorizzato.* Si ricava da EPC e numero di revisione:
`nonce = SHA256(dominio | EPC | revisione)[:12]`. Su 46 byte utili, spendere 12
byte per un nonce esplicito sarebbe stato insostenibile. Poiche' l'EPC e' unico
per contenitore, i nonce non collidono; le riscritture dello stesso contenitore
sono distinte dalla revisione, che percio' **va incrementata a ogni riscrittura**.

*Il sigillo e' legato al chip.* I dati autenticati (AAD) comprendono
l'intestazione, l'EPC e il **TID**, l'identificativo di fabbrica che non e'
riscrivibile. Un tag clonato copiando EPC e USER memory su un chip vergine non
supera la verifica di autenticita': in una catena di custodia di campioni
istologici questo distingue uno scambio di provetta da una semplice rilettura.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "GCM_TAG_SIZE",
    "HEADER_SIZE",
    "KEY_SIZE",
    "MAX_PLAINTEXT",
    "SEAL_OVERHEAD",
    "SEAL_SCHEMA_V1",
    "CryptoError",
    "Keyring",
    "PayloadAuthenticationError",
    "PayloadFormatError",
    "SealedPayload",
    "UnknownKeyError",
    "max_plaintext_bytes",
    "open_sealed",
    "read_header",
    "seal",
]

KEY_SIZE = 32          # AES-256
GCM_TAG_SIZE = 16
HEADER_SIZE = 4
SEAL_OVERHEAD = HEADER_SIZE + GCM_TAG_SIZE
SEAL_SCHEMA_V1 = 0x01
MAX_PLAINTEXT = 0xFFFF

_MAGIC_NIBBLE = 0xA0   # riconosce a colpo d'occhio un payload di questo sistema
_DOMAIN = b"RFID-LIMS-v1"


class CryptoError(Exception):
    """Errore nel sigillo del payload."""


class PayloadFormatError(CryptoError):
    """Intestazione assente, troncata o di versione sconosciuta."""


class UnknownKeyError(CryptoError):
    """Il payload e' cifrato con una chiave che questo laboratorio non possiede."""


class PayloadAuthenticationError(CryptoError):
    """Autenticazione fallita: chiave errata, tag clonato o dati manomessi."""


def max_plaintext_bytes(user_memory_bytes: int) -> int:
    """Byte di testo in chiaro che stanno in una USER memory di quella dimensione.

    Per i 64 byte tipici di un chip con 512 bit di USER memory restituisce 46.
    """
    if user_memory_bytes < SEAL_OVERHEAD:
        return 0
    return user_memory_bytes - SEAL_OVERHEAD


def _normalize_bytes(value: bytes | str, field_name: str) -> bytes:
    if isinstance(value, str):
        try:
            return bytes.fromhex("".join(value.split()))
        except ValueError as exc:
            raise ValueError(f"{field_name} non e' esadecimale valido") from exc
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    raise TypeError(f"{field_name} deve essere bytes o stringa esadecimale")


def _check_key_id(key_id: int) -> int:
    if not isinstance(key_id, int) or isinstance(key_id, bool):
        raise TypeError("key_id deve essere un intero")
    if not 0 <= key_id <= 0x0F:
        raise ValueError("key_id fuori intervallo 0..15 (occupa mezzo byte)")
    return key_id


def _check_revision(revision: int) -> int:
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise TypeError("revision deve essere un intero")
    if not 0 <= revision <= 0x0F:
        raise ValueError(
            "revision fuori intervallo 0..15: oltre la sedicesima riscrittura dello "
            "stesso contenitore il nonce si ripeterebbe, va emesso un nuovo EPC"
        )
    return revision


def _derive_nonce(epc: bytes, revision: int) -> bytes:
    digest = hashlib.sha256(_DOMAIN + b"|nonce|" + epc + bytes([revision])).digest()
    return digest[:12]


def _build_aad(header: bytes, epc: bytes, tid: bytes) -> bytes:
    # Il TID entra nei dati autenticati: e' cio' che lega il payload al chip
    # fisico e rende inutile la copia su un tag vergine.
    return b"|".join((_DOMAIN, header, epc, tid))


@dataclass(frozen=True)
class SealedPayload:
    """Intestazione in chiaro di un payload sigillato.

    `length` e' la lunghezza del testo in chiaro. Senza di essa il lettore non
    saprebbe dove finisce il payload e dove cominciano i byte non scritti della
    USER memory: gli zeri residui entrerebbero nel calcolo di autenticazione e
    ogni verifica fallirebbe. Non si puo' nemmeno tagliare gli zeri finali,
    perche' il tag GCM e' casuale e puo' legittimamente terminare con uno zero.
    """

    schema: int
    key_id: int
    revision: int
    length: int = 0

    def header_bytes(self) -> bytes:
        return bytes(
            [_MAGIC_NIBBLE | self.schema, (self.key_id << 4) | self.revision]
        ) + self.length.to_bytes(2, "big")

    @property
    def total_size(self) -> int:
        """Byte occupati sul tag, intestazione e tag di autenticazione compresi."""
        return SEAL_OVERHEAD + self.length


def read_header(data: bytes) -> SealedPayload:
    """Legge l'intestazione in chiaro senza tentare di decifrare.

    Serve alla postazione di ricezione: permette di dire "questo tag e' nostro ma
    e' cifrato con la chiave 3, che non abbiamo" invece di un generico errore.
    """
    if len(data) < HEADER_SIZE:
        raise PayloadFormatError(
            f"intestazione troncata: {len(data)} byte, minimo {HEADER_SIZE}"
        )
    first = data[0]
    if first & 0xF0 != _MAGIC_NIBBLE:
        raise PayloadFormatError(
            f"non e' un payload di questo sistema (primo byte 0x{first:02X})"
        )
    schema = first & 0x0F
    if schema != SEAL_SCHEMA_V1:
        raise PayloadFormatError(f"versione sigillo non supportata: {schema}")
    return SealedPayload(
        schema=schema,
        key_id=data[1] >> 4,
        revision=data[1] & 0x0F,
        length=int.from_bytes(data[2:4], "big"),
    )


def seal(
    plaintext: bytes,
    *,
    epc: bytes | str,
    tid: bytes | str,
    key: bytes,
    key_id: int = 0,
    revision: int = 0,
    align_to_words: bool = True,
) -> bytes:
    """Sigilla il payload per la scrittura sul tag.

    `align_to_words` allinea il risultato a lunghezza pari, perche' il comando di
    scrittura del lettore accetta solo multipli di due byte (`reader.py:427`).
    Il riempimento e' fatto di byte nulli in coda, che `codec.unpack_payload`
    scarta insieme al padding del nome.
    """
    if not isinstance(plaintext, (bytes, bytearray)):
        raise TypeError("plaintext deve essere bytes")
    if len(key) != KEY_SIZE:
        raise ValueError(f"la chiave deve essere di {KEY_SIZE} byte (AES-256)")
    epc_bytes = _normalize_bytes(epc, "epc")
    tid_bytes = _normalize_bytes(tid, "tid")
    if not epc_bytes:
        raise ValueError("epc obbligatorio: il nonce ne deriva")
    if not tid_bytes:
        raise ValueError("tid obbligatorio: e' il legame con il chip fisico")
    _check_key_id(key_id)
    _check_revision(revision)

    payload = bytes(plaintext)
    if align_to_words and len(payload) % 2 != 0:
        payload += b"\x00"
    if len(payload) > MAX_PLAINTEXT:
        raise ValueError(f"payload troppo lungo: massimo {MAX_PLAINTEXT} byte")

    header = SealedPayload(SEAL_SCHEMA_V1, key_id, revision, len(payload)).header_bytes()
    nonce = _derive_nonce(epc_bytes, revision)
    aad = _build_aad(header, epc_bytes, tid_bytes)
    return header + AESGCM(key).encrypt(nonce, payload, aad)


def open_sealed(
    data: bytes,
    *,
    epc: bytes | str,
    tid: bytes | str,
    keys: Mapping[int, bytes] | "Keyring",
) -> tuple[bytes, SealedPayload]:
    """Verifica e decifra un payload letto dal tag.

    Distingue tre esiti diversi, perche' all'operatore servono tre messaggi
    diversi: formato estraneo, chiave mancante, autenticazione fallita.
    """
    meta = read_header(data)
    if meta.length == 0:
        raise PayloadFormatError("l'intestazione dichiara un payload di lunghezza nulla")
    if len(data) < meta.total_size:
        raise PayloadFormatError(
            f"payload troncato: l'intestazione ne dichiara {meta.total_size} byte, "
            f"ne sono disponibili {len(data)}"
        )
    # Si prende esattamente quanto dichiarato: cio' che segue e' memoria non
    # scritta del tag e non deve entrare nel calcolo di autenticazione.
    body = data[HEADER_SIZE : meta.total_size]

    if isinstance(keys, Keyring):
        key = keys.get(meta.key_id)
    else:
        key = keys.get(meta.key_id)
    if key is None:
        raise UnknownKeyError(
            f"payload cifrato con la chiave {meta.key_id}, non disponibile in questo "
            "laboratorio: va concordata con il mittente"
        )

    epc_bytes = _normalize_bytes(epc, "epc")
    tid_bytes = _normalize_bytes(tid, "tid")
    nonce = _derive_nonce(epc_bytes, meta.revision)
    aad = _build_aad(meta.header_bytes(), epc_bytes, tid_bytes)
    try:
        plaintext = AESGCM(key).decrypt(nonce, body, aad)
    except InvalidTag as exc:
        raise PayloadAuthenticationError(
            "autenticazione fallita: chiave errata, oppure EPC o TID non "
            "corrispondono a quelli con cui il payload e' stato scritto "
            "(possibile tag clonato o riprogrammato)"
        ) from exc
    return plaintext, meta


class Keyring:
    """Insieme delle chiavi simmetriche condivise con gli altri laboratori.

    Le chiavi non stanno nel database e non compaiono mai nei log o nelle
    rappresentazioni testuali. Senza rete fra i laboratori, la consegna della
    chiave e' un passaggio fuori banda da fare una volta sola.
    """

    def __init__(self, keys: Mapping[int, bytes] | None = None, *, default_key_id: int = 0):
        self._keys: dict[int, bytes] = {}
        for key_id, key in (keys or {}).items():
            self.add(int(key_id), key)
        self.default_key_id = _check_key_id(default_key_id)

    def add(self, key_id: int, key: bytes) -> None:
        _check_key_id(key_id)
        if len(key) != KEY_SIZE:
            raise ValueError(f"la chiave {key_id} deve essere di {KEY_SIZE} byte")
        self._keys[key_id] = bytes(key)

    def get(self, key_id: int) -> bytes | None:
        return self._keys.get(key_id)

    def require(self, key_id: int) -> bytes:
        key = self.get(key_id)
        if key is None:
            raise UnknownKeyError(f"chiave {key_id} non presente nel portachiavi")
        return key

    @property
    def default_key(self) -> bytes:
        return self.require(self.default_key_id)

    @property
    def key_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._keys))

    def generate(self, key_id: int | None = None) -> int:
        """Crea una nuova chiave casuale e la restituisce come identificativo."""
        if key_id is None:
            candidates = [value for value in range(16) if value not in self._keys]
            if not candidates:
                raise ValueError("tutti i 16 identificativi di chiave sono occupati")
            key_id = candidates[0]
        self.add(key_id, secrets.token_bytes(KEY_SIZE))
        return key_id

    # -- persistenza -------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Keyring":
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        keys = {int(key_id): bytes.fromhex(value) for key_id, value in raw.get("keys", {}).items()}
        return cls(keys, default_key_id=int(raw.get("default_key_id", 0)))

    def save(self, path: str | Path) -> Path:
        """Scrive il portachiavi limitando i permessi per quanto il sistema consente."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "default_key_id": self.default_key_id,
            "keys": {str(key_id): key.hex() for key_id, key in sorted(self._keys.items())},
        }
        path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Su Windows chmod non riproduce le ACL: la protezione del file va
            # garantita dai permessi della cartella che lo contiene.
            pass
        return path

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, key_id: object) -> bool:
        return key_id in self._keys

    def __repr__(self) -> str:
        # Mai il materiale delle chiavi: questo oggetto finisce nei messaggi di
        # errore e nei log di diagnostica.
        return f"Keyring(key_ids={list(self.key_ids)}, default={self.default_key_id})"
