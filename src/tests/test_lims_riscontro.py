"""Il verbale di riscontro e il riepilogo del transito.

Fino a qui il mittente sapeva una cosa sola: di aver spedito. Le ricevute PEC
provano che il documento e' arrivato, non che le provette ci siano. Le promesse
che questi test tengono ferme:

* un verbale si esporta **solo dopo** che la scatola e' stata letta e la
  ricezione confermata: certificare un arrivo mai controllato sarebbe peggio
  che non certificarlo;
* il verbale e' cifrato e autenticato come la distinta, e con le stesse
  chiavi: un file manomesso non entra;
* un verbale che parla di **un'altra spedizione** viene respinto;
* «tutto a buon fine» significa una cosa sola — ogni spedizione del periodo ha
  un verbale che dice che e' arrivata intera. Spedito e mai confermato si
  chiama **non confermato**, mai «arrivato».
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend, SimulatedTag

from lims.crypto import Keyring, PayloadAuthenticationError, PayloadFormatError, UnknownKeyError
from lims.riscontro import (
    Riscontro,
    apri_riscontro,
    costruisci_riscontro,
    sigilla_riscontro,
)
from webui.workflow import Workflow, WorkflowError

CF_UNO = "MRTMTT25D09F205Z"
CF_DUE = "RSSMRA80A01H501U"


# ---------------------------------------------------------------------------
# Attrezzatura: due laboratori, ognuno col suo archivio
# ---------------------------------------------------------------------------
def _tmp(nome: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=f"riscontro_{nome}_"))


def _config(cartella: Path, *, lab_id: int, nome: str, codice: str) -> dict:
    return {
        "reader": {"region": 0x08},
        "antennas": [
            {"id": 1, "read_power": 2900, "write_power": 2000},
            {"id": 2, "read_power": 2900, "write_power": 2000},
            {"id": 3, "read_power": 2000, "write_power": 2000},
        ],
        "tag_access": {"access_password_hex": "00000000", "timeout_ms": 1000},
        "laboratorio": {"nome": nome, "codice": codice},
        "lims": {
            "lab_id": lab_id,
            "database": str(cartella / "lims.db"),
            # Stesso portachiavi: e' la chiave del circuito, concordata fuori
            # banda una volta sola. Senza, i due non si leggono i documenti.
            "keyring": str(cartella.parent / "circuito.json"),
            "user_memory_bytes": 64,
            "write_antennas": [3],
            "read_antennas": [1, 2],
            "seal_min_antennas": 1,
            "seal_powers_cdbm": [2000, 2900],
        },
        "destinatari": [{"nome": "Ospedale Foligno", "codice": "OF", "attivo": True}],
    }


def _tag(indice: int) -> SimulatedTag:
    return SimulatedTag(
        bytes.fromhex("AAAAAAAAAAAAAAAAAAAA") + bytes([0x00, indice]),
        tid=bytes.fromhex("E2801190200050A1B2C300") + bytes([indice]),
        user_bytes=64,
    )


class _Circuito:
    """Due laboratori che si scambiano una scatola, sullo stesso banco simulato."""

    def __init__(self, radice: Path, quanti: int = 2):
        self.tag = [_tag(indice) for indice in range(1, quanti + 1)]
        self.backend_mittente = FakeTagBackend(list(self.tag), antennas=(1, 2, 3))
        self.backend_destinatario = FakeTagBackend([], antennas=(1, 2, 3))
        self.mittente = Workflow(
            _config(radice / "spoleto", lab_id=1, nome="Ospedale Spoleto", codice="OSP-S"),
            self.backend_mittente,
        )
        self.destinatario = Workflow(
            _config(radice / "foligno", lab_id=2, nome="Ospedale Foligno", codice="OF"),
            self.backend_destinatario,
        )
        self.mittente.imposta_operatore("SPOLETO")
        self.destinatario.imposta_operatore("FOLIGNO")

    def chiudi(self) -> None:
        self.mittente.chiudi()
        self.destinatario.chiudi()

    # -- i gesti ----------------------------------------------------------
    def spedisci(self, quanti: int = 2) -> dict:
        """Scrive, riempie, sigilla ed esporta la distinta."""
        for indice in range(quanti):
            self.mittente.registra_accettazione(
                {
                    "codice_fiscale": CF_UNO if indice == 0 else CF_DUE,
                    "cognome": "Della Valle" if indice == 0 else "Rossi",
                    "nome": "Prova",
                    "sesso": "M",
                    "contenitori": 1,
                    "descrizione": "pezzo operatorio",
                }
            )
            self.backend_mittente.tags[:] = [self.tag[indice]]
            esito = self.mittente.scrivi_prossimo()
            assert esito["scrittura"]["ok"], esito["scrittura"]
            self.mittente.nuova_accettazione()

        self.backend_mittente.tags.clear()
        self.mittente.avvia_riempimento("Ospedale Foligno")
        self.backend_mittente.tags[:] = self.tag[:quanti]
        self.mittente.sorveglia_scatola()
        self.mittente.chiudi_riempimento()
        record = self.mittente.sigilla()
        assert record["sigillo"]["ok"], record["sigillo"]
        blob, _ = self.mittente.esporta_distinta()
        self.mittente.conferma_invio()
        return {"shipment_id": self.mittente.shipment_id, "distinta": blob}

    def ricevi(self, distinta: bytes, *, dentro: list | None = None, motivo: str = ""):
        """Importa la distinta, legge la scatola e conferma."""
        self.destinatario.importa_distinta(distinta)
        self.backend_destinatario.tags[:] = self.tag if dentro is None else dentro
        self.destinatario.leggi_volume()
        self.destinatario.conferma_ricezione(motivo)


# ---------------------------------------------------------------------------
# Il giro completo
# ---------------------------------------------------------------------------
def test_il_verbale_chiude_il_giro():
    """Il mittente scopre com'e' andata: e' l'unico modo che ha di saperlo."""
    circuito = _Circuito(_tmp("giro"))
    try:
        spedizione = circuito.spedisci()
        identificativo = spedizione["shipment_id"]

        # Prima del verbale il mittente sa solo di aver spedito.
        prima = circuito.mittente.riepilogo_transito("Ospedale Foligno")
        assert prima["totali"]["confermate"] == 0
        assert prima["spedizioni"][0]["esito"] == "non confermata"
        assert prima["tutto_a_buon_fine"] is False

        circuito.ricevi(spedizione["distinta"])
        blob, nome = circuito.destinatario.esporta_riscontro()
        assert nome.endswith(".rfidric")

        esito = circuito.mittente.importa_riscontro(blob)
        assert esito["ok"] is True
        assert esito["attesi"] == 2 and esito["arrivati"] == 2
        assert esito["mancanti"] == 0
        assert esito["spedizione"]["stato"] == "received"

        dopo = circuito.mittente.riepilogo_transito("Ospedale Foligno")
        assert dopo["totali"]["confermate"] == 1
        assert dopo["totali"]["non_confermate"] == 0
        assert dopo["spedizioni"][0]["esito"] == "arrivata"
        assert dopo["tutto_a_buon_fine"] is True
        assert dopo["totali"]["pazienti"] == 2
        assert dopo["totali"]["pezzi"] == 2
    finally:
        circuito.chiudi()


def test_un_campione_perso_per_strada_non_diventa_arrivato():
    """E' il caso per cui il verbale esiste."""
    circuito = _Circuito(_tmp("perso"))
    try:
        spedizione = circuito.spedisci()
        # Alla scatola manca un contenitore.
        circuito.ricevi(
            spedizione["distinta"],
            dentro=[circuito.tag[0]],
            motivo="un contenitore non risulta nella scatola",
        )
        blob, _ = circuito.destinatario.esporta_riscontro()
        esito = circuito.mittente.importa_riscontro(blob)

        assert esito["ok"] is False
        assert esito["mancanti"] == 1
        assert esito["non_conformita"]
        # E il mancante ha un nome, non solo un EPC.
        assert esito["mancanti_descritti"][0]["paziente"]

        riepilogo = circuito.mittente.riepilogo_transito("Ospedale Foligno")
        assert riepilogo["spedizioni"][0]["esito"] == "incompleta"
        assert riepilogo["totali"]["mancanti"] == 1
        assert riepilogo["tutto_a_buon_fine"] is False
        # Lo stato della spedizione **non** diventa «ricevuta»: quella
        # spedizione non e' finita bene e non deve sembrare chiusa.
        assert riepilogo["spedizioni"][0]["stato"] != "received"
    finally:
        circuito.chiudi()


def test_senza_conferma_di_ricezione_non_c_e_verbale():
    """Certificare un arrivo mai controllato sarebbe peggio che non farlo."""
    circuito = _Circuito(_tmp("prematuro"))
    try:
        spedizione = circuito.spedisci()
        circuito.destinatario.importa_distinta(spedizione["distinta"])
        try:
            circuito.destinatario.esporta_riscontro()
            raise AssertionError("WorkflowError attesa")
        except WorkflowError as exc:
            assert "letta" in str(exc) or "confermare" in str(exc)

        # Nemmeno dopo la sola lettura: manca la conferma dell'operatore.
        circuito.backend_destinatario.tags[:] = circuito.tag
        circuito.destinatario.leggi_volume()
        try:
            circuito.destinatario.esporta_riscontro()
            raise AssertionError("WorkflowError attesa")
        except WorkflowError as exc:
            assert "confermare" in str(exc)
    finally:
        circuito.chiudi()


def test_un_verbale_di_un_altra_spedizione_viene_respinto():
    circuito = _Circuito(_tmp("altra"), quanti=2)
    try:
        spedizione = circuito.spedisci()
        circuito.ricevi(spedizione["distinta"])
        verbale = costruisci_riscontro(
            circuito.destinatario.db,
            circuito.destinatario.inbound_id,
            operatore="FOLIGNO",
        )
        # Stessa spedizione, ma con dentro campioni che non sono quelli.
        verbale.attesi = ["FF" * 12, "EE" * 12, "DD" * 12]
        blob = sigilla_riscontro(verbale, circuito.destinatario.keyring)
        try:
            circuito.mittente.importa_riscontro(blob)
            raise AssertionError("WorkflowError attesa")
        except WorkflowError as exc:
            assert "non corrisponde" in str(exc)
    finally:
        circuito.chiudi()


def test_una_spedizione_sconosciuta_lo_dice():
    circuito = _Circuito(_tmp("sconosciuta"))
    try:
        verbale = Riscontro(shipment_id=9999, attesi=["AA" * 12], arrivati=["AA" * 12])
        blob = sigilla_riscontro(verbale, circuito.mittente.keyring)
        try:
            circuito.mittente.importa_riscontro(blob)
            raise AssertionError("WorkflowError attesa")
        except WorkflowError as exc:
            assert "non risulta in questo archivio" in str(exc)
    finally:
        circuito.chiudi()


# ---------------------------------------------------------------------------
# Il documento
# ---------------------------------------------------------------------------
def test_un_verbale_manomesso_non_si_apre():
    portachiavi = Keyring()
    portachiavi.generate(0)
    verbale = Riscontro(shipment_id=1, attesi=["AA" * 12], arrivati=["AA" * 12])
    blob = bytearray(sigilla_riscontro(verbale, portachiavi))
    blob[-1] ^= 0x01
    try:
        apri_riscontro(bytes(blob), portachiavi)
        raise AssertionError("PayloadAuthenticationError attesa")
    except PayloadAuthenticationError:
        pass


def test_senza_la_chiave_del_circuito_non_si_legge():
    mio = Keyring()
    mio.generate(0)
    altrui = Keyring()
    altrui.generate(3)
    blob = sigilla_riscontro(Riscontro(shipment_id=1), mio)
    try:
        apri_riscontro(blob, altrui)
        raise AssertionError("UnknownKeyError attesa")
    except UnknownKeyError as exc:
        assert "concordata" in str(exc)


def test_un_file_qualsiasi_non_e_un_verbale():
    portachiavi = Keyring()
    portachiavi.generate(0)
    for guasto in (b"", b"ciao", b"RFIDLIMS-MANIFEST" + b"\x01" * 40):
        try:
            apri_riscontro(guasto, portachiavi)
            raise AssertionError(f"PayloadFormatError attesa per {guasto[:12]!r}")
        except PayloadFormatError:
            pass


def test_il_verbale_va_e_torna_intatto():
    portachiavi = Keyring()
    portachiavi.generate(0)
    verbale = Riscontro(
        shipment_id=12,
        inbound_id=4,
        manifest_uuid="abc-123",
        operatore="FOLIGNO",
        attesi=["AA" * 12, "BB" * 12],
        arrivati=["AA" * 12],
        mancanti=["BB" * 12],
        non_conformita="un vasetto rotto in transito",
    )
    riletto = apri_riscontro(sigilla_riscontro(verbale, portachiavi), portachiavi)
    assert riletto.to_dict() == verbale.to_dict()
    assert riletto.ok is False


def test_una_scatola_vuota_non_e_una_spedizione_riuscita():
    """Nessun campione atteso e nessuno mancante non fa «tutto a buon fine»."""
    assert Riscontro(attesi=[], arrivati=[]).ok is False
    assert Riscontro(attesi=["AA" * 12], arrivati=["AA" * 12]).ok is True
    assert Riscontro(attesi=["AA" * 12], arrivati=["AA" * 12], inattesi=["BB" * 12]).ok is False


# ---------------------------------------------------------------------------
# Il riepilogo
# ---------------------------------------------------------------------------
def test_il_riepilogo_esce_anche_in_csv():
    circuito = _Circuito(_tmp("csv"))
    try:
        spedizione = circuito.spedisci()
        circuito.ricevi(spedizione["distinta"])
        blob, _ = circuito.destinatario.esporta_riscontro()
        circuito.mittente.importa_riscontro(blob)

        contenuto, nome = circuito.mittente.esporta_riepilogo("Ospedale Foligno")
        testo = contenuto.decode("utf-8")
        assert nome.endswith(".csv")
        # Il punto e virgola e il BOM sono per Excel in italiano.
        assert testo.startswith("﻿")
        assert ";" in testo.splitlines()[0]
        assert "spedizione;destinazione" in testo
        assert "arrivata" in testo
    finally:
        circuito.chiudi()


def test_il_riepilogo_di_un_periodo_senza_spedizioni_non_mente():
    circuito = _Circuito(_tmp("vuoto"))
    try:
        riepilogo = circuito.mittente.riepilogo_transito("Ospedale Foligno")
        assert riepilogo["spedizioni"] == []
        assert riepilogo["totali"]["pezzi"] == 0
        # Nessuna spedizione non e' «tutto a buon fine»: non e' successo niente.
        assert riepilogo["tutto_a_buon_fine"] is False
    finally:
        circuito.chiudi()


def test_il_riepilogo_elenca_i_pazienti_transitati():
    circuito = _Circuito(_tmp("pazienti"))
    try:
        spedizione = circuito.spedisci()
        circuito.ricevi(spedizione["distinta"])
        blob, _ = circuito.destinatario.esporta_riscontro()
        circuito.mittente.importa_riscontro(blob)

        riepilogo = circuito.mittente.riepilogo_transito("Ospedale Foligno")
        pazienti = {voce["codice_fiscale"]: voce for voce in riepilogo["pazienti"]}
        assert set(pazienti) == {CF_UNO, CF_DUE}
        assert all(voce["pezzi"] == 1 for voce in pazienti.values())
        assert pazienti[CF_UNO]["paziente"].startswith("DELLA VALLE")
    finally:
        circuito.chiudi()


def test_un_verbale_piu_recente_sostituisce_il_precedente():
    """Capita quando il destinatario riapre una ricezione e la richiude."""
    circuito = _Circuito(_tmp("secondo"))
    try:
        spedizione = circuito.spedisci()
        circuito.ricevi(
            spedizione["distinta"], dentro=[circuito.tag[0]], motivo="ne manca uno"
        )
        primo, _ = circuito.destinatario.esporta_riscontro()
        circuito.mittente.importa_riscontro(primo)
        assert circuito.mittente.riepilogo_transito("")["spedizioni"][0]["esito"] == "incompleta"

        # Il contenitore salta fuori: si rilegge e si riconferma.
        circuito.backend_destinatario.tags[:] = circuito.tag
        circuito.destinatario.leggi_volume()
        circuito.destinatario.conferma_ricezione("")
        secondo, _ = circuito.destinatario.esporta_riscontro()
        esito = circuito.mittente.importa_riscontro(secondo)

        assert esito["ok"] is True
        riepilogo = circuito.mittente.riepilogo_transito("")
        assert riepilogo["spedizioni"][0]["esito"] == "arrivata"
        assert riepilogo["totali"]["confermate"] == 1, "non due verbali, uno aggiornato"
    finally:
        circuito.chiudi()


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print(f"{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
