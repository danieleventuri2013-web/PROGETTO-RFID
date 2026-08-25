"""Avvio applicazione RFID SIM7200.

Launcher multipiattaforma: imposta PYTHONPATH e verifica le dipendenze
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
  python run.py service    -> host JSON-RPC 2.0 su stdin/stdout
  python run.py service-gui -> GUI collegata al processo service
  python run.py tag-profile -> misura TID e USER memory di un tag
  python run.py lims       -> accettazione e ricezione campioni
  python run.py campaign   -> campagna di misura: tara potenza e parametri radio

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
    "cryptography>=42.0",
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


def ensure_deps(allow_install: bool = False) -> None:
    """Verifica le dipendenze; installa solo con --install-deps esplicito."""
    import importlib

    missing = []
    checks = {
        "pyserial": "serial",
        "pyyaml": "yaml",
        "matplotlib": "matplotlib",
        "numpy": "numpy",
        "cryptography": "cryptography",
    }
    for pkg, mod in checks.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    if not allow_install:
        command = f'"{sys.executable}" -m pip install -r "{ROOT / "requirements.txt"}"'
        raise RuntimeError(
            "Dipendenze mancanti: "
            + ", ".join(missing)
            + f". Installarle con: {command} oppure avviare con --install-deps"
        )
    specs = [spec for spec in REQUIREMENTS if spec.split(">=", 1)[0].lower() in missing]
    print(f"Installazione dipendenze mancanti: {', '.join(missing)} ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", *specs])


def run_gui() -> int:
    from app import gui  # noqa: F401  (import dopo env/deps)

    cfg_path = SRC / "app" / "config.yaml"
    cfg = gui.load_config(str(cfg_path))
    log_cfg = cfg.get("logging", {})
    gui.setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_dir=log_cfg.get("dir", "logs"),
        file_prefix="gui",
        force_debug="--debug" in sys.argv[2:],
    )
    gui.launch_gui(cfg, cfg_path)
    return 0


def run_step1() -> int:
    from app import step1_test_rw

    sys.argv = ["step1_test_rw.py", "--config", str(SRC / "app" / "config.yaml"), *sys.argv[2:]]
    return step1_test_rw.main()


def run_health() -> int:
    from app import health_check

    sys.argv = ["health_check.py", "--config", str(SRC / "app" / "config.yaml"), *sys.argv[2:]]
    return health_check.main()


def run_tag_profile() -> int:
    from app import tag_profile

    sys.argv = ["tag_profile.py", "--config", str(SRC / "app" / "config.yaml"), *sys.argv[2:]]
    return tag_profile.main()


def run_campaign() -> int:
    from app import read_campaign

    sys.argv = ["read_campaign.py", "--config", str(SRC / "app" / "config.yaml"), *sys.argv[2:]]
    return read_campaign.main()


def run_lims() -> int:
    from app import lims_gui

    sys.argv = ["lims_gui.py", "--config", str(SRC / "app" / "config.yaml"), *sys.argv[2:]]
    return lims_gui.main()


def run_webui() -> int:
    """Interfaccia operativa nel browser, servita solo in locale."""
    import webbrowser

    from app import gui
    from rfid_silion.diagnostics import setup_logging
    from rfid_silion.service import RFIDService
    from webui.server import PortaOccupataError, WebUIServer

    cfg_path = SRC / "app" / "config.yaml"
    cfg = gui.load_config(str(cfg_path))
    log_cfg = cfg.get("logging", {})
    setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_dir=log_cfg.get("dir", "logs"),
        file_prefix="webui",
        force_debug="--debug" in sys.argv[2:],
    )

    web_cfg = cfg.get("webui", {}) or {}
    server = WebUIServer(
        cfg,
        RFIDService(cfg),
        host=str(web_cfg.get("host", "127.0.0.1")),
        port=int(web_cfg.get("port", 8770)),
        # Token fisso opzionale: rende l'indirizzo stabile fra un avvio e
        # l'altro, che serve quando si lavora con un collegamento sul desktop
        # invece di copiare l'indirizzo dal terminale ogni volta.
        token=str(web_cfg.get("token") or "") or None,
        config_path=cfg_path,
    )
    # Si prende la porta prima di annunciare l'indirizzo: se l'interfaccia e'
    # gia' aperta altrove, stampare un secondo indirizzo che non funzionera'
    # e' peggio che non partire.
    try:
        server.apri()
    except PortaOccupataError as exc:
        indicazione = Path(log_cfg.get("dir", "logs")) / "webui_url.txt"
        print(
            "\n"
            "  ============================================================\n"
            f"   {exc}\n"
            "\n"
            "   Non ne servono due: userebbero lo stesso lettore.\n"
            "   Usa la finestra gia' aperta, oppure chiudila (Ctrl+C) e\n"
            "   riavvia da qui.\n"
            "\n"
            f"   L'indirizzo di quella attiva e' in {indicazione}\n"
            "  ============================================================\n",
            flush=True,
        )
        try:
            print(f"   {indicazione.read_text(encoding='utf-8').strip()}\n", flush=True)
        except OSError:
            pass
        return 2

    fisso = bool(web_cfg.get("token"))
    nota_token = (
        "   Il token e' fisso (webui.token in config.yaml): questo\n"
        "   indirizzo si puo' salvare nei preferiti.\n"
        if fisso
        else "   Il token in fondo CAMBIA A OGNI AVVIO. Un indirizzo salvato\n"
        "   nei preferiti non funziona: usa sempre quello qui sopra.\n"
    )

    # Se il server ascolta anche fuori da questa macchina, l'indirizzo del
    # loopback non serve a chi arriva dalla rete: la tavoletta ha bisogno
    # dell'IP di questo computer, che il browser non puo' indovinare.
    indirizzi = server.indirizzi()
    nota_rete = ""
    if indirizzi["rete"]:
        righe = "\n".join(f"   {u}" for u in indirizzi["rete"])
        nota_rete = (
            "\n"
            "   Dalla tavoletta (stessa rete Wi-Fi), in Chrome:\n"
            "\n" + righe + "\n"
            "\n"
            "   Se ce n'e' piu' di uno, il primo e' quello giusto quasi\n"
            "   sempre. Poi Chrome → menu → «Aggiungi a schermata Home».\n"
        )
        if not fisso:
            nota_rete += (
                "   Prima pero' fissa webui.token in config.yaml, altrimenti\n"
                "   quel collegamento smette di funzionare al prossimo avvio.\n"
            )
        print(
            "\n"
            "  [attenzione] L'interfaccia e' raggiungibile dalla rete locale\n"
            f"  (webui.host: {indirizzi['host']}). Chi ha il token comanda il\n"
            "  lettore: tienilo su una rete di cui ti fidi.\n",
            flush=True,
        )
    # flush esplicito: se stdout non e' un terminale (avvio da .bat, da un IDE,
    # da un lanciatore) Python bufferizza e l'indirizzo non compare finche' il
    # programma non chiude — cioe' proprio quando servirebbe.
    print(
        "\n"
        "  ============================================================\n"
        "   Interfaccia operativa pronta. Apri questo indirizzo:\n"
        "\n"
        f"   {server.url}\n"
        "\n" + nota_token + "   Serve a impedire che un altro programma usi il lettore.\n"
        + nota_rete
        + "\n"
        "   Ctrl+C per chiudere.\n"
        "  ============================================================\n",
        flush=True,
    )

    # L'indirizzo anche su file: se la finestra del terminale viene chiusa per
    # sbaglio, o l'avvio e' avvenuto senza console, resta un posto dove leggerlo.
    percorso_url = Path(log_cfg.get("dir", "logs")) / "webui_url.txt"
    try:
        percorso_url.parent.mkdir(parents=True, exist_ok=True)
        percorso_url.write_text(server.url + "\n", encoding="utf-8")
        print(f"   (l'indirizzo e' anche in {percorso_url})\n", flush=True)
    except OSError as exc:
        print(f"   [avviso] indirizzo non salvato su file: {exc}\n", flush=True)

    if web_cfg.get("open_browser", True) and "--no-browser" not in sys.argv[2:]:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nChiusura…")
    finally:
        server.shutdown()
        percorso_url.unlink(missing_ok=True)
    return 0


def run_service() -> int:
    from rfid_silion import service_host

    args = list(sys.argv[2:])
    if not any(arg == "--config" or arg.startswith("--config=") for arg in args):
        args[:0] = ["--config", str(SRC / "app" / "config.yaml")]
    return service_host.main(args)


def run_service_gui() -> int:
    """GUI di collaudo collegata a un processo service separato."""
    from app import gui
    from rfid_silion import RFIDProcessClient

    cfg_path = SRC / "app" / "config.yaml"
    cfg = gui.load_config(str(cfg_path))
    debug = "--debug" in sys.argv[2:]
    log_cfg = cfg.get("logging", {})
    gui.setup_logging(
        level=log_cfg.get("level", "INFO"),
        log_dir=log_cfg.get("dir", "logs"),
        file_prefix="gui_service",
        force_debug=debug,
    )
    command = [
        sys.executable,
        str(ROOT / "run.py"),
        "service",
        "--config",
        str(cfg_path),
    ]
    if debug:
        command.append("--debug")
    with RFIDProcessClient(
        cfg_path,
        command=command,
        cwd=ROOT,
        timeout_s=30,
    ) as backend:
        gui.launch_gui(
            cfg,
            cfg_path,
            service=backend,
            owns_service=True,
            auto_connect=False,
        )
    return 0


def run_tests() -> int:
    rc = 0
    eseguiti: set = set()
    from tests import (
        test_client,
        test_config_misura,
        test_dense_inventory,
        test_extended_protocol,
        test_gen2_config,
        test_lims_campaign,
        test_lims_codec,
        test_lims_crypto,
        test_lims_db,
        test_lims_end_to_end,
        test_lims_labels,
        test_lims_manifest,
        test_lims_profiler,
        test_lims_reuse,
        test_lims_sealing,
        test_lims_tagio,
        test_lock,
        test_protocol,
        test_reader,
        test_rpc,
        test_select_embedded,
        test_service,
        test_transports,
        test_webui,
    )

    for mod in (
        test_protocol,
        test_reader,
        test_transports,
        test_service,
        test_rpc,
        test_client,
        test_lock,
        test_extended_protocol,
        test_gen2_config,
        test_select_embedded,
        test_dense_inventory,
        test_lims_campaign,
        test_lims_codec,
        test_lims_crypto,
        test_lims_db,
        test_lims_end_to_end,
        test_lims_labels,
        test_lims_manifest,
        test_lims_profiler,
        test_lims_reuse,
        test_lims_sealing,
        test_lims_tagio,
        test_config_misura,
        test_webui,
    ):
        # Un modulo ripetuto per distrazione gonfierebbe il totale dei test
        # superati senza che nulla segnali l'errore.
        if mod in eseguiti:
            continue
        eseguiti.add(mod)
        if hasattr(mod, "_run_all"):
            rc |= mod._run_all()
    return rc


# mapping nomi -> funzioni (definito dopo le funzioni)
ACTIONS = {
    "1": ("Interfaccia operativa (browser)", run_webui),
    "2": ("CLI Step 1 (test read/write)", run_step1),
    "3": ("Test automatici (senza hardware)", run_tests),
    "4": ("Health check lettore (diagnosi rapida)", run_health),
    "5": ("Service JSON-RPC locale", run_service),
    "6": ("GUI collaudo via service", run_service_gui),
    "7": ("Profilazione tag (TID + USER memory)", run_tag_profile),
    "8": ("Tracciabilita' campioni — GUI Tkinter (collaudo)", run_lims),
    "9": ("Campagna di misura della lettura", run_campaign),
    "10": ("GUI di controllo Tkinter (collaudo)", run_gui),
}


def main() -> int:
    ensure_env()
    allow_install = "--install-deps" in sys.argv
    if allow_install:
        sys.argv.remove("--install-deps")
    try:
        ensure_deps(allow_install=allow_install)
    except RuntimeError as e:
        print(f"Errore ambiente: {e}")
        return 2

    # argomento diretto
    direct = {
        "webui": run_webui,
        "web": run_webui,
        "gui": run_gui,
        "step1": run_step1,
        "tests": run_tests,
        "test": run_tests,
        "health": run_health,
        "service": run_service,
        "service-gui": run_service_gui,
        "gui-service": run_service_gui,
        "tag-profile": run_tag_profile,
        "tag_profile": run_tag_profile,
        "lims": run_lims,
        "campaign": run_campaign,
    }
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
