"""Avvia il banco SAM 2 locale; solo foto e clic, senza RFID o database."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.sam2_engine import MODEL_DIR, MODEL_ID, Sam2Locale
from app.vision_preview import crea_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="scarica il modello ufficiale, senza inviare immagini")
    parser.add_argument("--port", type=int, default=8772)
    parser.add_argument("--motore", choices=("torch", "openvino"), default="torch", help="motore locale di inferenza")
    parser.add_argument("--dispositivo", choices=("GPU", "CPU"), default="GPU", help="dispositivo esplicito per OpenVINO")
    parser.add_argument("--precisione", choices=("f32", "f16"), default="f32", help="precisione OpenVINO; F32 conserva la precisione dei pesi")
    parser.add_argument("--codifica", choices=("torch", "openvino"), default="openvino", help="codifica immagine; torch permette il motore misto CPU/GPU")
    parser.add_argument("--riusa-token", action="store_true", help="riusa il collegamento locale esistente durante un aggiornamento del servizio")
    args = parser.parse_args()
    if args.download:
        from huggingface_hub import snapshot_download
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_download(MODEL_ID, local_dir=str(MODEL_DIR), allow_patterns=["*.json", "*.safetensors"])
        print(f"Modello locale pronto: {MODEL_DIR}", flush=True)
        return
    if args.motore == "openvino":
        from app.sam2_openvino import Sam2OpenVino
        print(f"Caricamento SAM 2.1 Tiny con OpenVINO {args.dispositivo}…", flush=True)
        engine = Sam2OpenVino(args.dispositivo, precision=args.precisione, encoding=args.codifica)
        engine.prepara()
        print(f"Dispositivi effettivi: {engine.dispositivi_esecuzione()}", flush=True)
    else:
        print("Caricamento SAM 2.1 Tiny sulla CPU…", flush=True)
        engine = Sam2Locale()
    logs = Path(__file__).resolve().parents[2] / "logs"
    previous_token = None
    if args.riusa_token and (logs / "sam2_url.txt").is_file():
        previous = urlsplit((logs / "sam2_url.txt").read_text(encoding="utf-8").strip())
        if previous.hostname == "127.0.0.1" and previous.port == args.port:
            previous_token = parse_qs(previous.query).get("t", [None])[0]
    qwen = None
    if os.getenv("OPENROUTER_API_KEY"):
        from app.openrouter_vision import OpenRouterVision
        qwen = OpenRouterVision(reasoning=True, provider="DekaLLM")
        print("Qwen3.8 27B disponibile su scelta esplicita; foto inviate a OpenRouter soltanto allo scatto/rianalisi.", flush=True)
    server, token = crea_server(args.port, sam2=engine, token=previous_token, qwen=qwen)
    url = f"http://127.0.0.1:{server.server_port}/?t={token}"
    logs.mkdir(exist_ok=True)
    (logs / "sam2_url.txt").write_text(url, encoding="utf-8")
    auto_url = f"http://127.0.0.1:{server.server_port}/sam2-auto.html?t={token}"
    (logs / "sam2_auto_url.txt").write_text(auto_url, encoding="utf-8")
    print(url, flush=True)
    print(f"Scatto automatico: {auto_url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
