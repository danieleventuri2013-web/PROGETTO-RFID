"""Perimetro sperimentale della borsa con riferimenti di inquadratura fissi."""
from __future__ import annotations

import time

# Punti sulle pareti, ricavati una volta dalla prima inquadratura di prova.
# Non sono punti sui campioni e non costituiscono addestramento del modello.
PUNTI_PARETI = ((.5, .12), (.92, .5), (.5, .88), (.06, .5))


def seleziona_borsa(masks, scores, punti=PUNTI_PARETI):
    """Preferisce una regione ampia che comprende tutti i riferimenti."""
    import cv2
    import numpy as np

    candidates = []
    for index, (mask, score) in enumerate(zip(masks, scores, strict=True)):
        mask = np.asarray(mask, dtype=np.uint8)
        height, width = mask.shape
        area = float(mask.mean())
        if not .55 <= area <= .98 or float(score) < .8:
            continue
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            continue
        contour = max(contours, key=cv2.contourArea)
        filled = np.zeros_like(mask)
        cv2.drawContours(filled, [contour], -1, 1, cv2.FILLED)
        # Elimina maschere frammentate: la regione principale deve dominare.
        if np.logical_and(filled, mask).sum() < .95 * mask.sum():
            continue
        # Il perimetro comprende anche i buchi interni esclusi da SAM per
        # riflessi/agganci: verificare i riferimenti sulla sagoma riempita.
        if not all(filled[min(height-1, int(y*height)), min(width-1, int(x*width))] for x, y in punti):
            continue
        candidates.append((float(score), index, filled, contour))
    if not candidates:
        return None, {"rilevata": False, "avvisi": ["bordo non determinabile con il profilo di inquadratura"]}
    score, index, mask, contour = max(candidates, key=lambda item: item[0])
    height, width = mask.shape
    margin = max(2, round(min(width, height)*.01))
    sides = {"sinistra": mask[:, :margin].any(axis=1), "destra": mask[:, -margin:].any(axis=1),
             "alto": mask[:margin].any(axis=0), "basso": mask[-margin:].any(axis=0)}
    clipped = [name for name, contact in sides.items() if int(contact.sum()) >= max(5, round(len(contact)*.02))]
    outline = cv2.approxPolyDP(contour, 1.5, True)
    x, y, w, h = cv2.boundingRect(contour)
    return mask, {"rilevata": True, "variante": index, "score_modello": round(score, 3),
                  "area_percento": round(float(mask.mean())*100, 2),
                  "contorno": [[round(float(p[0][0])/width, 6), round(float(p[0][1])/height, 6)] for p in outline],
                  "rettangolo": [x, y, w, h], "lati_al_limite": clipped,
                  "margine_limite_pixel": margin,
                  "perimetro_completo": not clipped,
                  "avvisi": ["perimetro al limite della foto: i tratti mancanti non sono ricostruiti"] if clipped else []}


def rileva_borsa(engine, image, punti=PUNTI_PARETI):
    """Una codifica e tre maschere SAM, senza conoscere il numero di campioni."""
    started = time.perf_counter()
    width, height = image.size
    inputs = engine.processor(images=image,
                              input_points=[[[[x*width, y*height] for x, y in punti]]],
                              input_labels=[[[1]*len(punti)]], return_tensors="pt")
    with engine.torch.inference_mode():
        embeddings = engine.model.get_image_embeddings(inputs.pop("pixel_values"))
        output = engine.model(**inputs, image_embeddings=embeddings, multimask_output=True)
        masks = engine.processor.post_process_masks(output.pred_masks, inputs["original_sizes"])[0][0]
    masks = masks.cpu().numpy()
    scores = output.iou_scores[0, 0].cpu().numpy()
    mask, result = seleziona_borsa(masks, scores, punti)
    result.update(dimensioni=[width, height], punti_profilo=[list(p) for p in punti],
                  tempo_secondi=round(time.perf_counter()-started, 2),
                  metodo="SAM 2.1 Tiny con quattro riferimenti fissi sulle pareti; contorno esterno della maschera")
    return mask, result
