"""Test del sigillo di una scatola: certificare cosa contiene alla chiusura.

I tag simulati qui non sono tutti uguali: alcuni rispondono solo da certe
antenne, altri solo alzando la potenza, altri un ciclo su tre. E' l'unico modo
per verificare che la strategia multi-passata serva davvero, e non solo che il
codice giri.

Il test che conta di piu' e' `test_non_dichiara_mai_completo_un_insieme_incompleto`:
tutto il resto e' migliorabile, quello e' la promessa da non tradire.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag
from lims.codec import build_box_epc, build_epc
from lims.sealing import (
    ClosureProof,
    ReadPass,
    SealingPolicy,
    SealingRecord,
    SealingSession,
    default_passes,
)

LAB = 0x00A5
ACC = 987654


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _epc(indice: int, totale: int) -> bytes:
    return build_epc(LAB, ACC, indice, totale, random_suffix=bytes([0, 0, indice]))


def _facile(indice: int, totale: int, **kwargs) -> SimulatedTag:
    return SimulatedTag(_epc(indice, totale), user_bytes=64, **kwargs)


def _sessione(backend, attesi, *, passes=None, policy=None, **kwargs) -> SealingSession:
    return SealingSession(
        backend,
        [e.hex().upper() if isinstance(e, bytes) else e for e in attesi],
        passes=passes or default_passes((1, 2, 3)),
        policy=policy or SealingPolicy(stable_passes=1, max_passes=20, pause_s=0.0),
        **kwargs,
    )


# --------------------------------------------------------------------------
# Caso nominale
# --------------------------------------------------------------------------
def test_scatola_completa_di_tag_facili() -> None:
    tag = [_facile(i, 3) for i in (1, 2, 3)]
    backend = FakeTagBackend(tag)
    record = _sessione(backend, [t.epc for t in tag]).run(
        closure_proof=ClosureProof.OPERATOR
    )

    assert record.ok is True, record.error
    assert record.complete is True
    assert record.counts == (3, 3)
    assert record.missing == ()
    assert record.unexpected == ()
    assert record.closure_proof is ClosureProof.OPERATOR


def test_si_ferma_appena_trova_tutto() -> None:
    # Non deve consumare tutte le passate se l'insieme e' gia' completo e stabile.
    tag = [_facile(i, 2) for i in (1, 2)]
    backend = FakeTagBackend(tag)
    policy = SealingPolicy(stable_passes=2, max_passes=30, pause_s=0.0)
    record = _sessione(backend, [t.epc for t in tag], policy=policy).run()

    assert record.ok is True
    assert record.passes_run < 30
    assert "stabili" in record.stop_reason


# --------------------------------------------------------------------------
# La promessa da non tradire
# --------------------------------------------------------------------------
def test_non_dichiara_mai_completo_un_insieme_incompleto() -> None:
    """Un contenitore atteso ma assente deve sempre risultare mancante."""
    presenti = [_facile(i, 3) for i in (1, 3)]
    attesi = [_epc(i, 3).hex().upper() for i in (1, 2, 3)]
    backend = FakeTagBackend(presenti)

    record = _sessione(backend, attesi).run()

    assert record.ok is False
    assert record.complete is False
    assert record.counts == (2, 3)
    assert record.missing == (_epc(2, 3).hex().upper(),)


def test_un_tag_muto_non_viene_inventato() -> None:
    # Nessun tag risponde: il verdetto deve essere "mancano tutti", non "vuoto ok".
    attesi = [_epc(i, 2).hex().upper() for i in (1, 2)]
    record = _sessione(FakeTagBackend([]), attesi).run()
    assert record.ok is False
    assert len(record.missing) == 2
    assert record.found == ()


def test_distinta_vuota_non_e_un_sigillo_valido() -> None:
    record = _sessione(FakeTagBackend([]), []).run()
    assert record.ok is False, "una scatola senza distinta non e' certificabile"


# --------------------------------------------------------------------------
# Tag difficili: e' qui che serve la strategia multi-passata
# --------------------------------------------------------------------------
def test_recupera_un_tag_visibile_da_una_sola_antenna() -> None:
    difficile = _facile(2, 3, visible_from={3})
    tag = [_facile(1, 3), difficile, _facile(3, 3)]
    backend = FakeTagBackend(tag)

    record = _sessione(backend, [t.epc for t in tag]).run()
    assert record.ok is True, f"mancanti: {record.missing}"
    prova = next(item for item in record.evidence if item.epc == difficile.epc_hex)
    assert prova.antennas == (3,)


def test_recupera_un_tag_che_risponde_solo_ad_alta_potenza() -> None:
    difficile = _facile(2, 3, min_read_power_cdbm=3000)
    tag = [_facile(1, 3), difficile, _facile(3, 3)]
    backend = FakeTagBackend(tag)

    record = _sessione(backend, [t.epc for t in tag]).run()
    assert record.ok is True, f"mancanti: {record.missing}"


def test_recupera_un_tag_intermittente() -> None:
    difficile = _facile(2, 3, visible_every=4)
    tag = [_facile(1, 3), difficile, _facile(3, 3)]
    backend = FakeTagBackend(tag)

    record = _sessione(backend, [t.epc for t in tag]).run()
    assert record.ok is True, f"mancanti: {record.missing}"
    prova = next(item for item in record.evidence if item.epc == difficile.epc_hex)
    assert prova.detection_rate < 1.0, "un tag intermittente deve risultare tale"


def test_una_sola_passata_non_basterebbe() -> None:
    # Dimostra che le passate multiple non sono ornamentali: con una sola
    # configurazione il tag difficile resterebbe fuori.
    difficile = _facile(2, 3, min_read_power_cdbm=3000)
    tag = [_facile(1, 3), difficile, _facile(3, 3)]
    attesi = [t.epc for t in tag]

    debole = [ReadPass(antennas=(1, 2, 3), read_power_cdbm=2000, label="solo 20 dBm")]
    record = _sessione(
        FakeTagBackend(tag),
        attesi,
        passes=debole,
        policy=SealingPolicy(stable_passes=1, max_passes=6, pause_s=0.0),
    ).run()
    assert record.ok is False
    assert difficile.epc_hex in record.missing


# --------------------------------------------------------------------------
# Falsi positivi: non contare cio' che sta fuori dalla scatola
# --------------------------------------------------------------------------
def test_un_tag_fuori_distinta_non_viene_contato() -> None:
    """Il vassoio di un'altra spedizione appoggiato accanto al lettore."""
    dentro = [_facile(i, 2) for i in (1, 2)]
    estraneo = SimulatedTag(build_epc(LAB, 111, 1, 1), user_bytes=64)
    backend = FakeTagBackend(dentro + [estraneo])

    record = _sessione(backend, [t.epc for t in dentro]).run()

    assert record.ok is True
    assert record.counts == (2, 2), "l'estraneo non deve entrare nel conteggio"
    assert record.unexpected == (estraneo.epc_hex,)
    assert estraneo.epc_hex not in record.found


def test_consenso_fra_antenne_scarta_chi_ne_raggiunge_una_sola() -> None:
    # Discriminante geometrico: dentro il volume un tag e' visto da piu' antenne,
    # appoggiato fuori tipicamente da una sola.
    dentro = _facile(1, 2)
    borderline = _facile(2, 2, visible_from={1})
    backend = FakeTagBackend([dentro, borderline])
    policy = SealingPolicy(min_antennas=2, stable_passes=1, max_passes=12, pause_s=0.0)

    record = _sessione(backend, [dentro.epc, borderline.epc], policy=policy).run()

    assert record.ok is False
    assert borderline.epc_hex in record.missing
    assert borderline.epc_hex in record.rejected
    prova = next(item for item in record.evidence if item.epc == borderline.epc_hex)
    assert "antenna" in prova.reason


def test_soglia_di_rilevamento_scarta_i_tag_troppo_deboli() -> None:
    saltuario = _facile(2, 2, visible_every=6)
    backend = FakeTagBackend([_facile(1, 2), saltuario])
    policy = SealingPolicy(
        min_detection_rate=0.9, stable_passes=1, max_passes=12, pause_s=0.0
    )
    record = _sessione(backend, [_epc(1, 2), saltuario.epc], policy=policy).run()

    assert saltuario.epc_hex in record.rejected
    prova = next(item for item in record.evidence if item.epc == saltuario.epc_hex)
    assert "tasso di rilevamento" in prova.reason


# --------------------------------------------------------------------------
# Evidenza e tracciabilita'
# --------------------------------------------------------------------------
def test_il_record_conserva_la_configurazione_di_ogni_passata() -> None:
    tag = [_facile(1, 1)]
    backend = FakeTagBackend(tag)
    record = _sessione(backend, [tag[0].epc]).run()

    assert record.passes, "le passate eseguite vanno registrate"
    prima = record.passes[0]
    assert "antennas" in prima and "read_power_cdbm" in prima and "label" in prima
    assert prima["pass"] == 1


def test_il_record_e_serializzabile() -> None:
    import json

    tag = [_facile(1, 1)]
    record = _sessione(FakeTagBackend(tag), [tag[0].epc]).run(
        closure_proof=ClosureProof.OPERATOR
    )
    documento = record.to_dict()
    json.dumps(documento)
    assert documento["closure_proof"] == "operator"
    assert documento["trovati"] == 1 and documento["attesi"] == 1
    assert documento["evidence"][0]["tasso_rilevamento"] > 0


def test_le_leve_radio_vengono_davvero_applicate() -> None:
    tag = [_facile(1, 1)]
    backend = FakeTagBackend(tag)
    _sessione(backend, [tag[0].epc]).run()
    assert "configure" in backend.calls, "la potenza della passata va applicata"
    assert "configure_gen2" in backend.calls, "i parametri Gen2 vanno applicati"
    assert backend.gen2.get("session") is not None


def test_avanzamento_riportato_all_interfaccia() -> None:
    tag = [_facile(i, 2) for i in (1, 2)]
    avanzamenti: list[tuple[int, int, int]] = []
    _sessione(FakeTagBackend(tag), [t.epc for t in tag]).run(
        on_progress=lambda passata, trovati, attesi: avanzamenti.append(
            (passata, trovati, attesi)
        )
    )
    assert avanzamenti, "l'avanzamento deve essere riportato a ogni passata"
    assert avanzamenti[-1][1] == 2 and avanzamenti[-1][2] == 2


def test_interruzione_dall_operatore() -> None:
    import threading

    tag = [_facile(i, 3) for i in (1, 2, 3)]
    stop = threading.Event()
    stop.set()
    record = _sessione(FakeTagBackend(tag), [t.epc for t in tag]).run(stop_event=stop)
    assert record.ok is False
    assert "interrotto" in record.stop_reason
    assert record.passes_run == 0


def test_errore_del_lettore_non_produce_un_sigillo_valido() -> None:
    tag = [_facile(1, 1)]
    backend = FakeTagBackend(tag)

    def inventory_guasto(_request):
        return backend._ko("inventory", "lettore scollegato")

    backend.inventory = inventory_guasto
    record = _sessione(backend, [tag[0].epc]).run()
    assert record.ok is False
    assert "scollegato" in record.error


def test_limite_di_capienza_invalida_il_sigillo() -> None:
    # Se l'accumulatore avesse scartato EPC per limite di capienza, l'insieme
    # trovato sarebbe incompleto senza che nessuno se ne accorga.
    tag = [_facile(i, 3) for i in (1, 2, 3)]
    record = _sessione(
        FakeTagBackend(tag), [t.epc for t in tag], max_unique_epcs=1
    ).run()
    assert record.ok is False
    assert "capienza" in record.error


# --------------------------------------------------------------------------
# Strategia e configurazione
# --------------------------------------------------------------------------
def test_default_passes_varia_le_condizioni() -> None:
    passate = default_passes((1, 2, 3))
    potenze = {p.read_power_cdbm for p in passate}
    assert len(potenze) >= 3, "la potenza deve variare fra le passate"
    assert any(len(p.antennas) == 1 for p in passate), "servono passate per singola antenna"
    assert any(len(p.antennas) == 3 for p in passate), "servono passate con tutte le antenne"
    assert any(p.rf_mode == 0x71 for p in passate), "serve la passata ad alta sensibilita'"
    assert any(p.target_dynamic for p in passate), "serve il ribaltamento automatico A-B"


def test_default_passes_con_una_sola_antenna() -> None:
    passate = default_passes((1,))
    assert passate and all(p.antennas == (1,) for p in passate)


def test_validazione_degli_ingressi() -> None:
    _expect(ValueError, lambda: default_passes(()), "nessuna antenna")
    _expect(ValueError, lambda: ReadPass(antennas=()), "passata senza antenne")
    _expect(ValueError, lambda: SealingPolicy(min_antennas=0), "min_antennas nullo")
    _expect(ValueError, lambda: SealingPolicy(min_detection_rate=1.5), "tasso fuori scala")
    _expect(ValueError, lambda: SealingPolicy(stable_passes=0), "stabilita' nulla")
    _expect(
        ValueError,
        lambda: SealingSession(FakeTagBackend([]), [], passes=[]),
        "nessuna passata",
    )


def test_record_normalizza_la_prova_di_chiusura() -> None:
    # Un record ricaricato da JSON porta una stringa, non il membro enum.
    record = SealingRecord(closure_proof="sensor")
    assert record.closure_proof is ClosureProof.SENSOR
    assert record.to_dict()["closure_proof"] == "sensor"


# --------------------------------------------------------------------------
# Tag sul coperchio: identita' della scatola, non un contenitore
# --------------------------------------------------------------------------
def test_il_tag_del_coperchio_identifica_la_scatola_senza_essere_contato() -> None:
    dentro = [_facile(i, 2) for i in (1, 2)]
    coperchio = SimulatedTag(build_box_epc(LAB, 4242, random_suffix=bytes([0, 0, 0, 1])),
                             user_bytes=64)
    backend = FakeTagBackend(dentro + [coperchio])

    record = _sessione(backend, [t.epc for t in dentro]).run(
        closure_proof=ClosureProof.OPERATOR
    )

    assert record.ok is True, record.error
    assert record.counts == (2, 2), "il coperchio non e' un contenitore"
    assert record.box_id == 4242
    assert record.box_epc == coperchio.epc_hex
    # E soprattutto non deve finire fra gli intrusi: e' atteso nel campo.
    assert coperchio.epc_hex not in record.unexpected
    assert coperchio.epc_hex not in record.rejected


def test_due_coperchi_nel_campo_invalidano_il_sigillo() -> None:
    # O ci sono due scatole, o una e' di un'altra spedizione: in entrambi i casi
    # non si sa a quale scatola si riferisce il sigillo.
    dentro = [_facile(1, 1)]
    coperchi = [
        SimulatedTag(build_box_epc(LAB, 1, random_suffix=bytes([0, 0, 0, 1])), user_bytes=64),
        SimulatedTag(build_box_epc(LAB, 2, random_suffix=bytes([0, 0, 0, 2])), user_bytes=64),
    ]
    record = _sessione(
        FakeTagBackend(dentro + coperchi), [dentro[0].epc]
    ).run()

    assert record.ok is False
    assert "tag di scatola" in record.error


def test_senza_coperchio_il_sigillo_resta_valido() -> None:
    # Il tag sul coperchio e' un miglioramento, non un requisito: una scatola
    # senza tag si sigilla lo stesso, solo senza identita' registrata.
    dentro = [_facile(1, 1)]
    record = _sessione(FakeTagBackend(dentro), [dentro[0].epc]).run()
    assert record.ok is True
    assert record.box_epc == "" and record.box_id is None


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
