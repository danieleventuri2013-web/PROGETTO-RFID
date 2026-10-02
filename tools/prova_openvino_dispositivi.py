"""Verifica i dispositivi OpenVINO con un'inferenza effettiva, senza fallback."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import openvino as ov
from openvino import opset13 as ops


def main():
    core = ov.Core()
    report = {"versione": ov.__version__, "dispositivi": core.available_devices, "prove": {}}
    x = ops.parameter([1, 3, 32, 32], np.float32)
    model = ov.Model([ops.relu(x)], [x], "prova_gpu")
    for device in core.available_devices:
        data = {"nome": core.get_property(device, "FULL_DEVICE_NAME")}
        try:
            compiled = core.compile_model(model, device)
            result = compiled([np.full((1, 3, 32, 32), -1, np.float32)])[compiled.output(0)]
            data.update(inferenza_ok=bool(np.all(result == 0)),
                        esecuzione=compiled.get_property("EXECUTION_DEVICES"))
        except Exception as exc:
            data.update(inferenza_ok=False, errore=str(exc))
        report["prove"][device] = data
    output = Path(__file__).resolve().parents[1]/"demo-output"/"openvino-20261002"
    output.mkdir(parents=True, exist_ok=True)
    (output/"dispositivi.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
