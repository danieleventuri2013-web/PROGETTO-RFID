"""Profilazione di un tag sconosciuto — CLI.

Risponde alla domanda da cui dipende tutto il resto del progetto: **quanti dati
entrano in questi tag?** Nel progetto non e' mai entrato un datasheet del tag, e
la capacita' dipende dal chip, non dal lettore. Questo comando la misura.

Esegue in sequenza:
  1. avvio del servizio (connessione + boot firmware) e applicazione di regione e potenze
  2. inventory: nel campo deve esserci **un solo tag**
  3. lettura e decodifica del banco TID (costruttore, modello, numero di serie)
  4. misura della USER memory per ricerca binaria
  5. verdetto: il tag e' utilizzabile per portare i dati di un campione?

e salva un report JSON in logs/tag_profile_<timestamp>.json.

**Il valore misurato finisce da solo in `config.yaml`** (`lims.user_memory_bytes`,
piu' un registro `lims.tag_misurato` che dice da dove viene). Trascriverlo a mano
era il passaggio piu' facile da dimenticare, e dimenticarlo si scopre solo quando
la scrittura fallisce a meta' sul primo campione vero.

Vale sempre il tag **peggiore** del lotto: la soglia scende da sola e non sale,
perche' alzarla su un tag piu' capiente renderebbe illeggibili quelli piu'
piccoli gia' misurati. `--forza` fa ripartire il conteggio da questo tag.

Uso:
  python run.py tag-profile
  python run.py tag-profile --non-scrivere      # misura e basta
  python run.py tag-profile --forza             # riparte da questo tag
  python src/app/tag_profile.py --config src/app/config.yaml --debug

Exit code: 0 = tag utilizzabile, 1 = tag non adatto, 2 = profilazione non riuscita.
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
from app.config_misura import Misura, scrivi_misura
from lims.profiler import TagProfile, profile_tag, summarize
from rfid_silion.diagnostics import checkpoint, setup_logging
from rfid_silion.service import AntennaPower, ReaderSettings, RFIDService

log = logging.getLogger("tag_profile")


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _reader_settings(cfg: dict) -> ReaderSettings:
    return ReaderSettings(
        region=cfg.get("reader", {}).get("region", 0x08),
        powers=tuple(
            AntennaPower(
                antenna_id=antenna["id"],
                read_power_cdbm=antenna.get("read_power", 2000),
                write_power_cdbm=antenna.get("write_power", 2000),
            )
            for antenna in cfg.get("antennas", [])
        ),
    )


def run_profile(cfg: dict) -> TagProfile:
    """Avvia il servizio, profila il tag presente e richiude."""
    antenne = tuple(antenna["id"] for antenna in cfg.get("antennas", [])) or (1, 2, 3)
    accesso = cfg.get("tag_access", {})

    # Niente `with RFIDService(...)`: il suo `__enter__` solleva se l'avvio non
    # riesce, e qui un lettore scollegato deve produrre un report leggibile, non
    # una traccia di eccezione.
    service = RFIDService(cfg)
    avvio = service.start()
    if not avvio.ok:
        messaggio = (avvio.error or {}).get("message", "causa non riportata")
        return TagProfile(error=f"avvio del servizio fallito: {messaggio}")

    try:
        configurazione = service.configure(_reader_settings(cfg))
        if not configurazione.ok:
            log.warning(
                "Configurazione di regione/potenze non riuscita: %s",
                (configurazione.error or {}).get("message"),
            )

        return profile_tag(
            service,
            antennas=antenne,
            access_password_hex=accesso.get("access_password_hex", "00000000"),
            timeout_ms=accesso.get("timeout_ms", 1000),
        )
    finally:
        service.stop()


def _print_summary(profile: TagProfile) -> None:
    def mark(ok: bool) -> str:
        return "[OK]  " if ok else "[FAIL]"

    print("\n=== PROFILAZIONE TAG ===")
    if not profile.ok:
        print(f"{mark(False)} {profile.error}")
        return

    print(f"{mark(True)} EPC letto: {profile.epc}")
    if profile.tid is not None:
        print(f"{mark(True)} Chip: {profile.tid.describe()}")
        print(f"       TID: {profile.tid.tid_hex}")
    suffisso_capienza = " o piu'" if profile.user_capped else ""
    print(
        f"{mark(profile.user_bytes > 0)} USER memory: {profile.user_bytes} byte "
        f"({profile.user_words} word){suffisso_capienza}"
    )
    print(f"       Payload utile dopo la cifratura: {profile.usable_payload_bytes} byte")
    print(f"       Letture eseguite: {profile.reads_performed}")
    for avviso in profile.warnings:
        print(f"[WARN] {avviso}")
    print(f"\n{mark(profile.suitable)} {summarize(profile)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Profilazione di un tag UHF sconosciuto")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--debug", action="store_true", help="log DEBUG (dump esadecimale frame TX/RX)")
    ap.add_argument(
        "--non-scrivere",
        action="store_true",
        help="misura e basta, senza aggiornare config.yaml",
    )
    ap.add_argument(
        "--forza",
        action="store_true",
        help="riparte da questo tag anche se ne era gia' stato misurato uno piu' piccolo",
    )
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_dir = Path(cfg.get("logging", {}).get("dir", "logs"))
    setup_logging(
        level=cfg.get("logging", {}).get("level", "INFO"),
        log_dir=log_dir,
        file_prefix="tag_profile",
        force_debug=args.debug,
    )

    print("Posizionare UN SOLO tag davanti alle antenne, poi attendere.")
    profile = run_profile(cfg)
    _print_summary(profile)
    checkpoint(
        log,
        "tag_profile_cli",
        ok=profile.ok,
        user_bytes=profile.user_bytes,
        tier=profile.tier,
        suitable=profile.suitable,
    )

    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / f"tag_profile_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f, indent=2, ensure_ascii=False, default=str)
    print(f"Report salvato in: {out}")

    if profile.ok and not args.non_scrivere:
        _aggiorna_configurazione(args.config, profile, forza=args.forza)

    if not profile.ok:
        return 2
    return 0 if profile.suitable else 1


def _aggiorna_configurazione(percorso: str, profile: TagProfile, *, forza: bool) -> None:
    """Riporta la misura in `config.yaml` e dice cosa e' cambiato.

    Se la memoria e' risultata «tappata» (`user_capped`) il tag ne ha almeno
    tanta quanta ne e' stata letta, ma la ricerca si e' fermata al limite del
    comando: scriverla come misura esatta farebbe passare per certo un numero
    che e' un minimo. Meglio non toccare niente e dirlo.
    """
    if profile.user_capped:
        print(
            f"\n[config] Non aggiornata: la misura si e' fermata al limite del comando "
            f"({profile.user_bytes} byte o piu'), quindi non e' un valore esatto.\n"
            f"         Se serve, scrivere lims.user_memory_bytes a mano in {percorso}."
        )
        return

    misura = Misura(
        user_bytes=profile.user_bytes,
        tid_serializzato=bool(profile.tid and profile.tid.serialized),
        chip=profile.tid.describe() if profile.tid else "",
        epc=profile.epc,
    )
    try:
        esito = scrivi_misura(percorso, misura, forza=forza)
    except OSError as errore:
        print(f"\n[config] Non aggiornata: {errore}")
        return

    if not esito.scritto:
        print(f"\n[config] Non aggiornata: {esito.motivo}")
        return

    cambiato = esito.prima != esito.dopo
    print(f"\n[config] {percorso}")
    print(
        f"         lims.user_memory_bytes: {esito.prima} -> {esito.dopo}"
        if cambiato
        else f"         lims.user_memory_bytes: {esito.dopo} (invariato)"
    )
    print(f"         {esito.motivo}")
    if esito.tag_provati < 3:
        print(
            f"         Tag misurati finora: {esito.tag_provati}. Conviene provarne "
            "tre o quattro dello stesso lotto: se danno risultati diversi il lotto "
            "non e' omogeneo e vale il peggiore."
        )


if __name__ == "__main__":
    sys.exit(main())
