"""Prova SAM 2 del contenitore esterno con inquadratura pressoché fissa."""
from __future__ import annotations

import argparse
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

from app.sam2_engine import Sam2Locale


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cartella", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--solo", default="")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    engine = Sam2Locale()
    for path in sorted(args.cartella.glob("*.png")):
        if args.solo and path.name != args.solo:
            continue
        image = Image.open(path).convert("RGB")
        width, height = image.size
        started = time.perf_counter()
        base = engine.processor(images=image, return_tensors="pt")
        with engine.torch.inference_mode():
            embeddings = engine.model.get_image_embeddings(base["pixel_values"])
        box = [[[width*.005, height*.005, width*.995, height*.995]]]
        points = [[[[width*x, height*y] for x, y in [( .5, .12), (.92, .5), (.5, .88), (.06, .5)]]]]
        prompts = {"box": {"input_boxes": box}, "pareti": {"input_points": points, "input_labels": [[[1, 1, 1, 1]]]},
                   "pareti_box": {"input_boxes": box, "input_points": points, "input_labels": [[[1, 1, 1, 1]]]}}
        masks, rows = [], []
        gallery = Image.new("RGB", (width*3, (height+30)*3), "white")
        for j, (name, prompt) in enumerate(prompts.items()):
            inputs = engine.processor(images=image, **prompt, return_tensors="pt")
            inputs.pop("pixel_values")
            with engine.torch.inference_mode():
                output = engine.model(**inputs, image_embeddings=embeddings, multimask_output=True)
                variants = engine.processor.post_process_masks(output.pred_masks, inputs["original_sizes"])[0][0]
            for i, mask in enumerate(variants):
                mask = mask.cpu().numpy().astype(np.uint8)
                masks.append(mask)
                contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                cell = image.copy()
                drawing = ImageDraw.Draw(cell)
                for contour in contours:
                    p = [tuple(map(int, v[0])) for v in cv2.approxPolyDP(contour, 2, True)]
                    if len(p) > 2:
                        drawing.line(p+p[:1], fill="lime", width=3)
                row = {"prompt": name, "variante": i, "area_percento": round(float(mask.mean())*100, 2),
                       "score": round(float(output.iou_scores[0, 0, i]), 3)}
                rows.append(row)
                gallery.paste(cell, (i*width, j*(height+30)))
                ImageDraw.Draw(gallery).text((i*width+8, j*(height+30)+8), f"{name}/{i}: area {row['area_percento']}% · score {row['score']}", fill="red")
        np.savez_compressed(args.output/f"{path.stem}_varianti.npz", masks=np.asarray(masks))
        gallery.thumbnail((1600, 1500))
        gallery.save(args.output/f"{path.stem}_varianti.png")
        (args.output/f"{path.stem}_varianti.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(path.name, round(time.perf_counter()-started, 2), json.dumps(rows), flush=True)


if __name__ == "__main__":
    main()
