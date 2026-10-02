"""Invii email cumulativi e archivio delle distinte attese.

Il mixin usa il database e le guardie del Workflow; non apre il lettore né
spedisce messaggi alla creazione di una bozza.
"""
from __future__ import annotations

import base64
import binascii
import copy
import datetime as dt
import io
import json
import uuid
import zipfile

from lims import mail_dispatch as mail
from lims.codec import epc_kind


def _now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class ExchangeMixin:
    """Operazioni serializzate dal dispatcher HTTP insieme al resto del flusso."""

    def impostazioni_email(self):
        cfg = mail.mail_config(self.config.get("email", {}) or {})
        import os
        return {**cfg, "credenziale_presente": bool(os.environ.get(cfg["password_env"]))}

    def salva_email(self, dati):
        self._require_operator("configurare l'email")
        try:
            cfg = mail.mail_config(dati)
        except (mail.MailError, ValueError, TypeError) as exc:
            raise self.exchange_error(str(exc)) from exc
        nuova = copy.deepcopy(self.config)
        nuova["email"] = cfg
        self._scrivi_config(nuova)
        self.config = nuova
        return self.impostazioni_email()

    def colli_email(self):
        self.verifica_operazione("colli_email")
        rows = self.db.connection.execute("""
            SELECT s.id, s.destinazione, s.state, s.data, o.filename,
                   o.item_count, o.manifest_uuid
              FROM shipments s LEFT JOIN outbound_manifests o ON o.shipment_id=s.id
             WHERE s.state IN ('sealed','exported','sent') ORDER BY s.id DESC
        """).fetchall()
        return {"colli": [dict(r) for r in rows], "invii": self.elenco_invii_email()}

    def elenco_invii_email(self):
        rows = self.db.connection.execute("""
            SELECT id, recipient, subject, created_at, created_by, state, last_error
              FROM mail_dispatches ORDER BY created_at DESC, rowid DESC LIMIT 50
        """).fetchall()
        return [dict(r) for r in rows]

    def _invio_row(self, dispatch_id):
        row = self.db.connection.execute("SELECT * FROM mail_dispatches WHERE id=?", (str(dispatch_id),)).fetchone()
        if row is None:
            raise self.exchange_error("invio email inesistente")
        return dict(row)

    def dettaglio_email(self, dispatch_id):
        self.verifica_operazione("dettaglio_email")
        row = self._invio_row(dispatch_id)
        row.pop("raw_eml")
        ids = json.loads(row.pop("shipment_ids"))
        row["allegati"] = [{"shipment_id": sid, "nome": self.db.outbound_manifest(sid)["filename"]} for sid in ids]
        return row

    def prepara_invio_email(self, shipment_ids):
        self.verifica_operazione("prepara_invio_email")
        self._require_operator("preparare un invio email")
        if not isinstance(shipment_ids, list) or not 1 <= len(shipment_ids) <= mail.MAX_FILES:
            raise self.exchange_error("selezionare da 1 a 50 colli")
        try:
            ids = sorted(set(int(sid) for sid in shipment_ids))
            rows = [self.db.shipment_row(sid) for sid in ids]
            cfg = self.impostazioni_email()
            sender = mail.address(cfg["sender"])
            destinations = {r["destinazione"] for r in rows}
            if len(destinations) != 1:
                raise mail.MailError("selezionare colli diretti allo stesso destinatario")
            if any(r["state"] not in {"sealed", "exported", "sent"} for r in rows):
                raise mail.MailError("ogni collo deve essere chiuso e verificato")
            destination = self.destinatario(rows[0]["destinazione"]) or {}
            recipient = mail.address(destination.get("email", ""))
        except (ValueError, TypeError, KeyError) as exc:
            raise self.exchange_error(str(exc)) from exc
        # Non cambiare la spedizione attiva sul banco durante la selezione multipla.
        previous = self.shipment_id
        manifests = []
        try:
            for sid in ids:
                self.shipment_id = sid
                manifests.append(self._garantisci_distinta_archiviata())
        finally:
            self.shipment_id = previous
        dispatch_id = uuid.uuid4().hex
        short = dispatch_id[:12].upper()
        lab = self.laboratorio()
        subject = f"Invio {short} — {lab.get('nome') or 'Magazzino'} → {rows[0]['destinazione']} — {len(ids)} colli"
        lines = [f"Si trasmettono le distinte cifrate dell'invio {short}.", "",
                 f"Colli previsti: {len(ids)}.", ""]
        for sid, manifest in zip(ids, manifests, strict=True):
            lines.append(f"Collo / spedizione {sid}: {manifest['item_count']} contenitori — {manifest['filename']}")
        lines += ["", "Importare tutti gli allegati nella schermata Ricezione.",
                  "All'arrivo, leggere il tag del collo per richiamare la distinta e verificarne il contenuto.",
                  "Se il collo non ha un tag, selezionare la distinta dall'elenco.", "",
                  "I dati dei pazienti sono contenuti esclusivamente negli allegati cifrati."]
        body = "\n".join(lines)
        try:
            raw, message_id = mail.build_message(sender, recipient, subject, body,
                [(m["filename"], bytes(m["encrypted_blob"])) for m in manifests])
        except mail.MailError as exc:
            raise self.exchange_error(str(exc)) from exc
        now = _now()
        with self.db.connection:
            self.db.connection.execute("""INSERT INTO mail_dispatches
                (id, shipment_ids, sender, recipient, subject, body, raw_eml, message_id,
                 created_at, created_by, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (dispatch_id, json.dumps(ids), sender, recipient, subject, body, raw,
                 message_id, now, self.operatore, now, self.operatore))
        return self.dettaglio_email(dispatch_id)

    def file_email(self, dispatch_id, formato="eml"):
        self.verifica_operazione("file_email")
        row = self._invio_row(dispatch_id)
        if formato == "eml":
            blob = bytes(row["raw_eml"])
            mime = "message/rfc822"
        elif formato == "zip":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for sid in json.loads(row["shipment_ids"]):
                    record = self.db.outbound_manifest(sid)
                    archive.writestr(record["filename"], bytes(record["encrypted_blob"]))
            blob = buffer.getvalue()
            mime = "application/zip"
        else:
            raise self.exchange_error("formato non disponibile")
        return {"nome": f"invio_{row['id']}.{formato}", "tipo": mime,
                "contenuto_base64": base64.b64encode(blob).decode("ascii")}

    def invia_email(self, dispatch_id):
        self.verifica_operazione("invia_email")
        self._require_operator("inviare l'email")
        row = self._invio_row(dispatch_id)
        if row["state"] not in {"prepared", "failed"}:
            raise self.exchange_error("invio già eseguito o con esito incerto: controllare la posta prima di preparare un nuovo invio")
        if (self.config.get("pec", {}) or {}).get("enabled"):
            raise self.exchange_error("questa sede richiede la PEC: usare il canale PEC configurato")
        with self.db.connection:
            changed = self.db.connection.execute("""UPDATE mail_dispatches SET state='sending', updated_at=?, updated_by=?
                WHERE id=? AND state IN ('prepared','failed')""", (_now(), self.operatore, row["id"])).rowcount
        if changed != 1:
            raise self.exchange_error("invio già in corso")
        try:
            mail.send_message(self.config.get("email", {}) or {}, bytes(row["raw_eml"]), row["sender"], row["recipient"])
        except mail.MailError as exc:
            state = "send_unknown" if isinstance(exc, mail.SendUncertain) else "failed"
            with self.db.connection:
                self.db.connection.execute("UPDATE mail_dispatches SET state=?, last_error=?, updated_at=? WHERE id=?",
                    (state, str(exc), _now(), row["id"]))
            raise self.exchange_error(str(exc)) from exc
        with self.db.connection:
            self.db.connection.execute("UPDATE mail_dispatches SET state='smtp_accepted', last_error='', updated_at=? WHERE id=?",
                (_now(), row["id"]))
        return self.dettaglio_email(dispatch_id)

    def conferma_email_manuale(self, dispatch_id):
        self.verifica_operazione("conferma_email_manuale")
        self._require_operator("confermare l'invio manuale dell'email")
        row = self._invio_row(dispatch_id)
        if row["state"] == "smtp_accepted":
            raise self.exchange_error("email già accettata dal server")
        with self.db.connection:
            self.db.connection.execute("UPDATE mail_dispatches SET state='manual_sent', updated_at=?, updated_by=? WHERE id=?", (_now(), self.operatore, row["id"]))
        return self.dettaglio_email(dispatch_id)

    def importa_distinte(self, files):
        self.verifica_operazione("importa_distinte")
        self._require_operator("importare le distinte")
        if not isinstance(files, list) or not 1 <= len(files) <= mail.MAX_FILES:
            raise self.exchange_error("selezionare da 1 a 50 file")
        results = []
        expanded = []
        total = 0
        for item in files:
            if not isinstance(item, dict):
                raise self.exchange_error("elenco file non valido")
            name = str(item.get("nome", ""))[:200]
            try:
                blob = base64.b64decode(str(item.get("contenuto_base64", "")), validate=True)
                contents = mail.unpack_files(name, blob)
                total += sum(len(data) for _, data in contents)
                if total > mail.MAX_TOTAL_BYTES or len(expanded) + len(contents) > mail.MAX_FILES:
                    raise self.exchange_error("selezione oltre 50 distinte o 2 MiB: importare in più gruppi")
                expanded.extend(contents)
            except (binascii.Error, ValueError) as exc:
                results.append({"nome": name, "esito": "rifiutata", "errore": str(exc)})
        for name, blob in expanded:
            try:
                result = self.importa_distinta(blob, attiva=False)
                results.append({"nome": name, "esito": "già presente" if result["gia_importata"] else "importata",
                                "inbound_id": result["inbound_id"]})
            except self.exchange_error as exc:
                results.append({"nome": name, "esito": "rifiutata", "errore": str(exc)})
        return {"risultati": results, **self.distinte_attese()}

    def distinte_attese(self):
        self.verifica_operazione("distinte_attese")
        # Indicizza le distinte precedenti alla migrazione senza attivarle.
        old = self.db.connection.execute("SELECT id, encrypted_blob FROM inbound_shipments WHERE box_epc IS NULL").fetchall()
        errors = []
        for row in old:
            try:
                self.importa_distinta(bytes(row["encrypted_blob"]), attiva=False)
            except self.exchange_error:
                errors.append(int(row["id"]))
        rows = self.db.connection.execute("""SELECT id, origin_lab_id, origin_shipment_id,
            manifest_uuid, imported_at, state, box_epc, item_count, confirmed_at
            FROM inbound_shipments ORDER BY imported_at DESC, id DESC""").fetchall()
        return {"distinte": [dict(r) for r in rows], "non_indicizzate": errors}

    def seleziona_distinta(self, inbound_id):
        self.verifica_operazione("seleziona_distinta")
        self._require_operator("selezionare la distinta")
        row = self.db.inbound_row(int(inbound_id))
        return self.importa_distinta(bytes(row["encrypted_blob"]))

    def riconosci_collo(self):
        self.verifica_operazione("riconosci_collo")
        self._require_operator("riconoscere il collo")
        self.distinte_attese()
        self._invalida_lettura_ricezione()
        survey = self._tagio(scrittura=False).survey_field(expected_epcs=None)
        if survey.error:
            raise self.exchange_error(f"lettura del collo non riuscita: {survey.error}")
        boxes = {o.epc.upper() for o in survey.observations if epc_kind(o.epc) == "box"}
        if len(boxes) != 1:
            message = "nessun tag di collo letto: riprovare o selezionare la distinta" if not boxes else "più colli nel campo: presentare una sola scatola"
            raise self.exchange_error(message)
        box = next(iter(boxes))
        rows = self.db.connection.execute("SELECT id FROM inbound_shipments WHERE box_epc=? AND state <> 'received'", (box,)).fetchall()
        if not rows:
            raise self.exchange_error("nessuna distinta in attesa per questo collo: importarla o controllare le ricezioni già completate")
        if len(rows) != 1:
            raise self.exchange_error("più distinte aperte per lo stesso collo: selezionare e verificare manualmente quella corretta")
        self.seleziona_distinta(rows[0]["id"])
        result = self._riconcilia_rilievo(survey)
        result["distinta"] = self.stato_ricezione()
        return result

    def _invalida_lettura_ricezione(self):
        """Una nuova lettura fallita non permette di confermare la prova precedente."""
        if self.inbound_id is not None:
            with self.db.connection:
                self.db.connection.execute("UPDATE inbound_shipments SET scan_valid=0 WHERE id=?", (self.inbound_id,))
