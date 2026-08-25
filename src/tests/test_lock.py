"""Test del comando Lock Tag (0x25) e della compatibilita' fra versioni API.

Il lock e' l'unica operazione del progetto che puo' rendere un tag
definitivamente non riscrivibile. I valori attesi qui sotto vengono dalla
Figura 6 e dalla tabella del manuale EX10 2024-12 §6.3, non da deduzione:
sono il riferimento contro cui verificare il codice.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# la cartella dei test, per riusare FakeTransport e make_response di test_reader
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_reader import FakeTransport, make_response
from test_service import FakeReader

from rfid_silion import protocol as P
from rfid_silion.errors import SilionError
from rfid_silion.reader import SIM7200Reader
from rfid_silion.rpc import _api_version_compatible
from rfid_silion.service import (
    SERVICE_API_VERSION,
    LockMode,
    LockRequest,
    LockTarget,
    RFIDService,
)


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


# --------------------------------------------------------------------------
# Composizione dei campi Mask e Action
# --------------------------------------------------------------------------
def test_disposizione_bit_dalla_figura_6() -> None:
    # Bit 9..0: Kill Pwd(R/W,Perm) Access Pwd(R/W,Perm) EPC(W,Perm)
    #           TID(W,Perm) User(W,Perm); i bit 15..10 non sono usati.
    assert P.LOCK_FIELD_BITS["kill_password"] == (9, 8)
    assert P.LOCK_FIELD_BITS["access_password"] == (7, 6)
    assert P.LOCK_FIELD_BITS["epc"] == (5, 4)
    assert P.LOCK_FIELD_BITS["tid"] == (3, 2)
    assert P.LOCK_FIELD_BITS["user"] == (1, 0)


def test_cinque_operazioni_del_manuale() -> None:
    # Tabella §6.3, come (mask_W, mask_Perm, action_W, action_Perm) sulla banca USER.
    assert P.build_lock_bits({"user": P.LOCK_NO_ACTION}) == (0x0000, 0x0000)
    assert P.build_lock_bits({"user": P.LOCK_LOCK}) == (0x0002, 0x0002)
    assert P.build_lock_bits({"user": P.LOCK_UNLOCK}) == (0x0002, 0x0000)
    assert P.build_lock_bits({"user": P.LOCK_PERMALOCK}) == (0x0003, 0x0003)
    assert P.build_lock_bits({"user": P.LOCK_PERMAUNLOCK}) == (0x0003, 0x0001)


def test_lock_su_banche_diverse() -> None:
    assert P.build_lock_bits({"epc": P.LOCK_LOCK}) == (0x0020, 0x0020)
    assert P.build_lock_bits({"tid": P.LOCK_LOCK}) == (0x0008, 0x0008)
    assert P.build_lock_bits({"access_password": P.LOCK_LOCK}) == (0x0080, 0x0080)
    assert P.build_lock_bits({"kill_password": P.LOCK_LOCK}) == (0x0200, 0x0200)


def test_lock_combinato_somma_i_bit() -> None:
    mask, action = P.build_lock_bits({"epc": P.LOCK_LOCK, "user": P.LOCK_LOCK})
    assert (mask, action) == (0x0022, 0x0022)


def test_banche_non_citate_restano_intoccate() -> None:
    mask, _ = P.build_lock_bits({"user": P.LOCK_LOCK})
    for campo in ("epc", "tid", "access_password", "kill_password"):
        bit_w, bit_perm = P.LOCK_FIELD_BITS[campo]
        assert not mask & (1 << bit_w), f"{campo} non doveva essere toccata"
        assert not mask & (1 << bit_perm), f"{campo} non doveva essere toccata"


def test_build_lock_bits_rifiuta_valori_sconosciuti() -> None:
    _expect(ValueError, lambda: P.build_lock_bits({"banca_inventata": "lock"}), "campo ignoto")
    _expect(ValueError, lambda: P.build_lock_bits({"user": "distruggi"}), "operazione ignota")


def test_operazioni_permanenti_elencate() -> None:
    assert P.LOCK_PERMANENT_OPERATIONS == {P.LOCK_PERMALOCK, P.LOCK_PERMAUNLOCK}


# --------------------------------------------------------------------------
# Frame del comando 0x25
# --------------------------------------------------------------------------
def _reader(status: int = 0x0000) -> tuple[SIM7200Reader, FakeTransport]:
    """Lettore su trasporto in memoria, con la risposta al lock gia' precaricata."""
    transport = FakeTransport(make_response(P.CMD_LOCK_TAG, status))
    return SIM7200Reader(transport), transport


def test_frame_lock_ha_il_formato_del_manuale() -> None:
    reader, transport = _reader()
    mask, action = P.build_lock_bits({"user": P.LOCK_LOCK})
    reader.lock_tag(mask, action, access_password=bytes.fromhex("11223344"), timeout_ms=1000)

    frame = bytes(transport.tx)
    assert frame[0] == P.HEADER
    assert frame[2] == P.CMD_LOCK_TAG == 0x25
    data = frame[3:-2]
    # Timeout(2) | Option(1) | AccessPassword(4) | Mask(2) | Action(2)
    assert len(data) == 11
    assert data[0:2] == (1000).to_bytes(2, "big")
    assert data[2] == 0x00, "il manuale vieta Option 0x05 per il comando 0x25"
    assert data[3:7] == bytes.fromhex("11223344")
    assert data[7:9] == b"\x00\x02"
    assert data[9:11] == b"\x00\x02"
    # CRC calcolato sul corpo, header escluso
    assert int.from_bytes(frame[-2:], "big") == P.crc16(frame[1:-2])


def test_lock_propaga_lo_stato_di_errore() -> None:
    reader, _ = _reader(status=0x0400)   # nessun tag nel campo
    mask, action = P.build_lock_bits({"user": P.LOCK_LOCK})
    _expect(SilionError, lambda: reader.lock_tag(mask, action), "stato di errore ignorato")


def test_lock_valida_gli_argomenti() -> None:
    reader, _ = _reader()
    _expect(ValueError, lambda: reader.lock_tag(0x0400, 0x0000), "mask oltre i 10 bit")
    _expect(ValueError, lambda: reader.lock_tag(0x0000, 0x0400), "action oltre i 10 bit")
    _expect(ValueError, lambda: reader.lock_tag(0x0002, 0x0001), "action fuori dalla mask")
    _expect(ValueError, lambda: reader.lock_tag(0x0002, 0x0002, b"\x00"), "password corta")
    _expect(TypeError, lambda: reader.lock_tag(True, 0x0000), "mask booleana")


# --------------------------------------------------------------------------
# Guardie del servizio
# --------------------------------------------------------------------------
EPC_IN_CAMPO = "E2000017221101441890ABCD"


def _service() -> tuple[RFIDService, FakeReader]:
    """Servizio avviato sul lettore simulato condiviso con `test_service`."""
    reader = FakeReader()
    service = RFIDService({}, reader_factory=lambda _cfg: reader)
    avvio = service.start()
    assert avvio.ok, avvio.error
    return service, reader


def test_lock_richiede_inventory_con_un_solo_epc_atteso() -> None:
    service, reader = _service()
    richiesta = LockRequest(
        targets={LockTarget.USER: LockMode.LOCK},
        expected_epc=EPC_IN_CAMPO,
    )
    # Senza inventory precedente la guardia deve scattare.
    risposta = service.lock(richiesta)
    assert risposta.ok is False
    assert "inventory" in risposta.error["message"]
    assert reader.lock_calls == []

    service.inventory({"antennas": [1]})
    assert service.lock(richiesta).ok is True
    assert len(reader.lock_calls) == 1
    _, mask, action, _, _ = reader.lock_calls[0]
    assert (mask, action) == (0x0002, 0x0002)
    service.stop()


def test_lock_rifiuta_epc_diverso_da_quello_in_campo() -> None:
    service, reader = _service()
    service.inventory({"antennas": [1]})
    risposta = service.lock(
        LockRequest(targets={"user": "lock"}, expected_epc="FFFFFFFFFFFFFFFFFFFFFFFF")
    )
    assert risposta.ok is False
    assert reader.lock_calls == []
    service.stop()


def test_operazione_permanente_richiede_consenso_esplicito() -> None:
    service, reader = _service()
    service.inventory({"antennas": [1]})
    permanente = dict(
        targets={"user": "permalock"}, expected_epc=EPC_IN_CAMPO
    )

    negata = service.lock(LockRequest(**permanente))
    assert negata.ok is False
    assert "allow_permanent" in negata.error["message"]
    assert reader.lock_calls == [], "un permalock non consentito non deve raggiungere il tag"

    concessa = service.lock(LockRequest(**permanente, allow_permanent=True))
    assert concessa.ok is True
    _, mask, action, _, _ = reader.lock_calls[0]
    assert (mask, action) == (0x0003, 0x0003)
    assert concessa.data["permanent"] == ["user"]
    service.stop()


def test_lock_non_permanente_non_richiede_consenso() -> None:
    service, _ = _service()
    service.inventory({"antennas": [1]})
    risposta = service.lock(
        LockRequest(targets={"user": "lock", "epc": "lock"},
                    expected_epc=EPC_IN_CAMPO)
    )
    assert risposta.ok is True
    assert risposta.data["permanent"] == []
    assert risposta.data["mask"] == "0x0022"
    service.stop()


def test_lock_emette_un_evento() -> None:
    service, _ = _service()
    service.inventory({"antennas": [1]})
    service.lock(
        LockRequest(targets={"user": "lock"}, expected_epc=EPC_IN_CAMPO)
    )
    tipi = [evento["kind"] for evento in service.recent_events(0)]
    assert "tag.locked" in tipi
    service.stop()


def test_lock_request_valida_gli_ingressi() -> None:
    _expect(
        ValueError,
        lambda: LockRequest(targets={}, expected_epc="AABB"),
        "nessuna banca indicata",
    )
    _expect(
        ValueError,
        lambda: LockRequest(targets={"inventata": "lock"}, expected_epc="AABB"),
        "banca inesistente",
    )
    _expect(
        ValueError,
        lambda: LockRequest(targets={"user": "distruggi"}, expected_epc="AABB"),
        "modo inesistente",
    )


def test_lock_request_riconosce_i_bersagli_permanenti() -> None:
    richiesta = LockRequest(
        targets={"user": "permalock", "epc": "lock"}, expected_epc="AABB"
    )
    assert richiesta.permanent_targets == ("user",)


# --------------------------------------------------------------------------
# Compatibilita' fra versioni dell'API
# --------------------------------------------------------------------------
def test_tutte_le_minor_precedenti_restano_servite() -> None:
    # Il contratto promette che in 1.x si aggiunge soltanto: nessun client
    # scritto per una minor precedente deve smettere di funzionare quando il
    # service avanza. Il test e' scritto sulla regola, non sul numero di
    # versione corrente, cosi' resta valido a ogni incremento.
    major, minor = (int(parte) for parte in SERVICE_API_VERSION.split("."))
    for precedente in range(minor + 1):
        assert _api_version_compatible(f"{major}.{precedente}") is True, (
            f"un client {major}.{precedente} non deve essere respinto da {SERVICE_API_VERSION}"
        )


def test_api_versione_futura_o_estranea_respinta() -> None:
    major, minor = (int(parte) for parte in SERVICE_API_VERSION.split("."))
    assert _api_version_compatible(f"{major}.{minor + 1}") is False, (
        "una minor successiva potrebbe usare metodi che qui non esistono"
    )
    assert _api_version_compatible(f"{major + 1}.0") is False
    assert _api_version_compatible(f"{major - 1}.9") is False
    assert _api_version_compatible(None) is False
    assert _api_version_compatible("uno.zero") is False
    assert _api_version_compatible("1") is False


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
