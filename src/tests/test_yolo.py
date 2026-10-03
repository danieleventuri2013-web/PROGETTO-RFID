"""YOLO locale: contratto dei rilevamenti, avvio offline e API del banco, senza pesi."""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app.vision_preview import crea_server
from app.yolo_engine import YoloLocale, riassumi_rilevamenti

COCO = {i: f"classe{i}" for i in range(80)} | {41: "cup", 45: "bowl", 74: "clock"}


def _rifiuta(fn, testo, eccezione=ValueError):
    try:
        fn()
    except eccezione as exc:
        assert testo in str(exc), exc
    else:
        raise AssertionError(f"atteso errore: {testo}")


def test_riquadri_normalizzati_ordinati_e_borsa_esclusa():
    boxes = [[600, 300, 700, 400], [100, 100, 200, 200], [400, 90, 500, 190], [0, 0, 1000, 500]]
    objects, excluded = riassumi_rilevamenti(boxes, [.9, .8, .7, .95], [41, 45, 41, 74], COCO, (1000, 500))
    assert excluded == {"borsa": 1, "annidati": 0}, "un riquadro grande come la borsa non è un campione"
    assert [o["id"] for o in objects] == [1, 2, 3]
    # Prima riga da sinistra, poi la riga sotto: stesso ordine di lettura di SAM.
    assert [o["centro"] for o in objects] == [[.15, .3], [.45, .28], [.65, .7]]
    assert [o["classe"] for o in objects] == ["bowl", "cup", "cup"]
    assert objects[0]["contorni"] == [[[.1, .2], [.2, .2], [.2, .4], [.1, .4]]]
    assert objects[0]["confidenza"] == .8


def test_maschere_semplificate_e_centri_duplicati_rifiutati():
    angles = np.linspace(0, 2*np.pi, 900, endpoint=False)
    circle = np.stack([500+80*np.cos(angles), 250+80*np.sin(angles)], axis=1)
    objects, _ = riassumi_rilevamenti([[420, 170, 580, 330]], [.6], [41], COCO, (1000, 500), maschere=[circle])
    poly = objects[0]["contorni"][0]
    assert 8 <= len(poly) <= 200, len(poly)
    assert all(0 <= x <= 1 and 0 <= y <= 1 for x, y in poly)
    same, excluded = riassumi_rilevamenti([[1, 1, 9, 9]]*2, [.5, .4], [41, 41], COCO, (100, 100))
    assert len(same) == 1 and excluded["annidati"] == 1
    _rifiuta(lambda: riassumi_rilevamenti([[1, 1, 9, 9]]*2, [.5, .4], [41, 41], COCO, (100, 100), contenimento=None),
             "centri identici")
    assert riassumi_rilevamenti([[5, 5, 5, 9], [float("nan"), 0, 1, 1]], [.5, .5], [41, 41], COCO, (100, 100))[0] == []


def test_riquadro_annidato_stesso_contenitore():
    # esempio4: coperchio da solo dentro coperchio+corpo dello stesso barattolo inclinato.
    boxes = [[536, 305, 663, 410], [563, 312, 670, 402], [383, 310, 467, 377]]
    objects, excluded = riassumi_rilevamenti(boxes, [.78, .84, .95], [0, 0, 0], {0: "contenitore"}, (808, 508))
    assert len(objects) == 2 and excluded["annidati"] == 1
    assert sorted(o["confidenza"] for o in objects) == [.84, .95], "resta il riquadro più sicuro"
    # Due barattoli accostati si toccano ma non sono annidati.
    side, _ = riassumi_rilevamenti([[0, 0, 50, 50], [45, 0, 95, 50]], [.9, .9], [0, 0], {0: "c"}, (400, 400))
    assert len(side) == 2
    raw, _ = riassumi_rilevamenti(boxes, [.78, .84, .95], [0, 0, 0], {0: "c"}, (808, 508), contenimento=None)
    assert len(raw) == 3, "la regola si può disattivare per il confronto"


def test_pesi_predefiniti_preferiscono_scene_sintetiche():
    from app.yolo_engine import PESI_PREDEFINITI, pesi_predefiniti

    folder = Path(__file__).resolve().parents[2]/"tmp"/"yolo_predefiniti"
    if folder.exists():
        for f in folder.iterdir():
            f.unlink()
    folder.mkdir(parents=True, exist_ok=True)
    assert pesi_predefiniti(folder) == PESI_PREDEFINITI, "senza pesi addestrati resta il modello base"
    (folder/"contenitori-yolo11n.pt").write_bytes(b"x")
    assert pesi_predefiniti(folder) == "contenitori-yolo11n.pt"
    (folder/"contenitori-sintetiche-yolo11n.pt").write_bytes(b"x")
    assert pesi_predefiniti(folder) == "contenitori-sintetiche-yolo11n.pt"


def test_pesi_solo_locali_e_parametri_validati():
    _rifiuta(lambda: YoloLocale("assente.pt", model_dir=Path("non-esiste")), "pesi YOLO assenti", RuntimeError)
    _rifiuta(lambda: YoloLocale("x.pt", dispositivo="cuda"), "dispositivo YOLO non valido")
    _rifiuta(lambda: YoloLocale("x.pt", confidenza=0), "confidenza")


def _tensore(values):
    array = np.asarray(values, dtype=float)
    return types.SimpleNamespace(cpu=lambda: types.SimpleNamespace(numpy=lambda: array))


class _Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = _tensore(xyxy), _tensore(conf), _tensore(cls)


class _FakeYOLO:
    calls = []
    names = COCO
    boxes = ([[10, 10, 30, 30], [50, 10, 70, 30]], [.4, .3], [74, 45])

    def __init__(self, path, task=None):
        self.path = path

    def predict(self, source, **kwargs):
        _FakeYOLO.calls.append((np.asarray(source).copy(), kwargs))
        return [types.SimpleNamespace(boxes=_Boxes(*self.boxes), masks=None)]


def _motore(tmp_name, **kwargs):
    folder = Path(__file__).resolve().parents[2]/"tmp"/tmp_name
    folder.mkdir(parents=True, exist_ok=True)
    (folder/"yolo11n.pt").write_bytes(b"pesi finti")
    fake = types.ModuleType("ultralytics")
    fake.YOLO = _FakeYOLO
    _FakeYOLO.calls = []
    with patch.dict(sys.modules, {"ultralytics": fake}):
        return YoloLocale("yolo11n.pt", model_dir=folder, **kwargs)


def test_motore_coco_marcato_incerto_classi_filtrate_e_bgr():
    _rifiuta(lambda: _motore("yolo_classi", classi=["barattolo"]), "classi assenti")
    engine = _motore("yolo_motore", classi=["cup", "bowl", "clock"], confidenza=.2)
    assert len(_FakeYOLO.calls) == 1, "il riscaldamento avviene all'avvio, fuori dal tempo dello scatto"
    assert sorted(_FakeYOLO.calls[0][1]["classes"]) == [41, 45, 74]
    image = Image.new("RGB", (100, 40), (255, 0, 0))
    result = engine.analizza_automatico(image)
    source, kwargs = _FakeYOLO.calls[-1]
    assert tuple(source[0, 0]) == (0, 0, 255), "Ultralytics riceve BGR come da convenzione OpenCV"
    assert kwargs["conf"] == .2 and kwargs["agnostic_nms"] and kwargs["device"] == "cpu"
    assert result["conteggio"] == 2 and result["dimensioni"] == [100, 40]
    assert result["incerto"], "il modello base COCO non è addestrato sui contenitori"
    assert any("COCO" in a for a in result["avvisi"])
    assert result["classi_rilevate"] == {"clock": 1, "bowl": 1} and result["tipo_overlay"] == "riquadri"
    assert "yolo11n" in result["modello"] and "CPU" in result["modello"]
    assert any("confidenza bassa sui campioni 1, 2" in w for w in result["avvisi"])


def test_pesi_addestrati_sicuri_non_incerti():
    with patch.object(_FakeYOLO, "names", {0: "contenitore"}),          patch.object(_FakeYOLO, "boxes", ([[10, 10, 30, 30], [50, 10, 70, 30]], [.9, .8], [0, 0])):
        engine = _motore("yolo_addestrato")
        result = engine.analizza_automatico(Image.new("RGB", (100, 40)))
    assert result["conteggio"] == 2 and not result["incerto"] and result["avvisi"] == []
    assert result["classi_rilevate"] == {"contenitore": 2}


def test_api_yolo_protetta_serializzata_e_ponte_webui():
    from webui import vision_models

    entered, release = threading.Event(), threading.Event()

    class Yolo:
        delayed = broken = False

        def analizza_automatico(self, image):
            if self.delayed:
                entered.set()
                assert release.wait(5)
            if self.broken:
                raise RuntimeError("inferenza fallita")
            objects, _ = riassumi_rilevamenti([[30, 20, 90, 80], [150, 40, 210, 100]], [.9, .8], [0, 0], {0: "contenitore"}, image.size)
            return {"automatico": True, "dimensioni": list(image.size), "conteggio": len(objects), "oggetti": objects,
                    "incerto": False, "avvisi": [], "modello": "finto", "tipo_overlay": "riquadri", "tempo_secondi": .05, "borsa": {}}

    engine = Yolo()
    server, token = crea_server(0, yolo=engine)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    buffer = io.BytesIO()
    Image.new("RGB", (300, 200), (40, 50, 60)).save(buffer, format="JPEG")
    payload = {"immagine_base64": base64.b64encode(buffer.getvalue()).decode()}

    def post(path="/api/yolo/automatico", data=payload, secret=token):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}", data=json.dumps(data).encode(),
                                     headers={"X-RFID-Token": secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        assert post(secret="errato")[0] == 401
        code, result = post()
        assert code == 200 and result["conteggio"] == 2, (code, result)
        assert post("/api/sam2/automatico")[0] == 503, "senza SAM caricato non si finge un conteggio"
        code, prepared = post("/api/sam2/prepara", {**payload, "calibrazione": {"punti": [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]}})
        assert code == 200, "la rettifica prospettica serve anche al servizio solo YOLO"
        engine.delayed = True
        first = []
        pending = threading.Thread(target=lambda: first.append(post()))
        pending.start()
        try:
            assert entered.wait(3)
            assert post()[0] == 409, "una sola analisi alla volta, anche fra motori diversi"
        finally:
            release.set()
            pending.join(5)
        assert first[0][0] == 200
        engine.delayed, engine.broken = False, True
        with patch("traceback.print_exc"):
            code, error = post()
        assert code == 503 and "YOLO" in error["errore"]
        engine.broken = False
        with patch.object(vision_models, "servizio", return_value={"base": f"http://127.0.0.1:{server.server_port}", "token": token}):
            image = Image.new("RGB", (300, 200), "white")
            detected = vision_models.analizza(image, "yolo")
            assert detected["conteggio"] == 2 and detected["oggetti"][0]["contorni"][0][0] == [.1, .1]
        assert "yolo" in vision_models.MODELLI and "yolo" in vision_models.MODELLI_AI
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    empty, token2 = crea_server(0)
    thread = threading.Thread(target=empty.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{empty.server_port}/api/yolo/automatico", data=json.dumps(payload).encode(),
                                     headers={"X-RFID-Token": token2, "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503 and "YOLO non avviato" in json.load(exc)["errore"]
        else:
            raise AssertionError("YOLO assente deve rispondere 503")
    finally:
        empty.shutdown()
        empty.server_close()
        thread.join(timeout=2)


def _run_all():
    import traceback

    tests = [f for n, f in sorted(globals().items()) if n.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
            print("PASS", fn.__name__)
        except Exception:
            failed += 1
            traceback.print_exc()
    print(f"{len(tests)-failed}/{len(tests)} test superati")
    return int(bool(failed))


if __name__ == "__main__":
    sys.exit(_run_all())
