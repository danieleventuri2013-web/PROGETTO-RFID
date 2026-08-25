"""Invio della distinta tramite PEC e lettura delle ricevute tecniche.

Il trasporto usa soltanto protocolli standard (SMTP submission e IMAP). Le
credenziali non fanno parte della configurazione YAML: il file contiene un
identificativo e la password viene richiesta al portachiavi del sistema
operativo tramite il pacchetto ``keyring``.
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import smtplib
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.message import EmailMessage, Message
from email.utils import make_msgid
from typing import Any, Callable, Iterable, Mapping

__all__ = [
    "PecConfig",
    "PecError",
    "PecReceipt",
    "PecSendResult",
    "PecTransport",
    "parse_pec_receipt",
    "resolve_secret",
]


class PecError(Exception):
    """Errore operativo del canale PEC."""


def resolve_secret(service: str, username: str) -> str:
    """Legge una credenziale dal portachiavi del sistema, fallendo in chiaro."""
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError as exc:
        raise PecError(
            "manca il supporto al portachiavi di sistema: installare la dipendenza keyring"
        ) from exc
    valore = keyring.get_password(str(service), str(username))
    if not valore:
        raise PecError(f"credenziale assente nel portachiavi ({service!r}, {username!r})")
    return valore


@dataclass(frozen=True)
class PecConfig:
    sender: str
    username: str
    credential_service: str
    smtp_host: str
    smtp_port: int = 465
    smtp_mode: str = "ssl"
    imap_host: str = ""
    imap_port: int = 993

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PecConfig":
        cfg = cls(
            sender=str(value.get("sender", "")).strip(),
            username=str(value.get("username", "")).strip(),
            credential_service=str(
                value.get("credential_service", "RFID-LIMS-PEC")
            ).strip(),
            smtp_host=str(value.get("smtp_host", "")).strip(),
            smtp_port=int(value.get("smtp_port", 465)),
            smtp_mode=str(value.get("smtp_mode", "ssl")).strip().lower(),
            imap_host=str(value.get("imap_host", "")).strip(),
            imap_port=int(value.get("imap_port", 993)),
        )
        if not cfg.sender or "@" not in cfg.sender:
            raise PecError("indirizzo PEC mittente mancante o non valido")
        if not cfg.username or not cfg.smtp_host:
            raise PecError("configurazione SMTP PEC incompleta")
        if cfg.smtp_mode not in {"ssl", "starttls"}:
            raise PecError("smtp_mode deve essere 'ssl' oppure 'starttls'")
        return cfg


@dataclass(frozen=True)
class PecSendResult:
    message_id: str
    raw_message: bytes


@dataclass(frozen=True)
class PecReceipt:
    receipt_type: str
    message_id: str
    raw_eml: bytes
    daticert_xml: bytes


def _attachment(message: Message, filename: str) -> bytes:
    for parte in message.walk():
        if parte.get_filename() == filename:
            return parte.get_payload(decode=True) or b""
    return b""


def parse_pec_receipt(raw_eml: bytes) -> PecReceipt | None:
    """Estrae tipo e identificativo dalla ricevuta standard ``daticert.xml``."""
    messaggio = email.message_from_bytes(bytes(raw_eml), policy=email.policy.default)
    xml = _attachment(messaggio, "daticert.xml")
    if not xml:
        return None
    try:
        radice = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise PecError(f"daticert.xml non valido: {exc}") from exc
    tipo = str(radice.attrib.get("tipo", "")).strip().lower()
    identificativo = ""
    for nodo in radice.iter():
        nome = nodo.tag.rsplit("}", 1)[-1].lower()
        if nome in {"identificativo", "message-id", "messageid"} and nodo.text:
            identificativo = nodo.text.strip()
            break
    if not tipo:
        raise PecError("ricevuta PEC senza tipologia")
    return PecReceipt(tipo, identificativo, bytes(raw_eml), xml)


class PecTransport:
    """Client sottile e iniettabile: i test non aprono connessioni reali."""

    def __init__(
        self,
        config: PecConfig,
        *,
        password: str | None = None,
        smtp_ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
    ):
        self.config = config
        self._password = password
        self._smtp_ssl_factory = smtp_ssl_factory
        self._smtp_factory = smtp_factory
        self._imap_factory = imap_factory

    @property
    def password(self) -> str:
        return self._password or resolve_secret(
            self.config.credential_service, self.config.username
        )

    def send_manifest(
        self,
        *,
        recipient: str,
        filename: str,
        blob: bytes,
        manifest_uuid: str,
        shipment_id: int,
        item_count: int,
        sha256: str,
        source_code: str,
        destination_code: str,
    ) -> PecSendResult:
        if not recipient or "@" not in recipient:
            raise PecError("indirizzo PEC del destinatario mancante o non valido")
        dominio = self.config.sender.rsplit("@", 1)[-1]
        message_id = make_msgid(idstring=str(manifest_uuid), domain=dominio)
        messaggio = EmailMessage()
        messaggio["From"] = self.config.sender
        messaggio["To"] = recipient
        messaggio["Message-ID"] = message_id
        messaggio["Subject"] = (
            f"Distinta RFID {source_code}-{shipment_id} verso {destination_code}"
        )
        messaggio.set_content(
            "\n".join(
                (
                    "Distinta cifrata di materiali in consegna.",
                    f"Identificativo: {manifest_uuid}",
                    f"Spedizione: {source_code}-{shipment_id}",
                    f"Destinatario: {destination_code}",
                    f"Contenitori: {item_count}",
                    f"SHA-256: {sha256.lower()}",
                    "Il messaggio non contiene dati sanitari in chiaro.",
                )
            )
        )
        messaggio.add_attachment(
            bytes(blob),
            maintype="application",
            subtype="octet-stream",
            filename=str(filename),
        )
        grezzo = messaggio.as_bytes(policy=email.policy.SMTP)
        contesto = ssl.create_default_context()
        try:
            if self.config.smtp_mode == "ssl":
                with self._smtp_ssl_factory(
                    self.config.smtp_host,
                    self.config.smtp_port,
                    context=contesto,
                    timeout=30,
                ) as client:
                    client.login(self.config.username, self.password)
                    client.sendmail(self.config.sender, [recipient], grezzo)
            else:
                with self._smtp_factory(
                    self.config.smtp_host, self.config.smtp_port, timeout=30
                ) as client:
                    client.ehlo()
                    client.starttls(context=contesto)
                    client.ehlo()
                    client.login(self.config.username, self.password)
                    client.sendmail(self.config.sender, [recipient], grezzo)
        except smtplib.SMTPResponseException as exc:
            raise PecError(
                f"il gestore PEC ha rifiutato l'invio ({exc.smtp_code}): "
                f"{exc.smtp_error.decode(errors='replace')}"
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            # Dopo un errore di rete non si puo' sapere sempre se il server ha
            # accettato DATA: il workflow lo marca come esito incerto.
            raise PecError(f"esito dell'invio PEC incerto: {exc}") from exc
        return PecSendResult(message_id, grezzo)

    def fetch_receipts(self) -> list[PecReceipt]:
        if not self.config.imap_host:
            raise PecError("configurazione IMAP PEC mancante")
        risultati: list[PecReceipt] = []
        try:
            with self._imap_factory(
                self.config.imap_host,
                self.config.imap_port,
                ssl_context=ssl.create_default_context(),
            ) as client:
                client.login(self.config.username, self.password)
                stato, _ = client.select("INBOX", readonly=True)
                if stato != "OK":
                    raise PecError("impossibile aprire la casella PEC")
                stato, dati = client.search(None, "UNSEEN")
                if stato != "OK":
                    raise PecError("impossibile cercare le ricevute PEC")
                identificativi: Iterable[bytes] = (dati[0] or b"").split()
                for identificativo in identificativi:
                    stato, corpo = client.fetch(identificativo, "(BODY.PEEK[])")
                    if stato != "OK" or not corpo:
                        continue
                    raw = next(
                        (
                            elemento[1]
                            for elemento in corpo
                            if isinstance(elemento, tuple) and isinstance(elemento[1], bytes)
                        ),
                        b"",
                    )
                    if not raw:
                        continue
                    ricevuta = parse_pec_receipt(raw)
                    if ricevuta is not None:
                        risultati.append(ricevuta)
        except PecError:
            raise
        except (OSError, imaplib.IMAP4.error) as exc:
            raise PecError(f"lettura delle ricevute PEC non riuscita: {exc}") from exc
        return risultati
