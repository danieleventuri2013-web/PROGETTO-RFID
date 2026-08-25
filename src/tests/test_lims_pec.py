"""Test del canale PEC senza connessioni di rete."""

from __future__ import annotations

import email
import email.policy
import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims.pec import PecConfig, PecTransport, parse_pec_receipt


class _SmtpFinto:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.login_eseguito = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, username, password):
        self.login_eseguito = bool(username and password)

    def sendmail(self, sender, recipients, raw_message):
        assert sender == "unita-a@pec.example"
        assert recipients == ["ospedale@pec.example"]
        type(self).ultimo = raw_message


def test_invio_pec_contiene_esattamente_il_file_e_nessun_dato_sanitario() -> None:
    blob = b"busta-cifrata-esatta"
    trasporto = PecTransport(
        PecConfig(
            sender="unita-a@pec.example",
            username="unita-a@pec.example",
            credential_service="test",
            smtp_host="smtp.pec.example",
        ),
        password="segreto",
        smtp_ssl_factory=_SmtpFinto,
    )
    esito = trasporto.send_manifest(
        recipient="ospedale@pec.example",
        filename="distinta.rfidman",
        blob=blob,
        manifest_uuid="uuid-1",
        shipment_id=12,
        item_count=3,
        sha256="ab" * 32,
        source_code="UNITA-A",
        destination_code="OSPEDALE-1",
    )
    assert _SmtpFinto.ultimo == esito.raw_message
    letta = email.message_from_bytes(esito.raw_message, policy=email.policy.default)
    allegati = [parte for parte in letta.iter_attachments()]
    assert len(allegati) == 1
    assert allegati[0].get_payload(decode=True) == blob
    corpo = letta.get_body(preferencelist=("plain",)).get_content()
    assert "MRTMTT25D09F205Z" not in corpo
    assert "SHA-256" in corpo and esito.message_id


def test_ricevuta_pec_estrae_tipo_identificativo_e_xml_integrale() -> None:
    xml = b'<postacert tipo="avvenuta-consegna"><identificativo>&lt;id@pec&gt;</identificativo></postacert>'
    messaggio = EmailMessage()
    messaggio["From"] = "gestore@pec.example"
    messaggio["To"] = "unita-a@pec.example"
    messaggio.set_content("Ricevuta PEC")
    messaggio.add_attachment(
        xml,
        maintype="application",
        subtype="xml",
        filename="daticert.xml",
    )
    ricevuta = parse_pec_receipt(messaggio.as_bytes(policy=email.policy.SMTP))
    assert ricevuta is not None
    assert ricevuta.receipt_type == "avvenuta-consegna"
    assert ricevuta.message_id == "<id@pec>"
    assert ricevuta.daticert_xml == xml
