"""Etichette di nuove foto per l'addestramento YOLO: proposta, correzione, contorni.

Con il modello già addestrato le etichette non si disegnano da zero:

1. YOLO propone i riquadri (``--pesi``, il modello in uso);
2. si correggono a mano i soli errori, in due file JSON: ``--aggiunte``
   (riquadri persi, in pixel) e ``--togli`` (numeri YOLO da scartare);
3. SAM 2 ricava il contorno di ogni riquadro (prompt a riquadro), perché
   ``tools/scene_sintetiche.py`` ritaglia i contenitori dal contorno, non dal
   rettangolo che comprenderebbe anche il fondo.

Il risultato è un ``<foto>/risultato.json`` nello stesso formato del
conteggio SAM, quindi ``tools/addestra_yolo.py --sam`` lo legge senza
modifiche. Una foto della scatola vuota (atteso 0) produce un file senza
oggetti: un esempio negativo. Il numero atteso è obbligatorio e si controlla
qui: le etichette di una foto con un contenitore in più o in meno
insegnerebbero al modello proprio l'errore che deve evitare.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

from PIL import Image, ImageDraw

from app.yolo_engine import YoloLocale, pesi_predefiniti

# Sotto questa sovrapposizione fra contorno SAM e riquadro il contorno ha
# preso altro (solo il coperchio, o il vicino): si tiene il riquadro come ellisse.
IOU_MINIMA = .6


def _iou(a, b):
    w = min(a[2], b[2])-max(a[0], b[0])
    h = min(a[3], b[3])-max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.
    inter = w*h
    return inter/((a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter)


def proposte(engine, image, togli=(), aggiunte=()):
    """Riquadri in pixel con la loro origine: YOLO tranne i numeri scartati, più le aggiunte."""
    width, height = image.size
    result = engine.analizza_automatico(image)
    boxes = []
    for obj in result["oggetti"]:
        if obj["id"] in togli:
            continue
        points = [p for poly in obj["contorni"] for p in poly]
        boxes.append({"riquadro": [min(p[0] for p in points)*width, min(p[1] for p in points)*height,
                                   max(p[0] for p in points)*width, max(p[1] for p in points)*height],
                      "origine": "yolo", "id_yolo": obj["id"], "confidenza": obj["confidenza"]})
    for box in aggiunte:
        boxes.append({"riquadro": [float(v) for v in box], "origine": "aggiunta"})
    return boxes, result


def contorni_sam(sam, image, boxes):
    """Un contorno per riquadro, normalizzato; ellisse nel riquadro se SAM ha preso altro."""
    import cv2
    import numpy as np

    width, height = image.size
    inputs = sam.processor(images=image, return_tensors="pt")
    objects = []
    with sam.torch.inference_mode():
        embeddings = sam.model.get_image_embeddings(inputs["pixel_values"])
        for item in boxes:
            x1, y1, x2, y2 = item["riquadro"]
            prompt = sam.processor(images=image, input_boxes=[[[x1, y1, x2, y2]]], return_tensors="pt")
            prompt.pop("pixel_values")
            output = sam.model(**prompt, image_embeddings=embeddings, multimask_output=False)
            mask = sam.processor.post_process_masks(output.pred_masks, prompt["original_sizes"])[0][0, 0]
            mask = mask.cpu().numpy().astype(np.uint8)
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contour = max(contours, key=cv2.contourArea) if contours else None
            iou = 0.
            if contour is not None:
                bx, by, bw, bh = cv2.boundingRect(contour)
                iou = _iou((bx, by, bx+bw, by+bh), item["riquadro"])
            if iou >= IOU_MINIMA:
                points = cv2.approxPolyDP(contour, 1.5, True)[:, 0]
                source = "sam"
            else:
                angles = np.linspace(0, 2*np.pi, 48, endpoint=False)
                points = np.stack([(x1+x2)/2+(x2-x1)/2*np.cos(angles), (y1+y2)/2+(y2-y1)/2*np.sin(angles)], 1)
                source = "ellisse"
            objects.append({**item, "contorni": [[[round(float(x)/width, 5), round(float(y)/height, 5)] for x, y in points]],
                            "contorno": source, "iou_sam_riquadro": round(iou, 3),
                            "qualita_modello": round(float(output.iou_scores[0, 0, 0]), 3)})
    return objects


def annota(image, objects, title, destination):
    image = image.convert("RGB").copy()
    drawing = ImageDraw.Draw(image)
    colors = {"sam": "#22dd77", "ellisse": "#ffb000"}
    for i, obj in enumerate(objects, 1):
        for poly in obj["contorni"]:
            points = [(round(x*image.width), round(y*image.height)) for x, y in poly]
            drawing.line(points+points[:1], fill=colors[obj["contorno"]], width=3)
        x1, y1, x2, y2 = obj["riquadro"]
        drawing.rectangle((x1, y1, x2, y2), outline="#ff3355" if obj["origine"] == "aggiunta" else "#5599ff", width=1)
        x, y = (x1+x2)/2, (y1+y2)/2
        drawing.ellipse((x-12, y-12, x+12, y+12), fill="#143e2b")
        drawing.text((x-5, y-7), str(i), fill="white")
    drawing.rectangle((0, 0, image.width, 26), fill="#143e2b")
    drawing.text((8, 7), title, fill="white")
    image.save(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foto", type=Path, nargs="+")
    parser.add_argument("--attesi", type=int, nargs="+", required=True)
    parser.add_argument("--pesi", default=pesi_predefiniti(), help="modello che propone i riquadri; predefinito quello del servizio")
    parser.add_argument("--confidenza", type=float, default=.25)
    parser.add_argument("--aggiunte", type=Path, help="JSON {nome_foto: [[x1,y1,x2,y2] px, ...]} dei contenitori persi")
    parser.add_argument("--togli", type=Path, help="JSON {nome_foto: [numero YOLO, ...]} dei riquadri da scartare")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.attesi) != len(args.foto):
        parser.error("un conteggio atteso per ogni foto")
    extra = json.loads(args.aggiunte.read_text(encoding="utf-8")) if args.aggiunte else {}
    remove = json.loads(args.togli.read_text(encoding="utf-8")) if args.togli else {}
    from app.sam2_engine import Sam2Locale

    engine, sam = YoloLocale(args.pesi, confidenza=args.confidenza), Sam2Locale()
    errors = []
    for photo, expected in zip(args.foto, args.attesi, strict=True):
        image = Image.open(photo).convert("RGB")
        boxes, proposal = proposte(engine, image, remove.get(photo.name, []), extra.get(photo.name, []))
        objects = contorni_sam(sam, image, boxes) if boxes else []
        folder = args.output/photo.stem
        folder.mkdir(parents=True, exist_ok=True)
        data = {"automatico": False, "foto": photo.name, "dimensioni": list(image.size), "attesi": expected,
                "proposta": {"modello": proposal["modello"], "conteggio": proposal["conteggio"]},
                "oggetti": [{"id": i, **obj} for i, obj in enumerate(objects, 1)]}
        (folder/"risultato.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        title = (f"{photo.name}: {len(objects)} etichette, attesi {expected} - YOLO {proposal['conteggio']}, "
                 f"aggiunte {len(extra.get(photo.name, []))}, tolte {len(remove.get(photo.name, []))}")
        annota(image, objects, title, folder/"etichette.png")
        print(json.dumps({"foto": photo.name, "etichette": len(objects), "attesi": expected,
                          "ellissi": sum(o["contorno"] == "ellisse" for o in objects)}, ensure_ascii=False), flush=True)
        if len(objects) != expected:
            errors.append(f"{photo.name}: {len(objects)} etichette ma {expected} contenitori attesi")
    if errors:
        raise SystemExit("\n".join(errors))


if __name__ == "__main__":
    main()
