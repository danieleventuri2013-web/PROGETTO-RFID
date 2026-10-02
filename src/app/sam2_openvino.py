"""SAM 2 locale su OpenVINO: stesso modello, processor e filtri del banco CPU."""
from __future__ import annotations

import time
from types import MethodType

from app.sam2_engine import MODEL_DIR, Sam2Locale


class Sam2OpenVino(Sam2Locale):
    def __init__(self, device="GPU", model_dir=MODEL_DIR, precision="f32", encoding="openvino"):
        import openvino as ov

        if device not in ("GPU", "CPU"):
            raise ValueError("scegliere GPU o CPU esplicitamente; nessun fallback automatico")
        if precision not in ("f32", "f16"):
            raise ValueError("precisione OpenVINO non valida")
        if encoding not in ("torch", "openvino"):
            raise ValueError("codifica non valida")
        self.core = ov.Core()
        if device not in self.core.available_devices:
            raise RuntimeError(f"OpenVINO non trova {device}: {self.core.available_devices}")
        super().__init__(model_dir)
        self.device_ov = device
        self.encoding = encoding
        self.description = f"SAM 2.1 Tiny · OpenVINO {device} {precision.upper()} · codifica {encoding}"
        self.torch_encode = self.model.get_image_embeddings
        destination = model_dir.parent/"openvino"
        for name in (("encoder-eager", "decoder") if encoding == "openvino" else ("decoder",)):
            if not (destination/f"{name}.xml").is_file():
                raise RuntimeError("Modello OpenVINO mancante: eseguire tools/converti_sam2_openvino.py")
        self.config_ov = {"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1",
                          "INFERENCE_PRECISION_HINT": ov.Type.f16 if precision == "f16" else ov.Type.f32}
        if device == "CPU":
            self.config_ov["INFERENCE_NUM_THREADS"] = 2
        cache = destination/f"cache-{device.lower()}"
        cache.mkdir(exist_ok=True)
        self.config_ov["CACHE_DIR"] = str(cache)
        self.encoder = (self.core.compile_model(str(destination/"encoder-eager.xml"), device, self.config_ov)
                        if encoding == "openvino" else None)
        self.decoder_path = destination/"decoder.xml"
        self.decoders = {}
        self.timings = {"codifica_secondi": 0., "decoder_secondi": 0., "chiamate_decoder": 0}
        # Conserva il tipo Sam2Model richiesto dalla pipeline ufficiale. Cambia
        # esclusivamente l'inferenza di questa istanza, senza toccare la libreria.
        def encode(_model, pixel_values, **_kwargs):
            return self._encode(pixel_values)

        def decode(_model, pixel_values=None, image_embeddings=None, input_points=None,
                   input_labels=None, input_boxes=None, input_masks=None,
                   multimask_output=True, **_kwargs):
            if input_boxes is not None or input_masks is not None or not multimask_output:
                raise ValueError("banco OpenVINO: usare clic e tre maschere, senza box o maschere di ingresso")
            if image_embeddings is None:
                image_embeddings = self._encode(pixel_values)
            return self._decode(image_embeddings, input_points, input_labels)

        self.model.get_image_embeddings = MethodType(encode, self.model)
        self.model.forward = MethodType(decode, self.model)

    def _encode(self, pixels):
        started = time.perf_counter()
        if self.encoder is None:
            features = self.torch_encode(pixels)
        else:
            result = self.encoder([pixels.detach().cpu().numpy()])
            features = [self.torch.from_numpy(result[self.encoder.output(i)].copy()) for i in range(3)]
        self.timings["codifica_secondi"] += time.perf_counter()-started
        return features

    def _decoder(self, objects, points):
        key = objects, points
        if key not in self.decoders:
            model = self.core.read_model(str(self.decoder_path))
            model.reshape({model.input(3): [1,objects,points,2], model.input(4): [1,objects,points]})
            self.decoders[key] = self.core.compile_model(model, self.device_ov, self.config_ov)
        return self.decoders[key]

    def prepara(self):
        """Compila i due profili automatici all'avvio, fuori dal tempo dello scatto."""
        self._decoder(1, 4)
        self._decoder(8, 1)

    def _decode(self, features, points, labels):
        from transformers.models.sam2.modeling_sam2 import Sam2ImageSegmentationOutput

        if points is None or labels is None or points.shape[0] != 1:
            raise ValueError("SAM OpenVINO richiede clic su una singola foto")
        decoder = self._decoder(points.shape[1], points.shape[2])
        values = [v.detach().cpu().numpy() for v in [*features, points, labels]]
        started = time.perf_counter()
        result = decoder(values)
        tensors = [self.torch.from_numpy(result[decoder.output(i)].copy()) for i in range(3)]
        self.timings["decoder_secondi"] += time.perf_counter()-started
        self.timings["chiamate_decoder"] += 1
        return Sam2ImageSegmentationOutput(pred_masks=tensors[0], iou_scores=tensors[1],
                                           object_score_logits=tensors[2], image_embeddings=features)

    def dispositivi_esecuzione(self):
        return {"encoder": self.encoder.get_property("EXECUTION_DEVICES") if self.encoder is not None else ["CPU (PyTorch)"],
                "decoder": {str(key): decoder.get_property("EXECUTION_DEVICES")
                            for key, decoder in self.decoders.items()}}

    def analizza(self, image, objects):
        result = super().analizza(image, objects)
        result["modello"] = self.description
        return result
