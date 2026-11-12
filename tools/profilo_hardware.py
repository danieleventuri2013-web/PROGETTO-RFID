"""Profilo hardware della postazione e scelta dei parametri del riconoscimento visivo.

Non sceglie dal nome della scheda grafica: misura. YOLO e la codifica di SAM 2
girano su ogni dispositivo che OpenVINO vede; vince il più veloce **a parità di
risultato** (stessi conteggi YOLO del riferimento CPU, stesse feature SAM entro
la tolleranza). Scrive ``avvia-riconoscimento.bat`` con i parametri scelti e il
rapporto completo in ``logs/profilo-hardware.json``.

Avvio:  .venv-openvino\\Scripts\\python.exe tools\\profilo_hardware.py
"""
from __future__ import annotations

import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time
from pathlib import Path

RADICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RADICE / "src"))

YOLO_PT = "contenitori-20261005-yolo11n.pt"
YOLO_OV = "contenitori-20261005-yolo11n_openvino_model"
# F16 sulla GPU è ammesso solo se le feature dell'encoder restano entro l'1%
# del riferimento CPU F32 (soglia prudente, non una misura sui conteggi).
TOLLERANZA_F16 = 0.01


def cim(classe, campi):
    comando = (f"Get-CimInstance {classe} | Select-Object {','.join(campi)} | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", comando],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        dati = json.loads(out) if out else []
        return dati if isinstance(dati, list) else [dati]
    except Exception as exc:  # il profilo hardware non deve fermare le misure
        return [{"errore": str(exc)}]


def hardware():
    import openvino as ov

    core = ov.Core()
    cpu = cim("Win32_Processor", ["Name", "NumberOfCores", "NumberOfLogicalProcessors"])
    ram = cim("Win32_ComputerSystem", ["TotalPhysicalMemory"])
    gpu = cim("Win32_VideoController", ["Name", "DriverVersion", "DriverDate"])
    dispositivi = {}
    for d in core.available_devices:
        info = {"nome": core.get_property(d, "FULL_DEVICE_NAME")}
        if d.startswith("GPU"):
            try:
                info["memoria_gb"] = round(core.get_property(d, "GPU_DEVICE_TOTAL_MEM_SIZE") / 2**30, 1)
            except Exception:
                pass
        dispositivi[d] = info
    ram_gb = round(int(ram[0].get("TotalPhysicalMemory", 0)) / 2**30, 1) if ram and "errore" not in ram[0] else None
    return {"processore": cpu, "ram_gb": ram_gb, "schede_grafiche": gpu,
            "openvino": {"versione": ov.__version__, "dispositivi": dispositivi}}


def foto_di_prova(massimo=8):
    cartelle = [Path(os.path.expandvars(r"%USERPROFILE%\Pictures\RFID")),
                RADICE / "demo-output" / "yolo-20261005"]
    for cartella in cartelle:
        foto = sorted(p for p in cartella.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
        if foto:
            return foto[:massimo]
    return []


def misura_yolo(foto):
    from PIL import Image
    from app.yolo_engine import YoloLocale

    import openvino as ov
    candidati = [("cpu", YOLO_PT)]
    if (RADICE / "models" / "yolo" / YOLO_OV).is_dir():
        candidati.append(("intel:cpu", YOLO_OV))
        if any(d.startswith("GPU") for d in ov.Core().available_devices):
            candidati.append(("intel:gpu", YOLO_OV))
    immagini = [Image.open(p).convert("RGB") for p in foto]
    esiti = {}
    for dispositivo, pesi in candidati:
        print(f"  YOLO su {dispositivo} ...", flush=True)
        try:
            t0 = time.perf_counter()
            motore = YoloLocale(pesi, dispositivo=dispositivo)
            avvio = time.perf_counter() - t0
            conteggi, tempi = [], []
            for img in immagini:
                t = time.perf_counter()
                conteggi.append(motore.analizza_automatico(img)["conteggio"])
                tempi.append(time.perf_counter() - t)
            esiti[dispositivo] = {"pesi": pesi, "avvio_s": round(avvio, 2), "conteggi": conteggi,
                                  "media_s": round(sum(tempi) / len(tempi), 3)}
        except Exception as exc:
            esiti[dispositivo] = {"pesi": pesi, "errore": str(exc)}
    rif = esiti.get("cpu", {}).get("conteggi")
    validi = {d: e for d, e in esiti.items() if "errore" not in e and e["conteggi"] == rif}
    for d, e in esiti.items():
        e["stessi_conteggi_della_cpu"] = d in validi
    scelta = min(validi, key=lambda d: validi[d]["media_s"]) if validi else "cpu"
    return esiti, scelta


def misura_sam():
    import numpy as np
    import openvino as ov

    core = ov.Core()
    cartella = RADICE / "models" / "sam2" / "openvino"
    xml = cartella / "encoder-eager.xml"
    if not xml.is_file():
        return {"errore": "encoder OpenVINO assente"}, None
    candidati = [("CPU", "f32")]
    if "GPU" in core.available_devices:
        candidati += [("GPU", "f16"), ("GPU", "f32")]
    x = np.random.default_rng(0).standard_normal((1, 3, 1024, 1024)).astype(np.float32)
    esiti, riferimento = {}, None
    for dispositivo, precisione in candidati:
        chiave = f"{dispositivo} {precisione}"
        print(f"  SAM 2 su {chiave} (la prima compilazione può richiedere minuti) ...", flush=True)
        # Stessa configurazione di app/sam2_openvino.py, così la cache resta valida.
        config = {"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": "1",
                  "INFERENCE_PRECISION_HINT": ov.Type.f16 if precisione == "f16" else ov.Type.f32}
        if dispositivo == "CPU":
            config["INFERENCE_NUM_THREADS"] = 2
        cache = cartella / f"cache-{dispositivo.lower()}"
        cache.mkdir(exist_ok=True)
        config["CACHE_DIR"] = str(cache)
        try:
            t0 = time.perf_counter()
            enc = core.compile_model(str(xml), dispositivo, config)
            compilazione = time.perf_counter() - t0
            uscite, tempi = None, []
            for _ in range(3):
                t = time.perf_counter()
                r = enc([x])
                tempi.append(time.perf_counter() - t)
                uscite = [r[enc.output(i)].copy() for i in range(3)]
            e = {"compilazione_s": round(compilazione, 1), "codifica_s": round(min(tempi), 3)}
            if riferimento is None:
                riferimento = uscite
                e["scarto_relativo"] = 0.0
            else:
                e["scarto_relativo"] = float(max(np.abs(a - b).max() / (np.abs(b).max() + 1e-9)
                                                 for a, b in zip(uscite, riferimento)))
            esiti[chiave] = e
        except Exception as exc:
            esiti[chiave] = {"errore": str(exc)}
    validi = {k: e for k, e in esiti.items() if "errore" not in e and
              (not k.endswith("f16") or e["scarto_relativo"] <= TOLLERANZA_F16)}
    scelta = min(validi, key=lambda k: validi[k]["codifica_s"]) if validi else None
    return esiti, scelta


def scrivi_bat(yolo_disp, yolo_pesi, sam, hw):
    gpu = ", ".join(g.get("Name", "?") for g in hw["schede_grafiche"])
    if sam:
        disp, prec = sam.split()
        sam_args = f"--motore openvino --dispositivo {disp} --precisione {prec}"
    else:
        sam_args = "--senza-sam"
    righe = ["@echo off", 'cd /d "%~dp0"',
             f"REM Generato da tools\\profilo_hardware.py il {dt.datetime.now():%d/%m/%Y %H:%M}",
             f"REM Schede grafiche: {gpu}",
             "echo Avvio del riconoscimento visivo con i parametri misurati su questo PC...",
             f'".venv-openvino\\Scripts\\python.exe" src\\app\\sam2_preview.py {sam_args} '
             f"--yolo {yolo_pesi} --yolo-dispositivo {yolo_disp} --riusa-token %*",
             "if errorlevel 1 pause", ""]
    (RADICE / "avvia-riconoscimento.bat").write_text("\r\n".join(righe), encoding="ascii", errors="replace")


def main():
    print("Profilo hardware della postazione...", flush=True)
    hw = hardware()
    for c in hw["processore"]:
        print(f"  Processore: {c.get('Name')} ({c.get('NumberOfCores')} core, {c.get('NumberOfLogicalProcessors')} thread)")
    print(f"  RAM: {hw['ram_gb']} GB")
    for g in hw["schede_grafiche"]:
        print(f"  Scheda grafica: {g.get('Name')} (driver {g.get('DriverVersion')})")
    print(f"  Dispositivi OpenVINO: " + ", ".join(f"{d} = {i['nome']}" for d, i in hw["openvino"]["dispositivi"].items()))

    avvisi = []
    nomi_gpu = " ".join(str(g.get("Name", "")) for g in hw["schede_grafiche"]).upper()
    if "NVIDIA" in nomi_gpu:
        avvisi.append("Scheda NVIDIA presente: PyTorch installato e' la versione solo CPU e OpenVINO usa solo GPU Intel. "
                      "Per sfruttarla serve PyTorch con CUDA (richiede Internet): chiedere prima di installarlo.")
    if "AMD" in nomi_gpu or "RADEON" in nomi_gpu:
        avvisi.append("Scheda AMD presente: OpenVINO non la usa; il riconoscimento gira su CPU o GPU Intel integrata.")
    if "GPU" not in hw["openvino"]["dispositivi"]:
        avvisi.append("OpenVINO non vede una GPU Intel: se il PC ne ha una, aggiornare il driver grafico Intel.")
    if "NPU" in hw["openvino"]["dispositivi"]:
        avvisi.append("NPU Intel presente: il servizio visivo oggi non la usa (scelte possibili: CPU/GPU).")

    foto = foto_di_prova()
    print(f"\nMisura YOLO su {len(foto)} foto...", flush=True)
    if foto:
        yolo, yolo_disp = misura_yolo(foto)
    else:
        yolo, yolo_disp = {"errore": "nessuna foto di prova trovata"}, "cpu"
        avvisi.append("Nessuna foto di prova: YOLO lasciato su CPU senza misura.")
    print("\nMisura SAM 2 (codifica immagine)...", flush=True)
    sam, sam_scelta = misura_sam()
    yolo_pesi = YOLO_PT if yolo_disp == "cpu" else YOLO_OV
    scrivi_bat(yolo_disp, yolo_pesi, sam_scelta, hw)

    rapporto = {"data": dt.datetime.now().isoformat(timespec="seconds"), "hardware": hw,
                "foto_di_prova": [str(p) for p in foto], "yolo": yolo, "sam2": sam,
                "scelta": {"yolo_dispositivo": yolo_disp, "yolo_pesi": yolo_pesi, "sam2": sam_scelta},
                "avvisi": avvisi}
    (RADICE / "logs").mkdir(exist_ok=True)
    (RADICE / "logs" / "profilo-hardware.json").write_text(json.dumps(rapporto, indent=2, ensure_ascii=False),
                                                           encoding="utf-8")
    print("\nRISULTATI")
    for d, e in (yolo.items() if isinstance(yolo, dict) and "errore" not in yolo else []):
        if "errore" in e:
            print(f"  YOLO {d:10} errore: {e['errore'][:100]}")
        else:
            print(f"  YOLO {d:10} {e['media_s']:.3f} s/foto  conteggi {e['conteggi']}"
                  + ("" if e["stessi_conteggi_della_cpu"] else "  <- DIVERSI dalla CPU, scartato"))
    for k, e in sam.items():
        if "errore" in e:
            print(f"  SAM  {k:10} errore: {str(e['errore'])[:100]}")
        else:
            print(f"  SAM  {k:10} {e['codifica_s']:.3f} s/codifica  scarto {e['scarto_relativo']:.2e}")
    print(f"\nSCELTA: YOLO su {yolo_disp}, SAM 2 su {sam_scelta or 'non disponibile (solo YOLO)'}")
    print("Avvio con i parametri scelti: avvia-riconoscimento.bat")
    for a in avvisi:
        print(f"AVVISO: {a}")
    print(f"Rapporto completo: {RADICE / 'logs' / 'profilo-hardware.json'}")


if __name__ == "__main__":
    main()
