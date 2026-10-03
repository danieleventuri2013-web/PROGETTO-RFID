"""Scene sintetiche per l'addestramento YOLO, generate in locale dalle foto reali.

Da poche foto etichettate si ricavano due ingredienti:

- i **ritagli** dei contenitori, dai contorni del conteggio automatico SAM
  (``risultato.json``); un riquadro aggiunto a mano diventa un'ellisse;
- gli **sfondi**: le stesse foto, con i loro contenitori e le loro etichette,
  più eventuali foto reali della scatola vuota.

Ogni scena aggiunge un numero casuale di ritagli negli spazi liberi del
fondo, con rotazione, scala e luminosità casuali, bordo sfumato e un'ombra
morbida. Le etichette nascono dal ritaglio incollato, quindi sono esatte.
A differenza delle varianti di una stessa foto (rotazioni, luminosità), qui
cambiano disposizione, quantità e vicinanze fra contenitori: proprio ciò
che sei foto non mostrano. Nulla lascia il PC.

I contenitori veri non vengono rimossi: provato e scartato, la ricostruzione
del fondo al loro posto lasciava dischi grigi dai bordi netti (ombre dei
barattoli inclinati, griglia e pareti della borsa) che il modello avrebbe
imparato come se fossero reali. Ogni pixel di fondo resta quindi una
fotografia; per scene con pochi contenitori servono foto della scatola vuota
(``--sfondi-vuoti``).

Il generatore non conosce le foto di prova: chi lo chiama passa solo le foto
di addestramento (vedi ``tools/addestra_yolo.py --sintetiche``).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

# Attorno a un contenitore vero il riquadro si allarga: comprende il corpo e
# l'ombra di un barattolo inclinato, che il contorno del coperchio lascia fuori.
MARGINE_REALI = .15
# Quota dell'impronta che deve cadere sul foglio chiaro del fondo.
COPERTURA_FONDO = .97
# Le pareti della borsa sono chiare quanto il foglio e riflettono i coperchi:
# i ritagli restano nella zona dei contenitori veri, allargata di questa quota.
MARGINE_ZONA = .08


def _rgb(path):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"foto non leggibile: {path}")
    return image  # BGR, come lo scrive cv2.imwrite


def carica_foto(photo, risultato, aggiunte=()):
    """Foto BGR, maschere dei contenitori (una per oggetto) e riquadri in pixel."""
    image = _rgb(photo)
    height, width = image.shape[:2]
    data = json.loads(Path(risultato).read_text(encoding="utf-8"))
    if data["dimensioni"] != [width, height]:
        raise ValueError(f"{Path(photo).name}: dimensioni diverse dal risultato SAM")
    masks = []
    for obj in data["oggetti"]:
        mask = np.zeros((height, width), np.uint8)
        for poly in obj["contorni"]:
            points = np.round(np.asarray(poly, np.float32)*[width, height]).astype(np.int32)
            cv2.fillPoly(mask, [points], 255)
        masks.append(mask)
    for x1, y1, x2, y2 in aggiunte:
        mask = np.zeros((height, width), np.uint8)
        cv2.ellipse(mask, (round((x1+x2)/2), round((y1+y2)/2)), (round((x2-x1)/2), round((y2-y1)/2)), 0, 0, 360, 255, -1)
        masks.append(mask)
    boxes = []
    for mask in masks:
        ys, xs = np.nonzero(mask)
        boxes.append((int(xs.min()), int(ys.min()), int(xs.max())+1, int(ys.max())+1))
    return image, masks, boxes


def ritagli(image, masks):
    """Ritagli BGRA dei contenitori: alpha sfumato di un paio di pixel."""
    patches = []
    for mask in masks:
        ys, xs = np.nonzero(mask)
        pad = 3
        x1, y1 = max(0, xs.min()-pad), max(0, ys.min()-pad)
        x2, y2 = min(image.shape[1], xs.max()+pad+1), min(image.shape[0], ys.max()+pad+1)
        alpha = cv2.GaussianBlur(cv2.dilate(mask[y1:y2, x1:x2], np.ones((3, 3), np.uint8)), (5, 5), 0)
        patches.append(np.dstack([image[y1:y2, x1:x2], alpha]))
    return patches


def fondo(image):
    """Pixel del foglio: chiari e poco saturi, senza puntini isolati."""
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 2] > 150) & (hsv[:, :, 1] < 40)).astype(np.uint8)
    return cv2.erode(cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8)), np.ones((5, 5), np.uint8))


def _ruota(patch, angle, scale):
    h, w = patch.shape[:2]
    matrix = cv2.getRotationMatrix2D((w/2, h/2), angle, scale)
    cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
    nw, nh = int(h*sin+w*cos)+2, int(h*cos+w*sin)+2
    matrix[0, 2] += nw/2-w/2
    matrix[1, 2] += nh/2-h/2
    return cv2.warpAffine(patch, matrix, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))


def _sovrapposti(box, boxes, limite=.04):
    for other in boxes:
        w = min(box[2], other[2])-max(box[0], other[0])
        h = min(box[3], other[3])-max(box[1], other[1])
        if w > 0 and h > 0 and w*h/min((box[2]-box[0])*(box[3]-box[1]), (other[2]-other[0])*(other[3]-other[1])) > limite:
            return True
    return False


def componi(background, rng, extra):
    """Una scena: i contenitori veri dello sfondo più fino a ``extra`` ritagli incollati.

    ``background`` = (immagine BGR, maschera del fondo, riquadri veri, ritagli).
    """
    image, floor, real, patches = background
    scene = image.astype(np.float32)*rng.uniform(.88, 1.12)
    height, width = image.shape[:2]
    if real:
        zx1 = max(0, min(b[0] for b in real)-MARGINE_ZONA*width)
        zy1 = max(0, min(b[1] for b in real)-MARGINE_ZONA*height)
        zx2 = min(width, max(b[2] for b in real)+MARGINE_ZONA*width)
        zy2 = min(height, max(b[3] for b in real)+MARGINE_ZONA*height)
    else:
        zx1, zy1, zx2, zy2 = 0, 0, width, height
    blocked = []
    for x1, y1, x2, y2 in real:
        mx, my = (x2-x1)*MARGINE_REALI, (y2-y1)*MARGINE_REALI
        blocked.append((x1-mx, y1-my, x2+mx, y2+my))
    boxes = list(real)
    added = attempts = 0
    while added < extra and attempts < extra*60:
        attempts += 1
        patch = patches[rng.integers(len(patches))]
        if rng.random() < .5:
            patch = patch[:, ::-1]
        patch = _ruota(patch, rng.uniform(0, 360), rng.uniform(.85, 1.15))
        alpha = patch[:, :, 3].astype(np.float32)/255
        ph, pw = patch.shape[:2]
        if pw >= width or ph >= height:
            continue
        x, y = int(rng.integers(0, width-pw)), int(rng.integers(0, height-ph))
        inside = alpha > .5
        ys, xs = np.nonzero(inside)
        if not len(xs) or floor[y:y+ph, x:x+pw][inside].mean() < COPERTURA_FONDO:
            continue
        box = (x+int(xs.min()), y+int(ys.min()), x+int(xs.max())+1, y+int(ys.max())+1)
        if box[0] < zx1 or box[1] < zy1 or box[2] > zx2 or box[3] > zy2:
            continue
        if _sovrapposti(box, blocked+boxes[len(real):]):
            continue
        # Ombra morbida verso il basso a destra, come sotto la luce del banco.
        shadow = cv2.GaussianBlur(alpha, (0, 0), max(2., pw/18))*rng.uniform(.12, .28)
        dx, dy = int(rng.integers(1, 6)), int(rng.integers(2, 8))
        sy2, sx2 = min(height, y+dy+ph), min(width, x+dx+pw)
        scene[y+dy:sy2, x+dx:sx2] *= (1-shadow[:sy2-y-dy, :sx2-x-dx, None])
        color = patch[:, :, :3].astype(np.float32)*rng.uniform(.9, 1.1)*rng.uniform(.96, 1.04, 3)
        region = scene[y:y+ph, x:x+pw]
        region[:] = region*(1-alpha[:, :, None])+color*alpha[:, :, None]
        boxes.append(box)
        added += 1
    noise = rng.normal(0, rng.uniform(0, 4), scene.shape)
    return np.clip(scene+noise, 0, 255).astype(np.uint8), boxes


def prepara(fonti, sfondi_vuoti=()):
    """``fonti``: (foto, risultato.json, aggiunte). Sfondi con i loro riquadri veri."""
    loaded = [carica_foto(*fonte) for fonte in fonti]
    patches = [patch for image, masks, _boxes in loaded for patch in ritagli(image, masks)]
    backgrounds = [(image, fondo(image), boxes, patches) for image, _masks, boxes in loaded]
    backgrounds += [(_rgb(path), fondo(_rgb(path)), [], patches) for path in sfondi_vuoti]
    return patches, backgrounds


def genera(fonti, destination, quante, *, seed=0, massimo=18, sfondi_vuoti=(), split=""):
    """Scrive ``quante`` scene con le etichette YOLO (classe 0) in images/<split> e labels/<split>."""
    rng = np.random.default_rng(seed)
    patches, backgrounds = prepara(fonti, sfondi_vuoti)
    images, labels = Path(destination)/"images"/split, Path(destination)/"labels"/split
    images.mkdir(parents=True, exist_ok=True)
    labels.mkdir(parents=True, exist_ok=True)
    counts = []
    for i in range(quante):
        background = backgrounds[rng.integers(len(backgrounds))]
        extra = int(rng.integers(0, max(1, massimo-len(background[2]))+1))
        scene, boxes = componi(background, rng, extra)
        name = f"sintetica_{seed}_{i:04d}"
        cv2.imwrite(str(images/f"{name}.jpg"), scene, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(80, 96))])
        h, w = scene.shape[:2]
        lines = [f"0 {(a+c)/2/w:.6f} {(b+d)/2/h:.6f} {(c-a)/w:.6f} {(d-b)/h:.6f}" for a, b, c, d in boxes]
        (labels/f"{name}.txt").write_text("\n".join(lines)+("\n" if lines else ""), encoding="utf-8")
        counts.append(len(boxes))
    return {"scene": quante, "ritagli": len(patches), "sfondi": len(backgrounds),
            "contenitori_per_scena": {"min": min(counts), "max": max(counts), "media": round(float(np.mean(counts)), 2)}}


def anteprima(destination, output, quante=12):
    """Mosaico di controllo con i riquadri disegnati: verificare a occhio prima di addestrare."""
    images = sorted((Path(destination)/"images").glob("*.jpg"))[:quante]
    tiles = []
    for path in images:
        image = cv2.imread(str(path))
        h, w = image.shape[:2]
        rows = [r.split() for r in (Path(destination)/"labels"/(path.stem+".txt")).read_text(encoding="utf-8").splitlines() if r]
        for _c, cx, cy, bw, bh in rows:
            cx, cy, bw, bh = (float(v) for v in (cx, cy, bw, bh))
            cv2.rectangle(image, (round((cx-bw/2)*w), round((cy-bh/2)*h)), (round((cx+bw/2)*w), round((cy+bh/2)*h)), (80, 220, 30), 2)
        cv2.putText(image, f"{len(rows)}", (10, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
        tiles.append(cv2.resize(image, (400, round(400*h/w))))
    columns = 3
    height = max(t.shape[0] for t in tiles)
    rows = [np.hstack([cv2.copyMakeBorder(t, 0, height-t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in tiles[i:i+columns]]
                      + [np.zeros((height, 400, 3), np.uint8)]*(columns-len(tiles[i:i+columns])))
            for i in range(0, len(tiles), columns)]
    cv2.imwrite(str(output), np.vstack(rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foto", type=Path, nargs="+")
    parser.add_argument("--sam", type=Path, required=True, help="cartella con <foto>/risultato.json")
    parser.add_argument("--aggiunte", type=Path, help="JSON {nome_foto: [[x1,y1,x2,y2] px]} dei contenitori persi da SAM")
    parser.add_argument("--sfondi-vuoti", type=Path, nargs="*", default=[], help="foto reali della scatola vuota")
    parser.add_argument("--quante", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    extra = json.loads(args.aggiunte.read_text(encoding="utf-8")) if args.aggiunte else {}
    fonti = [(p, args.sam/p.stem/"risultato.json", extra.get(p.name, [])) for p in args.foto]
    summary = genera(fonti, args.output, args.quante, seed=args.seed, sfondi_vuoti=args.sfondi_vuoti)
    anteprima(args.output, args.output/"anteprima.jpg")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
