"""Rettifica del piano del contenitore dalla calibrazione a quattro angoli."""
from __future__ import annotations

import base64
import io
import math


def valida_calibrazione(data):
    if not isinstance(data, dict):
        raise ValueError("calibrazione mancante")
    points = data.get("punti")
    if not isinstance(points, list) or len(points) != 4:
        raise ValueError("indicare i quattro angoli in ordine: alto sinistra, alto destra, basso destra, basso sinistra")
    clean = []
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
            raise ValueError("angolo fuori immagine")
        clean.append([float(v) for v in point])
    for i in range(4):
        a, b, c = clean[i], clean[(i+1)%4], clean[(i+2)%4]
        cross = (b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])
        if cross <= .0001:
            raise ValueError("gli angoli devono formare un rettangolo prospettico convesso, in senso orario")
    area = sum(clean[i][0]*clean[(i+1)%4][1]-clean[(i+1)%4][0]*clean[i][1] for i in range(4))/2
    if area < .02:
        raise ValueError("rettangolo troppo piccolo")
    sizes = [data.get("larghezza_cm"), data.get("lunghezza_cm")]
    if all(v is None for v in sizes):
        ratio = None
    elif any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not .1 <= v <= 1000 for v in sizes):
        raise ValueError("indicare entrambe le dimensioni reali oppure lasciarle vuote")
    else:
        ratio = sizes[0]/sizes[1]
        if not .2 <= ratio <= 5:
            raise ValueError("rapporto delle dimensioni fuori intervallo")
    camera_height = data.get("altezza_camera_cm")
    if camera_height is not None and (isinstance(camera_height, bool) or not isinstance(camera_height, (int, float)) or not math.isfinite(camera_height) or not .1 <= camera_height <= 1000):
        raise ValueError("altezza della camera non valida")
    return clean, ratio


def rettifica(image, data):
    """Omografia sul piano segnato; non corregge lente o parallasse in altezza."""
    import cv2
    import numpy as np
    from PIL import Image

    points, ratio = valida_calibrazione(data)
    source = np.asarray([[x*(image.width-1), y*(image.height-1)] for x, y in points], dtype=np.float32)
    apparent_w = (math.dist(source[0], source[1])+math.dist(source[3], source[2]))/2
    apparent_h = (math.dist(source[0], source[3])+math.dist(source[1], source[2]))/2
    estimated = ratio is None
    ratio = ratio or apparent_w/apparent_h
    # Conserva la densità disponibile; le dimensioni reali fissano il rapporto.
    body_h = min(apparent_h, apparent_w/ratio)
    body_w = body_h*ratio
    # Piccolo margine reale fuori dai bordi per permettere a SAM di distinguerli.
    width, height = body_w/.92, body_h/.92
    scale = min(1, 1920/width, 1080/height)
    width, height = round(width*scale), round(height*scale)
    if min(width, height) < 100:
        raise ValueError("area rettificata troppo piccola: avvicinare la webcam")
    mx, my = width*.04, height*.04
    target = np.asarray([[mx, my], [width-1-mx, my], [width-1-mx, height-1-my], [mx, height-1-my]], dtype=np.float32)
    transform = cv2.getPerspectiveTransform(source, target)
    pixels = cv2.warpPerspective(np.asarray(image), transform, (width, height), flags=cv2.INTER_LINEAR)
    corrected = Image.fromarray(pixels)
    buf = io.BytesIO()
    corrected.save(buf, format="JPEG", quality=96)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    if len(encoded) > 2_800_000:
        buf = io.BytesIO()
        corrected.save(buf, format="JPEG", quality=85)
        encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    if len(encoded) > 2_800_000:
        raise ValueError("foto rettificata troppo grande")
    return {"immagine_base64": encoded, "dimensioni": [width, height],
            "correzione": {"rapporto": round(ratio, 4), "proporzioni_stimate": estimated,
                           "altezza_camera_cm": data.get("altezza_camera_cm"),
                           "angoli_rettificati": (target/np.asarray([width, height])).round(6).tolist(),
                           "nota": "Correzione prospettica del piano segnato; non corregge distorsione dell'obiettivo o parallasse dei campioni in altezza.",
                           "avvisi": ["dimensioni reali non indicate: proporzioni stimate dall'immagine"] if estimated else []}}
