"""Banco ottico autonomo: HTTP con fotogrammi sintetici, senza hardware."""
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

from app.vision_preview import crea_server


def test_anteprima_conteggia_otto_cerchi_e_protegge_fotogrammi():
    import cv2
    import numpy as np
    from PIL import Image

    canvas = np.full((480, 640, 3), 240, dtype=np.uint8)
    cv2.rectangle(canvas, (35, 35), (605, 445), (20, 20, 20), 5)
    for y in (155, 315):
        for x in (110, 250, 390, 530):
            cv2.circle(canvas, (x, y), 30, (30, 30, 30), 4)
    buf = io.BytesIO()
    Image.fromarray(canvas).save(buf, "JPEG")
    payload = {"immagine_base64": base64.b64encode(buf.getvalue()).decode()}
    server, token = crea_server(0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(data, secret=token):
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/anteprima",
                                     json.dumps(data).encode(), {"X-RFID-Token": secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    try:
        code, result = post(payload)
        assert code == 200 and result["area"] is not None, result
        assert len(result["centri"]) == len(result["cerchi"]) == 8, result
        assert "miniatura" not in result and "immagine_base64" not in result
        assert post(payload, "errato")[0] == 401
        assert post({"immagine_base64": "JPEG non valido"})[0] == 400
        assert post({**payload, "raggio_min": .2, "raggio_max": .1})[0] == 400
        assert post({**payload, "sensibilita": float("nan")})[0] == 400
        # Fotogramma Full HD nativo: il server conserva tutti i pixel.
        fullhd = np.full((1080, 1920, 3), 240, dtype=np.uint8)
        cv2.rectangle(fullhd, (105, 105), (1815, 975), (20, 20, 20), 8)
        for y in (350, 735):
            for x in (330, 750, 1170, 1590):
                cv2.circle(fullhd, (x, y), 70, (30, 30, 30), 8)
        buf = io.BytesIO()
        Image.fromarray(fullhd).save(buf, "JPEG", quality=96)
        code, result = post({"immagine_base64": base64.b64encode(buf.getvalue()).decode()})
        assert code == 200 and result["dimensioni"] == [1920, 1080], result
        assert len(result["cerchi"]) == 8, result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    test_anteprima_conteggia_otto_cerchi_e_protegge_fotogrammi()
    print("PASS prova ottica autonoma: 8 cerchi, bordo, token e immagini/parametri invalidi")
