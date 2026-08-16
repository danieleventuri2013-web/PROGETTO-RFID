"""Test della generazione delle etichette ZPL.

L'etichetta e' l'unica parte leggibile senza lettore RFID: se il tag si rompe o
si stacca, e' quello che resta. I test coprono quindi due cose diverse — che lo
ZPL sia sintatticamente sano, e che i dati che servono a un umano ci siano tutti.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims.codec import SpecimenFlags
from lims.labels import (
    FileLabelPrinter,
    LabelContent,
    LabelTemplate,
    print_container_label,
    render_zpl,
)


def _expect(exc_type, callable_, message: str) -> None:
    try:
        callable_()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{message}: atteso {exc_type.__name__}, ottenuto {exc!r}") from None
    raise AssertionError(f"{message}: nessuna eccezione sollevata")


def _contenuto(**overrides) -> LabelContent:
    base = dict(
        display_name="DELLA VALLE GIANFRANCO",
        codice_fiscale="MRTMTT25D09F205Z",
        accession_id=987654,
        container_index=2,
        container_total=3,
        epc="0100A5000F12060203DEADBE",
        external_ref="CHIR-2026-0042",
        material_code=3,
        fixative_code=1,
        site_code=2,
        data_prelievo=date(2026, 8, 15),
        flags=SpecimenFlags.NONE,
        lab_name="Anatomia Patologica",
    )
    base.update(overrides)
    return LabelContent(**base)


# --------------------------------------------------------------------------
# Struttura ZPL
# --------------------------------------------------------------------------
def test_etichetta_ben_formata() -> None:
    zpl = render_zpl(_contenuto())
    assert zpl.startswith("^XA")
    assert zpl.rstrip().endswith("^XZ")
    # Ogni campo aperto con ^FD va chiuso con ^FS, altrimenti la stampante
    # inghiotte il resto dell'etichetta.
    assert zpl.count("^FD") == zpl.count("^FS")


def test_codifica_utf8_dichiarata() -> None:
    # Senza ^CI28 un cognome accentato esce come simboli casuali.
    assert "^CI28" in render_zpl(_contenuto())


def test_dimensioni_convertite_in_punti() -> None:
    template = LabelTemplate(width_mm=50, height_mm=30, dpi=203)
    assert template.width_dots == 400   # 50 mm a 203 dpi
    assert template.height_dots == 240
    zpl = render_zpl(_contenuto(), template)
    assert "^PW400" in zpl and "^LL240" in zpl


def test_dpi_diverso_cambia_le_coordinate() -> None:
    a_203 = render_zpl(_contenuto(), LabelTemplate(dpi=203))
    a_300 = render_zpl(_contenuto(), LabelTemplate(dpi=300))
    assert a_203 != a_300
    assert "^PW400" in a_203 and "^PW591" in a_300


def test_copie_multiple() -> None:
    assert "^PQ" not in render_zpl(_contenuto(), LabelTemplate(copies=1))
    assert "^PQ3" in render_zpl(_contenuto(), LabelTemplate(copies=3))


def test_template_valida_gli_ingressi() -> None:
    _expect(ValueError, lambda: LabelTemplate(width_mm=0), "larghezza nulla")
    _expect(ValueError, lambda: LabelTemplate(dpi=150), "dpi non supportato")
    _expect(ValueError, lambda: LabelTemplate(copies=0), "zero copie")


# --------------------------------------------------------------------------
# Contenuto: cosa serve a un umano senza lettore RFID
# --------------------------------------------------------------------------
def test_dati_essenziali_presenti() -> None:
    zpl = render_zpl(_contenuto())
    for atteso in (
        "DELLA VALLE GIANFRANCO",
        "MRTMTT25D09F205Z",
        "987654",
        "CHIR-2026-0042",
        "2/3",
        "pezzo operatorio",
        "formalina 10% tamponata",
        "mammella",
        "15/08/2026",
        "Anatomia Patologica",
    ):
        assert atteso in zpl, f"manca dall'etichetta: {atteso}"


def test_data_matrix_contiene_l_epc() -> None:
    # E' il ponte fra etichetta e tag: permette di ritrovare il record anche con
    # un lettore di codici a barre, senza RFID.
    zpl = render_zpl(_contenuto())
    assert "^BXN" in zpl
    assert "0100A5000F12060203DEADBE" in zpl


def test_riferimento_esterno_omesso_se_assente() -> None:
    zpl = render_zpl(_contenuto(external_ref=""))
    assert "rif." not in zpl


def test_avvertenze_stampate_in_negativo() -> None:
    zpl = render_zpl(
        _contenuto(flags=SpecimenFlags.INFECTIOUS | SpecimenFlags.URGENT)
    )
    assert "RISCHIO BIOLOGICO" in zpl
    assert "URGENTE" in zpl
    # ^FR inverte il campo sul riquadro nero: deve essere impossibile ignorarlo.
    assert "^FR" in zpl and "^GB" in zpl


def test_nessuna_avvertenza_nessun_riquadro() -> None:
    zpl = render_zpl(_contenuto(flags=SpecimenFlags.NONE))
    assert "^FR" not in zpl


def test_avvertenze_selezionate() -> None:
    contenuto = _contenuto(flags=SpecimenFlags.DECALCIFIED | SpecimenFlags.FROZEN)
    # Solo quelle che cambiano il comportamento di chi apre il contenitore.
    assert contenuto.warnings == ["CONGELATO"]


# --------------------------------------------------------------------------
# Sicurezza del generatore
# --------------------------------------------------------------------------
def test_caratteri_di_comando_neutralizzati() -> None:
    """Un cognome non deve poter alterare il flusso ZPL."""
    zpl = render_zpl(_contenuto(display_name="ROSSI^XZ~JA MARIO"))
    # Dopo il nome il flusso deve proseguire: un solo ^XZ, quello finale.
    assert zpl.count("^XZ") == 1
    assert "~JA" not in zpl


def test_codice_fiscale_lungo_non_rompe_il_flusso() -> None:
    zpl = render_zpl(_contenuto(codice_fiscale="^" * 40))
    assert zpl.count("^XA") == 1 and zpl.count("^XZ") == 1


# --------------------------------------------------------------------------
# Invio
# --------------------------------------------------------------------------
def test_stampa_su_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "etichette.zpl"
        stampante = FileLabelPrinter(percorso)
        zpl = print_container_label(stampante, _contenuto())

        assert percorso.exists()
        salvato = percorso.read_text(encoding="utf-8")
        assert salvato.strip() == zpl.strip()
        assert stampante.printed == [zpl]


def test_stampa_in_coda() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        percorso = Path(tmp) / "coda.zpl"
        stampante = FileLabelPrinter(percorso, append=True)
        print_container_label(stampante, _contenuto(container_index=1))
        print_container_label(stampante, _contenuto(container_index=2))
        assert percorso.read_text(encoding="utf-8").count("^XA") == 2
        assert len(stampante.printed) == 2


def test_contenuto_da_record_di_archivio() -> None:
    from lims.db import ContainerRecord
    from lims.labels import content_from_record
    from lims.model import ContainerState

    record = ContainerRecord(
        container_id=1,
        epc="0100A5000F12060203DEADBE",
        tid="E2801190",
        index=2,
        total=3,
        state=ContainerState.PROVISIONED,
        revision=0,
        accession_id=987654,
        codice_fiscale="MRTMTT25D09F205Z",
        cognome="DELLA VALLE",
        nome="GIANFRANCO",
        material_code=3,
        site_code=2,
        fixative_code=1,
        descrizione="Nodulo",
        data_prelievo=date(2026, 8, 15),
    )
    contenuto = content_from_record(record, external_ref="CHIR-1", lab_name="AP")
    assert contenuto.display_name == "DELLA VALLE GIANFRANCO"
    assert contenuto.label == "2/3"
    assert contenuto.epc == "0100A5000F12060203DEADBE"
    assert "CHIR-1" in render_zpl(contenuto)


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
