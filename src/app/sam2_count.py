"""Generazione automatica di maschere SAM 2 e filtri dei campioni rotondi."""
from __future__ import annotations

import math

import cv2
import numpy as np


def crea_generatore(engine):
    """Griglia SAM 2 quadrata: evita la normalizzazione longest-edge di SAM 1.

    La versione installata di Sam2ImageProcessor.generate_crop_boxes usa la
    scala uniforme di SAM 1, mentre SAM 2 ridimensiona separatamente i due assi
    a 1024. Su immagini rettangolari la griglia finirebbe solo in parte della
    foto. Il decoder e i filtri AMG restano quelli della libreria ufficiale.
    """
    from transformers.pipelines.mask_generation import MaskGenerationPipeline

    class GrigliaSam2(MaskGenerationPipeline):
        def preprocess(self, image, points_per_batch=8, points_per_crop=16, crops_n_layers=0, **_kwargs):
            if crops_n_layers != 0:
                raise ValueError("questo banco usa un solo livello di ricerca")
            inputs = engine.processor(images=image, return_tensors="pt")
            with engine.torch.inference_mode():
                embeddings = engine.model.get_image_embeddings(inputs["pixel_values"])
            target = engine.processor.target_size
            coordinates = [[(x+.5)/points_per_crop*target, (y+.5)/points_per_crop*target]
                           for y in range(points_per_crop) for x in range(points_per_crop)]
            for offset in range(0, len(coordinates), points_per_batch):
                points = coordinates[offset:offset+points_per_batch]
                if offset % 64 == 0:
                    print(f"  griglia SAM 2: {offset}/{len(coordinates)} punti", flush=True)
                yield {"input_points": engine.torch.tensor(points, dtype=engine.torch.float32).reshape(1, len(points), 1, 2),
                       "input_labels": engine.torch.ones((1, len(points), 1), dtype=engine.torch.int64),
                       "input_boxes": engine.torch.tensor([[0, 0, image.width, image.height]]),
                       "original_sizes": inputs["original_sizes"], "image_embeddings": embeddings,
                       "is_last": offset + points_per_batch >= len(coordinates)}

    return GrigliaSam2(model=engine.model, image_processor=engine.processor.image_processor,
                      task="mask-generation", device=-1)


def candidati_circolari(masks, scores, size):
    """Criteri uguali per tutte le foto; non conosce il numero atteso."""
    width, height = size
    candidates = []
    for i, (mask, score) in enumerate(zip(masks, scores, strict=True)):
        mask = np.asarray(mask, dtype=np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(contour)
        if not .0015 * width * height <= area <= .09 * width * height:
            continue
        if area < .85 * sum(cv2.contourArea(c) for c in contours):
            continue
        perimeter = cv2.arcLength(contour, True)
        circularity = 4 * math.pi * area / (perimeter * perimeter) if perimeter else 0
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        solidity = area / hull_area if hull_area else 0
        bx, by, bw, bh = cv2.boundingRect(contour)
        # Queste foto contengono campioni interi. Una regione tagliata dal bordo
        # non è conteggiabile con questo criterio: segnalarla nell'esito grezzo.
        if bx <= 1 or by <= 1 or bx + bw >= width - 1 or by + bh >= height - 1:
            continue
        if circularity < .65 or solidity < .88 or not .6 <= bw / bh <= 1.65:
            continue
        moments = cv2.moments(contour)
        x, y = moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]
        candidates.append({"mask_index": i, "score": float(score), "centro": [x, y],
                           "raggio_equivalente": math.sqrt(area / math.pi), "area": area,
                           "circolarita": circularity, "solidita": solidity,
                           "bbox": [bx, by, bw, bh]})
    selected = []
    for candidate in sorted(candidates, key=lambda c: -c["score"]):
        mask = np.asarray(masks[candidate["mask_index"]], dtype=bool)
        duplicate = False
        for prev in selected:
            prev_mask = np.asarray(masks[prev["mask_index"]], dtype=bool)
            intersection = np.logical_and(mask, prev_mask).sum()
            smaller = min(mask.sum(), prev_mask.sum())
            distance = math.dist(candidate["centro"], prev["centro"])
            if smaller and intersection / smaller > .7 and distance < .6 * max(candidate["raggio_equivalente"], prev["raggio_equivalente"]):
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
    selected.sort(key=lambda c: (round(c["centro"][1] / (height * .2)), c["centro"][0]))
    return selected, len(candidates)


