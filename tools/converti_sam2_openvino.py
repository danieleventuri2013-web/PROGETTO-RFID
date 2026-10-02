"""Converte i pesi SAM 2 locali; nessun download e nessuna immagine inviata."""
from __future__ import annotations

import argparse
import os
import sys
import time
import warnings
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))

import openvino as ov
import torch

from app.sam2_engine import MODEL_DIR, Sam2Locale


class Codifica(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, pixel_values):
        return tuple(self.model.get_image_embeddings(pixel_values))


class Decodifica(torch.nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, feat0, feat1, feat2, points, labels):
        output = self.model(image_embeddings=[feat0, feat1, feat2],
                            input_points=points, input_labels=labels, multimask_output=True)
        return output.pred_masks, output.iou_scores, output.object_score_logits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eager", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sdpa-sperimentale", action="store_true", help="solo confronto: conversione SDPA non validata")
    args = parser.parse_args()
    destination = MODEL_DIR.parent/"openvino"
    destination.mkdir(exist_ok=True)
    engine = Sam2Locale()
    if not args.sdpa_sperimentale:
        engine.model.set_attn_implementation("eager")
    started = time.perf_counter()
    encoder_path = destination/("encoder.xml" if args.sdpa_sperimentale else "encoder-eager.xml")
    decoder_path = destination/"decoder.xml"
    with torch.inference_mode(), warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=torch.jit.TracerWarning)
        if not encoder_path.exists():
            print("Conversione codifica immagine...", flush=True)
            encoder = ov.convert_model(Codifica(engine.model).eval(),
                                       example_input=torch.zeros(1, 3, 1024, 1024),
                                       input=[1, 3, 1024, 1024])
            ov.save_model(encoder, encoder_path, compress_to_fp16=False)
            print(f"Codifica salvata: {time.perf_counter()-started:.1f} s", flush=True)
        if not decoder_path.exists():
            print("Conversione decoder con tre maschere...", flush=True)
            examples = (torch.zeros(1, 32, 256, 256), torch.zeros(1, 64, 128, 128),
                        torch.zeros(1, 256, 64, 64), torch.full((1, 8, 1, 2), 512.),
                        torch.ones(1, 8, 1, dtype=torch.int64))
            decoder = ov.convert_model(Decodifica(engine.model).eval(), example_input=examples,
                                      input=[[1,32,256,256], [1,64,128,128], [1,256,64,64],
                                             [1,-1,-1,2], [1,-1,-1]])
            ov.save_model(decoder, decoder_path, compress_to_fp16=False)
    print(f"Conversione terminata: {time.perf_counter()-started:.1f} s", flush=True)


if __name__ == "__main__":
    main()
