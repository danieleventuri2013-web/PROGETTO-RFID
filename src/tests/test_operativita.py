"""Regressioni del flusso manuale e dei profili, senza lettore fisico."""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import FakeTagBackend
from test_lims_tagio import _backend_vergine, _payload, _tagio
from test_strumenti import CF_UNO, _config, _tag

from lims.profiler import profile_tag
from lims.tagio import MODALITA_SOLO_EPC
from webui.server import WebUIServer


def _server(**opzioni):
    cartella = Path(tempfile.mkdtemp(prefix="rfid_operativita_"))
    config = _config(cartella, modalita_scrittura="solo_epc", user_memory_bytes=0, **opzioni)
    server = WebUIServer(config, FakeTagBackend([_tag(1, user_bytes=0)], antennas=(1, 2, 3)),
                         config_path=cartella / "config.yaml", host="127.0.0.1", port=0)
    server.workflow.imposta_operatore("TEST")
    assert server.call("connetti", {})[0] == 200
    return server


def test_collegamento_non_interroga_hardware_o_tag():
    s = _server()
    try:
        def vietato(*a, **kw):
            raise AssertionError("scansione automatica")
        s.backend.identify = vietato
        s.backend.inventory = vietato
        for trasporto in ({"trasporto": "seriale", "port": "COM_TEST"},
                          {"trasporto": "tcp", "host": "127.0.0.1", "port_tcp": 8080}):
            status, risposta = s.call("applica_collegamento", trasporto)
            assert status == 200 and risposta["configurazione_ok"], risposta
            assert s.call("connetti", {})[1]["hardware"]["rilevato"] is False
        assert s.workflow.ultimo_profilo is None
    finally:
        s.shutdown()


def test_profili_persistono_e_non_richiedono_user():
    s = _server()
    try:
        dati = {"station_mode": "ricezione", "prototype_mode": True,
                "profile_name": "Tag piccolo", "user_memory_bytes": 0,
                "memorie": {"read_tid": False, "read_user": False, "write_user": False}}
        status, risposta = s.call("salva_operativita", dati)
        assert status == 200, risposta
        assert risposta["tag_profiles"]["Tag piccolo"]["user_memory_bytes"] == 0
        status, risposta = s.call("salva_antenne", {"lettura": [1], "scrittura": []})
        assert status == 200, risposta
        import yaml
        salvata = yaml.safe_load(s.workflow.config_path.read_text(encoding="utf-8"))
        assert salvata["lims"]["prototype_mode"] is True
        assert salvata["lims"]["write_antennas"] == []
        altro = WebUIServer(salvata, FakeTagBackend([]), host="127.0.0.1", port=0)
        try:
            assert altro.workflow.impostazioni_operative()["tag_profiles"] == risposta.get("tag_profiles", salvata["lims"]["tag_profiles"])
            assert altro.workflow.station_mode == "ricezione"
        finally:
            altro.shutdown()
    finally:
        s.shutdown()


def test_sede_ricezione_blocca_le_scritture_api():
    s = _server(station_mode="ricezione")
    try:
        for nome in ("scrivi", "registra", "sigilla", "prepara_spedizione", "invia_distinta_pec"):
            stato, risposta = s.call(nome, {})
            assert stato == 400 and "sola ricezione" in risposta["errore"], (nome, risposta)
        assert s.call("leggi_volume", {})[0] == 200
        assert s.call("impostazioni", {})[0] == 200
    finally:
        s.shutdown()


def test_errore_salvataggio_non_cambia_configurazione():
    s = _server()
    try:
        prima = copy.deepcopy(s.workflow.config)
        def fallisci(*a):
            raise OSError("disco pieno")
        s.workflow._scrivi_config = fallisci
        assert s.call("salva_operativita", {"prototype_mode": True})[0] == 500
        assert s.workflow.config == prima
        assert s.call("salva_antenne", {"lettura": [2], "scrittura": [1]})[0] == 500
        assert s.workflow.config == prima
    finally:
        s.shutdown()


def test_solo_epc_senza_tid_e_senza_chiavi():
    from lims.crypto import Keyring
    from lims.tagio import TagIO
    backend = _backend_vergine(user_bytes=0, tid=b"\xe2\x00\x00\x00")
    io = TagIO(backend, Keyring(), modalita=MODALITA_SOLO_EPC, read_tid=False, read_user=False)
    for _ in range(20):
        r = io.provision(_payload())
        assert r.ok and not r.tid and not r.blocks_written, r.error
    assert "read" not in backend.calls and "write" not in backend.calls


def test_user_non_richiesta_non_viene_letta():
    backend = _backend_vergine(user_bytes=0)
    io = _tagio(backend, modalita=MODALITA_SOLO_EPC, read_tid=False, read_user=False)
    assert io.provision(_payload()).ok
    backend.calls.clear()
    r = io.survey_field()
    assert r.ok and r.observations[0].status == "solo_epc"
    assert backend.calls == ["inventory"]


def test_timeout_non_diventa_user_assente():
    backend = _backend_vergine()
    originale = backend.read
    def leggi(r):
        return backend._ko("read", "0x0400: No tag found") if r.bank == 3 else originale(r)
    backend.read = leggi
    assert not profile_tag(backend).ok


def test_riscrittura_prototipo_e_recupero_risposta_persa():
    s = _server(prototype_mode=True)
    try:
        for giro in range(20):
            assert s.call("registra", {"codice_fiscale": CF_UNO, "cognome": "Prova", "nome": "Test",
                                       "sesso": "M", "descrizione": "contenitore"})[0] == 200
            assert s.call("conferma_conteggio", {})[0] == 200
            originale = s.backend.write_epc
            if giro == 0:
                def persa(r, originale=originale):
                    originale(r)
                    return s.backend._ko("write_epc", "risposta persa dopo scrittura")
                s.backend.write_epc = persa
                _, r = s.call("scrivi", {})
                assert not r["scrittura"]["ok"]
                riservato = r["scrittura"]["epc"]
                s.backend.write_epc = originale
            _, r = s.call("scrivi", {})
            assert r["scrittura"]["ok"], r
            if giro == 0:
                assert r["scrittura"]["epc"] == riservato
            assert s.call("nuova_accettazione", {})[0] == 200
        righe = s.workflow.db.connection.execute("SELECT * FROM tag_assignments WHERE released_at IS NULL").fetchall()
        assert len(righe) == 1
    finally:
        s.shutdown()


def test_rf_continua_non_promette_un_reset_s0():
    backend = _backend_vergine(user_bytes=0)
    backend.alimentazione_continua = True
    io = _tagio(backend, modalita=MODALITA_SOLO_EPC, read_tid=False, read_user=False)
    r = io.provision(_payload())
    assert not r.ok, "S0 non azzera il flag di un chip ancora alimentato"


def test_modelli_diversi_non_ereditano_la_capacita_del_piu_piccolo():
    s = _server()
    try:
        for nome, capacita, user in (("Piccolo", 0, False), ("Capiente", 64, True), ("Piccolo", 0, False)):
            status, r = s.call("salva_operativita", {
                "profile_name": nome, "user_memory_bytes": capacita,
                "memorie": {"read_tid": True, "read_user": user, "write_user": user},
            })
            assert status == 200, r
            assert r["user_memory_bytes"] == capacita
        assert r["tag_profiles"]["Capiente"]["user_memory_bytes"] == 64
        assert r["tag_profiles"]["Piccolo"]["user_memory_bytes"] == 0
    finally:
        s.shutdown()


def test_ripristino_radio_fallito_blocca_il_comando_successivo():
    s = _server()
    try:
        originale = s.workflow._restore_radio
        def fallisci(*a):
            raise RuntimeError("ripristino non riuscito")
        s.workflow._restore_radio = fallisci
        with s.workflow._assetto_accesso((3,)):
            pass
        assert not s.workflow.radio_configurata
        assert s.call("scrivi", {})[0] == 400
        s.workflow._restore_radio = originale
        assert s.call("applica_radio", {})[1]["configurazione_ok"]
    finally:
        s.shutdown()


def test_configurazione_non_si_modifica_durante_scrittura():
    s = _server()
    try:
        s._acquire("scrivi")
        try:
            for nome in ("salva_operativita", "salva_impostazioni", "applica_profilo", "registra", "operatore"):
                assert s.call(nome, {})[0] == 409, nome
            assert s.call("descrivi", {})[0] == 200
        finally:
            s._release()
    finally:
        s.shutdown()


def test_riavvio_recupera_epc_scritto_con_risposta_persa():
    s = _server()
    altro = None
    try:
        assert s.call("registra", {"codice_fiscale": CF_UNO, "cognome": "Prova", "nome": "Test",
                                   "sesso": "M", "descrizione": "contenitore"})[0] == 200
        assert s.call("conferma_conteggio", {})[0] == 200
        originale = s.backend.write_epc
        def persa(r):
            originale(r)
            return s.backend._ko("write_epc", "risposta persa")
        s.backend.write_epc = persa
        _, esito = s.call("scrivi", {})
        assert not esito["scrittura"]["ok"]
        riservato = esito["scrittura"]["epc"]
        s.backend.write_epc = originale
        altro = WebUIServer(copy.deepcopy(s.workflow.config), s.backend, host="127.0.0.1", port=0)
        altro.workflow.imposta_operatore("TEST")
        assert altro.call("connetti", {})[0] == 200
        _, ripresa = altro.call("scrivi", {})
        assert ripresa["scrittura"]["ok"], ripresa
        assert ripresa["scrittura"]["epc"] == riservato
    finally:
        if altro:
            altro.shutdown()
        s.shutdown()


def _prova_ripresa_capacita(*, storica=False, risposta_persa=False, modifica=""):
    """Riproduce il cambio 16 -> 0 byte dopo un tentativo incompleto."""
    import hashlib
    import json
    from dataclasses import replace

    from test_lims_reuse import CF_A, _contenitore
    from test_lims_tagio import TID_B

    from lims.db import LimsDatabase
    from lims.tagio import MODALITA_PAYLOAD

    with LimsDatabase(":memory:") as db:
        payload = _payload(container_total=1)
        cid = _contenitore(db, CF_A, payload.accession_id)
        backend = _backend_vergine()
        originale = backend.write_epc

        def persa(r):
            if risposta_persa:
                originale(r)
            return backend._ko("write_epc", "risposta persa")

        backend.write_epc = persa
        io = _tagio(backend, db=db, modalita=MODALITA_SOLO_EPC, user_memory_bytes=16)
        primo = io.provision(payload, container_id=cid)
        assert not primo.ok
        if storica:
            # Formato realmente persistito prima della correzione.
            impronta = hashlib.sha256(json.dumps({
                "payload": payload.describe(), "modalita": MODALITA_SOLO_EPC,
                "capacita": 16, "revision": 0,
            }, sort_keys=True, default=str).encode()).hexdigest()
            db.connection.execute("UPDATE provision_attempts SET fingerprint=? WHERE container_id=?",
                                  (impronta, cid))
            db.connection.commit()
        tentativo = db.provision_attempt(cid)
        backend.write_epc = originale
        backend.calls.clear()
        io.user_memory_bytes = 0
        revisione = 0
        if modifica == "payload":
            payload = replace(payload, display_name="ALTRO PAZIENTE")
        elif modifica == "revisione":
            revisione = 1
        elif modifica == "modalita":
            io.modalita = MODALITA_PAYLOAD
            io.user_memory_bytes = 64
        elif modifica == "tag":
            backend = _backend_vergine(tid=TID_B)
            io.backend = backend
        ripresa = io.provision(payload, container_id=cid, revision=revisione)
        if modifica:
            assert not ripresa.ok, modifica
            assert "tentativo incompleto" in ripresa.error, ripresa.error
            assert "write_epc" not in backend.calls and "write" not in backend.calls
            assert db.provision_attempt(cid) == tentativo
        else:
            assert ripresa.ok, ripresa.error
            assert ripresa.epc == primo.epc == tentativo["epc"]
            assert db.provision_attempt(cid)["phase"] == "verificata"
            assert "write" not in backend.calls
            if risposta_persa:
                assert "write_epc" not in backend.calls


def test_ripresa_solo_epc_dopo_cambio_capacita_user():
    for storica in (False, True):
        for risposta_persa in (False, True):
            _prova_ripresa_capacita(storica=storica, risposta_persa=risposta_persa)


def test_ripresa_solo_epc_conserva_controlli_dati_modalita_e_tag():
    for storica in (False, True):
        for modifica in ("payload", "revisione", "modalita", "tag"):
            _prova_ripresa_capacita(storica=storica, modifica=modifica)


def _run_all():
    import logging
    logging.disable(logging.CRITICAL)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print("PASS", test.__name__)
    print(f"{len(tests)}/{len(tests)} test superati")
    return 0


if __name__ == "__main__":
    sys.exit(_run_all())
