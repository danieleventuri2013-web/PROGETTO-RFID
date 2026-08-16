"""Test del sigillo crittografico del payload, senza hardware.

Il test che conta piu' di tutti e' `test_tid_alterato_fallisce_autenticazione`:
dimostra che copiare EPC e USER memory su un chip vergine non produce un tag
valido, perche' il TID di fabbrica entra nei dati autenticati.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims.codec import (
    SpecimenFlags,
    TagPayload,
    build_epc,
    pack_payload,
    parse_epc,
    unpack_payload,
)
from lims.crypto import (
    GCM_TAG_SIZE,
    HEADER_SIZE,
    KEY_SIZE,
    SEAL_OVERHEAD,
    Keyring,
    PayloadAuthenticationError,
    PayloadFormatError,
    UnknownKeyError,
    max_plaintext_bytes,
    open_sealed,
    read_header,
    seal,
)

CF_VALIDO = "MRTMTT25D09F205Z"
CHIAVE = bytes(range(KEY_SIZE))
ALTRA_CHIAVE = bytes(range(100, 100 + KEY_SIZE))
EPC = build_epc(0x00A5, 987654, 2, 3, random_suffix=b"\xde\xad\xbe")
EPC_INFO = parse_epc(EPC)
TID = bytes.fromhex("E2801190200050A1B2C3D4E5")
ALTRO_TID = bytes.fromhex("E2801190200050FFFFFFFFFF")


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001 - vogliamo il tipo esatto
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _keyring() -> Keyring:
    return Keyring({0: CHIAVE})


def _payload() -> TagPayload:
    return TagPayload(
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


# --------------------------------------------------------------------------
# Giro completo
# --------------------------------------------------------------------------
def test_seal_open_roundtrip() -> None:
    sigillato = seal(b"dati riservati", epc=EPC, tid=TID, key=CHIAVE)
    chiaro, meta = open_sealed(sigillato, epc=EPC, tid=TID, keys=_keyring())
    assert chiaro.rstrip(b"\x00") == b"dati riservati"
    assert meta.key_id == 0 and meta.revision == 0


def test_overhead_di_20_byte() -> None:
    sigillato = seal(b"x" * 44, epc=EPC, tid=TID, key=CHIAVE)
    assert len(sigillato) == 44 + SEAL_OVERHEAD == 64
    assert SEAL_OVERHEAD == HEADER_SIZE + GCM_TAG_SIZE == 20


def test_max_plaintext_per_una_user_memory_da_64_byte() -> None:
    assert max_plaintext_bytes(64) == 44
    assert max_plaintext_bytes(128) == 108
    assert max_plaintext_bytes(8) == 0


def test_risultato_sempre_di_lunghezza_pari() -> None:
    # Il comando di scrittura del lettore rifiuta le lunghezze dispari.
    for lunghezza in (17, 18, 19, 43, 44):
        sigillato = seal(b"y" * lunghezza, epc=EPC, tid=TID, key=CHIAVE)
        assert len(sigillato) % 2 == 0, f"lunghezza dispari con {lunghezza} byte in chiaro"


def test_payload_reale_sta_in_64_byte_di_user_memory() -> None:
    # Verifica end-to-end del bilancio dichiarato nel piano: dominio -> codec ->
    # sigillo -> 64 byte, cioe' una sola scrittura da 32 word.
    chiaro = pack_payload(_payload(), max_bytes=max_plaintext_bytes(64))
    sigillato = seal(chiaro, epc=EPC, tid=TID, key=CHIAVE)
    assert len(sigillato) <= 64

    riletto, _ = open_sealed(sigillato, epc=EPC, tid=TID, keys=_keyring())
    restaurato = unpack_payload(riletto, EPC_INFO)
    assert restaurato.codice_fiscale == CF_VALIDO
    assert restaurato.display_name == "ROSSI MARIO"
    assert restaurato.container_index == 2 and restaurato.container_total == 3
    assert restaurato.data_prelievo == date(2026, 8, 15)
    assert restaurato.flags == SpecimenFlags.URGENT


# --------------------------------------------------------------------------
# Autenticazione: e' qui che sta la difesa
# --------------------------------------------------------------------------
def test_tid_alterato_fallisce_autenticazione() -> None:
    """Il caso del tag clonato: stesso EPC, stesso payload, chip diverso."""
    sigillato = seal(b"dati riservati", epc=EPC, tid=TID, key=CHIAVE)
    _expect(
        PayloadAuthenticationError,
        lambda: open_sealed(sigillato, epc=EPC, tid=ALTRO_TID, keys=_keyring()),
        "un payload copiato su un chip diverso deve essere rifiutato",
    )


def test_epc_alterato_fallisce_autenticazione() -> None:
    altro_epc = build_epc(0x00A5, 987654, 3, 3, random_suffix=b"\xde\xad\xbe")
    sigillato = seal(b"dati riservati", epc=EPC, tid=TID, key=CHIAVE)
    _expect(
        PayloadAuthenticationError,
        lambda: open_sealed(sigillato, epc=altro_epc, tid=TID, keys=_keyring()),
        "riscrivere l'EPC deve invalidare il payload",
    )


def test_chiave_errata_fallisce_autenticazione() -> None:
    sigillato = seal(b"dati riservati", epc=EPC, tid=TID, key=CHIAVE)
    _expect(
        PayloadAuthenticationError,
        lambda: open_sealed(sigillato, epc=EPC, tid=TID, keys=Keyring({0: ALTRA_CHIAVE})),
        "una chiave sbagliata non deve produrre dati",
    )


def test_ciphertext_manomesso_fallisce_autenticazione() -> None:
    sigillato = bytearray(seal(b"dati riservati", epc=EPC, tid=TID, key=CHIAVE))
    sigillato[HEADER_SIZE + 1] ^= 0x01
    _expect(
        PayloadAuthenticationError,
        lambda: open_sealed(bytes(sigillato), epc=EPC, tid=TID, keys=_keyring()),
        "la manomissione di un bit deve essere rilevata",
    )


def test_intestazione_manomessa_fallisce_autenticazione() -> None:
    # L'intestazione e' in chiaro ma autenticata: cambiare la revisione dichiarata
    # non permette di far decifrare il payload con un nonce diverso.
    sigillato = bytearray(seal(b"dati riservati", epc=EPC, tid=TID, key=CHIAVE, revision=1))
    sigillato[1] = (sigillato[1] & 0xF0) | 0x02
    _expect(
        PayloadAuthenticationError,
        lambda: open_sealed(bytes(sigillato), epc=EPC, tid=TID, keys=_keyring()),
        "l'intestazione deve essere coperta dall'autenticazione",
    )


def test_chiave_sconosciuta_e_distinta_dal_fallimento_di_autenticazione() -> None:
    sigillato = seal(b"dati", epc=EPC, tid=TID, key=CHIAVE, key_id=3)
    _expect(
        UnknownKeyError,
        lambda: open_sealed(sigillato, epc=EPC, tid=TID, keys=_keyring()),
        "una chiave mancante deve dare un messaggio diverso da un tag manomesso",
    )


def test_dati_estranei_danno_errore_di_formato() -> None:
    _expect(
        PayloadFormatError,
        lambda: read_header(b"\x00\x00\x00\x00"),
        "un tag di terzi non deve sembrare un tag manomesso",
    )
    _expect(PayloadFormatError, lambda: read_header(b"\xa1\x00"), "intestazione troncata")
    _expect(
        PayloadFormatError,
        lambda: read_header(b"\xa7\x00\x00\x10"),
        "versione di sigillo sconosciuta",
    )


def test_corpo_troppo_corto_da_errore_di_formato() -> None:
    _expect(
        PayloadFormatError,
        lambda: open_sealed(b"\xa1\x00\x00\x00", epc=EPC, tid=TID, keys=_keyring()),
        "un payload di lunghezza dichiarata nulla e' malformato",
    )
    _expect(
        PayloadFormatError,
        lambda: open_sealed(b"\xa1\x00\x00\x20" + b"\x00" * 8, epc=EPC, tid=TID, keys=_keyring()),
        "l'intestazione dichiara piu' byte di quanti ne siano presenti",
    )


def test_memoria_non_scritta_dopo_il_payload_non_disturba() -> None:
    """La USER memory e' piu' grande del payload: il resto sono byte a zero.

    Senza la lunghezza dichiarata nell'intestazione quegli zeri entrerebbero nel
    calcolo di autenticazione e ogni rilettura fallirebbe. Non basterebbe
    tagliarli: il tag GCM e' casuale e puo' finire con uno zero.
    """
    sigillato = seal(b"payload corto", epc=EPC, tid=TID, key=CHIAVE)
    letto_dal_tag = sigillato + bytes(64 - len(sigillato))   # rilettura di 64 byte
    chiaro, meta = open_sealed(letto_dal_tag, epc=EPC, tid=TID, keys=_keyring())
    assert chiaro.rstrip(b"\x00") == b"payload corto"
    assert meta.total_size == len(sigillato)


# --------------------------------------------------------------------------
# Nonce e revisioni
# --------------------------------------------------------------------------
def test_revisioni_diverse_producono_ciphertext_diversi() -> None:
    primo = seal(b"stesso testo", epc=EPC, tid=TID, key=CHIAVE, revision=0)
    secondo = seal(b"stesso testo", epc=EPC, tid=TID, key=CHIAVE, revision=1)
    assert primo[HEADER_SIZE:] != secondo[HEADER_SIZE:], "riuso del nonce alla riscrittura"


def test_epc_diversi_producono_ciphertext_diversi() -> None:
    altro_epc = build_epc(0x00A5, 987654, 3, 3, random_suffix=b"\x01\x02\x03")
    primo = seal(b"stesso testo", epc=EPC, tid=TID, key=CHIAVE)
    secondo = seal(b"stesso testo", epc=altro_epc, tid=TID, key=CHIAVE)
    assert primo[HEADER_SIZE:] != secondo[HEADER_SIZE:]


def test_revisione_oltre_il_limite_rifiutata() -> None:
    _expect(
        ValueError,
        lambda: seal(b"x", epc=EPC, tid=TID, key=CHIAVE, revision=16),
        "oltre la revisione 15 il nonce si ripeterebbe",
    )


def test_epc_e_tid_obbligatori() -> None:
    _expect(ValueError, lambda: seal(b"x", epc=b"", tid=TID, key=CHIAVE), "epc mancante")
    _expect(ValueError, lambda: seal(b"x", epc=EPC, tid=b"", key=CHIAVE), "tid mancante")


def test_chiave_di_lunghezza_errata_rifiutata() -> None:
    _expect(
        ValueError,
        lambda: seal(b"x", epc=EPC, tid=TID, key=b"corta"),
        "AES-256 richiede 32 byte",
    )


def test_accetta_epc_e_tid_in_esadecimale() -> None:
    sigillato = seal(b"dati", epc=EPC.hex(), tid=TID.hex().upper(), key=CHIAVE)
    chiaro, _ = open_sealed(sigillato, epc=EPC, tid=TID, keys=_keyring())
    assert chiaro.rstrip(b"\x00") == b"dati"


# --------------------------------------------------------------------------
# Portachiavi
# --------------------------------------------------------------------------
def test_keyring_salva_e_ricarica() -> None:
    portachiavi = Keyring()
    key_id = portachiavi.generate()
    portachiavi.default_key_id = key_id
    with tempfile.TemporaryDirectory() as tmp:
        percorso = portachiavi.save(Path(tmp) / "chiavi.json")
        ricaricato = Keyring.load(percorso)
    assert ricaricato.key_ids == portachiavi.key_ids
    assert ricaricato.default_key_id == key_id
    assert ricaricato.require(key_id) == portachiavi.require(key_id)


def test_keyring_genera_chiavi_distinte() -> None:
    portachiavi = Keyring()
    primo = portachiavi.generate()
    secondo = portachiavi.generate()
    assert primo != secondo
    assert portachiavi.require(primo) != portachiavi.require(secondo)
    assert len(portachiavi) == 2


def test_keyring_non_rivela_le_chiavi_nel_repr() -> None:
    # Questo oggetto compare nei messaggi di errore e nei log di diagnostica.
    portachiavi = _keyring()
    testo = repr(portachiavi)
    assert CHIAVE.hex() not in testo
    assert "key_ids=[0]" in testo


def test_keyring_rifiuta_chiavi_e_identificativi_non_validi() -> None:
    _expect(ValueError, lambda: Keyring({0: b"corta"}), "chiave troppo corta")
    _expect(ValueError, lambda: Keyring({99: CHIAVE}), "key_id fuori intervallo")
    _expect(UnknownKeyError, lambda: Keyring().require(0), "chiave assente")


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
