"""Percorso completo di un campione, dall'accettazione alla ricezione.

Riproduce senza hardware il criterio di accettazione del progetto: tre
contenitori della stessa accettazione vengono scritti, spediti e riletti; se uno
non arriva, deve risultare mancante. E' il test che dice se il sistema fa il suo
mestiere, al di la' della correttezza dei singoli pezzi.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag

from lims.codec import SpecimenFlags, TagPayload
from lims.crypto import KEY_SIZE, Keyring
from lims.db import LimsDatabase
from lims.model import Case, ContainerState, Patient, Sex, Shipment, ShipmentState, Specimen
from lims.tagio import TagIO

CF_PAZIENTE = "MRTMTT25D09F205Z"
CHIAVE_MITTENTE = bytes(range(KEY_SIZE))
TID_BASE = bytes.fromhex("E2801190200050A1B2C300")


def _tag_vergine(indice: int) -> SimulatedTag:
    """Tag di fabbrica: EPC generico e TID diverso per esemplare."""
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=TID_BASE + bytes([indice]),
        user_bytes=64,
    )


def _accetta(db: LimsDatabase, *, totale: int) -> tuple[int, list[int], Patient]:
    """Registra paziente, accettazione e reperto; pianifica i contenitori."""
    paziente = Patient(
        codice_fiscale=CF_PAZIENTE,
        cognome="Della Valle",
        nome="Gianfranco",
        data_nascita=date(1985, 4, 9),
        sesso=Sex.MALE,
    )
    patient_id = db.upsert_patient(paziente)
    accession_id = db.next_accession_id()
    case_id = db.create_case(
        Case(
            accession_id=accession_id,
            patient_id=patient_id,
            data_prelievo=date(2026, 8, 15),
            reparto="Chirurgia generale",
            medico="Dott.ssa Bianchi",
        )
    )
    specimen_id = db.add_specimen(
        Specimen(
            case_id=case_id,
            descrizione="Nodulo mammario quadrante supero-esterno",
            material_code=3,      # pezzo operatorio
            fixative_code=1,      # formalina 10% tamponata
            site_code=2,          # mammella
        )
    )
    return accession_id, db.plan_containers(specimen_id, totale), paziente


def _payload(paziente: Patient, accession_id: int, indice: int, totale: int) -> TagPayload:
    return TagPayload(
        codice_fiscale=paziente.codice_fiscale,
        accession_id=accession_id,
        container_index=indice,
        container_total=totale,
        display_name=paziente.display_name,
        data_prelievo=date(2026, 8, 15),
        material_code=3,
        fixative_code=1,
        site_code=2,
        flags=SpecimenFlags.INFECTIOUS,
    )


def _scrivi_serie(db: LimsDatabase, totale: int = 3):
    """Scrive `totale` contenitori, uno per volta come alla postazione reale."""
    accession_id, container_ids, paziente = _accetta(db, totale=totale)
    scritti = []
    for indice, container_id in enumerate(container_ids, start=1):
        # Un solo tag per volta davanti all'antenna: e' il vincolo dell'opzione 0x05.
        postazione = FakeTagBackend([_tag_vergine(indice)])
        tagio = TagIO(
            postazione,
            Keyring({0: CHIAVE_MITTENTE}),
            lab_id=0x00A5,
            antennas=(1,),
            db=db,
            operator="accettazione",
        )
        esito = tagio.provision(
            _payload(paziente, accession_id, indice, totale), container_id=container_id
        )
        assert esito.ok, f"contenitore {indice}: {esito.error}"
        scritti.append((esito, postazione.only_tag))
    return accession_id, container_ids, paziente, scritti


def test_percorso_completo_con_spedizione_completa() -> None:
    with LimsDatabase() as db:
        accession_id, container_ids, paziente, scritti = _scrivi_serie(db)

        # Tutti i contenitori risultano scritti e verificati nell'archivio.
        registrati = db.containers_for_accession(accession_id)
        assert [record.label for record in registrati] == ["1/3", "2/3", "3/3"]
        assert all(record.state == ContainerState.PROVISIONED for record in registrati)
        assert len({record.epc for record in registrati}) == 3, "EPC tutti distinti"
        assert len({record.tid for record in registrati}) == 3, "TID tutti distinti"

        # Spedizione.
        shipment_id = db.create_shipment(
            Shipment(destinazione="Laboratorio Centrale", data=date(2026, 8, 16))
        )
        db.add_to_shipment(shipment_id, container_ids)
        db.set_shipment_state(shipment_id, ShipmentState.SENT)
        assert len(db.shipment_contents(shipment_id)) == 3

        # Ricezione: la scatola nel volume, con la sola chiave condivisa.
        volume = FakeTagBackend([tag for _, tag in scritti])
        destinatario = TagIO(
            volume, Keyring({0: CHIAVE_MITTENTE}), antennas=(1, 2, 3), operator="ricezione"
        )
        rilievo = destinatario.survey_field()

        assert rilievo.ok
        assert rilievo.complete is True, f"mancanti: {[m.label for m in rilievo.missing]}"
        assert len(rilievo.known) == 3
        assert rilievo.foreign == []
        gruppi = rilievo.by_accession()
        assert list(gruppi) == [accession_id]
        assert [item.epc_info.container_index for item in gruppi[accession_id]] == [1, 2, 3]


def test_un_contenitore_mancante_viene_rilevato() -> None:
    """Il caso che il sistema deve intercettare: la scatola arriva incompleta."""
    with LimsDatabase() as db:
        accession_id, _, _, scritti = _scrivi_serie(db)

        # Il contenitore 2 non arriva a destinazione.
        volume = FakeTagBackend([scritti[0][1], scritti[2][1]])

        rilievo = TagIO(volume, Keyring({0: CHIAVE_MITTENTE})).survey_field()
        assert rilievo.complete is False
        assert [mancante.label for mancante in rilievo.missing] == [
            f"accettazione {accession_id}, contenitore 2/3"
        ]
        assert len(rilievo.known) == 2


def test_assenza_totale_di_un_accettazione_rilevata_solo_con_la_distinta() -> None:
    """Il caso cieco della deduzione automatica: non arriva NESSUN contenitore.

    Senza distinta l'accettazione non compare fra gli attesi — perche' gli attesi
    si deducono dagli EPC letti — e l'assenza totale passerebbe inosservata.
    """
    with LimsDatabase() as db:
        _, _, _, scritti = _scrivi_serie(db, totale=3)
        attesi = [esito.epc for esito, _ in scritti]

        volume = FakeTagBackend([])   # la scatola arriva vuota
        destinatario = TagIO(volume, Keyring({0: CHIAVE_MITTENTE}))

        alla_cieca = destinatario.survey_field()
        assert alla_cieca.missing == [], "senza distinta non c'e' nulla da cui dedurre"

        con_distinta = destinatario.survey_field(expected_epcs=attesi)
        assert len(con_distinta.missing) == 3
        assert con_distinta.complete is False


def test_il_destinatario_senza_chiave_vede_comunque_i_mancanti() -> None:
    """Il controllo di completezza non richiede la chiave: sta tutto nell'EPC."""
    with LimsDatabase() as db:
        accession_id, _, _, scritti = _scrivi_serie(db)
        volume = FakeTagBackend([scritti[0][1], scritti[1][1]])

        senza_chiave = TagIO(volume, Keyring())
        rilievo = senza_chiave.survey_field()

        assert rilievo.complete is False
        assert [mancante.label for mancante in rilievo.missing] == [
            f"accettazione {accession_id}, contenitore 3/3"
        ]
        # I dati del paziente restano inaccessibili, come deve essere.
        assert all(osservazione.payload is None for osservazione in rilievo.observations)


def test_il_destinatario_con_la_chiave_ricostruisce_il_campione() -> None:
    with LimsDatabase() as db:
        accession_id, _, paziente, scritti = _scrivi_serie(db, totale=1)
        volume = FakeTagBackend([scritti[0][1]])

        osservazione = TagIO(
            volume, Keyring({0: CHIAVE_MITTENTE}), antennas=(1, 2, 3)
        ).survey_field().observations[0]

        assert osservazione.status == "decodificato", osservazione.detail
        campione = osservazione.payload
        assert campione is not None
        assert campione.codice_fiscale == CF_PAZIENTE
        assert campione.display_name.startswith("DELLA VALLE")
        assert campione.accession_id == accession_id
        assert campione.container_index == 1 and campione.container_total == 1
        assert campione.data_prelievo == date(2026, 8, 15)

        descritto = campione.describe()
        assert descritto["materiale"] == "pezzo operatorio"
        assert descritto["fissativo"] == "formalina 10% tamponata"
        assert descritto["sede"] == "mammella"
        # L'avvertenza di rischio biologico deve arrivare a chi apre il contenitore.
        assert "INFECTIOUS" in descritto["avvertenze"]


def test_chiave_diversa_non_apre_i_tag_di_un_altro_circuito() -> None:
    with LimsDatabase() as db:
        _, _, _, scritti = _scrivi_serie(db, totale=1)
        volume = FakeTagBackend([scritti[0][1]])

        altro_laboratorio = Keyring({0: bytes(range(200, 200 + KEY_SIZE))})
        osservazione = TagIO(volume, altro_laboratorio).survey_field().observations[0]
        assert osservazione.status == "non_autenticato"
        assert osservazione.payload is None


def test_tag_estraneo_nella_scatola_viene_segnalato() -> None:
    with LimsDatabase() as db:
        _, _, _, scritti = _scrivi_serie(db, totale=1)
        estraneo = SimulatedTag(bytes.fromhex("300833B2DDD9014035050000"), user_bytes=64)
        volume = FakeTagBackend([scritti[0][1], estraneo])

        rilievo = TagIO(volume, Keyring({0: CHIAVE_MITTENTE})).survey_field()
        assert len(rilievo.foreign) == 1
        assert rilievo.foreign[0].status == "estraneo"
        assert len(rilievo.known) == 1


def test_l_archivio_conserva_la_traccia_delle_operazioni() -> None:
    with LimsDatabase() as db:
        accession_id, _, _, scritti = _scrivi_serie(db)
        for esito, _ in scritti:
            eventi = db.events_for_epc(esito.epc)
            assert len(eventi) == 1
            assert eventi[0]["operation"] == "provision"
            assert eventi[0]["ok"] == 1
            assert eventi[0]["operator"] == "accettazione"
            assert eventi[0]["tid"] == esito.tid

        # Dall'EPC letto a destinazione si risale al contenitore e al paziente.
        record = db.find_container_by_epc(scritti[0][0].epc)
        assert record is not None
        assert record.accession_id == accession_id
        assert record.codice_fiscale == CF_PAZIENTE
        assert record.label == "1/3"


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
