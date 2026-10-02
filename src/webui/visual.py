"""Controllo aggiuntivo del collo, senza modificare le regole del sigillo RFID."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import threading
import time
import uuid
from dataclasses import replace

from cryptography.exceptions import InvalidTag

from lims import vision
from lims.crypto import CryptoError
from lims.sealing import ClosureProof, ReadPass, SealingPolicy, SealingSession, default_passes

from . import vision_models


def ora():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class VisualMixin:
    def foto_visiva_ricevuta(self, dati):
        self._require_operator("consultare la prova visiva")
        if self.distinta is None or dati.get("inbound_id") != self.inbound_id:
            raise self.exchange_error("selezionare nuovamente la distinta ricevuta")
        return {"prova": self.distinta.visual_check}

    def _visual_init(self):
        if not hasattr(self, "_visual_lock"):
            self._visual_lock = threading.RLock()
            self._visual_session = None

    def _visual_config(self):
        row = self.db.connection.execute("SELECT encrypted_blob FROM visual_settings WHERE id=1").fetchone()
        return vision.apri(row[0], self.keyring, "config") if row else {}

    def _visual_hash(self, sid):
        values = [(r.container_id, r.epc) for r in self.db.shipment_contents(sid)]
        return hashlib.sha256(json.dumps(sorted(values)).encode()).hexdigest()

    def visual_documento(self, sid=None):
        sid = self.shipment_id if sid is None else sid
        if sid is None:
            return None
        row = self.db.connection.execute("SELECT content_hash, encrypted_blob FROM visual_checks WHERE shipment_id=?", (sid,)).fetchone()
        if not row:
            return None
        if row[0] != self._visual_hash(sid):
            return {"stato": "invalidato", "motivo": "contenuto modificato"}
        try:
            return vision.apri(row[1], self.keyring, f"spedizione:{sid}")
        except (InvalidTag, ValueError, KeyError, CryptoError):
            return {"stato": "non leggibile", "motivo": "prova non autenticabile con le chiavi disponibili"}

    def _visual_save(self, sid, documento):
        if self.db.outbound_manifest(sid):
            raise self.exchange_error("distinta già archiviata: la prova non può essere modificata")
        blob = vision.cifra(documento, self.keyring, f"spedizione:{sid}")
        with self.db.connection:
            self.db.connection.execute("INSERT OR REPLACE INTO visual_checks VALUES (?,?,?)", (sid, self._visual_hash(sid), blob))

    def _visual_context(self, dati, *, modifica=True):
        self._require_operator("controllare visivamente la scatola")
        self.verifica_operazione("controllo_visivo")
        sid = dati.get("shipment_id")
        if not self.shipment_id or sid != self.shipment_id:
            raise self.exchange_error("la spedizione è cambiata: riaprire il controllo")
        row = self.db.shipment_row(sid)
        if modifica and (self.db.outbound_manifest(sid) or row["state"] not in ("open", "sealed")):
            raise self.exchange_error("spedizione non modificabile: consultare la prova archiviata")
        if modifica and self.riempimento is not None:
            raise self.exchange_error("terminare il riempimento prima del controllo visivo")
        fingerprint = self._visual_hash(sid)
        session = self._visual_session
        if session is None or session["sid"] != sid or session["hash"] != fingerprint:
            if session and session.get("stop"):
                session["stop"].set()
            session = {"sid": sid, "hash": fingerprint, "id": uuid.uuid4().hex, "history": [], "rfid": None,
                       "latest": None, "corretti": None, "pending": None}
            self._visual_session = session
        if dati.get("sessione") and dati["sessione"] != session["id"]:
            raise self.exchange_error("il contenuto o la sessione visiva è cambiato")
        return session

    def _visual_reset(self, s, *, conserva_ai=False):
        if s.get("stop"):
            s["stop"].set()
        s.update(id=uuid.uuid4().hex, history=[], rfid=None, corretti=None, pending=None, stable=False)
        if not conserva_ai:
            s.pop("ai", None)

    def _visual_modello(self, dati):
        with self._visual_lock:
            s = self._visual_context(dati)
            model = dati.get("modello")
            if model not in ("sam2", "qwen"):
                raise ValueError("scegliere SAM 2 oppure Qwen")
            if dati.get("profilo") is None:
                raise ValueError("configurare prima l'area della webcam nel banco SAM 2 / Qwen")
            raw = vision.immagine(dati.get("immagine_base64"))
            im, correction = vision_models.prepara(raw, dati.get("profilo"))
            self._visual_reset(s)
            s.update(modello=model, profilo=dati.get("profilo"), latest=None, stable=False)
            ticket = s["id"]
            self._visual_save(s["sid"], {"stato": "da verificare", "operatore": self.operatore, "quando": ora()})
        # L'inferenza non occupa la radio e non blocca l'invalidazione della scena.
        detected = vision_models.analizza(im, model)
        centers = [obj["centro"] for obj in detected.get("oggetti", [])]
        outline = detected.get("borsa", {}).get("contorno")
        if outline:
            xs, ys = zip(*outline, strict=True)
            polygon = [[min(xs), min(ys)], [max(xs), min(ys)], [max(xs), max(ys)], [min(xs), max(ys)]]
        else:
            polygon = [[0, 0], [1, 0], [1, 1], [0, 1]]
        scene = vision.analizza(im, self._visual_config(), area_manuale=polygon, solo_scena=True)
        scene.update(centri=centers, modello=detected.get("modello", model), motore=model,
                     oggetti=detected.get("oggetti", []), tipo_overlay=detected.get("tipo_overlay"),
                     borsa=detected.get("borsa"), area_campioni=detected.get("area_campioni"),
                     origine_area="bordo SAM 2" if detected.get("borsa", {}).get("rilevata") else "area dello scatto webcam",
                     tempo_secondi=detected.get("tempo_secondi"), correzione=correction,
                     nota=detected.get("nota", ""), avvisi=detected.get("avvisi", []))
        uncertain = detected.get("conteggio") is None or bool(detected.get("incerto"))
        with self._visual_lock:
            current = self._visual_context({"shipment_id": s["sid"]})
            if current is not s or ticket != s["id"]:
                raise ValueError("scatto superato: contenuto o sessione modificati durante il riconoscimento")
            s["ai"] = {"im": im.copy(), "scene": scene, "incerto": uncertain,
                       "conteggio": detected.get("conteggio"), "modello": model}
            s.update(latest=scene, im=im, seen=time.monotonic(), stable=False)
            return {**{k: v for k, v in scene.items() if k != "miniatura"},
                    "sessione": s["id"], "conteggio": detected.get("conteggio"), "confermati": centers,
                    "qualita": "incerta" if uncertain else scene["qualita"], "stabile": False,
                    "manuale": False, "rfid": None, "immagine_base64": vision_models.jpeg(im)}

    def _visual_analisi(self, s, dati):
        cfg = self._visual_config()
        im = vision.immagine(dati.get("immagine_base64"))
        model = dati.get("modello", s.get("modello", "cerchi"))
        if model not in vision_models.MODELLI:
            raise ValueError("modello di riconoscimento non valido")
        if model != s.get("modello", "cerchi"):
            self._visual_reset(s)
        s["modello"] = model
        if model != "cerchi":
            try:
                im, _ = vision_models.prepara(im, dati.get("profilo"))
            except ValueError:
                self._visual_reset(s)
                raise
            ai = s.get("ai")
            if ai and (dati.get("profilo") != s.get("profilo") or not vision_models.scena_uguale(ai["im"], im,
                    s["corretti"] if s["corretti"] is not None else ai["scene"]["centri"])):
                self._visual_reset(s)
                ai = None
                self._visual_save(s["sid"], {"stato": "invalidato", "motivo": "scena modificata dopo il riconoscimento", "quando": ora(), "operatore": self.operatore})
            if ai is None:
                s.update(stable=False, seen=time.monotonic(), im=im, latest=None)
                return {"sessione": s["id"], "stabile": False, "conteggio": None, "confermati": [],
                        "qualita": "da riconoscere", "manuale": False, "rfid": None, "dimensioni": list(im.size),
                        "area": None, "motore": model}
            observed = vision.analizza(im, cfg, area_manuale=ai["scene"]["area"], solo_scena=True)
            result = {**ai["scene"], "miniatura": observed.get("miniatura"), "qualita": observed["qualita"]}
            if ai["incerto"] and s["corretti"] is None:
                result["qualita"] = "incerta"
        else:
            result = vision.analizza(im, cfg, area_manuale=dati.get("area_manuale"))
        last = s.get("latest")
        same = (last is not None and result["qualita"] == "leggibile" and last["qualita"] == "leggibile"
                and vision.vicini(last["area"], result["area"], .025)
                and vision.vicini(last["centri"], result["centri"]))
        if same:
            # Rileva anche sostituzioni/spostamenti a conteggio invariato.
            same = sum(abs(a-b) for a, b in zip(last["miniatura"], result["miniatura"], strict=True)) / 3072 < 12
        if not same and model == "cerchi":
            self._visual_reset(s)
        now = time.monotonic()
        s["history"] = (s["history"] + [now])[-3:] if result["qualita"] == "leggibile" else []
        s["latest"], s["im"], s["seen"] = result, im, now
        s["stable"] = len(s["history"]) == 3 and now - s["history"][0] >= 1
        centres = s["corretti"] if s["corretti"] is not None else result["centri"]
        return {**{k: v for k, v in result.items() if k != "miniatura"}, "sessione": s["id"],
                "stabile": s["stable"], "confermati": centres, "conteggio": len(centres),
                "manuale": s["corretti"] is not None, "rfid": s["rfid"],
                **({"conteggio": None} if model != "cerchi" and s["ai"]["conteggio"] is None and s["corretti"] is None else {})}

    def controllo_visivo(self, dati):
        self._visual_init()
        try:
            if dati.get("azione") == "analizza_modello":
                return self._visual_modello(dati)
            with self._visual_lock:
                self._require_operator("usare il controllo visivo")
                azione = dati.get("azione", "stato")
                if azione == "marcatori":
                    return {"marcatori": vision.pagina_marcatori()}
                if azione == "impostazioni":
                    cfg = self._visual_config()
                    service = vision_models.servizio()
                    return {"config": {k: v for k, v in cfg.items() if k not in ("aperta", "chiusa")},
                            "console_modelli": service["console"] if service else None,
                            "calibrata_aperta": bool(cfg.get("aperta")), "calibrata_chiusa": bool(cfg.get("chiusa"))}
                if azione == "configura":
                    cfg = self._visual_config()
                    new = {k: dati.get(k) is True for k in ("marcatori_scatola", "marcatori_coperchio", "controlla_coperchio")}
                    for key, default, low, high in (("raggio_min", .012, .005, .2), ("raggio_max", .16, .01, .4), ("sensibilita", 30, 10, 80)):
                        new[key] = float(dati.get(key, default))
                        if not low <= new[key] <= high:
                            raise ValueError("parametri visivi fuori intervallo")
                    if new["raggio_min"] >= new["raggio_max"]:
                        raise ValueError("raggio minimo maggiore del massimo")
                    new["camera"] = str(dati.get("camera", ""))[:300]
                    new["modello"] = dati.get("modello", cfg.get("modello", "cerchi"))
                    if new["modello"] not in vision_models.MODELLI:
                        raise ValueError("modello di riconoscimento non valido")
                    # Un'altra camera o un'altra modalità richiedono una calibrazione nuova.
                    geometry = ("camera", "marcatori_scatola", "marcatori_coperchio")
                    if all(new[k] == cfg.get(k, False if k != "camera" else "") for k in geometry):
                        new.update({k: cfg[k] for k in ("aperta", "chiusa") if k in cfg})
                    self._visual_save_config(new)
                    if self._visual_session:
                        self._visual_reset(self._visual_session)
                    return {"ok": True}
                if azione == "archivio":
                    sid = int(dati["shipment_id"])
                    self.db.shipment_row(sid)
                    return {"prova": self.visual_documento(sid)}
                if azione == "anteprima_configurazione":
                    im = vision.immagine(dati.get("immagine_base64"))
                    if dati.get("modello") in ("sam2", "qwen"):
                        im, _ = vision_models.prepara(im, dati.get("profilo"))
                    result = vision.analizza(im, self._visual_config(), area_manuale=dati.get("area_manuale"))
                    return {**{k: v for k, v in result.items() if k != "miniatura"},
                            "sessione": None, "stabile": False, "confermati": result["centri"],
                            "conteggio": len(result["centri"]), "manuale": False, "rfid": None}
                if azione == "calibra":
                    cfg = self._visual_config()
                    area = vision.punti(dati.get("area"), quattro=True)
                    im = vision.immagine(dati.get("immagine_base64"))
                    if dati.get("modello") in ("sam2", "qwen"):
                        im, _ = vision_models.prepara(im, dati.get("profilo"))
                    result = vision.analizza(im, cfg, area_manuale=area)
                    if result["qualita"] != "leggibile":
                        raise ValueError("immagine non leggibile per la calibrazione")
                    tipo = dati.get("tipo")
                    if tipo not in ("aperta", "chiusa"):
                        raise ValueError("scegliere scatola aperta o chiusa")
                    if cfg.get("marcatori_scatola") and not all(str(i) in result["markers"] for i in range(4)):
                        raise ValueError("devono essere visibili i quattro marcatori scatola 0–3")
                    if tipo == "chiusa" and cfg.get("marcatori_coperchio") and not all(str(i) in result["markers"] for i in (4, 5)):
                        raise ValueError("devono essere visibili i marcatori coperchio 4 e 5")
                    cfg[tipo] = {k: result[k] for k in ("area", "markers", "miniatura")}
                    self._visual_save_config(cfg)
                    if self._visual_session:
                        self._visual_reset(self._visual_session)
                    return {"ok": True}
                s = self._visual_context(dati, modifica=azione != "stato")
                if azione == "stato":
                    return {"sessione": s["id"], "prova": self.visual_documento(s["sid"])}
                if azione == "analizza":
                    return self._visual_analisi(s, dati)
                if azione == "correggi":
                    if not s["latest"] or s["latest"]["qualita"] not in ("leggibile", "incerta"):
                        raise ValueError("analizzare prima un'immagine leggibile")
                    centri = vision.punti(dati.get("centri"))
                    import cv2
                    import numpy as np

                    poly = np.array(s["latest"]["area"], dtype="float32")
                    if any(cv2.pointPolygonTest(poly, tuple(p), False) < 0 for p in centri):
                        raise ValueError("marcatore esterno alla scatola")
                    self._visual_reset(s, conserva_ai=True)
                    s["corretti"], s["stable"] = centri, True
                    s["seen"] = time.monotonic()
                    return {"sessione": s["id"], "conteggio": len(centri), "stabile": True}
                if azione in ("salta", "invalida"):
                    self._visual_reset(s)
                    self._visual_save(s["sid"], {"stato": "saltato" if azione == "salta" else "invalidato", "operatore": self.operatore, "quando": ora()})
                    return {"ok": True}
                if azione == "prepara_foto":
                    previous = s["id"]
                    self._visual_analisi(s, dati)
                    if previous != s["id"] or not s.get("rfid") or not s["rfid"]["concorde"]:
                        raise ValueError("scena cambiata o confronto non concordante: ripetere la verifica")
                    if time.monotonic() - s["rfid"]["_fine"] > 60:
                        raise ValueError("lettura RFID non più recente: ripetere la verifica")
                    photo = vision.fotografia(s["im"])
                    s["pending"] = {"token": uuid.uuid4().hex, "foto": photo, "quando": ora(), "creata": time.monotonic()}
                    return {**s["pending"], "centri": s["corretti"] if s["corretti"] is not None else s["latest"]["centri"], "manuale": s["corretti"] is not None}
                if azione == "conferma_foto":
                    pending = s.get("pending")
                    if not pending or dati.get("token") != pending["token"] or time.monotonic()-pending["creata"] > 60:
                        raise ValueError("foto scaduta: acquisire un nuovo fotogramma")
                    cfg = self._visual_config()
                    documento = {"versione": 1, "stato": "concordante", "operatore": self.operatore, "quando": pending["quando"],
                                 "contenuto_hash": s["hash"], "automatici": s["latest"]["centri"],
                                 "confermati": s["corretti"] if s["corretti"] is not None else s["latest"]["centri"],
                                 "manuale": s["corretti"] is not None, "area": s["latest"]["area"],
                                 "origine_area": s["latest"]["origine_area"],
                                 "riconoscimento": {k: s["latest"][k] for k in ("motore", "modello", "tempo_secondi", "nota", "avvisi", "correzione") if k in s["latest"]},
                                 "config": {k: v for k, v in cfg.items() if k not in ("aperta", "chiusa")},
                                 "rfid": {k: v for k, v in s["rfid"].items() if not k.startswith("_")},
                                 "foto_contenuto": pending["foto"],
                                 "coperchio": {"stato": "da verificare" if cfg.get("controlla_coperchio") else "non richiesto"}}
                    self._visual_save(s["sid"], documento)
                    s["pending"] = None
                    return {"ok": True, "prova": documento}
                if azione == "foto_coperchio":
                    doc = self.visual_documento(s["sid"])
                    if not doc or doc.get("stato") != "concordante":
                        raise ValueError("confermare prima la foto del contenuto")
                    im = vision.immagine(dati.get("immagine_base64"))
                    if dati.get("modello") in ("sam2", "qwen"):
                        im, _ = vision_models.prepara(im, dati.get("profilo"))
                    cfg = self._visual_config()
                    result = vision.analizza(im, cfg, area_manuale=dati.get("area_manuale"))
                    if not cfg.get("controlla_coperchio"):
                        raise ValueError("controllo coperchio non attivo")
                    doc["coperchio"] = {"stato": result["coperchio"], "quando": ora(), "operatore": self.operatore,
                                        "agganci": "da confermare dall'operatore nella sigillatura"}
                    if result["coperchio"] == "posizionato":
                        doc["foto_coperchio"] = vision.fotografia(im)
                    else:
                        doc.pop("foto_coperchio", None)
                    self._visual_save(s["sid"], doc)
                    return {"ok": True, "prova": doc}
                raise ValueError("azione visiva non riconosciuta")
        except (ValueError, TypeError, KeyError, ImportError, RuntimeError, OSError, InvalidTag, CryptoError) as exc:
            raise self.exchange_error(str(exc)) from exc

    def visual_finalizza(self, record):
        """Il riscontro preliminare rimane distinto dalla certificazione finale."""
        doc = self.visual_documento()
        if not doc or self.db.outbound_manifest(self.shipment_id):
            return
        doc["sigillo_finale"] = {"quando": record.finished_at, "trovati": len(record.found),
            "mancanti": list(record.missing), "estranei": list(record.unexpected),
            "concorde": bool(record.ok and not record.unexpected and len(record.found) == len(doc.get("confermati", []))),
            "agganci": record.closure_proof.value}
        self._visual_save(self.shipment_id, doc)

    def _visual_save_config(self, cfg):
        blob = vision.cifra(cfg, self.keyring, "config")
        with self.db.connection:
            self.db.connection.execute("INSERT OR REPLACE INTO visual_settings VALUES (1,?)", (blob,))

    def recupera_visivo(self, dati, *, stop_event, on_progress=None):
        self._visual_init()
        with self._visual_lock:
            s = self._visual_context(dati)
            if not s.get("stable") or time.monotonic()-s.get("seen", 0) > 5:
                raise self.exchange_error("attendere un conteggio visivo stabile e recente")
            centri = s["corretti"] if s["corretti"] is not None else s["latest"]["centri"]
            expected = [r.epc for r in self.db.shipment_contents(s["sid"]) if r.epc]
            if len(centri) != len(expected) or not expected:
                raise self.exchange_error("il numero visivo differisce dalla distinta: controllare il contenuto")
            ident = s["id"]
            s["stop"], s["rfid"] = stop_event, None
            self._visual_save(s["sid"], {"stato": "da verificare", "quando": ora(), "operatore": self.operatore})
        antennas = self.antenne_lettura()
        powers = [int(p) for p in self.lims_cfg.get("seal_powers_cdbm", [2000, 2500, 2900])]
        limits = {int(a["id"]): int(a.get("read_power", 2000)) for a in self.config.get("antennas", [])}
        if self.config.get("reader", {}).get("region", 8) != 8:
            raise self.exchange_error("il controllo richiede la regione EU configurata")
        passes = [ReadPass(antennas=antennas, label="assetto operativo")]
        # Nessuna modifica della potenza di scrittura durante il recupero.
        for p in default_passes(antennas, powers_cdbm=powers, high_sensitivity_rf_mode=None):
            cap = min(limits[a] for a in p.antennas)
            power = min(p.read_power_cdbm, cap)
            label = p.label if power == p.read_power_cdbm else f"{p.label} (limitata a {power/100:g} dBm)"
            passes.append(replace(p, read_power_cdbm=power, label=label))
        snapshot = self._snapshot_gen2()
        if snapshot.rf_mode is not None:
            passes.append(ReadPass(antennas=antennas, session=2, target=0, target_dynamic=True,
                                   rf_mode=0x71, label="alta sensibilità, se supportata dal lettore"))
        session = SealingSession(self.backend, expected, passes=passes,
            policy=SealingPolicy(min_antennas=int(self.lims_cfg.get("seal_min_antennas", 1)), max_passes=30, max_seconds=120),
            region=8, operator=self.operatore)
        original_apply = session._apply
        assetti = []
        avvisi = []

        def apply(p):
            from lims.responses import ServiceCallError

            if p.read_power_cdbm is not None:
                from lims.responses import raise_for_status

                settings = self._reader_settings()
                settings = replace(settings, powers=tuple(replace(a, read_power_cdbm=p.read_power_cdbm)
                    if a.antenna_id in p.antennas else a for a in settings.powers))
                raise_for_status(self.backend.configure(settings), "potenze recupero visivo")
            desired = replace(p, read_power_cdbm=None)
            try:
                original_apply(desired)
                actual = self._snapshot_gen2()
                if desired.rf_mode is not None and actual.rf_mode != desired.rf_mode:
                    raise ValueError("modalità RF richiesta non applicata dal lettore")
            except (ServiceCallError, ValueError) as exc:
                if desired.rf_mode is None:
                    raise
                # La modalità facoltativa può non essere supportata dal firmware.
                # Ripetere con quella salvata, verificandola prima dell'inventory.
                avvisi.append(f"Alta sensibilità non disponibile: {exc}; uso modalità RF operativa")
                desired = replace(desired, rf_mode=snapshot.rf_mode)
                original_apply(desired)
                actual = self._snapshot_gen2()
            for key in ("session", "target", "rf_mode"):
                requested = getattr(desired, key)
                if requested is not None and getattr(actual, key) != requested:
                    raise ValueError(f"parametro Gen2 {key} non applicato")
            if desired.target is not None and actual.target_dynamic != desired.target_dynamic:
                raise ValueError("alternanza target Gen2 non applicata")
            assetti.append({"session": actual.session, "target": actual.target,
                "target_dynamic": actual.target_dynamic, "rf_mode": actual.rf_mode})
        session._apply = apply
        try:
            record = session.run(closure_proof=ClosureProof.NONE, on_progress=on_progress, stop_event=stop_event)
        finally:
            try:
                self._restore_radio(snapshot)
                if self._snapshot_gen2() != snapshot:
                    raise self.exchange_error("ripristino Gen2 non confermato dal lettore: riconnettere")
            except Exception:
                self.radio_configurata = False
                raise
        with self._visual_lock:
            if self._visual_session is not s or ident != s["id"] or s["hash"] != self._visual_hash(s["sid"]):
                self._visual_save(s["sid"], {"stato": "invalidato", "motivo": "scena modificata durante le letture", "quando": ora(), "operatore": self.operatore})
                raise self.exchange_error("scena modificata durante le letture: ripetere il controllo")
            result = record.to_dict()
            for passata, assetto in zip(result["passes"], assetti, strict=False):
                passata["gen2_effettivo"] = assetto
            result["avvisi"] = avvisi
            result.update(visibili=len(centri), concorde=bool(record.ok and not record.unexpected and not stop_event.is_set()), _fine=time.monotonic())
            s["rfid"] = result
            s.pop("stop", None)
            self._visual_save(s["sid"], {"stato": "da confermare" if result["concorde"] else "non concordante",
                "quando": ora(), "operatore": self.operatore, "confermati": centri,
                "rfid": {k: v for k, v in result.items() if not k.startswith("_")}})
            return result
