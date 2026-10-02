"""Prova QR senza webcam: parti, firma, confronto e decoder ottico facoltativo."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.qr_webcam import RaccoltaQR, confronta, descrivi, leggi_qr
from lims.manifest import Manifest, ManifestEntry
from lims.tabella import TabellaError, codifica_tabella, righe_da_manifest

CHIAVE = b"K" * 32


def _esempio():
    distinta = Manifest(shipment_id=5, entries=[ManifestEntry(
        epc=f"01000100000001010{i}ABCDEF", accession_id=1, container_index=i,
        container_total=3, codice_fiscale="", display_name="DELLA VALLE ANNA",
        descrizione="Campione di prova", data_prelievo="2026-10-01", material_code=1,
    ) for i in range(1, 4)])
    righe = [replace(r, cognome="DELLA VALLE", nome="ANNA", sesso="F",
                     data_nascita="19800102", ora_prelievo="0930") for r in righe_da_manifest(distinta)]
    return distinta, righe


def _rifiuta(fn):
    try:
        fn()
    except TabellaError:
        return
    raise AssertionError("Scansione errata accettata")


def test_confronto_completo_cognome_composto_e_campi_solo_qr():
    distinta, righe = _esempio()
    parti = codifica_tabella(righe, identificativo="ABCDEF01", chiave=CHIAVE)
    raccolta = RaccoltaQR((CHIAVE,))
    assert raccolta.parti == {}, "nessuna acquisizione automatica"
    letta = raccolta.acquisisci(parti)
    assert letta.firma_verificata
    assert confronta(letta, distinta)["ok"]
    assert "02/01/1980" in descrivi(letta, distinta)
    assert "non sono disponibili" in descrivi(letta, distinta)


def test_mancanti_estranei_e_differenze_non_sono_conformi():
    distinta, righe = _esempio()
    righe[0] = replace(righe[0], descrizione="DIVERSO")
    righe[1] = replace(righe[1], epc="F" * 24)
    letta = RaccoltaQR().acquisisci(codifica_tabella(righe))
    esito = confronta(letta, distinta)
    assert not esito["ok"]
    assert esito["mancanti"] == [distinta.entries[1].epc]
    assert esito["estranei"] == ["F" * 24]
    assert len(esito["differenze"]) == 1 and "descrizione" in esito["differenze"][0]


def test_epc_duplicato_non_diventa_confronto_conforme():
    distinta, righe = _esempio()
    letta = RaccoltaQR().acquisisci(codifica_tabella(righe + [righe[0]]))
    assert not confronta(letta, distinta)["ok"]


def test_parti_multiple_fuori_ordine_e_duplicati():
    distinta, righe = _esempio()
    parti = codifica_tabella(righe, identificativo="1234ABCD", chiave=CHIAVE, caratteri_per_qr=90)
    assert len(parti) > 1
    raccolta = RaccoltaQR((CHIAVE,))
    assert raccolta.acquisisci([parti[-1]]) is None
    assert raccolta.acquisisci([parti[-1]]) is None
    assert len(raccolta.parti) == 1
    letta = raccolta.acquisisci(list(reversed(parti[:-1])))
    assert confronta(letta, distinta)["ok"]


def test_distinte_mischiate_rifiutate_senza_perdere_le_parti():
    _, righe = _esempio()
    parti = codifica_tabella(righe, identificativo="11111111", caratteri_per_qr=90)
    altre = codifica_tabella(righe, identificativo="22222222", caratteri_per_qr=90)
    raccolta = RaccoltaQR()
    raccolta.acquisisci([parti[0]])
    _rifiuta(lambda: raccolta.acquisisci([altre[1]]))
    assert raccolta.parti == {1: parti[0]}
    assert raccolta.acquisisci(parti[1:]) is not None


def test_firma_errata_blocca_i_dati_e_rotazione_chiave_supportata():
    _, righe = _esempio()
    parti = codifica_tabella(righe, chiave=CHIAVE)
    raccolta = RaccoltaQR((b"X" * 32,))
    _rifiuta(lambda: raccolta.acquisisci(parti))
    assert not raccolta.parti
    assert RaccoltaQR((b"X" * 32, CHIAVE)).acquisisci(parti).firma_verificata


def test_qr_estraneo_e_assenza_codice_rifiutati():
    raccolta = RaccoltaQR()
    _rifiuta(lambda: raccolta.acquisisci([]))
    _rifiuta(lambda: raccolta.acquisisci(["https://example.test"]))
    assert not raccolta.parti


def test_senza_chiave_non_dichiara_firma_verificata():
    _, righe = _esempio()
    letta = RaccoltaQR().acquisisci(codifica_tabella(righe, chiave=CHIAVE))
    assert letta.firma_verificata is None
    assert "NON verificata" in descrivi(letta, None)


def verifica_ottica():
    """Il simbolo generato dal progetto deve essere letto da ZXing, anche ruotato."""
    import numpy as np

    from lims.qr import codifica

    distinta, righe = _esempio()
    parti = codifica_tabella(righe, identificativo="ABCDEF01", chiave=CHIAVE, caratteri_per_qr=180)
    raccolta = RaccoltaQR((CHIAVE,))
    for parte in parti:
        moduli = np.array(codifica(parte).moduli, dtype=np.uint8)
        immagine = np.kron(np.pad(255 * (1 - moduli), 4, constant_values=255), np.ones((7, 7), dtype=np.uint8))
        rilevati = leggi_qr(np.ascontiguousarray(np.rot90(immagine)))
        assert [testo for testo, _ in rilevati] == [parte]
        letta = raccolta.acquisisci([testo for testo, _ in rilevati])
    assert confronta(letta, distinta)["ok"]
    assert leggi_qr(np.full((360, 640), 255, dtype=np.uint8)) == []
    print("PASS decoder ottico: QR reali generati, ruotati, multiparte e immagine vuota")


def verifica_video_indipendente():
    """Un decoder fermo non deve fermare i fotogrammi o nascondere il video nero."""
    import threading
    import time
    from unittest.mock import patch

    import numpy as np

    from app.qr_webcam import Camera

    entrato, sblocca = threading.Event(), threading.Event()

    class Sorgente:
        valore = 100
        rilasciata = False

        def isOpened(self):
            return True

        def read(self):
            time.sleep(0.01)
            return True, np.full((48, 64, 3), self.valore, dtype=np.uint8)

        def release(self):
            self.rilasciata = True

    def lento(_frame):
        entrato.set()
        assert sblocca.wait(3)
        return []

    sorgente = Sorgente()
    with patch("cv2.VideoCapture", return_value=sorgente), patch("app.qr_webcam.leggi_qr", side_effect=lento):
        camera = Camera(0)
        try:
            assert entrato.wait(2)
            prima = camera.diagnostica()[0]
            limite = time.monotonic() + 1
            while camera.diagnostica()[0] < prima + 5 and time.monotonic() < limite:
                time.sleep(.01)
            assert camera.diagnostica()[0] >= prima + 5, "il decoder ha fermato il video"
            sorgente.valore = 0
            limite = time.monotonic() + 1
            while not camera.diagnostica()[2] and time.monotonic() < limite:
                time.sleep(.01)
            assert camera.diagnostica()[2], "fotogrammi neri non segnalati"
        finally:
            sblocca.set()
            camera.attendi_chiusura()
        assert sorgente.rilasciata
        assert not camera.thread.is_alive() and not camera.decoder.is_alive()
    print("PASS anteprima indipendente dal decoder, diagnosi video nero e rilascio webcam")


def verifica_fotogramma_webui():
    """JPEG come dal browser: contenuto, coordinate, limiti e rilascio decoder."""
    import base64
    import io

    import numpy as np
    from PIL import Image

    from lims.qr import codifica
    from webui.qr_camera import decodifica_fotogramma
    from webui.workflow import WorkflowError

    def fotogramma(image, formato="JPEG"):
        buffer = io.BytesIO()
        image.save(buffer, format=formato, quality=90)
        return {"immagine_base64": base64.b64encode(buffer.getvalue()).decode("ascii")}

    _, righe = _esempio()
    testo = codifica_tabella(righe, chiave=CHIAVE)[0]
    moduli = np.array(codifica(testo).moduli, dtype=np.uint8)
    pixels = np.kron(np.pad(255 * (1 - moduli), 4, constant_values=255), np.ones((6, 6), dtype=np.uint8))
    immagine = Image.fromarray(pixels).rotate(90)
    result = decodifica_fotogramma(fotogramma(immagine))
    assert result["larghezza"] == immagine.width and result["altezza"] == immagine.height
    assert len(result["codici"]) == 1 and result["codici"][0]["testo"] == testo
    assert len(result["codici"][0]["vertici"]) == 4
    for guasto in ({}, {"immagine_base64": "?"}, {"immagine_base64": "A" * 2_800_001},
                   fotogramma(immagine, "PNG"), fotogramma(Image.new("RGB", (1921, 1080)))):
        try:
            decodifica_fotogramma(guasto)
        except WorkflowError:
            pass
        else:
            raise AssertionError("fotogramma errato accettato")
    assert decodifica_fotogramma(fotogramma(Image.new("RGB", (640, 480))))["codici"] == []
    print("PASS decoder WebUI: JPEG, QR ruotato, coordinate, limiti e recupero dopo errore")


def _run_all():
    import traceback

    tests = [fn for name, fn in sorted(globals().items()) if name.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} test superati")
    return int(bool(failed))


if __name__ == "__main__":
    result = _run_all()
    if "--ottico" in sys.argv:
        verifica_ottica()
        verifica_video_indipendente()
        verifica_fotogramma_webui()
    sys.exit(result)
