"""Test della profilazione dei tag: decodifica TID e misura della USER memory."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# anche la cartella dei test, cosi' `fake_backend` si importa sia lanciando
# questo file da solo sia tramite `python run.py tests`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag
from lims.profiler import (
    TID_AAD_BYTES,
    TagProfile,
    decode_tid,
    profile_tag,
    summarize,
)

EPC = bytes.fromhex("0100A5000F12060203DEADBE")

# TID reali, usati per verificare la decodifica contro valori noti.
TID_MONZA_R6 = bytes.fromhex("E280116000000000A1B2C3D4")   # Impinj, modello 0x160
TID_HIGGS_3 = bytes.fromhex("E2003412013A1B2C3D4E5F60")    # Alien, modello 0x412
TID_UCODE_8 = bytes.fromhex("E2806B9000005A1B2C3D4E5F")    # NXP


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _backend(user_bytes: int, tid: bytes = TID_HIGGS_3) -> FakeTagBackend:
    return FakeTagBackend([SimulatedTag(EPC, tid=tid, user_bytes=user_bytes)])


# --------------------------------------------------------------------------
# Decodifica del TID
# --------------------------------------------------------------------------
def test_decode_tid_impinj_monza_r6() -> None:
    info = decode_tid(TID_MONZA_R6)
    assert info.class_id == 0xE2
    assert info.is_epc_class
    assert info.xtid is True
    assert info.mdid == 0x001
    assert info.tmn == 0x160
    assert info.manufacturer == "Impinj"


def test_decode_tid_alien_higgs_3() -> None:
    info = decode_tid(TID_HIGGS_3)
    assert info.mdid == 0x003
    assert info.tmn == 0x412
    assert info.xtid is False
    assert info.manufacturer == "Alien Technology"


def test_decode_tid_nxp() -> None:
    info = decode_tid(TID_UCODE_8)
    assert info.mdid == 0x006
    assert info.manufacturer == "NXP Semiconductors"


def test_decode_tid_costruttore_sconosciuto_non_inventa_un_nome() -> None:
    info = decode_tid(bytes.fromhex("E2FF0000AABBCCDDEEFF0011"))
    assert "sconosciuto" in info.manufacturer


def test_decode_tid_troppo_corto() -> None:
    _expect(ValueError, lambda: decode_tid(b"\xe2\x80"), "TID troncato")


def test_tid_tutto_a_zeri_non_e_serializzato() -> None:
    # Identifica il modello ma non l'esemplare: non basta per l'anti-clonazione.
    info = decode_tid(bytes.fromhex("E2003412") + bytes(8))
    assert info.serialized is False
    assert decode_tid(TID_HIGGS_3).serialized is True


def test_tid_corto_non_e_serializzato() -> None:
    assert decode_tid(bytes.fromhex("E2003412")).serialized is False


# --------------------------------------------------------------------------
# Misura della USER memory
# --------------------------------------------------------------------------
def test_misura_user_memory_di_64_byte() -> None:
    profilo = profile_tag(_backend(64), antennas=(1, 2))
    assert profilo.ok is True
    assert profilo.user_bytes == 64
    assert profilo.user_words == 32
    assert profilo.usable_payload_bytes == 44
    assert profilo.tier == "B"
    assert profilo.suitable is True


def test_misura_dimensioni_diverse() -> None:
    for user_bytes in (2, 8, 30, 64, 128, 256, 512):
        profilo = profile_tag(_backend(user_bytes))
        assert profilo.ok is True, f"{user_bytes} byte: {profilo.error}"
        assert profilo.user_bytes == user_bytes, (
            f"misurati {profilo.user_bytes} byte invece di {user_bytes}"
        )


def test_misura_dimensione_dispari_in_word() -> None:
    # 46 byte = 23 word: la ricerca binaria non deve arrotondare a potenze di due.
    profilo = profile_tag(_backend(46))
    assert profilo.user_words == 23 and profilo.user_bytes == 46


def test_tag_senza_user_memory_e_tier_a() -> None:
    profilo = profile_tag(_backend(0, tid=TID_MONZA_R6))
    assert profilo.ok is True
    assert profilo.user_bytes == 0
    assert profilo.tier == "A"
    assert profilo.suitable is False
    assert "non e' soddisfacibile" in profilo.recommendation


def test_user_memory_troppo_piccola_non_e_utilizzabile() -> None:
    # 32 byte lasciano 12 byte utili: sotto i 18 della sola parte fissa.
    profilo = profile_tag(_backend(32))
    assert profilo.tier == "A+"
    assert profilo.suitable is False


def test_user_memory_ampia_e_tier_c() -> None:
    profilo = profile_tag(_backend(512))
    assert profilo.tier == "C"
    assert profilo.suitable is True
    assert profilo.usable_payload_bytes == 492


def test_misura_efficiente() -> None:
    # La ricerca binaria deve costare un numero di letture logaritmico: una
    # scansione lineare su 512 byte richiederebbe centinaia di comandi radio.
    profilo = profile_tag(_backend(512))
    assert profilo.reads_performed < 40, f"troppe letture: {profilo.reads_performed}"


def test_misura_tollera_una_lettura_persa() -> None:
    backend = _backend(64)
    backend.read_failures = 1  # rumore radio sulla prima lettura utile
    profilo = profile_tag(backend)
    assert profilo.ok is True
    assert profilo.user_bytes == 64, "una lettura persa non deve accorciare la misura"


# --------------------------------------------------------------------------
# Avvertenze e casi limite
# --------------------------------------------------------------------------
def test_tid_non_serializzato_rende_il_tag_non_utilizzabile() -> None:
    profilo = profile_tag(_backend(64, tid=bytes.fromhex("E2003412") + bytes(8)))
    assert profilo.ok is True
    assert profilo.suitable is False, "senza numero di serie il legame col chip non regge"
    assert any("TID non serializzato" in avviso for avviso in profilo.warnings)


def test_classe_tid_non_gen2_segnalata() -> None:
    profilo = profile_tag(_backend(64, tid=bytes.fromhex("E1003412013A1B2C3D4E5F60")))
    assert any("non e' un tag EPC Gen2" in avviso for avviso in profilo.warnings)


def test_campo_vuoto() -> None:
    profilo = profile_tag(FakeTagBackend([]))
    assert profilo.ok is False
    assert "nessun tag nel campo" in profilo.error


def test_piu_tag_nel_campo_rifiutati() -> None:
    backend = FakeTagBackend(
        [
            SimulatedTag(EPC, user_bytes=64),
            SimulatedTag(bytes.fromhex("0100A5000F12060303C0FFEE"), user_bytes=64),
        ]
    )
    profilo = profile_tag(backend)
    assert profilo.ok is False
    assert "2 tag" in profilo.error, profilo.error


def test_usa_i_primi_dodici_byte_del_tid() -> None:
    profilo = profile_tag(_backend(64))
    assert profilo.tid is not None
    assert len(profilo.tid.tid_hex) == TID_AAD_BYTES * 2


def test_report_serializzabile() -> None:
    import json

    profilo = profile_tag(_backend(64))
    documento = profilo.to_dict()
    json.dumps(documento)  # non deve sollevare
    assert documento["tid"]["manufacturer"] == "Alien Technology"
    assert documento["tid"]["tmn"] == "0x412"
    assert documento["tier"] == "B"
    assert documento["suitable"] is True


def test_summarize_leggibile() -> None:
    assert "Tier B" in summarize(profile_tag(_backend(64)))
    assert "non riuscita" in summarize(TagProfile(ok=False, error="tag assente"))


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
