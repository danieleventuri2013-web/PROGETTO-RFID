"""Bordo e conteggio della foto ritagliata, senza indicazioni sui campioni."""
from __future__ import annotations

import time

from app.sam2_borsa import rileva_borsa

# Profilo dell'interno della borsa già provato sulle foto: relativo al suo bbox.
INTERNO_BORSA = (.05, .18, .90, .86)


def finestra_interna(borsa, size):
    width, height = size
    x, y, w, h = borsa["rettangolo"]
    x1, y1, x2, y2 = INTERNO_BORSA
    return (max(0, round(x+x1*w)), max(0, round(y+y1*h)),
            min(width, round(x+x2*w)), min(height, round(y+y2*h)))


def riassumi_conteggio(image, masks, scores, borsa_mask, box):
    """Filtra i campioni: interi nella finestra e dentro la sagoma della borsa."""
    import cv2
    import numpy as np

    from app.sam2_count import candidati_circolari

    width, height = image.size
    full_masks = []
    for source in masks:
        mask = np.zeros((height, width), dtype=np.uint8)
        mask[box[1]:box[3], box[0]:box[2]] = np.asarray(source, dtype=np.uint8)
        full_masks.append(mask)
    selected, _ = candidati_circolari(full_masks, scores, image.size)
    objects, excluded = [], []
    for item in selected:
        x, y, w, h = item["bbox"]
        if x <= box[0]+1 or y <= box[1]+1 or x+w >= box[2]-1 or y+h >= box[3]-1:
            excluded.append("regione tagliata dalla finestra dei campioni")
            continue
        mask = full_masks[item["mask_index"]]
        if np.logical_and(mask, borsa_mask).sum() < .95*mask.sum():
            excluded.append("regione esterna alla borsa")
            continue
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        outlines = [[[round(float(p[0][0])/width, 6), round(float(p[0][1])/height, 6)]
                     for p in cv2.approxPolyDP(contour, 1.5, True)] for contour in contours if cv2.contourArea(contour) >= 8]
        objects.append({"id": len(objects)+1, "contorni": outlines,
                        "centro": [round(item["centro"][0]/width, 6), round(item["centro"][1]/height, 6)]})
    return objects, sorted(set(excluded))


def analizza_automatico(engine, image):
    """Prima la borsa, poi AMG 16×16 nel suo interno: nessun numero atteso."""
    from app.sam2_count import crea_generatore

    started = time.perf_counter()
    mask, borsa = rileva_borsa(engine, image)
    result = {"automatico": True, "borsa": borsa, "dimensioni": list(image.size),
              "conteggio": None, "oggetti": [], "avvisi": list(borsa["avvisi"]),
              "modello": getattr(engine, "description", "SAM 2.1 Tiny · CPU"), "griglia": 16}
    if mask is not None:
        box = finestra_interna(borsa, image.size)
        if min(box[2]-box[0], box[3]-box[1]) < 64:
            result["avvisi"].append("area dei campioni troppo piccola: avvicinare la webcam")
        else:
            before = time.perf_counter()
            generator = crea_generatore(engine)
            output = generator(image.crop(box), points_per_batch=8, points_per_crop=16,
                               pred_iou_thresh=.75, stability_score_thresh=.90, crops_n_layers=0,
                               output_bboxes_mask=True)
            objects, warnings = riassumi_conteggio(image, output["masks"], output["scores"], mask, box)
            result.update(oggetti=objects, conteggio=len(objects),
                          tempo_conteggio_secondi=round(time.perf_counter()-before, 2),
                          area_campioni=[round(v/(image.width if i%2 == 0 else image.height), 6) for i, v in enumerate(box)])
            result["avvisi"].extend(warnings)
    result.update(tempo_secondi=round(time.perf_counter()-started, 2),
                  tempo_bordo_secondi=borsa["tempo_secondi"],
                  nota="Profilo della stessa borsa con coperchi rotondi; verificare i contorni e mantenere l'inquadratura configurata.")
    if callable(getattr(engine, "dispositivi_esecuzione", None)):
        result["esecuzione_openvino"] = engine.dispositivi_esecuzione()
    return result
