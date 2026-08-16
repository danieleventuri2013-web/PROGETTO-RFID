"""Test dell'inventory asincrono in modalita' tag densi (0xAA58/0xAA59).

E' l'unica parte del driver in cui non c'e' una domanda e una risposta: il
modulo comincia a spingere i tag da solo e smette quando glielo si dice. Da qui
le due cose che i test devono coprire piu' delle altre:

* un timeout del trasporto **non e' un errore** — significa solo che in quel
  momento non e' arrivato niente;
* il modulo va fermato anche se qualcosa va storto, altrimenti al comando
  successivo si trova a parlare da solo.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_reader import FakeTransport

from rfid_silion import protocol as P
from rfid_silion.reader import SIM7200Reader


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


def _sent_subdata(frame: bytes) -> bytes:
    """SubData di un frame esteso inviato.

    Coda: SubCRC(1) + terminatore(1) + CRC(2). Contarli a mano ogni volta e' un
    invito a sbagliare di un byte.
    """
    testa = 3 + len(P.EXTENDED_MARKER) + 2
    return frame[testa : -(1 + 1 + 2)]


def _extended_reply(subcmd: int, subdata: bytes = b"", status: int = 0x0000) -> bytes:
    payload = P.EXTENDED_MARKER + bytes([(subcmd >> 8) & 0xFF, subcmd & 0xFF]) + subdata
    body = bytes([len(payload), P.CMD_EXTENDED, (status >> 8) & 0xFF, status & 0xFF]) + payload
    crc = P.crc16(body)
    return bytes([P.HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _upload(record: bytes, metadata_flags: int = 0x0007, status: int = 0x0000) -> bytes:
    """Pacchetto auto-caricato: come sopra ma **senza** marcatore Moduletech."""
    payload = metadata_flags.to_bytes(2, "big") + record
    body = bytes([len(payload), P.CMD_EXTENDED, (status >> 8) & 0xFF, status & 0xFF]) + payload
    crc = P.crc16(body)
    return bytes([P.HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


def _record(epc: bytes, *, read_count: int = 1, rssi: int = -45, antenna: int = 1) -> bytes:
    """Record tag con i metadati di default (ReadCount|RSSI|AntennaID)."""
    epc_bits = (len(epc) + 4) * 8
    return (
        bytes([read_count, rssi & 0xFF, (antenna << 4) | antenna])
        + epc_bits.to_bytes(2, "big")
        + b"\x30\x00"          # PC
        + epc
        + b"\x12\x34"          # TagCRC
    )


EPC_A = bytes.fromhex("0100A5000F12060103AAAAA1")
EPC_B = bytes.fromhex("0100A5000F12060203BBBBB2")


# --------------------------------------------------------------------------
# Distinzione fra risposta estesa e pacchetto auto-caricato
# --------------------------------------------------------------------------
def test_il_marcatore_distingue_i_due_formati() -> None:
    """Entrambi hanno opcode 0xAA: solo la risposta porta "Moduletech"."""
    assert P.is_extended_frame(_extended_reply(P.SUBCMD_DENSE_INVENTORY_STOP)) is True
    assert P.is_extended_frame(_upload(_record(EPC_A))) is False


def test_parsing_di_un_pacchetto_auto_caricato() -> None:
    pacchetto = P.parse_async_upload(_upload(_record(EPC_A)))
    assert pacchetto.status == 0x0000
    assert pacchetto.metadata_flags == 0x0007
    assert pacchetto.is_heartbeat is False


def test_heartbeat_riconosciuto() -> None:
    # Non e' un tag: va scartato senza contarlo.
    pacchetto = P.parse_async_upload(_upload(P.ASYNC_HEARTBEAT_MARKER + b"\x00\x00"))
    assert pacchetto.is_heartbeat is True


def test_pacchetto_malformato_rifiutato() -> None:
    valido = _upload(_record(EPC_A))

    _expect(P.SilionFrameError, lambda: P.parse_async_upload(b"\xff\x02"), "troppo corto")

    header_errato = bytearray(valido)
    header_errato[0] = 0xFE
    _expect(
        P.SilionFrameError,
        lambda: P.parse_async_upload(bytes(header_errato)),
        "header errato",
    )

    crc_errato = bytearray(valido)
    crc_errato[-1] ^= 0xFF
    _expect(P.SilionFrameError, lambda: P.parse_async_upload(bytes(crc_errato)), "CRC errato")

    lunghezza_errata = bytearray(valido)
    lunghezza_errata[1] += 1
    _expect(
        P.SilionFrameError,
        lambda: P.parse_async_upload(bytes(lunghezza_errata)),
        "lunghezza incoerente",
    )


# --------------------------------------------------------------------------
# Comandi di avvio e arresto
# --------------------------------------------------------------------------
def test_frame_di_avvio_ha_la_forma_del_manuale() -> None:
    transport = FakeTransport(_extended_reply(P.SUBCMD_DENSE_INVENTORY_START))
    SIM7200Reader(transport).start_dense_inventory()

    inviato = bytes(transport.tx)
    assert inviato[1] == 0x27, "DataLen come nell'esempio del manuale"
    subdata = _sent_subdata(inviato)
    assert len(subdata) == 20 + 2 + 1 + 2
    assert subdata[0] == 0x00, "primo byte 0x00 = modalita' tag densi"
    assert subdata[1:20] == bytes(19), "gli altri 19 byte non sono impostabili"
    assert int.from_bytes(subdata[20:22], "big") == 0x0007


def test_modalita_pochi_tag() -> None:
    transport = FakeTransport(_extended_reply(P.SUBCMD_DENSE_INVENTORY_START))
    SIM7200Reader(transport).start_dense_inventory(dense=False)
    subdata = _sent_subdata(bytes(transport.tx))
    assert subdata[0] == 0x01, "0x01 = pochi tag, facili da leggere"


def test_frame_di_arresto() -> None:
    transport = FakeTransport(_extended_reply(P.SUBCMD_DENSE_INVENTORY_STOP))
    SIM7200Reader(transport).stop_dense_inventory()
    inviato = bytes(transport.tx)
    assert inviato[13:15] == b"\xaa\x59"
    assert inviato[-3] == P.EXTENDED_TERMINATOR


def test_avvio_valida_gli_argomenti() -> None:
    reader = SIM7200Reader(FakeTransport(b""))
    _expect(
        ValueError,
        lambda: reader.start_dense_inventory(metadata_flags=0x10000),
        "metadata oltre 16 bit",
    )
    _expect(
        ValueError,
        lambda: reader.start_dense_inventory(search_flags=-1),
        "search flags negativi",
    )


# --------------------------------------------------------------------------
# Ascolto dei tag spinti dal modulo
# --------------------------------------------------------------------------
def test_raccoglie_i_tag_spinti() -> None:
    transport = FakeTransport(_upload(_record(EPC_A)) + _upload(_record(EPC_B, antenna=2)))
    tag = SIM7200Reader(transport).collect_uploaded_tags(1.0)

    assert [t.epc for t in tag] == [EPC_A.hex().upper(), EPC_B.hex().upper()]
    assert tag[0].rssi == -45
    assert tag[1].antenna_id == 2


def test_il_silenzio_radio_non_e_un_errore() -> None:
    # Il trasporto esaurito ritorna b"": in ascolto significa "niente adesso",
    # non "guasto". Il ciclo deve arrivare a scadenza senza sollevare.
    reader = SIM7200Reader(FakeTransport(b""))
    assert reader.collect_uploaded_tags(0.05) == []
    assert reader.diag.frame_errors == 0, "un timeout in ascolto non e' un errore di frame"


def test_i_frame_di_controllo_non_diventano_tag() -> None:
    transport = FakeTransport(
        _extended_reply(P.SUBCMD_DENSE_INVENTORY_STOP) + _upload(_record(EPC_A))
    )
    tag = SIM7200Reader(transport).collect_uploaded_tags(1.0)
    assert [t.epc for t in tag] == [EPC_A.hex().upper()]


def test_gli_heartbeat_non_diventano_tag() -> None:
    transport = FakeTransport(
        _upload(P.ASYNC_HEARTBEAT_MARKER + b"\x00\x00") + _upload(_record(EPC_A))
    )
    tag = SIM7200Reader(transport).collect_uploaded_tags(1.0)
    assert len(tag) == 1


def test_un_pacchetto_corrotto_non_interrompe_l_ascolto() -> None:
    # Durante una raccolta lunga un frame perso e' verosimile: si conta e si
    # prosegue, invece di buttare via tutto quello che verrebbe dopo.
    corrotto = bytearray(_upload(_record(EPC_A)))
    corrotto[-1] ^= 0xFF
    transport = FakeTransport(bytes(corrotto) + _upload(_record(EPC_B)))

    reader = SIM7200Reader(transport)
    tag = reader.collect_uploaded_tags(1.0)
    assert [t.epc for t in tag] == [EPC_B.hex().upper()]
    assert reader.diag.frame_errors >= 1, "il frame perso va contato"


def test_interruzione_dall_operatore() -> None:
    stop = threading.Event()
    stop.set()
    transport = FakeTransport(_upload(_record(EPC_A)) * 5)
    tag = SIM7200Reader(transport).collect_uploaded_tags(5.0, stop_event=stop)
    assert tag == []


def test_durata_non_positiva_rifiutata() -> None:
    reader = SIM7200Reader(FakeTransport(b""))
    _expect(ValueError, lambda: reader.collect_uploaded_tags(0), "durata nulla")


# --------------------------------------------------------------------------
# Giro completo
# --------------------------------------------------------------------------
def test_dense_inventory_avvia_ascolta_e_ferma() -> None:
    transport = FakeTransport(
        _extended_reply(P.SUBCMD_DENSE_INVENTORY_START)
        + _upload(_record(EPC_A))
        + _upload(_record(EPC_B))
        + _extended_reply(P.SUBCMD_DENSE_INVENTORY_STOP)
    )
    tag = SIM7200Reader(transport).dense_inventory(0.3)

    assert {t.epc for t in tag} == {EPC_A.hex().upper(), EPC_B.hex().upper()}
    inviato = bytes(transport.tx)
    assert b"\xaa\x58" in inviato and b"\xaa\x59" in inviato


def test_il_modulo_viene_fermato_anche_se_l_ascolto_fallisce() -> None:
    """Lasciarlo a trasmettere significherebbe trovarselo addosso dopo."""
    transport = FakeTransport(_extended_reply(P.SUBCMD_DENSE_INVENTORY_START))
    reader = SIM7200Reader(transport)

    def ascolto_guasto(*_args, **_kwargs):
        raise RuntimeError("guasto durante l'ascolto")

    reader.collect_uploaded_tags = ascolto_guasto
    _expect(RuntimeError, lambda: reader.dense_inventory(0.1), "errore propagato")
    assert b"\xaa\x59" in bytes(transport.tx), "lo stop va inviato comunque"


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
