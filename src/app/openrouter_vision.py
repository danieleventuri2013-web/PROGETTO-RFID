"""Conteggio visuale via OpenRouter, invocato esplicitamente dal banco da foto."""
from __future__ import annotations

import base64
import io
import json
import math
import os
import time
import urllib.error
import urllib.request

PROMPT = """Analizza soltanto questa fotografia. Conta i contenitori fisici di campioni
visibili dentro la borsa/scatola trasparente esterna. I campioni sono piccoli
contenitori rotondi chiusi, di diametri diversi, visti dall'alto; conta ciascun
contenitore una sola volta, anche se ha bordi concentrici o riflessi.
Escludi la borsa/scatola esterna, maniglie, agganci, cerniere, scritte e riflessi.
Non assumere un numero prestabilito e non leggere le scritte per dedurre quanti
campioni ci sono. Individua ogni contenitore e restituisci il suo centro.
Le coordinate centro sono [x,y] normalizzate fra 0 e 1 sull'intera fotografia,
con origine in alto a sinistra. Il conteggio deve coincidere con il numero
di centri distinti. Se non è determinabile, restituisci conteggio null e
incerto true. Rispondi esclusivamente con il JSON richiesto, senza spiegazioni
lunghe. La nota può descrivere brevemente un dubbio visivo."""

PROMPT_WEBCAM = PROMPT + """
La borsa ha pareti trasparenti che possono riflettere i coperchi, soprattutto
vicino al bordo superiore. Conta i recipienti fisicamente appoggiati sul
piano di fondo interno. Una copia speculare, traslucida o parziale sulla
parete, sul bordo o sul coperchio esterno non è un altro campione.
Prima di rispondere controlla i centri vicino alle pareti: conserva un
centro solo se identifica un recipiente reale distinto sul fondo.
Non aggiungere oggetti per completare file o simmetrie; se non riesci a
distinguere un recipiente da un riflesso segnala incerto true nella risposta
e descrivi il dubbio nella nota. Non dedurre la quantità dalla simmetria."""

SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {
              "conteggio": {"type": ["integer", "null"], "minimum": 0, "maximum": 60},
              "campioni": {"type": "array", "maxItems": 60, "items": {
                  "type": "object", "additionalProperties": False,
                  "properties": {"centro": {"type": "array", "minItems": 2, "maxItems": 2,
                                               "items": {"type": "number", "minimum": 0, "maximum": 1}}},
                  "required": ["centro"]}},
              "incerto": {"type": "boolean"}, "nota": {"type": "string"}},
          "required": ["conteggio", "campioni", "incerto", "nota"]}


def valida_risposta(value):
    """Rifiuta risultati incoerenti senza correggere il conteggio del modello."""
    if not isinstance(value, dict):
        raise ValueError("il modello non ha restituito un oggetto JSON")
    count, samples = value.get("conteggio"), value.get("campioni")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= 60):
        raise ValueError("conteggio del modello non valido")
    if not isinstance(samples, list) or len(samples) > 60:
        raise ValueError("elenco dei campioni non valido")
    if count is not None and count != len(samples):
        raise ValueError("conteggio diverso dal numero di centri restituiti")
    for sample in samples:
        center = sample.get("centro") if isinstance(sample, dict) else None
        if not isinstance(center, list) or len(center) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
            for v in center
        ):
            raise ValueError("centro del campione fuori dalla fotografia")
    if len({tuple(sample["centro"]) for sample in samples}) != len(samples):
        raise ValueError("centri identici: possibile doppio conteggio")
    if not isinstance(value.get("incerto"), bool) or not isinstance(value.get("nota"), str):
        raise ValueError("avviso del modello non valido")
    if count is None and not value["incerto"]:
        raise ValueError("conteggio assente senza segnalazione di incertezza")
    return value


class OpenRouterVision:
    def __init__(self, model="qwen/qwen3.8-27b", *, reasoning=False, timeout=90, provider=None):
        self.api_key = os.getenv("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise RuntimeError("configurare OPENROUTER_API_KEY nell'ambiente del processo")
        self.model, self.reasoning, self.timeout = model, reasoning, timeout
        self.provider = provider

    def analizza(self, image, *, formato="PNG", prompt=PROMPT):
        started = time.perf_counter()
        if image.width > 1920 or image.height > 1080:
            raise ValueError("ritagliare la fotografia prima dell'analisi: massimo Full HD")
        buffer = io.BytesIO()
        if formato not in ("PNG", "JPEG"):
            raise ValueError("formato immagine non supportato")
        rgb = image.convert("RGB")
        if formato == "JPEG":
            for quality in (95, 90, 85, 80, 75):
                buffer.seek(0)
                buffer.truncate()
                rgb.save(buffer, format="JPEG", quality=quality, optimize=True)
                if buffer.tell() <= 1_200_000:
                    break
            if buffer.tell() > 1_200_000:
                raise ValueError("fotografia troppo grande: restringere l'area della webcam")
        else:
            rgb.save(buffer, format="PNG")
        mime = "image/jpeg" if formato == "JPEG" else "image/png"
        body = {"model": self.model, "temperature": 0, "max_tokens": 1000,
                "provider": {"require_parameters": True, "sort": "latency"},
                "response_format": {"type": "json_schema", "json_schema": {
                    "name": "conteggio_campioni", "strict": True, "schema": SCHEMA}},
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64," + base64.b64encode(buffer.getvalue()).decode("ascii")}}]}]}
        if self.reasoning:
            body["reasoning"] = {"enabled": False}
        if self.provider:
            body["provider"].update(order=[self.provider], allow_fallbacks=False)
        request = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                         data=json.dumps(body).encode("utf-8"),
                                         headers={"Authorization": "Bearer " + self.api_key,
                                                  "Content-Type": "application/json",
                                                  "X-Title": "RFID confronto conteggio campioni"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as exc:
            # Il body di errore non viene riversato nei log: potrebbe contenere
            # dettagli del provider o della richiesta con la fotografia.
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: richiesta non completata") from None
        if "error" in raw:
            raise RuntimeError("OpenRouter ha restituito un errore del provider")
        choice = raw["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("risposta interrotta o incompleta: non conteggiabile")
        result = valida_risposta(json.loads(choice["message"]["content"]))
        result.update(tempo_secondi=round(time.perf_counter()-started, 3),
                      modello_richiesto=self.model, modello=raw.get("model"), provider=raw.get("provider"),
                      id_richiesta=raw.get("id"), uso=raw.get("usage", {}), dimensioni=list(image.size),
                      formato_invio=formato, byte_immagine=buffer.tell(),
                      tipo_overlay="centri approssimativi del modello visuale, senza segmentazione")
        return result

    def analizza_web(self, image):
        """Stesso contratto del banco webcam; centri, senza inventare contorni."""
        result = self.analizza(image, formato="JPEG", prompt=PROMPT_WEBCAM)
        nota_modello = result["nota"].strip()
        result.update(automatico=True, tipo_overlay="centri",
                      oggetti=[{"id":i+1,"centro":item["centro"],"contorni":[]}
                               for i,item in enumerate(result["campioni"])],
                      tempo_conteggio_secondi=result["tempo_secondi"],
                      modello=f"Qwen3.8 27B · OpenRouter ({result.get('provider') or 'provider remoto'})",
                      avvisi=["il modello segnala incertezza: verificare i campioni"] if result["incerto"] else [],
                      nota="Punti blu: posizioni approssimative dei campioni riconosciuti da Qwen; non sono contorni di segmentazione."
                           + (f" Nota del modello: {nota_modello}" if nota_modello else ""))
        return result
