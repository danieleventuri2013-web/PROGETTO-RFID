"""Step 1 - Test di lettura/scrittura RFID nel parallelepipedo (3 antenne).

Esegue la sequenza:
  1. connessione + boot firmware
  2. set regione EU
  3. set potenze antenne 1,2,3
  4. diagnostica connessione antenne
  5. inventory sincrono (antenne 1,2,3) + report tag/antenna/RSSI
  6. lettura banca USER su ciascuna antenna
  7. scrittura banca USER + rilettura di verifica
  8. (opzionale) scrittura EPC
  9. salva report JSON in logs/ (include i contatori diagnostici)

Uso:
  python -m app.step1_test_rw --config src/app/config.yaml
  Opzioni: --skip-write --skip-epc --debug

Exit code: 0 = completato, 1 = errore fatale (vedi campo "error" nel report).
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
from rfid_silion.reader import reader_from_config
from rfid_silion import protocol as P  # noqa: F401  (banche/comandi da config)
from rfid_silion.errors import SilionError
from rfid_silion.diagnostics import setup_logging, checkpoint


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def hex2bytes(h: str) -> bytes:
    return bytes.fromhex(h)


def main() -> int:
    ap = argparse.ArgumentParser(description="Step 1 - test RFID parallelepipedo")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--skip-write", action="store_true",
                    help="non scrivere sul tag (solo lettura)")
    ap.add_argument("--skip-epc", action="store_true",
                    help="non scrivere l'EPC")
    ap.add_argument("--debug", action="store_true",
                    help="log DEBUG (dump esadecimale frame TX/RX)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_dir = Path(cfg.get("logging", {}).get("dir", "logs"))
    setup_logging(level=cfg.get("logging", {}).get("level", "INFO"),
                  log_dir=log_dir, file_prefix="step1",
                  force_debug=args.debug)
    log = logging.getLogger("step1")

    r = cfg["reader"]
    ants_cfg = cfg["antennas"]
    inv = cfg["inventory"]
    ta = cfg["tag_access"]
    antennas = [a["id"] for a in ants_cfg]

    report = {
        "started_at": dt.datetime.now().isoformat(),
        "config": cfg,
        "steps": [],
    }

    reader = None
    try:
        with reader_from_config(cfg) as reader:

            # 1. boot
            log.info("=== 1. Boot firmware ===")
            info = reader.boot_firmware()
            report["steps"].append({"step": "boot", "ok": True, "info": info})

            # 2. region
            log.info("=== 2. Set region 0x%02X ===", r["region"])
            reader.set_region(r["region"])
            report["steps"].append({"step": "set_region", "ok": True})

            # 3. potenze antenne
            log.info("=== 3. Set potenze antenne ===")
            powers = [(a["id"], a["read_power"], a["write_power"]) for a in ants_cfg]
            reader.set_antennas_power(powers)
            report["steps"].append({"step": "set_power", "ok": True, "powers": powers})

            # 4. connessione antenne
            log.info("=== 4. Diagnostica connessione antenne ===")
            try:
                connected = reader.get_antenna_connection()
                report["steps"].append({"step": "antenna_connection",
                                        "ok": True, "connected": connected})
                missing = [a for a in antennas if a not in connected]
                if missing:
                    log.warning("Antenne non rilevate come connesse: %s", missing)
            except SilionError as e:
                report["steps"].append({"step": "antenna_connection",
                                        "ok": False, "error": str(e)})
                log.warning("Get antenna connection non supportato: %s", e)

            # 5. inventory
            log.info("=== 5. Inventory sincrono (antenne %s) ===", antennas)
            reader.set_antennas_for_inventory([(a, a) for a in antennas])
            tags = reader.inventory(timeout_ms=inv["timeout_ms"],
                                    metadata_flags=inv["metadata_flags"])
            report["steps"].append({
                "step": "inventory",
                "ok": True,
                "tag_count": len(tags),
                "tags": [t.__dict__ for t in tags],
            })
            print(f"\n--- Inventory: {len(tags)} tag ---")
            for t in tags:
                print(f"  {t}")

            if not tags:
                log.warning("Nessun tag rilevato. Verificare la presenza di un tag "
                            "nel parallelepipedo e le potenze.")
                report["steps"].append({"step": "no_tag", "ok": False})

            # 6. lettura USER
            log.info("=== 6. Lettura banca USER (addr 0, %d word) ===", ta["read_user_words"])
            pwd = hex2bytes(ta["access_password_hex"])
            read_res = reader.read_try_all_antennas(
                antennas, ta["bank_user"], 0, ta["read_user_words"], pwd,
                ta["timeout_ms"])
            report["steps"].append({"step": "read_user", "result": _ser(read_res)})
            _print_antenna_results("READ USER", read_res)

            # 7. scrittura USER + verifica
            if not args.skip_write:
                wdata = hex2bytes(ta["write_data_hex"])
                log.info("=== 7. Scrittura banca USER: %s ===", ta["write_data_hex"])
                write_res = reader.write_try_all_antennas(
                    antennas, ta["bank_user"], 0, wdata, pwd, ta["timeout_ms"])
                report["steps"].append({"step": "write_user", "result": _ser(write_res)})
                _print_antenna_results("WRITE USER", write_res)

                log.info("=== 7b. Rilettura di verifica ===")
                verify = reader.read_try_all_antennas(
                    antennas, ta["bank_user"], 0, len(wdata) // 2, pwd,
                    ta["timeout_ms"])
                report["steps"].append({"step": "verify_user",
                                        "result": _ser(verify)})
                _print_antenna_results("VERIFY USER", verify)
                # confronto
                expected = wdata.hex().upper()
                for ant, res in verify.items():
                    if res["ok"]:
                        got = res["data"].hex().upper()
                        res["match"] = (got == expected)
                        log.info("Ant %d: scritto=%s letto=%s match=%s",
                                 ant, expected, got, res["match"])

            # 8. scrittura EPC (opzionale)
            if not args.skip_epc and tags:
                # incrementa ultimo byte dell'EPC corrente per test
                cur = bytes.fromhex(tags[0].epc)
                new_epc = bytearray(cur)
                new_epc[-1] = (new_epc[-1] + 1) & 0xFF
                log.info("=== 8. Write EPC -> %s ===", new_epc.hex().upper())
                try:
                    reader.write_tag_epc(bytes(new_epc), pwd, ta["timeout_ms"])
                    report["steps"].append({"step": "write_epc", "ok": True,
                                            "new_epc": new_epc.hex().upper()})
                except SilionError as e:
                    report["steps"].append({"step": "write_epc", "ok": False,
                                            "error": str(e)})
                    log.warning("Write EPC fallita: %s", e)

    except Exception as e:
        log.exception("Errore fatale")
        report["error"] = str(e)
    finally:
        if reader is not None:
            # contatori diagnostici della sessione (timeout, CRC, status err.)
            report["diagnostics"] = reader.diag.snapshot()
        report["finished_at"] = dt.datetime.now().isoformat()
        out = log_dir / f"step1_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False, default=str)
        checkpoint(log, "step1_done", ok=("error" not in report),
                   report=out.name)
        print(f"\nReport salvato in: {out}")
    return 1 if "error" in report else 0


def _ser(d: dict) -> dict:
    """Serializza i risultati renderizzando bytes in hex."""
    out = {}
    for k, v in d.items():
        if v.get("ok") and "data" in v:
            out[k] = {"ok": True, "data_hex": v["data"].hex().upper()}
        else:
            out[k] = v
    return out


def _print_antenna_results(title: str, results: dict) -> None:
    print(f"\n--- {title} ---")
    for ant, res in results.items():
        if res["ok"]:
            data_hex = res.get("data", b"").hex().upper() if "data" in res else ""
            print(f"  Ant {ant}: OK {data_hex}")
        else:
            print(f"  Ant {ant}: FAIL - {res.get('error')}")


if __name__ == "__main__":
    sys.exit(main())
