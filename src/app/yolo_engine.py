"""YOLO locale (Ultralytics): rilevamento rapido dei contenitori su una foto.

Stesso contratto del conteggio automatico SAM 2 / Qwen (``oggetti`` con
``centro`` e ``contorni`` normalizzati), così banco webcam e Sigillo lo
mostrano senza un percorso dedicato. I pesi sono soltanto locali: l'analisi
non scarica nulla e non invia immagini.

Il modello base COCO non ha una classe «barattolo visto dall'alto»: sulle
foto del banco vede al più qualche «clock» o «bowl» a bassa confidenza.
Per questo il risultato di un modello COCO è sempre marcato incerto; il
conteggio diventa utile con pesi addestrati sui contenitori (``classi``
vuoto = tutte le classi del modello) oppure con YOLOE e un ``prompt``.
"""
from __future__ import annotations

import math
import time
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parents[2] / "models" / "yolo"
PESI_PREDEFINITI = "yolo11n.pt"
# Prodotti da tools/addestra_yolo.py --finale: una sola classe, «contenitore».
# In ordine di preferenza: con scene sintetiche (leave-one-out 6/6, 48/48 nelle
# varianti ruotate) e poi quello delle sole foto reali (5/6, 40/48).
PESI_ADDESTRATI = ("contenitori-sintetiche-yolo11n.pt", "contenitori-yolo11n.pt")


def pesi_predefiniti(model_dir=MODEL_DIR):
    """I primi pesi addestrati presenti, altrimenti il modello base COCO."""
    return next((name for name in PESI_ADDESTRATI if (model_dir/name).is_file()), PESI_PREDEFINITI)
# Oltre questa frazione della foto un riquadro è la borsa o il piano, non un campione.
AREA_MAX = .12
# Sotto questa confidenza un riquadro è contato ma il risultato va verificato.
CONFIDENZA_SICURA = .5
# Un riquadro contenuto quasi per intero in un altro è lo stesso contenitore
# (es. solo coperchio + coperchio e corpo di un barattolo inclinato): visti
# dall'alto due recipienti non stanno uno dentro l'altro. La NMS di YOLO
# guarda l'IoU, che per due riquadri annidati di misura diversa resta bassa.
CONTENIMENTO = .85
DISPOSITIVI = ("cpu", "intel:gpu", "intel:cpu")


def risolvi_pesi(pesi, model_dir=MODEL_DIR):
    """Solo file locali: un nome senza percorso si cerca in ``models/yolo``."""
    path = Path(pesi)
    if not path.is_absolute() and not path.exists():
        path = model_dir / path
    if not path.exists():
        raise RuntimeError(f"pesi YOLO assenti: {path}. Scaricarli con src/app/sam2_preview.py --scarica-yolo")
    return path


def _poligono(points, width, height, limite=200):
    """Poligono normalizzato, semplificato: il contratto ammette 2000 punti."""
    import cv2
    import numpy as np

    pts = np.asarray(points, dtype=np.float32).reshape(-1, 1, 2)
    if len(pts) > 4:
        pts = cv2.approxPolyDP(pts, 1.5, True)
    pts = pts.reshape(-1, 2)[:limite]
    return [[round(min(1., max(0., float(x)/width)), 6), round(min(1., max(0., float(y)/height)), 6)] for x, y in pts]


def _contenuto(a, b):
    """Frazione del riquadro più piccolo che cade dentro l'altro."""
    w = min(a[2], b[2])-max(a[0], b[0])
    h = min(a[3], b[3])-max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.
    return w*h/min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))


def riassumi_rilevamenti(riquadri, confidenze, classi, nomi, size, *, maschere=None, area_max=AREA_MAX,
                         contenimento=CONTENIMENTO):
    """Rilevamenti grezzi → oggetti del contratto. Nessun numero atteso.

    ``riquadri`` in pixel xyxy; ``maschere`` (facoltative) poligoni in pixel,
    uno per riquadro. Scarta solo ciò che non può essere un campione: un
    riquadro grande quanto la borsa, e un riquadro annidato in uno più
    sicuro (``contenimento=None`` disattiva questa regola).
    Restituisce gli oggetti e i conteggi degli scarti per motivo.
    """
    width, height = size
    objects, excluded, kept = [], {"borsa": 0, "annidati": 0}, []
    order = sorted(range(len(riquadri)), key=lambda i: -float(confidenze[i]))
    for i in order:
        box, conf, cls = riquadri[i], confidenze[i], classi[i]
        x1, y1, x2, y2 = (float(v) for v in box)
        if not all(math.isfinite(v) for v in (x1, y1, x2, y2)) or x2 <= x1 or y2 <= y1:
            continue
        if (x2-x1)*(y2-y1) > area_max*width*height:
            excluded["borsa"] += 1
            continue
        if contenimento is not None and any(_contenuto((x1, y1, x2, y2), k) >= contenimento for k in kept):
            excluded["annidati"] += 1
            continue
        kept.append((x1, y1, x2, y2))
        poly = maschere[i] if maschere is not None and maschere[i] is not None and len(maschere[i]) >= 3 else \
            [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        objects.append({"centro": [round(min(1., max(0., (x1+x2)/2/width)), 6), round(min(1., max(0., (y1+y2)/2/height)), 6)],
                        "contorni": [_poligono(poly, width, height)],
                        "classe": str(nomi.get(int(cls), int(cls)) if isinstance(nomi, dict) else nomi[int(cls)]),
                        "confidenza": round(float(conf), 3)})
    # Stesso ordine di lettura di SAM: per righe, poi da sinistra.
    objects.sort(key=lambda o: (round(o["centro"][1]/.2), o["centro"][0]))
    seen = set()
    for i, obj in enumerate(objects):
        # Il contratto rifiuta centri duplicati: due riquadri identici sono un doppio conteggio.
        key = tuple(obj["centro"])
        if key in seen:
            raise ValueError("centri identici nei rilevamenti YOLO")
        seen.add(key)
        obj["id"] = i+1
    return objects, excluded


class YoloLocale:
    def __init__(self, pesi=PESI_PREDEFINITI, *, dispositivo="cpu", confidenza=.25, classi=None,
                 prompt=None, area_max=AREA_MAX, contenimento=CONTENIMENTO, imgsz=640, model_dir=MODEL_DIR):
        if dispositivo not in DISPOSITIVI:
            raise ValueError(f"dispositivo YOLO non valido: scegliere fra {', '.join(DISPOSITIVI)}")
        if not .01 <= float(confidenza) <= .95:
            raise ValueError("confidenza YOLO fuori intervallo (0,01–0,95)")
        path = risolvi_pesi(pesi, model_dir)
        if dispositivo.startswith("intel:") and not (path.is_dir() and path.name.endswith("_openvino_model")):
            raise ValueError("su OpenVINO indicare la cartella *_openvino_model esportata (tools/confronta_yolo.py --openvino)")
        import numpy as np
        from ultralytics import YOLO

        self.path, self.dispositivo, self.confidenza = path, dispositivo, float(confidenza)
        self.area_max, self.contenimento, self.imgsz = area_max, contenimento, imgsz
        self.prompt = list(prompt or [])
        if self.prompt:
            from ultralytics import YOLOE

            if "yoloe" not in path.name.lower():
                raise ValueError("il prompt testuale richiede pesi YOLOE")
            self.model = YOLOE(str(path))
            self.model.set_classes(self.prompt, self.model.get_text_pe(self.prompt))
        else:
            self.model = YOLO(str(path), task="segment" if "-seg" in path.name else "detect")
        names = self.model.names
        self.nomi = dict(names) if isinstance(names, dict) else dict(enumerate(names))
        self.coco = len(self.nomi) == 80 and self.nomi.get(41) == "cup" and not self.prompt
        wanted = list(classi or [])
        unknown = [c for c in wanted if c not in self.nomi.values()]
        if unknown:
            raise ValueError(f"classi assenti nel modello: {', '.join(unknown)}")
        self.classi = [k for k, v in self.nomi.items() if v in wanted] or None
        stem = path.name.removesuffix("_openvino_model").removesuffix(".pt")
        self.description = (f"{stem} · Ultralytics {'OpenVINO ' if dispositivo.startswith('intel:') else ''}"
                            f"{dispositivo.split(':')[-1].upper()}" + (" · prompt " + ", ".join(self.prompt) if self.prompt else ""))
        # Il primo predict compila/inizializza: fuori dal tempo dello scatto.
        self._predict(np.zeros((imgsz, imgsz, 3), dtype=np.uint8))

    def _predict(self, source):
        return self.model.predict(source, conf=self.confidenza, imgsz=self.imgsz, device=self.dispositivo,
                                  classes=self.classi, agnostic_nms=True, max_det=100, verbose=False)[0]

    def analizza_automatico(self, image):
        import numpy as np

        started = time.perf_counter()
        rgb = image.convert("RGB")
        # Ultralytics tratta gli array numpy come BGR (convenzione OpenCV).
        result = self._predict(np.ascontiguousarray(np.asarray(rgb)[:, :, ::-1]))
        inference = time.perf_counter()-started
        boxes = result.boxes
        masks = None
        if getattr(result, "masks", None) is not None:
            masks = [np.asarray(m) for m in result.masks.xy]
        objects, excluded = riassumi_rilevamenti(boxes.xyxy.cpu().numpy(), boxes.conf.cpu().numpy(),
                                                 boxes.cls.cpu().numpy(), self.nomi, rgb.size,
                                                 maschere=masks, area_max=self.area_max, contenimento=self.contenimento)
        warnings = []
        if excluded["borsa"]:
            warnings.append(f"{excluded['borsa']} riquadri grandi come la borsa esclusi dal conteggio")
        if excluded["annidati"]:
            # Prudenza: un piccolo recipiente davanti a uno grande potrebbe annidarsi in prospettiva.
            warnings.append(f"{excluded['annidati']} riquadri annidati in un altro contati una sola volta: verificare quei contenitori")
        if self.coco:
            warnings.append("modello base COCO, non addestrato sui contenitori: conteggio indicativo, verificare ogni riquadro")
        if not objects:
            warnings.append("nessun campione rilevato: verificare l'inquadratura")
        weak = [o["id"] for o in objects if o["confidenza"] < CONFIDENZA_SICURA]
        if weak:
            warnings.append(f"confidenza bassa sui campioni {', '.join(map(str, weak))}: verificarli")
        classes = {}
        for obj in objects:
            classes[obj["classe"]] = classes.get(obj["classe"], 0)+1
        return {"automatico": True, "dimensioni": list(rgb.size), "conteggio": len(objects), "oggetti": objects,
                "incerto": self.coco or not objects or bool(weak) or bool(excluded["annidati"]),
                "scartati": excluded, "classi_rilevate": classes, "avvisi": warnings,
                "modello": self.description, "tipo_overlay": "contorni" if masks is not None else "riquadri",
                "soglia_confidenza": self.confidenza, "tempo_conteggio_secondi": round(inference, 3),
                "tempo_secondi": round(time.perf_counter()-started, 3), "borsa": {},
                "nota": ("Contorni" if masks is not None else "Riquadri") + " verdi: contenitori rilevati da YOLO in locale, "
                        "con classe e confidenza; verificare che ogni numero corrisponda a un campione."}
