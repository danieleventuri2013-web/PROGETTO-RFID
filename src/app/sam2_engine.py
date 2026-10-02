"""SAM 2.1 Tiny su CPU: segmentazione assistita di una foto, senza archivio."""
from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "sam2" / "tiny"
MODEL_ID = "facebook/sam2.1-hiera-tiny"


def valida_oggetti(value):
    """Coordinate normalizzate: oggetti separati, clic positivi e negativi."""
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise ValueError("indicare da 1 a 20 campioni")
    result = []
    for obj in value:
        if not isinstance(obj, dict):
            raise ValueError("campione non valido")
        points, labels = obj.get("punti"), obj.get("etichette")
        if not isinstance(points, list) or not 1 <= len(points) <= 16:
            raise ValueError("massimo 16 clic per campione")
        if not isinstance(labels, list) or len(points) != len(labels):
            raise ValueError("etichette dei clic non valide")
        clean = []
        for point, label in zip(points, labels, strict=True):
            if not isinstance(point, list) or len(point) != 2 or label not in (0, 1):
                raise ValueError("clic non valido")
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
                raise ValueError("clic fuori immagine")
            clean.append([float(v) for v in point])
        if labels[0] != 1:
            raise ValueError("il primo clic deve essere dentro il campione")
        result.append({"punti": clean, "etichette": labels})
    return result


def riassumi_maschere(masks, scores, objects, width, height):
    """Contorni visibili e avvisi; mai spacciare il numero di clic per conteggio."""
    import cv2
    import numpy as np

    results, valid = [], []
    for i, (mask, score, obj) in enumerate(zip(masks, scores, objects, strict=True)):
        mask = np.asarray(mask, dtype=np.uint8)
        x, y = obj["punti"][0]
        px, py = min(width - 1, int(x * width)), min(height - 1, int(y * height))
        warnings = []
        pixels = int(mask.sum())
        if not pixels or not mask[py, px]:
            warnings.append("il contorno non include il clic iniziale")
        if pixels > width * height * .35:
            warnings.append("regione molto estesa: verificare se comprende la borsa")
        for prev_id, prev in valid:
            intersection = np.logical_and(mask, prev).sum()
            union = np.logical_or(mask, prev).sum()
            if union and intersection / union > .8:
                warnings.append(f"stessa regione del campione {prev_id}: correggere i clic")
                break
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        outlines = []
        for contour in contours:
            if cv2.contourArea(contour) < 8:
                continue
            contour = cv2.approxPolyDP(contour, 1.5, True)
            outlines.append([[round(float(p[0][0]) / width, 5), round(float(p[0][1]) / height, 5)] for p in contour])
        if not warnings and outlines:
            valid.append((i + 1, mask))
        elif not outlines and not warnings:
            warnings.append("nessun contorno utile")
        results.append({"id": i + 1, "contorni": outlines, "qualita_modello": round(float(score), 3),
                        "area_percento": round(pixels / (width * height) * 100, 2), "avvisi": warnings})
    return {"oggetti": results, "indicati": len(objects), "contorni_distinti": len(valid),
            "dimensioni": [width, height], "modello": "SAM 2.1 Tiny · CPU",
            "nota": "Segmentazione assistita dai clic: il risultato richiede verifica visiva; non è un conteggio automatico."}


class Sam2Locale:
    def __init__(self, model_dir=MODEL_DIR):
        import torch
        from transformers import Sam2Model, Sam2Processor

        torch.set_num_threads(2)
        self.torch = torch
        # Solo file locali: l'installazione scarica i pesi, l'analisi non usa rete.
        self.model = Sam2Model.from_pretrained(str(model_dir), local_files_only=True).to("cpu").eval()
        self.processor = Sam2Processor.from_pretrained(str(model_dir), local_files_only=True)
        self.cached_key = None
        self.cached_embeddings = None

    def analizza(self, image, objects):
        import numpy as np

        started = time.perf_counter()
        width, height = image.size
        key = hashlib.sha256(image.tobytes()).digest() + str(image.size).encode()
        inputs = self.processor(images=image, return_tensors="pt")
        encoding = 0
        with self.torch.inference_mode():
            if key != self.cached_key:
                before = time.perf_counter()
                self.cached_embeddings = self.model.get_image_embeddings(inputs["pixel_values"])
                self.cached_key = key
                encoding = time.perf_counter() - before
            masks, scores = [], []
            # Decoder uno alla volta: memoria limitata anche con Full HD e 20 oggetti.
            for obj in objects:
                points = [[[[(min(width-1, x*width)), (min(height-1, y*height))] for x, y in obj["punti"]]]]
                prompt = self.processor(images=image, input_points=points,
                                        input_labels=[[obj["etichette"]]], return_tensors="pt")
                prompt.pop("pixel_values")
                output = self.model(**prompt, image_embeddings=self.cached_embeddings, multimask_output=True)
                best = int(output.iou_scores[0, 0].argmax())
                # Ridimensionare solo la maschera scelta, anziché tutte e tre.
                selected = output.pred_masks[:, :, best:best+1]
                mask = self.processor.post_process_masks(selected, prompt["original_sizes"])[0][0, 0]
                masks.append(mask.cpu().numpy().astype(np.uint8))
                scores.append(float(output.iou_scores[0, 0, best]))
        result = riassumi_maschere(masks, scores, objects, width, height)
        result.update(tempo_secondi=round(time.perf_counter() - started, 2),
                      codifica_secondi=round(encoding, 2), foto_in_cache=encoding == 0)
        return result
