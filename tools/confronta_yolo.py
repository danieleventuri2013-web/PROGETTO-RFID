"""Confronto ripetibile YOLO sulle foto da file, accanto alle misure SAM 2 / Qwen.

Ogni configurazione YOLO viene caricata una volta (avvio escluso dai tempi),
poi ogni foto è analizzata ``--ripetizioni`` volte con lo stesso motore del
servizio (``YoloLocale.analizza_automatico``). Al modello non si fornisce il
numero atteso: serve solo a calcolare l'esito. SAM 2 e Qwen non vengono
rieseguiti: si riportano le misure già salvate in ``demo-output`` (stesse sei
foto), dichiarandone la data.
"""
from __future__ import annotations

import argparse
import html
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

from PIL import Image, ImageDraw

from app.yolo_engine import MODEL_DIR, YoloLocale

ROOT = Path(__file__).resolve().parents[1]


def esporta_openvino(pesi):
    """Esporta una volta i pesi .pt nella cartella *_openvino_model (FP16)."""
    from ultralytics import YOLO

    path = MODEL_DIR/pesi
    target = path.with_name(path.stem+"_openvino_model")
    if not target.is_dir():
        YOLO(str(path)).export(format="openvino", half=True, imgsz=640)
    return target.name


def annota(image, result, destination):
    image = image.copy()
    drawing = ImageDraw.Draw(image)
    for obj in result["oggetti"]:
        for poly in obj["contorni"]:
            points = [(round(x*image.width), round(y*image.height)) for x, y in poly]
            drawing.line(points+points[:1], fill="#22dd77", width=3)
        x, y = obj["centro"][0]*image.width, obj["centro"][1]*image.height
        drawing.ellipse((x-12, y-12, x+12, y+12), fill="#143e2b")
        drawing.text((x-4, y-7), str(obj["id"]), fill="white")
        x0 = min(p[0] for p in obj["contorni"][0])*image.width
        y0 = min(p[1] for p in obj["contorni"][0])*image.height
        drawing.text((x0+3, y0+2), f'{obj["classe"]} {obj["confidenza"]:.2f}', fill="#ffe14d")
    image.save(destination)


def misure_precedenti():
    """Risultati SAM 2 GPU e Qwen del 2 ottobre, se presenti: niente nuove richieste."""
    rows = []
    sam = ROOT/"demo-output"/"openvino-20261002"/"gpu-corretto"/"tempi.json"
    if sam.is_file():
        data = json.loads(sam.read_text(encoding="utf-8"))
        prove = data["prove"]
        rows.append({"modello": "SAM 2.1 Tiny · OpenVINO GPU F16 (misura 2/10/2026)",
                     "corrette": sum(p["corretto"] for p in prove), "prove": len(prove),
                     "media_secondi": statistics.mean(p["tempo_secondi"] for p in prove),
                     "conteggi": [p["conteggio"] for p in prove], "luogo": "locale"})
    qwen = ROOT/"demo-output"/"openrouter-20261002"/"confronto.json"
    if qwen.is_file():
        data = json.loads(qwen.read_text(encoding="utf-8"))
        for group in data["gruppi"]:
            if group["cartella"] == "qwen38":
                rows.append({"modello": "Qwen3.8 27B · OpenRouter DekaLLM (misura 2/10/2026)",
                             "corrette": group["corrette"], "prove": group["prove"],
                             "media_secondi": group["media_secondi"],
                             "conteggi": [r["conteggio"] for r in group["risultati"]], "luogo": "remoto"})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foto", type=Path, nargs="+")
    parser.add_argument("--attesi", type=int, nargs="+", required=True)
    parser.add_argument("--pesi", nargs="+", default=["yolov8n.pt", "yolo11n.pt"])
    parser.add_argument("--dispositivo", choices=("cpu", "intel:gpu", "intel:cpu"), default="cpu")
    parser.add_argument("--confidenza", type=float, default=.25)
    parser.add_argument("--prompt", nargs="*", default=None, help="solo YOLOE: oggetti da cercare")
    parser.add_argument("--ripetizioni", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.attesi) != len(args.foto):
        parser.error("un conteggio atteso per ogni foto")
    args.output.mkdir(parents=True, exist_ok=True)
    images = [Image.open(p).convert("RGB") for p in args.foto]
    groups = []
    for pesi in args.pesi:
        name = esporta_openvino(pesi) if args.dispositivo.startswith("intel:") else pesi
        started = time.perf_counter()
        engine = YoloLocale(name, dispositivo=args.dispositivo, confidenza=args.confidenza, prompt=args.prompt)
        startup = time.perf_counter()-started
        rows = []
        for path, image, expected in zip(args.foto, images, args.attesi, strict=True):
            runs = [engine.analizza_automatico(image) for _ in range(args.ripetizioni)]
            result = runs[0]
            folder = args.output/Path(name).stem/path.stem
            folder.mkdir(parents=True, exist_ok=True)
            (folder/"risultato.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            annota(image, result, folder/"annotazione.png")
            row = {"foto": path.name, "atteso": expected, "conteggio": result["conteggio"],
                   "corretto": result["conteggio"] == expected,
                   "stabile": len({r["conteggio"] for r in runs}) == 1,
                   "tempo_medio_secondi": round(statistics.mean(r["tempo_secondi"] for r in runs), 4),
                   "classi": result["classi_rilevate"], "avvisi": result["avvisi"],
                   "annotazione": f"{Path(name).stem}/{path.stem}/annotazione.png"}
            rows.append(row)
            print(engine.description, json.dumps(row, ensure_ascii=False), flush=True)
        groups.append({"modello": engine.description, "avvio_secondi": round(startup, 2),
                       "corrette": sum(r["corretto"] for r in rows), "prove": len(rows),
                       "media_secondi": statistics.mean(r["tempo_medio_secondi"] for r in rows),
                       "errore_assoluto_medio": statistics.mean(abs(r["conteggio"]-r["atteso"]) for r in rows),
                       "risultati": rows})
    previous = misure_precedenti()
    summary = {"foto": [p.name for p in args.foto], "attesi": args.attesi, "confidenza": args.confidenza,
               "dispositivo": args.dispositivo, "ripetizioni": args.ripetizioni, "yolo": groups, "precedenti": previous}
    (args.output/"report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    table = "".join(f'<tr><td>{html.escape(g["modello"])}</td><td>{g["corrette"]}/{g["prove"]}</td>'
                    f'<td>{", ".join(str(r["conteggio"]) for r in g["risultati"])}</td><td>{g["media_secondi"]*1000:.0f} ms</td>'
                    f'<td>{g["avvio_secondi"]:.1f} s</td></tr>' for g in groups)
    table += "".join(f'<tr><td>{html.escape(p["modello"])}</td><td>{p["corrette"]}/{p["prove"]}</td>'
                     f'<td>{", ".join(map(str, p["conteggi"]))}</td><td>{p["media_secondi"]:.2f} s</td><td>—</td></tr>' for p in previous)
    cards = "".join(f'<article><h3>{html.escape(g["modello"])} · {html.escape(r["foto"])}</h3>'
                    f'<p><strong>{r["conteggio"]}/{r["atteso"]}</strong> · {r["tempo_medio_secondi"]*1000:.0f} ms · '
                    f'{html.escape(json.dumps(r["classi"], ensure_ascii=False))}</p><img src="{r["annotazione"]}" alt=""></article>'
                    for g in groups for r in g["risultati"])
    page = (f'<!doctype html><html lang="it"><meta charset="utf-8"><title>Confronto YOLO</title><style>body{{font:16px system-ui;'
            f'max-width:1250px;margin:auto;padding:24px;background:#eef3f0;color:#23392d}}table{{width:100%;border-collapse:collapse;background:white}}'
            f'td,th{{padding:10px;text-align:left;border-bottom:1px solid #d7e2dc}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}}'
            f'article{{background:white;padding:12px;border-radius:10px}}img{{width:100%}}</style><h1>Conteggio campioni: YOLO e modelli precedenti</h1>'
            f'<p>Foto: {", ".join(summary["foto"])}; attesi {", ".join(map(str, args.attesi))}. YOLO su {args.dispositivo}, confidenza {args.confidenza}, '
            f'{args.ripetizioni} ripetizioni per foto, avvio escluso. SAM 2 e Qwen: misure salvate il 2 ottobre 2026, non rieseguite.</p>'
            f'<table><thead><tr><th>Modello</th><th>Corrette</th><th>Conteggi</th><th>Tempo medio per foto</th><th>Avvio</th></tr></thead>'
            f'<tbody>{table}</tbody></table><div class="grid">{cards}</div></html>')
    (args.output/"report.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
