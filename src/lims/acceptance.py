"""Archivio dell'accettazione giornaliera: bozze, progressi e coda persistente."""

from __future__ import annotations

import datetime as dt
from typing import Any

from .model import Patient


def giorno_italiano(value: str | None = None) -> str:
    """Giorno civile italiano, anche su Windows senza pacchetto tzdata."""
    if value:
        return dt.date.fromisoformat(value).isoformat()
    return data_italiana(dt.datetime.now(dt.timezone.utc).isoformat())


def data_italiana(value: str) -> str:
    istante = dt.datetime.fromisoformat(value)
    if istante.tzinfo is None:
        return istante.date().isoformat()  # vecchi archivi: ora locale della sede
    utc = istante.astimezone(dt.timezone.utc)

    # Regola europea vigente: ultima domenica di marzo/ottobre, alle 01 UTC.
    def domenica(mese):
        ultimo = dt.datetime(utc.year, mese, 31, 1, tzinfo=dt.timezone.utc)
        return ultimo - dt.timedelta(days=(ultimo.weekday() + 1) % 7)

    ore = 2 if domenica(3) <= utc < domenica(10) else 1
    return (utc + dt.timedelta(hours=ore)).date().isoformat()


class AcceptanceStore:
    def __init__(self, db):
        self.db = db
        self.conn = db.connection

    def dettaglio(self, patient_id: int) -> dict[str, Any]:
        p = self.db.get_patient(patient_id)
        paziente = {
            "id": p.id,
            "codice": f"P{p.id:06d}",
            "nome": p.nome,
            "cognome": p.cognome,
            "codice_fiscale": p.codice_fiscale,
            "sesso": p.sesso.value,
            "data_nascita": p.data_nascita.isoformat() if p.data_nascita else "",
        }
        casi = []
        for row in self.conn.execute(
            "SELECT * FROM cases WHERE patient_id=? ORDER BY created_at DESC, id DESC",
            (patient_id,),
        ).fetchall():
            caso = dict(row)
            caso["giorno"] = data_italiana(caso["created_at"])
            reperti = []
            for s in self.conn.execute(
                "SELECT * FROM specimens WHERE case_id=? ORDER BY id", (caso["id"],)
            ).fetchall():
                reperto = dict(s)
                reperto["contenitori"] = [
                    dict(c)
                    for c in self.conn.execute(
                        "SELECT * FROM containers WHERE specimen_id=? ORDER BY idx, id", (s["id"],)
                    ).fetchall()
                ]
                reperti.append(reperto)
            caso["reperti"] = reperti
            vivi = [c for s in reperti for c in s["contenitori"] if c["state"] != "voided"]
            caso["campioni"] = sum(
                any(c["state"] != "voided" for c in s["contenitori"]) for s in reperti
            )
            caso["totale"] = len(vivi)
            caso["verificati"] = sum(
                bool(c["provisioned_at"]) and c["state"] != "planned" for c in vivi
            )
            caso["errori"] = sum(
                bool(c["last_write_error"]) and c["state"] == "planned" for c in vivi
            )
            caso["stato"] = (
                "annullato"
                if caso["cancelled_at"] or (reperti and not vivi)
                else "da_preparare"
                if not vivi
                else "completato"
                if caso["verificati"] == len(vivi)
                else "parziale"
                if caso["verificati"]
                else "da_scrivere"
            )
            caso["modificabile"] = not caso["frozen_at"] and caso["stato"] != "annullato"
            casi.append(caso)
        return {"paziente": paziente, "accettazioni": casi}

    def giornata(
        self, data: str | None = None, query: str = "", filtro: str = "tutti"
    ) -> dict[str, Any]:
        giorno = giorno_italiano(data)
        if filtro not in {
            "tutti",
            "da_preparare",
            "da_scrivere",
            "parziale",
            "completato",
            "errori",
        }:
            raise ValueError("filtro accettazioni non valido")
        oggi, arretrati = [], []
        totali = {"pazienti": 0, "campioni": 0, "contenitori": 0, "verificati": 0, "da_scrivere": 0}
        # Esclude lo storico concluso, senza affidare a SQLite il fuso italiano.
        # Il margine di un giorno include gli istanti salvati con offset diversi.
        data_base = dt.date.fromisoformat(giorno)
        limiti = (
            (data_base + dt.timedelta(days=2)).isoformat(),
            (data_base - dt.timedelta(days=1)).isoformat(),
        )
        ids = [
            r[0]
            for r in self.conn.execute(
                """
            SELECT DISTINCT patient_id FROM cases k WHERE created_at<? AND (
                created_at>=? OR (cancelled_at IS NULL AND (
                    NOT EXISTS (SELECT 1 FROM specimens s WHERE s.case_id=k.id) OR
                    EXISTS (SELECT 1 FROM specimens s JOIN containers c ON c.specimen_id=s.id
                            WHERE s.case_id=k.id AND c.state<>'voided'
                              AND (c.state='planned' OR c.provisioned_at IS NULL))
                ))
            )
        """,
                limiti,
            )
        ]
        for pid in ids:
            dettaglio = self.dettaglio(pid)
            p = dettaglio["paziente"]
            match = (
                query.strip().casefold() in " ".join(str(v or "") for v in p.values()).casefold()
            )
            for destinazione, casi in (
                (oggi, [c for c in dettaglio["accettazioni"] if c["giorno"] == giorno]),
                (
                    arretrati,
                    [
                        c
                        for c in dettaglio["accettazioni"]
                        if c["giorno"] < giorno and c["stato"] not in {"completato", "annullato"}
                    ],
                ),
            ):
                if not casi:
                    continue
                riga = {
                    **p,
                    "accettazioni": [c["accession_id"] for c in casi],
                    **{
                        k: sum(c[k] for c in casi)
                        for k in ("campioni", "totale", "verificati", "errori")
                    },
                }
                stati = {c["stato"] for c in casi}
                riga["stato"] = (
                    "annullato"
                    if stati == {"annullato"}
                    else "completato"
                    if stati <= {"completato", "annullato"}
                    else "parziale"
                    if riga["verificati"]
                    else "da_scrivere"
                    if riga["totale"]
                    else "da_preparare"
                )
                if destinazione is oggi:
                    totali["pazienti"] += 1
                    for k in ("campioni", "verificati"):
                        totali[k] += riga[k]
                    totali["contenitori"] += riga["totale"]
                    totali["da_scrivere"] += riga["totale"] - riga["verificati"]
                if match and (
                    filtro == "tutti" or (filtro == "errori" and riga["errori"]) or filtro in stati
                ):
                    destinazione.append(riga)
        for elenco in (oggi, arretrati):
            elenco.sort(key=lambda p: (p["cognome"], p["nome"], p["id"]))
        return {"data": giorno, "pazienti": oggi, "arretrati": arretrati, "contatori": totali}

    def salva(self, paziente: Patient, dati: dict, reperti: list[dict]) -> dict:
        """Anagrafica, accettazione e contenitori sono una sola transazione."""
        now = dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self.conn:
            caso = None
            if dati.get("accession_id") is not None:
                caso = self.conn.execute(
                    "SELECT * FROM cases WHERE accession_id=?", (int(dati["accession_id"]),)
                ).fetchone()
                if caso is None:
                    raise ValueError("accettazione inesistente")
                if (
                    caso["frozen_at"]
                    or caso["cancelled_at"]
                    or self.conn.execute(
                        "SELECT 1 FROM specimens s JOIN containers c ON c.specimen_id=s.id LEFT JOIN provision_attempts a ON a.container_id=c.id WHERE s.case_id=? AND (a.container_id IS NOT NULL OR c.epc IS NOT NULL OR c.state<>'planned') LIMIT 1",
                        (caso["id"],),
                    ).fetchone()
                ):
                    raise ValueError(
                        "lotto già avviato: aggiungere i campioni in una nuova accettazione"
                    )
                if dati.get("edit_version") != caso["edit_version"]:
                    raise ValueError(
                        "accettazione modificata altrove: riaprire il dettaglio prima di salvare"
                    )
                if paziente.id != caso["patient_id"]:
                    raise ValueError("il paziente dell'accettazione non può essere cambiato")
            noto = (
                self.conn.execute("SELECT * FROM patients WHERE id=?", (paziente.id,)).fetchone()
                if paziente.id
                else None
            )
            if paziente.id and noto is None:
                raise ValueError("paziente inesistente")
            if paziente.codice_fiscale:
                per_cf = self.conn.execute(
                    "SELECT * FROM patients WHERE codice_fiscale=?", (paziente.codice_fiscale,)
                ).fetchone()
                if per_cf and (noto is None or per_cf["id"] != noto["id"]):
                    raise ValueError(
                        f"codice fiscale già presente: selezionare il paziente P{per_cf['id']:06d} dai risultati"
                    )
            valori = (
                paziente.codice_fiscale or None,
                paziente.cognome,
                paziente.nome,
                paziente.data_nascita.isoformat() if paziente.data_nascita else None,
                paziente.sesso.value,
            )
            if noto:
                precedenti = tuple(
                    noto[k] for k in ("codice_fiscale", "cognome", "nome", "data_nascita", "sesso")
                )
                if (
                    valori != precedenti
                    and self.conn.execute(
                        "SELECT 1 FROM cases WHERE patient_id=? AND frozen_at IS NOT NULL LIMIT 1",
                        (noto["id"],),
                    ).fetchone()
                ):
                    raise ValueError(
                        "anagrafica già collegata a tag avviati: conservarne i dati originali"
                    )
                self.conn.execute(
                    "UPDATE patients SET codice_fiscale=?, cognome=?, nome=?, data_nascita=?, sesso=? WHERE id=?",
                    (*valori, noto["id"]),
                )
                pid = noto["id"]
            else:
                pid = self.conn.execute(
                    "INSERT INTO patients(codice_fiscale,cognome,nome,data_nascita,sesso,created_at) VALUES(?,?,?,?,?,?)",
                    (*valori, now),
                ).lastrowid
            if caso:
                cid, accession = caso["id"], caso["accession_id"]
                self.conn.execute("DELETE FROM specimens WHERE case_id=?", (cid,))
            else:
                accession = self.db.next_accession_id()
                cid = self.conn.execute(
                    "INSERT INTO cases(accession_id,patient_id,created_at,draft) VALUES(?,?,?,1)",
                    (accession, pid, now),
                ).lastrowid
            totale = sum(r["contenitori"] for r in reperti)
            self.conn.execute(
                "UPDATE cases SET data_prelievo=?,ora_prelievo=?,reparto=?,medico=?,external_ref=?,count_confirmed=?,edit_version=edit_version+1 WHERE id=?",
                (
                    dati.get("data_prelievo"),
                    dati.get("ora_prelievo", ""),
                    dati.get("reparto", ""),
                    dati.get("medico", ""),
                    dati.get("external_ref", ""),
                    int(totale == 1),
                    cid,
                ),
            )
            idx = 1
            for r in reperti:
                sid = self.conn.execute(
                    "INSERT INTO specimens(case_id,descrizione,material_code,fixative_code,site_code,flags,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        cid,
                        r["descrizione"],
                        r["material_code"],
                        r["fixative_code"],
                        r["site_code"],
                        int(r["flags"]),
                        now,
                    ),
                ).lastrowid
                for _ in range(r["contenitori"]):
                    self.conn.execute(
                        "INSERT INTO containers(specimen_id,idx,total,state,created_at) VALUES(?,?,?,'planned',?)",
                        (sid, idx, totale, now),
                    )
                    idx += 1
        return {"patient_id": pid, "accession_id": accession}
