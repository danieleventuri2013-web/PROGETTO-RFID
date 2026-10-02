"""Collaudo ripetibile delle foto con un solo avvio del motore OpenVINO."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

from PIL import Image, ImageDraw

from app.sam2_automatico import analizza_automatico
from app.sam2_openvino import Sam2OpenVino


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foto", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dispositivo", choices=("GPU", "CPU"), default="GPU")
    parser.add_argument("--precisione", choices=("f16", "f32"), default="f32")
    parser.add_argument("--attesi", type=int, nargs="+")
    args = parser.parse_args()
    if args.attesi is not None and len(args.attesi) != len(args.foto):
        parser.error("un conteggio atteso per ogni foto")
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    engine = Sam2OpenVino(args.dispositivo, precision=args.precisione)
    engine.prepara()
    startup = time.perf_counter()-started
    print(f"Motore pronto in {startup:.2f} s: {engine.dispositivi_esecuzione()}", flush=True)
    rows=[]
    for index, path in enumerate(args.foto):
        image=Image.open(path).convert("RGB")
        result=analizza_automatico(engine, image)
        result.update(foto=path.name, esecuzione_openvino=engine.dispositivi_esecuzione())
        if args.attesi is not None:
            result.update(atteso=args.attesi[index], corretto=result["conteggio"]==args.attesi[index])
        destination=args.output/path.stem
        destination.mkdir(exist_ok=True)
        (destination/"risultato.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        drawing=ImageDraw.Draw(image)
        outlines=[(result["borsa"].get("contorno",[]),"#00d6ed")]
        outlines += [(p,"#22dd77") for obj in result["oggetti"] for p in obj["contorni"]]
        for poly,color in outlines:
            points=[(round(x*image.width),round(y*image.height)) for x,y in poly]
            if points:
                drawing.line(points+points[:1],fill=color,width=3)
        for obj in result["oggetti"]:
            x,y=obj["centro"][0]*image.width,obj["centro"][1]*image.height
            drawing.ellipse((x-12,y-12,x+12,y+12),fill="#143e2b")
            drawing.text((x-4,y-7),str(obj["id"]),fill="white")
        image.save(destination/"annotazione.png")
        row={k:result[k] for k in ("foto","conteggio","tempo_secondi","tempo_bordo_secondi","tempo_conteggio_secondi") if k in result}
        if args.attesi is not None:
            row.update(atteso=result["atteso"],corretto=result["corretto"])
        rows.append(row)
        print(json.dumps(row,ensure_ascii=False),flush=True)
        report={"modello":engine.description,"avvio_secondi":round(startup,2),
                "dispositivi":engine.dispositivi_esecuzione(),"prove":rows}
        (args.output/"tempi.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if args.attesi is not None and not all(row["corretto"] for row in rows):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
