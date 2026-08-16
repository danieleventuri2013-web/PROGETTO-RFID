"""Test della campagna di misura: taratura su due obiettivi opposti.

Il test decisivo e' `test_non_sceglie_la_configurazione_che_legge_di_piu`:
la tentazione naturale e' alzare la potenza finche' "si vede tutto", ed e'
esattamente il modo di costruire un sistema che conta contenitori che non ci
sono. La campagna deve scegliere il massimo *a fughe nulle*.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag
from lims.campaign import CampaignConfig, ReadCampaign, grid_passes
from lims.codec import build_epc
from lims.sealing import ReadPass

LAB = 0x00A5


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _tag(accession: int, indice: int, totale: int, **kwargs) -> SimulatedTag:
    return SimulatedTag(
        build_epc(LAB, accession, indice, totale, random_suffix=bytes([0, accession % 256, indice])),
        user_bytes=64,
        **kwargs,
    )


def _config(**kwargs) -> CampaignConfig:
    base = dict(cycles_per_configuration=4, pause_s=0.0, timeout_ms=50)
    base.update(kwargs)
    return CampaignConfig(**base)


# --------------------------------------------------------------------------
# Griglia
# --------------------------------------------------------------------------
def test_griglia_copre_le_combinazioni() -> None:
    griglia = grid_passes((1, 2), powers_cdbm=(2000, 3000), sessions=(0, 2), rf_modes=(None,))
    # 3 gruppi di antenne (entrambe + singole) x 2 potenze x 2 sessioni
    assert len(griglia) == 3 * 2 * 2
    assert {p.read_power_cdbm for p in griglia} == {2000, 3000}
    assert {p.session for p in griglia} == {0, 2}


def test_griglia_parte_dalle_potenze_basse() -> None:
    # Serve trovare la potenza piu' bassa che legge tutto, non la piu' alta che
    # legge qualcosa in piu': e' la potenza a definire il volume di lettura.
    griglia = grid_passes((1,))
    assert min(p.read_power_cdbm for p in griglia) < 2000


def test_griglia_rifiuta_zero_antenne() -> None:
    _expect(ValueError, lambda: grid_passes(()), "nessuna antenna")


# --------------------------------------------------------------------------
# Misura
# --------------------------------------------------------------------------
def test_misura_dentro_e_fuori() -> None:
    dentro = [_tag(1, i, 2) for i in (1, 2)]
    fuori = [_tag(99, 1, 1)]
    backend = FakeTagBackend(dentro + fuori)

    campagna = ReadCampaign(
        backend,
        inside_epcs=[t.epc_hex for t in dentro],
        outside_epcs=[t.epc_hex for t in fuori],
        config=_config(),
    )
    report = campagna.run([ReadPass(antennas=(1, 2), read_power_cdbm=3000, label="piena")])

    risultato = report.results[0]
    assert risultato.inside_found == 2
    assert risultato.outside_leaked == 1, "il tag esterno e' letto: e' una fuga"
    assert risultato.clean is False
    assert risultato.usable is False


def test_non_sceglie_la_configurazione_che_legge_di_piu() -> None:
    """Il cuore della campagna: massimo dentro, ma solo a fughe nulle."""
    # Un tag interno difficile che serve almeno 25 dBm, e un tag esterno che a
    # 30 dBm comincia a farsi sentire.
    dentro = [_tag(1, 1, 2), _tag(1, 2, 2, min_read_power_cdbm=2500)]
    fuori = [_tag(99, 1, 1, min_read_power_cdbm=3000)]
    backend = FakeTagBackend(dentro + fuori)

    campagna = ReadCampaign(
        backend,
        inside_epcs=[t.epc_hex for t in dentro],
        outside_epcs=[t.epc_hex for t in fuori],
        config=_config(),
    )
    report = campagna.run(
        grid_passes((1, 2), powers_cdbm=(2000, 2500, 3000), sessions=(0,), rf_modes=(None,))
    )

    migliore = report.best()
    assert migliore is not None, "una configurazione valida esiste: 25 dBm"
    assert migliore.usable is True
    assert migliore.configuration["read_power_cdbm"] == 2500, (
        "a 20 dBm manca un contenitore, a 30 dBm entra il tag esterno"
    )
    # A piena potenza si legge tutto dentro, ma la configurazione non e' usabile.
    piene = [r for r in report.results if r.configuration["read_power_cdbm"] == 3000]
    assert all(r.complete for r in piene)
    assert all(not r.usable for r in piene), "leggere tutto non basta se entra anche il fuori"


def test_a_parita_di_potenza_nessuna_fuga_resta_il_criterio() -> None:
    dentro = [_tag(1, 1, 1)]
    fuori = [_tag(99, 1, 1)]
    backend = FakeTagBackend(dentro + fuori)
    campagna = ReadCampaign(
        backend,
        inside_epcs=[dentro[0].epc_hex],
        outside_epcs=[fuori[0].epc_hex],
        config=_config(),
    )
    report = campagna.run([ReadPass(antennas=(1,), read_power_cdbm=3000, label="unica")])
    assert report.best() is None, "senza configurazioni pulite non si consiglia nulla"
    assert report.usable == []


def test_senza_tag_di_controllo_esterni_non_ci_sono_fughe() -> None:
    dentro = [_tag(1, i, 2) for i in (1, 2)]
    campagna = ReadCampaign(
        FakeTagBackend(dentro), inside_epcs=[t.epc_hex for t in dentro], config=_config()
    )
    report = campagna.run([ReadPass(antennas=(1,), read_power_cdbm=3000, label="unica")])
    risultato = report.results[0]
    assert risultato.outside_expected == 0
    assert risultato.outside_leak_rate == 0.0
    assert risultato.usable is True


def test_tasso_di_rilevamento_per_tag() -> None:
    saltuario = _tag(1, 2, 2, visible_every=2)
    dentro = [_tag(1, 1, 2), saltuario]
    campagna = ReadCampaign(
        FakeTagBackend(dentro),
        inside_epcs=[t.epc_hex for t in dentro],
        config=_config(cycles_per_configuration=6),
    )
    report = campagna.run([ReadPass(antennas=(1,), read_power_cdbm=3000, label="unica")])
    tassi = report.results[0].per_tag_detection
    assert tassi[dentro[0].epc_hex] == 1.0
    assert 0.0 < tassi[saltuario.epc_hex] < 1.0


def test_tag_piu_difficili_ordinati() -> None:
    difficile = _tag(1, 2, 2, visible_every=3)
    dentro = [_tag(1, 1, 2), difficile]
    campagna = ReadCampaign(
        FakeTagBackend(dentro),
        inside_epcs=[t.epc_hex for t in dentro],
        config=_config(cycles_per_configuration=6),
    )
    report = campagna.run(
        grid_passes((1,), powers_cdbm=(2000, 3000), sessions=(0,), rf_modes=(None,))
    )
    difficili = report.hardest_tags()
    assert difficili and difficili[0][0] == difficile.epc_hex


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def test_report_serializzabile_e_riporta_la_geometria() -> None:
    import json

    dentro = [_tag(1, 1, 1)]
    campagna = ReadCampaign(
        FakeTagBackend(dentro),
        inside_epcs=[dentro[0].epc_hex],
        config=_config(container_mm=(440, 220, 220), notes="prototipo, 2 antenne a pavimento"),
    )
    report = campagna.run([ReadPass(antennas=(1,), read_power_cdbm=2000, label="unica")])
    documento = report.to_dict()
    json.dumps(documento)
    # La geometria non entra nel calcolo ma senza di essa il risultato non e'
    # confrontabile con un'altra installazione.
    assert documento["container_mm"] == [440, 220, 220]
    assert "prototipo" in documento["notes"]
    assert documento["consigliata"] is not None


def test_errore_del_lettore_registrato_nel_report() -> None:
    dentro = [_tag(1, 1, 1)]
    backend = FakeTagBackend(dentro)

    def inventory_guasto(_request):
        return backend._ko("inventory", "lettore scollegato")

    backend.inventory = inventory_guasto
    campagna = ReadCampaign(backend, inside_epcs=[dentro[0].epc_hex], config=_config())
    report = campagna.run([ReadPass(antennas=(1,), read_power_cdbm=2000, label="unica")])
    assert "scollegato" in report.error
    assert report.best() is None


def test_avanzamento_riportato() -> None:
    dentro = [_tag(1, 1, 1)]
    passi: list[int] = []
    campagna = ReadCampaign(FakeTagBackend(dentro), inside_epcs=[dentro[0].epc_hex], config=_config())
    griglia = grid_passes((1,), powers_cdbm=(2000, 3000), sessions=(0,), rf_modes=(None,))
    campagna.run(griglia, on_progress=lambda i, n, _r: passi.append(i))
    assert passi == list(range(1, len(griglia) + 1))


def test_ingressi_incoerenti_rifiutati() -> None:
    dentro = _tag(1, 1, 1)
    _expect(
        ValueError,
        lambda: ReadCampaign(FakeTagBackend([]), inside_epcs=[]),
        "nessun tag dichiarato dentro",
    )
    _expect(
        ValueError,
        lambda: ReadCampaign(
            FakeTagBackend([]),
            inside_epcs=[dentro.epc_hex],
            outside_epcs=[dentro.epc_hex],
        ),
        "lo stesso EPC dentro e fuori",
    )


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
