"""Campagna di misura della lettura — CLI.

Tara la configurazione radio sui dati invece che a sentimento, misurando due
cose insieme:

* quanto si legge di cio' che sta **dentro** il contenitore;
* quanto si legge di cio' che sta **fuori** e non deve essere contato.

Procedura consigliata:

1. si carica il contenitore con i campioni veri, chiuso come in esercizio;
2. si posizionano di proposito alcuni **tag di controllo fuori**, alle distanze
   che si vogliono escludere: sul banco accanto, sotto il piano, nella scatola
   successiva;
3. si lancia questo comando, che spazza la griglia potenza x sessione x RF mode
   x antenne e misura ogni combinazione;
4. si prende la configurazione consigliata e la si riporta in `config.yaml`.

La configurazione consigliata **non** e' quella che legge di piu': e' la potenza
piu' bassa che legge tutto il contenuto senza leggere niente di esterno. Alzare
la potenza finche' "si vede tutto" e' il modo classico di costruire un sistema
che conta contenitori che non ci sono.

Lo stesso comando e lo stesso formato di report servono in prototipazione e al
collaudo dal cliente: e' quello che rende confrontabili due installazioni.

Uso:
  python run.py campaign --inside-from-shipment 3 --outside AABB... CCDD...
  python run.py campaign --inside EPC1 EPC2 --outside EPC3 --cycles 20

Exit code: 0 = trovata una configurazione utilizzabile, 1 = nessuna, 2 = errore.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lims.campaign import CampaignConfig, CampaignReport, ReadCampaign, grid_passes
from lims.db import LimsDatabase
from rfid_silion.diagnostics import checkpoint, setup_logging
from rfid_silion.service import ReaderTuning, RFIDService

log = logging.getLogger("campaign")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _inside_from_shipment(cfg: dict, shipment_id: int) -> list[str]:
    """EPC attesi presi dalla distinta di una spedizione gia' registrata."""
    lims_cfg = cfg.get("lims", {}) or {}
    with LimsDatabase(lims_cfg.get("database", "logs/lims.db")) as db:
        contenuto = db.shipment_contents(shipment_id)
    if not contenuto:
        raise SystemExit(f"la spedizione {shipment_id} non contiene contenitori registrati")
    return [record.epc for record in contenuto if record.epc]


def run_campaign(cfg: dict, args: argparse.Namespace) -> CampaignReport:
    lims_cfg = cfg.get("lims", {}) or {}
    antenne = tuple(lims_cfg.get("read_antennas") or [a["id"] for a in cfg.get("antennas", [])])
    if not antenne:
        raise SystemExit("nessuna antenna di lettura configurata")

    dentro = list(args.inside or [])
    if args.inside_from_shipment:
        dentro += _inside_from_shipment(cfg, args.inside_from_shipment)
    if not dentro:
        raise SystemExit(
            "indicare i tag dentro il contenitore con --inside oppure "
            "--inside-from-shipment"
        )

    geometria = None
    if args.container_mm:
        geometria = tuple(int(v) for v in args.container_mm)

    service = RFIDService(cfg)
    avvio = service.start()
    if not avvio.ok:
        raise SystemExit(
            f"avvio del servizio fallito: {(avvio.error or {}).get('message', '?')}"
        )
    try:
        # Prima di misurare qualsiasi cosa: togliere di mezzo le impostazioni che
        # falserebbero il risultato. Un filtro RSSI ereditato scarterebbe proprio
        # i tag deboli che la campagna deve trovare, e il risparmio energetico
        # spegne la radio fra un comando e l'altro.
        igiene = service.tune_reader(
            ReaderTuning(power_mode=0, disable_rssi_filter=True, max_rssi_reporting=True)
        )
        if not igiene.ok:
            log.warning(
                "Impostazioni preliminari non applicate: %s",
                (igiene.error or {}).get("message"),
            )

        campagna = ReadCampaign(
            service,
            inside_epcs=dentro,
            outside_epcs=args.outside or (),
            config=CampaignConfig(
                cycles_per_configuration=args.cycles,
                timeout_ms=args.timeout_ms,
                region=cfg.get("reader", {}).get("region", 0x08),
                container_mm=geometria,
                notes=args.notes,
            ),
        )
        griglia = grid_passes(
            antenne,
            powers_cdbm=tuple(args.powers),
            sessions=tuple(args.sessions),
            rf_modes=(None, 0x71) if args.high_sensitivity else (None,),
        )
        print(
            f"{len(griglia)} configurazioni x {args.cycles} cicli — "
            f"antenne {list(antenne)}, {len(dentro)} tag dentro, "
            f"{len(args.outside or [])} di controllo fuori\n"
        )

        def avanzamento(indice: int, totale: int, risultato) -> None:
            fuga = "" if risultato.clean else f"  FUGA {risultato.outside_leaked}"
            print(
                f"[{indice:3d}/{totale}] {risultato.label:44s} "
                f"dentro {risultato.inside_found}/{risultato.inside_expected}"
                f"{fuga}"
            )

        return campagna.run(griglia, on_progress=avanzamento)
    finally:
        service.stop()


def _print_summary(report: CampaignReport) -> None:
    print("\n=== CAMPAGNA DI MISURA ===")
    if report.error:
        print(f"[FAIL] {report.error}")
    print(f"Configurazioni provate:      {len(report.results)}")
    print(f"Configurazioni utilizzabili: {len(report.usable)}")

    migliore = report.best()
    if migliore is None:
        print(
            "\n[FAIL] Nessuna configurazione legge tutto il contenuto senza leggere\n"
            "       anche i tag esterni. Le leve rimaste sono fisiche, non software:\n"
            "       posizione delle antenne, distanziatori fra i campioni, schermatura\n"
            "       del banco, oppure antenne adatte alla banda EU."
        )
    else:
        print("\n[OK]   Configurazione consigliata:")
        print(f"       {migliore.label}")
        print(
            f"       dentro {migliore.inside_found}/{migliore.inside_expected} "
            f"(tasso medio {migliore.inside_detection_rate:.2f}), "
            f"fughe {migliore.outside_leaked}"
        )
        print(f"       {migliore.elapsed_s:.1f} s per {migliore.cycles} cicli")

    difficili = report.hardest_tags()
    if difficili:
        print("\nTag piu' difficili (media su tutte le configurazioni):")
        for epc, tasso in difficili:
            nota = "  <-- rivedere posizione o tag" if tasso < 0.5 else ""
            print(f"       {epc}  {tasso:.2f}{nota}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Campagna di misura della lettura RFID")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--inside", nargs="*", help="EPC dei tag dentro il contenitore")
    ap.add_argument(
        "--inside-from-shipment",
        type=int,
        metavar="ID",
        help="prende i tag dentro dalla distinta di una spedizione registrata",
    )
    ap.add_argument(
        "--outside",
        nargs="*",
        help="EPC dei tag di controllo posizionati FUORI dal contenitore",
    )
    ap.add_argument("--cycles", type=int, default=10, help="cicli per configurazione")
    ap.add_argument("--timeout-ms", type=int, default=500)
    ap.add_argument(
        "--powers",
        nargs="*",
        type=int,
        default=[1500, 2000, 2500, 3000],
        help="potenze da provare in centesimi di dBm",
    )
    ap.add_argument("--sessions", nargs="*", type=int, default=[0, 2])
    ap.add_argument(
        "--high-sensitivity",
        action="store_true",
        help="prova anche la modalita' RF a -93 dBm (lenta)",
    )
    ap.add_argument(
        "--container-mm",
        nargs=3,
        type=int,
        metavar=("L", "W", "H"),
        help="dimensioni del contenitore, riportate nel report",
    )
    ap.add_argument("--notes", default="", help="nota libera sul montaggio provato")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_dir = Path(cfg.get("logging", {}).get("dir", "logs"))
    setup_logging(
        level=cfg.get("logging", {}).get("level", "INFO"),
        log_dir=log_dir,
        file_prefix="campaign",
        force_debug=args.debug,
    )

    if not args.outside:
        print(
            "AVVISO: nessun tag di controllo esterno indicato con --outside.\n"
            "        Senza, la campagna misura solo quanto si legge, non quanto\n"
            "        si legge di troppo, e sceglierebbe sempre la potenza massima.\n"
        )

    report = run_campaign(cfg, args)
    _print_summary(report)
    checkpoint(
        log,
        "read_campaign",
        configurations=len(report.results),
        usable=len(report.usable),
        best=report.best().label if report.best() else None,
    )

    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / f"campaign_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False, default=str)
    print(f"\nReport salvato in: {out}")

    if report.error:
        return 2
    return 0 if report.best() is not None else 1


if __name__ == "__main__":
    sys.exit(main())
