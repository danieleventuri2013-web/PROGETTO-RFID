"""Contratto SAM 2: prompt indipendenti, contorni duplicati, HTTP protetto."""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sam2_engine import riassumi_maschere, valida_oggetti
from app.vision_preview import crea_server


def test_prompt_e_maschere():
    import numpy as np

    objects = [{"punti": [[.25, .25]], "etichette": [1]}, {"punti": [[.26, .26]], "etichette": [1]}]
    assert len(valida_oggetti(objects)) == 2
    for bad in ([], [{"punti": [[float("nan"), .5]], "etichette": [1]}],
                [{"punti": [[.2, .2]], "etichette": [0]}], [{"punti": [[1.1, .2]], "etichette": [1]}]):
        try:
            valida_oggetti(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    mask = np.zeros((100, 100), dtype=np.uint8)
    mask[20:40, 20:40] = 1
    result = riassumi_maschere([mask, mask], [.98, .99], objects, 100, 100)
    assert result["indicati"] == 2 and result["contorni_distinti"] == 1
    assert "stessa regione" in result["oggetti"][1]["avvisi"][0]
    assert len(result["oggetti"][0]["contorni"][0]) == 4
    result = riassumi_maschere([np.ones((100, 100), dtype=np.uint8)], [.98], objects[:1], 100, 100)
    assert result["contorni_distinti"] == 0 and result["oggetti"][0]["avvisi"]


def test_http_locale():
    from PIL import Image

    calls = []

    class FakeSam:
        def analizza(self, image, objects):
            calls.append((image.size, objects))
            return {"indicati": len(objects), "contorni_distinti": 0}

    server, token = crea_server(0, sam2=FakeSam())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    photo = io.BytesIO()
    Image.new("RGB", (640, 480), "white").save(photo, "JPEG")
    payload = {"immagine_base64": base64.b64encode(photo.getvalue()).decode(),
               "oggetti": [{"punti": [[.25, .3]], "etichette": [1]}], "attesi": 999}

    def post(data=payload, secret=token, origin="http://127.0.0.1:8770"):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/sam2", json.dumps(data).encode(),
                                     {"X-RFID-Token": secret, "Content-Type": "application/json", "Origin": origin})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        code, body = post()
        assert code == 200 and body["indicati"] == 1 and body["contorni_distinti"] == 0
        assert calls[0] == ((640, 480), payload["oggetti"])
        assert post(secret="errato")[0] == 401
        assert post(origin="https://altro.example")[0] == 403
        assert post(data={**payload, "oggetti": []})[0] == 400
        assert post(data={**payload, "immagine_base64": "non foto"})[0] == 400
        assert post(data=[])[0] == 400
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/?t={token}") as response:
            assert b"sam2.js" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    test_prompt_e_maschere()
    test_http_locale()
    print("PASS SAM 2: prompt, duplicati, maschera estesa, HTTP/token/origine e foto invalide")
    if "--modello" in sys.argv:
        from PIL import Image, ImageDraw

        from app.sam2_engine import Sam2Locale

        image = Image.new("RGB", (1920, 1080), "white")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((100, 100, 1820, 980), outline="red", width=15)
        objects = []
        for y in (350, 730):
            for x in (330, 750, 1170, 1590):
                drawing.ellipse((x-70, y-70, x+70, y+70), fill="lightgray", outline="black", width=7)
                objects.append({"punti": [[x/1920, y/1080]], "etichette": [1]})
        engine = Sam2Locale()
        first = engine.analizza(image, objects)
        assert first["indicati"] == len(first["oggetti"]) == 8 and first["dimensioni"] == [1920, 1080]
        assert all(obj["contorni"] for obj in first["oggetti"]), first
        second = engine.analizza(image, objects[:1])
        assert second["foto_in_cache"] and second["codifica_secondi"] == 0
        print("Modello reale, immagine sintetica:", json.dumps({k: v for k, v in first.items() if k != "oggetti"}, ensure_ascii=False))
        print("PASS inferenza SAM 2 CPU Full HD e riuso codifica; non è un collaudo della borsa reale")
