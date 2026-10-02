"""Banco ottico locale: solo webcam, nessun archivio o lettore RFID."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lims import vision


def crea_server(port=8771, *, sam2=None, token=None, qwen=None):
    token = token or secrets.token_urlsafe(24)
    static = Path(__file__).resolve().parents[1] / "webui" / "static"
    guard = threading.Lock()
    allowed_origin = "http://127.0.0.1:8770"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def risposta(self, code, body, mime="application/json"):
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            if self.headers.get("Origin") == allowed_origin:
                self.send_header("Access-Control-Allow-Origin", allowed_origin)
                self.send_header("Vary", "Origin")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            url = urlsplit(self.path)
            name = {"/": "sam2.html" if sam2 is not None else "prova-visiva.html",
                    "/prova-visiva.js": "prova-visiva.js", "/sam2.html": "sam2.html",
                    "/sam2.js": "sam2.js"}.get(url.path)
            if url.path in ("/sam2-auto.html", "/sam2-auto.js"):
                name = url.path[1:]
            if name is None:
                return self.risposta(404, {"errore": "pagina assente"})
            if url.path == "/" and parse_qs(url.query).get("t") != [token]:
                return self.risposta(401, {"errore": "aprire il collegamento della prova"})
            return self.risposta(200, (static / name).read_bytes(),
                                 "text/html; charset=utf-8" if name.endswith("html") else "text/javascript; charset=utf-8")

        def do_POST(self):
            if self.path not in ("/api/anteprima", "/api/sam2", "/api/sam2/automatico", "/api/sam2/prepara", "/api/qwen/automatico"):
                return self.risposta(404, {"errore": "operazione assente"})
            if not secrets.compare_digest(self.headers.get("X-RFID-Token", ""), token):
                return self.risposta(401, {"errore": "collegamento non valido"})
            if self.headers.get("Origin") not in (None, allowed_origin, f"http://127.0.0.1:{self.server.server_port}"):
                return self.risposta(403, {"errore": "origine non autorizzata"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2_850_000:
                    raise ValueError("fotogramma troppo grande")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("richiesta non valida")
                if self.path in ("/api/sam2", "/api/sam2/automatico", "/api/sam2/prepara", "/api/qwen/automatico"):
                    remote = self.path == "/api/qwen/automatico"
                    if remote and qwen is None:
                        return self.risposta(503, {"errore": "Qwen non configurato: impostare OPENROUTER_API_KEY nel servizio locale"})
                    if not remote and sam2 is None:
                        return self.risposta(503, {"errore": "avviare il servizio SAM 2 locale"})
                    from app.sam2_engine import valida_oggetti
                    automatico = self.path.endswith("/automatico")
                    prepara = self.path.endswith("/prepara")
                    objects = None if automatico or prepara else valida_oggetti(data.get("oggetti"))
                    image = vision.immagine(data.get("immagine_base64"))
                    if not guard.acquire(blocking=False):
                        return self.risposta(409, {"errore": "analisi già in corso; attendere il risultato"})
                    try:
                        if remote:
                            result = qwen.analizza_web(image)
                        elif prepara:
                            from app.sam2_geometry import rettifica
                            result = rettifica(image, data.get("calibrazione"))
                        elif automatico:
                            from app.sam2_automatico import analizza_automatico
                            result = analizza_automatico(sam2, image)
                        else:
                            result = sam2.analizza(image, objects)
                    finally:
                        guard.release()
                    return self.risposta(200, result)
                cfg = {}
                for key, default, low, high in (("raggio_min", .012, .005, .2), ("raggio_max", .16, .01, .4), ("sensibilita", 30, 10, 80)):
                    value = float(data.get(key, default))
                    if not low <= value <= high:
                        raise ValueError("parametri fuori intervallo")
                    cfg[key] = value
                if cfg["raggio_min"] >= cfg["raggio_max"]:
                    raise ValueError("raggio minimo maggiore del massimo")
                if not guard.acquire(blocking=False):
                    return self.risposta(409, {"errore": "analisi già in corso"})
                try:
                    result = vision.analizza(vision.immagine(data.get("immagine_base64")), cfg,
                                            area_manuale=data.get("area_manuale"))
                finally:
                    guard.release()
                return self.risposta(200, {k: v for k, v in result.items() if k != "miniatura"})
            except (ValueError, TypeError, KeyError, ImportError) as exc:
                return self.risposta(400, {"errore": str(exc)})
            except (RuntimeError, OSError):
                import traceback
                traceback.print_exc()
                name = "Qwen/OpenRouter" if self.path == "/api/qwen/automatico" else "SAM 2"
                return self.risposta(503, {"errore": f"{name} non ha completato l'analisi; controllare il log del servizio"})

        def do_OPTIONS(self):
            if self.path not in ("/api/anteprima", "/api/sam2", "/api/sam2/automatico", "/api/sam2/prepara", "/api/qwen/automatico") or self.headers.get("Origin") != allowed_origin:
                return self.risposta(403, {"errore": "origine non autorizzata"})
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", allowed_origin)
            self.send_header("Access-Control-Allow-Methods", "POST")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-RFID-Token")
            self.send_header("Content-Length", "0")
            self.end_headers()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8771)
    args = parser.parse_args()
    server, token = crea_server(args.port)
    url = f"http://127.0.0.1:{server.server_port}/?t={token}"
    logs = Path(__file__).resolve().parents[2] / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "prova_visiva_url.txt").write_text(url, encoding="utf-8")
    print(url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
