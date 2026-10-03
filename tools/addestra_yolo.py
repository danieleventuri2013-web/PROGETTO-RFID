"""Addestramento pilota YOLO sui contenitori, con validazione leave-one-out.

Le etichette non si disegnano da zero: si partono dai contorni del
conteggio automatico SAM 2 già salvati (``risultato.json``) e si
aggiungono a mano, in un file JSON, i soli riquadri che SAM ha perso.
Una classe sola, ``contenitore``.

``--loo`` addestra N volte su N-1 foto e conta sulla foto esclusa: è la
sola stima onesta con poche immagini, perché il modello non ha mai visto
la foto su cui viene misurato. ``--finale`` addestra su tutte e salva i
pesi in ``models/yolo``; la sua accuratezza è quella stimata dal LOO, non
quella misurata sulle stesse foto dell'addestramento.

``--sintetiche N`` aggiunge a ogni addestramento N scene composte da
``tools/scene_sintetiche.py`` con i ritagli e gli sfondi delle sole foto di
addestramento di quel fold: la foto esclusa resta mai vista. ``--cartella``
separa i pesi di prove diverse; ``--robustezza`` conta la foto esclusa di
ogni fold anche ruotata e capovolta (8 varianti) per le cartelle indicate.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

from confronta_yolo import annota
from PIL import Image
from scene_sintetiche import genera

from app.yolo_engine import CONTENIMENTO, MODEL_DIR, YoloLocale

# Le otto combinazioni di rotazione e capovolgimento: una vista dall'alto non ha verso.
VARIANTI = {"originale": [], "ruotata 90": [Image.Transpose.ROTATE_90], "ruotata 180": [Image.Transpose.ROTATE_180],
            "ruotata 270": [Image.Transpose.ROTATE_270], "specchio": [Image.Transpose.FLIP_LEFT_RIGHT],
            "capovolta": [Image.Transpose.FLIP_TOP_BOTTOM], "trasposta": [Image.Transpose.TRANSPOSE],
            "trasversa": [Image.Transpose.TRANSVERSE]}


def riquadri(risultato, aggiunte):
    """Riquadri normalizzati [x1,y1,x2,y2]: contorni SAM più le aggiunte manuali."""
    data = json.loads(risultato.read_text(encoding="utf-8"))
    boxes = []
    for obj in data["oggetti"]:
        points = [p for poly in obj["contorni"] for p in poly]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        boxes.append([min(xs), min(ys), max(xs), max(ys)])
    width, height = data["dimensioni"]
    for x1, y1, x2, y2 in aggiunte:
        boxes.append([x1/width, y1/height, x2/width, y2/height])
    return boxes, (width, height)


def scrivi_dataset(destination, items):
    """Struttura YOLO: images/ e labels/ con una riga «0 cx cy w h» per contenitore."""
    if destination.exists():
        shutil.rmtree(destination)
    for split, rows in items.items():
        (destination/"images"/split).mkdir(parents=True)
        (destination/"labels"/split).mkdir(parents=True)
        for photo, boxes in rows:
            shutil.copy(photo, destination/"images"/split/photo.name)
            lines = [f"0 {(x1+x2)/2:.6f} {(y1+y2)/2:.6f} {x2-x1:.6f} {y2-y1:.6f}" for x1, y1, x2, y2 in boxes]
            (destination/"labels"/split/(photo.stem+".txt")).write_text("\n".join(lines)+"\n", encoding="utf-8")
    yaml = destination/"dati.yaml"
    yaml.write_text(f"path: {destination.as_posix()}\ntrain: images/train\nval: images/val\nnames:\n  0: contenitore\n", encoding="utf-8")
    return yaml


def stato_addestramento(project, name, epochs):
    """«nuovo», «parziale» o «completo», dalle epoche registrate in results.csv."""
    run = Path(project)/name
    if not (run/"weights"/"last.pt").is_file() or not (run/"results.csv").is_file():
        return "nuovo"
    rows = [r for r in (run/"results.csv").read_text(encoding="utf-8").splitlines()[1:] if r.strip()]
    done = int(float(rows[-1].split(",")[0])) if rows else 0
    return "completo" if done >= epochs else "parziale"


def addestra(yaml, base, epochs, project, name, riprendi=False):
    from ultralytics import YOLO

    state = stato_addestramento(project, name, epochs) if riprendi else "nuovo"
    last = Path(project).resolve()/name/"weights"/"last.pt"
    if state == "completo":
        return last
    if state == "parziale":
        # Un addestramento interrotto riparte dall'ultima epoca salvata, stessi argomenti.
        model = YOLO(str(last))
        model.train(resume=True)
        return Path(model.trainer.save_dir)/"weights"/"last.pt"
    model = YOLO(str(MODEL_DIR/base))
    # Vista dall'alto: capovolgimenti in entrambi i sensi sono scene plausibili.
    # Percorso assoluto: un project relativo finirebbe sotto runs/detect/.
    model.train(data=str(yaml), epochs=epochs, imgsz=640, batch=4, device="cpu", workers=0,
                project=str(Path(project).resolve()), name=name, exist_ok=True, flipud=.5, fliplr=.5, degrees=10,
                patience=0, plots=False, verbose=False, seed=0, deterministic=True)
    weights = Path(model.trainer.save_dir)/"weights"/"last.pt"
    if not weights.is_file():
        raise SystemExit(f"pesi addestrati non trovati: {weights}")
    return weights


def robustezza(labelled, attesi, output, confidenza, cartelle):
    """Foto esclusa di ogni fold in 8 orientamenti, per ciascuna cartella di pesi LOO."""
    report = {"confidenza": confidenza, "varianti": list(VARIANTI), "cartelle": {}}
    for folder in cartelle:
        rows = []
        for (photo, _boxes), expected in zip(labelled, attesi, strict=True):
            weights = output/folder/photo.stem/"weights"/"last.pt"
            engines = {"annidati": YoloLocale(weights, confidenza=confidenza),
                       "grezzo": YoloLocale(weights, confidenza=confidenza, contenimento=None)}
            original = Image.open(photo).convert("RGB")
            for name, ops in VARIANTI.items():
                image = original
                for op in ops:
                    image = image.transpose(op)
                row = {"foto_esclusa": photo.name, "variante": name, "atteso": expected}
                for label, engine in engines.items():
                    result = engine.analizza_automatico(image)
                    row[label] = {"conteggio": result["conteggio"], "incerto": result["incerto"],
                                  "confidenza_minima": min((o["confidenza"] for o in result["oggetti"]), default=None)}
                rows.append(row)
        summary = {label: {"corrette": sum(r[label]["conteggio"] == r["atteso"] for r in rows), "prove": len(rows),
                           "errore_assoluto_medio": round(statistics.mean(abs(r[label]["conteggio"]-r["atteso"]) for r in rows), 3),
                           "errate_non_segnalate": sum(r[label]["conteggio"] != r["atteso"] and not r[label]["incerto"] for r in rows),
                           "corrette_marcate_incerte": sum(r[label]["conteggio"] == r["atteso"] and r[label]["incerto"] for r in rows)}
                   for label in ("grezzo", "annidati")}
        report["cartelle"][folder] = {"sintesi": summary, "prove": rows}
        print(folder, json.dumps(summary, ensure_ascii=False), flush=True)
    (output/"robustezza.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def rivaluta(labelled, attesi, output, confidenza):
    """Pesi LOO già addestrati: conteggio sulla foto esclusa senza e con la regola dei riquadri annidati."""
    rows = []
    for (photo, _boxes), expected in zip(labelled, attesi, strict=True):
        weights = output/"addestramenti"/photo.stem/"weights"/"last.pt"
        image = Image.open(photo).convert("RGB")
        row = {"foto_esclusa": photo.name, "atteso": expected}
        for label, rule in (("grezzo", None), ("annidati", CONTENIMENTO)):
            engine = YoloLocale(weights, confidenza=confidenza, contenimento=rule)
            runs = [engine.analizza_automatico(image) for _ in range(3)]
            row[label] = {"conteggio": runs[0]["conteggio"], "corretto": runs[0]["conteggio"] == expected,
                          "incerto": runs[0]["incerto"], "avvisi": runs[0]["avvisi"],
                          "tempo_medio_secondi": round(statistics.mean(r["tempo_secondi"] for r in runs), 3)}
            if rule is not None:
                annota(image, runs[0], output/f"loo-regola-{photo.stem}.png")
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    summary = {label: {"corrette": sum(r[label]["corretto"] for r in rows), "prove": len(rows),
                       "conteggi": [r[label]["conteggio"] for r in rows],
                       "tempo_medio_secondi": round(statistics.mean(r[label]["tempo_medio_secondi"] for r in rows), 3)}
               for label in ("grezzo", "annidati")}
    (output/"valutazione.json").write_text(json.dumps({"confidenza": confidenza, "contenimento": CONTENIMENTO,
                                                       "sintesi": summary, "foto": rows}, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foto", type=Path, nargs="+")
    parser.add_argument("--sam", type=Path, required=True, help="cartella con <foto>/risultato.json del conteggio SAM")
    parser.add_argument("--aggiunte", type=Path, help="JSON {nome_foto: [[x1,y1,x2,y2] px, ...]} dei riquadri mancanti")
    parser.add_argument("--attesi", type=int, nargs="+", required=True)
    parser.add_argument("--base", default="yolo11n.pt")
    parser.add_argument("--epoche", type=int, default=60)
    parser.add_argument("--confidenza", type=float, default=.25)
    parser.add_argument("--loo", action="store_true")
    parser.add_argument("--finale", help="nome dei pesi da salvare in models/yolo, addestrati su tutte le foto")
    parser.add_argument("--sintetiche", type=int, default=0, help="scene sintetiche aggiunte a ogni addestramento")
    parser.add_argument("--riprendi", action="store_true",
                        help="salta gli addestramenti completati e riprende quelli interrotti, senza rigenerare i loro dataset")
    parser.add_argument("--cartella", default="addestramenti", help="sottocartella di --output per i pesi di questa prova")
    parser.add_argument("--robustezza", nargs="+", metavar="CARTELLA",
                        help="non addestra: conta le foto escluse in 8 orientamenti con i pesi LOO di queste cartelle")
    parser.add_argument("--solo-valutazione", action="store_true",
                        help="non addestra: rivaluta i pesi LOO già presenti in --output, senza e con la regola dei riquadri annidati")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.attesi) != len(args.foto):
        parser.error("un conteggio atteso per ogni foto")
    extra = json.loads(args.aggiunte.read_text(encoding="utf-8")) if args.aggiunte else {}
    labelled = []
    for photo, expected in zip(args.foto, args.attesi, strict=True):
        boxes, size = riquadri(args.sam/photo.stem/"risultato.json", extra.get(photo.name, []))
        if Image.open(photo).size != size:
            raise SystemExit(f"{photo.name}: dimensioni diverse dal risultato SAM")
        if len(boxes) != expected:
            raise SystemExit(f"{photo.name}: {len(boxes)} etichette ma {expected} contenitori attesi")
        labelled.append((photo, boxes))
    args.output.mkdir(parents=True, exist_ok=True)
    if args.robustezza:
        return robustezza(labelled, args.attesi, args.output, args.confidenza, args.robustezza)
    if args.solo_valutazione:
        return rivaluta(labelled, args.attesi, args.output, args.confidenza)
    fonti = {photo.name: (photo, args.sam/photo.stem/"risultato.json", extra.get(photo.name, [])) for photo, _b in labelled}
    suffix = "" if args.cartella == "addestramenti" else "-"+args.cartella
    report = {"base": args.base, "epoche": args.epoche, "confidenza": args.confidenza, "sintetiche": args.sintetiche,
              "etichette": {p.name: len(b) for p, b in labelled}, "loo": []}
    if args.loo:
        for i, (photo, _boxes) in enumerate(labelled):
            train = [item for j, item in enumerate(labelled) if j != i]
            dataset = args.output/("dataset"+suffix)/photo.stem
            resumed = args.riprendi and stato_addestramento(args.output/args.cartella, photo.stem, args.epoche) != "nuovo"
            if resumed:
                yaml = dataset/"dati.yaml"
            else:
                yaml = scrivi_dataset(dataset, {"train": train, "val": [labelled[i]]})
                if args.sintetiche:
                    # Solo ritagli e sfondi delle foto di addestramento di questo fold.
                    genera([fonti[p.name] for p, _b in train], yaml.parent, args.sintetiche, seed=i, split="train")
            started = time.perf_counter()
            weights = addestra(yaml, args.base, args.epoche, args.output/args.cartella, photo.stem, args.riprendi)
            duration = time.perf_counter()-started
            engine = YoloLocale(weights, confidenza=args.confidenza)
            result = engine.analizza_automatico(Image.open(photo).convert("RGB"))
            row = {"foto_esclusa": photo.name, "atteso": args.attesi[i], "conteggio": result["conteggio"],
                   "corretto": result["conteggio"] == args.attesi[i], "tempo_secondi": result["tempo_secondi"],
                   "addestramento_secondi": round(duration, 1), "ripreso": resumed,
                   "confidenze": [o["confidenza"] for o in result["oggetti"]]}
            report["loo"].append(row)
            annota(Image.open(photo).convert("RGB"), result, args.output/f"loo{suffix}-{photo.stem}.png")
            print(json.dumps(row, ensure_ascii=False), flush=True)
        rows = report["loo"]
        report["loo_sintesi"] = {"corrette": sum(r["corretto"] for r in rows), "prove": len(rows),
                                 "errore_assoluto_medio": statistics.mean(abs(r["conteggio"]-r["atteso"]) for r in rows),
                                 "tempo_medio_secondi": statistics.mean(r["tempo_secondi"] for r in rows)}
    if args.finale:
        dataset = args.output/("dataset"+suffix)/"tutte"
        if args.riprendi and stato_addestramento(args.output/args.cartella, "finale", args.epoche) != "nuovo":
            yaml = dataset/"dati.yaml"
        else:
            yaml = scrivi_dataset(dataset, {"train": labelled, "val": labelled})
            if args.sintetiche:
                genera(list(fonti.values()), yaml.parent, args.sintetiche, seed=100, split="train")
        weights = addestra(yaml, args.base, args.epoche, args.output/args.cartella, "finale", args.riprendi)
        target = MODEL_DIR/args.finale
        shutil.copy(weights, target)
        report["finale"] = str(target)
        print(f"Pesi finali: {target}", flush=True)
    (args.output/f"report{suffix}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "loo"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
