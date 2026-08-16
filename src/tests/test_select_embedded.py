"""Test del filtro Select e della lettura innestata nell'inventory.

Sono le due funzioni che tolgono il vincolo «un tag alla volta»:

* il **filtro Select** punta un EPC preciso anche con altri tag nel campo, quindi
  si puo' leggere o scrivere un contenitore scelto senza isolarlo fisicamente;
* l'**embedded read** restituisce la memoria di *tutti* i tag in un solo giro di
  inventory.

I frame sono confrontati con gli esempi del manuale EX10 §5.1, §5.2 e §5.3.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_reader import FakeTransport, make_response

from rfid_silion import protocol as P
from rfid_silion.reader import SIM7200Reader

EPC = bytes.fromhex("111122223333444455556666")


def _hex(value: str) -> bytes:
    return bytes.fromhex(value.replace(" ", ""))


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


# --------------------------------------------------------------------------
# Composizione del filtro Select
# --------------------------------------------------------------------------
def test_filtro_su_epcid_non_invia_l_indirizzo() -> None:
    """Il dettaglio piu' facile da sbagliare del manuale."""
    option, dati = P.build_tag_singulation(P.SELECT_BY_EPC_ID, select_data=EPC)
    assert option == 0x01
    # password(4) + lunghezza(1) + dati(12): nessun Select Address.
    assert len(dati) == 4 + 1 + 12
    assert dati[:4] == bytes(4)
    assert dati[4] == 0x60, "96 bit dichiarati in bit, non in byte"
    assert dati[5:] == EPC


def test_filtro_su_epcid_rifiuta_un_indirizzo_esplicito() -> None:
    _expect(
        ValueError,
        lambda: P.build_tag_singulation(
            P.SELECT_BY_EPC_ID, select_data=EPC, select_address_bits=0x20
        ),
        "indirizzo dove e' implicito",
    )


def test_filtro_su_altre_banche_richiede_l_indirizzo() -> None:
    _expect(
        ValueError,
        lambda: P.build_tag_singulation(P.SELECT_BY_TID, select_data=b"\x60"),
        "indirizzo mancante",
    )
    option, dati = P.build_tag_singulation(
        P.SELECT_BY_TID, select_data=b"\x60", select_address_bits=0x10, select_bit_length=4
    )
    assert option == 0x02
    assert dati == bytes(4) + _hex("00000010") + b"\x04" + b"\x60"


def test_nessun_filtro_e_solo_password() -> None:
    option, dati = P.build_tag_singulation(P.SELECT_NONE)
    assert option == 0x00 and dati == b""

    option, dati = P.build_tag_singulation(P.SELECT_PASSWORD_ONLY, b"\x11\x22\x33\x44")
    assert option == 0x05 and dati == b"\x11\x22\x33\x44"


def test_filtro_invertito() -> None:
    option, _ = P.build_tag_singulation(P.SELECT_BY_EPC_ID, select_data=EPC, inverted=True)
    assert option == 0x01 | P.SELECT_OPT_INVERTED
    _expect(
        ValueError,
        lambda: P.build_tag_singulation(P.SELECT_PASSWORD_ONLY, inverted=True),
        "inversione senza filtro",
    )


def test_maschera_lunga_usa_due_byte_di_lunghezza() -> None:
    lunga = bytes(40)  # 320 bit, oltre quanto sta in un byte
    option, dati = P.build_tag_singulation(P.SELECT_BY_USER, select_data=lunga,
                                           select_address_bits=0)
    assert option & P.SELECT_OPT_LONG_LENGTH
    assert dati[8:10] == (320).to_bytes(2, "big")


def test_filtro_valida_gli_ingressi() -> None:
    _expect(ValueError, lambda: P.build_tag_singulation(0x09, select_data=EPC), "select ignoto")
    _expect(
        ValueError,
        lambda: P.build_tag_singulation(P.SELECT_BY_EPC_ID, b"\x00", select_data=EPC),
        "password non di 4 byte",
    )
    _expect(
        ValueError,
        lambda: P.build_tag_singulation(P.SELECT_BY_EPC_ID),
        "filtro senza dati",
    )
    _expect(
        ValueError,
        lambda: P.build_tag_singulation(P.SELECT_BY_EPC_ID, select_data=b"\x11",
                                        select_bit_length=64),
        "lunghezza oltre i dati forniti",
    )


# --------------------------------------------------------------------------
# Lettura e scrittura mirate
# --------------------------------------------------------------------------
def test_lettura_senza_filtro_resta_come_prima() -> None:
    # Retrocompatibilita': i frame gia' collaudati non devono cambiare.
    transport = FakeTransport(make_response(P.CMD_READ_TAG_DATA, 0x0000, b"\x05\x12\x34"))
    dati = SIM7200Reader(transport).read_tag_data(P.BANK_USER, 0, 1)
    assert dati == b"\x12\x34"
    assert bytes(transport.tx) == _hex("FF 0D 28 03E8 05 03 00000000 01 00000000 6544")


def test_lettura_mirata_su_un_epc() -> None:
    transport = FakeTransport(make_response(P.CMD_READ_TAG_DATA, 0x0000, b"\x01\x12\x34"))
    dati = SIM7200Reader(transport).read_tag_data(P.BANK_USER, 0, 1, select_epc=EPC)
    assert dati == b"\x12\x34"

    inviato = bytes(transport.tx)
    payload = inviato[3:-2]
    assert payload[2] == P.SELECT_BY_EPC_ID, "option con filtro sull'EPCID"
    assert payload[3] == P.BANK_USER
    assert EPC in inviato, "l'EPC da cercare deve essere nel frame"


def test_scrittura_mirata_su_un_epc() -> None:
    transport = FakeTransport(make_response(P.CMD_WRITE_TAG_DATA, 0x0000))
    SIM7200Reader(transport).write_tag_data(P.BANK_USER, 0, b"\xaa\xbb", select_epc=EPC)

    inviato = bytes(transport.tx)
    payload = inviato[3:-2]
    assert payload[2] == P.SELECT_BY_EPC_ID
    assert inviato.endswith(_hex("AABB") + inviato[-2:])
    assert EPC in inviato


def test_eco_dell_option_verificata_nella_risposta() -> None:
    # Il lettore rimanda l'option inviata: se non coincide qualcosa non torna, e
    # accettare la risposta significherebbe leggere dati di un altro comando.
    transport = FakeTransport(make_response(P.CMD_READ_TAG_DATA, 0x0000, b"\x05\x12\x34"))
    _expect(
        P.SilionFrameError,
        lambda: SIM7200Reader(transport).read_tag_data(P.BANK_USER, 0, 1, select_epc=EPC),
        "eco dell'option non coerente",
    )


# --------------------------------------------------------------------------
# Lettura innestata nell'inventory
# --------------------------------------------------------------------------
def test_embedded_read_uguale_all_esempio_del_manuale() -> None:
    # Manuale §5.3: quattro gruppi, una banca ciascuno.
    prodotto = P.build_embedded_read([(0, 0, 4), (1, 0, 8), (2, 0, 6), (3, 0, 4)])
    atteso = _hex(
        "04 1B 28 0000 00"
        " 00 00000000 04"
        " 01 00000000 08"
        " 02 00000000 06"
        " 03 00000000 04"
    )
    assert prodotto == atteso, prodotto.hex(" ").upper()


def test_lunghezza_dichiarata_e_tre_piu_sei_per_gruppo() -> None:
    # Timeout e Option compaiono una volta sola, non per gruppo.
    for gruppi in (1, 2, 5):
        prodotto = P.build_embedded_read([(P.BANK_USER, 0, 2)] * gruppi)
        assert prodotto[0] == gruppi
        assert prodotto[1] == 3 + 6 * gruppi


def test_embedded_read_valida_i_limiti_del_manuale() -> None:
    _expect(ValueError, lambda: P.build_embedded_read([]), "nessun gruppo")
    _expect(
        ValueError,
        lambda: P.build_embedded_read([(P.BANK_USER, 0, 1)] * 11),
        "oltre 10 gruppi",
    )
    _expect(
        ValueError,
        lambda: P.build_embedded_read([(P.BANK_USER, 0, 33)]),
        "oltre 32 word per gruppo",
    )
    _expect(
        ValueError,
        lambda: P.build_embedded_read([(P.BANK_USER, 0, 32), (P.BANK_TID, 0, 32)]),
        "oltre 56 word complessive",
    )
    _expect(ValueError, lambda: P.build_embedded_read([(9, 0, 1)]), "banca inesistente")


def test_inventory_con_embedded_accende_i_flag_giusti() -> None:
    transport = FakeTransport(make_response(P.CMD_SYNCHRONOUS_INVENTORY, 0x0000,
                                            b"\x00\x00\x04\x02"))
    SIM7200Reader(transport).sync_inventory(
        timeout_ms=1000, embedded_read=[(P.BANK_USER, 0, 4)]
    )
    payload = bytes(transport.tx)[3:-2]
    search_flags = (payload[1] << 8) | payload[2]
    assert search_flags & P.SEARCH_FLAG_EMBEDDED_DATA, "il flag BIT2 va acceso da solo"
    assert payload[5] == 1, "un gruppo di lettura"
    assert payload[7] == P.CMD_READ_TAG_DATA, "il comando innestato e' 0x28"


def test_inventory_senza_embedded_non_cambia_il_frame() -> None:
    # Retrocompatibilita': senza lettura innestata il payload resta i cinque byte
    # di sempre, con Search Flags a zero e nessuna coda.
    transport = FakeTransport(make_response(P.CMD_SYNCHRONOUS_INVENTORY, 0x0000,
                                            b"\x00\x00\x00\x02"))
    SIM7200Reader(transport).sync_inventory(timeout_ms=1000)
    payload = bytes(transport.tx)[3:-2]
    assert payload == _hex("00 0000 03E8")


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
