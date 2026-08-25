"""Test dello schema binario dei dati sul tag, senza hardware e senza database."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims.codec import (
    CF_PACKED_SIZE,
    EPC_SIZE,
    PAYLOAD_FIXED_SIZE,
    SpecimenFlags,
    TagPayload,
    build_epc,
    describe_material,
    pack_codice_fiscale,
    pack_payload,
    parse_epc,
    unpack_codice_fiscale,
    unpack_payload,
)
from lims.model import (
    Container,
    Patient,
    codice_fiscale_check_char,
    normalize_name,
    validate_codice_fiscale,
)

# Codice fiscale di esempio pubblicato nella documentazione dell'Agenzia delle
# Entrate: serve a verificare le tabelle del carattere di controllo contro un
# valore noto, non calcolato da noi.
CF_VALIDO = "MRTMTT25D09F205Z"

# Accettazione e numerazione non stanno nel payload: viaggiano nell'EPC, e la
# rilettura le reinserisce da li'.
EPC_INFO = parse_epc(build_epc(0x00A5, 987654, 2, 3, random_suffix=b"\xde\xad\xbe"))


def _expect_error(callable_, message: str) -> None:
    try:
        callable_()
    except (ValueError, TypeError):
        return
    raise AssertionError(message)


# --------------------------------------------------------------------------
# Codice fiscale
# --------------------------------------------------------------------------
def test_codice_fiscale_check_char_su_valore_noto() -> None:
    assert codice_fiscale_check_char(CF_VALIDO[:15]) == "Z"
    assert validate_codice_fiscale(CF_VALIDO) == CF_VALIDO


def test_codice_fiscale_normalizza_spazi_e_minuscole() -> None:
    assert validate_codice_fiscale(" mrt mtt25d09f205z ") == CF_VALIDO


def test_codice_fiscale_rifiuta_carattere_di_controllo_errato() -> None:
    sbagliato = CF_VALIDO[:15] + "A"
    _expect_error(
        lambda: validate_codice_fiscale(sbagliato),
        "un carattere di controllo errato deve essere rifiutato",
    )


def test_codice_fiscale_rifiuta_lunghezza_errata() -> None:
    _expect_error(
        lambda: validate_codice_fiscale("MRTMTT25D09F205"),
        "15 caratteri non sono un codice fiscale valido",
    )


def test_codice_fiscale_accetta_omocodia() -> None:
    # In omocodia le cifre sono sostituite da lettere: il formato deve accettarlo.
    stem = "MRTMTT25D09F2L5"
    omocodice = stem + codice_fiscale_check_char(stem)
    assert validate_codice_fiscale(omocodice) == omocodice


def test_codice_fiscale_roundtrip_impacchettato() -> None:
    packed = pack_codice_fiscale(CF_VALIDO)
    assert len(packed) == CF_PACKED_SIZE == 11
    assert unpack_codice_fiscale(packed) == CF_VALIDO


def test_codice_fiscale_impacchettato_risparmia_cinque_byte() -> None:
    # E' il motivo per cui esiste la codifica base 36: 16 byte ASCII non ci
    # starebbero nel payload da 46 byte insieme al resto.
    assert len(CF_VALIDO.encode("ascii")) - CF_PACKED_SIZE == 5


def test_codice_fiscale_estremi_alfabeto() -> None:
    for value in ("A" * 15, "Z" * 15, "0" * 15):
        cf = value + codice_fiscale_check_char(value)
        assert unpack_codice_fiscale(pack_codice_fiscale(cf)) == cf


# --------------------------------------------------------------------------
# EPC pseudonimo
# --------------------------------------------------------------------------
def test_epc_roundtrip() -> None:
    epc = build_epc(0x00A5, 1234567, 2, 3, random_suffix=b"\xde\xad\xbe")
    assert len(epc) == EPC_SIZE == 12
    info = parse_epc(epc)
    assert info.lab_id == 0x00A5
    assert info.accession_id == 1234567
    assert info.container_index == 2
    assert info.container_total == 3
    assert info.label == "2/3"
    assert info.random_hex == "DEADBE"


def test_epc_accetta_stringa_esadecimale() -> None:
    epc = build_epc(1, 42, 1, 1, random_suffix=b"\x00\x00\x01")
    assert parse_epc(epc.hex().upper()) == parse_epc(epc)


def test_epc_non_contiene_dati_personali() -> None:
    # Verifica strutturale: l'EPC e' costruito solo da numeri e da caso, e non
    # espone mai il codice fiscale del paziente.
    epc = build_epc(7, 99, 1, 2)
    assert pack_codice_fiscale(CF_VALIDO) not in epc


def test_epc_coda_casuale_differenzia_contenitori_uguali() -> None:
    primo = build_epc(1, 1, 1, 1)
    secondo = build_epc(1, 1, 1, 1)
    assert primo[:9] == secondo[:9]
    assert primo != secondo, "la coda casuale deve rendere unici due EPC identici"


def test_epc_rifiuta_lunghezza_e_schema_estranei() -> None:
    _expect_error(lambda: parse_epc(b"\x01\x02\x03"), "EPC troppo corto")
    estraneo = bytes([0x99]) + bytes(11)
    _expect_error(lambda: parse_epc(estraneo), "schema EPC sconosciuto")


def test_epc_rifiuta_numerazione_incoerente() -> None:
    _expect_error(lambda: build_epc(1, 1, 4, 3), "indice maggiore del totale")
    _expect_error(lambda: build_epc(1, 1, 0, 3), "indice zero non valido")
    incoerente = build_epc(1, 1, 1, 3)[:7] + bytes([4, 3]) + b"\x00\x00\x00"
    _expect_error(lambda: parse_epc(incoerente), "numerazione incoerente in lettura")


# --------------------------------------------------------------------------
# Payload del campione
# --------------------------------------------------------------------------
def _payload(**overrides) -> TagPayload:
    base = dict(
        codice_fiscale=CF_VALIDO,
        accession_id=987654,
        container_index=2,
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


def test_payload_roundtrip() -> None:
    packed = pack_payload(_payload())
    restored = unpack_payload(packed, EPC_INFO)
    assert restored.codice_fiscale == CF_VALIDO
    assert restored.accession_id == 987654
    assert restored.container_index == 2
    assert restored.container_total == 3
    assert restored.display_name == "ROSSI MARIO"
    assert restored.data_prelievo == date(2026, 8, 15)
    assert restored.material_code == 1
    assert restored.fixative_code == 1
    assert restored.site_code == 2
    assert restored.flags == SpecimenFlags.URGENT


def test_payload_parte_fissa_di_18_byte() -> None:
    packed = pack_payload(_payload(display_name=""))
    assert len(packed) == PAYLOAD_FIXED_SIZE == 18


def test_payload_sta_in_44_byte_utili() -> None:
    # Il vincolo che governa tutto il progetto: 64 byte di USER memory, meno 4 di
    # intestazione e 16 di tag GCM, lasciano 44 byte. Deve starci un nome reale.
    packed = pack_payload(_payload(display_name="DELLA VALLE GIANFRANCO"), max_bytes=44)
    assert len(packed) <= 44
    assert unpack_payload(packed, EPC_INFO).display_name == "DELLA VALLE GIANFRANCO"


def test_payload_tronca_il_nome_senza_perdere_i_dati_strutturati() -> None:
    lungo = "SCARAMUCCI DELLA ROVERE MASSIMILIANO GIUSEPPE"
    packed = pack_payload(_payload(display_name=lungo), max_bytes=44)
    assert len(packed) == 44
    restored = unpack_payload(packed, EPC_INFO)
    assert restored.codice_fiscale == CF_VALIDO, "il codice fiscale non va mai troncato"
    assert lungo.startswith(restored.display_name)
    assert len(restored.display_name) == 44 - PAYLOAD_FIXED_SIZE


def test_payload_non_duplica_i_dati_gia_presenti_nell_epc() -> None:
    # Accettazione e numerazione arrivano dall'EPC: due copie potrebbero
    # discordare, e non ci sarebbe modo di stabilire quale creda.
    packed = pack_payload(_payload(accession_id=987654, container_index=2, container_total=3))
    altro_epc = parse_epc(build_epc(0x00A5, 111, 1, 5, random_suffix=b"\x00\x00\x01"))
    restored = unpack_payload(packed, altro_epc)
    assert restored.accession_id == 111
    assert restored.container_index == 1 and restored.container_total == 5
    assert restored.codice_fiscale == CF_VALIDO


def test_payload_rifiuta_spazio_insufficiente_per_la_parte_fissa() -> None:
    _expect_error(
        lambda: pack_payload(_payload(), max_bytes=PAYLOAD_FIXED_SIZE - 1),
        "sotto la parte fissa la scrittura deve fallire, non troncare i dati",
    )


def test_payload_senza_data_di_prelievo() -> None:
    packed = pack_payload(_payload(data_prelievo=None))
    assert unpack_payload(packed, EPC_INFO).data_prelievo is None


def test_payload_rifiuta_schema_sconosciuto() -> None:
    _expect_error(
        lambda: pack_payload(_payload(schema=0x7F)),
        "scrivere con uno schema sconosciuto deve fallire",
    )
    packed = bytearray(pack_payload(_payload()))
    packed[0] = 0x7F
    _expect_error(
        lambda: unpack_payload(bytes(packed), EPC_INFO),
        "leggere uno schema sconosciuto deve fallire invece di indovinare",
    )


def test_payload_rifiuta_troppo_corto() -> None:
    _expect_error(lambda: unpack_payload(b"\x01" * 10, EPC_INFO), "payload troncato")


def test_payload_rifiuta_numerazione_incoerente() -> None:
    _expect_error(
        lambda: pack_payload(_payload(container_index=5, container_total=3)),
        "indice maggiore del totale",
    )


def test_payload_flag_multiple() -> None:
    flags = SpecimenFlags.URGENT | SpecimenFlags.INFECTIOUS
    packed = pack_payload(_payload(flags=flags))
    restored = unpack_payload(packed, EPC_INFO)
    assert restored.flags == flags
    assert "INFECTIOUS" in restored.describe()["avvertenze"]


def test_payload_massimo_255_contenitori() -> None:
    packed = pack_payload(_payload(container_index=255, container_total=255))
    epc_info = parse_epc(build_epc(0x00A5, 987654, 255, 255, random_suffix=b"\x00\x00\x01"))
    restored = unpack_payload(packed, epc_info)
    assert restored.container_index == 255 and restored.container_total == 255


def test_payload_describe_e_leggibile() -> None:
    descritto = _payload().describe()
    assert descritto["contenitore"] == "2/3"
    assert descritto["materiale"] == describe_material(1) == "biopsia"
    assert descritto["paziente"] == "ROSSI MARIO"


# --------------------------------------------------------------------------
# Normalizzazione dei nomi e coerenza con il modello
# --------------------------------------------------------------------------
def test_normalize_name_rimuove_accenti_e_comprime_spazi() -> None:
    assert normalize_name("  Niccolò   D'Amico-Rossi ") == "NICCOLO D'AMICO-ROSSI"
    assert normalize_name("Müller") == "MULLER"


def test_nome_con_accenti_sopravvive_al_giro_completo() -> None:
    packed = pack_payload(_payload(display_name="Peruzzi Nicolò"), max_bytes=46)
    assert unpack_payload(packed, EPC_INFO).display_name == "PERUZZI NICOLO"


def test_patient_display_name_coerente_col_payload() -> None:
    paziente = Patient(codice_fiscale=CF_VALIDO, cognome="Rossi", nome="Mario")
    packed = pack_payload(_payload(display_name=paziente.display_name))
    assert unpack_payload(packed, EPC_INFO).display_name == paziente.display_name


def test_container_valida_la_numerazione() -> None:
    assert Container(index=2, total=3).label == "2/3"
    _expect_error(lambda: Container(index=0, total=3), "indice zero")
    _expect_error(lambda: Container(index=4, total=3), "indice oltre il totale")


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
