"""Decodifica locale dei fotogrammi del browser, senza conservarli."""
from __future__ import annotations

import base64
import binascii
import io
import threading

_DECODER = threading.Lock()


def decodifica_fotogramma(dati):
    """Limita byte, pixel e concorrenza prima di usare il decoder ottico."""
    from .workflow import WorkflowError

    testo = dati.get("immagine_base64")
    if not isinstance(testo, str) or not 0 < len(testo) <= 2_800_000:
        raise WorkflowError("fotogramma mancante o troppo grande")
    try:
        blob = base64.b64decode(testo, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WorkflowError("fotogramma non valido") from exc
    if not _DECODER.acquire(blocking=False):
        raise WorkflowError("decoder webcam occupato: riprovare")
    try:
        try:
            import zxingcpp
            from PIL import Image
        except ImportError as exc:
            raise WorkflowError("decoder webcam non installato: eseguire python -m pip install -r requirements-qr.txt") from exc
        try:
            with Image.open(io.BytesIO(blob)) as image:
                if image.format != "JPEG" or image.width * image.height > 1920 * 1080:
                    raise ValueError("usare un fotogramma JPEG fino a 1920×1080")
                image.load()
                image = image.convert("RGB")
                risultati = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
                return {"larghezza": image.width, "altezza": image.height, "codici": [
                    {"testo": r.text, "vertici": [[p.x, p.y] for p in (
                        r.position.top_left, r.position.top_right,
                        r.position.bottom_right, r.position.bottom_left)]}
                    for r in risultati if r.valid]}
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise WorkflowError(f"fotogramma illeggibile: {exc}") from exc
    finally:
        _DECODER.release()
