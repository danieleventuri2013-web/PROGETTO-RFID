"""Analizza il perimetro esterno nelle foto con lo stesso profilo SAM 2."""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import cv2
import numpy as np
from PIL import Image, ImageDraw

from app.sam2_borsa import rileva_borsa
from app.sam2_engine import Sam2Locale


def annota(image, mask, result, counts_dir, stem, target):
    drawing = ImageDraw.Draw(image)
    count = None
    contained = []
    if counts_dir and (counts_dir/f"{stem}_risultato.json").is_file():
        prior = json.loads((counts_dir/f"{stem}_risultato.json").read_text(encoding="utf-8"))
        if prior["dimensioni"] != list(image.size):
            raise ValueError("dimensioni del conteggio precedente incompatibili")
        with np.load(counts_dir/f"{stem}_maschere.npz") as data:
            for i, item in enumerate(prior["sam_dettagli"], 1):
                sample = data["masks"][item["mask_index"]].astype(np.uint8)
                contours, _ = cv2.findContours(sample, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for contour in contours:
                    points = [tuple(map(int, p[0])) for p in cv2.approxPolyDP(contour, 1, True)]
                    if len(points) > 2:
                        drawing.line(points+points[:1], fill="#22dd77", width=3)
                x, y = item["centro"]
                drawing.ellipse((x-12, y-12, x+12, y+12), fill="#12442b")
                drawing.text((x-4, y-7), str(i), fill="white")
                if mask is not None:
                    contained.append(round(float(np.logical_and(sample, mask).sum()/max(1, sample.sum())), 4))
        count = prior["sam_conteggio"]
    if mask is not None:
        width, height = image.size
        margin = result["margine_limite_pixel"]
        points = [(round(x*width), round(y*height)) for x, y in result["contorno"]]
        # I segmenti a contatto con il ritaglio sono arancioni, non bordi completi.
        for a, b in zip(points, points[1:]+points[:1], strict=True):
            edge = ((a[0] < margin and b[0] < margin) or (a[0] >= width-margin and b[0] >= width-margin)
                    or (a[1] < margin and b[1] < margin) or (a[1] >= height-margin and b[1] >= height-margin))
            drawing.line((a, b), fill="#ffac33" if edge else "#00d6ed", width=4)
    canvas = Image.new("RGB", (image.width, image.height+54), "#102e39")
    canvas.paste(image, (0, 54))
    caption = ImageDraw.Draw(canvas)
    caption.text((10, 9), f"{stem} | bordo borsa: azzurro | limite foto: arancione", fill="white")
    caption.text((10, 30), f"Campioni verdi: {count if count is not None else 'conteggio non fornito'} | SAM con riferimenti fissi sulle pareti", fill="white")
    canvas.save(target)
    result.update(conteggio_precedente=count, frazioni_campioni_nella_borsa=contained,
                  nota_conteggio="contorni del conteggio precedente; la ricerca dei campioni non viene rieseguita")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cartella", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--conteggi", type=Path, help="esiti precedenti da mostrare insieme al perimetro")
    args = parser.parse_args()
    files = sorted(p for p in args.cartella.iterdir() if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"))
    args.output.mkdir(parents=True, exist_ok=True)
    engine = Sam2Locale()
    results = []
    for path in files:
        with Image.open(path) as source:
            image = source.convert("RGB")
        if image.width*image.height > 1920*1080:
            raise ValueError(f"{path.name}: ridurre la foto a massimo Full HD prima della prova")
        mask, result = rileva_borsa(engine, image)
        result.update(foto=path.name, annotazione=f"{path.stem}_borsa.png")
        annota(image, mask, result, args.conteggi, path.stem, args.output/result["annotazione"])
        if mask is not None:
            Image.fromarray(mask*255).save(args.output/f"{path.stem}_borsa_maschera.png")
        (args.output/f"{path.stem}_borsa.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(result)
        print(path.name, result["rilevata"], result.get("lati_al_limite"), result["tempo_secondi"], flush=True)
    (args.output/"risultati.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    cards = []
    for r in results:
        sides = ", ".join(r.get("lati_al_limite", [])) or "nessuno"
        cards.append(f'<article><h2>{html.escape(r["foto"])}</h2><p>Borsa: {"rilevata" if r["rilevata"] else "non determinabile"}. Lati al limite: {sides}. Analisi: {r["tempo_secondi"]} s.</p><img src="{html.escape(r["annotazione"])}" alt="Contorno borsa e campioni riconosciuti"></article>')
    page = '''<!doctype html><html lang="it"><meta charset="utf-8"><title>Bordo esterno della borsa · SAM 2</title>
<style>body{font:16px system-ui;color:#19333c;background:#edf3f4;max-width:1050px;margin:30px auto;padding:0 18px;line-height:1.5}article{background:white;border-radius:12px;padding:18px;margin:26px 0}img{width:100%;height:auto}strong{color:#096578}</style>
<h1>Perimetro della borsa/scatola esterna</h1><p><strong>Azzurro: contorno stimato. Verde: campioni già riconosciuti. Arancione: tratto al limite della foto.</strong></p>
<p>SAM 2.1 Tiny locale, con gli stessi quattro riferimenti sulle pareti in tutte le foto. Non occorre segnare il bordo per ogni foto di questa inquadratura. Il profilo è stato scelto per questa posizione: se cambia la borsa o si sposta la camera va ricalibrato. Non è un riconoscimento semantico universale.</p>
<p>La sagoma segue il corpo trasparente del contenitore; maniglie, agganci, trasparenza e bordo rosso possono alterarne alcuni tratti. Il punteggio SAM non misura l'errore reale del bordo. I lati fuori inquadratura non vengono ricostruiti. Per verificare tutto il perimetro occorre lasciare margine attorno alla borsa.</p>
<p>I contorni verdi provengono dalla precedente prova di conteggio: la ROI di quella prova resta invariata. Questa analisi non sostituisce ancora la ROI né è collegata alla procedura RFID o al video.</p>'''+"".join(cards)+"</html>"
    (args.output/"report.html").write_text(page, encoding="utf-8")


if __name__ == "__main__":
    main()
