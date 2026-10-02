"""Confronto esplicito su foto: invia immagini a OpenRouter, nessuna procedura RFID."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

from PIL import Image, ImageDraw

from app.openrouter_vision import OpenRouterVision, PROMPT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cartella",type=Path)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--modello",default="qwen/qwen3.8-27b")
    parser.add_argument("--provider",help="provider esplicito senza fallback")
    parser.add_argument("--senza-thinking",action="store_true",help="richiede disattivazione del thinking ai modelli che lo supportano")
    parser.add_argument("--ripetizioni",type=int,default=1,choices=range(1,4))
    parser.add_argument("--riprendi",action="store_true")
    parser.add_argument("--solo",nargs="+",help="nomi delle foto da includere")
    args=parser.parse_args()
    photos=sorted(p for p in args.cartella.iterdir() if p.suffix.lower() in (".png",".jpg",".jpeg"))
    if args.solo:
        photos=[p for p in photos if p.name in args.solo]
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/"prompt.txt").write_text(PROMPT,encoding="utf-8")
    engine=OpenRouterVision(args.modello,reasoning=args.senza_thinking,provider=args.provider)
    rows=[]
    for repeat in range(1,args.ripetizioni+1):
        for path in photos:
            destination=args.output/f"{path.stem}-prova{repeat}"
            destination.mkdir(exist_ok=True)
            result_path=destination/"risultato.json"
            if args.riprendi and result_path.exists():
                result=json.loads(result_path.read_text(encoding="utf-8"))
            else:
                image=Image.open(path).convert("RGB")
                try:
                    result=engine.analizza(image)
                except (ValueError,RuntimeError, OSError) as exc:
                    result={"errore":str(exc),"modello_richiesto":args.modello,"conteggio":None}
                result.update(foto=path.name,ripetizione=repeat)
                result_path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
                if "campioni" in result:
                    drawing=ImageDraw.Draw(image)
                    for index,item in enumerate(result["campioni"],1):
                        x,y=item["centro"][0]*image.width,item["centro"][1]*image.height
                        drawing.ellipse((x-13,y-13,x+13,y+13),fill="#234e71",outline="#00c9fc",width=2)
                        drawing.text((x-4,y-7),str(index),fill="white")
                    image.save(destination/"centri.png")
            row={k:result[k] for k in ("foto","ripetizione","conteggio","tempo_secondi","incerto","provider","uso","errore") if k in result}
            rows.append(row)
            (args.output/"report.json").write_text(json.dumps({"modello":args.modello,"prove":rows},ensure_ascii=False,indent=2),encoding="utf-8")
            print(json.dumps(row,ensure_ascii=False),flush=True)
            if result.get("errore"):
                raise SystemExit(2)


if __name__=="__main__":
    main()
