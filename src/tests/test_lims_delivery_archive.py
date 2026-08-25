"""Vincoli dell'archivio immutabile e delle ricevute di consegna."""

from __future__ import annotations

import hashlib
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims.db import LimsDatabase, LimsDatabaseError
from lims.model import Shipment, ShipmentState


def _archiviata(db: LimsDatabase) -> tuple[int, dict]:
    spedizione = db.create_shipment(Shipment(destinazione="Ospedale 1", data=date.today()))
    db.record_shipment_sealing(spedizione, ok=True, operator="operatore")
    db.set_shipment_state(spedizione, ShipmentState.SEALED)
    blob = b"distinta-immutabile"
    record = db.archive_outbound_manifest(
        spedizione,
        manifest_uuid="7fbe1c20-32cc-4590-9cc9-8b61362ae632",
        encrypted_blob=blob,
        manifest_hash=hashlib.sha256(blob).hexdigest(),
        filename="distinta.rfidman",
        item_count=1,
        source_code="UNITA-A",
        destination_code="OSPEDALE-1",
        operator="operatore",
    )
    return spedizione, record


def test_archivio_restituisce_gli_stessi_byte_e_rifiuta_una_seconda_distinta() -> None:
    with LimsDatabase() as db:
        spedizione, prima = _archiviata(db)
        assert bytes(prima["encrypted_blob"]) == b"distinta-immutabile"
        try:
            db.archive_outbound_manifest(
                spedizione,
                manifest_uuid="diversa",
                encrypted_blob=b"diversa",
                manifest_hash=hashlib.sha256(b"diversa").hexdigest(),
                filename="altra.rfidman",
                item_count=1,
                source_code="UNITA-A",
                destination_code="OSPEDALE-1",
            )
        except LimsDatabaseError:
            pass
        else:
            raise AssertionError("una seconda distinta ha sostituito quella archiviata")


def test_messaggio_e_ricevute_integrali_restano_archiviati_con_stato_monotono() -> None:
    with LimsDatabase() as db:
        spedizione, record = _archiviata(db)
        db.mark_outbound_smtp_accepted(spedizione, "<id@pec>", b"messaggio MIME")
        aggiornato = db.outbound_manifest(spedizione)
        assert bytes(aggiornato["sent_eml"]) == b"messaggio MIME"

        assert db.record_pec_receipt(
            record["id"],
            receipt_type="avvenuta-consegna",
            message_id="<id@pec>",
            raw_eml=b"ricevuta consegna",
            daticert_xml=b"<xml/>",
        )
        assert db.outbound_manifest(spedizione)["state"] == "delivered"
        # Una ricevuta di accettazione arrivata in ritardo non deve far tornare
        # indietro lo stato da consegnato ad accettato.
        db.record_pec_receipt(
            record["id"],
            receipt_type="accettazione",
            message_id="<id@pec>",
            raw_eml=b"ricevuta accettazione tardiva",
        )
        assert db.outbound_manifest(spedizione)["state"] == "delivered"
        assert len(db.pec_receipts(spedizione)) == 2


def test_annullamento_non_cancella_la_prova_ma_la_marca_superata() -> None:
    with LimsDatabase() as db:
        spedizione, _ = _archiviata(db)
        db.cancel_shipment(spedizione)
        assert db.outbound_manifest(spedizione)["state"] == "superseded"

