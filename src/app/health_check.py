"""Health check del lettore SIM7200 — diagnosi rapida da CLI.

Esegue in sequenza:
  1. connessione (seriale o TCP, da config.yaml)
  2. boot firmware (0x04) e lettura versioni
  3. verifica antenne fisicamente connesse (0x61/0x05) vs config
  4. (opzionale, --with-inventory) un inventory di prova
e salva un report JSON in logs/health_<timestamp>.json.

Uso:
  python src/app/health_check.py --config src/app/config.yaml
  python src/app/health_check.py --with-inventory   # include inventory di prova
  python src/app/health_check.py --debug            # dump frame TX/RX nei log
  python run.py health                              # equivalente dal launcher

Exit code: 0 = tutto OK, 1 = problemi rilevati, 2 = connessione fallita.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import yaml

# importa il package locale
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rfid_silion import __version__
from rfid_silion.reader import reader_from_config
from rfid_silion.errors import NoTagError
from rfid_silion.diagnostics import setup_logging, checkpoint

log = logging.getLogger("health")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_health_check(cfg: dict, with_inventory: bool = False) -> dict:
    """Esegue il controllo e ritorna il report (serializzabile in JSON).

    `report["ok"]` = esito complessivo; `report["warnings"]` = anomalie non
    bloccanti (es. antenna configurata ma non rilevata).
    """
    report: dict = {
        "started_at": dt.datetime.now().isoformat(timespec="seconds"),
        "driver_version": __version__,
        "ok": False,
        "warnings": [],
    }
    configured_ants = [a["id"] for a in cfg.get("antennas", [])] or [1, 2, 3]

    try:
        with reader_from_config(cfg) as reader:
            report["transport"] = reader._t.describe()
            info = reader.boot_firmware()
            report["firmware_info"] = info

            health = reader.health_check()
            report["health"] = health
            report["ok"] = health["ok"]

            connected = health.get("antennas_connected")
            if connected is not None:
                missing = [a for a in configured_ants if a not in connected]
                if missing:
                    report["warnings"].append(
                        f"Antenne configurate ma non rilevate: {missing}")

            if with_inventory:
                reader.set_antennas_for_inventory(
                    [(a, a) for a in configured_ants])
                try:
                    tags = reader.inventory(
                        timeout_ms=cfg.get("inventory", {}).get("timeout_ms", 1000))
                    report["inventory_test"] = {
                        "tags": len(tags),
                        "epcs": sorted({t.epc for t in tags}),
                    }
                except NoTagError:
                    report["inventory_test"] = {
                        "tags": 0,
                        "note": "nessun tag nel campo (non e' un guasto)",
                    }

            # snapshot finale dei contatori (dopo tutte le operazioni)
            report["counters"] = reader.diag.snapshot()
    except Exception as e:
        log.exception("Health check: connessione/boot falliti")
        report["connection_error"] = str(e)
        report["ok"] = False

    report["finished_at"] = dt.datetime.now().isoformat(timespec="seconds")
    checkpoint(log, "health_check_cli", ok=report["ok"],
               warnings=len(report["warnings"]))
    return report


def _print_summary(report: dict) -> None:
    def mark(ok: bool) -> str:
        return "[OK]  " if ok else "[FAIL]"

    print("\n=== HEALTH CHECK SIM7200 ===")
    print(f"{mark('connection_error' not in report)} Connessione: "
          f"{report.get('transport', report.get('connection_error', '?'))}")
    if "firmware_info" in report:
        fi = report["firmware_info"]
        print(f"{mark(True)} Boot firmware: FW {fi['firmware_version']} "
              f"HW {fi['hardware_version']}")
    health = report.get("health", {})
    if "antennas_connected" in health:
        print(f"{mark(True)} Antenne connesse: {health['antennas_connected']}")
    elif "antennas_error" in health:
        print(f"{mark(False)} Verifica antenne: {health['antennas_error']}")
    for w in report.get("warnings", []):
        print(f"[WARN] {w}")
    if "inventory_test" in report:
        it = report["inventory_test"]
        note = f" ({it['note']})" if "note" in it else ""
        print(f"{mark(True)} Inventory di prova: {it['tags']} tag{note}")
    c = report.get("counters", {})
    if c:
        print(f"       Contatori: comandi={c['commands_sent']} "
              f"ok={c['responses_ok']} timeout={c['timeouts']} "
              f"frame_err={c['frame_errors']} status_err={c['status_errors']}")
    print(f"Esito complessivo: {'OK' if report['ok'] and not report['warnings'] else 'PROBLEMI RILEVATI'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Health check lettore SIM7200")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--with-inventory", action="store_true",
                    help="esegue anche un inventory di prova")
    ap.add_argument("--debug", action="store_true",
                    help="log DEBUG (dump esadecimale frame TX/RX)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_dir = Path(cfg.get("logging", {}).get("dir", "logs"))
    setup_logging(level=cfg.get("logging", {}).get("level", "INFO"),
                  log_dir=log_dir, file_prefix="health",
                  force_debug=args.debug)

    report = run_health_check(cfg, with_inventory=args.with_inventory)
    _print_summary(report)

    out = log_dir / f"health_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False, default=str)
    print(f"Report salvato in: {out}")

    if "connection_error" in report:
        return 2
    return 0 if report["ok"] and not report["warnings"] else 1


if __name__ == "__main__":
    sys.exit(main())
