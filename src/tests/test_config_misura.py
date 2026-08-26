"""Scrittura della misura dei tag dentro `config.yaml`.

Due promesse, e nessuna delle due e' scontata:

* **la soglia scende ma non sale nello stesso modello.** `lims.user_memory_bytes`
  deve reggere il tag peggiore del lotto; un modello diverso apre una nuova serie;
* **il file resta quello che era.** `config.yaml` e' pieno di commenti che
  spiegano ogni parametro: sono la documentazione operativa, e una riscrittura
  con `yaml.safe_dump` li cancellerebbe tutti. Qui si toccano solo le righe
  interessate, fine riga CRLF compreso.
"""

from __future__ import annotations

import datetime as dt
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config_misura import Misura, aggiorna_testo, scrivi_misura

QUANDO = dt.datetime(2026, 8, 26, 9, 30, 0)

BASE = """\
# Configurazione del banco. I commenti sono la documentazione: non si perdono.
reader:
  # La regione e' un obbligo di legge, non una preferenza.
  region: 0x08

lims:
  lab_id: 1
  # Byte di USER memory dei tag in uso. Lo scrive la profilazione.
  user_memory_bytes: 64
  tag_misurato:
    chip: ''
    tid_serializzato: false
    minimo_byte: 0
    ultimo_byte: 0
    tag_provati: 0
    ultimo_epc: ''
    aggiornato: ''
  # Postazione di scrittura: antenna 3, distante dalle altre.
  write_antennas: [3]

webui:
  port: 8770
"""


def _misura(byte: int, **extra) -> Misura:
    dati = {"user_bytes": byte, "tid_serializzato": True, "chip": "Impinj Monza R6", "epc": "AABB"}
    dati.update(extra)
    return Misura(**dati)


def _lims(testo: str) -> dict:
    return yaml.safe_load(testo)["lims"]


# ---------------------------------------------------------------------------
# La soglia
# ---------------------------------------------------------------------------
def test_la_prima_misura_si_scrive_qualunque_sia():
    """Su un'installazione nuova il 64 predefinito e' un segnaposto, non una misura."""
    testo, esito = aggiorna_testo(BASE, _misura(128), adesso=QUANDO)
    assert esito.scritto
    assert _lims(testo)["user_memory_bytes"] == 128
    assert _lims(testo)["tag_misurato"]["tag_provati"] == 1


def test_un_tag_piu_piccolo_abbassa_la_soglia():
    testo, _ = aggiorna_testo(BASE, _misura(128), adesso=QUANDO)
    testo, esito = aggiorna_testo(testo, _misura(64), adesso=QUANDO)
    assert _lims(testo)["user_memory_bytes"] == 64, "la soglia deve seguire il tag peggiore"
    assert _lims(testo)["tag_misurato"]["tag_provati"] == 2
    assert "scende" in esito.motivo


def test_un_tag_piu_grande_non_alza_la_soglia():
    """E' il caso che rompe tutto: 64 misurato prima, 128 adesso.

    Scrivere 128 farebbe fallire a meta' la scrittura sui tag da 64 che sono
    gia' in circolazione, e lo si scoprirebbe sul primo campione vero.
    """
    testo, _ = aggiorna_testo(BASE, _misura(64), adesso=QUANDO)
    testo, esito = aggiorna_testo(testo, _misura(128), adesso=QUANDO)
    assert _lims(testo)["user_memory_bytes"] == 64, "la soglia non deve salire da sola"
    assert _lims(testo)["tag_misurato"]["minimo_byte"] == 64
    assert _lims(testo)["tag_misurato"]["ultimo_byte"] == 128, "l'ultima misura si registra"
    assert "peggiore" in esito.motivo


def test_forza_riparte_dal_tag_in_mano():
    """Cambio di lotto: si riparte da capo, e il conteggio torna a uno."""
    testo, _ = aggiorna_testo(BASE, _misura(64), adesso=QUANDO)
    testo, esito = aggiorna_testo(testo, _misura(128), forza=True, adesso=QUANDO)
    assert _lims(testo)["user_memory_bytes"] == 128
    assert _lims(testo)["tag_misurato"]["tag_provati"] == 1
    assert esito.tag_provati == 1


def test_un_chip_diverso_apre_una_nuova_serie():
    """Il minimo di un vecchio modello non deve contaminare quello nuovo."""
    testo, _ = aggiorna_testo(BASE, _misura(16, chip="Quanray Qstar-6"), adesso=QUANDO)
    testo, esito = aggiorna_testo(
        testo, _misura(86, chip="Alien Technology, modello 0x821"), adesso=QUANDO
    )
    lims = _lims(testo)
    assert lims["user_memory_bytes"] == 86
    assert lims["tag_misurato"]["minimo_byte"] == 86
    assert lims["tag_misurato"]["tag_provati"] == 1
    assert "chip cambiato" in esito.motivo


def test_una_misura_a_zero_non_tocca_niente():
    testo, esito = aggiorna_testo(BASE, _misura(0), adesso=QUANDO)
    assert not esito.scritto
    assert testo == BASE


def test_il_tid_non_serializzato_si_registra_comunque():
    """Non blocca la scrittura del valore: e' un avviso sul tag, non sul numero."""
    testo, esito = aggiorna_testo(BASE, _misura(64, tid_serializzato=False), adesso=QUANDO)
    assert esito.scritto
    assert _lims(testo)["tag_misurato"]["tid_serializzato"] is False


# ---------------------------------------------------------------------------
# Il file
# ---------------------------------------------------------------------------
def test_i_commenti_e_il_resto_non_si_perdono():
    testo, _ = aggiorna_testo(BASE, _misura(96), adesso=QUANDO)
    for commento in (
        "# Configurazione del banco. I commenti sono la documentazione: non si perdono.",
        "# La regione e' un obbligo di legge, non una preferenza.",
        "# Byte di USER memory dei tag in uso. Lo scrive la profilazione.",
        "# Postazione di scrittura: antenna 3, distante dalle altre.",
    ):
        assert commento in testo, f"commento perso: {commento}"
    # Le sezioni intorno restano intatte, valore e ordine compresi.
    dati = yaml.safe_load(testo)
    assert dati["reader"]["region"] == 0x08
    assert dati["lims"]["write_antennas"] == [3]
    assert dati["webui"]["port"] == 8770
    assert list(dati) == ["reader", "lims", "webui"]


def test_solo_le_righe_interessate_cambiano():
    testo, _ = aggiorna_testo(BASE, _misura(96), adesso=QUANDO)
    prima = BASE.splitlines()
    dopo = testo.splitlines()
    diverse = [riga for riga in dopo if riga not in prima]
    for riga in diverse:
        assert any(
            chiave in riga
            for chiave in ("user_memory_bytes", "minimo_byte", "ultimo_byte", "tag_provati",
                           "ultimo_epc", "aggiornato", "chip", "tid_serializzato")
        ), f"riga toccata senza motivo: {riga!r}"


def test_il_fine_riga_di_windows_resta_windows():
    """Mescolare CRLF e LF nello stesso file lo fa sembrare modificato ovunque."""
    testo, _ = aggiorna_testo(BASE.replace("\n", "\r\n"), _misura(96), adesso=QUANDO)
    assert "\r\n" in testo
    assert not [riga for riga in testo.split("\r\n") if riga.endswith("\r") or "\n" in riga]


def test_una_configurazione_senza_registro_lo_riceve():
    """Le installazioni gia' in giro non hanno il blocco: va aggiunto, non preteso."""
    vecchia = "\n".join(
        riga
        for riga in BASE.splitlines()
        if not riga.startswith("    ") and "tag_misurato" not in riga
    ) + "\n"
    assert "tag_misurato" not in vecchia
    testo, esito = aggiorna_testo(vecchia, _misura(96), adesso=QUANDO)
    assert esito.scritto
    assert _lims(testo)["tag_misurato"]["minimo_byte"] == 96
    assert _lims(testo)["write_antennas"] == [3], "il resto della sezione resta al suo posto"
    assert "# Postazione di scrittura: antenna 3, distante dalle altre." in testo


def test_riscritture_ripetute_non_gonfiano_il_file():
    """Il blocco si sostituisce, non si accumula."""
    testo = BASE
    for _ in range(4):
        testo, _ = aggiorna_testo(testo, _misura(64), adesso=QUANDO)
    assert testo.count("tag_misurato:") == 1
    assert testo.count("minimo_byte:") == 1
    assert _lims(testo)["tag_misurato"]["tag_provati"] == 4


def test_senza_la_sezione_lims_lo_dice_invece_di_inventarla():
    testo, esito = aggiorna_testo("webui:\n  port: 8770\n", _misura(64), adesso=QUANDO)
    assert not esito.scritto
    assert "lims" in esito.motivo


def test_scrittura_su_disco_in_due_tempi():
    cartella = Path(tempfile.mkdtemp(prefix="config_misura_"))
    percorso = cartella / "config.yaml"
    percorso.write_text(BASE, encoding="utf-8")

    esito = scrivi_misura(percorso, _misura(96), adesso=QUANDO)
    assert esito.scritto
    assert yaml.safe_load(percorso.read_text(encoding="utf-8"))["lims"]["user_memory_bytes"] == 96
    assert not percorso.with_suffix(".yaml.tmp").exists(), "il provvisorio va rimosso"


def test_il_file_vero_del_progetto_regge_la_scrittura():
    """La prova che conta: il `config.yaml` in consegna, non uno finto."""
    vero = Path(__file__).resolve().parents[1] / "app" / "config.yaml"
    with open(vero, "r", encoding="utf-8", newline="") as f:
        testo = f.read()
    nuovo, esito = aggiorna_testo(testo, _misura(48), adesso=QUANDO)
    assert esito.scritto
    dati = yaml.safe_load(nuovo)
    assert dati["lims"]["user_memory_bytes"] == 48
    assert dati["lims"]["tag_misurato"]["minimo_byte"] == 48
    # Niente sezioni perse per strada.
    assert list(dati) == list(yaml.safe_load(testo))
    assert nuovo.count("\r\n") >= testo.count("\r\n") - 1


def _run_all() -> int:
    falliti = 0
    for nome, funzione in sorted(globals().items()):
        if not nome.startswith("test_") or not callable(funzione):
            continue
        try:
            funzione()
            print(f"PASS {nome}")
        except AssertionError as errore:
            falliti += 1
            print(f"FAIL {nome}: {errore}")
    totale = len([n for n in globals() if n.startswith("test_")])
    print(f"\n{totale - falliti}/{totale} test superati")
    return 1 if falliti else 0


if __name__ == "__main__":
    sys.exit(_run_all())
