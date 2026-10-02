"""Messaggi con più distinte cifrate, indipendenti dal gestore di posta."""

from __future__ import annotations

import io
import os
import re
import smtplib
import ssl
import zipfile
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any, Mapping

MAX_FILES = 50
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024


class MailError(ValueError):
    """Configurazione o documento non utilizzabile."""


class SendUncertain(MailError):
    """La consegna al server potrebbe essere avvenuta: niente reinvio automatico."""


def address(value: Any) -> str:
    """Un solo indirizzo esplicito, senza intestazioni o destinatari nascosti."""
    value = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value):
        raise MailError("indirizzo email mancante o non valido")
    return value


def mail_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """La configurazione pubblica non contiene mai la password."""
    cfg = {
        "sender": str(value.get("sender", "")).strip(),
        "username": str(value.get("username", "")).strip(),
        "smtp_host": str(value.get("smtp_host", "smtp.gmail.com")).strip(),
        "smtp_port": int(value.get("smtp_port", 587)),
        "smtp_mode": str(value.get("smtp_mode", "starttls")),
        "password_env": str(value.get("password_env", "RFID_MAIL_PASSWORD")),
        "enabled": value.get("enabled") is True,
    }
    if cfg["sender"]:
        address(cfg["sender"])
    if cfg["username"]:
        address(cfg["username"])
    if cfg["smtp_mode"] not in {"ssl", "starttls"} or not 1 <= cfg["smtp_port"] <= 65535:
        raise MailError("usare SMTP con TLS: STARTTLS o SSL e una porta valida")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", cfg["smtp_host"]):
        raise MailError("server SMTP non valido")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg["password_env"]):
        raise MailError("nome della variabile della password non valido")
    if cfg["enabled"]:
        address(cfg["sender"])
        address(cfg["username"])
        if cfg["sender"].lower() != cfg["username"].lower():
            raise MailError("per l'invio usare come mittente la casella autenticata")
    return cfg


def build_message(sender: str, recipient: str, subject: str, body: str,
                  attachments: list[tuple[str, bytes]]) -> tuple[bytes, str]:
    """Produce una bozza MIME completa; nessun collegamento alla casella."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = address(sender)
    message["To"] = address(recipient)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@", 1)[1])
    message["X-Unsent"] = "1"
    message.set_content(body)
    if not attachments or len(attachments) > MAX_FILES:
        raise MailError("selezionare da 1 a 50 distinte")
    if sum(len(blob) for _, blob in attachments) > MAX_TOTAL_BYTES:
        raise MailError("allegati oltre 2 MiB: preparare più invii")
    for name, blob in attachments:
        message.add_attachment(blob, maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes(), str(message["Message-ID"])


def unpack_files(name: str, blob: bytes) -> list[tuple[str, bytes]]:
    """Apre il pacchetto in memoria, senza estrarre percorsi sul disco."""
    if len(blob) > MAX_TOTAL_BYTES:
        raise MailError("file oltre 2 MiB")
    if not name.lower().endswith(".zip"):
        if not name.lower().endswith(".rfidman") or len(blob) > MAX_FILE_BYTES:
            raise MailError("selezionare una distinta .rfidman o un pacchetto .zip")
        return [(name, blob)]
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            if not entries or len(entries) > MAX_FILES:
                raise MailError("il pacchetto deve contenere da 1 a 50 distinte")
            if sum(item.file_size for item in entries) > MAX_TOTAL_BYTES:
                raise MailError("pacchetto decompresso oltre 2 MiB")
            for item in entries:
                if (not item.filename.lower().endswith(".rfidman")
                        or "/" in item.filename or "\\" in item.filename
                        or item.file_size > MAX_FILE_BYTES or item.flag_bits & 1):
                    raise MailError("pacchetto non valido: usare solo distinte, senza cartelle né password ZIP")
            return [(item.filename, archive.read(item)) for item in entries]
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise MailError("pacchetto ZIP illeggibile") from exc


def send_message(config: Mapping[str, Any], raw: bytes, sender: str, recipient: str) -> None:
    """SMTP autenticato e cifrato. L'accettazione SMTP non prova l'arrivo del collo."""
    cfg = mail_config(config)
    if not cfg["enabled"]:
        raise MailError("invio email non abilitato nelle impostazioni")
    if sender.lower() != cfg["sender"].lower():
        raise MailError("il mittente della bozza è cambiato: preparare un nuovo invio")
    password = os.environ.get(cfg["password_env"], "")
    if not password:
        raise MailError(f"credenziale assente: impostare {cfg['password_env']} e riavviare il programma")
    # X-Unsent serve solo all'apertura locale della bozza.
    from email.parser import BytesParser
    message = BytesParser(policy=policy.SMTP).parsebytes(raw)
    if "X-Unsent" in message:
        del message["X-Unsent"]
    started = False
    client = None
    try:
        context = ssl.create_default_context()
        if cfg["smtp_mode"] == "ssl":
            client = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=30, context=context)
        else:
            client = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30)
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        client.login(cfg["username"], password)
        started = True
        rejected = client.sendmail(address(sender), [address(recipient)], message.as_bytes())
        if rejected:
            raise MailError("destinatario rifiutato dal server")
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
        raise MailError("messaggio rifiutato dal server SMTP; verificare casella e destinatario") from exc
    except (OSError, smtplib.SMTPException) as exc:
        if started:
            raise SendUncertain("esito incerto: controllare la posta inviata prima di un nuovo invio") from exc
        raise MailError("connessione o autenticazione SMTP fallita; verificare configurazione e password per app") from exc
    finally:
        if client is not None:
            # Un errore in QUIT dopo il 250 a DATA non annulla l'accettazione.
            try:
                client.quit()
            except (OSError, smtplib.SMTPException):
                client.close()
