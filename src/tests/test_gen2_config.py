"""Test dei parametri Gen2, della configurazione lettore e della diagnostica antenna.

Sono le leve radio da cui dipende la lettura completa di una scatola chiusa.
Gli esempi sono trascritti dal manuale EX10 2024-12 §7.1, §7.8 e §AA4A: qui il
confronto e' byte per byte, perche' un frame sbagliato su questi comandi non da'
errore — cambia in silenzio il comportamento radio.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_reader import FakeTransport, make_response

from rfid_silion import protocol as P
from rfid_silion.errors import SilionError
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


def _reader(risposta: bytes = b"") -> tuple[SIM7200Reader, FakeTransport]:
    transport = FakeTransport(risposta)
    return SIM7200Reader(transport), transport


def _sent_subdata(frame: bytes) -> bytes:
    """Estrae la SubData da un frame esteso inviato.

    Davanti: header, DataLen, cmd, marcatore, sottocomando.
    In coda: SubCRC, terminatore, CRC. Contarli a mano ogni volta e' un invito
    a sbagliare di un byte.
    """
    testa = 3 + len(P.EXTENDED_MARKER) + 2
    coda = 1 + 1 + 2
    return frame[testa:-coda]


def _extended_reply(subcmd: int, subdata: bytes = b"", status: int = 0x0000) -> bytes:
    payload = P.EXTENDED_MARKER + bytes([(subcmd >> 8) & 0xFF, subcmd & 0xFF]) + subdata
    body = bytes([len(payload), P.CMD_EXTENDED, (status >> 8) & 0xFF, status & 0xFF]) + payload
    crc = P.crc16(body)
    return bytes([P.HEADER]) + body + bytes([(crc >> 8) & 0xFF, crc & 0xFF])


# --------------------------------------------------------------------------
# Parametri Gen2 (0x9B) — esempi del manuale §7.8
# --------------------------------------------------------------------------
def test_frame_dei_quattro_esempi_del_manuale() -> None:
    casi = [
        ("Set Session 1", lambda r: r.set_gen2_session(1), "FF 03 9B 05 00 01 DC E9"),
        ("Set Target B statico", lambda r: r.set_gen2_target(1), "FF 04 9B 05 01 01 01 A2 FC"),
        ("Set RF MODE 0x6F", lambda r: r.set_gen2_rf_mode(0x6F), "FF 03 9B 05 02 6F DE 87"),
        ("Set Q statico 3", lambda r: r.set_gen2_q(3), "FF 04 9B 05 12 01 03 80 AC"),
    ]
    for nome, azione, atteso in casi:
        reader, transport = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG))
        azione(reader)
        assert bytes(transport.tx) == _hex(atteso), (
            f"{nome}: prodotto {bytes(transport.tx).hex(' ').upper()}"
        )


def test_target_dinamico_usa_option_zero() -> None:
    # Il ribaltamento automatico A<->B e' Option 0x00; e' la differenza che fa
    # rispondere ogni tag una volta per passata invece di lasciare parlare
    # sempre i soliti.
    reader, transport = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG))
    reader.set_gen2_target(0, dynamic=True)
    dati = bytes(transport.tx)[3:-2]
    assert dati == bytes([P.PROTOCOL_GEN2, P.GEN2_PARAM_TARGET, P.GEN2_TARGET_DYNAMIC, 0x00])


def test_q_dinamico_non_invia_il_valore() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG))
    reader.set_gen2_q(None)
    dati = bytes(transport.tx)[3:-2]
    assert dati == bytes([P.PROTOCOL_GEN2, P.GEN2_PARAM_Q, P.GEN2_Q_DYNAMIC])


def test_sessione_s2_e_rf_mode_ad_alta_sensibilita() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG))
    reader.set_gen2_session(2)
    assert bytes(transport.tx)[3:-2] == bytes([P.PROTOCOL_GEN2, P.GEN2_PARAM_SESSION, 2])

    reader, transport = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG))
    reader.set_gen2_rf_mode(P.RF_MODE_MAX_SENSITIVITY)
    assert bytes(transport.tx)[3:-2] == bytes(
        [P.PROTOCOL_GEN2, P.GEN2_PARAM_RF_MODE, P.RF_MODE_MAX_SENSITIVITY]
    )


def test_la_modalita_ad_alta_sensibilita_guadagna_cinque_db() -> None:
    # E' il motivo per cui esiste `RF_MODE_MAX_SENSITIVITY`.
    default = P.RF_MODE_SENSITIVITY_DBM[P.RF_MODE_DEFAULT]
    massima = P.RF_MODE_SENSITIVITY_DBM[P.RF_MODE_MAX_SENSITIVITY]
    assert default == -88 and massima == -93
    assert default - massima == 5


def test_parametri_gen2_fuori_intervallo_rifiutati() -> None:
    reader, _ = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG))
    _expect(ValueError, lambda: reader.set_gen2_session(4), "sessione oltre S3")
    _expect(ValueError, lambda: reader.set_gen2_session(-1), "sessione negativa")
    _expect(ValueError, lambda: reader.set_gen2_target(2), "target diverso da A/B")
    _expect(ValueError, lambda: reader.set_gen2_q(16), "Q oltre 15")
    _expect(ValueError, lambda: reader.set_gen2_rf_mode(0x100), "rf_mode oltre un byte")


def test_stato_di_errore_propagato() -> None:
    reader, _ = _reader(make_response(P.CMD_SET_PROTOCOL_CONFIG, 0x0105))
    _expect(SilionError, lambda: reader.set_gen2_session(1), "stato di errore ignorato")


# --------------------------------------------------------------------------
# Rilettura dei parametri (0x6B)
# --------------------------------------------------------------------------
def test_get_gen2_param_ritorna_option_e_valore() -> None:
    risposta = make_response(
        P.CMD_GET_PROTOCOL_CONFIG, 0x0000, bytes([P.PROTOCOL_GEN2, P.GEN2_PARAM_Q, 0x01, 0x03])
    )
    reader, transport = _reader(risposta)
    assert reader.get_gen2_param(P.GEN2_PARAM_Q) == bytes([0x01, 0x03])
    assert bytes(transport.tx)[3:-2] == bytes([P.PROTOCOL_GEN2, P.GEN2_PARAM_Q])


def test_get_gen2_param_rifiuta_risposta_su_parametro_diverso() -> None:
    # Il manuale avverte che un RF MODE non supportato viene accettato con stato
    # 0x0000 ma ripiegando su un altro modo: la rilettura e' l'unica difesa, e
    # deve accorgersi se il modulo risponde per un parametro che non era quello.
    risposta = make_response(
        P.CMD_GET_PROTOCOL_CONFIG, 0x0000, bytes([P.PROTOCOL_GEN2, P.GEN2_PARAM_SESSION, 0x00])
    )
    reader, _ = _reader(risposta)
    _expect(
        P.SilionFrameError,
        lambda: reader.get_gen2_param(P.GEN2_PARAM_RF_MODE),
        "risposta per un parametro diverso",
    )


# --------------------------------------------------------------------------
# Configurazione lettore (0x95)
# --------------------------------------------------------------------------
def test_dwell_time_per_antenna() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_READER_CONFIG))
    reader.set_antenna_dwell_time(5000)
    # Esempio del manuale: FF 05 95 02 00001388 D5AB
    assert bytes(transport.tx) == _hex("FF 05 95 02 00001388 D5AB")


def test_duty_cycle() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_READER_CONFIG))
    reader.set_duty_cycle(5000, 1000)
    # Esempio del manuale: FF 05 95 11 1388 03E8 9958
    assert bytes(transport.tx) == _hex("FF 05 95 11 1388 03E8 9958")


def test_configurazione_lettore_valida_gli_intervalli() -> None:
    reader, _ = _reader(make_response(P.CMD_SET_READER_CONFIG))
    _expect(ValueError, lambda: reader.set_antenna_dwell_time(10), "dwell sotto il minimo")
    _expect(ValueError, lambda: reader.set_antenna_dwell_time(60001), "dwell sopra il massimo")
    _expect(ValueError, lambda: reader.set_duty_cycle(70000, 1000), "duty oltre 16 bit")


# --------------------------------------------------------------------------
# Diagnostica antenna (0xAA4A)
# --------------------------------------------------------------------------
def test_frame_onda_stazionaria_uguale_al_manuale() -> None:
    reader, transport = _reader(b"")
    try:
        reader.measure_standing_wave(1, band=0x01)
    except P.SilionFrameError:
        pass  # nessuna risposta precaricata: qui interessa solo il frame inviato
    assert bytes(transport.tx) == _hex("FF 13 AA 4D6F64756C6574656368 AA4A 0BB8 01 01 00 B9 BB BD67")


def test_vswr_riproduce_il_calcolo_del_manuale() -> None:
    # Esempio del manuale: return loss grezzo 0x78 -> VSWR 1.67
    assert round(P.vswr_from_return_loss(0x78), 2) == 1.67
    # Riflessione totale: nessun VSWR finito, e non deve dividere per zero.
    assert P.vswr_from_return_loss(0x00) == float("inf")
    # Piu' return loss, migliore l'adattamento: il VSWR deve scendere.
    assert P.vswr_from_return_loss(0xC8) < P.vswr_from_return_loss(0x78)


def test_misura_onda_stazionaria_decodificata() -> None:
    # Eco dei 5 byte inviati + due punti di misura.
    eco = _hex("0BB8 01 08 00")
    # Un punto di misura sono 4 byte: 3 di frequenza in kHz + 1 di return loss.
    punti = _hex("0DF732 78") + _hex("0D3A3C 14")   # 915250 kHz e 866876 kHz
    reader, _ = _reader(_extended_reply(P.SUBCMD_STANDING_WAVE, eco + punti))
    misure = reader.measure_standing_wave(1)

    assert len(misure) == 2
    assert misure[0]["frequency_khz"] == 915250
    assert misure[0]["return_loss_db"] == 12.0
    assert round(misure[0]["vswr"], 2) == 1.67
    assert misure[0]["ok"] is True

    # 0x14 = 2.0 dB di return loss: adattamento pessimo, oltre la soglia.
    assert misure[1]["frequency_khz"] == 866876
    assert misure[1]["vswr"] > P.VSWR_ALERT_THRESHOLD
    assert misure[1]["ok"] is False


def test_misura_con_elenco_di_frequenze() -> None:
    # Frequenze del canale EU: vanno inviate in kHz su 3 byte, big-endian.
    eu = [865700, 866300, 866900, 867500]
    eco = _hex("0BB8 02 08 04")
    reader, transport = _reader(_extended_reply(P.SUBCMD_STANDING_WAVE, eco))
    reader.measure_standing_wave(2, frequencies_khz=eu)

    inviato = bytes(transport.tx)
    conteggio = 3 + len(P.EXTENDED_MARKER) + 2 + 2 + 1 + 1  # ..subcmd, potenza, antenna, banda
    assert inviato[conteggio - 1] == 0x08, "il default deve restare nella banda EU"
    assert inviato[conteggio] == len(eu), "il numero di frequenze deve precedere l'elenco"
    elenco = inviato[conteggio + 1 : conteggio + 1 + 3 * len(eu)]
    assert [
        int.from_bytes(elenco[i : i + 3], "big") for i in range(0, len(elenco), 3)
    ] == eu


def test_misura_rifiuta_corpo_non_multiplo_di_quattro() -> None:
    eco = _hex("0BB8 01 08 00")
    reader, _ = _reader(_extended_reply(P.SUBCMD_STANDING_WAVE, eco + b"\x01\x02\x03"))
    _expect(
        P.SilionFrameError,
        lambda: reader.measure_standing_wave(1),
        "misure di lunghezza incoerente",
    )


def test_misura_valida_gli_argomenti() -> None:
    reader, _ = _reader(b"")
    _expect(ValueError, lambda: reader.measure_standing_wave(9), "antenna inesistente")
    _expect(
        ValueError,
        lambda: reader.measure_standing_wave(1, frequencies_khz=[0x1000000]),
        "frequenza oltre 3 byte",
    )


def test_soglia_di_allarme_del_manuale() -> None:
    assert P.VSWR_ALERT_THRESHOLD == 7.0


# --------------------------------------------------------------------------
# Impostazioni che sabotano la lettura ripetuta se lasciate al valore di fabbrica
# --------------------------------------------------------------------------
def test_power_mode_frame_del_manuale() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_POWER_MODE))
    reader.set_power_mode(P.POWER_MODE_LOWEST)
    # Esempio del manuale §7.6: FF 01 98 03 44BE
    assert bytes(transport.tx) == _hex("FF 01 98 03 44BE")


def test_power_mode_reattivo_e_il_default() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_POWER_MODE))
    reader.set_power_mode()
    assert bytes(transport.tx)[3] == P.POWER_MODE_RESPONSIVE
    assert P.POWER_MODE_RESPONSIVE in P.POWER_MODES_RESPONSIVE


def test_power_mode_valida_l_intervallo() -> None:
    reader, _ = _reader(make_response(P.CMD_SET_POWER_MODE))
    _expect(ValueError, lambda: reader.set_power_mode(4), "modalita' inesistente")


def test_rssi_report_mode_massimo() -> None:
    reader, transport = _reader(make_response(P.CMD_SET_UNIQUE_CONFIG))
    reader.set_rssi_report_mode()
    assert bytes(transport.tx)[3:-2] == bytes(
        [P.UNIQUE_OPT, P.UNIQUE_KEY_RSSI_MODE, P.RSSI_MODE_MAX]
    )
    reader, _ = _reader(make_response(P.CMD_SET_UNIQUE_CONFIG))
    _expect(ValueError, lambda: reader.set_rssi_report_mode(2), "modalita' RSSI inesistente")


def test_filtro_rssi_disattivato() -> None:
    # Il caso che conta: un filtro ereditato scarterebbe in silenzio i tag deboli.
    reader, transport = _reader(_extended_reply(P.SUBCMD_RSSI_FILTER, b"\x01"))
    reader.set_rssi_filter(None)
    assert _sent_subdata(bytes(transport.tx)) == bytes([0x01, 0x00, 0x00, 0x00])


def test_filtro_rssi_impostato() -> None:
    reader, transport = _reader(_extended_reply(P.SUBCMD_RSSI_FILTER, b"\x01"))
    reader.set_rssi_filter(-50)
    # Esempio del manuale: 01 AA CE 00, dove 0xCE e' -50 come byte con segno.
    assert _sent_subdata(bytes(transport.tx)) == _hex("01 AA CE 00")


def test_filtro_rssi_valida_la_soglia() -> None:
    reader, _ = _reader(_extended_reply(P.SUBCMD_RSSI_FILTER, b"\x01"))
    _expect(ValueError, lambda: reader.set_rssi_filter(0), "soglia positiva")
    _expect(ValueError, lambda: reader.set_rssi_filter(-200), "soglia oltre un byte con segno")


def test_lettura_del_filtro_rssi() -> None:
    # Risposte del manuale: 00000000 = disattivato, 00AACE00 = attivo a -50.
    reader, transport = _reader(_extended_reply(P.SUBCMD_RSSI_FILTER, _hex("00000000")))
    assert reader.get_rssi_filter() is None
    assert _sent_subdata(bytes(transport.tx)) == P.RSSI_FILTER_QUERY

    reader, _ = _reader(_extended_reply(P.SUBCMD_RSSI_FILTER, _hex("00AACE00")))
    assert reader.get_rssi_filter() == -50


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
