"""Prova visiva e recupero RFID: dati sintetici, nessun lettore o webcam reali."""
from __future__ import annotations

import copy
import sys
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_backend import SimulatedTag
from test_webui import _Postazione, _tmp

from lims import vision
from lims.codec import build_epc
from lims.crypto import Keyring
from lims.manifest import Manifest, open_manifest, seal_manifest
from lims.model import Case, Patient, Shipment, Specimen
from webui.workflow import WorkflowError


def _popola(w, n=3):
    pid = w.db.upsert_patient(Patient(codice_fiscale="", cognome="TEST", nome="VISIVO"))
    case = w.db.create_case(Case(accession_id=1, patient_id=pid, data_prelievo=date.today()))
    specimen = w.db.add_specimen(Specimen(case_id=case, material_code=1))
    ids = w.db.plan_containers(specimen, n)
    sid = w.db.create_shipment(Shipment(destinazione="Distretto B", data=date.today()))
    tags = []
    for i, cid in enumerate(ids, 1):
        epc = build_epc(1, 1, i, n, random_suffix=bytes([0, 0, i]))
        w.db.assign_epc(cid, epc.hex().upper())
        w.db.mark_provisioned(cid, f"E28011902000500000{i:06X}")
        tags.append(SimulatedTag(epc))
    w.db.add_to_shipment(sid, ids)
    w.shipment_id = sid
    return tags


def _scena(n=3):
    return {"area": [[.05,.05],[.95,.05],[.95,.95],[.05,.95]], "origine_area": "bordo automatico",
            "centri": [[.12+(i%5)*.16, .2+(i//5)*.22] for i in range(n)], "markers": {},
            "dimensioni": [1280,720], "qualita": "leggibile", "miniatura": [100]*3072,
            "coperchio": "non determinabile"}


def _stabilizza(w, n=3):
    # Tre analisi consecutive su un'immagine sintetica; orologio controllato.
    base = time.monotonic()
    with patch("webui.visual.time.monotonic", side_effect=[base-1.2,base-.6,base]):
        for _ in range(3):
            out = w.controllo_visivo({"azione":"analizza", "shipment_id":w.shipment_id, "immagine_base64":"prova"})
    assert out["stabile"] and out["conteggio"] == n
    return out["sessione"]


def _rifiuta(fn, testo=""):
    try:
        fn()
    except WorkflowError as exc:
        assert testo in str(exc), str(exc)
    else:
        raise AssertionError("operazione non rifiutata")


def test_prove_cifrate_legate_alla_spedizione_e_distinta_compatibile():
    from cryptography.exceptions import InvalidTag

    keyring = Keyring({0:b"K"*32})
    doc = {"stato":"concordante", "foto_contenuto":{"jpeg":"FOTOGRAFIA_PRIVATA"}}
    blob = vision.cifra(doc, keyring, "spedizione:1")
    assert b"FOTOGRAFIA_PRIVATA" not in blob
    assert vision.apri(blob, keyring, "spedizione:1") == doc
    for raw, context in ((blob, "spedizione:2"), (blob[:-1]+bytes([blob[-1]^1]), "spedizione:1")):
        try:
            vision.apri(raw, keyring, context)
        except InvalidTag:
            pass
        else:
            raise AssertionError("foto sostituita o manomessa accettata")
    assert open_manifest(seal_manifest(Manifest(visual_check=doc), keyring), keyring).visual_check == doc
    assert Manifest.from_dict({"schema":1}).visual_check is None


def test_recupero_15_13_14_15_unici_ripristina_radio_e_conserva_foto():
    with _Postazione(_tmp("visivo_15")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena(15))):
        w = p.server.workflow
        tags = _popola(w, 15)
        original = p.backend.inventory
        step = [0]
        def leggi(req):
            step[0] += 1
            p.backend.tags[:] = tags[:min(15, 12+step[0])]
            return original(req)
        p.backend.inventory = leggi
        before = dict(p.backend.gen2)
        configs = []
        configure = p.backend.configure
        def config(settings):
            configs.append(settings)
            return configure(settings)
        p.backend.configure = config
        session = _stabilizza(w, 15)
        result = w.recupera_visivo({"shipment_id":w.shipment_id,"sessione":session}, stop_event=threading.Event())
        assert result["concorde"] and result["trovati"] == 15 and result["visibili"] == 15
        assert [len(x["epcs"]) for x in result["passes"][:3]] == [13,14,15]
        assert sum(len(x["nuovi"]) for x in result["passes"]) == 15
        assert p.backend.gen2 == before
        for cfg in configs:
            assert cfg.region == 8
            for power in cfg.powers:
                source = next(a for a in w.config["antennas"] if a["id"]==power.antenna_id)
                assert power.write_power_cdbm == source["write_power"]
                assert power.read_power_cdbm <= source["read_power"]
        with patch("lims.vision.fotografia", return_value={"jpeg":"FOTO_PRIVATA", "sha256":"test"}):
            photo = w.controllo_visivo({"azione":"prepara_foto", "shipment_id":w.shipment_id,"sessione":session,"immagine_base64":"finale"})
            assert "foto_contenuto" not in w.visual_documento(), "la foto va confermata"
            saved = w.controllo_visivo({"azione":"conferma_foto", "shipment_id":w.shipment_id,"sessione":session,"token":photo["token"]})
        assert saved["prova"]["foto_contenuto"]["jpeg"] == "FOTO_PRIVATA"
        raw = w.db.connection.execute("SELECT encrypted_blob FROM visual_checks").fetchone()[0]
        assert b"FOTO_PRIVATA" not in raw
        sid = w.shipment_id
        w._visual_session = None
        assert w.visual_documento(sid)["rfid"]["trovati"] == 15
        w.sigilla()
        blob, _ = w.esporta_distinta()
        assert open_manifest(blob,w.keyring).visual_check["sigillo_finale"]["concorde"]
        _rifiuta(lambda:w.controllo_visivo({"azione":"salta","shipment_id":sid}), "non modificabile")


def test_tag_estraneo_non_compensa_un_mancante_e_interruzione_non_concorda():
    with _Postazione(_tmp("visivo_estranei")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena())):
        w = p.server.workflow
        tags = _popola(w)
        p.backend.tags[:] = tags[:2] + [SimulatedTag(build_epc(1,9,1,1,random_suffix=b"abc"))]
        _stabilizza(w)
        result = w.recupera_visivo({"shipment_id":w.shipment_id},stop_event=threading.Event())
        assert result["trovati"] == 2 and result["unexpected"] and not result["concorde"]
        stop = threading.Event()
        stop.set()
        result = w.recupera_visivo({"shipment_id":w.shipment_id},stop_event=stop)
        assert not result["concorde"] and result["trovati"] == 0


def test_conteggio_diverso_da_distinta_non_avvia_la_radio():
    with _Postazione(_tmp("visivo_diversi")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena(4))):
        w = p.server.workflow
        _popola(w)
        _stabilizza(w,4)
        before = p.backend.inventory_calls
        _rifiuta(lambda:w.recupera_visivo({"shipment_id":w.shipment_id},stop_event=threading.Event()), "differisce")
        assert p.backend.inventory_calls == before


def test_movimento_a_numero_invariato_annulla_recupero_e_foto():
    scene = _scena()
    with _Postazione(_tmp("visivo_movimento")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(scene)):
        w = p.server.workflow
        p.backend.tags[:] = _popola(w)
        ident = _stabilizza(w)
        def progress(*_):
            scene["centri"][0][0] = .75
            w.controllo_visivo({"azione":"analizza","shipment_id":w.shipment_id,"immagine_base64":"spostata"})
        _rifiuta(lambda:w.recupera_visivo({"shipment_id":w.shipment_id},stop_event=threading.Event(),on_progress=progress), "scena modificata")
        _rifiuta(lambda:w.controllo_visivo({"azione":"prepara_foto","shipment_id":w.shipment_id,"sessione":ident,"immagine_base64":"finale"}), "sessione")
        assert w.visual_documento()["stato"] == "invalidato"


def test_salta_non_altera_sigillo_e_hash_contenuto_invalida_la_prova():
    with _Postazione(_tmp("visivo_salta")) as p:
        w = p.server.workflow
        p.backend.tags[:] = _popola(w)
        w.controllo_visivo({"azione":"salta","shipment_id":w.shipment_id})
        assert w.visual_documento()["stato"] == "saltato"
        result = w.sigilla()
        assert result["sigillo"]["ok"]
        with patch.object(w,"_visual_hash",return_value="diverso"):
            assert w.visual_documento()["stato"] == "invalidato"


def test_accesso_http_visivo_e_redazione_immagini():
    from webui.server import _ripulisci

    with _Postazione(_tmp("visivo_accesso")) as p:
        assert p.post("/api/controllo_visivo",{"azione":"impostazioni"},token=None)[0] == 401
        p.server.workflow.operatore=""
        assert p.post("/api/controllo_visivo",{"azione":"impostazioni"})[0] == 400
        p.server.workflow.operatore="TEST"
        p.server.workflow.lims_cfg["station_mode"]="ricezione"
        assert p.post("/api/controllo_visivo",{"azione":"impostazioni"})[0] == 400
    assert "SEGRETO" not in str(_ripulisci({"visual_check":{"foto_contenuto":{"jpeg":"SEGRETO"}},"immagine_base64":"SEGRETO"}))


def test_foto_nuova_scena_diversa_e_token_scaduto_non_archiviano():
    scene = _scena()
    with _Postazione(_tmp("visivo_foto")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(scene)):
        w = p.server.workflow
        p.backend.tags[:] = _popola(w)
        ident = _stabilizza(w)
        w.recupera_visivo({"shipment_id":w.shipment_id}, stop_event=threading.Event())
        scene["miniatura"] = [200]*3072
        _rifiuta(lambda:w.controllo_visivo({"azione":"prepara_foto", "shipment_id":w.shipment_id,"sessione":ident,"immagine_base64":"nuova"}), "scena cambiata")
        assert "foto_contenuto" not in w.visual_documento()
        _stabilizza(w)
        w.recupera_visivo({"shipment_id":w.shipment_id}, stop_event=threading.Event())
        with patch("lims.vision.fotografia", return_value={"jpeg":"FOTO_PRIVATA"}):
            photo = w.controllo_visivo({"azione":"prepara_foto", "shipment_id":w.shipment_id,"immagine_base64":"nuova"})
        with patch("webui.visual.time.monotonic", return_value=time.monotonic()+61):
            _rifiuta(lambda:w.controllo_visivo({"azione":"conferma_foto", "shipment_id":w.shipment_id,"token":photo["token"]}), "scaduta")
        _rifiuta(lambda:w.controllo_visivo({"azione":"conferma_foto", "shipment_id":w.shipment_id+1,"token":photo["token"]}), "spedizione")
        assert "foto_contenuto" not in w.visual_documento()


def test_modalita_rf_non_supportata_rilevata_e_recupero_con_assetto_operativo():
    with _Postazione(_tmp("visivo_rf")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena())):
        w = p.server.workflow
        tags = _popola(w)
        before = dict(p.backend.gen2)
        p.backend.rf_mode_supportate = {before["rf_mode"]}
        original = p.backend.inventory
        step = [0]
        def leggi(req):
            step[0] += 1
            p.backend.tags[:] = tags if step[0] >= 8 else tags[:2]
            return original(req)
        p.backend.inventory = leggi
        _stabilizza(w)
        result = w.recupera_visivo({"shipment_id":w.shipment_id}, stop_event=threading.Event())
        assert result["concorde"] and result["avvisi"], result
        assert all(p["gen2_effettivo"]["rf_mode"] == before["rf_mode"] for p in result["passes"])
        assert p.backend.gen2 == before


def test_ripristino_fallito_non_lascia_radio_pronta_o_prova_concordante():
    with _Postazione(_tmp("visivo_restore")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena())):
        w = p.server.workflow
        p.backend.tags[:] = _popola(w)
        _stabilizza(w)
        with patch.object(w,"_restore_radio",side_effect=WorkflowError("ripristino fallito")):
            _rifiuta(lambda:w.recupera_visivo({"shipment_id":w.shipment_id},stop_event=threading.Event()), "ripristino")
        assert not w.radio_configurata
        assert w.visual_documento()["stato"] == "da verificare"
        _rifiuta(lambda:w.controllo_visivo({"azione":"prepara_foto","shipment_id":w.shipment_id,"immagine_base64":"foto"}), "non concordante")


def test_calibrazione_cifrata_e_cambio_camera_elimina_riferimenti():
    with _Postazione(_tmp("visivo_config")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena())):
        w = p.server.workflow
        _popola(w)
        w.controllo_visivo({"azione":"configura","camera":"camera uno"})
        w.controllo_visivo({"azione":"calibra","shipment_id":w.shipment_id,"tipo":"aperta","area":_scena()["area"],"immagine_base64":"foto"})
        assert w.controllo_visivo({"azione":"impostazioni"})["calibrata_aperta"]
        raw = w.db.connection.execute("SELECT encrypted_blob FROM visual_settings").fetchone()[0]
        assert b"camera uno" not in raw and b"miniatura" not in raw
        w.controllo_visivo({"azione":"configura","camera":"camera due"})
        assert not w.controllo_visivo({"azione":"impostazioni"})["calibrata_aperta"]
        _rifiuta(lambda:w.controllo_visivo({"azione":"configura","raggio_min":.2,"raggio_max":.1}), "raggio")


def test_impostazioni_calibrazione_senza_spedizione_e_archivio_immutabile():
    with _Postazione(_tmp("visivo_config_globale")) as p, patch("lims.vision.immagine", return_value=object()), patch("lims.vision.analizza", side_effect=lambda *a, **k: copy.deepcopy(_scena())):
        w = p.server.workflow
        w.shipment_id = None
        preview = {"azione":"anteprima_configurazione", "immagine_base64":"foto"}
        calibration = {"azione":"calibra", "tipo":"aperta", "area":_scena()["area"], "immagine_base64":"foto"}
        with patch.object(w, "recupera_visivo", side_effect=AssertionError("RFID non previsto")):
            status, out = p.post("/api/controllo_visivo", preview)
            assert status == 200 and out["rfid"] is None and not out["stabile"] and out["sessione"] is None
            assert p.post("/api/controllo_visivo", calibration)[0] == 200
        assert w._visual_session is None
        assert w.controllo_visivo({"azione":"impostazioni"})["calibrata_aperta"]
        _popola(w)
        document = {"stato":"concordante", "foto_contenuto":{"jpeg":"PROVA_CONSERVATA"}}
        w._visual_save(w.shipment_id, document)
        w.db.connection.execute("UPDATE shipments SET state='received' WHERE id=?", (w.shipment_id,))
        w.db.connection.commit()
        assert p.post("/api/controllo_visivo", {**calibration,"tipo":"chiusa"})[0] == 200
        assert w.visual_documento() == document, "calibrare non modifica la prova archiviata"
        w.operatore = ""
        assert p.post("/api/controllo_visivo", preview)[0] == 400
        assert p.post("/api/controllo_visivo", calibration)[0] == 400


def test_riquadro_area_incorporabile_solo_nella_webui_stessa_origine():
    import urllib.request

    with _Postazione(_tmp("visivo_riquadro")) as p:
        for path in ("/", "/sam2-auto.html", "/sam2-auto.html?impostazioni=0"):
            with urllib.request.urlopen(p.base + path) as response:
                assert response.headers["X-Frame-Options"] == "DENY"
        with urllib.request.urlopen(p.base + "/sam2-auto.html?impostazioni=1") as response:
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
            assert response.headers["Content-Security-Policy"] == "frame-ancestors 'self'"


def test_foto_distinta_v2_firmata_cifrata_e_ricevuta():
    from test_lims_secure_workflow import (
        test_percorso_v2_blocca_la_partenza_fino_alla_consegna_e_si_importa,
    )

    test_percorso_v2_blocca_la_partenza_fino_alla_consegna_e_si_importa()


def _scatto_ai():
    from PIL import Image, ImageDraw

    from webui import vision_models

    im = Image.new("RGB", (480, 320), "white")
    draw = ImageDraw.Draw(im)
    for x in range(20, 460, 40):
        draw.rectangle((x, 30, x+15, 290), fill="gray")
    profile = {"versione": 2, "modo": "rettangolo", "area": [.05, .05, .95, .95],
               "dimensioni": [480, 320]}
    return im, {"immagine_base64": vision_models.jpeg(im), "profilo": profile, "modello": "qwen"}


def _rilevamento_ai(im, model):
    return {"dimensioni": list(im.size), "conteggio": 3, "oggetti": [{"centro": p} for p in _scena()["centri"]],
            "modello": "Qwen di prova" if model == "qwen" else "SAM di prova",
            "tempo_secondi": 1.5, "nota": "Riflessi esclusi", "incerto": False}


def _stabilizza_ai(w, data):
    base = time.monotonic()
    with patch("webui.visual.time.monotonic", side_effect=[base-1.2, base-.6, base]):
        for _ in range(3):
            result = w.controllo_visivo({"azione": "analizza", "shipment_id": w.shipment_id, **data})
    return result


def test_qwen_nel_sigillo_stabilita_rfid_foto_e_metadati_cifrati():
    from webui import vision_models

    _, data = _scatto_ai()
    with _Postazione(_tmp("visivo_qwen")) as p, patch.object(vision_models, "analizza", side_effect=_rilevamento_ai) as detector:
        w = p.server.workflow
        p.backend.tags[:] = _popola(w)
        code, result = p.post("/api/controllo_visivo", {"azione": "analizza_modello", "shipment_id": w.shipment_id, **data})
        assert code == 200 and result["conteggio"] == 3 and not result["stabile"]
        assert result["dimensioni"] == [432, 288] and result["motore"] == "qwen"
        assert p.backend.inventory_calls == 0
        stable = _stabilizza_ai(w, data)
        assert stable["stabile"] and stable["conteggio"] == 3 and detector.call_count == 1
        result = w.recupera_visivo({"shipment_id": w.shipment_id}, stop_event=threading.Event())
        assert result["concorde"]
        pending = w.controllo_visivo({"azione": "prepara_foto", "shipment_id": w.shipment_id, **data})
        saved = w.controllo_visivo({"azione": "conferma_foto", "shipment_id": w.shipment_id, "token": pending["token"]})["prova"]
        assert saved["riconoscimento"]["modello"] == "Qwen di prova"
        assert saved["riconoscimento"]["nota"] == "Riflessi esclusi"
        assert saved["foto_contenuto"]["larghezza"] == 432
        raw = w.db.connection.execute("SELECT encrypted_blob FROM visual_checks").fetchone()[0]
        assert b"Qwen di prova" not in raw
        w.sigilla()
        blob, _ = w.esporta_distinta()
        assert open_manifest(blob, w.keyring).visual_check["riconoscimento"] == saved["riconoscimento"]


def test_ai_incerto_e_bordo_assente_non_diventano_falso_zero_o_stabili():
    from webui import vision_models

    _, data = _scatto_ai()
    with _Postazione(_tmp("visivo_ai_incerto")) as p:
        w = p.server.workflow
        _popola(w)
        for missing in (False, True):
            def detect(im, model, missing=missing):
                result = _rilevamento_ai(im, model)
                result["incerto"] = True
                if missing:
                    result.update(conteggio=None, oggetti=[], borsa={"rilevata": False})
                return result
            with patch.object(vision_models, "analizza", side_effect=detect):
                w.controllo_visivo({"azione": "analizza_modello", "shipment_id": w.shipment_id, **data})
                result = _stabilizza_ai(w, data)
                assert not result["stabile"] and result["qualita"] == "incerta"
                assert result["conteggio"] == (None if missing else 3)
                _rifiuta(lambda: w.recupera_visivo({"shipment_id": w.shipment_id}, stop_event=threading.Event()), "stabile")
                w.controllo_visivo({"azione": "correggi", "shipment_id": w.shipment_id, "centri": _scena()["centri"]})
                assert _stabilizza_ai(w, data)["stabile"]


def test_ai_movimento_locale_cambio_profilo_e_invalida_bloccano_recupero():
    from PIL import ImageDraw

    from webui import vision_models

    im, data = _scatto_ai()
    with _Postazione(_tmp("visivo_ai_movimento")) as p, patch.object(vision_models, "analizza", side_effect=_rilevamento_ai):
        w = p.server.workflow
        _popola(w)
        for kind in ("movimento", "profilo", "invalida"):
            w.controllo_visivo({"azione": "analizza_modello", "shipment_id": w.shipment_id, **data})
            assert _stabilizza_ai(w, data)["stabile"]
            changed = copy.deepcopy(data)
            if kind == "movimento":
                moved = im.copy()
                ImageDraw.Draw(moved).rectangle((56, 60, 91, 89), fill="black")
                changed["immagine_base64"] = vision_models.jpeg(moved)
            elif kind == "profilo":
                changed["profilo"]["area"] = [.06, .05, .95, .95]
            else:
                w.controllo_visivo({"azione": "invalida", "shipment_id": w.shipment_id})
            result = w.controllo_visivo({"azione": "analizza", "shipment_id": w.shipment_id, **changed})
            assert result["qualita"] == "da riconoscere" and result["conteggio"] is None
            _rifiuta(lambda: w.recupera_visivo({"shipment_id": w.shipment_id}, stop_event=threading.Event()), "stabile")


def test_ai_risposta_tardiva_e_errore_non_riutilizzano_il_risultato():
    from webui import vision_models

    _, data = _scatto_ai()
    with _Postazione(_tmp("visivo_ai_tardivo")) as p:
        w = p.server.workflow
        _popola(w)
        def late(im, model):
            w.controllo_visivo({"azione": "invalida", "shipment_id": w.shipment_id})
            return _rilevamento_ai(im, model)
        with patch.object(vision_models, "analizza", side_effect=late):
            _rifiuta(lambda: w.controllo_visivo({"azione": "analizza_modello", "shipment_id": w.shipment_id, **data}), "superato")
        with patch.object(vision_models, "analizza", side_effect=RuntimeError("servizio occupato")):
            _rifiuta(lambda: w.controllo_visivo({"azione": "analizza_modello", "shipment_id": w.shipment_id, **data}), "occupato")
        _rifiuta(lambda: w.recupera_visivo({"shipment_id": w.shipment_id}, stop_event=threading.Event()), "stabile")


def test_ponte_motori_http_validazione_e_calibrazione_prospettica():
    import io
    import json

    from app.sam2_geometry import rettifica
    from app.vision_preview import crea_server
    from webui import vision_models

    im, data = _scatto_ai()
    cfg = data["profilo"]
    cfg.update(modo="prospettiva", punti=[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],
               larghezza_cm=48, lunghezza_cm=26.5, altezza_camera_cm=64)
    prepared, correction = vision_models.prepara(im, cfg)
    assert correction["altezza_camera_cm"] == 64 and not correction["proporzioni_stimate"]
    assert prepared.size == tuple(rettifica(im, {k: cfg[k] for k in ("punti", "larghezza_cm", "lunghezza_cm", "altezza_camera_cm")})["dimensioni"])
    class Qwen:
        def analizza_web(self, photo):
            return _rilevamento_ai(photo, "qwen")
    server, token = crea_server(0, qwen=Qwen())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(vision_models, "servizio", return_value={"base": f"http://127.0.0.1:{server.server_port}", "token": token}):
            assert vision_models.analizza(prepared, "qwen")["conteggio"] == 3
        malformed = _rilevamento_ai(prepared, "qwen")
        for value in ([], {**malformed, "conteggio": True}, {**malformed, "oggetti": [1, 2, 3]},
                      {**malformed, "dimensioni": [1, 2]}, {**malformed, "oggetti": [{"centro": [2, 0]}]*3}):
            with patch.object(vision_models, "servizio", return_value={"base": "http://127.0.0.1:8772", "token": "test"}), patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(value).encode())):
                try:
                    vision_models.analizza(prepared, "qwen")
                except ValueError:
                    pass
                else:
                    raise AssertionError("risposta non valida accettata")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def verifica_ottica():
    import cv2
    import numpy as np
    from PIL import Image

    canvas = np.full((900,1200,3),235,dtype=np.uint8)
    cv2.rectangle(canvas,(65,65),(1135,835),(20,20,20),5)
    for i in range(20):
        x,y=160+(i%5)*210,165+(i//5)*190
        cv2.circle(canvas,(x,y),25+(i%3)*10,(30,30,30),4)
    im=Image.fromarray(canvas)
    result=vision.analizza(im,{})
    assert result["area"] is not None and len(result["centri"])==20, (result["area"], len(result["centri"]))
    assert len(result["cerchi"]) == 20 and all(r > 0 for x, y, r in result["cerchi"])
    assert result["coperchio"]=="non determinabile"
    photo=vision.fotografia(im)
    assert len(photo["jpeg"])<=350000
    assert vision.immagine(photo["jpeg"]).size==(1200,900)
    black=vision.analizza(Image.new("RGB",(640,480)),{},area_manuale=[[.1,.1],[.9,.1],[.9,.9],[.1,.9]])
    assert black["qualita"]=="non determinabile"
    # I riferimenti identificano il coperchio in posizione, non quello appoggiato accanto.
    dictionary=cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    locations=[(15,15),(1125,15),(1125,825),(15,825),(350,400),(800,400)]
    for i,(x,y) in enumerate(locations):
        marker=cv2.aruco.generateImageMarker(dictionary,i,60)
        canvas[y:y+60,x:x+60,:]=marker[:,:,None]
    area=[[.1,.1],[.9,.1],[.9,.9],[.1,.9]]
    im=Image.fromarray(canvas)
    base=vision.analizza(im,{},area_manuale=area)
    ref={k:base[k] for k in ("area","markers","miniatura")}
    for box in (False,True):
        # Nessun marcatore / solo scatola: aperto e chiuso indistinguibili
        # devono restare non determinabili anche se il bordo è leggibile.
        cfg={"marcatori_scatola":box,"marcatori_coperchio":False,"aperta":ref,"chiusa":ref}
        test=vision.analizza(im,cfg,area_manuale=area if not box else None)
        assert test["coperchio"]=="non determinabile"
        cfg={"marcatori_scatola":box,"marcatori_coperchio":True,"aperta":ref,"chiusa":ref}
        test=vision.analizza(im,cfg,area_manuale=area if not box else None)
        assert test["coperchio"]=="posizionato", (test["coperchio"], test["markers"].keys())
        moved=canvas.copy()
        moved[400:460,350:410,:]=235
        moved[600:660,350:410,:]=cv2.aruco.generateImageMarker(dictionary,4,60)[:,:,None]
        test=vision.analizza(Image.fromarray(moved),cfg,area_manuale=area if not box else None)
        assert test["coperchio"]=="non posizionato"
    print("PASS ottica: bordo, 20 cerchi misti, JPEG, nero, marcatori e coperchio disallineato")


def _run_all():
    import traceback

    tests=[f for n,f in sorted(globals().items()) if n.startswith("test_")]
    failed=0
    for fn in tests:
        try:
            fn()
            print("PASS",fn.__name__)
        except Exception:
            failed+=1
            traceback.print_exc()
    print(f"{len(tests)-failed}/{len(tests)} test superati")
    return int(bool(failed))


if __name__=="__main__":
    result=_run_all()
    if "--ottico" in sys.argv:
        verifica_ottica()
    sys.exit(result)
