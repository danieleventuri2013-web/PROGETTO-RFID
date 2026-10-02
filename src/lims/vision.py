"""Visione locale della scatola: immagini limitate, geometria e prove cifrate.

Non assegna identità RFID agli oggetti e non certifica gli agganci del coperchio.
OpenCV e Pillow vengono caricati solo quando si usa la funzione facoltativa.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def cifra(documento, keyring, contesto):
    kid = keyring.default_key_id
    nonce = secrets.token_bytes(12)
    header = b"VIS1" + bytes([kid])
    payload = json.dumps(documento, ensure_ascii=False).encode("utf-8")
    return header + nonce + AESGCM(keyring.require(kid)).encrypt(nonce, payload, header + contesto.encode())


def apri(blob, keyring, contesto):
    blob = bytes(blob)
    if len(blob) < 33 or blob[:4] != b"VIS1":
        raise ValueError("prova visiva non valida")
    return json.loads(AESGCM(keyring.require(blob[4])).decrypt(blob[5:17], blob[17:], blob[:5] + contesto.encode()))


def immagine(testo):
    """JPEG soltanto, massimo Full HD; nessuna decompressione senza limiti."""
    from PIL import Image

    if not isinstance(testo, str) or not 0 < len(testo) <= 2_800_000:
        raise ValueError("fotogramma mancante o troppo grande")
    raw = base64.b64decode(testo, validate=True)
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format != "JPEG" or im.width * im.height > 1920 * 1080 or min(im.size) < 64:
                raise ValueError("usare un JPEG fra 64 pixel e 1920×1080")
            im.load()
            return im.convert("RGB")
    except (OSError, Image.DecompressionBombError) as exc:
        raise ValueError("fotogramma JPEG non leggibile") from exc


def fotografia(im):
    for quality in (88, 78, 65, 50, 35):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        raw = buf.getvalue()
        if len(raw) <= 256 * 1024:
            return {"jpeg": base64.b64encode(raw).decode("ascii"), "sha256": hashlib.sha256(raw).hexdigest(),
                    "larghezza": im.width, "altezza": im.height}
    raise ValueError("foto oltre 256 KiB: ridurre la risoluzione della videocamera")


def punti(value, *, quattro=False):
    if not isinstance(value, list) or len(value) > 100 or (quattro and len(value) != 4):
        raise ValueError("numero di punti non valido")
    result = []
    for p in value:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("coordinate non valide")
        x, y = map(float, p)
        if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("coordinate fuori immagine")
        result.append([x, y])
    if quattro:
        import cv2
        import numpy as np

        polygon = np.array(result, dtype="float32")
        if not cv2.isContourConvex(polygon) or abs(cv2.contourArea(polygon)) < .02:
            raise ValueError("selezionare i quattro angoli in ordine lungo il bordo")
    return result


def vicini(a, b, soglia=.035):
    """Corrispondenza uno a uno; il solo numero uguale non dimostra stabilità."""
    if len(a) != len(b):
        return False
    restanti = list(b)
    for x, y in a:
        if not restanti:
            return False
        indice = min(range(len(restanti)), key=lambda i: math.dist((x, y), restanti[i]))
        if math.dist((x, y), restanti.pop(indice)) > soglia:
            return False
    return True


def riferimenti(gray):
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    corners, ids, _ = cv2.aruco.ArucoDetector(dictionary).detectMarkers(gray)
    h, w = gray.shape
    return {} if ids is None else {str(int(i)): [[float(x/w), float(y/h)] for x, y in c[0]]
                                   for i, c in zip(ids.flatten(), corners, strict=True) if 0 <= int(i) <= 5}


def miniatura(gray, area):
    import cv2
    import numpy as np

    h, w = gray.shape
    src = np.array(area, dtype="float32") * (w, h)
    dst = np.array([[0, 0], [63, 0], [63, 47], [0, 47]], dtype="float32")
    rect = cv2.warpPerspective(gray, cv2.getPerspectiveTransform(src.astype("float32"), dst), (64, 48))
    return cv2.GaussianBlur(rect, (5, 5), 0).flatten().tolist()


def analizza(im, cfg, *, area_manuale=None):
    import cv2
    import numpy as np

    rgb = np.asarray(im)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    markers = riferimenti(gray)
    area, origine, transform = None, "non determinabile", None
    ref = cfg.get("aperta", {})
    if cfg.get("marcatori_scatola"):
        comuni = [str(i) for i in range(4) if str(i) in markers and str(i) in ref.get("markers", {})]
        if len(comuni) >= 3:
            before = np.array([p for i in comuni for p in ref["markers"][i]], dtype="float32")
            after = np.array([p for i in comuni for p in markers[i]], dtype="float32")
            transform, _ = cv2.findHomography(before, after, cv2.RANSAC, .008)
            if transform is not None:
                area = cv2.perspectiveTransform(np.array([ref["area"]], dtype="float32"), transform)[0].tolist()
                origine = "marcatori scatola"
    else:
        edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        candidati = []
        for c in contours:
            poly = cv2.approxPolyDP(c, .025 * cv2.arcLength(c, True), True)
            fraction = abs(cv2.contourArea(poly)) / (w*h)
            if len(poly) == 4 and cv2.isContourConvex(poly) and .12 < fraction < .94:
                xy = poly[:, 0, :].astype("float32") / (w, h)
                # Ordine: alto-sinistra, alto-destra, basso-destra, basso-sinistra.
                sums, diffs = xy.sum(axis=1), xy[:, 0] - xy[:, 1]
                ordered = [xy[sums.argmin()].tolist(), xy[diffs.argmax()].tolist(),
                           xy[sums.argmax()].tolist(), xy[diffs.argmin()].tolist()]
                if ref.get("area") and not vicini(ordered, ref["area"], .10):
                    continue
                if not any(vicini(ordered, other, .04) for _, other in candidati):
                    candidati.append((fraction, ordered))
        candidati.sort(reverse=True)
        if len(candidati) == 1 or (len(candidati) > 1 and candidati[0][0] > candidati[1][0] * 1.5):
            area, origine = candidati[0][1], "bordo automatico"
    if area_manuale is not None:
        area, origine = punti(area_manuale, quattro=True), "bordo confermato dall'operatore"
    if area is not None:
        try:
            area = punti(area, quattro=True)
        except ValueError:
            area, origine = None, "non determinabile"
    result = {"area": area, "origine_area": origine, "markers": markers, "centri": [], "cerchi": [],
              "dimensioni": [w, h], "qualita": "non determinabile", "coperchio": "non determinabile"}
    if area is None or float(gray.std()) < 5 or float(gray.mean()) < 8:
        return result
    mask = np.zeros_like(gray)
    polygon = (np.array(area) * (w, h)).astype("int32")
    cv2.fillConvexPoly(mask, polygon, 255)
    blurred = cv2.GaussianBlur(gray, (7, 7), 1.5)
    min_r = max(5, int(float(cfg.get("raggio_min", .012)) * min(w, h)))
    max_r = max(min_r+1, int(float(cfg.get("raggio_max", .16)) * min(w, h)))
    circles = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=max(10, min_r * 1.6),
                               param1=100, param2=float(cfg.get("sensibilita", 30)), minRadius=min_r, maxRadius=max_r)
    selected = []
    distance = cv2.distanceTransform(255-cv2.Canny(gray, 60, 160), cv2.DIST_L2, 3)
    angles = np.linspace(0, 2*np.pi, 96, endpoint=False)
    if circles is not None:
        for x, y, r in sorted(circles[0], key=lambda c: -c[2]):
            if cv2.pointPolygonTest(polygon, (float(x), float(y)), True) < r * .7:
                continue
            xx = np.clip((x+r*np.cos(angles)).astype(int), 0, w-1)
            yy = np.clip((y+r*np.sin(angles)).astype(int), 0, h-1)
            if float(np.mean(distance[yy, xx] <= max(3, r*.05))) < .60:
                continue
            if any(math.hypot(x-a, y-b) < max(r, s)*.6 for a, b, s in selected):
                continue
            # I riferimenti stampati non sono contenitori.
            if any(cv2.pointPolygonTest((np.array(c)*(w, h)).astype("float32"), (float(x), float(y)), False) >= 0 for c in markers.values()):
                continue
            selected.append((float(x), float(y), float(r)))
    result.update(centri=[[x/w, y/h] for x, y, _ in selected],
                  cerchi=[[x/w, y/h, r/min(w, h)] for x, y, r in selected],
                  qualita="leggibile", miniatura=miniatura(gray, area))
    closed = cfg.get("chiusa", {})
    if cfg.get("marcatori_coperchio"):
        if all(str(i) in markers and str(i) in closed.get("markers", {}) for i in (4, 5)):
            attesi = [p for i in (4, 5) for p in closed["markers"][str(i)]]
            lid_transform = None
            if cfg.get("marcatori_scatola"):
                comuni = [str(i) for i in range(4) if str(i) in markers and str(i) in closed.get("markers", {})]
                if len(comuni) >= 3:
                    before = np.array([p for i in comuni for p in closed["markers"][i]], dtype="float32")
                    after = np.array([p for i in comuni for p in markers[i]], dtype="float32")
                    lid_transform, _ = cv2.findHomography(before, after, cv2.RANSAC, .008)
            elif closed.get("area"):
                lid_transform = cv2.getPerspectiveTransform(np.array(closed["area"], dtype="float32"), np.array(area, dtype="float32"))
            if lid_transform is None:
                return result
            attesi = cv2.perspectiveTransform(np.array([attesi], dtype="float32"), lid_transform)[0].tolist()
            osservati = [p for i in (4, 5) for p in markers[str(i)]]
            result["coperchio"] = "posizionato" if all(math.dist(a, b) < .025 for a, b in zip(attesi, osservati, strict=True)) else "non posizionato"
    elif closed.get("miniatura") and ref.get("miniatura"):
        thumb = np.array(result["miniatura"], dtype=float)
        dc = float(np.mean(abs(thumb - np.array(closed["miniatura"]))))
        da = float(np.mean(abs(thumb - np.array(ref["miniatura"]))))
        # Il materiale trasparente può rendere le due condizioni indistinguibili.
        if dc < 12 and da - dc > 10:
            result["coperchio"] = "posizionato"
        elif da < 12 and dc - da > 10:
            result["coperchio"] = "non posizionato"
    return result


def pagina_marcatori():
    """SVG deterministico stampabile, nessun servizio esterno."""
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    labels = ["Scatola alto sinistra", "Scatola alto destra", "Scatola basso destra",
              "Scatola basso sinistra", "Coperchio sinistra", "Coperchio destra"]
    result = []
    for i, label in enumerate(labels):
        grid = cv2.aruco.generateImageMarker(dictionary, i, 6)
        rects = ''.join(f'<rect x="{x+1}" y="{y+1}" width="1" height="1"/>' for y in range(6) for x in range(6) if grid[y, x] == 0)
        result.append({"id": i, "nome": label, "svg": f'<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm" viewBox="0 0 8 8"><rect width="8" height="8" fill="white"/><g fill="black">{rects}</g></svg>'})
    return result
