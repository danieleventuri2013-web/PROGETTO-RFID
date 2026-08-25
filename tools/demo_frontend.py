"""Avvia due postazioni WebUI isolate per la dimostrazione RIXLAB.

Il runner usa il backend RFID simulato gia' impiegato dai test.  Tutto cio' che
crea (database, chiavi, certificati, distinte e log) resta nella cartella
``demo-output`` indicata a riga di comando.  Non apre connessioni PEC e non
legge la configurazione operativa del progetto.

Esempi::

    python tools/demo_frontend.py serve --output demo-output/rixlab_frontend_demo
    python tools/demo_frontend.py campo --session .../demo_session.json \
        --postazione mittente --tag 1
    python tools/demo_frontend.py esporta --session .../demo_session.json
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import signal
import sys
import threading
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = SRC / "tests"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(TESTS))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

from fake_backend import FakeTagBackend, SimulatedTag  # noqa: E402
from lims.pec import PecReceipt, PecSendResult  # noqa: E402
from webui.server import WebUIServer  # noqa: E402

NOME_MITTENTE = "Unita Locale Demo"
CODICE_MITTENTE = "UL-DEMO"
NOME_DESTINATARIO = "Anatomia Patologica - Ospedale Regionale Demo"
CODICE_DESTINATARIO = "AP-DEMO"
OPERATORE = "OP-DEMO"
PEC_MITTENTE = "ul-demo@pec.invalid"
PEC_DESTINATARIO = "ap-demo@pec.invalid"
TID_BASE = bytes.fromhex("E2801190200050A1B2C300")


def _scrivi_pem(path: Path, value: Any, *, private: bool = False) -> None:
    if private:
        raw = value.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    else:
        raw = value.public_bytes(serialization.Encoding.PEM)
    path.write_bytes(raw)


def _crea_pki(cartella: Path) -> dict[str, Path]:
    """Crea una PKI effimera valida soltanto per questa esecuzione demo."""
    cartella.mkdir(parents=True, exist_ok=True)
    adesso = dt.datetime.now(dt.timezone.utc)
    ca_key = ed25519.Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CA DEMO RFID")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(adesso - dt.timedelta(days=1))
        .not_valid_after(adesso + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, algorithm=None)
    )

    station_key = ed25519.Ed25519PrivateKey.generate()
    station_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CODICE_MITTENTE)])
    station_cert = (
        x509.CertificateBuilder()
        .subject_name(station_name)
        .issuer_name(ca_name)
        .public_key(station_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(adesso - dt.timedelta(days=1))
        .not_valid_after(adesso + dt.timedelta(days=7))
        .sign(ca_key, algorithm=None)
    )

    recipient_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    recipient_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, CODICE_DESTINATARIO)]
    )
    recipient_cert = (
        x509.CertificateBuilder()
        .subject_name(recipient_name)
        .issuer_name(ca_name)
        .public_key(recipient_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(adesso - dt.timedelta(days=1))
        .not_valid_after(adesso + dt.timedelta(days=7))
        .sign(ca_key, algorithm=None)
    )

    paths = {
        "ca": cartella / "ca-demo.pem",
        "station_cert": cartella / "mittente-demo.pem",
        "station_key": cartella / "mittente-demo-private.pem",
        "recipient_cert": cartella / "destinatario-demo.pem",
        "recipient_key": cartella / "destinatario-demo-private.pem",
    }
    _scrivi_pem(paths["ca"], ca_cert)
    _scrivi_pem(paths["station_cert"], station_cert)
    _scrivi_pem(paths["station_key"], station_key, private=True)
    _scrivi_pem(paths["recipient_cert"], recipient_cert)
    _scrivi_pem(paths["recipient_key"], recipient_key, private=True)
    return paths


def _tag_vergine(indice: int) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0, indice]),
        tid=TID_BASE + bytes([indice]),
        user_bytes=64,
        rssi=-42 - indice,
    )


def _base_config(cartella: Path, *, lab_id: int) -> dict[str, Any]:
    return {
        "reader": {"region": 0x08},
        "antennas": [
            {"id": 1, "role": "base sinistra", "read_power": 2900, "write_power": 2000},
            {"id": 2, "role": "base destra", "read_power": 2900, "write_power": 2000},
            {"id": 3, "role": "parete verticale", "read_power": 2000, "write_power": 2000},
        ],
        "geometry": {
            "volume_mm": [440, 220, 220],
            "antenna_size_mm": [220, 220, 24],
            "antenna_positions_mm": [
                {"id": 1, "center": [-110, 0, 0], "normal": [0, 0, 1]},
                {"id": 2, "center": [110, 0, 0], "normal": [0, 0, 1]},
                {"id": 3, "center": [220, 0, 110], "normal": [-1, 0, 0]},
            ],
        },
        "tag_access": {"access_password_hex": "00000000", "timeout_ms": 250},
        "lims": {
            "lab_id": lab_id,
            "database": str(cartella / "lims-demo.db"),
            "keyring": str(cartella / "keys-demo.json"),
            "manifest_archive": str(cartella / "archivio-distinte"),
            "user_memory_bytes": 64,
            "write_antennas": [3],
            "read_antennas": [1, 2],
            "seal_min_antennas": 1,
            "seal_powers_cdbm": [2000, 2900],
        },
        "operatori": [
            {"codice": OPERATORE, "cognome": "Operatore Demo", "ruolo": "operatore"}
        ],
        "webui": {"host": "127.0.0.1", "port": 0, "theme": "chiaro"},
    }


def _sender_config(cartella: Path, pki: Mapping[str, Path]) -> dict[str, Any]:
    config = _base_config(cartella, lab_id=101)
    config.update(
        {
            "laboratorio": {
                "nome": NOME_MITTENTE,
                "codice": CODICE_MITTENTE,
                "citta": "Regione Demo",
                "referente": "Referente Demo",
            },
            "destinatari": [
                {
                    "nome": NOME_DESTINATARIO,
                    "codice": CODICE_DESTINATARIO,
                    "pec": PEC_DESTINATARIO,
                    "email": "laboratorio@ospedale.invalid",
                    "citta": "Capoluogo Demo",
                    "referente": "Ricezione campioni",
                    "encryption_certificate": str(pki["recipient_cert"]),
                }
            ],
            "security": {
                "station_certificate": str(pki["station_cert"]),
                "station_private_key": str(pki["station_key"]),
            },
            "pec": {
                "enabled": True,
                "sender": PEC_MITTENTE,
                "username": PEC_MITTENTE,
                "smtp_host": "smtp.pec.invalid",
                "imap_host": "imap.pec.invalid",
            },
        }
    )
    return config


def _receiver_config(cartella: Path, pki: Mapping[str, Path]) -> dict[str, Any]:
    config = _base_config(cartella, lab_id=202)
    # La chiave dei payload RFID viene distribuita ai laboratori autorizzati
    # fuori banda, una volta sola. Nella demo le due postazioni condividono lo
    # stesso file effimero per riprodurre quella prerequisito senza usare chiavi
    # operative reali.
    config["lims"]["keyring"] = str(cartella.parent / "mittente" / "keys-demo.json")
    config.update(
        {
            "laboratorio": {
                "nome": NOME_DESTINATARIO,
                "codice": CODICE_DESTINATARIO,
                "citta": "Capoluogo Demo",
            },
            "security": {
                "recipient_certificate": str(pki["recipient_cert"]),
                "recipient_private_key": str(pki["recipient_key"]),
                "trusted_ca": str(pki["ca"]),
            },
        }
    )
    return config


class _PecDemo:
    """PEC in memoria: produce messaggio e ricevute, ma non usa la rete."""

    def __init__(self) -> None:
        self.message_id = "<distinta-demo@pec.invalid>"
        self.sent = False

    def send_manifest(self, **dati: Any) -> PecSendResult:
        messaggio = EmailMessage()
        messaggio["From"] = PEC_MITTENTE
        messaggio["To"] = PEC_DESTINATARIO
        messaggio["Message-ID"] = self.message_id
        messaggio["Subject"] = f"Distinta DEMO {dati['manifest_uuid']}"
        messaggio.set_content(
            "Messaggio dimostrativo: contiene soltanto la distinta cifrata allegata."
        )
        messaggio.add_attachment(
            bytes(dati["blob"]),
            maintype="application",
            subtype="octet-stream",
            filename=str(dati["filename"]),
        )
        self.sent = True
        return PecSendResult(self.message_id, messaggio.as_bytes())

    def fetch_receipts(self) -> list[PecReceipt]:
        if not self.sent:
            return []
        return [
            PecReceipt(
                "accettazione",
                self.message_id,
                b"Ricevuta PEC DEMO di accettazione",
                b'<postacert tipo="accettazione"/>',
            ),
            PecReceipt(
                "avvenuta-consegna",
                self.message_id,
                b"Ricevuta PEC DEMO di avvenuta consegna",
                b'<postacert tipo="avvenuta-consegna"/>',
            ),
        ]


class DemoRuntime:
    def __init__(self, output: Path):
        self.output = output.resolve()
        self.runtime = self.output / "_runtime"
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.pki = _crea_pki(self.runtime / "pki")
        self.tags = [_tag_vergine(1), _tag_vergine(2)]
        self.sender_backend = FakeTagBackend([], antennas=(1, 2, 3))
        self.receiver_backend = FakeTagBackend([], antennas=(1, 2, 3))
        self.sender = WebUIServer(
            _sender_config(self.runtime / "mittente", self.pki),
            self.sender_backend,
            host="127.0.0.1",
            port=0,
        )
        self.receiver = WebUIServer(
            _receiver_config(self.runtime / "destinatario", self.pki),
            self.receiver_backend,
            host="127.0.0.1",
            port=0,
        )
        self.pec = _PecDemo()
        self.sender.workflow._pec_transport = lambda: self.pec  # type: ignore[method-assign]
        self._aggiungi_controlli(self.sender, self.sender_backend, "mittente")
        self._aggiungi_controlli(self.receiver, self.receiver_backend, "destinatario")

    def _aggiungi_controlli(
        self, server: WebUIServer, backend: FakeTagBackend, nome: str
    ) -> None:
        def campo(dati: Mapping[str, Any]) -> dict[str, Any]:
            indici = {int(v) for v in dati.get("tag", [])}
            non_validi = indici - {1, 2}
            if non_validi:
                raise ValueError(f"tag demo non validi: {sorted(non_validi)}")
            backend.tags[:] = [self.tags[i - 1] for i in sorted(indici)]
            backend.observed_epcs = frozenset()
            return {
                "postazione": nome,
                "tag_nel_campo": sorted(indici),
                "epc": [tag.epc_hex for tag in backend.tags],
            }

        server._operations["demo_campo"] = (campo, False)  # noqa: SLF001

    def start(self) -> Path:
        self.sender.workflow.imposta_operatore(OPERATORE)
        self.receiver.workflow.imposta_operatore(OPERATORE)
        self.sender.start_background()
        self.receiver.start_background()
        session = {
            "demo": True,
            "dati_fittizi": True,
            "mittente": {
                "url": self.sender.url,
                "base": f"http://127.0.0.1:{self.sender.port}",
                "token": self.sender.token,
            },
            "destinatario": {
                "url": self.receiver.url,
                "base": f"http://127.0.0.1:{self.receiver.port}",
                "token": self.receiver.token,
            },
            "distinta": str(self.output / "distinta_demo.rfidman"),
            "scenario": {
                "mittente": NOME_MITTENTE,
                "destinatario": NOME_DESTINATARIO,
                "operatore": OPERATORE,
                "paziente": "PAZIENTE DEMO",
                "codice_fiscale": "DMODMO80A01H501B",
                "contenitori": 2,
            },
        }
        path = self.output / "demo_session.json"
        path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def stop(self) -> None:
        self.sender.shutdown()
        self.receiver.shutdown()


def _session(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _post(base: str, token: str, operation: str, data: Mapping[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base}/api/{operation}",
        data=json.dumps(data).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "X-RFID-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{operation}: HTTP {exc.code}: {detail}") from exc


def _download(base: str, token: str, path: Path) -> None:
    request = urllib.request.Request(
        f"{base}/api/distinta",
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json", "X-RFID-Token": token},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        path.write_bytes(response.read())


def _command_serve(args: argparse.Namespace) -> int:
    runtime = DemoRuntime(args.output)
    session = runtime.start()
    print(json.dumps({"pronto": True, "sessione": str(session)}, ensure_ascii=False), flush=True)
    stop = threading.Event()

    def termina(*_args: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, termina)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, termina)
    try:
        stop.wait()
    finally:
        runtime.stop()
    return 0


def _command_field(args: argparse.Namespace) -> int:
    session = _session(args.session)
    station = session[args.postazione]
    result = _post(station["base"], station["token"], "demo_campo", {"tag": args.tag})
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _command_export(args: argparse.Namespace) -> int:
    session = _session(args.session)
    sender = session["mittente"]
    path = Path(session["distinta"])
    _download(sender["base"], sender["token"], path)
    print(json.dumps({"distinta": str(path), "byte": path.stat().st_size}, ensure_ascii=False))
    return 0


def _command_import(args: argparse.Namespace) -> int:
    session = _session(args.session)
    receiver = session["destinatario"]
    path = Path(session["distinta"])
    result = _post(
        receiver["base"],
        receiver["token"],
        "importa_distinta",
        {"contenuto_base64": base64.b64encode(path.read_bytes()).decode("ascii")},
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="avvia le due postazioni demo")
    serve.add_argument("--output", type=Path, required=True)
    serve.set_defaults(func=_command_serve)

    field = commands.add_parser("campo", help="sceglie i tag virtuali nel campo RF")
    field.add_argument("--session", type=Path, required=True)
    field.add_argument(
        "--postazione", choices=("mittente", "destinatario"), required=True
    )
    field.add_argument("--tag", type=int, action="append", default=[])
    field.set_defaults(func=_command_field)

    export = commands.add_parser("esporta", help="salva localmente la distinta demo")
    export.add_argument("--session", type=Path, required=True)
    export.set_defaults(func=_command_export)

    import_ = commands.add_parser("importa", help="importa la distinta nel destinatario")
    import_.add_argument("--session", type=Path, required=True)
    import_.set_defaults(func=_command_import)
    return root


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
