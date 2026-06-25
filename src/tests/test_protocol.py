"""Unit test del protocollo Silion (senza hardware).

Verifica CRC-16, build/parse dei frame usando gli esempi del manuale
MANUALI/Communication_Protocol_Doc__20210716.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion import protocol as P
from rfid_silion.tags import parse_tag_buffer


def test_crc_boot_firmware():
    """Boot Firmware (0x04): FF 00 04 1D 0B.

    Nota: il valore 1D 0C che si trova nei sample del manuale NON e' un typo del
    CRC di 0x04, ma il CRC del comando 0x03 (Get Version): FF 00 03 1D 0C.
    Sono due comandi diversi -> 0x04 = 1D 0B, 0x03 = 1D 0C (vedi CRC16test.cpp).
    """
    pkt = P.build_packet(P.CMD_BOOT_FIRMWARE, b"")
    assert pkt == bytes([0xFF, 0x00, 0x04, 0x1D, 0x0B]), pkt.hex(" ").upper()


def test_crc_get_antenna_option00():
    """Get Antenna Ports option 0x00: FF 01 61 00 -> CRC BD BD."""
    pkt = P.build_packet(P.CMD_GET_ANTENNA_PORTS, bytes([0x00]))
    assert pkt == bytes([0xFF, 0x01, 0x61, 0x00, 0xBD, 0xBD]), pkt.hex(" ").upper()


def test_crc_set_antenna_access():
    """Set Antenna access option 0x00, ant 1: FF 03 91 00 01 01 -> CRC 62 87."""
    pkt = P.build_packet(P.CMD_SET_ANTENNA_PORTS, bytes([0x00, 0x01, 0x01]))
    assert pkt == bytes([0xFF, 0x03, 0x91, 0x00, 0x01, 0x01, 0x62, 0x87]), \
        pkt.hex(" ").upper()


def test_crc_set_region():
    """Set Region NA: FF 01 97 01 -> CRC 4B BC."""
    pkt = P.build_packet(P.CMD_SET_CURRENT_REGION, bytes([0x01]))
    assert pkt == bytes([0xFF, 0x01, 0x97, 0x01, 0x4B, 0xBC]), pkt.hex(" ").upper()


def test_parse_response_boot():
    """Risposta Boot Firmware del manuale."""
    raw = bytes.fromhex("FF 14 04 00 00 13 04 15 00 A8 00 00 01 "
                        "20 13 05 22 13 05 23 00 00 00 00 10".replace(" ", ""))
    # CRC del manuale non fornito nel testo: ricalcoliamo per validare il parser
    crc = P.crc16(raw[1:])
    frame = raw + bytes([(crc >> 8) & 0xFF, crc & 0xFF])
    resp = P.parse_response(frame)
    assert resp.cmd == 0x04
    assert resp.status == 0x0000
    assert resp.ok
    assert len(resp.data) == 0x14


def test_parse_response_get_antenna():
    """Risposta Get Antenna Ports ex.1: ...03 03 4C 20."""
    raw = bytes([0xFF, 0x02, 0x61, 0x00, 0x00, 0x03, 0x03, 0x4C, 0x20])
    resp = P.parse_response(raw)
    assert resp.cmd == 0x61
    assert resp.status == 0x0000
    assert resp.data == bytes([0x03, 0x03])


def test_parse_tag_buffer_default_flags():
    """Get Tag Buffer (0x29) con i flag di default 0x0007 (ReadCount|RSSI|AntennaID).

    Regressione del bug del campo "Tag Data Length" inesistente: il record per
    tag e' [metadati] | EpcLength(2,bit) | PC(2) | EPC(N) | TagCRC(2),
    con N = EpcLength/8 - 4. Verifica anche EPC di lunghezza diversa e l'RSSI
    signed, e che il buffer venga consumato per intero (nessun residuo).
    """
    flags = 0x0007
    # MetadataFlags(2) | Option(1) | TagCount(1)
    header = bytes([0x00, 0x07, 0x00, 0x02])
    # Tag A: RC=5, RSSI=0xC8(-56), Ant byte 0x11 (TX1|RX1 -> logica 1),
    #        EpcLen=0x0080(128bit -> 12B EPC), PC=0x3000
    epc_a = bytes.fromhex("E200001722110123456789AB")          # 12 byte
    tag_a = (bytes([0x05, 0xC8, 0x11]) + bytes([0x00, 0x80]) +
             bytes([0x30, 0x00]) + epc_a + bytes([0xAB, 0xCD]))
    # Tag B: RC=2, RSSI=0xA0(-96), Ant byte 0x22 (-> logica 2),
    #        EpcLen=0x0060(96bit -> 8B EPC), PC=0x2000
    epc_b = bytes.fromhex("AABBCCDDEEFF0011")                   # 8 byte
    tag_b = (bytes([0x02, 0xA0, 0x22]) + bytes([0x00, 0x60]) +
             bytes([0x20, 0x00]) + epc_b + bytes([0x12, 0x34]))

    tags = parse_tag_buffer(header + tag_a + tag_b, flags)
    assert len(tags) == 2, tags
    a, b = tags
    assert a.epc == "E200001722110123456789AB"
    assert a.read_count == 5 and a.rssi == -56 and a.antenna_id == 1
    assert a.pc == 0x3000 and a.crc == 0xABCD
    assert b.epc == "AABBCCDDEEFF0011"
    assert b.read_count == 2 and b.rssi == -96 and b.antenna_id == 2
    assert b.pc == 0x2000 and b.crc == 0x1234


def test_parse_tag_buffer_timestamp_flags():
    """0x29 con flag 0x0015 (ReadCount|AntennaID|Timestamp), RSSI assente.

    Verifica l'ordine dei metadati: saltando l'RSSI (BIT1 non attivo) e leggendo
    il Timestamp a 4 byte, l'allineamento al blocco EPC deve restare corretto.
    """
    flags = 0x0015
    header = bytes([0x00, 0x15, 0x00, 0x01])
    epc = bytes.fromhex("111122223333444455556666")            # 12 byte
    # ordine 0x0015: ReadCount(0x03), AntennaID(byte 0x11 -> logica 1), Timestamp(4)
    tag = (bytes([0x03, 0x11]) + bytes([0x00, 0x00, 0x00, 0x64]) +
           bytes([0x00, 0x80]) + bytes([0x31, 0xC1]) + epc + bytes([0xFB, 0x15]))

    tags = parse_tag_buffer(header + tag, flags)
    assert len(tags) == 1, tags
    t = tags[0]
    assert t.epc == "111122223333444455556666"
    assert t.read_count == 3 and t.antenna_id == 1
    assert t.timestamp == 0x64
    assert t.rssi is None and t.pc == 0x31C1 and t.crc == 0xFB15


def test_data_length_limit():
    try:
        P.build_packet(0x22, b"\x00" * 253)
        assert False, "dovrebbe sollevare"
    except ValueError:
        pass


def _run_all() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
