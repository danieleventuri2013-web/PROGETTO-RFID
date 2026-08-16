"""Test del formato esteso "Moduletech" (manuale EX10 2024-12 §3.2).

Gli esempi qui sotto sono trascritti **byte per byte dal manuale** e sono la sola
prova che abbiamo della correttezza del formato prima di parlare con l'hardware.
In particolare il byte SubCRC: il manuale non ne pubblica la formula, solo gli
esempi. Quella implementata (somma modulo 256) e' stata ricavata da questi cinque
casi e li riproduce tutti. Se l'hardware la smentisse, e' qui che deve rompersi.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion import protocol as P
from rfid_silion.protocol import (
    SilionFrameError,
    build_extended_packet,
    extended_subcrc,
    parse_extended_response,
)


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


# Esempi ufficiali: (nome, subcmd, subdata, frame completo atteso)
ESEMPI_MANUALE = [
    (
        "0xAA48 avvio inventory asincrono",
        0xAA48,
        _hex("00BF 00 8003"),
        _hex("FF 13 AA 4D6F64756C6574656368 AA48 00BF 00 8003 34 BB 290F"),
    ),
    (
        "0xAA49 stop inventory asincrono",
        0xAA49,
        b"",
        _hex("FF 0E AA 4D6F64756C6574656368 AA49 F3 BB 0391"),
    ),
    (
        "0xAA58 modalita' tag densi",
        0xAA58,
        _hex("00" * 20 + "003F 00 0000"),
        _hex("FF 27 AA 4D6F64756C6574656368 AA58 " + "00" * 20 + " 003F 00 0000 41 BB 3F7D"),
    ),
    (
        "0xAA4D flip Session2 su lato A",
        0xAA4D,
        _hex("0001 000DC65E 0CE4"),
        _hex("FF 16 AA 4D6F64756C6574656368 AA4D 0001 000DC65E 0CE4 19 BB 06AA"),
    ),
    (
        "0xAA4A rilevamento onda stazionaria",
        0xAA4A,
        _hex("0BB8 01 01 00"),
        _hex("FF 13 AA 4D6F64756C6574656368 AA4A 0BB8 01 01 00 B9 BB BD67"),
    ),
]


# --------------------------------------------------------------------------
# Costruzione: confronto integrale con i frame del manuale
# --------------------------------------------------------------------------
def test_frame_identici_agli_esempi_del_manuale() -> None:
    for nome, subcmd, subdata, atteso in ESEMPI_MANUALE:
        prodotto = build_extended_packet(subcmd, subdata)
        assert prodotto == atteso, (
            f"{nome}:\n  prodotto {prodotto.hex().upper()}\n  atteso   {atteso.hex().upper()}"
        )


def test_subcrc_riproduce_tutti_gli_esempi() -> None:
    attesi = {0xAA48: 0x34, 0xAA49: 0xF3, 0xAA58: 0x41, 0xAA4D: 0x19, 0xAA4A: 0xB9}
    for nome, subcmd, subdata, _ in ESEMPI_MANUALE:
        assert extended_subcrc(subcmd, subdata) == attesi[subcmd], nome


def test_struttura_del_frame() -> None:
    frame = build_extended_packet(0xAA49)
    assert frame[0] == P.HEADER
    assert frame[2] == P.CMD_EXTENDED == 0xAA
    assert frame[3:13] == P.EXTENDED_MARKER == b"Moduletech"
    assert frame[13:15] == b"\xaa\x49"
    assert frame[-3] == P.EXTENDED_TERMINATOR == 0xBB
    # DataLen copre da "Moduletech" al terminatore incluso.
    assert frame[1] == len(P.EXTENDED_MARKER) + 2 + 0 + 1 + 1
    # In INVIO il frame totale e' 5 + DataLen (header, DataLen, cmd, CRC).
    # In RISPOSTA sono 7 + DataLen, per i due byte di stato che non rientrano
    # nella lunghezza dichiarata: confondere le due formule sposta il parsing
    # di due byte.
    assert len(frame) == 5 + frame[1]
    risposta = _risposta(0xAA49)
    assert len(risposta) == 7 + risposta[1]


def test_crc_esterno_e_quello_del_formato_comune() -> None:
    # Il formato esteso cambia solo il contenuto del campo Data: il CRC-16 CCITT
    # sul corpo resta identico a quello di `build_packet`.
    for _, subcmd, subdata, atteso in ESEMPI_MANUALE:
        del subcmd, subdata
        assert P.crc16(atteso[1:-2]) == (atteso[-2] << 8) | atteso[-1]


def test_subcmd_fuori_intervallo_rifiutato() -> None:
    _expect(ValueError, lambda: build_extended_packet(0x10000), "subcmd oltre 16 bit")
    _expect(ValueError, lambda: build_extended_packet(-1), "subcmd negativo")


def test_subdata_troppo_lunga_rifiutata() -> None:
    _expect(
        ValueError,
        lambda: build_extended_packet(0xAA48, b"\x00" * 250),
        "il frame non puo' superare i 255 byte",
    )


# --------------------------------------------------------------------------
# Parsing delle risposte
# --------------------------------------------------------------------------
def test_risposta_del_manuale() -> None:
    # Risposta allo stop dell'inventory asincrono (manuale §5.6).
    risposta = parse_extended_response(
        _hex("FF 0C AA 0000 4D6F64756C6574656368 AA49 0F22")
    )
    assert risposta.subcmd == 0xAA49
    assert risposta.status == 0x0000
    assert risposta.ok is True
    assert risposta.data == b""


def _risposta(subcmd: int, status: int = 0x0000, subdata: bytes = b"") -> bytes:
    """Costruisce una risposta estesa valida, come farebbe il lettore."""
    payload = (
        P.EXTENDED_MARKER
        + bytes([(subcmd >> 8) & 0xFF, subcmd & 0xFF])
        + subdata
    )
    body = bytes([len(payload), P.CMD_EXTENDED, (status >> 8) & 0xFF, status & 0xFF]) + payload
    crc = P.crc16(body)
    return bytes([P.HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def test_risposta_con_dati() -> None:
    carico = _hex("0BB8 01 01 00 000DF732 78")
    risposta = parse_extended_response(_risposta(0xAA4A, 0x0000, carico))
    assert risposta.subcmd == 0xAA4A
    assert risposta.data == carico


def test_risposta_con_stato_di_errore() -> None:
    risposta = parse_extended_response(_risposta(0xAA4A, 0xAA4A))
    assert risposta.ok is False
    assert risposta.status == 0xAA4A


def test_risposta_non_porta_subcrc_ne_terminatore() -> None:
    # Il manuale li prevede solo in invio: la lunghezza dichiarata deve fermarsi
    # alla fine di SubData, altrimenti il parsing scivola di due byte.
    risposta_grezza = _risposta(0xAA49)
    assert risposta_grezza[1] == len(P.EXTENDED_MARKER) + 2
    assert P.EXTENDED_TERMINATOR not in risposta_grezza[5:-2]


def test_rifiuta_frame_malformati() -> None:
    valido = _risposta(0xAA4A, 0x0000, b"\x01\x02")

    _expect(SilionFrameError, lambda: parse_extended_response(b"\xff\x0c"), "buffer troppo corto")

    header_errato = bytearray(valido)
    header_errato[0] = 0xFE
    _expect(SilionFrameError, lambda: parse_extended_response(bytes(header_errato)), "header errato")

    non_esteso = bytearray(valido)
    non_esteso[2] = 0x22
    _expect(
        SilionFrameError,
        lambda: parse_extended_response(bytes(non_esteso)),
        "frame del formato comune passato al parser esteso",
    )

    lunghezza_errata = bytearray(valido)
    lunghezza_errata[1] += 1
    _expect(
        SilionFrameError,
        lambda: parse_extended_response(bytes(lunghezza_errata)),
        "lunghezza dichiarata incoerente",
    )

    crc_errato = bytearray(valido)
    crc_errato[-1] ^= 0xFF
    _expect(SilionFrameError, lambda: parse_extended_response(bytes(crc_errato)), "CRC errato")


def test_rifiuta_marcatore_assente() -> None:
    # Un frame con opcode 0xAA ma senza "Moduletech" non e' del formato esteso.
    payload = b"NonMarker!" + b"\xaa\x49"
    body = bytes([len(payload), P.CMD_EXTENDED, 0x00, 0x00]) + payload
    crc = P.crc16(body)
    frame = bytes([P.HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    _expect(SilionFrameError, lambda: parse_extended_response(frame), "marcatore mancante")


def test_andata_e_ritorno_su_tutti_i_sottocomandi() -> None:
    for _, subcmd, subdata, _atteso in ESEMPI_MANUALE:
        risposta = parse_extended_response(_risposta(subcmd, 0x0000, subdata))
        assert risposta.subcmd == subcmd
        assert risposta.data == subdata


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
