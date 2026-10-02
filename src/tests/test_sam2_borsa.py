"""Contorni incompleti e rifiuto di regioni estranee al profilo borsa."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sam2_borsa import seleziona_borsa


def test_perimetro_intero():
    source = np.zeros((100, 100), dtype=np.uint8)
    source[3:97, 3:97] = 1
    # Un buco dovuto a un riflesso non deve creare un secondo bordo esterno.
    source[40:60, 40:60] = 0
    source[48:53, 4:9] = 0  # Riflesso proprio su uno dei riferimenti laterali.
    mask, result = seleziona_borsa([source], [.95])
    assert result["rilevata"] and result["perimetro_completo"]
    assert not result["lati_al_limite"] and len(result["contorno"]) == 4
    assert mask[50, 50] == 1


def test_lato_tagliato():
    source = np.zeros((100, 100), dtype=np.uint8)
    source[3:97, :97] = 1
    _, result = seleziona_borsa([source], [.95])
    assert result["lati_al_limite"] == ["sinistra"]
    assert not result["perimetro_completo"] and result["avvisi"]


def test_rifiuta_sfondo_e_campione():
    source = np.ones((100, 100), dtype=np.uint8)
    small = np.zeros_like(source)
    small[30:70, 30:70] = 1
    mask, result = seleziona_borsa([source, small], [.99, .99])
    assert mask is None and not result["rilevata"]


def test_rifiuta_regione_senza_riferimenti():
    source = np.ones((100, 100), dtype=np.uint8)
    source[:15] = 0  # Esclude il riferimento superiore, pur avendo area ampia.
    mask, result = seleziona_borsa([source], [.99])
    assert mask is None and not result["rilevata"]


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    print(f"{len(tests)}/{len(tests)} test superati")
