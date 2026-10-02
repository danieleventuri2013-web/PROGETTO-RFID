"""Conteggio sperimentale offline da foto: Hough e SAM 2 automatico, senza clic.

Le maschere SAM vengono filtrate per geometria circolare; non è un detector
addestrato dei contenitori. Salva immagini annotate e dati per verifica visiva.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cv2
import numpy as np
from PIL import Image, ImageDraw

from app.sam2_count import candidati_circolari, crea_generatore
from app.sam2_engine import Sam2Locale
from lims import vision


def disegna_sam(image, masks, selected, title, target):
    base = image.convert("RGBA")
    tint = np.zeros((image.height, image.width, 4), dtype=np.uint8)
    for item in selected:
        tint[np.asarray(masks[item["mask_index"]], dtype=bool)] = [25, 225, 100, 55]
    base = Image.alpha_composite(base, Image.fromarray(tint))
    drawing = ImageDraw.Draw(base)
    for i, item in enumerate(selected, 1):
        contours, _ = cv2.findContours(np.asarray(masks[item["mask_index"]], dtype=np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            points = [tuple(map(int, p[0])) for p in cv2.approxPolyDP(contour, 1, True)]
            if len(points) > 2:
                drawing.line(points + points[:1], fill="#19e164", width=3)
        x, y = item["centro"]
        drawing.ellipse((x-13, y-13, x+13, y+13), fill="#173e2e")
        drawing.text((x-5, y-8), str(i), fill="white")
    drawing.rectangle((0, 0, image.width, 28), fill="#173e2e")
    drawing.text((10, 8), title, fill="white")
    base.convert("RGB").save(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cartella", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--griglia", type=int, default=32, choices=[16, 24, 32])
    parser.add_argument("--solo", default="")
    parser.add_argument("--riprendi", action="store_true", help="riusa gli esiti più recenti delle foto, con la stessa griglia")
    parser.add_argument("--roi", type=float, nargs=4, metavar=("X1", "Y1", "X2", "Y2"),
                        help="finestra relativa di ricerca uguale per tutte le foto, senza indicare i campioni")
    args = parser.parse_args()
    if args.roi and not (0 <= args.roi[0] < args.roi[2] <= 1 and 0 <= args.roi[1] < args.roi[3] <= 1):
        parser.error("finestra ROI non valida")
    args.output.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in args.cartella.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg") and (not args.solo or p.name == args.solo))
    print(f"Foto: {len(files)}. SAM 2 Tiny CPU, griglia {args.griglia}×{args.griglia}, nessun clic manuale.", flush=True)
    engine = Sam2Locale()
    generator = crea_generatore(engine)
    report = []
    for index, path in enumerate(files, 1):
        cached = args.output / f"{path.stem}_risultato.json"
        if args.riprendi and cached.is_file() and cached.stat().st_mtime >= path.stat().st_mtime:
            item = json.loads(cached.read_text(encoding="utf-8"))
            if item.get("griglia") == args.griglia and item.get("roi") == args.roi and item.get("coordinate_sam2_quadrate"):
                report.append(item)
                print(f"[{index}/{len(files)}] {path.name}: esito già disponibile, {item['sam_conteggio']} candidati", flush=True)
                continue
        print(f"[{index}/{len(files)}] {path.name}: inizio…", flush=True)
        with Image.open(path) as source:
            image = source.convert("RGB")
        if image.width * image.height > 1920 * 1080:
            image.thumbnail((1920, 1080))
        before = time.perf_counter()
        hough = vision.analizza(image, {"raggio_min": .012, "raggio_max": .16, "sensibilita": 30})
        crop_box = tuple(round(v * (image.width if i % 2 == 0 else image.height)) for i, v in enumerate(args.roi)) if args.roi else (0, 0, image.width, image.height)
        search_image = image.crop(crop_box)
        output = generator(search_image, points_per_batch=8, points_per_crop=args.griglia,
                           pred_iou_thresh=.75, stability_score_thresh=.90, crops_n_layers=0,
                           output_bboxes_mask=True)
        masks, scores = output["masks"], output["scores"]
        if args.roi:
            full_masks = []
            for mask in masks:
                full = np.zeros((image.height, image.width), dtype=np.uint8)
                full[crop_box[1]:crop_box[3], crop_box[0]:crop_box[2]] = np.asarray(mask, dtype=np.uint8)
                full_masks.append(full)
            masks = full_masks
        # Conserva gli esiti grezzi: filtri/verifica possono essere ripetuti senza inferenza.
        np.savez_compressed(args.output / f"{path.stem}_maschere.npz", masks=np.asarray(masks, dtype=np.uint8), scores=np.asarray(scores))
        selected, circular_candidates = candidati_circolari(masks, scores, image.size)
        elapsed = round(time.perf_counter() - before, 2)
        annotation = f"{path.stem}_sam2.png"
        disegna_sam(image, masks, selected, f"SAM 2 automatico: {len(selected)} candidati - {path.name}", args.output / annotation)
        item = {"foto": path.name, "dimensioni": list(image.size), "hough": len(hough["cerchi"]) if hough["area"] else None,
                "bordo_hough": hough["origine_area"], "sam_regioni": len(masks), "sam_candidati_circolari": circular_candidates,
                "sam_conteggio": len(selected), "sam_dettagli": selected, "tempo_secondi": elapsed,
                "griglia": args.griglia, "annotazione": annotation,
                "roi": args.roi,
                "coordinate_sam2_quadrate": True,
                "metodo": "SAM 2 automatico + filtri geometrici e rimozione duplicati; nessun numero atteso o clic fornito"}
        report.append(item)
        (args.output / f"{path.stem}_risultato.json").write_text(json.dumps(item, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({k: v for k, v in item.items() if k != "sam_dettagli"}, ensure_ascii=False), flush=True)
    (args.output / "risultati.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    cards = "".join(f'<section><h2>{html.escape(r["foto"])}: {r["sam_conteggio"]} candidati SAM</h2><p>Regioni grezze: {r["sam_regioni"]}; Hough: {r["hough"] if r["hough"] is not None else "bordo non determinabile"}; tempo {r["tempo_secondi"]} s.</p><img src="{html.escape(r["annotazione"])}"></section>' for r in report)
    page = '<!doctype html><html lang="it"><meta charset="utf-8"><title>Conteggio foto RFID</title><style>body{font:16px system-ui;max-width:1050px;margin:30px auto;background:#f4f5f0;color:#20342e}img{max-width:100%}section{margin:30px 0}p{line-height:1.5}</style><h1>Conteggio sperimentale con SAM 2 automatico</h1><p>Nessun clic o numero atteso. Le regioni circolari sono candidate a contenitori: verificare le immagini. Non è un detector addestrato.</p>'+cards+'</html>'
    (args.output / "report.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
