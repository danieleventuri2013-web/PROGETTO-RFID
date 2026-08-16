"""Persistenza SQLite dell'archivio pazienti, reperti, contenitori e spedizioni.

Il database e' l'archivio principale del **laboratorio di origine**: e' qui che i
dati restano dopo che il contenitore e' partito, ed e' qui che si risale a un
campione se il tag viene danneggiato. Il tag e' autosufficiente in transito, non
sostitutivo dell'archivio.

Qui vive anche il **registro di unicita' degli EPC** che
`docs/CONTRATTO_SERVICE.md:196` lascia esplicitamente al livello applicativo:
`RFIDService.generate_epc` garantisce l'unicita' solo all'interno della sessione,
la garanzia definitiva e' il vincolo `UNIQUE` su `containers.epc`.

L'unicita' e' **perpetua**, e deve restarlo: il tag non torna indietro, parte col
contenitore e resta al laboratorio destinatario. Anche l'EPC di un contenitore
annullato resta occupato per sempre — reimmetterlo in circolo sarebbe il modo
perfetto per confondere due campioni a distanza di mesi.

Quello che la versione 2 aggiunge non e' il riuso, ma il **guasto**: un
contenitore che si rompe o un tag che non funziona prima della spedizione. Il
campione passa a un contenitore nuovo con un tag nuovo, e la numerazione «2 di 3»
resta quella (`replace_container`).

Solo `sqlite3` dalla libreria standard: nessuna dipendenza aggiuntiva.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .model import (
    Case,
    Container,
    ContainerState,
    Patient,
    Sex,
    Shipment,
    ShipmentState,
    Specimen,
    TagState,
    validate_codice_fiscale,
)

__all__ = [
    "SCHEMA_VERSION",
    "ContainerRecord",
    "CountAdjustment",
    "DuplicateEpcError",
    "LimsDatabase",
    "LimsDatabaseError",
    "NotFoundError",
]

SCHEMA_VERSION = 3


class LimsDatabaseError(Exception):
    """Errore dell'archivio."""


class DuplicateEpcError(LimsDatabaseError):
    """L'EPC e' gia' assegnato a un altro contenitore.

    E' la rete di sicurezza contro due contenitori indistinguibili in campo.
    """


class NotFoundError(LimsDatabaseError):
    """Il record richiesto non esiste."""


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def _as_iso(value: dt.date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS patients (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    codice_fiscale  TEXT NOT NULL UNIQUE,
    cognome         TEXT NOT NULL,
    nome            TEXT NOT NULL,
    data_nascita    TEXT,
    sesso           TEXT NOT NULL DEFAULT 'X',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    accession_id    INTEGER NOT NULL UNIQUE,
    patient_id      INTEGER NOT NULL REFERENCES patients(id) ON DELETE RESTRICT,
    data_prelievo   TEXT,
    reparto         TEXT NOT NULL DEFAULT '',
    medico          TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_patient ON cases(patient_id);

CREATE TABLE IF NOT EXISTS specimens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    descrizione     TEXT NOT NULL DEFAULT '',
    material_code   INTEGER NOT NULL DEFAULT 0,
    site_code       INTEGER NOT NULL DEFAULT 0,
    fixative_code   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_specimens_case ON specimens(case_id);

-- L'unicita' di `epc` e' il registro globale degli pseudonimi: senza di essa
-- due contenitori potrebbero rispondere con lo stesso EPC nello stesso volume.
CREATE TABLE IF NOT EXISTS containers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id     INTEGER NOT NULL REFERENCES specimens(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    total           INTEGER NOT NULL,
    epc             TEXT UNIQUE,
    tid             TEXT,
    state           TEXT NOT NULL DEFAULT 'planned',
    revision        INTEGER NOT NULL DEFAULT 0,
    provisioned_at  TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (specimen_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_containers_specimen ON containers(specimen_id);
CREATE INDEX IF NOT EXISTS idx_containers_state ON containers(state);

CREATE TABLE IF NOT EXISTS shipments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    destinazione    TEXT NOT NULL,
    data            TEXT,
    state           TEXT NOT NULL DEFAULT 'open',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipment_items (
    shipment_id     INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    container_id    INTEGER NOT NULL REFERENCES containers(id) ON DELETE CASCADE,
    added_at        TEXT NOT NULL,
    PRIMARY KEY (shipment_id, container_id)
);

-- Traccia di ogni operazione RFID. In ambito sanitario la domanda "chi ha
-- scritto questo tag e quando" va potuta risolvere anche a distanza di anni.
CREATE TABLE IF NOT EXISTS tag_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    operation       TEXT NOT NULL,
    epc             TEXT,
    tid             TEXT,
    container_id    INTEGER REFERENCES containers(id) ON DELETE SET NULL,
    antenna         INTEGER,
    rssi            INTEGER,
    ok              INTEGER NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    operator        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tag_events_epc ON tag_events(epc);
CREATE INDEX IF NOT EXISTS idx_tag_events_ts ON tag_events(ts);
"""


# Versione 2: registro dei tag, annullamento e sostituzione dei contenitori.
#
# Il tag **non torna indietro**: parte col contenitore e resta al laboratorio
# destinatario, dove il campione va conservato intatto. Quindi niente riuso, e
# l'unicita' perpetua di `containers.epc` della versione 1 **resta**: un EPC
# identifica un contenitore per sempre, anche se annullato. Reimmetterlo in
# circolo sarebbe il modo perfetto per confondere due campioni a distanza di
# mesi.
#
# Quello che serve davvero e' modellare cosa succede quando qualcosa va storto
# prima della spedizione: il contenitore si rompe, oppure il tag non funziona.
# Il campione passa a un altro contenitore, e la numerazione «2 di 3» deve
# restare quella. Da qui l'indice parziale sullo slot: due contenitori attivi
# non possono occupare la stessa posizione, ma uno annullato la libera.
_MIGRATION_V2 = """
CREATE TABLE IF NOT EXISTS tags (
    tid                 TEXT PRIMARY KEY,
    state               TEXT NOT NULL DEFAULT 'free',
    user_memory_bytes   INTEGER,
    model               TEXT NOT NULL DEFAULT '',
    write_count         INTEGER NOT NULL DEFAULT 0,
    failure_count       INTEGER NOT NULL DEFAULT 0,
    reference_rssi      INTEGER,
    commissioned_at     TEXT NOT NULL,
    last_seen_at        TEXT,
    note                TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tags_state ON tags(state);

-- Storico: risponde a "su quale contenitore e' finito questo chip", domanda che
-- in ambito sanitario prima o poi arriva. `released_at` non e' un ritorno in
-- magazzino: segna l'annullamento prima della spedizione.
CREATE TABLE IF NOT EXISTS tag_assignments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tid             TEXT NOT NULL REFERENCES tags(tid) ON DELETE CASCADE,
    container_id    INTEGER REFERENCES containers(id) ON DELETE SET NULL,
    epc             TEXT NOT NULL UNIQUE,
    revision        INTEGER NOT NULL DEFAULT 0,
    assigned_at     TEXT NOT NULL,
    released_at     TEXT,
    release_reason  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_assignments_tid ON tag_assignments(tid);
CREATE INDEX IF NOT EXISTS idx_assignments_container ON tag_assignments(container_id);

-- SQLite non sa aggiungere colonne con vincoli a una tabella esistente in modo
-- pulito: la si ricostruisce. `epc` resta UNIQUE, come nella versione 1.
CREATE TABLE containers_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id     INTEGER NOT NULL REFERENCES specimens(id) ON DELETE CASCADE,
    idx             INTEGER NOT NULL,
    total           INTEGER NOT NULL,
    epc             TEXT UNIQUE,
    tid             TEXT,
    state           TEXT NOT NULL DEFAULT 'planned',
    revision        INTEGER NOT NULL DEFAULT 0,
    provisioned_at  TEXT,
    voided_at       TEXT,
    voided_reason   TEXT NOT NULL DEFAULT '',
    replaces        INTEGER,
    created_at      TEXT NOT NULL
);
INSERT INTO containers_v2
    (id, specimen_id, idx, total, epc, tid, state, revision, provisioned_at, created_at)
    SELECT id, specimen_id, idx, total, epc, tid, state, revision, provisioned_at, created_at
    FROM containers;
DROP TABLE containers;
ALTER TABLE containers_v2 RENAME TO containers;
CREATE INDEX IF NOT EXISTS idx_containers_specimen ON containers(specimen_id);
CREATE INDEX IF NOT EXISTS idx_containers_state ON containers(state);
CREATE INDEX IF NOT EXISTS idx_containers_tid ON containers(tid);
CREATE INDEX IF NOT EXISTS idx_containers_replaces ON containers(replaces);

-- Due contenitori ATTIVI non possono occupare la stessa posizione «n di N»;
-- uno annullato la libera per il suo sostituto.
CREATE UNIQUE INDEX IF NOT EXISTS idx_containers_active_slot
    ON containers(specimen_id, idx) WHERE state <> 'voided';

-- I contenitori gia' scritti nella v1 entrano nel registro dei tag, cosi'
-- l'archivio esistente non resta fuori dal nuovo modello.
INSERT OR IGNORE INTO tags (tid, state, commissioned_at, write_count)
    SELECT tid, 'assigned', COALESCE(provisioned_at, created_at), 1
    FROM containers WHERE tid IS NOT NULL AND tid <> '';
INSERT OR IGNORE INTO tag_assignments (tid, container_id, epc, revision, assigned_at)
    SELECT tid, id, epc, revision, COALESCE(provisioned_at, created_at)
    FROM containers WHERE tid IS NOT NULL AND tid <> '' AND epc IS NOT NULL;
"""

# Versione 3: la spedizione diventa tracciabile da sola.
#
# Prima si sapeva *cosa* era stato spedito, ma non quando esattamente, da chi, e
# con quale esito del sigillo: quei dati stavano solo in `tag_events`, mescolati
# a tutto il resto e senza legame con la spedizione. Se un'autorita' chiede conto
# di un campione, la risposta deve stare in una riga, non in una ricostruzione.
#
# Scritta come funzione e non come script: `ALTER TABLE ADD COLUMN` non e'
# ripetibile, e se lo script si interrompesse a meta' (disco pieno, processo
# ucciso) l'archivio non si riaprirebbe piu' — la migrazione ritenterebbe una
# colonna gia' aggiunta e fallirebbe per sempre. Aggiungendo solo cio' che
# manca, un secondo tentativo completa invece di bloccare.
_COLONNE_V3 = (
    ("operator", "TEXT NOT NULL DEFAULT ''"),
    ("sealed_at", "TEXT"),
    ("sent_at", "TEXT"),
    ("sealing_ok", "INTEGER"),
    ("sealing_detail", "TEXT NOT NULL DEFAULT ''"),
)


def _migrazione_v3(conn: sqlite3.Connection) -> None:
    presenti = {riga[1] for riga in conn.execute("PRAGMA table_info(shipments)")}
    for nome, tipo in _COLONNE_V3:
        if nome not in presenti:
            conn.execute(f"ALTER TABLE shipments ADD COLUMN {nome} {tipo}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_shipments_sent ON shipments(sent_at)")


#: Una migrazione e' uno script SQL oppure una funzione che riceve la
#: connessione. La seconda forma serve quando il passo non e' ripetibile scritto
#: in SQL puro.
_MIGRATIONS: dict[int, "str | Callable[[sqlite3.Connection], None]"] = {
    1: _SCHEMA,
    2: _MIGRATION_V2,
    3: _migrazione_v3,
}


@dataclass(frozen=True)
class CountAdjustment:
    """Esito di una variazione del numero di contenitori di un reperto."""

    specimen_id: int
    new_total: int
    added: tuple[int, ...] = ()
    removed: tuple[int, ...] = ()
    #: Contenitori gia' scritti che portano ancora il vecchio totale nel chip,
    #: come `(container_id, indice, totale_scritto)`. Non e' un guasto: e' una
    #: discrepanza da mostrare all'operatore, che decide se rifarli.
    stale: tuple[tuple[int, int, int], ...] = ()

    @property
    def has_stale_tags(self) -> bool:
        return bool(self.stale)

    def describe(self) -> dict[str, Any]:
        return {
            "nuovo_totale": self.new_total,
            "aggiunti": len(self.added),
            "rimossi": len(self.removed),
            "tag_con_totale_superato": [
                f"{indice}/{totale} (contenitore {cid})" for cid, indice, totale in self.stale
            ],
        }


@dataclass(frozen=True)
class ContainerRecord:
    """Vista denormalizzata di un contenitore con il suo contesto clinico.

    E' quello che serve alla schermata di ricezione per mostrare una riga
    completa senza ricomporre a mano quattro tabelle.
    """

    container_id: int
    epc: str
    tid: str
    index: int
    total: int
    state: ContainerState
    revision: int
    accession_id: int
    codice_fiscale: str
    cognome: str
    nome: str
    material_code: int
    site_code: int
    fixative_code: int
    descrizione: str
    data_prelievo: dt.date | None

    @property
    def display_name(self) -> str:
        return f"{self.cognome} {self.nome}"

    @property
    def label(self) -> str:
        return f"{self.index}/{self.total}"


class LimsDatabase:
    """Archivio SQLite. Usabile come context manager."""

    def __init__(self, path: str | Path = ":memory:", *, single_thread: bool = True):
        """`single_thread=False` consente l'uso da piu' thread.

        Serve al server web, dove ogni richiesta arriva su un thread diverso.
        Chi lo disattiva si prende l'onere di serializzare gli accessi: SQLite
        non protegge la coerenza di una sequenza di istruzioni, solo quella
        delle singole. `webui.workflow` lo fa con un lock.
        """
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=single_thread)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        try:
            self._migrate()
        except Exception:
            # Senza questa chiusura una migrazione rifiutata lascerebbe il file
            # bloccato: su Windows il database resterebbe inaccessibile fino
            # all'uscita del processo.
            self._conn.close()
            raise

    # -- ciclo di vita -----------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "LimsDatabase":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def _migrate(self) -> None:
        """Porta l'archivio alla versione corrente, un passo per volta.

        Le migrazioni sono cumulative e ordinate: un archivio alla versione 1 ne
        applica una sola per arrivare alla 2. Cosi' un database in esercizio da
        mesi si aggiorna senza perdere nulla, che e' l'unico comportamento
        accettabile per dati sanitari.
        """
        current = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise LimsDatabaseError(
                f"il database e' alla versione {current}, questo programma conosce "
                f"la {SCHEMA_VERSION}: aggiornare il software prima di aprirlo"
            )
        for versione in range(current + 1, SCHEMA_VERSION + 1):
            script = _MIGRATIONS.get(versione)
            if script is None:
                raise LimsDatabaseError(f"manca la migrazione alla versione {versione}")
            # Le migrazioni riscrivono tabelle: i vincoli di chiave esterna vanno
            # sospesi, altrimenti la copia intermedia li violerebbe.
            self._conn.execute("PRAGMA foreign_keys = OFF")
            if callable(script):
                try:
                    script(self._conn)
                    self._conn.execute(f"PRAGMA user_version = {versione}")
                    self._conn.commit()
                finally:
                    self._conn.execute("PRAGMA foreign_keys = ON")
                continue
            try:
                self._conn.executescript(script)
                self._conn.execute(f"PRAGMA user_version = {versione}")
                self._conn.commit()
            finally:
                self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # -- pazienti ----------------------------------------------------------
    def upsert_patient(self, patient: Patient) -> int:
        """Inserisce o aggiorna il paziente identificandolo dal codice fiscale."""
        existing = self.find_patient(patient.codice_fiscale)
        if existing is not None:
            self._conn.execute(
                "UPDATE patients SET cognome=?, nome=?, data_nascita=?, sesso=? "
                "WHERE codice_fiscale=?",
                (
                    patient.cognome,
                    patient.nome,
                    _as_iso(patient.data_nascita),
                    patient.sesso.value,
                    patient.codice_fiscale,
                ),
            )
            self._conn.commit()
            return int(existing.id)
        cursor = self._conn.execute(
            "INSERT INTO patients (codice_fiscale, cognome, nome, data_nascita, sesso, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                patient.codice_fiscale,
                patient.cognome,
                patient.nome,
                _as_iso(patient.data_nascita),
                patient.sesso.value,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def find_patient(self, codice_fiscale: str) -> Patient | None:
        row = self._conn.execute(
            "SELECT * FROM patients WHERE codice_fiscale=?",
            (validate_codice_fiscale(codice_fiscale),),
        ).fetchone()
        return self._row_to_patient(row) if row else None

    def get_patient(self, patient_id: int) -> Patient:
        row = self._conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"paziente {patient_id} inesistente")
        return self._row_to_patient(row)

    @staticmethod
    def _row_to_patient(row: sqlite3.Row) -> Patient:
        return Patient(
            id=row["id"],
            codice_fiscale=row["codice_fiscale"],
            cognome=row["cognome"],
            nome=row["nome"],
            data_nascita=_as_date(row["data_nascita"]),
            sesso=Sex(row["sesso"]),
        )

    # -- accettazioni ------------------------------------------------------
    def next_accession_id(self) -> int:
        row = self._conn.execute("SELECT MAX(accession_id) AS massimo FROM cases").fetchone()
        return int(row["massimo"] or 0) + 1

    def create_case(self, case: Case) -> int:
        if case.patient_id is None:
            raise ValueError("patient_id obbligatorio per creare un'accettazione")
        try:
            cursor = self._conn.execute(
                "INSERT INTO cases (accession_id, patient_id, data_prelievo, reparto, medico, "
                "note, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    case.accession_id,
                    case.patient_id,
                    _as_iso(case.data_prelievo),
                    case.reparto,
                    case.medico,
                    case.note,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LimsDatabaseError(
                f"numero di accettazione {case.accession_id} gia' usato"
            ) from exc
        self._conn.commit()
        return int(cursor.lastrowid)

    def find_case(self, accession_id: int) -> Case | None:
        row = self._conn.execute(
            "SELECT * FROM cases WHERE accession_id=?", (accession_id,)
        ).fetchone()
        if row is None:
            return None
        return Case(
            id=row["id"],
            accession_id=row["accession_id"],
            patient_id=row["patient_id"],
            data_prelievo=_as_date(row["data_prelievo"]),
            reparto=row["reparto"],
            medico=row["medico"],
            note=row["note"],
        )

    # -- reperti e contenitori --------------------------------------------
    def add_specimen(self, specimen: Specimen) -> int:
        if specimen.case_id is None:
            raise ValueError("case_id obbligatorio per registrare un reperto")
        cursor = self._conn.execute(
            "INSERT INTO specimens (case_id, descrizione, material_code, site_code, "
            "fixative_code, created_at) VALUES (?,?,?,?,?,?)",
            (
                specimen.case_id,
                specimen.descrizione,
                specimen.material_code,
                specimen.site_code,
                specimen.fixative_code,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def plan_containers(self, specimen_id: int, total: int) -> list[int]:
        """Crea i `total` contenitori previsti per un reperto, ancora senza tag."""
        if not 1 <= total <= 255:
            raise ValueError("il numero di contenitori deve essere compreso tra 1 e 255")
        created: list[int] = []
        # Tutto o niente: un reperto con meta' dei contenitori previsti sarebbe
        # peggio di un reperto senza contenitori, perche' sembrerebbe completo.
        try:
            with self._conn:
                for index in range(1, total + 1):
                    container = Container(index=index, total=total, specimen_id=specimen_id)
                    cursor = self._conn.execute(
                        "INSERT INTO containers (specimen_id, idx, total, state, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (specimen_id, container.index, container.total,
                         ContainerState.PLANNED.value, _now()),
                    )
                    created.append(int(cursor.lastrowid))
        except sqlite3.IntegrityError as exc:
            raise LimsDatabaseError(
                f"impossibile creare i contenitori del reperto {specimen_id}: {exc}"
            ) from exc
        return created

    def assign_epc(self, container_id: int, epc: str, tid: str = "", revision: int = 0) -> None:
        """Registra lo pseudonimo assegnato a un contenitore.

        Va chiamata **prima** di scrivere il tag: se l'EPC risultasse gia' preso,
        e' meglio scoprirlo con una transazione fallita che con due contenitori
        indistinguibili nel volume di lettura.
        """
        epc = epc.strip().upper()
        try:
            self._conn.execute(
                "UPDATE containers SET epc=?, tid=?, revision=? WHERE id=?",
                (epc, tid.strip().upper(), revision, container_id),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEpcError(f"EPC {epc} gia' assegnato a un altro contenitore") from exc
        self._conn.commit()

    def mark_provisioned(self, container_id: int, tid: str = "", revision: int | None = None) -> None:
        parametri: list[Any] = [ContainerState.PROVISIONED.value, _now()]
        sql = "UPDATE containers SET state=?, provisioned_at=?"
        if tid:
            sql += ", tid=?"
            parametri.append(tid.strip().upper())
        if revision is not None:
            sql += ", revision=?"
            parametri.append(revision)
        sql += " WHERE id=?"
        parametri.append(container_id)
        self._conn.execute(sql, parametri)
        self._conn.commit()

    def set_container_state(self, container_id: int, state: ContainerState) -> None:
        self._conn.execute(
            "UPDATE containers SET state=? WHERE id=?", (ContainerState(state).value, container_id)
        )
        self._conn.commit()

    def epc_exists(self, epc: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM containers WHERE epc=?", (epc.strip().upper(),)
        ).fetchone()
        return row is not None

    _RECORD_QUERY = """
        SELECT c.id AS container_id, c.epc, c.tid, c.idx, c.total, c.state, c.revision,
               s.descrizione, s.material_code, s.site_code, s.fixative_code,
               k.accession_id, k.data_prelievo,
               p.codice_fiscale, p.cognome, p.nome
        FROM containers c
        JOIN specimens s ON s.id = c.specimen_id
        JOIN cases     k ON k.id = s.case_id
        JOIN patients  p ON p.id = k.patient_id
    """

    def find_container_by_epc(self, epc: str) -> ContainerRecord | None:
        row = self._conn.execute(
            self._RECORD_QUERY + " WHERE c.epc=?", (epc.strip().upper(),)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_container(self, container_id: int) -> ContainerRecord:
        row = self._conn.execute(
            self._RECORD_QUERY + " WHERE c.id=?", (container_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"contenitore {container_id} inesistente")
        return self._row_to_record(row)

    def containers_for_accession(self, accession_id: int) -> list[ContainerRecord]:
        rows = self._conn.execute(
            self._RECORD_QUERY + " WHERE k.accession_id=? ORDER BY s.id, c.idx",
            (accession_id,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ContainerRecord:
        return ContainerRecord(
            container_id=row["container_id"],
            epc=row["epc"] or "",
            tid=row["tid"] or "",
            index=row["idx"],
            total=row["total"],
            state=ContainerState(row["state"]),
            revision=row["revision"],
            accession_id=row["accession_id"],
            codice_fiscale=row["codice_fiscale"],
            cognome=row["cognome"],
            nome=row["nome"],
            material_code=row["material_code"],
            site_code=row["site_code"],
            fixative_code=row["fixative_code"],
            descrizione=row["descrizione"],
            data_prelievo=_as_date(row["data_prelievo"]),
        )

    # -- parco tag riutilizzabili ------------------------------------------
    def register_tag(
        self,
        tid: str,
        *,
        user_memory_bytes: int | None = None,
        model: str = "",
        reference_rssi: int | None = None,
        note: str = "",
    ) -> str:
        """Censisce un tag fisico, o ne aggiorna il profilo se gia' noto.

        `reference_rssi` e' la lettura di riferimento alla messa in servizio: e'
        il termine di paragone con cui, mesi dopo, si capisce se un tag si sta
        degradando o se e' solo posizionato male.
        """
        tid = tid.strip().upper()
        if not tid:
            raise ValueError("tid obbligatorio")
        esistente = self.get_tag(tid)
        if esistente is None:
            self._conn.execute(
                "INSERT INTO tags (tid, state, user_memory_bytes, model, reference_rssi, "
                "commissioned_at, note) VALUES (?,?,?,?,?,?,?)",
                (tid, TagState.FREE.value, user_memory_bytes, model, reference_rssi, _now(), note),
            )
        else:
            self._conn.execute(
                "UPDATE tags SET user_memory_bytes=COALESCE(?, user_memory_bytes), "
                "model=CASE WHEN ?<>'' THEN ? ELSE model END, "
                "reference_rssi=COALESCE(?, reference_rssi), "
                "note=CASE WHEN ?<>'' THEN ? ELSE note END WHERE tid=?",
                (user_memory_bytes, model, model, reference_rssi, note, note, tid),
            )
        self._conn.commit()
        return tid

    def get_tag(self, tid: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tags WHERE tid=?", (tid.strip().upper(),)
        ).fetchone()
        return dict(row) if row else None

    def tags_by_state(self, state: "TagState | str") -> list[dict[str, Any]]:
        valore = TagState(state).value if not isinstance(state, str) else TagState(state).value
        rows = self._conn.execute(
            "SELECT * FROM tags WHERE state=? ORDER BY tid", (valore,)
        ).fetchall()
        return [dict(row) for row in rows]

    def set_tag_state(self, tid: str, state: "TagState | str") -> None:
        self._conn.execute(
            "UPDATE tags SET state=? WHERE tid=?",
            (TagState(state).value, tid.strip().upper()),
        )
        self._conn.commit()

    def assign_tag(
        self,
        tid: str,
        container_id: int,
        epc: str,
        revision: int = 0,
        *,
        allow_rewrite: bool = False,
    ) -> int:
        """Apre un'assegnazione tag -> contenitore.

        Fallisce se l'EPC e' gia' stato usato, **anche da un contenitore
        annullato**: e' il registro di unicita' che impedisce due contenitori
        indistinguibili nello stesso volume, oggi e fra due anni.
        """
        tid = tid.strip().upper()
        epc = epc.strip().upper()
        if self.get_tag(tid) is None:
            self.register_tag(tid)
        stato = (self.get_tag(tid) or {}).get("state")
        # Un tag gia' scritto non si riscrive: e' partito, o e' stato scartato,
        # o e' difettoso. In tutti e tre i casi rimetterlo in gioco significa
        # sovrascrivere dati di un altro paziente. `allow_rewrite` e' la deroga
        # per assistenza e collaudo, e chi la usa se ne assume la responsabilita'
        # a monte: qui si applica soltanto.
        if stato != TagState.FREE.value and not allow_rewrite:
            raise LimsDatabaseError(
                f"il tag {tid} e' in stato '{stato}' e non puo' essere assegnato"
            )
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO tag_assignments (tid, container_id, epc, revision, assigned_at) "
                    "VALUES (?,?,?,?,?)",
                    (tid, container_id, epc, revision, _now()),
                )
                self._conn.execute(
                    "UPDATE tags SET state=?, write_count=write_count+1, last_seen_at=? "
                    "WHERE tid=?",
                    (TagState.ASSIGNED.value, _now(), tid),
                )
                self._conn.execute(
                    "UPDATE containers SET epc=?, tid=?, revision=? WHERE id=?",
                    (epc, tid, revision, container_id),
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateEpcError(
                f"EPC {epc} gia' assegnato a un contenitore in circolazione"
            ) from exc
        return int(cursor.lastrowid)

    def mark_tag_shipped(self, tid: str) -> None:
        """Il tag e' partito col contenitore: non tornera' e non va piu' toccato."""
        self._conn.execute(
            "UPDATE tags SET state=?, last_seen_at=? WHERE tid=?",
            (TagState.SHIPPED.value, _now(), tid.strip().upper()),
        )
        self._conn.commit()

    def void_container(
        self,
        container_id: int,
        *,
        reason: str,
        tag_faulty: bool = False,
    ) -> None:
        """Annulla un contenitore prima della spedizione.

        I due casi reali: il contenitore si e' rotto, oppure il tag non ha
        funzionato. In entrambi il campione passera' a un contenitore nuovo con
        un tag nuovo — non si riscrive niente.

        L'EPC annullato **non torna disponibile**: resta occupato per sempre.
        Reimmetterlo in circolo sarebbe il modo perfetto per confondere due
        campioni a distanza di mesi.

        Con `tag_faulty` il chip finisce in quarantena invece che fra gli
        annullati: la distinzione serve a non rimettere in circolo un tag che
        ha gia' dato problemi.
        """
        if not reason.strip():
            raise ValueError("annullare un contenitore richiede una motivazione")
        record = self._conn.execute(
            "SELECT tid FROM containers WHERE id=?", (container_id,)
        ).fetchone()
        if record is None:
            raise NotFoundError(f"contenitore {container_id} inesistente")
        tid = (record["tid"] or "").strip().upper()

        with self._conn:
            self._conn.execute(
                "UPDATE containers SET state=?, voided_at=?, voided_reason=? WHERE id=?",
                (ContainerState.VOIDED.value, _now(), reason, container_id),
            )
            if tid:
                self._conn.execute(
                    "UPDATE tag_assignments SET released_at=?, release_reason=? "
                    "WHERE tid=? AND container_id=? AND released_at IS NULL",
                    (_now(), reason, tid, container_id),
                )
                self._conn.execute(
                    "UPDATE tags SET state=?, last_seen_at=?, note=? WHERE tid=?",
                    (
                        TagState.QUARANTINE.value if tag_faulty else TagState.VOIDED.value,
                        _now(),
                        reason,
                        tid,
                    ),
                )

    def replace_container(self, container_id: int, *, reason: str) -> int:
        """Annulla un contenitore e ne crea il sostituto nella stessa posizione.

        La numerazione resta quella: se si rompe il 2 di 3, il sostituto e'
        ancora il 2 di 3. Cambiare la numerazione costringerebbe a riscrivere
        anche gli altri contenitori, che sono gia' partiti o gia' sigillati.
        """
        riga = self._conn.execute(
            "SELECT specimen_id, idx, total FROM containers WHERE id=?", (container_id,)
        ).fetchone()
        if riga is None:
            raise NotFoundError(f"contenitore {container_id} inesistente")

        self.void_container(container_id, reason=reason)
        cursor = self._conn.execute(
            "INSERT INTO containers (specimen_id, idx, total, state, replaces, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                riga["specimen_id"],
                riga["idx"],
                riga["total"],
                ContainerState.PLANNED.value,
                container_id,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def adjust_container_count(self, specimen_id: int, new_total: int) -> "CountAdjustment":
        """Cambia in corso d'opera quanti contenitori servono per un reperto.

        Capita spesso: si registra il paziente con tre campioni e contandoli
        fisicamente sono quattro, o due.

        Il punto delicato e' che il totale **e' scritto dentro l'EPC e dentro il
        payload** dei tag gia' fatti, e quei tag non si riscrivono. Quindi:

        * i contenitori ancora da scrivere si aggiungono o si tolgono liberamente,
          e il loro totale viene aggiornato;
        * quelli gia' scritti restano con il vecchio totale e vengono elencati in
          `stale`. Non e' un errore da nascondere: l'operatore decide se lasciarli
          — la distinta in archivio resta l'autorita' per il sigillo e per la
          spedizione — oppure rifarli con `replace_container`.

        Ridurre il totale sotto il numero di contenitori gia' scritti viene
        rifiutato: quei campioni esistono, e vanno annullati uno per uno con una
        motivazione, non fatti sparire da un conteggio.
        """
        if not 1 <= new_total <= 255:
            raise ValueError("il numero di contenitori deve essere compreso tra 1 e 255")

        attivi = [
            dict(row)
            for row in self._conn.execute(
                "SELECT id, idx, total, epc, state FROM containers "
                "WHERE specimen_id=? AND state<>? ORDER BY idx",
                (specimen_id, ContainerState.VOIDED.value),
            ).fetchall()
        ]
        if not attivi:
            raise NotFoundError(f"il reperto {specimen_id} non ha contenitori attivi")

        scritti = [c for c in attivi if c["epc"]]
        bloccati = [c for c in scritti if c["idx"] > new_total]
        if bloccati:
            etichette = ", ".join(f"{c['idx']}/{c['total']}" for c in bloccati)
            raise LimsDatabaseError(
                f"non si puo' scendere a {new_total}: i contenitori {etichette} sono "
                "gia' stati scritti. Vanno annullati singolarmente con una motivazione."
            )

        da_rimuovere = [c["id"] for c in attivi if c["idx"] > new_total]
        presenti = {c["idx"] for c in attivi if c["idx"] <= new_total}
        da_aggiungere = [i for i in range(1, new_total + 1) if i not in presenti]

        with self._conn:
            if da_rimuovere:
                self._conn.executemany(
                    "DELETE FROM containers WHERE id=?", [(i,) for i in da_rimuovere]
                )
            aggiunti: list[int] = []
            for indice in da_aggiungere:
                cursor = self._conn.execute(
                    "INSERT INTO containers (specimen_id, idx, total, state, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (specimen_id, indice, new_total, ContainerState.PLANNED.value, _now()),
                )
                aggiunti.append(int(cursor.lastrowid))
            # Il nuovo totale si applica solo a chi non ha ancora un EPC: gli
            # altri lo portano gia' scritto nel chip.
            self._conn.execute(
                "UPDATE containers SET total=? WHERE specimen_id=? AND state<>? "
                "AND (epc IS NULL OR epc='')",
                (new_total, specimen_id, ContainerState.VOIDED.value),
            )

        return CountAdjustment(
            specimen_id=specimen_id,
            new_total=new_total,
            added=tuple(aggiunti),
            removed=tuple(da_rimuovere),
            stale=tuple(
                (c["id"], c["idx"], c["total"]) for c in scritti if c["total"] != new_total
            ),
        )

    def active_containers_for_accession(self, accession_id: int) -> list[ContainerRecord]:
        """Contenitori non annullati: e' cio' che deve stare nella scatola."""
        rows = self._conn.execute(
            self._RECORD_QUERY + " WHERE k.accession_id=? AND c.state <> ? ORDER BY s.id, c.idx",
            (accession_id, ContainerState.VOIDED.value),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def active_assignment(self, tid: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM tag_assignments WHERE tid=? AND released_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (tid.strip().upper(),),
        ).fetchone()
        return dict(row) if row else None

    def assignment_history(self, tid: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tag_assignments WHERE tid=? ORDER BY id", (tid.strip().upper(),)
        ).fetchall()
        return [dict(row) for row in rows]

    def record_tag_failure(self, tid: str, *, quarantine_after: int = 3) -> dict[str, Any]:
        """Registra un guasto del tag e lo mette in quarantena se insiste.

        Non esiste un comando Gen2 che dichiari la salute di un tag: si deduce
        dal comportamento. Un tag che fallisce ripetutamente scrittura o verifica
        va tolto dalla circolazione prima che faccia perdere un campione, non
        dopo.
        """
        tid = tid.strip().upper()
        if self.get_tag(tid) is None:
            self.register_tag(tid)
        with self._conn:
            self._conn.execute(
                "UPDATE tags SET failure_count=failure_count+1, last_seen_at=? WHERE tid=?",
                (_now(), tid),
            )
            riga = self._conn.execute(
                "SELECT failure_count FROM tags WHERE tid=?", (tid,)
            ).fetchone()
            if riga and riga["failure_count"] >= quarantine_after:
                self._conn.execute(
                    "UPDATE tags SET state=? WHERE tid=?", (TagState.QUARANTINE.value, tid)
                )
        return self.get_tag(tid) or {}

    def clear_tag_failures(self, tid: str) -> None:
        """Azzera i guasti dopo un'operazione riuscita, se il tag non e' fermo."""
        self._conn.execute(
            "UPDATE tags SET failure_count=0 WHERE tid=? AND state NOT IN (?,?)",
            (tid.strip().upper(), TagState.QUARANTINE.value, TagState.RETIRED.value),
        )
        self._conn.commit()

    def find_container_by_tid(self, tid: str) -> ContainerRecord | None:
        """Contenitore a cui il chip e' assegnato **adesso**.

        Il passaggio dall'assegnazione attiva non e' un giro inutile: lo stesso
        TID compare su tutti i contenitori che quel tag ha servito nel tempo,
        perche' il campo resta li' per l'audit. Cercare direttamente in
        `containers` restituirebbe il primo della storia, cioe' il paziente
        sbagliato.
        """
        row = self._conn.execute(
            self._RECORD_QUERY
            + " JOIN tag_assignments a ON a.container_id = c.id"
            + " WHERE a.tid=? AND a.released_at IS NULL"
            + " ORDER BY a.id DESC LIMIT 1",
            (tid.strip().upper(),),
        ).fetchone()
        return self._row_to_record(row) if row else None

    # -- spedizioni --------------------------------------------------------
    def create_shipment(self, shipment: Shipment) -> int:
        cursor = self._conn.execute(
            "INSERT INTO shipments (destinazione, data, state, note, created_at) VALUES (?,?,?,?,?)",
            (
                shipment.destinazione,
                _as_iso(shipment.data),
                shipment.state.value,
                shipment.note,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def add_to_shipment(self, shipment_id: int, container_ids: Iterable[int]) -> int:
        righe = [(shipment_id, container_id, _now()) for container_id in container_ids]
        self._conn.executemany(
            "INSERT OR IGNORE INTO shipment_items (shipment_id, container_id, added_at) "
            "VALUES (?,?,?)",
            righe,
        )
        self._conn.executemany(
            "UPDATE containers SET state=? WHERE id=?",
            [(ContainerState.SHIPPED.value, container_id) for _, container_id, _ in righe],
        )
        self._conn.commit()
        return len(righe)

    def shipment_contents(self, shipment_id: int) -> list[ContainerRecord]:
        rows = self._conn.execute(
            self._RECORD_QUERY
            + " JOIN shipment_items i ON i.container_id = c.id"
            + " WHERE i.shipment_id=? ORDER BY k.accession_id, c.idx",
            (shipment_id,),
        ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def set_shipment_state(self, shipment_id: int, state: ShipmentState) -> None:
        """Cambia lo stato e marca l'istante in cui e' avvenuto.

        Sigillo e partenza sono i due momenti che, mesi dopo, qualcuno chiedera'
        di datare: si registrano quando accadono, non si deducono.
        """
        valore = ShipmentState(state).value
        colonna = {
            ShipmentState.SEALED.value: "sealed_at",
            ShipmentState.SENT.value: "sent_at",
        }.get(valore)
        if colonna:
            self._conn.execute(
                f"UPDATE shipments SET state=?, {colonna}=? WHERE id=?",
                (valore, _now(), shipment_id),
            )
        else:
            self._conn.execute("UPDATE shipments SET state=? WHERE id=?", (valore, shipment_id))
        self._conn.commit()

    def record_shipment_sealing(
        self, shipment_id: int, *, ok: bool, detail: str = "", operator: str = ""
    ) -> None:
        """Attacca alla spedizione l'esito del sigillo e chi l'ha supervisionata."""
        self._conn.execute(
            "UPDATE shipments SET sealing_ok=?, sealing_detail=?, operator=? WHERE id=?",
            (1 if ok else 0, str(detail), str(operator), shipment_id),
        )
        self._conn.commit()

    # -- archivio: ricerca e storico ---------------------------------------
    def search_patients(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Cerca per cognome, nome, codice fiscale o numero di accettazione.

        Una ricerca sola su tutti i campi: chi ha in mano un foglio non sa in
        quale colonna cercare, sa solo cosa c'e' scritto sopra.
        """
        testo = str(query).strip()
        if not testo:
            return []
        come = f"%{testo.upper()}%"
        accettazione = int(testo) if testo.isdigit() else -1
        righe = self._conn.execute(
            """
            SELECT p.id, p.codice_fiscale, p.cognome, p.nome, p.data_nascita, p.sesso,
                   COUNT(DISTINCT c.id)  AS accettazioni,
                   MAX(c.created_at)     AS ultima
              FROM patients p
              LEFT JOIN cases c ON c.patient_id = p.id
             WHERE UPPER(p.cognome) LIKE ?
                OR UPPER(p.nome) LIKE ?
                OR UPPER(p.codice_fiscale) LIKE ?
                OR c.accession_id = ?
             GROUP BY p.id
             ORDER BY ultima DESC NULLS LAST, p.cognome
             LIMIT ?
            """,
            (come, come, come, accettazione, int(limit)),
        ).fetchall()
        return [dict(riga) for riga in righe]

    def patient_history(self, patient_id: int) -> dict[str, Any]:
        """Tutto lo storico di un paziente, accettazione per accettazione.

        Risponde alla domanda che arriva da fuori — «cosa e' stato mandato, dove,
        quando, chi ha supervisionato, ed e' andato a buon fine?» — senza dover
        ricostruire niente a mano.
        """
        paziente = self._conn.execute(
            "SELECT * FROM patients WHERE id=?", (int(patient_id),)
        ).fetchone()
        if paziente is None:
            raise NotFoundError(f"paziente {patient_id} inesistente")

        accettazioni = []
        for caso in self._conn.execute(
            "SELECT * FROM cases WHERE patient_id=? ORDER BY created_at DESC",
            (int(patient_id),),
        ).fetchall():
            contenitori = self._conn.execute(
                """
                SELECT ct.id, ct.idx, ct.total, ct.epc, ct.tid, ct.state,
                       ct.provisioned_at, s.descrizione, s.material_code,
                       sh.id            AS shipment_id,
                       sh.destinazione, sh.sent_at, sh.sealed_at,
                       sh.operator      AS supervisore,
                       sh.sealing_ok, sh.sealing_detail, sh.state AS shipment_state
                  FROM containers ct
                  JOIN specimens s        ON s.id = ct.specimen_id
                  LEFT JOIN shipment_items si ON si.container_id = ct.id
                  LEFT JOIN shipments sh  ON sh.id = si.shipment_id
                 WHERE s.case_id = ?
                 ORDER BY ct.idx
                """,
                (caso["id"],),
            ).fetchall()
            accettazioni.append(
                {
                    "accession_id": caso["accession_id"],
                    "data_prelievo": caso["data_prelievo"],
                    "reparto": caso["reparto"],
                    "medico": caso["medico"],
                    "note": caso["note"],
                    "creata": caso["created_at"],
                    "contenitori": [dict(riga) for riga in contenitori],
                }
            )
        return {"paziente": dict(paziente), "accettazioni": accettazioni}

    def container_trace(self, epc: str) -> list[dict[str, Any]]:
        """Ogni operazione registrata su un EPC, dalla prima all'ultima."""
        righe = self._conn.execute(
            "SELECT * FROM tag_events WHERE epc=? ORDER BY id", (str(epc).strip().upper(),)
        ).fetchall()
        return [dict(riga) for riga in righe]

    # -- traccia delle operazioni -----------------------------------------
    def log_event(
        self,
        operation: str,
        *,
        ok: bool,
        epc: str = "",
        tid: str = "",
        container_id: int | None = None,
        antenna: int | None = None,
        rssi: int | None = None,
        detail: str = "",
        operator: str = "",
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO tag_events (ts, operation, epc, tid, container_id, antenna, rssi, "
            "ok, detail, operator) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                _now(),
                operation,
                epc.strip().upper() or None,
                tid.strip().upper() or None,
                container_id,
                antenna,
                rssi,
                1 if ok else 0,
                detail,
                operator,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def events_for_epc(self, epc: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tag_events WHERE epc=? ORDER BY id", (epc.strip().upper(),)
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_events(self, limit: int = 50) -> Iterator[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM tag_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return (dict(row) for row in rows)
