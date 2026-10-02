"""Collaudo da foto dello stesso motore usato dallo scatto automatico."""
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

from PIL import Image, ImageDraw

from app.sam2_automatico import analizza_automatico
from app.sam2_engine import Sam2Locale
from app.sam2_geometry import rettifica
from lims.vision import immagine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foto", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--angoli", type=float, nargs=8, help="quattro angoli relativi, in senso orario")
    parser.add_argument("--openvino", choices=("GPU", "CPU"), help="prova separata con il modello OpenVINO locale")
    parser.add_argument("--precisione", choices=("f32", "f16"), default="f32")
    parser.add_argument("--codifica", choices=("torch", "openvino"), default="openvino")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    image = Image.open(args.foto).convert("RGB")
    geometry = None
    if args.angoli:
        data = rettifica(image, {"punti": [args.angoli[i:i+2] for i in range(0, 8, 2)]})
        image = immagine(data["immagine_base64"])
        geometry = data["correzione"]
    started = time.perf_counter()
    if args.openvino:
        from app.sam2_openvino import Sam2OpenVino
        engine = Sam2OpenVino(args.openvino, precision=args.precisione, encoding=args.codifica)
        engine.prepara()
    else:
        engine = Sam2Locale()
    startup = time.perf_counter()-started
    result = analizza_automatico(engine, image)
    result["avvio_secondi"] = round(startup, 2)
    if args.openvino:
        result["esecuzione_openvino"] = engine.dispositivi_esecuzione()
        result["tempi_inferenza"] = engine.timings
    result.update(foto=args.foto.name, correzione=geometry)
    (args.output/"risultato.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    drawing = ImageDraw.Draw(image)
    outlines = [(result["borsa"].get("contorno", []), "#00d6ed")]
    outlines += [(p, "#22dd77") for obj in result["oggetti"] for p in obj["contorni"]]
    for poly, color in outlines:
        points = [(round(x*image.width), round(y*image.height)) for x, y in poly]
        if points:
            drawing.line(points+points[:1], fill=color, width=3)
    for obj in result["oggetti"]:
        x, y = obj["centro"][0]*image.width, obj["centro"][1]*image.height
        drawing.ellipse((x-12, y-12, x+12, y+12), fill="#143e2b")
        drawing.text((x-4, y-7), str(obj["id"]), fill="white")
    image.save(args.output/"annotazione.png")
    print(json.dumps({k:v for k,v in result.items() if k not in ("borsa", "oggetti")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
