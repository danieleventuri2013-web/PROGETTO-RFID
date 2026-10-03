"""Ponte locale verso gli stessi motori del banco webcam SAM 2 / Qwen / YOLO."""
from __future__ import annotations

import base64
import io
import json
import math
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from lims import vision

MODELLI = ("cerchi", "sam2", "qwen", "yolo")
# Motori del servizio esterno sulla porta 8772: richiedono area e prospettiva salvate.
MODELLI_AI = ("sam2", "qwen", "yolo")
TIMEOUT = {"sam2": 300, "qwen": 120, "yolo": 60}
LOGS = Path(__file__).resolve().parents[2] / "logs"


def servizio():
    try:
        url = urlsplit((LOGS / "sam2_url.txt").read_text(encoding="utf-8").strip())
        token = parse_qs(url.query).get("t", [None])[0]
        if url.scheme != "http" or url.hostname != "127.0.0.1" or url.port != 8772 or not token:
            return None
        return {"base": "http://127.0.0.1:8772", "token": token,
                "console": "/sam2-auto.html?" + urlencode({"t": token, "port": 8772})}
    except (OSError, ValueError):
        return None


def prepara(im, profilo=None):
    """Riuso della calibrazione salvata dal banco: ritaglio prima della rettifica."""
    if profilo is None:
        return im, None
    if not isinstance(profilo, dict) or profilo.get("versione") != 2:
        raise ValueError("profilo webcam non valido: ripetere la configurazione dell'area")
    area, dims = profilo.get("area"), profilo.get("dimensioni")
    if not isinstance(area, list) or len(area) != 4 or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
        for v in area
    ) or area[2]-area[0] < .03 or area[3]-area[1] < .03:
        raise ValueError("area webcam non valida")
    if not isinstance(dims, list) or len(dims) != 2 or any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 64 for v in dims
    ) or abs(dims[0]/dims[1]-im.width/im.height) >= .02:
        raise ValueError("inquadratura cambiata: ripetere la configurazione dell'area")
    mode = profilo.get("modo")
    if mode not in ("rettangolo", "prospettiva"):
        raise ValueError("modalità area non valida")
    bounds = area[:]
    if mode == "prospettiva":
        from app.sam2_geometry import valida_calibrazione

        points, _ = valida_calibrazione({"punti": profilo.get("punti"),
            "larghezza_cm": profilo.get("larghezza_cm"), "lunghezza_cm": profilo.get("lunghezza_cm"),
            "altezza_camera_cm": profilo.get("altezza_camera_cm")})
        if any(not area[0] <= x <= area[2] or not area[1] <= y <= area[3] for x, y in points):
            raise ValueError("angoli esterni all'area configurata")
        px, py = (area[2]-area[0])*.06, (area[3]-area[1])*.06
        bounds = [max(0, area[0]-px), max(0, area[1]-py), min(1, area[2]+px), min(1, area[3]+py)]
    left, top, right, bottom = [round(v*(im.width if i % 2 == 0 else im.height)) for i, v in enumerate(bounds)]
    cropped = im.crop((left, top, right, bottom))
    if min(cropped.size) < 100:
        raise ValueError("area troppo piccola: selezionare almeno 100 pixel per lato")
    if mode == "rettangolo":
        return cropped, {"nota": "Area webcam salvata: ritaglio ai pixel della sorgente."}
    from app.sam2_geometry import rettifica

    corrected = rettifica(cropped, {"punti": [[(x*im.width-left)/cropped.width, (y*im.height-top)/cropped.height] for x, y in points],
        "larghezza_cm": profilo.get("larghezza_cm"), "lunghezza_cm": profilo.get("lunghezza_cm"),
        "altezza_camera_cm": profilo.get("altezza_camera_cm")})
    return vision.immagine(corrected["immagine_base64"]), corrected["correzione"]


def jpeg(im):
    for quality in (96, 90, 82, 74):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        raw = base64.b64encode(buf.getvalue()).decode("ascii")
        if len(raw) <= 2_800_000:
            return raw
    raise ValueError("fotogramma troppo grande: restringere l'area della webcam")


def analizza(im, modello):
    if modello not in MODELLI_AI:
        raise ValueError("scegliere SAM 2, Qwen oppure YOLO")
    target = servizio()
    if target is None:
        raise RuntimeError("servizio di riconoscimento non avviato: aprire il banco SAM 2 / Qwen / YOLO")
    req = urllib.request.Request(target["base"] + f"/api/{modello}/automatico",
        data=json.dumps({"immagine_base64": jpeg(im)}).encode(),
        headers={"Content-Type": "application/json", "X-RFID-Token": target["token"]})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT[modello]) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            error = json.load(exc).get("errore", "riconoscimento non riuscito")
        except (ValueError, OSError):
            error = "riconoscimento non riuscito"
        raise RuntimeError(str(error)[:300]) from None
    except OSError:
        raise RuntimeError("servizio SAM 2 / Qwen / YOLO non disponibile: avviare il banco di riconoscimento") from None
    if not isinstance(result, dict) or result.get("dimensioni") != list(im.size):
        raise ValueError("risultato del motore non valido per questo scatto")
    count, objects = result.get("conteggio"), result.get("oggetti", [])
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 100):
        raise ValueError("conteggio del motore non valido")
    if not isinstance(objects, list) or len(objects) > 100 or (count is not None and count != len(objects)):
        raise ValueError("conteggio diverso dai campioni riconosciuti")
    if any(not isinstance(obj, dict) for obj in objects):
        raise ValueError("campioni del motore non validi")
    centers = vision.punti([obj.get("centro") for obj in objects])
    if len({tuple(p) for p in centers}) != len(centers):
        raise ValueError("centri duplicati nel risultato")
    for obj in objects:
        contours = obj.get("contorni", [])
        if not isinstance(contours, list) or len(contours) > 10:
            raise ValueError("contorni del motore non validi")
        for poly in contours:
            if not isinstance(poly, list) or len(poly) > 2000:
                raise ValueError("contorno troppo esteso")
            for point in poly:
                vision.punti([point])
    bag = result.get("borsa", {})
    if not isinstance(bag, dict):
        raise ValueError("bordo del motore non valido")
    outline = bag.get("contorno", [])
    if not isinstance(outline, list) or len(outline) > 2000:
        raise ValueError("bordo del motore troppo esteso")
    for point in outline:
        vision.punti([point])
    return result


def scena_uguale(reference, current, centers):
    """Controllo locale ancorato allo scatto, anche nei pressi dei singoli centri."""
    import cv2
    import numpy as np

    if reference.size != current.size:
        return False
    a = cv2.GaussianBlur(cv2.cvtColor(np.asarray(reference), cv2.COLOR_RGB2GRAY), (5, 5), 0)
    b = cv2.GaussianBlur(cv2.cvtColor(np.asarray(current), cv2.COLOR_RGB2GRAY), (5, 5), 0)
    delta = cv2.absdiff(a, b)
    if float(delta.mean()) >= 12:
        return False
    h, w = delta.shape
    radius = max(10, round(min(w, h)*.04))
    for x, y in centers:
        cx, cy = round(x*(w-1)), round(y*(h-1))
        patch = delta[max(0, cy-radius):min(h, cy+radius+1), max(0, cx-radius):min(w, cx+radius+1)]
        if float(patch.mean()) >= 12:
            return False
    return True
