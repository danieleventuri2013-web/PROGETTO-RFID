"""Rettifica geometrica, conteggio interno e API automatiche protette."""
from __future__ import annotations

import base64
import io
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sam2_automatico import analizza_automatico, riassumi_conteggio
from app.sam2_geometry import rettifica, valida_calibrazione
from app.vision_preview import crea_server
from lims.vision import immagine


def test_rettifica_con_proporzioni_e_altezza():
    image = Image.new("RGB", (1000, 800), "white")
    points = [[.1, .1], [.85, .2], [.9, .85], [.2, .9]]
    draw = ImageDraw.Draw(image)
    colors = ["red", "green", "blue", "yellow"]
    for (x, y), color in zip(points, colors, strict=True):
        draw.ellipse((x*999-12, y*799-12, x*999+12, y*799+12), fill=color)
    config = {"punti": points, "larghezza_cm": 60, "lunghezza_cm": 40, "altezza_camera_cm": 80}
    result = rettifica(image, config)
    corrected = immagine(result["immagine_base64"])
    assert abs(corrected.width/corrected.height-1.5)<.01
    assert not result["correzione"]["proporzioni_stimate"]
    assert result["correzione"]["altezza_camera_cm"] == 80
    for (x, y), color in zip(result["correzione"]["angoli_rettificati"], colors, strict=True):
        from PIL import ImageColor
        actual = corrected.getpixel((round(x*corrected.width), round(y*corrected.height)))
        assert max(abs(a-b) for a,b in zip(actual, ImageColor.getrgb(color), strict=True)) < 45
    # L'altezza è documentata, non alterata in una falsa compensazione 3D.
    again = rettifica(image, {**config, "altezza_camera_cm": 120})
    assert again["immagine_base64"] == result["immagine_base64"]
    assert rettifica(image, {"punti": points})["correzione"]["proporzioni_stimate"]


def test_calibrazione_rifiuta_incrocioni_e_misure_incomplete():
    points = [[.1, .1], [.9, .1], [.9, .9], [.1, .9]]
    bad = [{"punti": points[::-1]}, {"punti": [points[0], points[2], points[1], points[3]]},
           {"punti": [[float("nan"), .1]]+points[1:]}, {"punti": points, "larghezza_cm": 50},
           {"punti": points, "altezza_camera_cm": -1}]
    for config in bad:
        try:
            valida_calibrazione(config)
        except ValueError:
            pass
        else:
            raise AssertionError(config)


def test_conteggio_esclude_fuori_borsa_e_duplicati():
    import cv2

    image = Image.new("RGB", (300, 200))
    source = np.zeros((160, 260), dtype=np.uint8)
    cv2.circle(source, (70, 60), 20, 1, -1)
    outside = np.zeros_like(source)
    cv2.circle(outside, (210, 60), 20, 1, -1)
    mask = np.zeros((200, 300), dtype=np.uint8)
    mask[20:180, 20:160] = 1
    objects, warnings = riassumi_conteggio(image, [source, source, outside], [.95, .9, .99], mask, (20, 20, 280, 180))
    assert len(objects) == 1 and objects[0]["id"] == 1
    assert objects[0]["centro"] == [.3, .4]
    assert warnings == ["regione esterna alla borsa"]


def test_bordo_assente_non_significa_zero_campioni():
    borsa = {"rilevata": False, "tempo_secondi": 1, "avvisi": ["bordo non determinabile"]}
    with patch("app.sam2_automatico.rileva_borsa", return_value=(None, borsa)):
        result = analizza_automatico(None, Image.new("RGB", (500, 300)))
    assert result["conteggio"] is None and not result["oggetti"]


def test_http_automatico_e_preparazione():
    import json
    import urllib.error
    import urllib.request

    server, token = crea_server(0, sam2=object())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    buf = io.BytesIO()
    Image.new("RGB", (800, 500), "white").save(buf, format="JPEG")
    payload = {"immagine_base64": base64.b64encode(buf.getvalue()).decode()}

    def post(path, data=payload, secret=token, origin="http://127.0.0.1:8770"):
        request = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/api/sam2/{path}",
                                         data=json.dumps(data).encode(),
                                         headers={"X-RFID-Token":secret, "Content-Type":"application/json", "Origin":origin})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    try:
        with patch("app.sam2_automatico.analizza_automatico", return_value={"automatico":True,"conteggio":6}) as analyze:
            assert post("automatico")[1]["conteggio"] == 6
            assert analyze.call_args[0][1].size == (800, 500)
            assert post("automatico", secret="errato")[0] == 401
            assert post("automatico", origin="https://esterno.example")[0] == 403
            assert post("automatico", data={"immagine_base64":"no"})[0] == 400
        points = [[.1,.1],[.9,.1],[.9,.9],[.1,.9]]
        code, data = post("prepara", {**payload,"calibrazione":{"punti":points}})
        assert code == 200 and immagine(data["immagine_base64"]).size == tuple(data["dimensioni"])
        assert post("prepara", {**payload,"calibrazione":{"punti":points[::-1]}})[0] == 400
        assert post("prepara", secret="errato")[0] == 401
        # Le due API condividono il lock: nessuna analisi CPU accodata.
        entered, release = threading.Event(), threading.Event()
        first_result = []

        def delayed(*_args):
            entered.set()
            assert release.wait(5)
            return {"conteggio": 6}

        with patch("app.sam2_automatico.analizza_automatico", side_effect=delayed):
            pending = threading.Thread(target=lambda: first_result.append(post("automatico")))
            pending.start()
            try:
                assert entered.wait(3)
                assert post("automatico")[0] == 409
                assert post("prepara", {**payload, "calibrazione": {"punti": points}})[0] == 409
            finally:
                release.set()
                pending.join(5)
            assert first_result[0][0] == 200
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/sam2-auto.html?t={token}") as response:
            assert b"sam2-auto.js" in response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


if __name__ == "__main__":
    tests = [value for name, value in list(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"OK {test.__name__}")
    print(f"{len(tests)}/{len(tests)} test superati")
