"""Confronta codifica e decoder numericamente con gli stessi input PyTorch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

import numpy as np
import openvino as ov
from PIL import Image

from app.sam2_borsa import PUNTI_PARETI
from app.sam2_engine import Sam2Locale


def main():
    engine = Sam2Locale()
    core = ov.Core()
    folder = Path("models/sam2/openvino")
    config = {"INFERENCE_PRECISION_HINT": ov.Type.f32, "INFERENCE_NUM_THREADS": 2,
              "PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1", "CACHE_DIR": str(folder/"cache-cpu")}
    encoder = core.compile_model(str(folder/(sys.argv[1] if len(sys.argv)>1 else "encoder.xml")), "CPU", config)
    decoder_model = core.read_model(str(folder/"decoder.xml"))
    decoder_model.reshape({decoder_model.input(3):[1,1,4,2],decoder_model.input(4):[1,1,4]})
    decoder = core.compile_model(decoder_model, "CPU", config)
    image = Image.open(r"C:\Users\Daniele\Pictures\RFID\esempio1.png").convert("RGB")
    inputs = engine.processor(images=image,input_points=[[[[x*image.width,y*image.height] for x,y in PUNTI_PARETI]]],
                              input_labels=[[[1]*4]],return_tensors="pt")
    pixels = inputs.pop("pixel_values")
    def compare(a,b):
        return {"errore_massimo":float(np.max(np.abs(a-b))), "errore_medio":float(np.mean(np.abs(a-b))),
                "scala_media":float(np.mean(np.abs(a))), "shape":list(a.shape)}
    with engine.torch.inference_mode():
        torch_features=engine.model.get_image_embeddings(pixels)
        ov_features=encoder([pixels.numpy()])
        ov_features=[ov_features[encoder.output(i)] for i in range(3)]
        report={"features":[compare(a.numpy(),b) for a,b in zip(torch_features,ov_features,strict=True)]}
        golden=engine.model(**inputs,image_embeddings=torch_features)
        points,labels=inputs["input_points"].numpy(),inputs["input_labels"].numpy()
        for name,features in (("decoder_stessi_embeddings",[f.numpy() for f in torch_features]),
                              ("modello_completo",ov_features)):
            out=decoder([*features,points,labels])
            report[name]={"maschere":compare(golden.pred_masks.numpy(),out[decoder.output(0)]),
                          "score":compare(golden.iou_scores.numpy(),out[decoder.output(1)]),
                          "score_torch":golden.iou_scores.numpy().tolist(),
                          "score_openvino":out[decoder.output(1)].tolist()}
    print(json.dumps(report,indent=2),flush=True)
    output=Path("demo-output/openvino-20261002/confronto-numerico.json")
    output.write_text(json.dumps(report,indent=2),encoding="utf-8")


if __name__ == "__main__":
    main()
