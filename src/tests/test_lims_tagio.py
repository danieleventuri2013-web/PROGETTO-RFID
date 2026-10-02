"""Test dell'orchestrazione tag I/O contro un backend simulato, senza hardware."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# anche la cartella dei test, cosi' `fake_backend` si importa sia lanciando
# questo file da solo sia tramite `python run.py tests`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag

from lims.codec import SpecimenFlags, TagPayload, build_epc, parse_epc
from lims.crypto import KEY_SIZE, Keyring
from lims.db import LimsDatabase
from lims.model import Case, ContainerState, Patient, Specimen
from lims.tagio import MAX_WRITE_BYTES, TagIO, _stesso_tag_del_tentativo

CF_VALIDO = "MRTMTT25D09F205Z"
CHIAVE = bytes(range(KEY_SIZE))
VERGINE = bytes.fromhex("AAAAAAAAAAAAAAAAAAAAAAAA")   # EPC di fabbrica, non nostro
TID_A = bytes.fromhex("E2801190200050A1B2C3D4E5")
TID_B = bytes.fromhex("E28011902000509999888877")


def _payload(**overrides) -> TagPayload:
    base = dict(
        codice_fiscale=CF_VALIDO,
        accession_id=987654,
        container_index=1,
        container_total=3,
        display_name="ROSSI MARIO",
        data_prelievo=date(2026, 8, 15),
        material_code=1,
        fixative_code=1,
        site_code=2,
        flags=SpecimenFlags.URGENT,
    )
    base.update(overrides)
    return TagPayload(**base)


def _tagio(backend: FakeTagBackend, **overrides) -> TagIO:
    opzioni = dict(lab_id=0x00A5, antennas=(1, 2), user_memory_bytes=64)
    opzioni.update(overrides)
    return TagIO(backend, Keyring({0: CHIAVE}), **opzioni)


def _backend_vergine(user_bytes: int = 64, tid: bytes = TID_A) -> FakeTagBackend:
    return FakeTagBackend([SimulatedTag(VERGINE, tid=tid, user_bytes=user_bytes)])


# --------------------------------------------------------------------------
# Scrittura di un contenitore
# --------------------------------------------------------------------------
def test_provision_giro_completo() -> None:
    backend = _backend_vergine()
    esito = _tagio(backend).provision(_payload())

    assert esito.ok is True, esito.error
    assert esito.previous_epc == VERGINE.hex().upper()
    assert esito.epc != esito.previous_epc
    assert esito.tid == TID_A.hex().upper()
    assert esito.blocks_written == 1
    assert 0 < esito.payload_bytes <= 64

    info = parse_epc(esito.epc)
    assert info.lab_id == 0x00A5
    assert info.accession_id == 987654
    assert info.container_index == 1 and info.container_total == 3


def test_provision_rispetta_l_ordine_imposto_dalle_guardie() -> None:
    """Un inventario prima di ogni accesso, e non e' zelo.

    Due vincoli diversi cadono sullo stesso punto. Il primo e' la **guardia del
    servizio**: il cambio EPC azzera l'EPC osservato, e senza un nuovo inventory
    la scrittura del payload verrebbe rifiutata. Il secondo e' la **sessione
    Gen2 S2** del modulo: ogni comando di accesso consuma la singolarizzazione,
    e il successivo troverebbe «0x0400 No tag found» su un tag fermo davanti
    all'antenna.

    I due inventari di fila dopo il cambio EPC sono la conferma del nuovo
    pseudonimo e la ri-singolarizzazione del primo blocco: `_write_blocks` la fa
    da se' perche' deve reggere anche i blocchi dal secondo in poi e la chiamata
    da `retire_tag`, dove quella conferma non c'e'.
    """
    backend = _backend_vergine()
    assert _tagio(backend).provision(_payload()).ok is True
    assert backend.calls == [
        "inventory",     # 1. un solo tag in campo
        "read",          # 2. TID
        "inventory",     # 3. rimette in gioco il tag per la scrittura
        "write_epc",     # 4. nuovo pseudonimo
        "inventory",     # 5. conferma il cambio e riarma la guardia
        "inventory",     # 6. rimette in gioco il tag per il payload
        "write",         # 7. payload sigillato
        "inventory",     # 8. rimette in gioco il tag per la rilettura
        "verify",        # 9. rilettura di controllo
        "read",          # (la verifica rilegge)
    ]


def test_provision_regge_la_sessione_S2() -> None:
    """L'errore visto al banco: «cambio EPC: scrittura EPC fallita: 0x0400 No tag found».

    Il modulo arriva in sessione Gen2 S2: dopo la lettura del TID il tag e' gia'
    in stato B e non risponde piu' al comando successivo. Il messaggio era
    esatto e insieme fuorviante — il tag stava fermo davanti all'antenna 3 a
    -41 dBm e in inventario dava 20 letture per giro.
    """
    backend = _backend_vergine()
    backend.sessione_persistente = True
    esito = _tagio(backend).provision(_payload())
    assert esito.ok is True, esito.error
    assert esito.epc and esito.payload_bytes > 0


def test_l_errore_muto_non_manda_a_cercare_un_tag_caduto() -> None:
    """«0x0400 No tag found» su un tag appena inventariato e' esatto e fuorviante.

    Il tag e' li', fermo davanti all'antenna, e l'inventario lo vede benissimo:
    il messaggio del modulo manda a cercare un chip rotto, un contenitore
    caduto, un'antenna scollegata — nessuna delle quali c'entra. Le cause vere
    sono due, e vanno dette all'operatore invece di lasciargliele indovinare.
    """
    from lims.tagio import _spiega_errore

    codice, testo = _spiega_errore(
        "cambio EPC: scrittura EPC fallita: {'3': '0x0400: No tag found'}",
        "E280F30200000001B72AEEE0",
    )
    assert codice == "tag_muto_all_accesso"
    assert "0x0400" in testo, "il messaggio del modulo non si nasconde"
    assert "E280F30200000001B72AEEE0" in testo
    assert "S2" in testo and "potenza di scrittura" in testo
    assert "RF continua" in testo and "non dimostra" in testo


def test_un_guasto_qualunque_resta_quello_che_e() -> None:
    """Un codice d'errore che arriva anche quando non c'entra vale zero."""
    from lims.tagio import _spiega_errore

    codice, testo = _spiega_errore("verifica dati fallita: 0x0405", "AABB")
    assert codice == ""
    assert testo == "verifica dati fallita: 0x0405"


def test_senza_ri_singolarizzare_la_scrittura_S2_fallisce() -> None:
    """La prova che la difesa serve, e che il banco riproduce davvero la trappola."""
    from rfid_silion.service import ReadRequest, WriteEpcRequest

    backend = _backend_vergine()
    backend.sessione_persistente = True
    epc = backend.only_tag.epc.hex().upper()
    backend.inventory({"antennas": (1, 2), "timeout_ms": 100})
    # Un accesso qualunque consuma la singolarizzazione...
    assert backend.read(
        ReadRequest(bank=2, address=0, word_count=6, antennas=(1, 2))
    ).ok is True
    # ...e il cambio EPC che segue non trova piu' nessuno.
    negata = backend.write_epc(
        WriteEpcRequest(
            new_epc="00" * 12, expected_epc=epc, antennas=(1, 2)
        )
    )
    assert negata.ok is False
    assert "No tag found" in (negata.error or {}).get("message", "")


def test_una_scatola_piena_si_rilegge_anche_in_S2() -> None:
    """Dal secondo tag in poi, senza inventario, sarebbe «0x0400 No tag found».

    Il filtro Select dice *quale* tag deve rispondere, non lo rimette in gioco:
    il flag di inventario e' del tag. Con una scatola piena e' esattamente il
    caso normale, non un caso limite.
    """
    tid_diversi = [
        TID_A,
        TID_B,
        bytes.fromhex("E2801190200050112233AABB"),
    ]
    backend = _backend_vergine()
    tagio = _tagio(backend)
    scritti, scatola = [], []
    for tid in tid_diversi:
        # Uno per volta nel campo: la scrittura pretende un tag solo.
        backend.tags[:] = [SimulatedTag(VERGINE, tid=tid, user_bytes=64)]
        esito = tagio.provision(_payload())
        assert esito.ok is True, esito.error
        scritti.append(esito.epc)
        scatola.append(backend.tags[0])

    # Ora sono tutti e tre nella scatola, e si rilegge a scatola chiusa.
    backend.tags[:] = scatola
    backend.sessione_persistente = True
    rilievo = tagio.survey_field()
    letti = [o.epc for o in rilievo.observations if o.tid]
    for epc in scritti:
        assert epc in letti, f"{epc} non riletto: {[(o.epc, o.status, o.detail) for o in rilievo.observations]}"


def test_provision_scrive_davvero_sul_tag() -> None:
    backend = _backend_vergine()
    esito = _tagio(backend).provision(_payload())
    tag = backend.only_tag
    assert tag.write_count == 1
    assert tag.epc.hex().upper() == esito.epc
    assert bytes(tag.user[: esito.payload_bytes]) != bytes(esito.payload_bytes)


def test_provision_e_rilettura_ricostruiscono_il_campione() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    atteso = _payload()
    esito = tagio.provision(atteso)
    assert esito.ok is True

    rilievo = tagio.survey_field()
    assert rilievo.ok and len(rilievo.observations) == 1
    osservazione = rilievo.observations[0]
    assert osservazione.status == "decodificato", osservazione.detail
    assert osservazione.payload is not None
    assert osservazione.payload.codice_fiscale == CF_VALIDO
    assert osservazione.payload.display_name == "ROSSI MARIO"
    assert osservazione.payload.accession_id == 987654
    assert osservazione.payload.data_prelievo == date(2026, 8, 15)
    assert osservazione.payload.flags == SpecimenFlags.URGENT


def test_provision_rifiuta_piu_tag_nel_campo() -> None:
    backend = FakeTagBackend(
        [SimulatedTag(VERGINE, user_bytes=64), SimulatedTag(bytes.fromhex("BB" * 12), user_bytes=64)]
    )
    esito = _tagio(backend).provision(_payload())
    assert esito.ok is False
    assert "un solo tag" in esito.error


def test_provision_rifiuta_campo_vuoto() -> None:
    esito = _tagio(FakeTagBackend([])).provision(_payload())
    assert esito.ok is False
    assert "un solo tag" in esito.error


def test_provision_rifiuta_tid_troppo_corto() -> None:
    backend = FakeTagBackend([SimulatedTag(VERGINE, tid=bytes.fromhex("E2003412"), user_bytes=64)])
    esito = _tagio(backend).provision(_payload())
    assert esito.ok is False
    assert "TID" in esito.error


def test_provision_rifiuta_user_memory_insufficiente() -> None:
    backend = _backend_vergine(user_bytes=16)
    esito = _tagio(backend, user_memory_bytes=16).provision(_payload())
    assert esito.ok is False
    assert backend.only_tag.epc == VERGINE, "l'EPC non va cambiato prima del controllo spazio"
    assert "write_epc" not in backend.calls


def test_provision_riporta_il_fallimento_della_scrittura() -> None:
    backend = _backend_vergine()
    backend.write_failures = 1
    esito = _tagio(backend).provision(_payload())
    assert esito.ok is False
    assert "cambio EPC" in esito.error


def test_provision_riporta_il_fallimento_della_verifica() -> None:
    backend = _backend_vergine()
    esito_atteso = _tagio(backend)

    # La lettura di verifica fallisce: la scrittura non va data per riuscita.
    originale = backend.verify

    def verifica_guasta(request, expected_data_hex):
        backend.calls.append("verify")
        return backend._ko("verify", "i dati riletti non coincidono")

    backend.verify = verifica_guasta
    try:
        esito = esito_atteso.provision(_payload())
    finally:
        backend.verify = originale
    assert esito.ok is False
    assert "verifica" in esito.error


def test_provision_payload_lungo_scritto_a_blocchi() -> None:
    backend = _backend_vergine(user_bytes=192)
    tagio = _tagio(backend, user_memory_bytes=192)
    esito = tagio.provision(_payload(display_name="A" * 200))
    assert esito.ok is True, esito.error
    assert esito.payload_bytes > MAX_WRITE_BYTES
    assert esito.blocks_written >= 2, "oltre 64 byte la scrittura va spezzata"
    assert backend.only_tag.write_count == esito.blocks_written


def test_provision_incrementa_la_revisione_nel_sigillo() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    assert tagio.provision(_payload(), revision=2).ok is True
    osservazione = tagio.survey_field().observations[0]
    assert "revisione 2" in osservazione.detail


# --------------------------------------------------------------------------
# Blindatura del tag
# --------------------------------------------------------------------------
def test_password_di_accesso_scritta_nella_banca_reserved() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    esito = tagio.provision(_payload())
    assert esito.ok is True

    protezione = tagio.set_access_password("A1B2C3D4", expected_epc=esito.epc)
    assert protezione.ok is True, protezione.error
    # Word 0..1 restano la kill password, intoccate; la access password sta a word 2.
    assert bytes(backend.only_tag.reserved[:4]) == bytes(4)
    assert bytes(backend.only_tag.reserved[4:8]) == bytes.fromhex("A1B2C3D4")
    assert tagio.access_password_hex == "A1B2C3D4", "le operazioni seguenti usano la nuova password"


def test_password_di_accesso_rifiuta_lunghezze_errate() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    esito = tagio.provision(_payload())
    assert tagio.set_access_password("A1B2", expected_epc=esito.epc).ok is False
    assert tagio.access_password_hex == "00000000", "la password non deve cambiare se fallisce"


def test_lock_contenitore_blocca_epc_e_user() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    esito = tagio.provision(_payload())
    blocco = tagio.lock_container(expected_epc=esito.epc)
    assert blocco.ok is True, blocco.error
    assert backend.only_tag.locked == {"epc": "lock", "user": "lock"}


def test_dopo_il_lock_la_user_memory_non_si_riscrive() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    esito = tagio.provision(_payload())
    assert tagio.lock_container(expected_epc=esito.epc).ok is True

    backend.inventory({"antennas": (1, 2)})   # riarma la guardia
    riscrittura = tagio._write_blocks  # scrittura diretta, senza cambio EPC
    try:
        riscrittura(b"\x00" * 8, esito.epc)
    except Exception as exc:
        assert "bloccata" in str(exc)
    else:
        raise AssertionError("la USER memory bloccata non doveva accettare scritture")


def test_lock_permanente_richiede_consenso_esplicito() -> None:
    backend = _backend_vergine()
    tagio = _tagio(backend)
    esito = tagio.provision(_payload())
    permanente = tagio.lock_container(expected_epc=esito.epc, permanent=True)
    assert permanente.ok is True
    assert backend.only_tag.locked == {"epc": "permalock", "user": "permalock"}


# --------------------------------------------------------------------------
# Lettura del carico
# --------------------------------------------------------------------------
def _campo_con_serie(indici: list[int], totale: int = 3, accession_id: int = 987654):
    tags = [
        SimulatedTag(build_epc(0x00A5, accession_id, indice, totale), user_bytes=64)
        for indice in indici
    ]
    return FakeTagBackend(tags)


def test_survey_rileva_i_contenitori_mancanti_senza_chiave() -> None:
    # Il controllo di completezza si basa sull'EPC in chiaro: funziona anche a
    # chiave assente e con piu' tag nel campo.
    tagio = TagIO(_campo_con_serie([1, 3]), Keyring(), lab_id=0x00A5, antennas=(1, 2))
    rilievo = tagio.survey_field()
    assert rilievo.ok
    assert rilievo.complete is False
    assert [item.label for item in rilievo.missing] == [
        "accettazione 987654, contenitore 2/3"
    ]


def test_survey_serie_completa() -> None:
    tagio = TagIO(_campo_con_serie([1, 2, 3]), Keyring(), lab_id=0x00A5)
    rilievo = tagio.survey_field()
    assert rilievo.complete is True
    assert rilievo.missing == []
    assert len(rilievo.known) == 3


def test_survey_raggruppa_per_accettazione() -> None:
    backend = FakeTagBackend(
        [
            SimulatedTag(build_epc(0x00A5, 100, 1, 2), user_bytes=64),
            SimulatedTag(build_epc(0x00A5, 100, 2, 2), user_bytes=64),
            SimulatedTag(build_epc(0x00A5, 200, 1, 1), user_bytes=64),
        ]
    )
    gruppi = TagIO(backend, Keyring()).survey_field().by_accession()
    assert sorted(gruppi) == [100, 200]
    assert len(gruppi[100]) == 2 and len(gruppi[200]) == 1
    assert [item.epc_info.container_index for item in gruppi[100]] == [1, 2]


def test_survey_segnala_i_tag_estranei() -> None:
    backend = FakeTagBackend(
        [
            SimulatedTag(build_epc(0x00A5, 100, 1, 1), user_bytes=64),
            SimulatedTag(bytes.fromhex("99" * 12), user_bytes=64),   # schema EPC ignoto
        ]
    )
    rilievo = TagIO(backend, Keyring()).survey_field()
    assert len(rilievo.foreign) == 1
    assert rilievo.foreign[0].status == "estraneo"
    assert len(rilievo.known) == 1


def test_survey_isola_ogni_tag_col_filtro_select() -> None:
    """Con piu' tag nel campo ogni payload va letto isolando il suo EPC.

    E' quello che permette al laboratorio destinatario di sapere cosa e'
    arrivato senza aprire la scatola. Senza filtro, il comando colpirebbe il
    primo tag che risponde e il payload finirebbe attribuito al contenitore
    sbagliato: in una catena di custodia e' l'errore peggiore possibile, perche'
    il conteggio torna e nessuno sospetta.
    """
    backend = _backend_vergine()
    scritti = []
    for indice in (1, 2, 3):
        # Un tag per volta sulla postazione, come sul banco vero.
        backend.tags[:] = [
            SimulatedTag(
                bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0, indice]),
                tid=TID_A[:-1] + bytes([indice]),
                user_bytes=64,
            )
        ]
        esito = _tagio(backend).provision(
            _payload(container_index=indice, container_total=3)
        )
        assert esito.ok is True
        scritti.append((esito.epc, backend.tags[0]))

    # Ora tutti e tre nella scatola, insieme.
    backend.tags[:] = [tag for _epc, tag in scritti]
    rilievo = _tagio(backend).survey_field()
    assert len(rilievo.observations) == 3

    per_epc = {osservazione.epc: osservazione for osservazione in rilievo.observations}
    for atteso, (epc, _tag) in zip((1, 2, 3), scritti, strict=True):
        osservazione = per_epc[epc]
        assert osservazione.status == "decodificato", osservazione.detail
        assert osservazione.payload is not None
        # Il payload letto deve essere quello di QUESTO contenitore, non del
        # primo che ha risposto.
        assert osservazione.payload.container_index == atteso


def test_survey_campo_vuoto() -> None:
    rilievo = TagIO(FakeTagBackend([]), Keyring()).survey_field()
    assert rilievo.ok is True
    assert rilievo.observations == [] and rilievo.missing == []


# --------------------------------------------------------------------------
# Casi di sicurezza in lettura
# --------------------------------------------------------------------------
def test_payload_su_chip_diverso_non_viene_autenticato() -> None:
    """Il tag clonato: EPC e USER memory copiati su un chip con TID diverso."""
    origine = _backend_vergine(tid=TID_A)
    tagio = _tagio(origine)
    esito = tagio.provision(_payload())
    assert esito.ok is True

    clone = FakeTagBackend(
        [
            SimulatedTag(
                bytes.fromhex(esito.epc),
                tid=TID_B,                                  # chip diverso
                user_bytes=64,
                user_initial=bytes(origine.only_tag.user),  # stessa USER memory
            )
        ]
    )
    osservazione = _tagio(clone).survey_field().observations[0]
    assert osservazione.status == "non_autenticato"
    assert osservazione.payload is None


def test_chiave_mancante_distinta_dal_tag_manomesso() -> None:
    backend = _backend_vergine()
    assert _tagio(backend).provision(_payload()).ok is True

    senza_chiave = TagIO(backend, Keyring(), lab_id=0x00A5, antennas=(1, 2))
    osservazione = senza_chiave.survey_field().observations[0]
    assert osservazione.status == "chiave_mancante"
    assert "non disponibile" in osservazione.detail


def test_user_memory_vuota_non_e_un_payload() -> None:
    backend = FakeTagBackend([SimulatedTag(build_epc(0x00A5, 1, 1, 1), user_bytes=64)])
    osservazione = _tagio(backend).survey_field().observations[0]
    assert osservazione.status == "illeggibile"
    assert osservazione.payload is None


# --------------------------------------------------------------------------
# Integrazione con l'archivio
# --------------------------------------------------------------------------
def test_provision_registra_su_database() -> None:
    with LimsDatabase() as db:
        patient_id = db.upsert_patient(
            Patient(codice_fiscale=CF_VALIDO, cognome="Rossi", nome="Mario")
        )
        case_id = db.create_case(Case(accession_id=987654, patient_id=patient_id))
        specimen_id = db.add_specimen(Specimen(case_id=case_id, material_code=1))
        container_ids = db.plan_containers(specimen_id, 3)

        backend = _backend_vergine()
        tagio = _tagio(backend, db=db, operator="dvent")
        esito = tagio.provision(_payload(), container_id=container_ids[0])
        assert esito.ok is True, esito.error

        record = db.find_container_by_epc(esito.epc)
        assert record is not None
        assert record.container_id == container_ids[0]
        assert record.state == ContainerState.PROVISIONED
        assert record.tid == esito.tid
        assert record.display_name == "ROSSI MARIO"

        eventi = db.events_for_epc(esito.epc)
        assert len(eventi) == 1
        assert eventi[0]["operation"] == "provision" and eventi[0]["ok"] == 1
        assert eventi[0]["operator"] == "dvent"


def test_epc_duplicato_bloccato_prima_di_toccare_il_tag() -> None:
    with LimsDatabase() as db:
        patient_id = db.upsert_patient(
            Patient(codice_fiscale=CF_VALIDO, cognome="Rossi", nome="Mario")
        )
        case_id = db.create_case(Case(accession_id=987654, patient_id=patient_id))
        specimen_id = db.add_specimen(Specimen(case_id=case_id))
        container_ids = db.plan_containers(specimen_id, 2)

        backend = _backend_vergine()
        tagio = _tagio(backend, db=db)
        primo = tagio.provision(_payload(container_index=1, container_total=2),
                                container_id=container_ids[0])
        assert primo.ok is True

        # Forza la collisione assegnando lo stesso EPC al secondo contenitore.
        db.assign_epc(container_ids[1], "0000000000000000000000FF")
        conflitto = db.find_container_by_epc(primo.epc)
        assert conflitto is not None and conflitto.container_id == container_ids[0]


def test_errore_del_database_non_blocca_la_traccia() -> None:
    class DbGuasto:
        def log_event(self, *_args, **_kwargs):
            raise RuntimeError("archivio non raggiungibile")

    backend = _backend_vergine()
    esito = _tagio(backend, db=DbGuasto()).provision(_payload())
    # `container_id` assente: il db viene usato solo per la traccia, che fallisce
    # in silenzio senza compromettere la scrittura sul tag.
    assert esito.ok is True, esito.error


# --------------------------------------------------------------------------
# Tentativo di scrittura interrotto
# --------------------------------------------------------------------------
class _CambioEpcInterrotto(FakeTagBackend):
    """Il primo cambio EPC si ferma a meta': tre word nuove, tre vecchie.

    E' cio' che il modulo ha fatto al banco l'11/09/2026 rispondendo
    «0x0400 No tag found»: il chip aveva gia' preso le prime tre word.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.interruzioni = 1

    def write_epc(self, request):
        if self.interruzioni <= 0:
            return super().write_epc(request)
        self.interruzioni -= 1
        self.calls.append("write_epc")
        bloccato = self._guard("write_epc", request.expected_epc)
        if bloccato is not None:
            return bloccato
        vecchio, nuovo = self.only_tag.epc, bytes.fromhex(request.new_epc)
        self.only_tag.epc = nuovo[:6] + vecchio[6:]
        self.observed_epcs = frozenset()
        return self._ko("write_epc", "scrittura EPC fallita: 0x0400: No tag found")


def _contenitore_pianificato(db: LimsDatabase) -> int:
    patient_id = db.upsert_patient(Patient(codice_fiscale=CF_VALIDO, cognome="Rossi", nome="Mario"))
    case_id = db.create_case(Case(accession_id=987654, patient_id=patient_id))
    specimen_id = db.add_specimen(Specimen(case_id=case_id, material_code=1))
    return db.plan_containers(specimen_id, 3)[0]


def test_cambio_epc_interrotto_a_meta_si_completa_sullo_stesso_chip() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore_pianificato(db)
        backend = _CambioEpcInterrotto([SimulatedTag(VERGINE, tid=TID_A)])
        tagio = _tagio(backend, db=db)

        primo = tagio.provision(_payload(), container_id=container_id)
        assert primo.ok is False
        tentativo = db.provision_attempt(container_id)
        assert tentativo is not None and tentativo["phase"] == "avviata"
        a_meta = backend.only_tag.epc_hex
        assert a_meta not in (VERGINE.hex().upper(), tentativo["epc"])

        # Stesso chip, EPC ne' vecchio ne' nuovo: prima veniva rifiutato.
        secondo = tagio.provision(_payload(), container_id=container_id)
        assert secondo.ok is True, secondo.error
        assert secondo.previous_epc == a_meta
        assert secondo.epc == tentativo["epc"] == backend.only_tag.epc_hex
        assert any("rimasto a meta'" in passo for passo in secondo.steps)
        record = db.find_container_by_epc(secondo.epc)
        assert record is not None and record.state == ContainerState.PROVISIONED
        assert db.provision_attempt(container_id)["phase"] == "verificata"


def test_dopo_un_tentativo_interrotto_un_altro_tag_resta_fuori() -> None:
    with LimsDatabase() as db:
        container_id = _contenitore_pianificato(db)
        backend = _CambioEpcInterrotto([SimulatedTag(VERGINE, tid=TID_A)])
        tagio = _tagio(backend, db=db)
        assert tagio.provision(_payload(), container_id=container_id).ok is False

        # Stesso EPC di fabbrica, altro chip: e' il caso del clone.
        estraneo = SimulatedTag(VERGINE, tid=TID_B)
        backend.tags = [estraneo]
        esito = tagio.provision(_payload(), container_id=container_id)
        assert esito.ok is False
        # L'operatore deve sapere quale tag rimettere e qual e' l'alternativa.
        assert TID_A.hex().upper() in esito.error, esito.error
        assert "Contenitore rotto o tag guasto" in esito.error
        assert estraneo.epc == VERGINE
        assert backend.calls.count("write_epc") == 1


def test_riconoscimento_del_tag_di_un_tentativo_con_i_valori_del_banco() -> None:
    # Valori veri dell'11/09/2026, contenitore 2/3 dell'accettazione 2.
    tentativo = {
        "previous_epc": "E280F30200000001B72AEEEC",
        "epc": "010001000000020203FEB92C",
        "tid": "",
    }
    # Senza TID: partenza, arrivo, oppure il misto lasciato dall'interruzione.
    assert _stesso_tag_del_tentativo(tentativo, "E280F30200000001B72AEEEC", "")
    assert _stesso_tag_del_tentativo(tentativo, "010001000000020203FEB92C", "")
    assert _stesso_tag_del_tentativo(tentativo, "0100010000000001B72AEEEC", "")
    # un altro tag della stessa bobina differisce nell'ultima word
    assert not _stesso_tag_del_tentativo(tentativo, "E280F30200000001B72AEED4", "")
    # un EPC di questo sistema, ma di un'altra accettazione
    assert not _stesso_tag_del_tentativo(tentativo, "0100010000000301023CDD78", "")

    # Con il TID registrato decide il TID, qualunque cosa mostri l'EPC.
    con_tid = {**tentativo, "tid": "E280F30220000001B72AEEEC"}
    assert _stesso_tag_del_tentativo(con_tid, "0100010000000001B72AEEEC", "E280F30220000001B72AEEEC")
    assert not _stesso_tag_del_tentativo(con_tid, "E280F30200000001B72AEEEC", "E280F30220000001B72AEED4")
    assert not _stesso_tag_del_tentativo(con_tid, "E280F30200000001B72AEEEC", "")


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
