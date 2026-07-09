"""Avvio applicazione RFID SIM7200.

Launcher multipiattaforma: imposta PYTHONPATH, verifica/installa le dipendenze
e mostra un menu per scegliere cosa avviare:
  1) GUI di controllo            (src/app/gui.py)
  2) CLI Step 1 test read/write  (src/app/step1_test_rw.py)
  3) Test protocollo + reader    (src/tests/)
  4) Health check lettore        (src/app/health_check.py)

Uso diretto:
  python run.py            -> menu interattivo
  python run.py gui        -> avvia direttamente la GUI
  python run.py step1      -> avvia lo Step 1
  python run.py tests      -> test senza hardware
  python run.py health     -> health check (diagnosi rapida)

Funziona su Windows / Linux / macOS.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# radice del progetto = directory che contiene questo file
ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

REQUIREMENTS = [
    "pyserial>=3.5",
    "pyyaml>=6.0",
    "matplotlib>=3.6",
    "numpy>=1.24",
]


def ensure_env() -> None:
    """Aggiunge src/ al PYTHONPATH per importare il package rfid_silion."""
    src_str = str(SRC)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)
    # anche per i subprocess
    os.environ["PYTHONPATH"] = os.pathsep.join(
        p for p in [src_str, os.environ.get("PYTHONPATH", "")] if p
    )


def ensure_deps() -> None:
    """Verifica che le dipendenze siano installate; se mancano, le installa."""
    import importlib
    missing = []
    checks = {
        "pyserial": "serial",
        "pyyaml": "yaml",
        "matplotlib": "matplotlib",
        "numpy": "numpy",
    }
    for pkg, mod in checks.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print(f"Installazione dipendenze mancanti: {', '.join(missing)} ...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--upgrade", *REQUIREMENTS]
    )


def run_gui() -> int:
    from app import gui  # noqa: F401  (import dopo env/deps)
    import tkinter as tk
    from pathlib import Path as _P
    cfg_path = SRC / "app" / "config.yaml"
    cfg = gui.load_config(str(cfg_path))
    root = tk.Tk()
    gui.RFIDGui(root, cfg, _P(str(cfg_path)))
    root.mainloop()
    return 0


def run_step1() -> int:
    from app import step1_test_rw
    sys.argv = ["step1_test_rw.py", "--config", str(SRC / "app" / "config.yaml")]
    return step1_test_rw.main()


def run_health() -> int:
    from app import health_check
    sys.argv = ["health_check.py", "--config", str(SRC / "app" / "config.yaml")]
    return health_check.main()


def run_tests() -> int:
    rc = 0
    from tests import test_protocol, test_reader
    for mod in (test_protocol, test_reader):
        if hasattr(mod, "_run_all"):
            rc |= mod._run_all()
    return rc


# mapping nomi -> funzioni (definito dopo le funzioni)
ACTIONS = {
    "1": ("GUI di controllo", run_gui),
    "2": ("CLI Step 1 (test read/write)", run_step1),
    "3": ("Test del protocollo (senza hardware)", run_tests),
    "4": ("Health check lettore (diagnosi rapida)", run_health),
}


def main() -> int:
    ensure_env()
    ensure_deps()

    # argomento diretto
    direct = {"gui": run_gui, "step1": run_step1, "tests": run_tests,
              "test": run_tests, "health": run_health}
    if len(sys.argv) > 1 and sys.argv[1].lower() in direct:
        return direct[sys.argv[1].lower()]()

    # menu interattivo
    print("=" * 60)
    print(" RFID SIM7200 — launcher")
    print("=" * 60)
    for k, (label, _) in ACTIONS.items():
        print(f"  {k}) {label}")
    print()
    choice = input("Scelta [1]: ").strip() or "1"
    if choice not in ACTIONS:
        print(f"Scelta non valida: {choice}")
        return 1
    label, fn = ACTIONS[choice]
    print(f"\n>> Avvio: {label}\n")
    try:
        return fn()
    except KeyboardInterrupt:
        print("\nInterrotto.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
