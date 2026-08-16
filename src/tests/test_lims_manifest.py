"""Test della distinta cifrata e della riconciliazione all'arrivo.

La distinta non serve a far arrivare i dati — il tag e' autosufficiente — ma a
fare da **riscontro indipendente**. Il suo valore sta tutto nel caso in cui le
due fonti non concordino, ed e' quello che questi test coprono.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lims.crypto import (
    KEY_SIZE,
    Keyring,
    PayloadAuthenticationError,
    PayloadFormatError,
    UnknownKeyError,
)
from lims.db import LimsDatabase
from lims.manifest import (
    MANIFEST_MAGIC,
    Manifest,
    ManifestEntry,
    build_manifest,
    open_manifest,
    reconcile,
    seal_manifest,
)
from lims.model import Case, Patient, Shipment, Specimen
from lims.sealing import ClosureProof, SealingRecord

CF = "MRTMTT25D09F205Z"
CHIAVE = bytes(range(KEY_SIZE))
ALTRA_CHIAVE = bytes(range(100, 100 + KEY_SIZE))


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _voce(indice: int, totale: int = 3) -> ManifestEntry:
    return ManifestEntry(
        epc=f"0100A5000F1206{indice:02d}{totale:02d}DEADB{indice}",
        accession_id=987654,
        container_index=indice,
        container_total=totale,
        codice_fiscale=CF,
        display_name="DELLA VALLE GIANFRANCO",
        material_code=3,
        data_prelievo="2026-08-15",
    )


def _manifest(voci: int = 3) -> Manifest:
    return Manifest(
        lab_id=0x00A5,
        shipment_id=7,
        destination="Laboratorio Centrale",
        operator="dvent",
        entries=[_voce(i) for i in range(1, voci + 1)],
    )


def _archivio_con_spedizione(db: LimsDatabase, totale: int = 3) -> int:
    patient_id = db.upsert_patient(
        Patient(codice_fiscale=CF, cognome="Della Valle", nome="Gianfranco")
    )
    case_id = db.create_case(
        Case(accession_id=987654, patient_id=patient_id, data_prelievo=date(2026, 8, 15))
    )
    specimen_id = db.add_specimen(
        Specimen(case_id=case_id, material_code=3, fixative_code=1, site_code=2)
    )
    contenitori = db.plan_containers(specimen_id, totale)
    for offset, container_id in enumerate(contenitori, start=1):
        db.assign_epc(container_id, f"0100A5000F1206{offset:02d}{totale:02d}AABBC{offset}")
        db.mark_provisioned(container_id, f"E280119020005000000000{offset:02d}")
    shipment_id = db.create_shipment(
        Shipment(destinazione="Laboratorio Centrale", data=date(2026, 8, 16))
    )
    db.add_to_shipment(shipment_id, contenitori)
    return shipment_id


# --------------------------------------------------------------------------
# Composizione dall'archivio
# --------------------------------------------------------------------------
def test_distinta_costruita_dai_contenitori_in_spedizione() -> None:
    with LimsDatabase() as db:
        shipment_id = _archivio_con_spedizione(db)
        distinta = build_manifest(db, shipment_id, lab_id=0x00A5, operator="dvent")

        assert len(distinta.entries) == 3
        assert distinta.destination == "Laboratorio Centrale"
        assert distinta.shipment_id == shipment_id
        prima = distinta.entries[0]
        assert prima.codice_fiscale == CF
        assert prima.display_name == "DELLA VALLE GIANFRANCO"
        assert prima.label == "1/3"
        assert prima.data_prelievo == "2026-08-15"


def test_i_contenitori_annullati_non_entrano_in_distinta() -> None:
    """Un campione che non parte non deve comparire fra quelli spediti."""
    with LimsDatabase() as db:
        shipment_id = _archivio_con_spedizione(db)
        contenuto = db.shipment_contents(shipment_id)
        db.void_container(contenuto[1].container_id, reason="contenitore rotto")
        db.connection.execute(
            "DELETE FROM shipment_items WHERE container_id=?", (contenuto[1].container_id,)
        )
        db.connection.commit()

        distinta = build_manifest(db, shipment_id, lab_id=0x00A5)
        assert len(distinta.entries) == 2
        assert contenuto[1].epc not in distinta.epcs


def test_il_record_di_sigillo_viene_allegato() -> None:
    with LimsDatabase() as db:
        shipment_id = _archivio_con_spedizione(db)
        sigillo = SealingRecord(
            ok=True,
            expected=("A", "B"),
            found=("A", "B"),
            passes_run=4,
            closure_proof=ClosureProof.OPERATOR,
            operator="dvent",
        )
        distinta = build_manifest(db, shipment_id, sealing=sigillo)
        assert distinta.sealing is not None
        assert distinta.sealing["closure_proof"] == "operator"
        assert distinta.sealing["passes_run"] == 4


# --------------------------------------------------------------------------
# Sigillo crittografico del documento
# --------------------------------------------------------------------------
def test_giro_completo_di_cifratura() -> None:
    portachiavi = Keyring({0: CHIAVE})
    originale = _manifest()
    blob = seal_manifest(originale, portachiavi)
    riletta = open_manifest(blob, portachiavi)

    assert riletta.lab_id == originale.lab_id
    assert riletta.destination == originale.destination
    assert riletta.epcs == originale.epcs
    assert riletta.entries[0].codice_fiscale == CF


def test_i_dati_del_paziente_non_sono_in_chiaro_nel_file() -> None:
    blob = seal_manifest(_manifest(), Keyring({0: CHIAVE}))
    assert CF.encode() not in blob
    assert b"DELLA VALLE" not in blob
    # L'intestazione invece resta leggibile: serve a riconoscere il file e a
    # sapere quale chiave chiedere.
    assert blob.startswith(MANIFEST_MAGIC)


def test_chiave_errata_fallisce_autenticazione() -> None:
    blob = seal_manifest(_manifest(), Keyring({0: CHIAVE}))
    _expect(
        PayloadAuthenticationError,
        lambda: open_manifest(blob, Keyring({0: ALTRA_CHIAVE})),
        "chiave sbagliata",
    )


def test_chiave_mancante_distinta_dal_file_manomesso() -> None:
    blob = seal_manifest(_manifest(), Keyring({0: CHIAVE, 5: ALTRA_CHIAVE}), key_id=5)
    _expect(
        UnknownKeyError,
        lambda: open_manifest(blob, Keyring({0: CHIAVE})),
        "chiave non posseduta",
    )


def test_file_manomesso_rilevato() -> None:
    blob = bytearray(seal_manifest(_manifest(), Keyring({0: CHIAVE})))
    blob[-5] ^= 0x01
    _expect(
        PayloadAuthenticationError,
        lambda: open_manifest(bytes(blob), Keyring({0: CHIAVE})),
        "contenuto alterato",
    )


def test_intestazione_manomessa_rilevata() -> None:
    # Cambiare il key_id per far provare un'altra chiave deve fallire, non riuscire.
    portachiavi = Keyring({0: CHIAVE, 1: ALTRA_CHIAVE})
    blob = bytearray(seal_manifest(_manifest(), portachiavi, key_id=0))
    blob[len(MANIFEST_MAGIC) + 1] = 1
    _expect(
        PayloadAuthenticationError,
        lambda: open_manifest(bytes(blob), portachiavi),
        "intestazione alterata",
    )


def test_file_estraneo_riconosciuto_come_tale() -> None:
    _expect(
        PayloadFormatError,
        lambda: open_manifest(b"{ \"non\": \"una distinta\" }" + b"\x00" * 40, Keyring({0: CHIAVE})),
        "file di altro tipo",
    )
    _expect(
        PayloadFormatError,
        lambda: open_manifest(b"corto", Keyring({0: CHIAVE})),
        "file troppo corto",
    )


def test_versione_futura_rifiutata() -> None:
    blob = bytearray(seal_manifest(_manifest(), Keyring({0: CHIAVE})))
    blob[len(MANIFEST_MAGIC)] = 9
    _expect(
        PayloadFormatError,
        lambda: open_manifest(bytes(blob), Keyring({0: CHIAVE})),
        "schema sconosciuto",
    )


def test_nonce_diverso_a_ogni_sigillo() -> None:
    portachiavi = Keyring({0: CHIAVE})
    distinta = _manifest()
    primo = seal_manifest(distinta, portachiavi)
    secondo = seal_manifest(distinta, portachiavi)
    assert primo != secondo, "due sigilli della stessa distinta non devono coincidere"


def test_distinta_su_file_e_riletta() -> None:
    portachiavi = Keyring({0: CHIAVE})
    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "distinta.bin"
        percorso.write_bytes(seal_manifest(_manifest(), portachiavi))
        riletta = open_manifest(percorso.read_bytes(), portachiavi)
        assert len(riletta.entries) == 3


# --------------------------------------------------------------------------
# Riconciliazione
# --------------------------------------------------------------------------
def test_riconciliazione_completa() -> None:
    distinta = _manifest()
    esito = reconcile(distinta, distinta.epcs)
    assert esito.ok is True
    assert esito.counts == (3, 3)
    assert esito.missing == () and esito.unexpected == ()


def test_riconciliazione_con_un_mancante() -> None:
    distinta = _manifest()
    arrivati = [voce.epc for voce in distinta.entries[:2]]
    esito = reconcile(distinta, arrivati)

    assert esito.ok is False
    assert esito.counts == (2, 3)
    assert esito.missing == (distinta.entries[2].epc,)
    # Un EPC da solo non dice niente a nessuno: serve nome e numerazione.
    descrizione = esito.describe_missing()[0]
    assert "3/3" in descrizione and "DELLA VALLE" in descrizione and "987654" in descrizione


def test_un_contenitore_in_piu_e_un_errore_non_un_bonus() -> None:
    distinta = _manifest()
    arrivati = list(distinta.epcs) + ["0100A5000F99999999FFFFFF"]
    esito = reconcile(distinta, arrivati)

    assert esito.ok is False, "un contenitore inatteso e' un errore di spedizione"
    assert esito.unexpected == ("0100A5000F99999999FFFFFF",)
    assert esito.missing == ()


def test_riconciliazione_normalizza_maiuscole_e_spazi() -> None:
    distinta = _manifest(1)
    esito = reconcile(distinta, [f"  {distinta.entries[0].epc.lower()}  "])
    assert esito.ok is True


def test_riconciliazione_serializzabile() -> None:
    import json

    distinta = _manifest()
    esito = reconcile(distinta, distinta.epcs[:1])
    documento = esito.to_dict()
    json.dumps(documento)
    assert documento["arrivati"] == 1 and documento["attesi"] == 3
    assert len(documento["mancanti_descritti"]) == 2


def test_percorso_completo_archivio_distinta_arrivo() -> None:
    """Mittente e destinatario, con la sola chiave condivisa fra loro."""
    portachiavi_mittente = Keyring({0: CHIAVE})
    with LimsDatabase() as db:
        shipment_id = _archivio_con_spedizione(db, totale=3)
        distinta = build_manifest(db, shipment_id, lab_id=0x00A5, operator="dvent")
        blob = seal_manifest(distinta, portachiavi_mittente)

    # Laboratorio destinatario: ha solo la chiave e il file.
    portachiavi_destinatario = Keyring({0: CHIAVE})
    ricevuta = open_manifest(blob, portachiavi_destinatario)
    letti = [voce.epc for voce in ricevuta.entries[:2]]   # uno non arriva

    esito = reconcile(ricevuta, letti)
    assert esito.counts == (2, 3)
    assert len(esito.missing) == 1
    assert "DELLA VALLE" in esito.describe_missing()[0]


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print(f"{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
