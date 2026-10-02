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
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

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

SCHEMA_VERSION = 10


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


def _migrazione_v4(conn: sqlite3.Connection) -> None:
    """Rende persistenti i workflow e separa preparazione, export e partenza."""
    colonne_reperti = {riga[1] for riga in conn.execute("PRAGMA table_info(specimens)")}
    if "flags" not in colonne_reperti:
        conn.execute("ALTER TABLE specimens ADD COLUMN flags INTEGER NOT NULL DEFAULT 0")

    colonne_spedizioni = {
        riga[1] for riga in conn.execute("PRAGMA table_info(shipments)")
    }
    for nome, tipo in (
        ("exported_at", "TEXT"),
        ("manifest_hash", "TEXT NOT NULL DEFAULT ''"),
        ("sealing_json", "TEXT NOT NULL DEFAULT ''"),
    ):
        if nome not in colonne_spedizioni:
            conn.execute(f"ALTER TABLE shipments ADD COLUMN {nome} {tipo}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflow_context (
            id                  INTEGER PRIMARY KEY CHECK (id = 1),
            active_specimen_id  INTEGER REFERENCES specimens(id) ON DELETE SET NULL,
            active_shipment_id  INTEGER REFERENCES shipments(id) ON DELETE SET NULL,
            active_inbound_id   INTEGER,
            count_confirmed     INTEGER NOT NULL DEFAULT 0,
            updated_at          TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS inbound_shipments (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            origin_lab_id       INTEGER NOT NULL,
            origin_shipment_id  INTEGER NOT NULL,
            manifest_hash       TEXT NOT NULL UNIQUE,
            encrypted_blob      BLOB NOT NULL,
            destination         TEXT NOT NULL DEFAULT '',
            source_created_at   TEXT NOT NULL DEFAULT '',
            imported_at         TEXT NOT NULL,
            imported_by         TEXT NOT NULL DEFAULT '',
            state               TEXT NOT NULL DEFAULT 'open',
            confirmed_at        TEXT,
            confirmed_by        TEXT NOT NULL DEFAULT '',
            nonconformity_reason TEXT NOT NULL DEFAULT '',
            UNIQUE (origin_lab_id, origin_shipment_id)
        );

        CREATE TABLE IF NOT EXISTS inbound_reconciliations (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            inbound_id      INTEGER NOT NULL REFERENCES inbound_shipments(id) ON DELETE CASCADE,
            ts              TEXT NOT NULL,
            operator        TEXT NOT NULL DEFAULT '',
            ok              INTEGER NOT NULL,
            expected_json   TEXT NOT NULL,
            arrived_json    TEXT NOT NULL,
            missing_json    TEXT NOT NULL,
            unexpected_json TEXT NOT NULL,
            detail          TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_inbound_state ON inbound_shipments(state);
        CREATE INDEX IF NOT EXISTS idx_reconciliations_inbound
            ON inbound_reconciliations(inbound_id, ts);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO workflow_context (id, updated_at) VALUES (1, ?)",
        (_now(),),
    )


def _migrazione_v5(conn: sqlite3.Connection) -> None:
    """Archivia la distinta immutabile e le prove di consegna PEC."""
    colonne_casi = {riga[1] for riga in conn.execute("PRAGMA table_info(cases)")}
    if "external_ref" not in colonne_casi:
        conn.execute("ALTER TABLE cases ADD COLUMN external_ref TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            UPDATE cases
               SET external_ref=SUBSTR(note, 15)
             WHERE note LIKE 'rif. esterno: %' AND external_ref=''
            """
        )

    colonne_spedizioni = {
        riga[1] for riga in conn.execute("PRAGMA table_info(shipments)")
    }
    for nome, tipo in (
        ("departure_override_at", "TEXT"),
        ("departure_override_by", "TEXT NOT NULL DEFAULT ''"),
        ("departure_override_reason", "TEXT NOT NULL DEFAULT ''"),
    ):
        if nome not in colonne_spedizioni:
            conn.execute(f"ALTER TABLE shipments ADD COLUMN {nome} {tipo}")

    colonne_ingresso = {
        riga[1] for riga in conn.execute("PRAGMA table_info(inbound_shipments)")
    }
    for nome, tipo in (
        ("manifest_uuid", "TEXT NOT NULL DEFAULT ''"),
        ("verification_ok", "INTEGER"),
        ("signer_code", "TEXT NOT NULL DEFAULT ''"),
        ("signer_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("recipient_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("verification_detail", "TEXT NOT NULL DEFAULT ''"),
    ):
        if nome not in colonne_ingresso:
            conn.execute(f"ALTER TABLE inbound_shipments ADD COLUMN {nome} {tipo}")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS outbound_manifests (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id             INTEGER NOT NULL UNIQUE
                                      REFERENCES shipments(id) ON DELETE RESTRICT,
            manifest_uuid           TEXT NOT NULL UNIQUE,
            encrypted_blob          BLOB NOT NULL,
            manifest_hash           TEXT NOT NULL UNIQUE,
            filename                TEXT NOT NULL,
            item_count              INTEGER NOT NULL,
            source_code             TEXT NOT NULL DEFAULT '',
            destination_code        TEXT NOT NULL DEFAULT '',
            signer_fingerprint      TEXT NOT NULL DEFAULT '',
            recipient_fingerprint   TEXT NOT NULL DEFAULT '',
            archive_path            TEXT NOT NULL DEFAULT '',
            state                   TEXT NOT NULL DEFAULT 'archived',
            created_at              TEXT NOT NULL,
            created_by              TEXT NOT NULL DEFAULT '',
            message_id              TEXT NOT NULL DEFAULT '',
            sent_eml                BLOB,
            smtp_accepted_at        TEXT,
            pec_accepted_at         TEXT,
            delivered_at            TEXT,
            attempts                INTEGER NOT NULL DEFAULT 0,
            last_error              TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_outbound_state
            ON outbound_manifests(state);
        CREATE INDEX IF NOT EXISTS idx_outbound_message
            ON outbound_manifests(message_id);

        CREATE TABLE IF NOT EXISTS pec_receipts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            outbound_id     INTEGER NOT NULL
                              REFERENCES outbound_manifests(id) ON DELETE CASCADE,
            receipt_type    TEXT NOT NULL,
            message_id      TEXT NOT NULL DEFAULT '',
            received_at     TEXT NOT NULL,
            raw_eml         BLOB NOT NULL,
            daticert_xml    BLOB,
            receipt_hash    TEXT NOT NULL,
            UNIQUE (outbound_id, receipt_type, receipt_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_pec_receipts_outbound
            ON pec_receipts(outbound_id, received_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_manifest_uuid
            ON inbound_shipments(manifest_uuid) WHERE manifest_uuid <> '';
        """
    )
    colonne_uscita = {
        riga[1] for riga in conn.execute("PRAGMA table_info(outbound_manifests)")
    }
    if "sent_eml" not in colonne_uscita:
        conn.execute("ALTER TABLE outbound_manifests ADD COLUMN sent_eml BLOB")


#: Una migrazione e' uno script SQL oppure una funzione che riceve la
#: connessione. La seconda forma serve quando il passo non e' ripetibile scritto
#: in SQL puro.

def _migrazione_v6(conn: sqlite3.Connection) -> None:
    """L'ora del prelievo, e il riscontro che chiude il giro.

    Due aggiunte che vengono dal flusso di lavoro vero:

    * l'etichetta del reparto porta **data e ora** del prelievo, e finora si
      trascriveva solo la data. L'ora serve sulla distinta stampata, che e'
      l'unico documento che il laboratorio destinatario riceve;
    * `shipment_arrivals` registra il **verbale di riscontro** che il
      destinatario rimanda. Senza, il mittente non sa mai come e' andata: sa
      solo di aver spedito. E' una tabella nuova invece di colonne aggiunte
      perche' un arrivo e' un fatto a se', con la sua data e il suo firmatario.
    """
    colonne_casi = {riga[1] for riga in conn.execute("PRAGMA table_info(cases)")}
    if "ora_prelievo" not in colonne_casi:
        conn.execute("ALTER TABLE cases ADD COLUMN ora_prelievo TEXT NOT NULL DEFAULT ''")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS shipment_arrivals (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            shipment_id         INTEGER NOT NULL UNIQUE
                                  REFERENCES shipments(id) ON DELETE RESTRICT,
            -- Il numero che la spedizione ha nell'archivio del destinatario:
            -- non coincide col nostro, e serve per parlarsi al telefono.
            remote_inbound_id   INTEGER,
            manifest_uuid       TEXT NOT NULL DEFAULT '',
            arrived_at          TEXT,
            confirmed_at        TEXT NOT NULL,
            operator            TEXT NOT NULL DEFAULT '',
            expected            INTEGER NOT NULL DEFAULT 0,
            arrived             INTEGER NOT NULL DEFAULT 0,
            missing             INTEGER NOT NULL DEFAULT 0,
            unexpected          INTEGER NOT NULL DEFAULT 0,
            ok                  INTEGER NOT NULL DEFAULT 0,
            nonconformity       TEXT NOT NULL DEFAULT '',
            detail_json         TEXT NOT NULL DEFAULT '',
            signer_fingerprint  TEXT NOT NULL DEFAULT '',
            imported_at         TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arrivals_shipment
            ON shipment_arrivals(shipment_id);
        """
    )


_MIGRATIONS: dict[int, "str | Callable[[sqlite3.Connection], None]"] = {
    10: """
        BEGIN IMMEDIATE;
        CREATE TABLE visual_checks (
            shipment_id INTEGER PRIMARY KEY REFERENCES shipments(id),
            content_hash TEXT NOT NULL,
            encrypted_blob BLOB NOT NULL
        );
        CREATE TABLE visual_settings (id INTEGER PRIMARY KEY CHECK(id=1), encrypted_blob BLOB NOT NULL);
        COMMIT;
    """,
    9: """
        BEGIN IMMEDIATE;
        ALTER TABLE inbound_shipments ADD COLUMN box_epc TEXT;
        ALTER TABLE inbound_shipments ADD COLUMN item_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE inbound_shipments ADD COLUMN scan_valid INTEGER NOT NULL DEFAULT 0;
        UPDATE inbound_shipments SET scan_valid=1 WHERE id IN
            (SELECT inbound_id FROM inbound_reconciliations);
        CREATE INDEX idx_inbound_box ON inbound_shipments(box_epc, state);
        CREATE TABLE mail_dispatches (
            id TEXT PRIMARY KEY,
            shipment_ids TEXT NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            raw_eml BLOB NOT NULL,
            message_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'prepared',
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT ''
        );
    """,
    1: _SCHEMA,
    2: _MIGRATION_V2,
    3: _migrazione_v3,
    4: _migrazione_v4,
    5: _migrazione_v5,
    6: _migrazione_v6,
    7: """
        CREATE TABLE IF NOT EXISTS provision_attempts (
            container_id INTEGER PRIMARY KEY REFERENCES containers(id),
            previous_epc TEXT NOT NULL, epc TEXT NOT NULL UNIQUE,
            tid TEXT NOT NULL, fingerprint TEXT NOT NULL,
            phase TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """,
    8: """
        BEGIN IMMEDIATE;
        CREATE TABLE patients_v8 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            codice_fiscale TEXT UNIQUE,
            cognome TEXT NOT NULL, nome TEXT NOT NULL,
            data_nascita TEXT, sesso TEXT NOT NULL DEFAULT 'X',
            created_at TEXT NOT NULL
        );
        INSERT INTO patients_v8 SELECT id, NULLIF(codice_fiscale, ''), cognome,
            nome, data_nascita, sesso, created_at FROM patients;
        UPDATE sqlite_sequence SET seq=MAX(seq, COALESCE(
            (SELECT seq FROM sqlite_sequence WHERE name='patients'), 0))
            WHERE name='patients_v8';
        DROP TABLE patients;
        ALTER TABLE patients_v8 RENAME TO patients;
        ALTER TABLE cases ADD COLUMN draft INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE cases ADD COLUMN count_confirmed INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE cases ADD COLUMN frozen_at TEXT;
        ALTER TABLE cases ADD COLUMN cancelled_at TEXT;
        ALTER TABLE cases ADD COLUMN edit_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE containers ADD COLUMN last_write_error TEXT NOT NULL DEFAULT '';
        UPDATE cases SET frozen_at=created_at WHERE EXISTS (
            SELECT 1 FROM specimens s JOIN containers c ON c.specimen_id=s.id
            WHERE s.case_id=cases.id AND (c.provisioned_at IS NOT NULL OR
                EXISTS (SELECT 1 FROM provision_attempts a WHERE a.container_id=c.id))
        );
        UPDATE cases SET count_confirmed=1 WHERE frozen_at IS NOT NULL;
        UPDATE cases SET count_confirmed=(SELECT count_confirmed FROM workflow_context WHERE id=1)
            WHERE id=(SELECT s.case_id FROM specimens s JOIN workflow_context w ON w.active_specimen_id=s.id WHERE w.id=1);
        CREATE INDEX idx_cases_created ON cases(created_at);
        PRAGMA user_version=8;
        COMMIT;
    """,
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
    flags: int = 0
    external_ref: str = ""
    #: Servono alla distinta stampata, che e' l'unico documento che il
    #: laboratorio destinatario riceve: li' un paziente va identificato per
    #: intero, non per pseudonimo.
    ora_prelievo: str = ""
    data_nascita: dt.date | None = None
    sesso: str = ""

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
            except BaseException:
                # La migrazione 8 è transazionale: un errore non deve lasciare
                # né una tabella intermedia né i vincoli sospesi sulla connessione.
                self._conn.rollback()
                raise
            finally:
                self._conn.execute("PRAGMA foreign_keys = ON")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # -- pazienti ----------------------------------------------------------
    def upsert_patient(self, patient: Patient) -> int:
        """Identifica dal suo ID o CF; senza entrambi crea una nuova anagrafica."""
        existing = self.get_patient(patient.id) if patient.id is not None else self.find_patient(patient.codice_fiscale)
        if existing is not None:
            campi = ("codice_fiscale", "cognome", "nome", "data_nascita", "sesso")
            if any(getattr(existing, k) != getattr(patient, k) for k in campi) and self._conn.execute(
                "SELECT 1 FROM cases WHERE patient_id=? AND frozen_at IS NOT NULL LIMIT 1",
                (existing.id,),
            ).fetchone():
                raise LimsDatabaseError("anagrafica già collegata a tag avviati: conservarne i dati originali")
            self._conn.execute(
                "UPDATE patients SET cognome=?, nome=?, data_nascita=?, sesso=?, codice_fiscale=? "
                "WHERE id=?",
                (
                    patient.cognome,
                    patient.nome,
                    _as_iso(patient.data_nascita),
                    patient.sesso.value,
                    patient.codice_fiscale or None,
                    existing.id,
                ),
            )
            self._conn.commit()
            return int(existing.id)
        cursor = self._conn.execute(
            "INSERT INTO patients (codice_fiscale, cognome, nome, data_nascita, sesso, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                patient.codice_fiscale or None,
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
        if not codice_fiscale:
            return None
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
            codice_fiscale=row["codice_fiscale"] or "",
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
                "INSERT INTO cases (accession_id, patient_id, data_prelievo, ora_prelievo, "
                "reparto, medico, external_ref, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    case.accession_id,
                    case.patient_id,
                    _as_iso(case.data_prelievo),
                    case.ora_prelievo,
                    case.reparto,
                    case.medico,
                    case.external_ref,
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
            ora_prelievo=row["ora_prelievo"] or "",
            reparto=row["reparto"],
            medico=row["medico"],
            external_ref=row["external_ref"],
            note=row["note"],
        )

    # -- reperti e contenitori --------------------------------------------
    def add_specimen(self, specimen: Specimen) -> int:
        if specimen.case_id is None:
            raise ValueError("case_id obbligatorio per registrare un reperto")
        cursor = self._conn.execute(
            "INSERT INTO specimens (case_id, descrizione, material_code, site_code, "
            "fixative_code, flags, created_at) VALUES (?,?,?,?,?,?,?)",
            (
                specimen.case_id,
                specimen.descrizione,
                specimen.material_code,
                specimen.site_code,
                specimen.fixative_code,
                specimen.flags,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def plan_containers(
        self,
        specimen_id: int,
        total: int,
        *,
        start_index: int = 1,
        accession_total: int | None = None,
    ) -> list[int]:
        """Crea i contenitori previsti per un reperto, ancora senza tag.

        `start_index` e `accession_total` servono quando l'accettazione ha piu'
        reperti: la numerazione che finisce nell'EPC e nel payload e' quella
        **dell'accettazione**, non quella del singolo reperto. Un vassoio con
        due vasetti di colon e uno di linfonodo fa 1/3, 2/3 e 3/3 — non
        1/2, 2/2 e 1/1, che sarebbero tre etichette in cui il paziente si
        perde. Senza i due parametri il comportamento e' quello di prima.
        """
        if not 1 <= total <= 255:
            raise ValueError("il numero di contenitori deve essere compreso tra 1 e 255")
        complessivo = total if accession_total is None else int(accession_total)
        if not 1 <= complessivo <= 255:
            raise ValueError("il totale dell'accettazione deve essere compreso tra 1 e 255")
        created: list[int] = []
        # Tutto o niente: un reperto con meta' dei contenitori previsti sarebbe
        # peggio di un reperto senza contenitori, perche' sembrerebbe completo.
        try:
            with self._conn:
                for scarto in range(total):
                    indice = start_index + scarto
                    container = Container(
                        index=indice, total=complessivo, specimen_id=specimen_id
                    )
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

    def plan_accession(
        self, case_id: int, reperti: "Sequence[tuple[Specimen, int]]"
    ) -> list[dict[str, Any]]:
        """Registra i reperti di un'accettazione e i loro contenitori.

        Un'accettazione puo' contenere **piu' reperti diversi dello stesso
        paziente**: quattro campioni presi nella stessa seduta hanno lo stesso
        nome sopra, ma descrizione, materiale, fissativo, sede e avvertenze
        possono essere tutti diversi — ed e' il caso normale, non l'eccezione.
        Un reperto solo, con N vasetti uguali, resta il caso piu' frequente e
        continua a funzionare com'era.

        La numerazione e' dell'accettazione: i contenitori vanno da 1 a N
        nell'ordine in cui sono stati trascritti, qualunque sia il reperto a
        cui appartengono.
        """
        elenco = list(reperti)
        if not elenco:
            raise ValueError("un'accettazione deve avere almeno un reperto")
        complessivo = sum(max(1, int(quanti)) for _, quanti in elenco)
        if not 1 <= complessivo <= 255:
            raise ValueError("il numero di contenitori deve essere compreso tra 1 e 255")

        pianificati: list[dict[str, Any]] = []
        indice = 1
        for reperto, quanti in elenco:
            quanti = max(1, int(quanti))
            specimen_id = self.add_specimen(
                Specimen(
                    case_id=case_id,
                    descrizione=reperto.descrizione,
                    material_code=reperto.material_code,
                    site_code=reperto.site_code,
                    fixative_code=reperto.fixative_code,
                    flags=reperto.flags,
                )
            )
            contenitori = self.plan_containers(
                specimen_id, quanti, start_index=indice, accession_total=complessivo
            )
            for scarto, container_id in enumerate(contenitori):
                pianificati.append(
                    {
                        "container_id": container_id,
                        "specimen_id": specimen_id,
                        "index": indice + scarto,
                        "total": complessivo,
                    }
                )
            indice += quanti
        return pianificati

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

    def provision_attempt(self, container_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM provision_attempts WHERE container_id=?", (container_id,)).fetchone()
        return dict(row) if row else None

    def begin_provision(self, container_id: int, previous_epc: str, epc: str, tid: str, fingerprint: str) -> None:
        """Prenotazione durevole prima del primo comando che modifica il tag."""
        with self._conn:
            self._conn.execute(
                "INSERT INTO provision_attempts VALUES (?,?,?,?,?,?,?)",
                (container_id, previous_epc, epc, tid, fingerprint, "avviata", _now()),
            )

    def provision_phase(self, container_id: int, phase: str) -> None:
        self._conn.execute("UPDATE provision_attempts SET phase=?, updated_at=? WHERE container_id=?",
                           (phase, _now(), container_id))
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
               s.descrizione, s.material_code, s.site_code, s.fixative_code, s.flags,
               k.accession_id, k.data_prelievo, k.ora_prelievo, k.external_ref,
               p.codice_fiscale, p.cognome, p.nome, p.data_nascita, p.sesso
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
            codice_fiscale=row["codice_fiscale"] or "",
            cognome=row["cognome"],
            nome=row["nome"],
            material_code=row["material_code"],
            site_code=row["site_code"],
            fixative_code=row["fixative_code"],
            flags=row["flags"],
            external_ref=row["external_ref"],
            descrizione=row["descrizione"],
            data_prelievo=_as_date(row["data_prelievo"]),
            ora_prelievo=row["ora_prelievo"] or "",
            data_nascita=_as_date(row["data_nascita"]),
            sesso=row["sesso"] or "",
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
        provisioned: bool = False,
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
                if allow_rewrite:
                    # La deroga riassegna un tag che ne aveva gia' una aperta.
                    # Lasciarla aperta significherebbe due assegnazioni vive per
                    # lo stesso TID, e `active_assignment` non saprebbe piu' quale
                    # delle due e' quella buona: la storia del tag diventerebbe
                    # illeggibile proprio nel caso in cui serve rileggerla.
                    self._conn.execute(
                        "UPDATE tag_assignments SET released_at=?, release_reason=? "
                        "WHERE tid=? AND released_at IS NULL",
                        (_now(), "riscrittura autorizzata", tid),
                    )
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
                if provisioned:
                    # Stato del contenitore e assegnazione del chip devono
                    # diventare definitivi nella stessa transazione.
                    self._conn.execute(
                        "UPDATE containers SET state=?, provisioned_at=? WHERE id=?",
                        (ContainerState.PROVISIONED.value, _now(), container_id),
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

        # La numerazione appartiene all'accettazione, non al reperto: se ci sono
        # piu' reperti bisogna guardarli tutti, altrimenti si assegnerebbero
        # due volte gli stessi numeri.
        riga = self._conn.execute(
            "SELECT case_id FROM specimens WHERE id=?", (specimen_id,)
        ).fetchone()
        if riga is None:
            raise NotFoundError(f"il reperto {specimen_id} non esiste")
        case_id = int(riga["case_id"])

        attivi = [
            dict(row)
            for row in self._conn.execute(
                """
                SELECT c.id, c.idx, c.total, c.epc, c.state, c.specimen_id
                  FROM containers c
                  JOIN specimens s ON s.id = c.specimen_id
                 WHERE s.case_id=? AND c.state<>?
                 ORDER BY c.idx
                """,
                (case_id, ContainerState.VOIDED.value),
            ).fetchall()
        ]
        if not attivi:
            raise NotFoundError(f"il reperto {specimen_id} non ha contenitori attivi")
        altri_reperti = {c["specimen_id"] for c in attivi} - {specimen_id}

        scritti = [c for c in attivi if c["epc"]]
        bloccati = [c for c in scritti if c["idx"] > new_total]
        if bloccati:
            etichette = ", ".join(f"{c['idx']}/{c['total']}" for c in bloccati)
            raise LimsDatabaseError(
                f"non si puo' scendere a {new_total}: i contenitori {etichette} sono "
                "gia' stati scritti. Vanno annullati singolarmente con una motivazione."
            )

        if altri_reperti:
            # Con piu' reperti «quanti sono in tutto» non individua piu' quale
            # cambiare: aggiungere o togliere un vasetto va fatto sul reperto
            # che lo riguarda, dicendo quale.
            raise LimsDatabaseError(
                "questa accettazione ha piu' reperti: il numero di contenitori "
                "va corretto su un reperto alla volta"
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

    # -- contesto operativo persistente -----------------------------------
    def workflow_context(self) -> dict[str, Any]:
        """Ritorna il lavoro attivo, cosi' un riavvio non azzera la postazione."""
        riga = self._conn.execute(
            "SELECT * FROM workflow_context WHERE id=1"
        ).fetchone()
        return dict(riga) if riga else {
            "active_specimen_id": None,
            "active_shipment_id": None,
            "active_inbound_id": None,
            "count_confirmed": 0,
        }

    def save_workflow_context(
        self,
        *,
        specimen_id: int | None,
        shipment_id: int | None,
        inbound_id: int | None,
        count_confirmed: bool,
    ) -> None:
        if specimen_id is not None:
            self._conn.execute(
                "UPDATE cases SET count_confirmed=? WHERE id=(SELECT case_id FROM specimens WHERE id=?)",
                (int(count_confirmed), specimen_id),
            )
        self._conn.execute(
            """
            INSERT INTO workflow_context
                (id, active_specimen_id, active_shipment_id, active_inbound_id,
                 count_confirmed, updated_at)
            VALUES (1,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                active_specimen_id=excluded.active_specimen_id,
                active_shipment_id=excluded.active_shipment_id,
                active_inbound_id=excluded.active_inbound_id,
                count_confirmed=excluded.count_confirmed,
                updated_at=excluded.updated_at
            """,
            (
                specimen_id,
                shipment_id,
                inbound_id,
                1 if count_confirmed else 0,
                _now(),
            ),
        )
        self._conn.commit()

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

    def create_shipment_with_containers(
        self, shipment: Shipment, container_ids: Iterable[int]
    ) -> int:
        """Crea bozza e composizione come un'unica transazione."""
        ids = tuple(container_ids)
        try:
            with self._conn:
                cursore = self._conn.execute(
                    "INSERT INTO shipments (destinazione, data, state, note, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (
                        shipment.destinazione,
                        _as_iso(shipment.data),
                        shipment.state.value,
                        shipment.note,
                        _now(),
                    ),
                )
                shipment_id = int(cursore.lastrowid)
                self.add_to_shipment(shipment_id, ids)
        except Exception:
            self._conn.rollback()
            raise
        return shipment_id

    def add_to_shipment(self, shipment_id: int, container_ids: Iterable[int]) -> int:
        ids = tuple(dict.fromkeys(int(value) for value in container_ids))
        if not ids:
            raise LimsDatabaseError("selezionare almeno un contenitore")
        stato_spedizione = self.shipment_row(shipment_id)["state"]
        if stato_spedizione != ShipmentState.OPEN.value:
            raise LimsDatabaseError(
                "la composizione di una spedizione non aperta e' immutabile"
            )
        segnaposti = ",".join("?" for _ in ids)
        with self._conn:
            righe = self._conn.execute(
                f"SELECT id, state FROM containers WHERE id IN ({segnaposti})", ids
            ).fetchall()
            if len(righe) != len(ids):
                raise NotFoundError("uno o piu' contenitori selezionati non esistono")
            non_pronti = [r["id"] for r in righe if r["state"] != ContainerState.PROVISIONED.value]
            if non_pronti:
                raise LimsDatabaseError(
                    "i contenitori selezionati non sono tutti pronti: "
                    + ", ".join(map(str, non_pronti))
                )
            gia_assegnati = self._conn.execute(
                f"""
                SELECT i.container_id
                  FROM shipment_items i
                  JOIN shipments s ON s.id=i.shipment_id
                 WHERE i.container_id IN ({segnaposti}) AND s.state <> ?
                """,
                (*ids, ShipmentState.CANCELLED.value),
            ).fetchall()
            if gia_assegnati:
                raise LimsDatabaseError("un contenitore e' gia' in un'altra spedizione")
            adesso = _now()
            self._conn.executemany(
                "INSERT INTO shipment_items (shipment_id, container_id, added_at) VALUES (?,?,?)",
                [(shipment_id, container_id, adesso) for container_id in ids],
            )
            self._conn.executemany(
                "UPDATE containers SET state=? WHERE id=?",
                [(ContainerState.PACKED.value, container_id) for container_id in ids],
            )
        return len(ids)

    def remove_from_shipment(self, shipment_id: int, container_ids: Iterable[int]) -> int:
        """Toglie contenitori da una spedizione ancora aperta.

        Serve al riempimento: se la lettura ha aggiunto un contenitore che sta
        sul tavolo accanto e non nella scatola, l'operatore deve poterlo
        togliere subito. Su una spedizione gia' sigillata non si tocca niente —
        li' la composizione e' quella certificata.
        """
        ids = tuple(dict.fromkeys(int(value) for value in container_ids))
        if not ids:
            return 0
        if self.shipment_row(shipment_id)["state"] != ShipmentState.OPEN.value:
            raise LimsDatabaseError(
                "la composizione di una spedizione non aperta e' immutabile"
            )
        segnaposti = ",".join("?" for _ in ids)
        with self._conn:
            cursore = self._conn.execute(
                f"DELETE FROM shipment_items WHERE shipment_id=? AND container_id IN ({segnaposti})",
                (shipment_id, *ids),
            )
            tolti = int(cursore.rowcount or 0)
            # Tornano pronti da spedire: il contenitore esiste ancora, e'
            # soltanto uscito da questa scatola.
            self._conn.executemany(
                "UPDATE containers SET state=? WHERE id=? AND state=?",
                [
                    (ContainerState.PROVISIONED.value, container_id, ContainerState.PACKED.value)
                    for container_id in ids
                ],
            )
        return tolti

    def ready_containers(self) -> list[ContainerRecord]:
        righe = self._conn.execute(
            self._RECORD_QUERY
            + " WHERE c.state=? ORDER BY k.accession_id, s.id, c.idx",
            (ContainerState.PROVISIONED.value,),
        ).fetchall()
        return [self._row_to_record(riga) for riga in righe]

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
            ShipmentState.EXPORTED.value: "exported_at",
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

    def shipment_row(self, shipment_id: int) -> dict[str, Any]:
        riga = self._conn.execute(
            "SELECT * FROM shipments WHERE id=?", (int(shipment_id),)
        ).fetchone()
        if riga is None:
            raise NotFoundError(f"spedizione {shipment_id} inesistente")
        return dict(riga)

    def mark_manifest_exported(self, shipment_id: int, manifest_hash: str) -> None:
        stato = self.shipment_row(shipment_id)["state"]
        if stato not in (ShipmentState.SEALED.value, ShipmentState.EXPORTED.value):
            raise LimsDatabaseError("la distinta si esporta solo dopo un sigillo valido")
        self._conn.execute(
            "UPDATE shipments SET state=?, exported_at=?, manifest_hash=? WHERE id=?",
            (ShipmentState.EXPORTED.value, _now(), manifest_hash, shipment_id),
        )
        self._conn.commit()

    def confirm_shipment_sent(self, shipment_id: int) -> None:
        """Conferma la partenza e solo qui chiude contenitori e tag."""
        stato = self.shipment_row(shipment_id)["state"]
        if stato != ShipmentState.EXPORTED.value:
            raise LimsDatabaseError(
                "prima di confermare la partenza occorre esportare la distinta"
            )
        with self._conn:
            self._conn.execute(
                "UPDATE shipments SET state=?, sent_at=? WHERE id=?",
                (ShipmentState.SENT.value, _now(), shipment_id),
            )
            self._conn.execute(
                """
                UPDATE containers SET state=?
                 WHERE id IN (SELECT container_id FROM shipment_items WHERE shipment_id=?)
                """,
                (ContainerState.SHIPPED.value, shipment_id),
            )
            self._conn.execute(
                """
                UPDATE tags SET state=?
                 WHERE tid IN (
                    SELECT c.tid FROM containers c
                    JOIN shipment_items i ON i.container_id=c.id
                    WHERE i.shipment_id=? AND c.tid IS NOT NULL AND c.tid <> ''
                 )
                """,
                (TagState.SHIPPED.value, shipment_id),
            )

    def cancel_shipment(self, shipment_id: int) -> None:
        stato = self.shipment_row(shipment_id)["state"]
        if stato in (ShipmentState.SENT.value, ShipmentState.RECEIVED.value):
            raise LimsDatabaseError("una spedizione gia' partita non puo' essere annullata")
        with self._conn:
            self._conn.execute(
                """
                UPDATE containers SET state=?
                 WHERE state=? AND id IN (
                    SELECT container_id FROM shipment_items WHERE shipment_id=?
                 )
                """,
                (ContainerState.PROVISIONED.value, ContainerState.PACKED.value, shipment_id),
            )
            self._conn.execute(
                "UPDATE shipments SET state=? WHERE id=?",
                (ShipmentState.CANCELLED.value, shipment_id),
            )
            self._conn.execute(
                "UPDATE outbound_manifests SET state='superseded' WHERE shipment_id=?",
                (int(shipment_id),),
            )

    def record_shipment_sealing(
        self,
        shipment_id: int,
        *,
        ok: bool,
        detail: str = "",
        operator: str = "",
        record: Mapping[str, Any] | None = None,
    ) -> None:
        """Attacca alla spedizione l'esito del sigillo e chi l'ha supervisionata."""
        self._conn.execute(
            "UPDATE shipments SET sealing_ok=?, sealing_detail=?, operator=?, "
            "sealing_json=? WHERE id=?",
            (
                1 if ok else 0,
                str(detail),
                str(operator),
                json.dumps(dict(record or {}), ensure_ascii=False, sort_keys=True),
                shipment_id,
            ),
        )
        self._conn.commit()

    # -- distinta immutabile e consegna PEC -------------------------------
    def outbound_manifest(self, shipment_id: int) -> dict[str, Any] | None:
        riga = self._conn.execute(
            "SELECT * FROM outbound_manifests WHERE shipment_id=?", (int(shipment_id),)
        ).fetchone()
        return dict(riga) if riga else None

    def archive_outbound_manifest(
        self,
        shipment_id: int,
        *,
        manifest_uuid: str,
        encrypted_blob: bytes,
        manifest_hash: str,
        filename: str,
        item_count: int,
        source_code: str,
        destination_code: str,
        signer_fingerprint: str = "",
        recipient_fingerprint: str = "",
        archive_path: str = "",
        operator: str = "",
    ) -> dict[str, Any]:
        """Archivia una sola rappresentazione byte-per-byte della distinta."""
        esistente = self.outbound_manifest(shipment_id)
        if esistente:
            if esistente["manifest_hash"] != str(manifest_hash).lower():
                raise LimsDatabaseError(
                    "la spedizione possiede gia' una distinta immutabile diversa"
                )
            return esistente
        stato = self.shipment_row(shipment_id)
        if stato["state"] not in (ShipmentState.SEALED.value, ShipmentState.EXPORTED.value):
            raise LimsDatabaseError("la distinta si archivia solo dopo un sigillo valido")
        if stato.get("sealing_ok") != 1:
            raise LimsDatabaseError("il sigillo della spedizione non e' valido")
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO outbound_manifests
                        (shipment_id, manifest_uuid, encrypted_blob, manifest_hash,
                         filename, item_count, source_code, destination_code,
                         signer_fingerprint, recipient_fingerprint, archive_path,
                         created_at, created_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(shipment_id),
                        str(manifest_uuid),
                        sqlite3.Binary(bytes(encrypted_blob)),
                        str(manifest_hash).lower(),
                        str(filename),
                        int(item_count),
                        str(source_code),
                        str(destination_code),
                        str(signer_fingerprint).upper(),
                        str(recipient_fingerprint).upper(),
                        str(archive_path),
                        _now(),
                        str(operator),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise LimsDatabaseError(f"distinta gia' archiviata: {exc}") from exc
        return self.outbound_manifest(shipment_id) or {}

    def update_outbound_archive_path(self, shipment_id: int, archive_path: str) -> None:
        self._conn.execute(
            "UPDATE outbound_manifests SET archive_path=? WHERE shipment_id=?",
            (str(archive_path), int(shipment_id)),
        )
        self._conn.commit()

    def mark_outbound_smtp_accepted(
        self, shipment_id: int, message_id: str, raw_message: bytes = b""
    ) -> None:
        self._conn.execute(
            """
            UPDATE outbound_manifests
               SET state='smtp_accepted', message_id=?, sent_eml=?, smtp_accepted_at=?,
                   attempts=attempts+1, last_error=''
             WHERE shipment_id=?
            """,
            (
                str(message_id),
                sqlite3.Binary(bytes(raw_message)),
                _now(),
                int(shipment_id),
            ),
        )
        self._conn.commit()

    def mark_outbound_error(self, shipment_id: int, detail: str, *, uncertain: bool) -> None:
        self._conn.execute(
            """
            UPDATE outbound_manifests
               SET state=?, attempts=attempts+1, last_error=?
             WHERE shipment_id=?
            """,
            ("delivery_unknown" if uncertain else "failed", str(detail), int(shipment_id)),
        )
        self._conn.commit()

    def find_outbound_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        normalizzato = str(message_id).strip().strip("<>")
        righe = self._conn.execute(
            "SELECT * FROM outbound_manifests WHERE message_id<>''"
        ).fetchall()
        for riga in righe:
            if str(riga["message_id"]).strip().strip("<>") == normalizzato:
                return dict(riga)
        return None

    def record_pec_receipt(
        self,
        outbound_id: int,
        *,
        receipt_type: str,
        message_id: str,
        raw_eml: bytes,
        daticert_xml: bytes = b"",
    ) -> bool:
        """Conserva la ricevuta integrale; `False` indica un duplicato innocuo."""
        tipo = str(receipt_type).strip().lower()
        impronta = hashlib.sha256(bytes(raw_eml)).hexdigest()
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO pec_receipts
                        (outbound_id, receipt_type, message_id, received_at,
                         raw_eml, daticert_xml, receipt_hash)
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        int(outbound_id),
                        tipo,
                        str(message_id),
                        _now(),
                        sqlite3.Binary(bytes(raw_eml)),
                        sqlite3.Binary(bytes(daticert_xml)) if daticert_xml else None,
                        impronta,
                    ),
                )
                stato = None
                colonna = None
                if tipo == "accettazione":
                    stato, colonna = "pec_accepted", "pec_accepted_at"
                elif tipo == "avvenuta-consegna":
                    stato, colonna = "delivered", "delivered_at"
                elif tipo in {
                    "non-accettazione",
                    "errore-consegna",
                    "preavviso-errore-consegna",
                    "rilevazione-virus",
                }:
                    stato = "failed"
                corrente = self._conn.execute(
                    "SELECT state FROM outbound_manifests WHERE id=?",
                    (int(outbound_id),),
                ).fetchone()
                gia_consegnata = bool(corrente and corrente["state"] == "delivered")
                if colonna and (stato == "delivered" or not gia_consegnata):
                    self._conn.execute(
                        f"UPDATE outbound_manifests SET state=?, {colonna}=? WHERE id=?",
                        (stato, _now(), int(outbound_id)),
                    )
                elif stato and not gia_consegnata:
                    self._conn.execute(
                        "UPDATE outbound_manifests SET state=?, last_error=? WHERE id=?",
                        (stato, f"ricevuta PEC: {tipo}", int(outbound_id)),
                    )
        except sqlite3.IntegrityError:
            return False
        return True

    def pec_receipts(self, shipment_id: int) -> list[dict[str, Any]]:
        righe = self._conn.execute(
            """
            SELECT r.* FROM pec_receipts r
            JOIN outbound_manifests o ON o.id=r.outbound_id
            WHERE o.shipment_id=? ORDER BY r.id
            """,
            (int(shipment_id),),
        ).fetchall()
        return [dict(riga) for riga in righe]

    def record_departure_override(
        self, shipment_id: int, *, operator: str, reason: str
    ) -> None:
        motivo = str(reason).strip()
        if not motivo:
            raise LimsDatabaseError("la deroga richiede una motivazione")
        self._conn.execute(
            """
            UPDATE shipments
               SET departure_override_at=?, departure_override_by=?,
                   departure_override_reason=?
             WHERE id=?
            """,
            (_now(), str(operator), motivo, int(shipment_id)),
        )
        self._conn.commit()

    # -- ricezioni --------------------------------------------------------
    # -- arrivi confermati dal destinatario ---------------------------------
    def record_arrival(
        self,
        shipment_id: int,
        *,
        manifest_uuid: str = "",
        remote_inbound_id: int | None = None,
        operator: str = "",
        arrived_at: str = "",
        confirmed_at: str = "",
        expected: int = 0,
        arrived: int = 0,
        missing: int = 0,
        unexpected: int = 0,
        ok: bool = False,
        nonconformity: str = "",
        detail: Mapping[str, Any] | None = None,
        signer_fingerprint: str = "",
    ) -> int:
        """Registra il verbale di riscontro arrivato dal destinatario.

        Si sostituisce se ne arriva uno nuovo per la stessa spedizione: capita
        quando il destinatario riapre una ricezione e la richiude, e l'ultimo
        verbale e' quello che vale. La sostituzione non cancella niente
        d'altro: la spedizione, la distinta e il sigillo restano dove sono.
        """
        riga = self.shipment_row(int(shipment_id))
        adesso = _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO shipment_arrivals
                    (shipment_id, remote_inbound_id, manifest_uuid, arrived_at,
                     confirmed_at, operator, expected, arrived, missing, unexpected,
                     ok, nonconformity, detail_json, signer_fingerprint, imported_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(shipment_id) DO UPDATE SET
                    remote_inbound_id=excluded.remote_inbound_id,
                    manifest_uuid=excluded.manifest_uuid,
                    arrived_at=excluded.arrived_at,
                    confirmed_at=excluded.confirmed_at,
                    operator=excluded.operator,
                    expected=excluded.expected,
                    arrived=excluded.arrived,
                    missing=excluded.missing,
                    unexpected=excluded.unexpected,
                    ok=excluded.ok,
                    nonconformity=excluded.nonconformity,
                    detail_json=excluded.detail_json,
                    signer_fingerprint=excluded.signer_fingerprint,
                    imported_at=excluded.imported_at
                """,
                (
                    int(shipment_id),
                    remote_inbound_id,
                    str(manifest_uuid or ""),
                    arrived_at or None,
                    confirmed_at or adesso,
                    operator,
                    int(expected),
                    int(arrived),
                    int(missing),
                    int(unexpected),
                    1 if ok else 0,
                    nonconformity,
                    json.dumps(dict(detail or {}), ensure_ascii=False),
                    signer_fingerprint,
                    adesso,
                ),
            )
            # Lo stato della spedizione segue il fatto: se il verbale dice che
            # e' arrivata tutta, «ricevuta» e' la verita'. Se manca qualcosa
            # resta «spedita», perche' quella spedizione non e' finita bene e
            # non deve sembrare chiusa.
            if ok and riga["state"] in (
                ShipmentState.SENT.value,
                ShipmentState.EXPORTED.value,
                ShipmentState.SEALED.value,
            ):
                self._conn.execute(
                    "UPDATE shipments SET state=? WHERE id=?",
                    (ShipmentState.RECEIVED.value, int(shipment_id)),
                )
        return int(shipment_id)

    def arrival_for_shipment(self, shipment_id: int) -> dict[str, Any] | None:
        riga = self._conn.execute(
            "SELECT * FROM shipment_arrivals WHERE shipment_id=?", (int(shipment_id),)
        ).fetchone()
        if riga is None:
            return None
        voce = dict(riga)
        try:
            voce["detail"] = json.loads(voce.get("detail_json") or "{}")
        except ValueError:
            voce["detail"] = {}
        voce["ok"] = bool(voce["ok"])
        return voce

    def transit_summary(
        self, destinazione: str, *, dal: str = "", al: str = ""
    ) -> list[dict[str, Any]]:
        """Le spedizioni verso una controparte, con quello che si sa dell'arrivo.

        Solo cio' che un verbale ha confermato risulta arrivato. Il resto e'
        marcato come non confermato invece che dato per buono: e' la
        differenza fra sapere e sperare, ed e' tutto il motivo per cui il
        verbale esiste.
        """
        condizioni = ["s.state != ?"]
        parametri: list[Any] = [ShipmentState.CANCELLED.value]
        if destinazione:
            condizioni.append("s.destinazione = ?")
            parametri.append(destinazione)
        if dal:
            condizioni.append("COALESCE(s.sent_at, s.sealed_at, s.data) >= ?")
            parametri.append(dal)
        if al:
            # Fino a tutto il giorno indicato: chi scrive una data intende il
            # giorno intero, non la sua mezzanotte.
            condizioni.append("COALESCE(s.sent_at, s.sealed_at, s.data) <= ?")
            parametri.append(al + "T23:59:59")

        righe = self._conn.execute(
            f"""
            SELECT s.id, s.destinazione, s.data, s.state, s.sealed_at, s.exported_at,
                   s.sent_at, s.sealing_ok, s.sealing_detail,
                   a.ok AS arrivo_ok, a.confirmed_at AS arrivo_il, a.operator AS arrivo_da,
                   a.expected AS arrivo_attesi, a.arrived AS arrivo_arrivati,
                   a.missing AS arrivo_mancanti, a.unexpected AS arrivo_inattesi,
                   a.nonconformity AS arrivo_non_conformita,
                   COUNT(i.container_id) AS pezzi
              FROM shipments s
              LEFT JOIN shipment_items i ON i.shipment_id = s.id
              LEFT JOIN shipment_arrivals a ON a.shipment_id = s.id
             WHERE {" AND ".join(condizioni)}
             GROUP BY s.id
             ORDER BY COALESCE(s.sent_at, s.sealed_at, s.data), s.id
            """,
            parametri,
        ).fetchall()
        return [dict(riga) for riga in righe]

    def patients_in_shipment(self, shipment_id: int) -> list[dict[str, Any]]:
        """Chi c'era dentro una spedizione, un paziente per riga."""
        righe = self._conn.execute(
            """
            SELECT p.codice_fiscale, p.cognome, p.nome, COUNT(c.id) AS pezzi
              FROM shipment_items i
              JOIN containers c ON c.id = i.container_id
              JOIN specimens s ON s.id = c.specimen_id
              JOIN cases k ON k.id = s.case_id
              JOIN patients p ON p.id = k.patient_id
             WHERE i.shipment_id = ?
             GROUP BY p.id
             ORDER BY p.cognome, p.nome
            """,
            (int(shipment_id),),
        ).fetchall()
        return [dict(riga) for riga in righe]

    def import_inbound_manifest(
        self,
        *,
        origin_lab_id: int,
        origin_shipment_id: int,
        manifest_hash: str,
        encrypted_blob: bytes,
        destination: str,
        source_created_at: str,
        operator: str,
        manifest_uuid: str = "",
        verification_ok: bool | None = None,
        signer_code: str = "",
        signer_fingerprint: str = "",
        recipient_fingerprint: str = "",
        verification_detail: str = "",
    ) -> tuple[int, bool]:
        """Registra una distinta una sola volta; ritorna ``(id, gia_nota)``."""
        esistente = self._conn.execute(
            "SELECT id, manifest_hash FROM inbound_shipments "
            "WHERE origin_lab_id=? AND origin_shipment_id=?",
            (int(origin_lab_id), int(origin_shipment_id)),
        ).fetchone()
        if esistente:
            if esistente["manifest_hash"] != manifest_hash:
                raise LimsDatabaseError(
                    "esiste gia' una distinta diversa con lo stesso mittente e numero"
                )
            return int(esistente["id"]), True
        try:
            cursore = self._conn.execute(
                """
                INSERT INTO inbound_shipments
                    (origin_lab_id, origin_shipment_id, manifest_hash, encrypted_blob,
                     destination, source_created_at, imported_at, imported_by,
                     manifest_uuid, verification_ok, signer_code, signer_fingerprint,
                     recipient_fingerprint, verification_detail)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(origin_lab_id),
                    int(origin_shipment_id),
                    manifest_hash,
                    sqlite3.Binary(encrypted_blob),
                    destination,
                    source_created_at,
                    _now(),
                    operator,
                    str(manifest_uuid),
                    None if verification_ok is None else (1 if verification_ok else 0),
                    str(signer_code),
                    str(signer_fingerprint).upper(),
                    str(recipient_fingerprint).upper(),
                    str(verification_detail),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LimsDatabaseError("distinta gia' importata") from exc
        self._conn.commit()
        return int(cursore.lastrowid), False

    def inbound_row(self, inbound_id: int) -> dict[str, Any]:
        riga = self._conn.execute(
            "SELECT * FROM inbound_shipments WHERE id=?", (int(inbound_id),)
        ).fetchone()
        if riga is None:
            raise NotFoundError(f"ricezione {inbound_id} inesistente")
        return dict(riga)

    def record_inbound_reconciliation(
        self,
        inbound_id: int,
        *,
        operator: str,
        expected: Iterable[str],
        arrived: Iterable[str],
        missing: Iterable[str],
        unexpected: Iterable[str],
        detail: str = "",
    ) -> int:
        attesi = sorted(set(expected))
        arrivati = sorted(set(arrived))
        mancanti = sorted(set(missing))
        inattesi = sorted(set(unexpected))
        cursore = self._conn.execute(
            """
            INSERT INTO inbound_reconciliations
                (inbound_id, ts, operator, ok, expected_json, arrived_json,
                 missing_json, unexpected_json, detail)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                int(inbound_id),
                _now(),
                operator,
                0 if mancanti or inattesi else 1,
                json.dumps(attesi),
                json.dumps(arrivati),
                json.dumps(mancanti),
                json.dumps(inattesi),
                detail,
            ),
        )
        self._conn.commit()
        return int(cursore.lastrowid)

    def latest_inbound_reconciliation(self, inbound_id: int) -> dict[str, Any] | None:
        riga = self._conn.execute(
            """
            SELECT * FROM inbound_reconciliations
             WHERE inbound_id=? ORDER BY id DESC LIMIT 1
            """,
            (int(inbound_id),),
        ).fetchone()
        if riga is None:
            return None
        esito = dict(riga)
        for nome in ("expected", "arrived", "missing", "unexpected"):
            esito[nome] = json.loads(esito.pop(f"{nome}_json"))
        esito["ok"] = bool(esito["ok"])
        return esito

    def confirm_inbound_receipt(
        self, inbound_id: int, *, operator: str, nonconformity_reason: str = ""
    ) -> None:
        ultima = self.latest_inbound_reconciliation(inbound_id)
        if ultima is None:
            raise LimsDatabaseError("eseguire prima la lettura e il confronto")
        motivo = str(nonconformity_reason).strip()
        if not ultima["ok"] and not motivo:
            raise LimsDatabaseError(
                "per chiudere una ricezione non conforme e' obbligatoria una motivazione"
            )
        self._conn.execute(
            """
            UPDATE inbound_shipments
               SET state=?, confirmed_at=?, confirmed_by=?, nonconformity_reason=?
             WHERE id=?
            """,
            ("received", _now(), operator, motivo, int(inbound_id)),
        )
        self._conn.commit()

    # -- archivio: ricerca e storico ---------------------------------------
    #: Come si ordina l'elenco dei pazienti. Sono due domande diverse: «cosa e'
    #: passato di qui ultimamente» e «trovami questo cognome che non so scrivere».
    ORDINI_PAZIENTI = {
        "recenti": "ultima DESC NULLS LAST, p.cognome, p.nome",
        "alfabetico": "p.cognome, p.nome, ultima DESC NULLS LAST",
    }

    def search_patients(
        self,
        query: str = "",
        *,
        limit: int = 50,
        offset: int = 0,
        order: str = "recenti",
    ) -> list[dict[str, Any]]:
        """Cerca per cognome, nome, codice fiscale o numero di accettazione.

        Una ricerca sola su tutti i campi: chi ha in mano un foglio non sa in
        quale colonna cercare, sa solo cosa c'e' scritto sopra.

        **Una ricerca vuota vuol dire «tutti»**, non «nessuno». L'archivio serve
        anche a sfogliare: chi cerca un caso di tre mesi fa spesso non ricorda il
        cognome, ricorda che c'era. Restituire una tabella vuota davanti a un
        archivio pieno lo manderebbe a indovinare.
        """
        dove, parametri = self._filtro_pazienti(query)
        ordine = self.ORDINI_PAZIENTI.get(str(order), self.ORDINI_PAZIENTI["recenti"])
        righe = self._conn.execute(
            f"""
            SELECT p.id, p.codice_fiscale, p.cognome, p.nome, p.data_nascita, p.sesso,
                   COUNT(DISTINCT c.id)  AS accettazioni,
                   MAX(c.created_at)     AS ultima
              FROM patients p
              LEFT JOIN cases c ON c.patient_id = p.id
             {dove}
             GROUP BY p.id
             ORDER BY {ordine}
             LIMIT ? OFFSET ?
            """,
            (*parametri, int(limit), max(0, int(offset))),
        ).fetchall()
        return [dict(riga) for riga in righe]

    def count_patients(self, query: str = "") -> int:
        """Quanti pazienti risponderebbero a questa ricerca, in tutto.

        Serve per dire «50 di 1284» invece di lasciar credere che l'archivio
        finisca dove finisce la pagina.
        """
        dove, parametri = self._filtro_pazienti(query)
        riga = self._conn.execute(
            f"""
            SELECT COUNT(*) FROM (
                SELECT p.id
                  FROM patients p
                  LEFT JOIN cases c ON c.patient_id = p.id
                 {dove}
                 GROUP BY p.id
            )
            """,
            parametri,
        ).fetchone()
        return int(riga[0])

    @staticmethod
    def _filtro_pazienti(query: str) -> tuple[str, tuple[Any, ...]]:
        """La clausola WHERE condivisa fra elenco e conteggio.

        Scritta una volta sola perche' un conteggio che filtrasse diversamente
        dall'elenco direbbe «1284 pazienti» sopra una tabella che ne pesca altri.
        """
        testo = str(query).strip()
        if not testo:
            return "", ()
        come = f"%{testo.upper()}%"
        accettazione = int(testo) if testo.isdigit() else -1
        return (
            """WHERE UPPER(p.cognome) LIKE ?
                OR UPPER(p.nome) LIKE ?
                OR UPPER(p.codice_fiscale) LIKE ?
                OR c.accession_id = ?""",
            (come, come, come, accettazione),
        )

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
                  LEFT JOIN shipment_items si
                    ON si.container_id = ct.id
                   AND EXISTS (
                       SELECT 1 FROM shipments sx
                        WHERE sx.id = si.shipment_id AND sx.state <> ?
                   )
                  LEFT JOIN shipments sh  ON sh.id = si.shipment_id
                 WHERE s.case_id = ?
                 ORDER BY ct.idx
                """,
                (ShipmentState.CANCELLED.value, caso["id"]),
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

    def other_containers_for_patient(
        self,
        codice_fiscale: str,
        *,
        exclude_accession: int | None = None,
        days: int = 1,
    ) -> list[dict[str, Any]]:
        """Gli altri contenitori attivi dello stesso paziente, di recente.

        I campioni arrivano in ordine sparso e nessuno sa, mentre trascrive il
        primo, se quel paziente ne ha altri tre in fondo al vassoio: si scopre
        solo dopo. Questa e' la risposta che il sistema puo' dare **appena la
        conosce**, cioe' alla trascrizione successiva, ed e' una nota da
        mostrare, non un blocco: due campioni dello stesso paziente sono
        normali, e il sistema non ha titolo per dubitarne.

        `days=1` significa «oggi». I contenitori annullati non contano: quelli
        sono contenitori che non esistono piu'.
        """
        cercato = str(codice_fiscale).strip().upper()
        if not cercato:
            return []
        limite = (dt.date.today() - dt.timedelta(days=max(0, int(days) - 1))).isoformat()
        righe = self._conn.execute(
            """
            SELECT c.id AS container_id, c.idx, c.total, c.epc, c.state,
                   k.accession_id, k.data_prelievo, s.descrizione
              FROM containers c
              JOIN specimens s ON s.id = c.specimen_id
              JOIN cases k ON k.id = s.case_id
              JOIN patients p ON p.id = k.patient_id
             WHERE p.codice_fiscale = ?
               AND c.state != ?
               AND k.data_prelievo >= ?
               AND (? IS NULL OR k.accession_id != ?)
             ORDER BY k.accession_id, c.idx
            """,
            (
                cercato,
                ContainerState.VOIDED.value,
                limite,
                exclude_accession,
                exclude_accession,
            ),
        ).fetchall()
        return [dict(riga) for riga in righe]

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
