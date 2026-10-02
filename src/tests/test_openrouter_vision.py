"""Contratto del confronto remoto: niente rete, numeri incoerenti e richieste."""
from __future__ import annotations

import base64
import io
import json
import os
import random
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from PIL import Image

from app.openrouter_vision import OpenRouterVision, valida_risposta
from app.vision_preview import crea_server


def test_incoerenze_e_centri_rifiutati():
    invalid=[{"conteggio":2,"campioni":[{"centro":[.2,.3]}],"incerto":False,"nota":""},
             {"conteggio":1,"campioni":[{"centro":[2,.3]}],"incerto":False,"nota":""},
             {"conteggio":2,"campioni":[{"centro":[.2,.3]},{"centro":[.2,.3]}],"incerto":False,"nota":""},
             {"conteggio":None,"campioni":[],"incerto":False,"nota":""}]
    for value in invalid:
        try:
            valida_risposta(value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)
    assert valida_risposta({"conteggio":None,"campioni":[],"incerto":True,"nota":"foto oscurata"})["conteggio"] is None


def test_richiesta_senza_nomi_o_numero_atteso():
    result={"conteggio":1,"campioni":[{"centro":[.4,.5]}],"incerto":False,"nota":""}
    def fake_open(request, timeout):
        body=json.loads(request.data)
        assert "atteso" not in json.dumps(body)
        assert body["model"]=="qwen/qwen3.8-27b"
        assert body["reasoning"]=={"enabled":False}
        assert body["provider"]["order"]==["DekaLLM"]
        assert body["provider"]["allow_fallbacks"] is False
        assert len(body["messages"])==1
        uri=body["messages"][0]["content"][1]["image_url"]["url"]
        photo=Image.open(io.BytesIO(base64.b64decode(uri.split(',')[1])))
        assert photo.size==(80,60) and photo.getpixel((0,0))==(17,28,39)
        return io.BytesIO(json.dumps({"choices":[{"finish_reason":"stop","message":{"content":json.dumps(result)}}],
                                     "model":body["model"],"usage":{"cost":.001},"provider":"DekaLLM"}).encode())
    with patch.dict(os.environ,{"OPENROUTER_API_KEY":"chiave-finta"}),patch("urllib.request.urlopen",fake_open):
        engine=OpenRouterVision(reasoning=True,provider="DekaLLM")
        output=engine.analizza(Image.new("RGB",(80,60),(17,28,39)))
        assert output["conteggio"]==1 and output["provider"]=="DekaLLM"
        assert "chiave-finta" not in json.dumps(output)


def test_risposta_troncata_non_diventa_conteggio():
    response={"choices":[{"finish_reason":"length","message":{"content":"{}"}}]}
    with patch.dict(os.environ,{"OPENROUTER_API_KEY":"chiave-finta"}),patch(
        "urllib.request.urlopen",lambda *_a,**_kw: io.BytesIO(json.dumps(response).encode())
    ):
        try:
            OpenRouterVision().analizza(Image.new("RGB",(80,60)))
        except ValueError as exc:
            assert "interrotta" in str(exc)
        else:
            raise AssertionError("risposta incompleta accettata")


def test_webcam_fullhd_jpeg_limitato_senza_perdere_risoluzione():
    image = Image.frombytes("RGB", (1920, 1080), random.Random(42).randbytes(1920 * 1080 * 3))
    png = io.BytesIO()
    image.save(png, format="PNG")
    assert png.tell() > 5_000_000, "riproduce lo scatto PNG troppo grande per il provider"
    result = {"conteggio": 1, "campioni": [{"centro": [.5, .5]}], "incerto": False, "nota": ""}

    def fake_open(request, timeout):
        body = json.loads(request.data)
        prompt = body["messages"][0]["content"][0]["text"]
        assert "piano di fondo" in prompt and "copia speculare" in prompt
        assert "17" not in prompt and "18" not in prompt
        uri = body["messages"][0]["content"][1]["image_url"]["url"]
        assert uri.startswith("data:image/jpeg;base64,")
        photo = base64.b64decode(uri.split(",")[1])
        assert len(photo) <= 1_200_000
        assert len(request.data) < 1_610_000
        decoded = Image.open(io.BytesIO(photo))
        assert decoded.format == "JPEG" and decoded.size == image.size
        return io.BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({**result, "nota": "dubbio vicino alla parete", "incerto": True})}}],
            "provider": "DekaLLM"}).encode())

    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "chiave-finta"}), patch("urllib.request.urlopen", fake_open):
        output = OpenRouterVision().analizza_web(image)
        assert output["conteggio"] == 1 and output["formato_invio"] == "JPEG"
        assert output["dimensioni"] == [1920, 1080] and output["tipo_overlay"] == "centri"
        assert output["avvisi"] and "dubbio vicino alla parete" in output["nota"]


def test_http_qwen_token_origine_lock_ed_errori():
    entered,release=threading.Event(),threading.Event()
    class FakeQwen:
        calls=0
        delayed=False
        broken=False
        def analizza_web(self,image):
            self.calls+=1
            assert image.size==(300,200)
            if self.delayed:
                entered.set()
                assert release.wait(5)
            if self.broken:
                raise OSError("rete non disponibile")
            return {"automatico":True,"conteggio":2,"tipo_overlay":"centri", "oggetti":[
                {"id":1,"centro":[.2,.3],"contorni":[]},{"id":2,"centro":[.7,.6],"contorni":[]}]}
    engine=FakeQwen()
    server,token=crea_server(0,sam2=object(),qwen=engine)
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    buffer=io.BytesIO()
    Image.new("RGB",(300,200),(50,60,70)).save(buffer,format="JPEG")
    body=json.dumps({"immagine_base64":base64.b64encode(buffer.getvalue()).decode()}).encode()
    def post(path="/api/qwen/automatico",secret=token,origin="http://127.0.0.1:8770"):
        req=urllib.request.Request(f"http://127.0.0.1:{server.server_port}{path}",data=body,
                                  headers={"X-RFID-Token":secret,"Content-Type":"application/json","Origin":origin})
        try:
            with urllib.request.urlopen(req,timeout=5) as response:
                assert response.headers["Access-Control-Allow-Origin"]==origin
                return response.status,json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code,json.load(exc)
    try:
        assert post(secret="errato")[0]==401
        assert post(origin="https://esterno.example")[0]==403
        assert engine.calls==0,"richieste non autorizzate non raggiungono OpenRouter"
        code,result=post()
        assert code==200 and result["conteggio"]==2 and result["tipo_overlay"]=="centri", (code,result)
        engine.delayed=True
        first=[]
        pending=threading.Thread(target=lambda:first.append(post()))
        pending.start()
        try:
            assert entered.wait(3)
            assert post()[0]==409
            assert post(path="/api/sam2/automatico")[0]==409,"Qwen e SAM condividono la serializzazione"
        finally:
            release.set()
            pending.join(5)
        assert first[0][0]==200
        engine.delayed=False
        engine.broken=True
        with patch("traceback.print_exc"):
            code,error=post()
        assert code==503 and "Qwen/OpenRouter" in error["errore"] and "conteggio" not in error
        engine.broken=False
        assert post()[0]==200,"lock rilasciato dopo un errore remoto"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(5)


if __name__=="__main__":
    tests=[test_incoerenze_e_centri_rifiutati,test_richiesta_senza_nomi_o_numero_atteso,
           test_risposta_troncata_non_diventa_conteggio,
           test_webcam_fullhd_jpeg_limitato_senza_perdere_risoluzione,
           test_http_qwen_token_origine_lock_ed_errori]
    for test in tests:
        test()
        print("OK",test.__name__)
    print(f"{len(tests)}/{len(tests)} test superati")
