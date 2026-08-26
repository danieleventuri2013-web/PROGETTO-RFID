"""Le operazioni del flusso operativo, esposte al frontend.

Questo livello sta fra il server HTTP e `lims.*`: riceve dizionari JSON, chiama
l'orchestrazione, restituisce dizionari JSON. Non conosce HTTP e non conosce
Tkinter, quindi si prova senza aprire ne' una porta ne' una finestra.

Riproduce le stesse guardie della GUI Tkinter, che non sono formalita':

* **una operazione radio alla volta** — la guardia di scrittura del servizio usa
  lo stato globale dell'ultimo inventory, quindi due operazioni sovrapposte si
  corromperebbero a vicenda;
* **conferma del numero di campioni alla prima scrittura** — da li' in poi il
  totale e' dentro i tag e non e' piu' correggibile;
* **conferma della chiusura prima del sigillo** — finche' non c'e' il sensore
  sugli agganci, e' la parola dell'operatore, e va registrata come tale;
* **mai un esito positivo se i contenitori trovati non sono quelli attesi.**
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

# Solo la tabella dei nomi delle bande: nessun I/O, nessun trasporto. La
# copia locale divergerebbe dalla fonte alla prima aggiunta di regione.
from rfid_silion.protocol import nome_regione

from lims.codec import FIXATIVES, MATERIALS, SITES, SpecimenFlags, TagPayload
from lims.codec import PAYLOAD_FIXED_SIZE
from lims.crypto import (
    Keyring,
    PayloadAuthenticationError,
    PayloadFormatError,
    UnknownKeyError,
    max_plaintext_bytes,
)
from lims.db import LimsDatabase
from lims.labels import LabelTemplate, NetworkLabelPrinter, content_from_record, render_zpl
from lims.manifest import (
    MANIFEST_MAGIC,
    MANIFEST_SCHEMA_V2,
    build_manifest,
    open_manifest,
    reconcile,
    seal_manifest,
)
from lims.model import (
    Case,
    ContainerState,
    Patient,
    Sex,
    Shipment,
    ShipmentState,
    Specimen,
    TagState,
    validate_codice_fiscale,
)
from lims.pec import PecConfig, PecError, PecTransport, resolve_secret
from lims.qr import Correzione, codifica as codifica_qr, svg as qr_svg
from lims.riscontro import (
    Riscontro,
    apri_riscontro,
    costruisci_riscontro,
    sigilla_riscontro,
)
from lims.riempimento import (
    Esito,
    SessioneRiempimento,
    politica_da_config,
    potenza_da_config,
)
from lims.sealing import ClosureProof, SealingPolicy, SealingSession, default_passes
from lims.tabella import (
    COLONNE,
    TabellaError,
    codifica_tabella,
    decodifica_tabella,
    per_stampa,
    righe_da_contenuto,
)
from lims.secure_manifest import (
    certificate_fingerprint,
    load_certificate,
    load_private_key,
    open_secure_manifest,
    seal_secure_manifest,
)
from lims.tagio import MODALITA_PAYLOAD, MODALITA_SOLO_EPC, TagIO
from rfid_silion.protocol import RF_MODE_MAX_SENSITIVITY as P_RF_MODE_MAX_SENSITIVITY
from rfid_silion.service import AntennaPower, Gen2Settings, ReaderSettings

__all__ = ["Workflow", "WorkflowError"]

log = logging.getLogger("webui.workflow")


class WorkflowError(RuntimeError):
    """Errore che l'operatore deve poter leggere e capire."""


def _oggi() -> dt.date:
    return dt.date.today()


def _codebook(codebook: Mapping[int, str]) -> list[dict[str, Any]]:
    return [{"codice": codice, "nome": nome} for codice, nome in sorted(codebook.items())]


def _intero_o_nulla(valore: Any) -> int | None:
    """`None` significa «lascia com'e'», e va distinto da zero."""
    if valore is None or valore == "":
        return None
    return int(valore)


def _etichetta_operatore(voce: Mapping[str, Any]) -> str:
    """Come si chiama un operatore nell'interfaccia e nel registro.

    Cognome o codifica, mai il nome per esteso: e' quello che l'utente ha
    chiesto, ed e' anche il minimo necessario — il registro deve dire chi ha
    scritto un tag, non compilare una rubrica.
    """
    return str(voce.get("codice") or voce.get("cognome") or "").strip()


#: Stati in cui una spedizione non occupa piu' la postazione: o e' partita,
#: o e' annullata, oppure e' sigillata e aspetta il corriere. In quest'ultimo
#: caso resta da finire (distinta, conferma di partenza) ma non impedisce di
#: riempire la scatola successiva — che e' esattamente cio' che serve quando i
#: campioni scritti sono piu' di quanti ne entrano in un contenitore.
_SPEDIZIONI_DA_PARTE = frozenset(
    {
        ShipmentState.SENT.value,
        ShipmentState.CANCELLED.value,
        ShipmentState.SEALED.value,
        ShipmentState.EXPORTED.value,
    }
)

#: La banda ETSI in cui il sistema puo' lavorare in Italia, in kHz. Tutto il
#: resto di una spazzata e' contesto: serve a capire *dove* l'antenna e'
#: accordata, non a scegliere dove usarla.
_BANDA_EU_KHZ = (865_000, 868_000)

#: Come si dice in italiano la prova che la scatola era chiusa.
_PROVE_CHIUSURA = {
    "operator": "parola dell'operatore",
    "sensor": "sensore sugli agganci",
    "none": "nessuna",
}


def _controlla_email(valore: str, dove: str) -> str:
    """Verifica minima: una chiocciola e un punto dopo.

    Non serve validare a norma di RFC — serve accorgersi del refuso prima che
    la distinta parta verso un indirizzo che non esiste.
    """
    testo = str(valore).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", testo):
        raise WorkflowError(f"{dove}: «{testo}» non sembra un indirizzo email")
    return testo


def _data_o_nulla(valore: Any) -> dt.date | None:
    """Una data dall'interfaccia, o niente.

    Vuoto e' un dato onesto: sull'etichetta del reparto la data di nascita a
    volte non c'e', e inventarla sarebbe peggio che lasciarla in bianco.
    """
    testo = str(valore or "").strip()
    if not testo:
        return None
    for formato in ("%Y-%m-%d", "%d/%m/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(testo, formato).date()
        except ValueError:
            continue
    raise WorkflowError(f"data non riconosciuta: «{testo}» (attesa gg/mm/aaaa)")


def _ora_pulita(valore: Any) -> str:
    """`14:30`, `1430`, `14.30` → `1430`. Vuoto resta vuoto."""
    testo = re.sub(r"[^0-9]", "", str(valore or ""))
    if not testo:
        return ""
    if len(testo) == 3:
        testo = "0" + testo
    if len(testo) != 4 or not 0 <= int(testo[:2]) <= 23 or not 0 <= int(testo[2:]) <= 59:
        raise WorkflowError(f"ora non riconosciuta: «{valore}» (attesa hh:mm)")
    return testo


def _esito_transito(riga: Mapping[str, Any]) -> str:
    """Com'e' finita quella spedizione, in una parola.

    «Non confermata» non e' un errore ed e' la condizione piu' comune finche'
    il verbale non torna indietro: e' semplicemente cio' che si sa.
    """
    if riga["arrivo_ok"] is None:
        if riga["sent_at"]:
            return "non confermata"
        return "non partita"
    if riga["arrivo_ok"]:
        return "arrivata"
    return "incompleta"


def _cosa_manca(riga: Mapping[str, Any]) -> str:
    """In una riga sola: cosa resta da fare su una spedizione."""
    stato = str(riga["state"])
    if stato == ShipmentState.OPEN.value:
        return "da sigillare"
    if not riga["exported_at"]:
        return "distinta da mandare"
    if not riga["sent_at"]:
        return "partenza da confermare"
    return ""


def _flag_da_avvertenze(avvertenze: Any) -> SpecimenFlags:
    flags = SpecimenFlags.NONE
    for nome in avvertenze or []:
        try:
            flags |= SpecimenFlags[str(nome).upper()]
        except KeyError as exc:
            raise WorkflowError(f"avvertenza sconosciuta: {nome}") from exc
    return flags


def _reperti_richiesti(dati: Mapping[str, Any]) -> list[dict[str, Any]]:
    """I reperti dell'accettazione, comunque li abbia mandati l'interfaccia.

    Quattro campioni presi allo stesso paziente nella stessa seduta hanno lo
    stesso nome sopra, ma **descrizione, materiale, fissativo, sede e
    avvertenze possono essere tutti diversi**: e' il caso normale, non
    l'eccezione. Da qui la forma con l'elenco.

    Resta accettata la forma piatta di prima — descrizione unica e
    `contenitori: N` — perche' e' il caso piu' frequente (N vasetti dello
    stesso reperto) e perche' un modulo che manda ancora quella non deve
    smettere di funzionare.
    """
    elenco = dati.get("reperti")
    if isinstance(elenco, (list, tuple)) and elenco:
        reperti = []
        for voce in elenco:
            if not isinstance(voce, Mapping):
                raise WorkflowError("ogni reperto deve essere un oggetto")
            quanti = int(voce.get("contenitori", 1) or 1)
            if not 1 <= quanti <= 255:
                raise WorkflowError(
                    "il numero di contenitori di un reperto deve essere fra 1 e 255"
                )
            reperti.append(
                {
                    "descrizione": str(voce.get("descrizione", "")).strip(),
                    "material_code": int(voce.get("material_code", 0) or 0),
                    "fixative_code": int(voce.get("fixative_code", 0) or 0),
                    "site_code": int(voce.get("site_code", 0) or 0),
                    "flags": _flag_da_avvertenze(voce.get("avvertenze")),
                    "contenitori": quanti,
                }
            )
        totale = sum(voce["contenitori"] for voce in reperti)
        if not 1 <= totale <= 255:
            raise WorkflowError("il numero di contenitori deve essere compreso tra 1 e 255")
        return reperti

    quanti = int(dati.get("contenitori", 1) or 1)
    if not 1 <= quanti <= 255:
        raise WorkflowError("il numero di contenitori deve essere compreso tra 1 e 255")
    return [
        {
            "descrizione": str(dati.get("descrizione", "")).strip(),
            "material_code": int(dati.get("material_code", 0) or 0),
            "fixative_code": int(dati.get("fixative_code", 0) or 0),
            "site_code": int(dati.get("site_code", 0) or 0),
            "flags": _flag_da_avvertenze(dati.get("avvertenze")),
            "contenitori": quanti,
        }
    ]


def _voci_attive(elenco: Any) -> list[dict[str, Any]]:
    """Solo le righe abilitate, senza quelle vuote."""
    voci = []
    for voce in elenco or []:
        if not isinstance(voce, Mapping):
            continue
        if voce.get("attivo") is False:
            continue
        voci.append(dict(voce))
    return voci


class Workflow:
    """Stato e operazioni di una postazione.

    Una sola istanza per processo: possiede l'archivio, il portachiavi e il
    contesto di lavoro corrente (accettazione in corso, spedizione in corso).
    """

    def __init__(
        self,
        config: Mapping[str, Any],
        backend: Any,
        *,
        config_path: str | Path | None = None,
    ):
        self.config = dict(config)
        self.backend = backend
        #: File da riscrivere quando l'operatore salva le impostazioni. Senza,
        #: le modifiche valgono solo per la sessione in corso e l'interfaccia
        #: lo dice invece di fingere di aver salvato.
        self.config_path = Path(config_path) if config_path else None
        self.lims_cfg = dict(self.config.get("lims", {}) or {})
        # Il server HTTP serve ogni richiesta su un thread diverso, quindi la
        # connessione non puo' restare legata al thread che l'ha aperta. La
        # coerenza delle *sequenze* di istruzioni la garantisce `_scrittura`,
        # che avvolge le operazioni che scrivono piu' righe insieme.
        self.db = LimsDatabase(
            self.lims_cfg.get("database", "logs/lims.db"), single_thread=False
        )
        self._scrittura = threading.RLock()
        self.keyring = self._carica_portachiavi()
        #: Cosa e' collegato davvero. Si riempie al primo collegamento
        #: (`_rileva_hardware`) e da li' in poi guida cosa l'interfaccia
        #: puo' offrire: prima si sa solo che non si sa.
        self.hardware: dict[str, Any] = {"rilevato": False, "motivo": "mai collegato"}

        self.operatore = ""

        # Accettazione in corso
        self.specimen_id: int | None = None
        self.accession_id: int | None = None
        self.external_ref = ""
        self.pending: list[dict[str, Any]] = []
        self.conteggio_confermato = False

        # Spedizione in corso
        self.shipment_id: int | None = None
        self.sealing_record: Any = None
        # Riempimento in corso: vive solo in memoria. Dopo un riavvio si
        # ricostruisce dal contenuto della scatola, che invece e' nell'archivio.
        self.riempimento: SessioneRiempimento | None = None
        # Distinta ricevuta, sul lato destinatario
        self.distinta: Any = None
        # Distinta letta dal QR sul foglio: la strada che non passa da un file.
        self.distinta_qr: Any = None
        # Campagna di misura: gli EPC dichiarati dentro e fuori dal contenitore
        self.campagna_epcs: dict[str, list[str]] = {"dentro": [], "fuori": []}
        # L'inventario preliminare vede contemporaneamente i tag interni ed
        # esterni. La classificazione successiva puo' usare solo questi EPC:
        # cosi' un refuso non introduce nella campagna un tag mai osservato.
        self.campagna_candidati: dict[str, dict[str, Any]] = {}
        self.inbound_id: int | None = None
        self._ripristina_contesto()

    # -- infrastruttura -----------------------------------------------------
    def _carica_portachiavi(self) -> Keyring:
        percorso = Path(self.lims_cfg.get("keyring", "logs/lims_keys.json"))
        if percorso.exists():
            return Keyring.load(percorso)
        # Prima esecuzione: si genera una chiave e la si salva. Va poi consegnata
        # al laboratorio destinatario fuori banda, una volta sola.
        portachiavi = Keyring()
        portachiavi.generate(0)
        portachiavi.save(percorso)
        log.warning(
            "Creato un nuovo portachiavi in %s: va consegnato ai laboratori "
            "destinatari perche' possano leggere i tag",
            percorso,
        )
        return portachiavi

    def _percorso(self, valore: str | Path) -> Path:
        """Risolve i file sensibili rispetto al file di configurazione."""
        percorso = Path(valore)
        if percorso.is_absolute() or self.config_path is None:
            return percorso
        return (self.config_path.parent / percorso).resolve()

    def _password_segreta(
        self,
        sezione: Mapping[str, Any],
        tipo: str,
        utente_predefinito: str,
    ) -> str | None:
        servizio = str(
            sezione.get(f"{tipo}_credential_service")
            or sezione.get("credential_service", "")
        ).strip()
        utente = str(
            sezione.get(f"{tipo}_credential_username")
            or sezione.get("credential_username", utente_predefinito)
        ).strip()
        if not servizio:
            return None
        try:
            return resolve_secret(servizio, utente)
        except PecError as exc:
            raise WorkflowError(str(exc)) from exc

    def _materiale_mittente(self, destinatario: Mapping[str, Any]) -> dict[str, Any]:
        sicurezza = self.config.get("security", {}) or {}
        richiesti = {
            "station_certificate": sicurezza.get("station_certificate"),
            "station_private_key": sicurezza.get("station_private_key"),
            "recipient_certificate": destinatario.get("encryption_certificate"),
        }
        mancanti = [nome for nome, valore in richiesti.items() if not valore]
        if mancanti:
            raise WorkflowError(
                "configurazione della distinta sicura incompleta: " + ", ".join(mancanti)
            )
        certificato_postazione = load_certificate(
            self._percorso(str(richiesti["station_certificate"]))
        )
        certificato_destinatario = load_certificate(
            self._percorso(str(richiesti["recipient_certificate"]))
        )
        chiave = load_private_key(
            self._percorso(str(richiesti["station_private_key"])),
            self._password_segreta(sicurezza, "station", "station-signing-key"),
        )
        return {
            "station_certificate": certificato_postazione,
            "station_private_key": chiave,
            "recipient_certificate": certificato_destinatario,
        }

    def _materiale_destinatario(self) -> dict[str, Any]:
        sicurezza = self.config.get("security", {}) or {}
        richiesti = (
            "recipient_certificate",
            "recipient_private_key",
            "trusted_ca",
        )
        mancanti = [nome for nome in richiesti if not sicurezza.get(nome)]
        if mancanti:
            raise WorkflowError(
                "configurazione di ricezione sicura incompleta: " + ", ".join(mancanti)
            )
        certificato = load_certificate(
            self._percorso(str(sicurezza["recipient_certificate"]))
        )
        chiave = load_private_key(
            self._percorso(str(sicurezza["recipient_private_key"])),
            self._password_segreta(
                sicurezza, "recipient", "recipient-decryption-key"
            ),
        )
        ca_raw = sicurezza.get("trusted_ca")
        ca_values = ca_raw if isinstance(ca_raw, list) else [ca_raw]
        autorita = [load_certificate(self._percorso(str(v))) for v in ca_values if v]
        return {
            "certificate": certificato,
            "private_key": chiave,
            "trusted_cas": autorita,
            "revoked_serials": tuple(int(v) for v in sicurezza.get("revoked_serials", [])),
        }

    def _tagio(self, *, scrittura: bool) -> TagIO:
        chiave = "write_antennas" if scrittura else "read_antennas"
        antenne = self.lims_cfg.get(chiave) or [
            antenna["id"] for antenna in self.config.get("antennas", [])
        ]
        accesso = self.config.get("tag_access", {}) or {}
        return TagIO(
            self.backend,
            self.keyring,
            lab_id=int(self.lims_cfg.get("lab_id", 0)),
            antennas=tuple(antenne),
            access_password_hex=accesso.get("access_password_hex", "00000000"),
            timeout_ms=accesso.get("timeout_ms", 1000),
            user_memory_bytes=int(self.lims_cfg.get("user_memory_bytes", 64)),
            db=self.db,
            operator=self.operatore,
            modalita=self.modalita_scrittura,
        )

    @property
    def modalita_scrittura(self) -> str:
        """Cosa si scrive sul tag: il campione cifrato, o solo lo pseudonimo.

        Un valore sconosciuto in configurazione non deve far degradare la
        sicurezza in silenzio: si torna alla modalita' piena e lo si dice nel log.
        """
        valore = str(self.lims_cfg.get("modalita_scrittura", MODALITA_PAYLOAD)).strip()
        if valore not in (MODALITA_PAYLOAD, MODALITA_SOLO_EPC):
            log.warning(
                "modalita_scrittura sconosciuta in configurazione (%r): si usa «%s»",
                valore,
                MODALITA_PAYLOAD,
            )
            return MODALITA_PAYLOAD
        return valore

    def _reader_settings(self) -> ReaderSettings:
        return ReaderSettings(
            region=self.config.get("reader", {}).get("region", 0x08),
            powers=tuple(
                AntennaPower(
                    antenna_id=antenna["id"],
                    read_power_cdbm=antenna.get("read_power", 2000),
                    write_power_cdbm=antenna.get("write_power", 2000),
                )
                for antenna in self.config.get("antennas", [])
            ),
        )

    def _snapshot_gen2(self) -> Gen2Settings:
        lettura = getattr(self.backend, "read_gen2_settings", None)
        if lettura is None:
            raise WorkflowError("il backend non consente di salvare l'assetto Gen2")
        risposta = lettura()
        if not risposta.ok:
            raise WorkflowError(
                (risposta.error or {}).get(
                    "message", "impossibile leggere l'assetto Gen2 corrente"
                )
            )
        return Gen2Settings.from_mapping((risposta.data or {}).get("settings", {}))

    def _restore_radio(self, gen2: Gen2Settings) -> None:
        configurazione = self.backend.configure(self._reader_settings())
        if not configurazione.ok:
            raise WorkflowError("ripristino delle potenze operative non riuscito")
        risposta = self.backend.configure_gen2(gen2)
        if not risposta.ok:
            raise WorkflowError("ripristino dei parametri Gen2 non riuscito")

    def chiudi(self) -> None:
        self.db.close()

    def _require_operator(self, azione: str) -> None:
        if not self.operatore.strip():
            raise WorkflowError(
                f"selezionare l'operatore in servizio prima di {azione}"
            )

    def _salva_contesto(self) -> None:
        self.db.save_workflow_context(
            specimen_id=self.specimen_id,
            shipment_id=self.shipment_id,
            inbound_id=self.inbound_id,
            count_confirmed=self.conteggio_confermato,
        )

    def _ripristina_contesto(self) -> None:
        """Ricostruisce i tre flussi attivi dall'archivio dopo un riavvio."""
        contesto = self.db.workflow_context()
        self.specimen_id = contesto.get("active_specimen_id")
        self.shipment_id = contesto.get("active_shipment_id")
        self.inbound_id = contesto.get("active_inbound_id")
        self.conteggio_confermato = bool(contesto.get("count_confirmed"))

        if self.specimen_id is not None:
            riga = self.db.connection.execute(
                """
                SELECT k.accession_id, k.data_prelievo, k.external_ref, k.note,
                       p.codice_fiscale, p.cognome, p.nome
                  FROM specimens s
                  JOIN cases k ON k.id=s.case_id
                  JOIN patients p ON p.id=k.patient_id
                 WHERE s.id=?
                """,
                (self.specimen_id,),
            ).fetchone()
            if riga is None:
                self.specimen_id = None
                self.conteggio_confermato = False
            else:
                self.accession_id = int(riga["accession_id"])
                nota = str(riga["note"] or "")
                self.external_ref = str(riga["external_ref"] or "") or (
                    nota.removeprefix("rif. esterno: ") if nota else ""
                )
                prelievo = (
                    dt.date.fromisoformat(riga["data_prelievo"])
                    if riga["data_prelievo"] else _oggi()
                )
                # Il payload si ricostruisce **da ogni contenitore**, non da un
                # reperto solo: un'accettazione puo' averne piu' d'uno, con
                # descrizioni e codici diversi, e riusare quelli del primo
                # scriverebbe nel chip il campione sbagliato.
                self.pending = [
                    {
                        "container_id": record.container_id,
                        "specimen_id": self._reperto_di(record.container_id),
                        "index": record.index,
                        "total": record.total,
                        "epc": record.epc,
                        "tid": record.tid,
                        "stato": "scritto" if record.epc else "da_scrivere",
                        "payload": TagPayload(
                            codice_fiscale=riga["codice_fiscale"],
                            accession_id=self.accession_id,
                            container_index=record.index,
                            container_total=record.total,
                            display_name=f"{riga['cognome']} {riga['nome']}",
                            data_prelievo=prelievo,
                            material_code=int(record.material_code),
                            fixative_code=int(record.fixative_code),
                            site_code=int(record.site_code),
                            flags=SpecimenFlags(int(record.flags)),
                        ),
                        "descrizione": record.descrizione,
                    }
                    for record in self.db.active_containers_for_accession(self.accession_id)
                ]

        if self.inbound_id is not None:
            try:
                blob = self.db.inbound_row(self.inbound_id)["encrypted_blob"]
                if (
                    bytes(blob).startswith(MANIFEST_MAGIC)
                    and len(blob) > len(MANIFEST_MAGIC)
                    and blob[len(MANIFEST_MAGIC)] == MANIFEST_SCHEMA_V2
                ):
                    materiale = self._materiale_destinatario()
                    self.distinta = open_secure_manifest(
                        bytes(blob),
                        recipient_private_key=materiale["private_key"],
                        recipient_certificate=materiale["certificate"],
                        trusted_cas=materiale["trusted_cas"],
                        revoked_serials=materiale["revoked_serials"],
                        expected_destination_code=str(
                            self.laboratorio().get("codice", "")
                        ),
                    ).manifest
                else:
                    self.distinta = open_manifest(bytes(blob), self.keyring)
            except Exception:  # noqa: BLE001
                log.exception("Impossibile ripristinare la ricezione %s", self.inbound_id)
                self.inbound_id = None

        self._salva_contesto()

    def _reperto_di(self, container_id: int) -> int | None:
        """A quale reperto appartiene un contenitore."""
        riga = self.db.connection.execute(
            "SELECT specimen_id FROM containers WHERE id=?", (int(container_id),)
        ).fetchone()
        return int(riga["specimen_id"]) if riga else None

    def riprendi_workflow(self) -> dict[str, Any]:
        """Snapshot unico usato dal frontend al caricamento della pagina."""
        return {
            "accettazione": self.stato_accettazione(),
            "spedizione": self.stato_spedizione(),
            "ricezione": self.stato_ricezione(),
        }

    # -- descrizione della postazione ---------------------------------------
    def descrivi(self) -> dict[str, Any]:
        """Tutto quello che serve al frontend per disegnarsi la prima volta."""
        geometria = self.config.get("geometry", {}) or {}
        webui = self.config.get("webui", {}) or {}
        return {
            "hardware": self.hardware,
            "lab_id": self.lims_cfg.get("lab_id", 0),
            "laboratorio": self.laboratorio(),
            "operatori": [
                {**voce, "etichetta": _etichetta_operatore(voce)}
                for voce in _voci_attive(self.config.get("operatori"))
                if _etichetta_operatore(voce)
            ],
            "destinatari": _voci_attive(self.config.get("destinatari")),
            "operatore": self.operatore,
            "tema": webui.get("theme", "sistema"),
            "pec": {
                "abilitata": bool((self.config.get("pec", {}) or {}).get("enabled", False)),
                "mittente": str((self.config.get("pec", {}) or {}).get("sender", "")),
            },
            "watch_interval_ms": int(webui.get("watch_interval_ms", 900)),
            # La pagina deve sapere se vale la pena accodare i propri eventi.
            "diario": {
                "interfaccia": bool(
                    (self.config.get("diario", {}) or {}).get("interfaccia", True)
                ),
                "attivo": bool((self.config.get("diario", {}) or {}).get("attivo", True)),
            },
            "antenne": {
                "scrittura": list(self.lims_cfg.get("write_antennas") or []),
                "lettura": list(self.lims_cfg.get("read_antennas") or []),
                "posizioni": geometria.get("antenna_positions_mm", []),
                "lato_mm": (geometria.get("antenna_size_mm") or [220])[0],
                # In dBm interi: e' cosi' che le mostra e le rimanda indietro
                # l'interfaccia. I centi-dBm restano un dettaglio del protocollo.
                "potenze": [
                    {
                        "id": antenna["id"],
                        "ruolo": antenna.get("role", ""),
                        "lettura": round(antenna.get("read_power", 2000) / 100),
                        "scrittura": round(antenna.get("write_power", 2000) / 100),
                    }
                    for antenna in self.config.get("antennas", [])
                ],
            },
            "volume_mm": geometria.get("volume_mm", [440, 220, 220]),
            "capienza_payload": max_plaintext_bytes(
                int(self.lims_cfg.get("user_memory_bytes", 64))
            ),
            "modalita_scrittura": self.modalita_scrittura,
            "user_memory_bytes": int(self.lims_cfg.get("user_memory_bytes", 64)),
            "codebook": {
                "materiali": _codebook(MATERIALS),
                "fissativi": _codebook(FIXATIVES),
                "sedi": _codebook(SITES),
            },
        }

    def laboratorio(self) -> dict[str, Any]:
        """Anagrafica del laboratorio che accetta, con i valori di ripiego.

        `lims.lab_name` esisteva prima di questa sezione ed e' ancora nelle
        configurazioni installate: si continua a leggerlo se `laboratorio.nome`
        e' vuoto, invece di far sparire il nome dalle etichette.
        """
        sezione = dict(self.config.get("laboratorio") or {})
        nome = str(sezione.get("nome") or self.lims_cfg.get("lab_name") or "").strip()
        codice = str(sezione.get("codice") or "").strip()
        return {
            "nome": nome,
            "codice": codice,
            # Cosa scrivere in alto: la sigla se c'e', se no il nome, se no il
            # numero — che c'e' sempre, perche' finisce in ogni EPC.
            "insegna": codice or nome or f"lab {self.lims_cfg.get('lab_id', 0)}",
            "indirizzo": str(sezione.get("indirizzo") or ""),
            "cap": str(sezione.get("cap") or ""),
            "citta": str(sezione.get("citta") or ""),
            "provincia": str(sezione.get("provincia") or ""),
            "telefono": str(sezione.get("telefono") or ""),
            "email": str(sezione.get("email") or ""),
            "referente": str(sezione.get("referente") or ""),
            "email_referente": str(sezione.get("email_referente") or ""),
        }

    def imposta_operatore(self, nome: str) -> dict[str, Any]:
        """Sceglie l'operatore in servizio.

        Se l'elenco e' configurato il nome deve venire da li': il registro
        associa ogni scrittura a chi l'ha fatta, e un nome digitato a mano ogni
        volta diverso lo rende inutilizzabile a distanza di mesi.
        """
        richiesto = str(nome).strip()
        elenco = [
            _etichetta_operatore(voce) for voce in _voci_attive(self.config.get("operatori"))
        ]
        elenco = [voce for voce in elenco if voce]
        if elenco and richiesto and richiesto not in elenco:
            raise WorkflowError(
                f"operatore sconosciuto: {richiesto}. "
                "Va aggiunto in Impostazioni prima di poter lavorare."
            )
        self.operatore = richiesto
        return {"operatore": self.operatore}

    # -- lifecycle del lettore ----------------------------------------------
    def connetti(self) -> dict[str, Any]:
        avvio = self.backend.start()
        if not avvio.ok:
            raise WorkflowError(
                (avvio.error or {}).get("message", "avvio del lettore non riuscito")
            )
        configurazione = self.backend.configure(self._reader_settings())
        # Il censimento si fa **qui**, una volta, appena il modulo risponde:
        # quali misure siano possibili dipende dall'esemplare, non dal
        # datasheet, e senza saperlo l'interfaccia proporrebbe procedure che
        # questo firmware rifiuta. Da qui in poi il resto si adatta.
        self._rileva_hardware()
        return {
            "avvio": avvio.to_dict(),
            "configurazione_ok": bool(configurazione.ok),
            "configurazione_errore": (configurazione.error or {}).get("message", ""),
            "hardware": self.hardware,
        }

    def _rileva_hardware(self) -> dict[str, Any]:
        """Interroga il modulo e conserva quello che ha risposto.

        Un censimento fallito non e' un collegamento fallito: si lavora anche
        senza sapere la certificazione, semplicemente senza poter dire quali
        misure siano disponibili. Meglio ammetterlo che indovinare.
        """
        try:
            risposta = self.backend.identify()
        except Exception as exc:  # noqa: BLE001 - il collegamento resta valido
            self.hardware = {"rilevato": False, "motivo": str(exc)}
            return self.hardware
        if not risposta.ok:
            self.hardware = {
                "rilevato": False,
                "motivo": (risposta.error or {}).get("message", "censimento non riuscito"),
            }
            return self.hardware

        dati = dict((risposta.to_dict().get("data") or {}))
        self.hardware = self._leggi_censimento(dati)
        log.info(
            "Hardware rilevato: %s | bande %s | antenne %s",
            self.hardware.get("modulo", "?"),
            ", ".join(self.hardware.get("bande_nomi", [])) or "?",
            self.hardware.get("antenne_collegate"),
        )
        self.db.log_event(
            "hardware_detect",
            ok=True,
            operator=self.operatore,
            detail=json.dumps(self.hardware, ensure_ascii=False)[:900],
        )
        return self.hardware

    def _leggi_censimento(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Da quello che il modulo ha risposto, cosa si puo' e cosa non si puo'.

        La regola sta in EX10 2024-12 §2.2 e §10.1: un modulo certificato per
        una sola regione **non accetta altre bande** (stato 0x010B) e non
        accetta elenchi di frequenze singole (il campo N deve valere 0). Su un
        modulo cosi' la spazzata larga non e' una funzione da abilitare: e' una
        cosa che il firmware non fa, e nessun software la aggira.
        """
        firmware = dati.get("firmware_info") or {}
        bande = sorted(int(b) for b in (dati.get("regions_available") or []))
        antenne = dati.get("antennas_connected")
        configurata = int(self.config.get("reader", {}).get("region", 0x08))
        # Una banda sola vuol dire modulo monoregione. Piu' d'una vuol dire
        # certificazione Cina, l'unica che permette anche le frequenze singole.
        multibanda = len(bande) > 1

        return {
            "rilevato": True,
            "modulo": firmware.get("model") or firmware.get("version") or "sconosciuto",
            "firmware": firmware,
            "trasporto": (dati.get("transport") or {}).get("description", ""),
            "seriale": dati.get("serial_number") or "",
            "temperatura_c": dati.get("temperature_c"),
            "antenne_collegate": antenne,
            "antenne_configurate": sorted(
                {int(a.get("id")) for a in (self.config.get("antennas") or []) if a.get("id")}
            ),
            "bande": bande,
            "bande_nomi": [nome_regione(b) for b in bande],
            "banda_configurata": configurata,
            "banda_configurata_nome": nome_regione(configurata),
            "multibanda": multibanda,
            # Le due cose che cambiano davvero cosa l'interfaccia puo' offrire.
            "spazzata_larga": multibanda,
            "frequenze_singole": multibanda,
            "motivo_limite": (
                ""
                if multibanda
                else "il modulo dichiara una sola banda: e' certificato per una "
                "regione e il firmware rifiuta le altre (0x010B), cosi' come "
                "gli elenchi di frequenze singole. La curva fuori banda va "
                "misurata con un analizzatore di antenna, non con il lettore."
            ),
            "non_disponibili": dati.get("non_disponibili") or {},
        }

    def disconnetti(self) -> dict[str, Any]:
        return {"stop": self.backend.stop().to_dict()}

    def stato(self) -> dict[str, Any]:
        return self.backend.snapshot()

    # -- accettazione --------------------------------------------------------
    def registra_accettazione(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Registra paziente, accettazione e reperto; pianifica i contenitori."""
        self._require_operator("registrare un'accettazione")
        if self.specimen_id is not None:
            raise WorkflowError(
                "c'e' gia' un'accettazione attiva: completarla, annullarla oppure "
                "premere Nuova accettazione"
            )
        try:
            paziente = Patient(
                codice_fiscale=validate_codice_fiscale(str(dati.get("codice_fiscale", ""))),
                cognome=str(dati.get("cognome", "")),
                nome=str(dati.get("nome", "")),
                sesso=Sex(str(dati.get("sesso", "X")).upper()),
                data_nascita=_data_o_nulla(dati.get("data_nascita")),
            )
        except (ValueError, TypeError) as exc:
            raise WorkflowError(str(exc)) from exc

        reperti = _reperti_richiesti(dati)
        totale = sum(voce["contenitori"] for voce in reperti)
        riferimento = str(dati.get("external_ref", "")).strip()

        # Cinque inserimenti che devono restare uno solo: se un'altra richiesta
        # si infilasse in mezzo, due accettazioni potrebbero prendersi lo stesso
        # numero.
        with self._scrittura:
            try:
                patient_id, accession_id, pianificati = self._inserisci(
                    paziente, dati, reperti, riferimento
                )
            except WorkflowError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc

        # Il contesto persistente ricorda un reperto solo, e va bene: e' la
        # chiave con cui si ritrova l'accettazione dopo un riavvio, e da li' si
        # rileggono tutti gli altri.
        self.specimen_id = pianificati[0]["specimen_id"]
        self.accession_id = accession_id
        # Con un contenitore solo la domanda «li hai contati?» non ha oggetto:
        # l'operatore ne ha uno in mano ed e' quello. La conferma resta
        # obbligatoria da due in su, dove il totale finisce nel chip e diventa
        # irreversibile. Si registra comunque chi l'ha data e come.
        self.conteggio_confermato = totale == 1
        if self.conteggio_confermato:
            self.db.log_event(
                "count_confirm",
                ok=True,
                operator=self.operatore,
                detail=f"accettazione {accession_id}: contenitore unico, conferma implicita",
            )
        self.external_ref = riferimento
        prelievo = _data_o_nulla(dati.get("data_prelievo")) or _oggi()
        self.pending = [
            {
                "container_id": voce["container_id"],
                "specimen_id": voce["specimen_id"],
                "index": voce["index"],
                "total": voce["total"],
                "epc": "",
                "tid": "",
                "stato": "da_scrivere",
                # Ogni contenitore porta nel chip **il proprio** reperto: due
                # vasetti dello stesso paziente possono essere colon e
                # linfonodo, e scriverli uguali sarebbe un errore che a
                # destinazione nessuno puo' piu' correggere.
                "payload": TagPayload(
                    codice_fiscale=paziente.codice_fiscale,
                    accession_id=accession_id,
                    container_index=voce["index"],
                    container_total=voce["total"],
                    display_name=paziente.display_name,
                    data_prelievo=prelievo,
                    material_code=voce["reperto"]["material_code"],
                    fixative_code=voce["reperto"]["fixative_code"],
                    site_code=voce["reperto"]["site_code"],
                    flags=voce["reperto"]["flags"],
                ),
                "descrizione": voce["reperto"]["descrizione"],
            }
            for voce in pianificati
        ]
        self._salva_contesto()
        del patient_id
        risposta = self.stato_accettazione()
        risposta["paziente_gia_noto"] = self._altri_del_paziente(paziente.codice_fiscale)
        return risposta

    def _inserisci(
        self,
        paziente: Patient,
        dati: Mapping[str, Any],
        reperti: list[dict[str, Any]],
        riferimento: str,
    ) -> tuple[int, int, list[dict[str, Any]]]:
        patient_id = self.db.upsert_patient(paziente)
        accession_id = self.db.next_accession_id()
        case_id = self.db.create_case(
            Case(
                accession_id=accession_id,
                patient_id=patient_id,
                # La data del prelievo viene dall'etichetta del reparto quando
                # c'e': il campione puo' essere stato prelevato ieri.
                data_prelievo=_data_o_nulla(dati.get("data_prelievo")) or _oggi(),
                ora_prelievo=_ora_pulita(dati.get("ora_prelievo")),
                reparto=str(dati.get("reparto", "")).strip(),
                medico=str(dati.get("medico", "")).strip(),
                external_ref=riferimento,
            )
        )
        pianificati = self.db.plan_accession(
            case_id,
            [
                (
                    Specimen(
                        case_id=case_id,
                        descrizione=voce["descrizione"],
                        material_code=voce["material_code"],
                        fixative_code=voce["fixative_code"],
                        site_code=voce["site_code"],
                        flags=int(voce["flags"]),
                    ),
                    voce["contenitori"],
                )
                for voce in reperti
            ],
        )
        # Rimette accanto a ogni contenitore il reperto da cui viene: serve a
        # costruire il payload giusto, e la lista e' nello stesso ordine.
        per_specimen: dict[int, dict[str, Any]] = {}
        indice_reperto = 0
        for voce in pianificati:
            if voce["specimen_id"] not in per_specimen:
                per_specimen[voce["specimen_id"]] = reperti[indice_reperto]
                indice_reperto += 1
            voce["reperto"] = per_specimen[voce["specimen_id"]]
        return patient_id, accession_id, pianificati

    def _altri_del_paziente(self, codice_fiscale: str) -> dict[str, Any]:
        """Gli altri contenitori gia' registrati oggi per questo paziente.

        E' una nota, non un ostacolo: i campioni arrivano in ordine sparso, e
        che lo stesso paziente ne abbia quattro e' normale. Serve perche'
        l'operatore lo sappia adesso invece di scoprirlo alla distinta.
        """
        try:
            altri = self.db.other_containers_for_patient(
                codice_fiscale, exclude_accession=self.accession_id
            )
        except Exception:  # noqa: BLE001
            log.exception("Ricerca degli altri contenitori del paziente non riuscita")
            return {"contenitori": 0, "accettazioni": []}
        return {
            "contenitori": len(altri),
            "accettazioni": sorted({int(voce["accession_id"]) for voce in altri}),
            "scritti": sum(1 for voce in altri if voce["epc"]),
        }

    def residuo_da_spedire(self) -> int:
        """Quanti contenitori sono scritti e non ancora in una spedizione.

        E' il numero che deve restare sotto gli occhi per tutta la giornata:
        finire la sera con un campione scritto e mai partito e' il modo in cui
        si perde un campione senza che nessuno se ne accorga.
        """
        try:
            return len(self.db.ready_containers())
        except Exception:  # noqa: BLE001
            log.exception("Conteggio del residuo da spedire non riuscito")
            return 0

    def stato_accettazione(self) -> dict[str, Any]:
        """Cosa deve mostrare la schermata di accettazione adesso."""
        prossimo = next((voce for voce in self.pending if not voce["epc"]), None)
        return {
            "accession_id": self.accession_id,
            "specimen_id": self.specimen_id,
            "conteggio_confermato": self.conteggio_confermato,
            "contenitori": [
                {
                    "container_id": voce["container_id"],
                    "index": voce["index"],
                    "total": voce["total"],
                    "etichetta": f"{voce['index']}/{voce['total']}",
                    "stato": voce["stato"],
                    "epc": voce["epc"],
                    "tid": voce["tid"],
                    "descrizione": voce.get("descrizione", ""),
                }
                for voce in self.pending
            ],
            "scritti": sum(1 for voce in self.pending if voce["epc"]),
            "totale": len(self.pending),
            "reperti": len({
                voce.get("specimen_id")
                for voce in self.pending
                if voce.get("specimen_id") is not None
            }),
            "residuo_da_spedire": self.residuo_da_spedire(),
            "prossimo": None
            if prossimo is None
            else {
                "container_id": prossimo["container_id"],
                "index": prossimo["index"],
                "total": prossimo["total"],
                # La descrizione non sta nel chip (li' ci sono i codici), ma
                # deve stare sotto gli occhi: con quattro reperti dello stesso
                # paziente e' l'unica cosa che dice quale si ha in mano.
                "descrizione": prossimo.get("descrizione", ""),
                "campione": prossimo["payload"].describe(),
            },
        }

    def correggi_conteggio(self, nuovo_totale: int) -> dict[str, Any]:
        """Cambia il numero di contenitori finche' e' ancora possibile."""
        if self.specimen_id is None:
            raise WorkflowError("registrare prima un'accettazione")
        with self._scrittura:
            try:
                esito = self.db.adjust_container_count(self.specimen_id, int(nuovo_totale))
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc
            self._ricarica_pending()
        risposta = self.stato_accettazione()
        risposta["variazione"] = esito.describe()
        # I tag gia' scritti portano nel chip il vecchio totale: e' una
        # discrepanza da mostrare, non da nascondere.
        risposta["tag_con_totale_superato"] = esito.describe()["tag_con_totale_superato"]
        return risposta

    def _ricarica_pending(self) -> None:
        if self.accession_id is None:
            return
        attivi = self.db.active_containers_for_accession(self.accession_id)
        noti = {voce["container_id"]: voce for voce in self.pending}
        base = self.pending[0]["payload"] if self.pending else None
        aggiornati: list[dict[str, Any]] = []
        for record in attivi:
            voce = noti.get(record.container_id)
            if voce is None:
                if base is None:
                    continue
                # Del contenitore nuovo si prendono i codici **dal suo record**,
                # non da quello del primo: in un'accettazione con piu' reperti
                # sarebbero il campione sbagliato.
                voce = {
                    "container_id": record.container_id,
                    "specimen_id": self._reperto_di(record.container_id),
                    "index": record.index,
                    "total": record.total,
                    "epc": record.epc or "",
                    "tid": record.tid or "",
                    "stato": "scritto" if record.epc else "da_scrivere",
                    "payload": replace(
                        base,
                        container_index=record.index,
                        container_total=record.total,
                        material_code=int(record.material_code),
                        fixative_code=int(record.fixative_code),
                        site_code=int(record.site_code),
                        flags=SpecimenFlags(int(record.flags)),
                    ),
                    "descrizione": record.descrizione,
                }
            else:
                voce["index"] = record.index
                voce["total"] = record.total
                if not voce["epc"]:
                    voce["payload"] = replace(
                        voce["payload"],
                        container_index=record.index,
                        container_total=record.total,
                    )
            aggiornati.append(voce)
        self.pending = aggiornati

    def conferma_conteggio(self) -> dict[str, Any]:
        """L'operatore dichiara di aver contato i campioni.

        Si chiede alla **prima scrittura**, non alla registrazione: e' li' che il
        numero diventa irreversibile, ed e' li' che l'operatore ha i campioni
        davanti invece della tastiera.
        """
        self._require_operator("confermare il conteggio")
        if self.specimen_id is None:
            raise WorkflowError("registrare prima un'accettazione")
        self.conteggio_confermato = True
        self._salva_contesto()
        return {"conteggio_confermato": True}

    def sorveglia_piatto(self) -> dict[str, Any]:
        """Guarda se c'e' un contenitore sulla postazione di scrittura.

        E' quello che permette alla scena di reagire da sola quando l'operatore
        appoggia il contenitore, senza un pulsante per dire «l'ho messo». La
        lettura serve comunque: la guardia di scrittura pretende un inventory
        con un tag solo.
        """
        from lims.responses import inventory_epcs
        from rfid_silion.service import InventoryRequest

        antenne = tuple(self.lims_cfg.get("write_antennas") or (1,))
        risposta = self.backend.inventory(
            InventoryRequest(antennas=antenne, timeout_ms=400)
        )
        if not risposta.ok:
            return {
                "stato": "errore",
                "epcs": [],
                "messaggio": (risposta.error or {}).get("message", "lettura non riuscita"),
            }
        epcs = inventory_epcs(risposta)
        if not epcs:
            stato = "vuoto"
        elif len(epcs) == 1:
            stato = "pronto"
        else:
            stato = "troppi"
        return {"stato": stato, "epcs": epcs}

    def scrivi_prossimo(
        self,
        *,
        on_step: Callable[[str], None] | None = None,
        authorized_rewrite: bool = False,
    ) -> dict[str, Any]:
        """Scrive il prossimo contenitore in attesa."""
        self._require_operator("scrivere un tag")
        if not self.conteggio_confermato:
            raise WorkflowError(
                "prima di scrivere serve la conferma del numero di campioni"
            )
        voce = next((item for item in self.pending if not item["epc"]), None)
        if voce is None:
            raise WorkflowError("non ci sono contenitori in attesa di scrittura")

        tagio = self._tagio(scrittura=True)
        esito = tagio.provision(
            voce["payload"],
            container_id=voce["container_id"],
            on_step=on_step,
            authorized_rewrite=authorized_rewrite,
        )
        if esito.ok:
            voce["epc"] = esito.epc
            voce["tid"] = esito.tid
            voce["stato"] = "scritto"
        else:
            voce["stato"] = "errore"

        self._salva_contesto()

        risposta = self.stato_accettazione()
        risposta["scrittura"] = esito.to_dict()
        risposta["campione"] = voce["payload"].describe()
        return risposta

    def annulla_accettazione(self, motivo: str = "") -> dict[str, Any]:
        """Abbandona l'accettazione in corso.

        I contenitori non ancora scritti vengono annullati e la postazione torna
        al modulo. **Quelli gia' scritti restano**: il tag e' scritto una volta
        sola e quei contenitori esistono ormai nel mondo fisico, con
        un'etichetta addosso. Fingere di poterli cancellare qui sarebbe il modo
        piu' rapido per perdere un campione.
        """
        self._require_operator("annullare un'accettazione")
        if self.specimen_id is None:
            raise WorkflowError("non c'e' nessuna accettazione in corso")

        scritti = [voce for voce in self.pending if voce["epc"]]
        da_annullare = [voce for voce in self.pending if not voce["epc"]]
        ragione = str(motivo).strip() or "accettazione annullata dall'operatore"

        with self._scrittura:
            annullati = []
            for voce in da_annullare:
                try:
                    self.db.void_container(voce["container_id"], reason=ragione)
                    annullati.append(voce["container_id"])
                except Exception:  # noqa: BLE001
                    log.exception("Annullamento del contenitore %s", voce["container_id"])

        accettazione = self.accession_id
        self.specimen_id = None
        self.accession_id = None
        self.pending = []
        self.conteggio_confermato = False
        self.external_ref = ""
        self._salva_contesto()

        return {
            "accettazione": accettazione,
            "annullati": len(annullati),
            "gia_scritti": [
                {"etichetta": f"{voce['index']}/{voce['total']}", "epc": voce["epc"]}
                for voce in scritti
            ],
        }

    def nuova_accettazione(self) -> dict[str, Any]:
        """Libera il modulo solo quando non restano tag da scrivere."""
        if self.specimen_id is not None and any(not voce["epc"] for voce in self.pending):
            raise WorkflowError(
                "l'accettazione attiva ha ancora contenitori da scrivere: "
                "completarla o annullarla"
            )
        precedente = self.accession_id
        self.specimen_id = None
        self.accession_id = None
        self.external_ref = ""
        self.pending = []
        self.conteggio_confermato = False
        self._salva_contesto()
        return {"ok": True, "accettazione_precedente": precedente}

    def annulla_contenitore(self, container_id: int, motivo: str, *, tag_guasto: bool = False):
        """Annulla un contenitore rotto o con tag guasto, e ne crea il sostituto."""
        self._require_operator("annullare un contenitore")
        # Il TID va letto prima: l'annullamento chiude l'assegnazione del tag.
        with self._scrittura:
            tid = ""
            try:
                tid = self.db.get_container(int(container_id)).tid
            except Exception:  # noqa: BLE001
                log.debug("Contenitore %s senza TID registrato", container_id)
            try:
                sostituto = self.db.replace_container(int(container_id), reason=str(motivo))
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc
            if tag_guasto and tid:
                # Un chip che non ha risposto non torna in circolazione senza
                # una verifica: e' esattamente il caso che il registro deve
                # ricordare.
                self.db.set_tag_state(tid, TagState.QUARANTINE)
            self._ricarica_pending()
            self._salva_contesto()
        risposta = self.stato_accettazione()
        risposta["sostituto"] = sostituto
        return risposta

    # -- sigillo e spedizione -----------------------------------------------
    def coda_spedizione(self) -> dict[str, Any]:
        """Contenitori pronti, raggruppati per accettazione per la selezione."""
        contenitori = self.db.ready_containers()
        gruppi: dict[int, dict[str, Any]] = {}
        for record in contenitori:
            gruppo = gruppi.setdefault(
                record.accession_id,
                {
                    "accession_id": record.accession_id,
                    "paziente": record.display_name,
                    "codice_fiscale": record.codice_fiscale,
                    "contenitori": [],
                },
            )
            gruppo["contenitori"].append(
                {
                    "container_id": record.container_id,
                    "epc": record.epc,
                    "etichetta": record.label,
                    "materiale": record.material_code,
                }
            )
        return {
            "gruppi": list(gruppi.values()),
            "totale": len(contenitori),
        }

    def _destinazione_valida(self, destinazione: str) -> str:
        """Il destinatario deve venire dall'elenco configurato.

        Era un campo libero: due grafie diverse dello stesso ospedale rendono
        l'archivio inservibile proprio nella domanda per cui esiste — dove sono
        finiti i campioni.
        """
        nome = str(destinazione).strip()
        if not nome:
            raise WorkflowError("scegliere il laboratorio destinatario")
        configurati = _voci_attive(self.config.get("destinatari"))
        if configurati and not self.destinatario(nome):
            raise WorkflowError(
                f"destinatario sconosciuto: {nome}. "
                "Va aggiunto in Impostazioni prima di poter spedire."
            )
        return nome

    def prepara_spedizione(
        self, destinazione: str, container_ids: list[int] | tuple[int, ...]
    ) -> dict[str, Any]:
        self._require_operator("preparare una spedizione")
        nome = self._destinazione_valida(destinazione)

        selezionati = tuple(dict.fromkeys(int(value) for value in container_ids))
        if not selezionati:
            raise WorkflowError("selezionare almeno un contenitore da spedire")
        if self.shipment_id is not None:
            stato_attivo = self.db.shipment_row(self.shipment_id)["state"]
            # Una scatola gia' sigillata non impedisce di riempirne un'altra:
            # e' il caso normale quando i tag scritti sono piu' di quanti ne
            # entrano in un contenitore di trasporto. Resta raggiungibile da
            # `spedizioni_aperte` per la distinta e per la conferma di
            # partenza. Impedisce invece di procedere una scatola ancora
            # aperta: quella e' una scatola che si sta riempiendo adesso.
            if stato_attivo not in _SPEDIZIONI_DA_PARTE:
                raise WorkflowError(
                    "c'e' una spedizione ancora aperta: sigillarla o annullarla "
                    "prima di iniziarne un'altra"
                )

        with self._scrittura:
            try:
                shipment_id = self.db.create_shipment_with_containers(
                    Shipment(destinazione=nome, data=_oggi()), selezionati
                )
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc

        self.shipment_id = shipment_id
        self.sealing_record = None
        self._salva_contesto()
        return self.stato_spedizione()

    # -- riempimento della scatola -------------------------------------------
    def _nuova_sessione_riempimento(self) -> SessioneRiempimento:
        """Apre la sessione con dentro cio' che la scatola contiene gia'.

        Dopo un riavvio il contenuto e' nell'archivio ma la sessione no: senza
        questo passaggio i contenitori gia' dentro verrebbero riletti come
        «gia' in un'altra scatola» — che e' la loro — e l'operatore vedrebbe
        trenta anomalie invece di trenta campioni.
        """
        contenuto = (
            self.db.shipment_contents(self.shipment_id) if self.shipment_id else []
        )
        return SessioneRiempimento(
            self.backend,
            self.db,
            antenne=tuple(self.lims_cfg.get("read_antennas") or (1, 2)),
            politica=politica_da_config(self.lims_cfg),
            potenza_cdbm=potenza_da_config(self.lims_cfg),
            region=int(self.config.get("reader", {}).get("region", 0x08)),
            contenitori_della_scatola=[r.container_id for r in contenuto],
        )

    def avvia_riempimento(self, destinazione: str) -> dict[str, Any]:
        """Apre una scatola vuota e comincia a guardare cosa ci entra."""
        self._require_operator("riempire una scatola")
        nome = self._destinazione_valida(destinazione)
        if self.shipment_id is not None:
            attiva = self.db.shipment_row(self.shipment_id)["state"]
            if attiva not in _SPEDIZIONI_DA_PARTE:
                raise WorkflowError(
                    "c'e' una scatola ancora aperta: chiuderla o annullarla "
                    "prima di iniziarne un'altra"
                )
        with self._scrittura:
            try:
                shipment_id = self.db.create_shipment(
                    Shipment(destinazione=nome, data=_oggi())
                )
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc
        self.shipment_id = shipment_id
        self.sealing_record = None
        self.riempimento = self._nuova_sessione_riempimento()
        self._salva_contesto()
        self.db.log_event(
            "fill_start",
            ok=True,
            operator=self.operatore,
            detail=f"scatola {shipment_id} verso {nome}",
        )
        risposta = self.stato_spedizione()
        risposta["riempimento"] = self.riempimento.riepilogo()
        return risposta

    def sorveglia_scatola(self) -> dict[str, Any]:
        """Un giro di letture sulla scatola aperta.

        E' il cuore del riempimento: l'operatore infila un campione e la
        postazione se ne accorge da sola, senza nessun pulsante che dica
        «l'ho messo». Ogni contenitore riconosciuto entra subito nella
        spedizione, cosi' l'elenco e la scatola non possono divergere.
        """
        if self.shipment_id is None:
            raise WorkflowError("aprire prima una scatola")
        if self.riempimento is None:
            # Dopo un riavvio: si riparte da cio' che la scatola contiene.
            self.riempimento = self._nuova_sessione_riempimento()

        esito = self.riempimento.passa()
        for evento in esito["eventi"]:
            if evento["esito"] != Esito.ENTRATO.value:
                continue
            container_id = evento.get("container_id")
            if container_id is None:
                continue
            if int(container_id) in self.riempimento.nostri:
                # E' gia' in questa scatola: succede dopo un riavvio, quando la
                # sessione riparte da zero ma il contenuto no. Riaggiungerlo
                # fallirebbe (e' gia' `packed`) e lo farebbe passare per
                # intruso, che e' il contrario della verita'.
                continue
            with self._scrittura:
                try:
                    self.db.add_to_shipment(self.shipment_id, [container_id])
                    self.riempimento.nostri.add(int(container_id))
                except Exception as exc:  # noqa: BLE001
                    # L'archivio sa cose che la sola lettura non poteva sapere.
                    # Il campione resta nel campo ma smette di contare, e il
                    # motivo lo legge l'operatore.
                    log.warning("Contenitore %s non aggiunto: %s", container_id, exc)
                    evento["esito"] = Esito.ALTRA_SPEDIZIONE.value
                    evento["anomalia"] = True
                    evento["dettaglio"] = str(exc)
                    self.riempimento.declassa(
                        evento["epc"], Esito.ALTRA_SPEDIZIONE, str(exc)
                    )

        risposta = self.riempimento.riepilogo()
        risposta["eventi"] = esito["eventi"]
        risposta["errore"] = esito.get("errore", "")
        risposta["shipment_id"] = self.shipment_id
        risposta["residuo_da_spedire"] = self.residuo_da_spedire()
        return risposta

    def togli_dalla_scatola(self, epc: str) -> dict[str, Any]:
        """L'operatore corregge: quel campione non era nella scatola.

        Succede quando la lettura arriva a un contenitore appoggiato accanto.
        Va tolto dalla spedizione **e** dimenticato dalla sessione, altrimenti
        al giro dopo e' ancora nel campo e rientrerebbe da solo.
        """
        self._require_operator("correggere il contenuto di una scatola")
        if self.shipment_id is None or self.riempimento is None:
            raise WorkflowError("non c'e' nessuna scatola aperta")
        cercato = str(epc).strip().upper()
        record = self.db.find_container_by_epc(cercato)
        if record is None:
            raise WorkflowError(f"nessun contenitore con EPC {cercato}")
        with self._scrittura:
            try:
                self.db.remove_from_shipment(self.shipment_id, [record.container_id])
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc
            self.riempimento.nostri.discard(record.container_id)
        self.riempimento.escludi(cercato)
        self.db.log_event(
            "fill_remove",
            ok=True,
            epc=cercato,
            container_id=record.container_id,
            operator=self.operatore,
            detail=f"tolto dalla scatola {self.shipment_id}",
        )
        risposta = self.riempimento.riepilogo()
        risposta["shipment_id"] = self.shipment_id
        risposta["residuo_da_spedire"] = self.residuo_da_spedire()
        return risposta

    def chiudi_riempimento(self) -> dict[str, Any]:
        """Basta cosi': la scatola e' composta, si passa al sigillo."""
        if self.shipment_id is None:
            raise WorkflowError("non c'e' nessuna scatola aperta")
        contenuto = self.db.shipment_contents(self.shipment_id)
        if not contenuto:
            raise WorkflowError(
                "la scatola e' vuota: appoggiare i campioni sulle antenne di lettura"
            )
        quanti = len(contenuto)
        self.riempimento = None
        self.db.log_event(
            "fill_end",
            ok=True,
            operator=self.operatore,
            detail=f"scatola {self.shipment_id}: {quanti} contenitori",
        )
        return self.stato_spedizione()

    def stato_spedizione(self) -> dict[str, Any]:
        if self.shipment_id is None:
            return {
                "shipment_id": None,
                "contenitori": [],
                "sigillo": None,
                "residuo_da_spedire": self.residuo_da_spedire(),
            }
        contenuto = self.db.shipment_contents(self.shipment_id)
        riga = self.db.shipment_row(self.shipment_id)
        nome_destinatario = riga["destinazione"]
        consegna = self.db.outbound_manifest(self.shipment_id)
        sigillo = self.sealing_record.to_dict() if self.sealing_record else None
        if sigillo is None and riga.get("sealing_json"):
            try:
                salvato = json.loads(riga["sealing_json"])
                if isinstance(salvato, dict) and salvato:
                    sigillo = salvato
            except (TypeError, ValueError):
                log.warning(
                    "prova di sigillatura non leggibile per la spedizione %s",
                    self.shipment_id,
                )
        if sigillo is None and riga.get("sealing_ok") is not None:
            sigillo = {
                "ok": bool(riga["sealing_ok"]),
                "dettaglio": riga.get("sealing_detail", ""),
                "ripristinato": True,
            }
        return {
            "shipment_id": self.shipment_id,
            "stato": riga["state"],
            "residuo_da_spedire": self.residuo_da_spedire(),
            "destinazione": nome_destinatario,
            "destinatario": self.destinatario(nome_destinatario),
            "contenitori": [
                {
                    "container_id": record.container_id,
                    "epc": record.epc,
                    "etichetta": record.label,
                    "paziente": record.display_name,
                    "codice_fiscale": record.codice_fiscale,
                    "accettazione": record.accession_id,
                    "materiale": record.material_code,
                }
                for record in contenuto
            ],
            "attesi": len([r for r in contenuto if r.epc]),
            "sigillo": sigillo,
            "esportata": riga.get("exported_at"),
            "inviata": riga.get("sent_at"),
            "consegna_pec": None
            if not consegna
            else {
                "manifest_uuid": consegna["manifest_uuid"],
                "sha256": consegna["manifest_hash"],
                "filename": consegna["filename"],
                "stato": consegna["state"],
                "message_id": consegna["message_id"],
                "smtp_accettata": consegna["smtp_accepted_at"],
                "pec_accettata": consegna["pec_accepted_at"],
                "consegnata": consegna["delivered_at"],
                "tentativi": consegna["attempts"],
                "errore": consegna["last_error"],
                "firmata": bool(consegna["signer_fingerprint"]),
            },
            "deroga_partenza": None
            if not riga.get("departure_override_at")
            else {
                "quando": riga["departure_override_at"],
                "chi": riga["departure_override_by"],
                "motivo": riga["departure_override_reason"],
            },
        }

    def spedizioni_aperte(self, limite: int = 20) -> dict[str, Any]:
        """Le scatole che non hanno ancora finito il loro giro.

        Serve da quando si puo' iniziare una scatola nuova mentre la
        precedente e' sigillata: senza questo elenco quella precedente
        sparirebbe dall'interfaccia con la distinta ancora da mandare.
        """
        righe = self.db.connection.execute(
            """
            SELECT s.id, s.destinazione, s.data, s.state, s.sealed_at, s.exported_at,
                   s.sent_at, s.sealing_ok, COUNT(i.container_id) AS pezzi
              FROM shipments s
              LEFT JOIN shipment_items i ON i.shipment_id = s.id
             WHERE s.state NOT IN (?, ?)
             GROUP BY s.id
             ORDER BY s.id DESC
             LIMIT ?
            """,
            (ShipmentState.SENT.value, ShipmentState.CANCELLED.value, int(limite)),
        ).fetchall()
        return {
            "attiva": self.shipment_id,
            "spedizioni": [
                {
                    "shipment_id": int(riga["id"]),
                    "destinazione": riga["destinazione"],
                    "data": riga["data"],
                    "stato": riga["state"],
                    "pezzi": int(riga["pezzi"] or 0),
                    "sigillata": riga["sealed_at"],
                    "esportata": riga["exported_at"],
                    "sigillo_ok": None if riga["sealing_ok"] is None else bool(riga["sealing_ok"]),
                    # Cosa manca perche' quella scatola sia finita davvero.
                    "da_fare": _cosa_manca(riga),
                }
                for riga in righe
            ],
        }

    def riapri_spedizione(self, shipment_id: int) -> dict[str, Any]:
        """Torna su una scatola precedente per mandarne la distinta.

        Non la «riapre» nel senso di poterci rimettere le mani dentro: lo stato
        resta quello che era. Rende soltanto di nuovo corrente la spedizione
        nell'interfaccia, che e' l'unico modo per esportarne la distinta dopo
        aver iniziato la scatola successiva.
        """
        riga = self.db.shipment_row(int(shipment_id))
        if riga["state"] in (ShipmentState.CANCELLED.value,):
            raise WorkflowError("quella spedizione e' stata annullata")
        self.shipment_id = int(shipment_id)
        self.sealing_record = None
        self._salva_contesto()
        return self.stato_spedizione()

    def annulla_spedizione(self) -> dict[str, Any]:
        self._require_operator("annullare una spedizione")
        if self.shipment_id is None:
            raise WorkflowError("non c'e' una spedizione attiva")
        try:
            self.db.cancel_shipment(self.shipment_id)
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(str(exc)) from exc
        annullata = self.shipment_id
        self.shipment_id = None
        self.sealing_record = None
        self._salva_contesto()
        return {"ok": True, "spedizione_annullata": annullata}

    def sigilla(
        self,
        *,
        on_progress: Callable[[int, int, int], None] | None = None,
        stop_event: Any = None,
    ) -> dict[str, Any]:
        """Certifica il contenuto della scatola chiusa."""
        self._require_operator("sigillare una spedizione")
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        contenuto = self.db.shipment_contents(self.shipment_id)
        attesi = [record.epc for record in contenuto if record.epc]
        if not attesi:
            raise WorkflowError("la spedizione non contiene contenitori con EPC assegnato")

        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))
        potenze = tuple(self.lims_cfg.get("seal_powers_cdbm") or (2000, 2500, 2900))
        gen2_precedente = self._snapshot_gen2()
        sessione = SealingSession(
            self.backend,
            attesi,
            passes=default_passes(antenne, powers_cdbm=potenze),
            policy=SealingPolicy(
                min_antennas=int(self.lims_cfg.get("seal_min_antennas", 1)),
                stable_passes=2,
            ),
            region=self.config.get("reader", {}).get("region", 0x08),
            operator=self.operatore,
            db=self.db,
        )
        try:
            record = sessione.run(
                closure_proof=ClosureProof.OPERATOR,
                on_progress=on_progress,
                stop_event=stop_event,
            )
        finally:
            # Le passate del sigillo cambiano potenze e parametri radio. La
            # postazione deve tornare esattamente alla configurazione operativa.
            self._restore_radio(gen2_precedente)
        self.sealing_record = record
        trovati, previsti = record.counts
        # L'esito resta attaccato alla spedizione, non solo al registro eventi:
        # e' la riga che risponde da sola se qualcuno chiede conto del carico.
        self.db.record_shipment_sealing(
            self.shipment_id,
            ok=record.ok,
            detail=(
                f"{trovati}/{previsti} in {record.passes_run} passate; "
                f"mancanti {len(record.missing)}; "
                # In italiano: questa riga la legge un operatore, non un
                # programma, e spesso mesi dopo.
                f"chiusura: {_PROVE_CHIUSURA.get(record.closure_proof.value, 'non dichiarata')}"
            ),
            operator=self.operatore,
            record=record.to_dict(),
        )
        if record.ok:
            self.db.set_shipment_state(self.shipment_id, ShipmentState.SEALED)

        risposta = self.stato_spedizione()
        risposta["sigillo"] = record.to_dict()
        # Il frontend deve poter mostrare i mancanti con nome e paziente: un EPC
        # da solo non dice niente a nessuno.
        per_epc = {r.epc: r for r in contenuto if r.epc}
        risposta["mancanti_descritti"] = [
            {
                "epc": epc,
                "etichetta": per_epc[epc].label if epc in per_epc else "—",
                "paziente": per_epc[epc].display_name if epc in per_epc else "",
            }
            for epc in record.missing
        ]
        return risposta

    def _verifica_completezza_distinta(
        self, contenuto: list[Any], sigillo: Mapping[str, Any]
    ) -> None:
        epc = [str(record.epc).strip().upper() for record in contenuto]
        tid = [str(record.tid).strip().upper() for record in contenuto]
        if not contenuto or any(not valore for valore in epc):
            raise WorkflowError("ogni contenitore della spedizione deve avere un EPC")
        if any(not valore for valore in tid):
            raise WorkflowError("ogni contenitore della spedizione deve avere un TID")
        if len(set(epc)) != len(epc) or len(set(tid)) != len(tid):
            raise WorkflowError("EPC e TID della spedizione devono essere univoci")
        attesi = {str(v).strip().upper() for v in sigillo.get("expected", [])}
        trovati = {str(v).strip().upper() for v in sigillo.get("found", [])}
        if not sigillo.get("ok") or set(epc) != attesi or attesi != trovati:
            raise WorkflowError(
                "la distinta non coincide esattamente con il contenuto certificato"
            )
        if sigillo.get("missing") or sigillo.get("unexpected"):
            raise WorkflowError("il sigillo contiene campioni mancanti o inattesi")

    @staticmethod
    def _nome_sicuro(valore: str, fallback: str) -> str:
        pulito = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(valore).strip()).strip("-.")
        return pulito or fallback

    def _garantisci_distinta_archiviata(self, *, require_v2: bool = False) -> dict[str, Any]:
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        esistente = self.db.outbound_manifest(self.shipment_id)
        if esistente:
            if require_v2 and not esistente.get("signer_fingerprint"):
                raise WorkflowError(
                    "la distinta archiviata e' nel formato legacy: annullare la spedizione "
                    "e crearne una nuova per l'invio PEC sicuro"
                )
            return esistente

        stato = self.stato_spedizione()
        sigillo = stato.get("sigillo")
        if not isinstance(sigillo, Mapping):
            raise WorkflowError("eseguire prima un sigillo valido")
        contenuto = self.db.shipment_contents(self.shipment_id)
        self._verifica_completezza_distinta(contenuto, sigillo)
        destinatario = stato.get("destinatario") or {}
        laboratorio = self.laboratorio()
        source_code = self._nome_sicuro(
            str(laboratorio.get("codice", "")), f"LAB-{self.lims_cfg.get('lab_id', 0)}"
        )
        destination_code = self._nome_sicuro(
            str(destinatario.get("codice", "")), stato.get("destinazione", "DEST")
        )
        identificativo = str(uuid.uuid4())
        box_epc = str(sigillo.get("box_epc", ""))
        distinta = build_manifest(
            self.db,
            self.shipment_id,
            lab_id=int(self.lims_cfg.get("lab_id", 0)),
            operator=self.operatore,
            sealing=sigillo,
            box_epc=box_epc,
        )
        distinta.manifest_uuid = identificativo
        distinta.source_code = source_code
        distinta.destination_code = destination_code
        firmatario = ""
        destinatario_fp = ""
        usa_v2 = bool(destinatario.get("encryption_certificate")) or bool(
            (self.config.get("pec", {}) or {}).get("enabled", False)
        )
        try:
            if usa_v2:
                if not str(laboratorio.get("codice", "")).strip():
                    raise WorkflowError(
                        "configurare il codice univoco del laboratorio mittente"
                    )
                if not str(destinatario.get("codice", "")).strip():
                    raise WorkflowError(
                        "configurare il codice univoco del laboratorio destinatario"
                    )
                materiale = self._materiale_mittente(destinatario)
                distinta.schema = MANIFEST_SCHEMA_V2
                blob = seal_secure_manifest(
                    distinta,
                    manifest_uuid=identificativo,
                    source_code=source_code,
                    destination_code=destination_code,
                    recipient_certificate=materiale["recipient_certificate"],
                    signing_key=materiale["station_private_key"],
                    signer_certificate=materiale["station_certificate"],
                )
                firmatario = certificate_fingerprint(materiale["station_certificate"])
                destinatario_fp = certificate_fingerprint(
                    materiale["recipient_certificate"]
                )
            else:
                if require_v2:
                    raise WorkflowError(
                        "il destinatario non ha un certificato pubblico di cifratura"
                    )
                blob = seal_manifest(distinta, self.keyring)
        except WorkflowError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(f"creazione della distinta sicura fallita: {exc}") from exc

        impronta = hashlib.sha256(blob).hexdigest()
        nome = (
            f"distinta_{source_code}_{self.shipment_id}_{identificativo}.rfidman"
        )
        archivio = self._percorso(
            self.lims_cfg.get("manifest_archive", "logs/archivio_distinte")
        ) / f"{_oggi():%Y}" / f"{_oggi():%m}"
        percorso = archivio / nome
        try:
            archivio.mkdir(parents=True, exist_ok=True)
            temporaneo = percorso.with_suffix(percorso.suffix + ".tmp")
            temporaneo.write_bytes(blob)
            temporaneo.replace(percorso)
            record = self.db.archive_outbound_manifest(
                self.shipment_id,
                manifest_uuid=identificativo,
                encrypted_blob=blob,
                manifest_hash=impronta,
                filename=nome,
                item_count=len(contenuto),
                source_code=source_code,
                destination_code=destination_code,
                signer_fingerprint=firmatario,
                recipient_fingerprint=destinatario_fp,
                archive_path=str(percorso),
                operator=self.operatore,
            )
            self.db.mark_manifest_exported(self.shipment_id, impronta)
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(f"archiviazione della distinta fallita: {exc}") from exc
        return record

    def distinta_stampabile(self) -> dict[str, Any]:
        """La distinta come va sul foglio: tabella leggibile e QR.

        E' il documento che accompagna la scatola. Contiene tutto perche' i due
        laboratori non condividono nessun archivio: quello che non e' scritto
        qui, a destinazione non esiste. Il QR porta esattamente le stesse righe,
        cosi' chi riceve non deve ridigitarne trenta.

        Non pretende una distinta cifrata gia' archiviata: il foglio si stampa
        appena la scatola e' certificata, che e' il momento in cui l'operatore
        ce l'ha davanti.
        """
        if self.shipment_id is None:
            raise WorkflowError("non c'e' nessuna spedizione da stampare")
        stato = self.stato_spedizione()
        contenuto = self.db.shipment_contents(self.shipment_id)
        if not contenuto:
            raise WorkflowError("la spedizione non contiene nessun contenitore")

        righe = righe_da_contenuto(contenuto)
        sigillo = stato.get("sigillo") or {}
        archiviata = self.db.outbound_manifest(self.shipment_id)
        identificativo = str(
            (archiviata or {}).get("manifest_uuid", "") or f"{self.shipment_id:08X}"
        )

        try:
            parti = codifica_tabella(
                righe,
                identificativo=identificativo.replace("-", ""),
                chiave=self._chiave_qr(),
            )
        except TabellaError as exc:
            raise WorkflowError(str(exc)) from exc

        # Il QR si stringe se le parti sono piu' di una: due codici affiancati
        # su un A4 devono starci senza rimpicciolire il testo della tabella.
        lato_mm = 45.0 if len(parti) == 1 else 38.0
        codici = []
        for numero, parte in enumerate(parti, start=1):
            simbolo = codifica_qr(parte, Correzione.M)
            codici.append(
                {
                    "parte": numero,
                    "parti": len(parti),
                    "versione": simbolo.versione,
                    "moduli": simbolo.lato,
                    "testo": parte,
                    "svg": qr_svg(
                        simbolo,
                        lato_mm=lato_mm,
                        titolo=f"Distinta {identificativo}, codice {numero} di {len(parti)}",
                    ),
                }
            )

        destinatario = stato.get("destinatario") or {}
        laboratorio = self.laboratorio()
        return {
            "identificativo": identificativo,
            "shipment_id": self.shipment_id,
            "stampata_il": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
            "mittente": laboratorio,
            "destinatario": {
                "nome": stato.get("destinazione", ""),
                "codice": destinatario.get("codice", ""),
                "indirizzo": destinatario.get("indirizzo", ""),
                "citta": destinatario.get("citta", ""),
                "referente": destinatario.get("referente", ""),
            },
            "operatore": self.operatore,
            "colonne": [{"chiave": chiave, "titolo": titolo} for chiave, titolo in COLONNE],
            "righe": per_stampa(righe),
            "totale": len(righe),
            "sigillo": {
                "ok": bool(sigillo.get("ok")),
                "trovati": sigillo.get("trovati", 0),
                "attesi": sigillo.get("attesi", 0),
                "passate": sigillo.get("passes_run", 0),
                "quando": sigillo.get("finished_at", ""),
                "scatola": sigillo.get("box_epc", ""),
                "prova_chiusura": _PROVE_CHIUSURA.get(
                    str(sigillo.get("closure_proof", "")), "non dichiarata"
                ),
            },
            "codici": codici,
        }

    def _chiave_qr(self) -> bytes | None:
        """La chiave con cui si firma il QR: quella del circuito.

        Se il portachiavi non ha ancora nulla il QR viaggia senza firma, e chi
        lo rilegge lo scopre: meglio una distinta non firmata che nessuna
        distinta, purche' non si spacci per firmata.
        """
        try:
            return self.keyring.default_key
        except Exception:  # noqa: BLE001
            log.warning("Portachiavi senza chiave: il QR della distinta non sara' firmato")
            return None

    def leggi_qr_distinta(self, scansioni: Any) -> dict[str, Any]:
        """Ricostruisce la distinta dalle scansioni del QR, in ricezione.

        E' la strada che non passa da nessun file: il pacco arriva col foglio
        attaccato, si legge il codice e la lista dei campioni attesi c'e'.
        """
        self._require_operator("leggere una distinta")
        if isinstance(scansioni, str):
            scansioni = [scansioni]
        if not isinstance(scansioni, (list, tuple)) or not scansioni:
            raise WorkflowError("nessun codice letto")
        try:
            letta = decodifica_tabella(
                [str(voce) for voce in scansioni], chiave=self._chiave_qr()
            )
        except TabellaError as exc:
            raise WorkflowError(str(exc)) from exc

        self.distinta_qr = letta
        risposta = letta.describe()
        risposta["righe"] = per_stampa(letta.righe)
        return risposta

    def esporta_distinta(self) -> tuple[bytes, str]:
        """Restituisce sempre gli stessi byte della distinta archiviata."""
        self._require_operator("esportare la distinta")
        record = self._garantisci_distinta_archiviata()
        return bytes(record["encrypted_blob"]), str(record["filename"])

    def _pec_transport(self) -> PecTransport:
        try:
            return PecTransport(PecConfig.from_mapping(self.config.get("pec", {}) or {}))
        except PecError as exc:
            raise WorkflowError(str(exc)) from exc

    def invia_distinta_pec(self) -> dict[str, Any]:
        """Archivia e invia la medesima distinta dalla PEC dell'unita' locale."""
        self._require_operator("inviare la distinta via PEC")
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        record = self._garantisci_distinta_archiviata(require_v2=True)
        if record["state"] in {
            "smtp_accepted",
            "pec_accepted",
            "delivered",
            "delivery_unknown",
        }:
            raise WorkflowError(
                "la distinta risulta gia' affidata al gestore PEC; aggiornare le ricevute"
            )
        destinatario = self.destinatario(self.db.shipment_row(self.shipment_id)["destinazione"])
        pec_destinatario = str((destinatario or {}).get("pec") or "").strip()
        try:
            esito = self._pec_transport().send_manifest(
                recipient=pec_destinatario,
                filename=record["filename"],
                blob=bytes(record["encrypted_blob"]),
                manifest_uuid=record["manifest_uuid"],
                shipment_id=self.shipment_id,
                item_count=int(record["item_count"]),
                sha256=record["manifest_hash"],
                source_code=record["source_code"],
                destination_code=record["destination_code"],
            )
        except PecError as exc:
            incerto = "incerto" in str(exc).lower()
            self.db.mark_outbound_error(self.shipment_id, str(exc), uncertain=incerto)
            raise WorkflowError(str(exc)) from exc
        self.db.mark_outbound_smtp_accepted(
            self.shipment_id, esito.message_id, esito.raw_message
        )
        return self.stato_spedizione()

    def aggiorna_ricevute_pec(self) -> dict[str, Any]:
        self._require_operator("aggiornare le ricevute PEC")
        if self.shipment_id is None:
            raise WorkflowError("non c'e' una spedizione attiva")
        record = self.db.outbound_manifest(self.shipment_id)
        if not record or not record.get("message_id"):
            raise WorkflowError("la distinta non e' ancora stata inviata via PEC")
        nuove = 0
        try:
            ricevute = self._pec_transport().fetch_receipts()
        except PecError as exc:
            raise WorkflowError(str(exc)) from exc
        for ricevuta in ricevute:
            collegata = self.db.find_outbound_by_message_id(ricevuta.message_id)
            if collegata and collegata["id"] == record["id"]:
                nuove += int(
                    self.db.record_pec_receipt(
                        record["id"],
                        receipt_type=ricevuta.receipt_type,
                        message_id=ricevuta.message_id,
                        raw_eml=ricevuta.raw_eml,
                        daticert_xml=ricevuta.daticert_xml,
                    )
                )
        risposta = self.stato_spedizione()
        risposta["nuove_ricevute"] = nuove
        return risposta

    def _autorizza_deroga(self, motivo: str, pin: str) -> None:
        configurato = next(
            (
                voce
                for voce in _voci_attive(self.config.get("operatori"))
                if _etichetta_operatore(voce) == self.operatore
            ),
            None,
        )
        if not configurato or str(configurato.get("ruolo", "")).lower() != "responsabile":
            raise WorkflowError("solo un responsabile puo' autorizzare la deroga PEC")
        if not str(motivo).strip():
            raise WorkflowError("indicare il motivo della partenza senza consegna PEC")
        servizio = str(configurato.get("pin_service", "RFID-LIMS-SUPERVISORI"))
        utente = str(configurato.get("pin_username", self.operatore))
        try:
            atteso = resolve_secret(servizio, utente)
        except PecError as exc:
            raise WorkflowError(str(exc)) from exc
        if not hmac.compare_digest(str(pin), atteso):
            raise WorkflowError("PIN del responsabile non valido")
        self.db.record_departure_override(
            self.shipment_id, operator=self.operatore, reason=motivo
        )

    def conferma_invio(self, *, motivo_deroga: str = "", pin: str = "") -> dict[str, Any]:
        """Registra la partenza dopo consegna PEC o deroga responsabile."""
        self._require_operator("confermare l'invio")
        if self.shipment_id is None:
            raise WorkflowError("non c'e' una spedizione attiva")
        pec_attiva = bool((self.config.get("pec", {}) or {}).get("enabled", False))
        if pec_attiva:
            distinta = self.db.outbound_manifest(self.shipment_id)
            consegnata = bool(distinta and distinta.get("state") == "delivered")
            if not consegnata:
                self._autorizza_deroga(motivo_deroga, pin)
        try:
            self.db.confirm_shipment_sent(self.shipment_id)
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(str(exc)) from exc
        self._salva_contesto()
        return self.stato_spedizione()

    def bozza_email(self) -> dict[str, Any]:
        """Testo e destinatari del messaggio con cui si manda la distinta.

        **L'allegato va messo a mano.** Una pagina web non puo' allegare un file
        a un messaggio del programma di posta: puo' solo preparare indirizzo,
        oggetto e testo. Dirlo qui, invece di lasciar credere che sia partito
        tutto, e' l'unico modo perche' nessuno spedisca una mail vuota.
        """
        self._require_operator("preparare l'email della spedizione")
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        if self.db.shipment_row(self.shipment_id)["state"] not in (
            ShipmentState.EXPORTED.value,
            ShipmentState.SENT.value,
        ):
            raise WorkflowError("esportare prima la distinta cifrata")
        stato = self.stato_spedizione()
        destinatario = stato.get("destinatario") or {}
        laboratorio = self.laboratorio()
        contenitori = stato.get("attesi", 0)
        nome_file = f"distinta_{self.shipment_id}_{_oggi():%Y%m%d}.rfidman"

        righe = [
            f"Spedizione {self.shipment_id} del {_oggi():%d/%m/%Y}"
            f" da {laboratorio['nome'] or 'laboratorio mittente'}.",
            "",
            f"Contenitori inclusi: {contenitori}.",
            "",
            "In allegato la distinta cifrata (file "
            f"{nome_file}), da aprire con l'interfaccia di tracciabilita'"
            " nella schermata Ricezione.",
            "",
            "I dati dei pazienti non sono in questo messaggio: stanno nella"
            " distinta cifrata e nei tag, e si leggono solo con la chiave"
            " concordata.",
        ]
        if laboratorio.get("referente"):
            righe += ["", f"Referente: {laboratorio['referente']}"]
        if laboratorio.get("telefono"):
            righe += [f"Telefono: {laboratorio['telefono']}"]

        return {
            "a": destinatario.get("email", ""),
            "cc": laboratorio.get("email_referente", ""),
            "oggetto": (
                f"Distinta spedizione {self.shipment_id}"
                f" — {contenitori} contenitori"
                + (f" — {laboratorio['nome']}" if laboratorio.get("nome") else "")
            ),
            "corpo": "\n".join(righe),
            "nome_file": nome_file,
            "destinatario": destinatario.get("nome", stato.get("destinazione", "")),
            "allegato_manuale": True,
        }

    # -- ricezione -----------------------------------------------------------
    def importa_distinta(self, blob: bytes) -> dict[str, Any]:
        """Carica la distinta del mittente.

        I tre errori restano distinti perche' all'operatore servono tre azioni
        diverse: chiedere la chiave, sospettare un clone, oppure accorgersi che
        ha aperto il file sbagliato.
        """
        self._require_operator("importare una distinta")
        verifica = {
            "verification_ok": None,
            "manifest_uuid": "",
            "signer_code": "",
            "signer_fingerprint": "",
            "recipient_fingerprint": "",
            "verification_detail": "distinta legacy con chiave condivisa",
        }
        try:
            schema = (
                blob[len(MANIFEST_MAGIC)]
                if bytes(blob).startswith(MANIFEST_MAGIC)
                and len(blob) > len(MANIFEST_MAGIC)
                else 0
            )
            if schema == MANIFEST_SCHEMA_V2:
                materiale = self._materiale_destinatario()
                laboratorio = self.laboratorio()
                if not str(laboratorio.get("codice", "")).strip():
                    raise WorkflowError(
                        "configurare il codice univoco di questo laboratorio"
                    )
                aperta = open_secure_manifest(
                    bytes(blob),
                    recipient_private_key=materiale["private_key"],
                    recipient_certificate=materiale["certificate"],
                    trusted_cas=materiale["trusted_cas"],
                    revoked_serials=materiale["revoked_serials"],
                    expected_destination_code=str(laboratorio.get("codice", "")),
                )
                distinta_aperta = aperta.manifest
                verifica = {
                    "verification_ok": True,
                    "manifest_uuid": aperta.header.manifest_uuid,
                    "signer_code": aperta.header.source_code,
                    "signer_fingerprint": aperta.header.signer_fingerprint,
                    "recipient_fingerprint": aperta.header.recipient_fingerprint,
                    "verification_detail": "firma Ed25519 e cifratura destinatario verificate",
                }
            else:
                distinta_aperta = open_manifest(bytes(blob), self.keyring)
        except UnknownKeyError as exc:
            raise WorkflowError(
                f"{exc} — la chiave va concordata con il laboratorio mittente"
            ) from exc
        except PayloadAuthenticationError as exc:
            raise WorkflowError(f"distinta non autentica: {exc}") from exc
        except PayloadFormatError as exc:
            raise WorkflowError(f"non e' una distinta di questo sistema: {exc}") from exc

        distinta = distinta_aperta
        if distinta.shipment_id is None:
            raise WorkflowError("la distinta non contiene il numero di spedizione")
        impronta = hashlib.sha256(bytes(blob)).hexdigest()
        if self.inbound_id is not None:
            attiva = self.db.inbound_row(self.inbound_id)
            if attiva["state"] != "received" and attiva["manifest_hash"] != impronta:
                raise WorkflowError(
                    "c'e' gia' una ricezione aperta: completarla prima di importarne un'altra"
                )
        try:
            self.inbound_id, gia_nota = self.db.import_inbound_manifest(
                origin_lab_id=distinta.lab_id,
                origin_shipment_id=int(distinta.shipment_id),
                manifest_hash=impronta,
                encrypted_blob=bytes(blob),
                destination=distinta.destination,
                source_created_at=distinta.created_at,
                operator=self.operatore,
                **verifica,
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(str(exc)) from exc
        self.distinta = distinta
        self._salva_contesto()
        risposta = self.stato_ricezione()
        risposta["gia_importata"] = gia_nota
        return risposta

    def _descrivi_distinta(self) -> dict[str, Any]:
        if self.distinta is None:
            return {"attesi": 0, "contenitori": []}
        distinta = self.distinta
        return {
            "attesi": len(distinta.entries),
            "destinazione": distinta.destination,
            "creata": distinta.created_at,
            "operatore": distinta.operator,
            "box_epc": distinta.box_epc,
            "sigillo_partenza": distinta.sealing,
            "contenitori": [
                {
                    "epc": voce.epc,
                    "etichetta": voce.label,
                    "paziente": voce.display_name,
                    "codice_fiscale": voce.codice_fiscale,
                    "accettazione": voce.accession_id,
                    "materiale": voce.material_code,
                }
                for voce in distinta.entries
            ],
        }

    def stato_ricezione(self) -> dict[str, Any]:
        if self.inbound_id is None or self.distinta is None:
            return {"inbound_id": None, "attesi": 0, "contenitori": []}
        riga = self.db.inbound_row(self.inbound_id)
        risposta = self._descrivi_distinta()
        risposta.update(
            {
                "inbound_id": self.inbound_id,
                "stato": riga["state"],
                "importata": riga["imported_at"],
                "confermata": riga["confirmed_at"],
                "motivo_non_conformita": riga["nonconformity_reason"],
                "verifica_documento": {
                    "ok": None
                    if riga.get("verification_ok") is None
                    else bool(riga["verification_ok"]),
                    "manifest_uuid": riga.get("manifest_uuid", ""),
                    "mittente": riga.get("signer_code", ""),
                    "firmatario": riga.get("signer_fingerprint", ""),
                    "destinatario": riga.get("recipient_fingerprint", ""),
                    "dettaglio": riga.get("verification_detail", ""),
                    "sha256": riga.get("manifest_hash", ""),
                },
                "riconciliazione": self.db.latest_inbound_reconciliation(
                    self.inbound_id
                ),
            }
        )
        return risposta

    def leggi_volume(self) -> dict[str, Any]:
        """Legge la scatola arrivata e la confronta con la distinta."""
        self._require_operator("controllare una ricezione")
        attesi = list(self.distinta.epcs) if self.distinta is not None else None
        rilievo = self._tagio(scrittura=False).survey_field(expected_epcs=attesi)
        risposta: dict[str, Any] = {"rilievo": rilievo.to_dict()}
        if self.distinta is not None:
            riconciliazione = reconcile(
                self.distinta, [osservazione.epc for osservazione in rilievo.observations]
            )
            risposta["riconciliazione"] = riconciliazione.to_dict()
            risposta["mancanti_descritti"] = [
                {
                    "epc": epc,
                    "etichetta": riconciliazione.details[epc].label,
                    "paziente": riconciliazione.details[epc].display_name,
                    "codice_fiscale": riconciliazione.details[epc].codice_fiscale,
                }
                for epc in riconciliazione.missing
                if epc in riconciliazione.details
            ]
            if self.inbound_id is not None:
                self.db.record_inbound_reconciliation(
                    self.inbound_id,
                    operator=self.operatore,
                    expected=riconciliazione.expected,
                    arrived=riconciliazione.arrived,
                    missing=riconciliazione.missing,
                    unexpected=riconciliazione.unexpected,
                )
        return risposta

    def conferma_ricezione(self, motivo_non_conformita: str = "") -> dict[str, Any]:
        self._require_operator("confermare una ricezione")
        if self.inbound_id is None:
            raise WorkflowError("importare prima la distinta")
        try:
            self.db.confirm_inbound_receipt(
                self.inbound_id,
                operator=self.operatore,
                nonconformity_reason=motivo_non_conformita,
            )
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(str(exc)) from exc
        self._salva_contesto()
        return self.stato_ricezione()

    # -- il verbale che chiude il giro ---------------------------------------
    def esporta_riscontro(self) -> tuple[bytes, str]:
        """Il verbale di come e' andata, da rimandare a chi ha spedito.

        Senza questo, il mittente sa solo di aver spedito: le ricevute PEC
        provano che il documento e' arrivato, non che le provette ci siano.
        """
        self._require_operator("esportare un verbale di riscontro")
        if self.inbound_id is None:
            raise WorkflowError("non c'e' nessuna ricezione da certificare")
        try:
            verbale = costruisci_riscontro(
                self.db,
                self.inbound_id,
                operatore=self.operatore,
                lab_id=int(self.lims_cfg.get("lab_id", 0)),
                destinazione=str(self.laboratorio().get("codice", ""))
                or str(self.laboratorio().get("nome", "")),
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc

        blob = sigilla_riscontro(verbale, self.keyring)
        nome = self._nome_sicuro(
            f"riscontro_{verbale.shipment_id}_{verbale.manifest_uuid or self.inbound_id}",
            f"riscontro_{self.inbound_id}",
        ) + ".rfidric"
        self.db.log_event(
            "arrival_export",
            ok=verbale.ok,
            operator=self.operatore,
            detail=(
                f"verbale per la spedizione {verbale.shipment_id}: "
                f"{len(verbale.arrivati)}/{len(verbale.attesi)} arrivati"
            ),
        )
        return blob, nome

    def stato_riscontro(self) -> dict[str, Any]:
        """Se questa ricezione ha gia' un verbale pronto da mandare."""
        if self.inbound_id is None:
            return {"disponibile": False}
        riga = self.db.inbound_row(self.inbound_id)
        riconciliazione = self.db.latest_inbound_reconciliation(self.inbound_id)
        return {
            "disponibile": riga["state"] == "received" and riconciliazione is not None,
            "stato": riga["state"],
            "confermata": riga["confirmed_at"],
            "mittente": riga.get("signer_code", ""),
        }

    def importa_riscontro(self, blob: bytes) -> dict[str, Any]:
        """Il mittente riceve il verbale e chiude la spedizione.

        E' l'unico momento in cui una spedizione smette di essere «partita» e
        diventa «arrivata». Prima di questo, dire che e' andata bene sarebbe
        una supposizione.
        """
        self._require_operator("importare un verbale di riscontro")
        try:
            verbale = apri_riscontro(bytes(blob), self.keyring)
        except UnknownKeyError as exc:
            raise WorkflowError(
                f"{exc} — la chiave va concordata con il laboratorio destinatario"
            ) from exc
        except PayloadAuthenticationError as exc:
            raise WorkflowError(f"verbale non autentico: {exc}") from exc
        except PayloadFormatError as exc:
            raise WorkflowError(f"non e' un verbale di questo sistema: {exc}") from exc

        if verbale.shipment_id is None:
            raise WorkflowError("il verbale non dice a quale spedizione si riferisce")
        try:
            riga = self.db.shipment_row(int(verbale.shipment_id))
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(
                f"la spedizione {verbale.shipment_id} non risulta in questo archivio: "
                "il verbale potrebbe essere di un altro laboratorio"
            ) from exc

        # Controllo di coerenza: gli EPC attesi nel verbale devono essere
        # quelli che sono partiti davvero. Un verbale che parla di altri
        # campioni e' un verbale di un'altra spedizione.
        nostri = {
            str(record.epc).upper()
            for record in self.db.shipment_contents(int(verbale.shipment_id))
            if record.epc
        }
        dichiarati = {str(epc).upper() for epc in verbale.attesi}
        if nostri and dichiarati and nostri != dichiarati:
            raise WorkflowError(
                "il verbale non corrisponde al contenuto di questa spedizione: "
                f"{len(nostri)} contenitori spediti, {len(dichiarati)} dichiarati"
            )

        with self._scrittura:
            self.db.record_arrival(
                int(verbale.shipment_id),
                manifest_uuid=verbale.manifest_uuid,
                remote_inbound_id=verbale.inbound_id,
                operator=verbale.operatore,
                arrived_at=verbale.letta_il,
                confirmed_at=verbale.confermata_il,
                expected=len(verbale.attesi),
                arrived=len(verbale.arrivati),
                missing=len(verbale.mancanti),
                unexpected=len(verbale.inattesi),
                ok=verbale.ok,
                nonconformity=verbale.non_conformita,
                detail=verbale.to_dict(),
            )
        self.db.log_event(
            "arrival_import",
            ok=verbale.ok,
            operator=self.operatore,
            detail=(
                f"spedizione {verbale.shipment_id}: "
                f"{len(verbale.arrivati)}/{len(verbale.attesi)} arrivati"
                + (f"; {verbale.non_conformita}" if verbale.non_conformita else "")
            ),
        )

        risposta = verbale.descrivi()
        risposta["spedizione"] = {
            "destinazione": riga["destinazione"],
            "partita": riga["sent_at"],
            "stato": self.db.shipment_row(int(verbale.shipment_id))["state"],
        }
        # I mancanti con nome e paziente: un EPC da solo non dice niente.
        per_epc = {
            str(record.epc).upper(): record
            for record in self.db.shipment_contents(int(verbale.shipment_id))
        }
        risposta["mancanti_descritti"] = [
            {
                "epc": epc,
                "etichetta": per_epc[epc].label if epc in per_epc else "—",
                "paziente": per_epc[epc].display_name if epc in per_epc else "",
            }
            for epc in verbale.mancanti
        ]
        return risposta

    # -- riepilogo del materiale transitato ----------------------------------
    def riepilogo_transito(
        self, destinazione: str = "", dal: str = "", al: str = ""
    ) -> dict[str, Any]:
        """Cosa e' passato fra i due centri, e come e' finita.

        «Tutto a buon fine» significa una cosa sola: ogni spedizione del
        periodo ha un verbale di riscontro che dice che e' arrivata intera. Il
        resto — spedito e mai confermato — si chiama col suo nome, perche' un
        riepilogo che da' per arrivato cio' che non sa e' peggio di nessun
        riepilogo.
        """
        righe = self.db.transit_summary(str(destinazione).strip(), dal=dal, al=al)
        spedizioni = []
        pazienti: dict[str, dict[str, Any]] = {}
        pezzi = arrivati = mancanti = 0
        confermate = 0

        for riga in righe:
            elenco = self.db.patients_in_shipment(int(riga["id"]))
            for voce in elenco:
                chiave = str(voce["codice_fiscale"])
                gia = pazienti.setdefault(
                    chiave,
                    {
                        "codice_fiscale": chiave,
                        "paziente": f"{voce['cognome']} {voce['nome']}",
                        "pezzi": 0,
                        "spedizioni": 0,
                    },
                )
                gia["pezzi"] += int(voce["pezzi"])
                gia["spedizioni"] += 1

            quanti = int(riga["pezzi"] or 0)
            pezzi += quanti
            arrivo_ok = riga["arrivo_ok"]
            if arrivo_ok is not None:
                confermate += 1
                arrivati += int(riga["arrivo_arrivati"] or 0)
                mancanti += int(riga["arrivo_mancanti"] or 0)

            spedizioni.append(
                {
                    "shipment_id": int(riga["id"]),
                    "destinazione": riga["destinazione"],
                    "data": riga["data"],
                    "stato": riga["state"],
                    "pezzi": quanti,
                    "pazienti": len(elenco),
                    "sigillata": riga["sealed_at"],
                    "partita": riga["sent_at"],
                    "sigillo_ok": None
                    if riga["sealing_ok"] is None
                    else bool(riga["sealing_ok"]),
                    "arrivo": None
                    if arrivo_ok is None
                    else {
                        "ok": bool(arrivo_ok),
                        "quando": riga["arrivo_il"],
                        "operatore": riga["arrivo_da"],
                        "attesi": int(riga["arrivo_attesi"] or 0),
                        "arrivati": int(riga["arrivo_arrivati"] or 0),
                        "mancanti": int(riga["arrivo_mancanti"] or 0),
                        "inattesi": int(riga["arrivo_inattesi"] or 0),
                        "non_conformita": riga["arrivo_non_conformita"] or "",
                    },
                    "esito": _esito_transito(riga),
                }
            )

        non_confermate = len(spedizioni) - confermate
        return {
            "controparte": str(destinazione).strip(),
            "dal": dal,
            "al": al,
            "generato_il": dt.datetime.now().astimezone().isoformat(timespec="minutes"),
            "laboratorio": self.laboratorio(),
            "operatore": self.operatore,
            "spedizioni": spedizioni,
            "pazienti": sorted(pazienti.values(), key=lambda v: v["paziente"]),
            "totali": {
                "spedizioni": len(spedizioni),
                "pezzi": pezzi,
                "pazienti": len(pazienti),
                "confermate": confermate,
                "non_confermate": non_confermate,
                "arrivati": arrivati,
                "mancanti": mancanti,
            },
            # La domanda che il riepilogo deve saper rispondere, e a cui puo'
            # rispondere «si'» solo se lo sa davvero.
            "tutto_a_buon_fine": bool(spedizioni)
            and non_confermate == 0
            and mancanti == 0
            and all(voce["esito"] == "arrivata" for voce in spedizioni),
        }

    def esporta_riepilogo(self, destinazione: str = "", dal: str = "", al: str = ""):
        """Il riepilogo come file CSV, da archiviare o da allegare."""
        import csv
        import io

        riepilogo = self.riepilogo_transito(destinazione, dal, al)
        buffer = io.StringIO()
        scrittore = csv.writer(buffer, delimiter=";")
        scrittore.writerow(
            [
                "spedizione",
                "destinazione",
                "data",
                "pezzi",
                "pazienti",
                "sigillata",
                "partita",
                "sigillo",
                "arrivo",
                "arrivati",
                "mancanti",
                "esito",
            ]
        )
        for voce in riepilogo["spedizioni"]:
            arrivo = voce["arrivo"] or {}
            scrittore.writerow(
                [
                    voce["shipment_id"],
                    voce["destinazione"],
                    voce["data"] or "",
                    voce["pezzi"],
                    voce["pazienti"],
                    voce["sigillata"] or "",
                    voce["partita"] or "",
                    "" if voce["sigillo_ok"] is None else ("ok" if voce["sigillo_ok"] else "no"),
                    arrivo.get("quando", ""),
                    arrivo.get("arrivati", ""),
                    arrivo.get("mancanti", ""),
                    voce["esito"],
                ]
            )
        # Il punto e virgola e il BOM sono per Excel in italiano, che e' dove
        # questo file verra' aperto: senza, le colonne finiscono tutte in una.
        contenuto = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        controparte = self._nome_sicuro(riepilogo["controparte"], "tutti")
        nome = f"transito_{controparte}_{dal or 'inizio'}_{al or 'oggi'}.csv"
        return contenuto, nome

    # -- strumenti e calibrazione -------------------------------------------
    def salute(self) -> dict[str, Any]:
        risposta = self.backend.health()
        return {"ok": bool(risposta.ok), **risposta.to_dict()}

    def imposta_potenze(self, potenze: Mapping[str, Any]) -> dict[str, Any]:
        """Cambia le potenze per antenna, in dBm interi come le mostra l'interfaccia."""
        antenne = []
        for voce in potenze.get("antenne", []) or []:
            antenne.append(
                AntennaPower(
                    antenna_id=int(voce["id"]),
                    read_power_cdbm=int(round(float(voce.get("lettura", 20)) * 100)),
                    write_power_cdbm=int(round(float(voce.get("scrittura", 20)) * 100)),
                )
            )
        if not antenne:
            raise WorkflowError("nessuna antenna indicata")
        risposta = self.backend.configure(
            ReaderSettings(
                region=self.config.get("reader", {}).get("region", 0x08), powers=tuple(antenne)
            )
        )
        if not risposta.ok:
            raise WorkflowError((risposta.error or {}).get("message", "configurazione fallita"))
        # Si aggiorna anche la configurazione in memoria, altrimenti la prossima
        # operazione ripristinerebbe di nascosto i valori del file.
        for voce in self.config.get("antennas", []):
            trovata = next((a for a in antenne if a.antenna_id == voce["id"]), None)
            if trovata is not None:
                voce["read_power"] = trovata.read_power_cdbm
                voce["write_power"] = trovata.write_power_cdbm
        return risposta.to_dict()

    def imposta_gen2(self, parametri: Mapping[str, Any]) -> dict[str, Any]:
        try:
            impostazioni = Gen2Settings(
                session=_intero_o_nulla(parametri.get("session")),
                target=_intero_o_nulla(parametri.get("target")),
                target_dynamic=bool(parametri.get("target_dynamic", False)),
                q=_intero_o_nulla(parametri.get("q")),
                q_dynamic=bool(parametri.get("q_dynamic", False)),
                rf_mode=_intero_o_nulla(parametri.get("rf_mode")),
            )
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        if not impostazioni.touches_anything:
            # Il servizio accetterebbe la richiesta senza fare niente, e
            # l'interfaccia direbbe «applicato» di un'operazione mai avvenuta.
            raise WorkflowError("nessun parametro Gen2 da applicare")
        risposta = self.backend.configure_gen2(impostazioni)
        if not risposta.ok:
            raise WorkflowError((risposta.error or {}).get("message", "Gen2 non applicati"))
        return {**risposta.to_dict(), "riletti": self._rileggi_gen2()}

    def _rileggi_gen2(self) -> dict[str, Any]:
        """Cosa il modulo ha davvero adesso, non cosa gli e' stato chiesto.

        Il manuale avverte che una modalita' RF non supportata viene
        **accettata e poi sostituita in silenzio**. Senza rileggere, l'interfaccia
        direbbe «applicato 0x71» mentre il modulo lavora a 0x6B, e tutte le
        misure fatte dopo sarebbero attribuite alla configurazione sbagliata.
        """
        lettura = getattr(self.backend, "read_gen2_settings", None)
        if lettura is None:
            return {}
        try:
            risposta = lettura()
        except Exception:  # noqa: BLE001
            log.debug("Rilettura dei parametri Gen2 non riuscita")
            return {}
        if not getattr(risposta, "ok", False):
            return {}
        return dict((risposta.data or {}).get("settings") or {})

    def gen2_consigliato(self) -> dict[str, Any]:
        """Applica la configurazione a riposo consigliata, e dice perche'.

        **I flussi operativi impostano gia' il Gen2 da soli**: il sigillo cambia
        sessione e modalita' a ogni passata (`lims.sealing.default_passes`), la
        campagna ha la sua griglia, il riempimento la sua potenza. Questa e' la
        configurazione in cui il lettore resta *fra* un'operazione e l'altra, e
        serve alle prove a mano: non e' lei a far funzionare il sigillo.
        """
        motivi = {
            "session": "S2 — chi ha gia' risposto tace per qualche secondo e "
            "lascia parlare gli altri: e' la scelta per una scatola piena",
            "target": "A↔B alternato — un giro per lato ripesca anche i tag "
            "gia' messi a tacere, che e' l'unico modo di non perderne nessuno",
            "q": "automatico — il numero di slot lo regola il modulo, e finche' "
            "la campagna non dice altro e' meglio di un numero scelto a mano",
            "rf_mode": "0x71 — circa 5 dB di sensibilita' in piu' della "
            "predefinita: su un tag al limite e' la differenza fra leggerlo e no",
        }
        impostazioni = Gen2Settings(
            session=2, target=0, target_dynamic=True, q=None, q_dynamic=True,
            rf_mode=P_RF_MODE_MAX_SENSITIVITY,
        )
        risposta = self.backend.configure_gen2(impostazioni)
        if not risposta.ok:
            raise WorkflowError(
                (risposta.error or {}).get("message", "configurazione consigliata non applicata")
            )
        riletti = self._rileggi_gen2()
        atteso = P_RF_MODE_MAX_SENSITIVITY
        sostituita = bool(riletti) and riletti.get("rf_mode") not in (None, atteso)
        return {
            **risposta.to_dict(),
            "motivi": motivi,
            "riletti": riletti,
            # Il caso che il manuale annuncia e nessuno verifica mai.
            "rf_sostituita": sostituita,
            "avviso": (
                f"il modulo ha sostituito la modalita' RF: chiesta 0x{atteso:02X}, "
                f"attiva 0x{int(riletti.get('rf_mode', 0)):02X}"
            )
            if sostituita
            else "",
        }

    def prova_lettura(self, cicli: int = 5) -> dict[str, Any]:
        """Un gruppo breve di letture, per vedere l'effetto di un parametro.

        E' il ritorno che ai parametri Gen2 e' sempre mancato: si cambia una
        tendina, si preme, e un numero dice se e' servito. Da solo «14 letture
        al secondo» non vuol dire niente — vale il confronto con la misura
        precedente, e per questo la si tiene accanto invece di sostituirla.
        """
        from lims.responses import inventory_tags
        from rfid_silion.service import InventoryRequest

        cicli = int(cicli)
        if not 1 <= cicli <= 40:
            raise WorkflowError("i cicli della prova devono essere fra 1 e 40")
        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))

        conteggi: dict[str, int] = {}
        rssi: dict[str, list[int]] = {}
        antenne_viste: dict[str, set[int]] = {}
        letture = 0
        errori = 0
        avvio = time.perf_counter()
        for _ in range(cicli):
            risposta = self.backend.inventory(
                InventoryRequest(antennas=antenne, timeout_ms=300)
            )
            if not risposta.ok:
                errori += 1
                continue
            visti: set[str] = set()
            for tag in inventory_tags(risposta):
                epc = str(tag.get("epc", "")).strip().upper()
                if not epc:
                    continue
                letture += 1
                visti.add(epc)
                if tag.get("rssi") is not None:
                    rssi.setdefault(epc, []).append(int(tag["rssi"]))
                if tag.get("antenna_id") is not None:
                    antenne_viste.setdefault(epc, set()).add(int(tag["antenna_id"]))
            for epc in visti:
                conteggi[epc] = conteggi.get(epc, 0) + 1
        durata = max(1e-6, time.perf_counter() - avvio)

        tutti = [valore for elenco in rssi.values() for valore in elenco]
        return {
            "cicli": cicli,
            "errori": errori,
            "durata_s": round(durata, 3),
            "letture": letture,
            "letture_al_secondo": round(letture / durata, 1),
            "cicli_al_secondo": round(cicli / durata, 1),
            "tag": len(conteggi),
            "antenne": list(antenne),
            "gen2": self._rileggi_gen2(),
            "rssi": None
            if not tutti
            else {
                "minimo": min(tutti),
                "mediano": sorted(tutti)[len(tutti) // 2],
                "massimo": max(tutti),
            },
            "dettaglio": sorted(
                (
                    {
                        "epc": epc,
                        "letture": quante,
                        "tasso": round(quante / cicli, 2),
                        "antenne": sorted(antenne_viste.get(epc, ())),
                        "rssi": max(rssi[epc]) if rssi.get(epc) else None,
                    }
                    for epc, quante in conteggi.items()
                ),
                key=lambda voce: (-voce["letture"], voce["epc"]),
            ),
        }

    def diagnostica_antenna(
        self,
        antenna: int,
        *,
        banda: int | None = None,
        da_khz: int | None = None,
        a_khz: int | None = None,
        passo_khz: int = 1000,
        consenti_cambio_regione: bool = False,
        nota: str = "",
    ) -> dict[str, Any]:
        """Return loss per frequenza: il dato da portare al fornitore.

        Le SLP1027 sono caratterizzate dal datasheet **solo da 900 MHz in su**,
        mentre la banda EU sta 35 MHz piu' in basso. Misurare solo dove il
        sistema lavora dice se l'antenna e' adattata, ma non dice la cosa che
        serve davvero sapere: se e' **cattiva** o se e' **buona ma accordata
        altrove**. Per rispondere serve guardare anche fuori.

        Tre modi, in ordine di quanto disturbano il modulo:

        1. senza argomenti — la banda configurata, com'e' sempre stato;
        2. `da_khz`/`a_khz` — un elenco esplicito di frequenze. Il comando
           0xAA4A lo accetta, e non tocca la regione impostata: e' la strada da
           provare per prima;
        3. `banda` con `consenti_cambio_regione` — il modulo viene commutato,
           misurato e **rimesso com'era in `finally`**. Serve quando il
           firmware rifiuta le frequenze fuori dalla sua regione.

        Il terzo modo trasmette fuori dalla banda ETSI. Ha senso su un banco di
        prototipazione e in nessun altro posto, e per questo non e' mai il
        comportamento predefinito: chi lo usa lo chiede.

        `nota` descrive **in che condizione** e' stata fatta la misura. Non e'
        un dettaglio burocratico: il VSWR e' l'impedenza vista al connettore, e
        quella dipende da tutto cio' che sta nel campo vicino dell'antenna —
        a 866 MHz circa sei centimetri. Un contenitore pieno di liquido
        appoggiato sopra sposta la curva; un tag no, e' uno scatteratore
        troppo piccolo per caricare l'antenna. Tre curve diverse nel registro
        possono essere tre antenne diverse **oppure la stessa antenna con tre
        cose diverse sul tavolo**, e senza questa riga fra un mese non si
        distinguono.
        """
        from rfid_silion.service import AntennaDiagnosticsRequest

        regione_attuale = int(self.config.get("reader", {}).get("region", 0x08))
        frequenze = self._frequenze(da_khz, a_khz, passo_khz)
        # Con un elenco esplicito la banda non serve al modulo, ma il campo
        # viaggia lo stesso: si manda quella corrente per non chiedergli di
        # cambiare regione senza che nessuno l'abbia deciso.
        chiesta = regione_attuale if frequenze else int(banda if banda is not None else regione_attuale)

        richiesta = AntennaDiagnosticsRequest(
            antenna=int(antenna), band=chiesta, frequencies_khz=frequenze
        )
        cambio = consenti_cambio_regione and chiesta != regione_attuale and not frequenze

        nota = str(nota).strip()[:200]
        # Il censimento fatto al collegamento dice se questo esemplare accetta
        # elenchi di frequenze singole. Il manuale (EX10 2024-12 §10.1) e'
        # esplicito: su un modulo non certificato Cina il campo N **deve**
        # valere 0. Provarci comunque produrrebbe un errore criptico invece di
        # una spiegazione, e farebbe credere a un guasto.
        if frequenze and self.hardware.get("rilevato") and not self.hardware.get(
            "frequenze_singole", True
        ):
            return {
                "non_supportata": True,
                "bande": self.hardware.get("bande", []),
                "bande_nomi": self.hardware.get("bande_nomi", []),
                "messaggio": self.hardware.get("motivo_limite", ""),
            }
        if cambio:
            esito = self._misura_cambiando_regione(richiesta, regione_attuale)
            esito["nota"] = nota
            return esito

        risposta = self.backend.antenna_diagnostics(richiesta)
        if not risposta.ok:
            messaggio = (risposta.error or {}).get("message", "misura non riuscita")
            if chiesta != regione_attuale:
                # Non e' un guasto: e' un firmware certificato per una regione
                # che rifiuta le altre. L'interfaccia puo' proporre il cambio.
                return {
                    "rifiutata": True,
                    "banda_chiesta": chiesta,
                    "regione_attuale": regione_attuale,
                    "messaggio": messaggio,
                }
            raise WorkflowError(messaggio)
        esito = self._descrivi_misura(risposta.to_dict(), antenna, chiesta, regione_attuale)
        esito["nota"] = nota
        return esito

    def _misura_cambiando_regione(self, richiesta: Any, regione_attuale: int) -> dict[str, Any]:
        """Commuta la regione, misura, e la rimette a posto comunque vada.

        Il ripristino sta in `finally` per la stessa ragione per cui ci sta
        quello del sigillo: una misura interrotta a meta' non deve lasciare la
        postazione a trasmettere in una banda che non e' la sua.
        """
        from rfid_silion.service import ReaderSettings

        applica = self.backend.configure(
            ReaderSettings(region=int(richiesta.band), powers=())
        )
        if not applica.ok:
            raise WorkflowError(
                (applica.error or {}).get(
                    "message", f"il modulo non accetta la regione 0x{richiesta.band:02X}"
                )
            )
        try:
            risposta = self.backend.antenna_diagnostics(richiesta)
            if not risposta.ok:
                raise WorkflowError(
                    (risposta.error or {}).get("message", "misura non riuscita")
                    + " (anche dopo il cambio di regione)"
                )
            esito = self._descrivi_misura(
                risposta.to_dict(), richiesta.antenna, int(richiesta.band), regione_attuale
            )
        finally:
            ripristino = self.backend.configure(self._reader_settings())
            if not ripristino.ok:
                log.error(
                    "Regione NON ripristinata dopo la misura: %s",
                    (ripristino.error or {}).get("message", ""),
                )
        esito["regione_commutata"] = True
        self.db.log_event(
            "band_sweep",
            ok=True,
            operator=self.operatore,
            detail=(
                f"antenna {richiesta.antenna}: regione commutata a "
                f"0x{richiesta.band:02X} e ripristinata a 0x{regione_attuale:02X}"
            ),
        )
        return esito

    @staticmethod
    def _frequenze(da_khz: int | None, a_khz: int | None, passo_khz: int) -> tuple[int, ...]:
        if da_khz is None or a_khz is None:
            return ()
        inizio, fine = int(da_khz), int(a_khz)
        if inizio > fine:
            inizio, fine = fine, inizio
        passo = max(100, int(passo_khz or 1000))
        # Il comando accetta al massimo 255 frequenze: se ne verrebbero di piu'
        # si allarga il passo invece di troncare l'intervallo, perche' una curva
        # che finisce a meta' non si confronta con niente.
        while (fine - inizio) // passo + 1 > 255:
            passo *= 2
        return tuple(range(inizio, fine + 1, passo))

    def _descrivi_misura(
        self, documento: Mapping[str, Any], antenna: int, banda: int, regione: int
    ) -> dict[str, Any]:
        """Aggiunge alla misura la frase che si porta al fornitore.

        Il VSWR peggiore da solo non dice se l'antenna e' sbagliata o
        semplicemente accordata altrove: quello lo dice **dove sta il minimo**.
        """
        esito = dict(documento)
        dati = dict(esito.get("data") or {})
        misure = list(dati.get("measurements") or [])
        esito["antenna"] = int(antenna)
        esito["banda"] = int(banda)
        esito["regione_configurata"] = int(regione)
        if not misure:
            return esito

        ordinate = sorted(misure, key=lambda voce: voce["frequency_khz"])
        migliore = min(ordinate, key=lambda voce: voce["vswr"])
        in_banda = [
            voce
            for voce in ordinate
            if _BANDA_EU_KHZ[0] <= voce["frequency_khz"] <= _BANDA_EU_KHZ[1]
        ]
        centro_eu = (_BANDA_EU_KHZ[0] + _BANDA_EU_KHZ[1]) / 2
        # Se il punto migliore e' il primo o l'ultimo della spazzata, la
        # risonanza vera sta **fuori** dall'intervallo misurato: chiamarla
        # risonanza sarebbe l'errore piu' facile da fare qui, e porterebbe a
        # dire al fornitore un numero che non esiste.
        al_bordo = migliore is ordinate[0] or migliore is ordinate[-1]
        dati["risonanza"] = {
            "frequency_khz": migliore["frequency_khz"],
            "vswr": migliore["vswr"],
            # Quanto e' lontano il punto migliore dal centro della banda in cui
            # il sistema deve lavorare: e' il numero della richiesta al fornitore.
            "scarto_da_eu_khz": round(migliore["frequency_khz"] - centro_eu),
            "al_bordo": al_bordo,
        }
        if in_banda:
            peggiore_eu = max(voce["vswr"] for voce in in_banda)
            dati["in_banda_eu"] = {
                "punti": len(in_banda),
                "vswr_peggiore": peggiore_eu,
                "ok": peggiore_eu < float(dati.get("threshold", 7.0)),
            }
        dati["larghezza_banda"] = self._larghezza_banda(ordinate)
        esito["data"] = dati
        return esito

    @staticmethod
    def _larghezza_banda(
        ordinate: list[Mapping[str, Any]], soglia: float = 2.0
    ) -> dict[str, Any]:
        """Fra che frequenze l'antenna resta sotto VSWR 2 (cioe' -10 dB).

        E' il numero che si scrive nei datasheet e che un fornitore riconosce
        subito: la SLP1027 dichiara «VSWR <= 1,3 su 902-928 MHz». Il *minimo*
        della curva, su un pannello a banda larga come questo, e' basso e
        piatto — la sua posizione esatta si sposta col rumore di misura e non
        e' un dato solido. **L'estensione della banda utile lo e'**, ed e' il
        confronto diretto con la riga del datasheet.

        Se la banda utile arriva fino al bordo della spazzata, si dice: vuol
        dire che continua oltre e non e' stata misurata tutta.
        """
        sotto = [voce for voce in ordinate if voce["vswr"] <= soglia]
        if not sotto:
            return {"soglia": soglia, "trovata": False}
        da, a = sotto[0]["frequency_khz"], sotto[-1]["frequency_khz"]
        return {
            "soglia": soglia,
            "trovata": True,
            "da_khz": da,
            "a_khz": a,
            "larghezza_khz": a - da,
            "al_bordo": da == ordinate[0]["frequency_khz"] or a == ordinate[-1]["frequency_khz"],
            # La banda utile copre tutta la banda ETSI? E' la domanda operativa.
            "copre_eu": da <= _BANDA_EU_KHZ[0] and a >= _BANDA_EU_KHZ[1],
        }

    def profila_tag(self) -> dict[str, Any]:
        """Misura TID e USER memory del tag, e dice cosa farne.

        Finora la misura finiva in un riquadro e andava ricopiata a mano in
        `config.yaml`: un numero misurato che nessuno riporta e' un numero
        perso. Adesso la risposta porta anche **cosa propone di cambiare**, e
        l'operatore lo conferma con `applica_profilo`.
        """
        from lims.profiler import profile_tag, summarize

        accesso = self.config.get("tag_access", {}) or {}
        profilo = profile_tag(
            self.backend,
            antennas=tuple(self.lims_cfg.get("write_antennas") or (1,)),
            access_password_hex=accesso.get("access_password_hex", "00000000"),
            timeout_ms=accesso.get("timeout_ms", 1000),
        )
        return {
            "profilo": profilo.to_dict(),
            "riassunto": summarize(profilo),
            "proposta": self._proposta_dal_profilo(profilo),
        }

    def _proposta_dal_profilo(self, profilo: Any) -> dict[str, Any]:
        """Cosa cambierebbe in configurazione questa misura.

        Non cambia niente da sola. Una modalita' ridotta che si attiva
        silenziosamente farebbe decadere una difesa di sicurezza senza che
        nessuno l'abbia decisa, e mesi dopo nessuno saprebbe piu' perche'.
        """
        if not profilo.ok:
            return {"cambia": False, "motivo": "la profilazione non e' riuscita"}

        attuale_byte = int(self.lims_cfg.get("user_memory_bytes", 64))
        attuale_modalita = self.modalita_scrittura
        utili = int(profilo.usable_payload_bytes)
        basta = utili >= PAYLOAD_FIXED_SIZE

        modalita = MODALITA_PAYLOAD if basta else MODALITA_SOLO_EPC
        motivi = []
        if not basta:
            motivi.append(
                f"questo tag offre {utili} byte utili di payload, ne servono "
                f"almeno {PAYLOAD_FIXED_SIZE}: il campione non ci sta"
            )
            motivi.append(
                "in modalita' «solo EPC» il contenitore resta identificato dal "
                "suo pseudonimo e cosa contiene lo dice la distinta stampata"
            )
            motivi.append(
                "si perde il legame fra campione e numero di serie del chip, "
                "cioe' la difesa contro un tag copiato"
            )
        elif profilo.tid is None or not profilo.tid.serialized:
            motivi.append(
                "il TID non e' serializzato: il legame anti-clonazione non regge "
                "comunque, anche scrivendo il campione"
            )

        return {
            "cambia": modalita != attuale_modalita or int(profilo.user_bytes) != attuale_byte,
            "modalita_scrittura": modalita,
            "modalita_attuale": attuale_modalita,
            "user_memory_bytes": int(profilo.user_bytes),
            "user_memory_attuale": attuale_byte,
            # Scendere e' sempre giusto (la soglia deve reggere il tag peggiore
            # del lotto); salire va deciso, perche' renderebbe illeggibile un
            # tag piu' piccolo gia' scritto.
            "in_aumento": int(profilo.user_bytes) > attuale_byte,
            "motivi": motivi,
        }

    def applica_profilo(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Scrive in `config.yaml` quello che la profilazione ha misurato.

        Si appoggia a `app.config_misura`, che modifica il file **riga per
        riga**: `config.yaml` e' pieno di commenti che spiegano ogni parametro,
        e riscriverlo con `yaml.safe_dump` li cancellerebbe tutti.
        """
        self._require_operator("cambiare la configurazione dei tag")
        if self.config_path is None:
            raise WorkflowError(
                "questa postazione non ha un file di configurazione da aggiornare"
            )
        from app.config_misura import Misura, scrivi_misura

        modalita = str(dati.get("modalita_scrittura", "")).strip() or None
        if modalita is not None and modalita not in (MODALITA_PAYLOAD, MODALITA_SOLO_EPC):
            raise WorkflowError(f"modalita' sconosciuta: {modalita}")

        byte = _intero_o_nulla(dati.get("user_memory_bytes"))
        esito = None
        if byte is not None:
            esito = scrivi_misura(
                self.config_path,
                Misura(
                    user_bytes=int(byte),
                    tid_serializzato=bool(dati.get("tid_serializzato", False)),
                    chip=str(dati.get("chip", "")),
                    epc=str(dati.get("epc", "")),
                ),
                forza=bool(dati.get("forza", False)),
                modalita_scrittura=modalita,
            )
        elif modalita is not None:
            esito = scrivi_misura(
                self.config_path, None, modalita_scrittura=modalita
            )

        # La configurazione in memoria segue il file, altrimenti l'interfaccia
        # direbbe una cosa e il prossimo tag ne farebbe un'altra.
        if modalita is not None:
            self.lims_cfg["modalita_scrittura"] = modalita
            self.config.setdefault("lims", {})["modalita_scrittura"] = modalita
        if byte is not None and esito is not None and esito.scritto:
            self.lims_cfg["user_memory_bytes"] = int(esito.dopo or byte)
            self.config.setdefault("lims", {})["user_memory_bytes"] = int(esito.dopo or byte)

        self.db.log_event(
            "tag_profile_apply",
            ok=True,
            operator=self.operatore,
            detail=(
                f"modalita' {self.modalita_scrittura}, "
                f"user_memory_bytes {self.lims_cfg.get('user_memory_bytes')}"
                + (f"; {esito.motivo}" if esito is not None else "")
            ),
        )
        return {
            "modalita_scrittura": self.modalita_scrittura,
            "user_memory_bytes": int(self.lims_cfg.get("user_memory_bytes", 64)),
            "scritto": bool(esito.scritto) if esito is not None else False,
            "motivo": esito.motivo if esito is not None else "niente da cambiare",
        }

    def rileva_controllo(self, posizione: str) -> dict[str, Any]:
        """Fotografa gli EPC presenti, per dichiararli dentro o fuori.

        La campagna sceglie la potenza piu' bassa che legge tutto *dentro* senza
        leggere niente *fuori*: senza i tag di controllo fuori, sceglierebbe
        sempre la potenza massima e il volume di lettura non sarebbe definito.
        """
        from lims.responses import inventory_epcs
        from rfid_silion.service import InventoryRequest

        if posizione not in ("dentro", "fuori"):
            raise WorkflowError("posizione deve essere 'dentro' o 'fuori'")
        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))
        risposta = self.backend.inventory(
            InventoryRequest(antennas=antenne, timeout_ms=1000)
        )
        if not risposta.ok:
            raise WorkflowError((risposta.error or {}).get("message", "lettura non riuscita"))
        epcs = inventory_epcs(risposta)
        self.campagna_epcs[posizione] = epcs
        # Un EPC dichiarato in entrambe le posizioni renderebbe la misura
        # insensata: si toglie dall'altro elenco, dove evidentemente non era.
        altra = "fuori" if posizione == "dentro" else "dentro"
        self.campagna_epcs[altra] = [
            epc for epc in self.campagna_epcs.get(altra, []) if epc not in epcs
        ]
        return {
            "posizione": posizione,
            "epcs": epcs,
            "dentro": len(self.campagna_epcs.get("dentro", [])),
            "fuori": len(self.campagna_epcs.get("fuori", [])),
        }

    def inventario_campagna(
        self,
        cicli: int = 20,
        *,
        on_progress: Callable[[int, int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Legge insieme tutti i tag da classificare come dentro o fuori.

        Una singola lettura non basta: orientamento, liquido e collisioni fanno
        sparire temporaneamente anche un tag presente. Si usa quindi l'unione di
        circa venti cicli e si conserva anche il tasso di rilevamento, utile a
        riconoscere subito un tag gia' difficile nella fotografia iniziale.
        """
        from lims.responses import inventory_tags
        from rfid_silion import protocol as P
        from rfid_silion.service import InventoryRequest

        cicli = int(cicli)
        if not 1 <= cicli <= 100:
            raise WorkflowError("i cicli dell'inventario iniziale devono essere fra 1 e 100")
        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))
        if not antenne:
            raise WorkflowError("nessuna antenna di lettura configurata")

        massimo_dbm = float(self.config.get("reader", {}).get("max_power_dbm", 30))
        potenza = int(round(massimo_dbm * 100))
        per_id = {int(a["id"]): a for a in self.config.get("antennas", [])}
        conteggi: dict[str, int] = {}
        antenne_viste: dict[str, set[int]] = {}
        rssi_massimo: dict[str, int] = {}
        gen2_precedente = self._snapshot_gen2()
        try:
            risposta = self.backend.configure(
                ReaderSettings(
                    region=int(self.config.get("reader", {}).get("region", 0x08)),
                    powers=tuple(
                        AntennaPower(
                            antenna_id=antenna,
                            read_power_cdbm=potenza,
                            write_power_cdbm=int(per_id.get(antenna, {}).get("write_power", 2000)),
                        )
                        for antenna in antenne
                    ),
                )
            )
            if not risposta.ok:
                raise WorkflowError(
                    (risposta.error or {}).get("message", "potenza di ricerca non applicata")
                )
            risposta = self.backend.configure_gen2(
                Gen2Settings(
                    session=0,
                    target=0,
                    target_dynamic=True,
                    q_dynamic=True,
                    rf_mode=P.RF_MODE_MAX_SENSITIVITY,
                )
            )
            if not risposta.ok:
                raise WorkflowError(
                    (risposta.error or {}).get("message", "parametri di ricerca non applicati")
                )

            for indice in range(1, cicli + 1):
                risposta = self.backend.inventory(
                    InventoryRequest(antennas=antenne, timeout_ms=700)
                )
                if not risposta.ok:
                    raise WorkflowError(
                        (risposta.error or {}).get("message", "inventario iniziale fallito")
                    )
                visti_nel_ciclo: set[str] = set()
                for tag in inventory_tags(risposta):
                    epc = str(tag.get("epc", "")).strip().upper()
                    if not epc:
                        continue
                    visti_nel_ciclo.add(epc)
                    antenna = tag.get("antenna_id")
                    if antenna is not None:
                        antenne_viste.setdefault(epc, set()).add(int(antenna))
                    rssi = tag.get("rssi")
                    if rssi is not None:
                        rssi_massimo[epc] = max(rssi_massimo.get(epc, -128), int(rssi))
                for epc in visti_nel_ciclo:
                    conteggi[epc] = conteggi.get(epc, 0) + 1
                if on_progress is not None:
                    on_progress(indice, cicli, len(conteggi))
                if indice < cicli:
                    time.sleep(0.02)
        finally:
            self._restore_radio(gen2_precedente)

        candidati = {
            epc: {
                "epc": epc,
                "letture": letture,
                "cicli": cicli,
                "tasso": round(letture / cicli, 3),
                "antenne": sorted(antenne_viste.get(epc, set())),
                "rssi_massimo": rssi_massimo.get(epc),
            }
            for epc, letture in conteggi.items()
        }
        self.campagna_candidati = candidati
        self.campagna_epcs = {"dentro": [], "fuori": []}
        return {
            "cicli": cicli,
            "potenza_dbm": massimo_dbm,
            "tag": sorted(
                candidati.values(), key=lambda voce: (-voce["letture"], voce["epc"])
            ),
        }

    def _classifica_campagna(self, dentro: Any, fuori: Any) -> None:
        """Convalida la classificazione manuale della fotografia iniziale."""
        if not self.campagna_candidati:
            raise WorkflowError("prima va eseguito l'inventario iniziale dei tag")
        if not isinstance(dentro, (list, tuple)) or not isinstance(fuori, (list, tuple)):
            raise WorkflowError("le selezioni dentro e fuori devono essere elenchi di EPC")
        interni = {str(epc).strip().upper() for epc in dentro if str(epc).strip()}
        esterni = {str(epc).strip().upper() for epc in fuori if str(epc).strip()}
        if not interni:
            raise WorkflowError("selezionare almeno un tag interno")
        if not esterni:
            raise WorkflowError("selezionare almeno un tag esterno")
        sovrapposti = interni & esterni
        if sovrapposti:
            raise WorkflowError("uno stesso tag non puo' essere sia interno sia esterno")
        candidati = set(self.campagna_candidati)
        sconosciuti = (interni | esterni) - candidati
        if sconosciuti:
            raise WorkflowError(
                f"{len(sconosciuti)} tag selezionati non appartengono all'inventario iniziale"
            )
        non_classificati = candidati - interni - esterni
        if non_classificati:
            raise WorkflowError(
                f"classificare tutti i tag: ne restano {len(non_classificati)} senza posizione"
            )
        self.campagna_epcs = {
            "dentro": sorted(interni),
            "fuori": sorted(esterni),
        }

    def esegui_campagna(
        self,
        parametri: Mapping[str, Any],
        *,
        on_progress: Callable[[int, int, Any], None] | None = None,
        stop_event: Any = None,
    ) -> dict[str, Any]:
        from lims.campaign import CampaignConfig, ReadCampaign, grid_passes

        if "dentro" in parametri or "fuori" in parametri:
            self._classifica_campagna(
                parametri.get("dentro", []), parametri.get("fuori", [])
            )

        dentro = self.campagna_epcs.get("dentro", [])
        if not dentro:
            raise WorkflowError(
                "prima va rilevato il contenuto del contenitore: senza, non c'e' "
                "niente da cercare"
            )
        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))
        potenze = tuple(
            int(round(float(p) * 100)) for p in parametri.get("potenze_dbm", [15, 20, 25, 30])
        )
        geometria = self.config.get("geometry", {}) or {}
        volume = geometria.get("volume_mm") or None
        gen2_precedente = self._snapshot_gen2()
        try:
            campagna = ReadCampaign(
                self.backend,
                inside_epcs=dentro,
                outside_epcs=self.campagna_epcs.get("fuori", []),
                config=CampaignConfig(
                    cycles_per_configuration=int(parametri.get("cicli", 10)),
                    region=self.config.get("reader", {}).get("region", 0x08),
                    container_mm=tuple(volume) if volume else None,
                    notes=str(parametri.get("note", "")),
                ),
            )
            griglia = grid_passes(antenne, powers_cdbm=potenze)
            report = campagna.run(griglia, on_progress=on_progress, stop_event=stop_event)
        except ValueError as exc:
            raise WorkflowError(str(exc)) from exc
        finally:
            self._restore_radio(gen2_precedente)
        return report.to_dict()

    # -- impostazioni di collegamento ---------------------------------------
    def porte_seriali(self) -> dict[str, Any]:
        """Le porte seriali viste dal sistema, con quel che si sa di ciascuna.

        Su USB la baseboard SLD1090 si presenta come una porta seriale: e' per
        questo che «USB» e «RS232» sono lo stesso trasporto e cambia solo il
        cavo. Il riconoscimento serve a non far indovinare la porta giusta
        all'operatore.
        """
        try:
            from serial.tools import list_ports
        except ImportError:
            return {"porte": [], "errore": "pyserial non installato"}

        porte = []
        for porta in list_ports.comports():
            descrizione = " ".join(
                filter(None, [porta.description or "", porta.manufacturer or "", porta.hwid or ""])
            ).upper()
            porte.append(
                {
                    "device": porta.device,
                    "descrizione": porta.description or "",
                    "costruttore": porta.manufacturer or "",
                    "hwid": porta.hwid or "",
                    # Il modulo si presenta come "HDSC" (vedi le note hardware);
                    # gli altri sono i convertitori USB-seriale piu' comuni.
                    "probabile": any(
                        marchio in descrizione
                        for marchio in ("HDSC", "SILION", "CH340", "CP210", "FTDI", "USB SERIAL")
                    ),
                }
            )
        return {"porte": porte}

    def impostazioni(self) -> dict[str, Any]:
        """Tutta la configurazione modificabile, com'e' adesso."""
        seriale = dict(self.config.get("serial") or self.config.get("serial_disabled") or {})
        tcp = dict(self.config.get("tcp") or self.config.get("tcp_disabled") or {})
        lettore = dict(self.config.get("reader") or {})
        inventario = dict(self.config.get("inventory") or {})
        taratura = dict(self.config.get("tuning") or {})
        return {
            "trasporto_attivo": "tcp" if "tcp" in self.config else "seriale",
            "salvabile": self.config_path is not None,
            "percorso_config": str(self.config_path) if self.config_path else "",
            "seriale": {
                "port": seriale.get("port", ""),
                "baudrate": seriale.get("baudrate", 115200),
                "timeout_s": seriale.get("timeout_s", 2.0),
                "inter_byte_timeout_s": seriale.get("inter_byte_timeout_s", 0.1),
            },
            "tcp": {
                "host": tcp.get("host", "192.168.1.100"),
                "port": tcp.get("port", 8080),
                "timeout_s": tcp.get("timeout_s", 2.0),
            },
            "lettore": {
                "region": lettore.get("region", 0x08),
                "boot_timeout_ms": lettore.get("boot_timeout_ms", 3000),
                "response_timeout_ms": lettore.get("response_timeout_ms", 2000),
                "max_power_dbm": lettore.get("max_power_dbm", 30),
            },
            "inventario": {
                "timeout_ms": inventario.get("timeout_ms", 1000),
                "metadata_flags": inventario.get("metadata_flags", 7),
                "duration_s": inventario.get("duration_s", 30.0),
                "max_unique_epcs": inventario.get("max_unique_epcs", 1000),
                "presence_threshold": inventario.get("presence_threshold", 2),
            },
            "taratura": {
                "power_mode": taratura.get("power_mode"),
                "antenna_dwell_ms": taratura.get("antenna_dwell_ms"),
                "rssi_filter_dbm": taratura.get("rssi_filter_dbm"),
                "max_rssi_reporting": taratura.get("max_rssi_reporting"),
                "duty_cycle_full_ms": taratura.get("duty_cycle_full_ms"),
                "duty_cycle_period_ms": taratura.get("duty_cycle_period_ms"),
            },
        }

    def _config_trasporto(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Costruisce la configurazione con il trasporto scelto.

        La sezione inattiva viene parcheggiata come `serial_disabled` /
        `tcp_disabled`: il trasporto si sceglie per **presenza della chiave**,
        quindi lasciarle entrambe significherebbe sceglierne una a caso.
        """
        nuova = copy.deepcopy(self.config)
        nuova.pop("serial", None)
        nuova.pop("tcp", None)
        scelta = str(dati.get("trasporto", "seriale")).lower()

        if scelta == "seriale":
            seriale = dict(self.config.get("serial") or self.config.get("serial_disabled") or {})
            porta = str(dati.get("port", "")).strip()
            if not porta:
                raise WorkflowError("scegliere la porta seriale")
            seriale.update(
                {
                    "port": porta,
                    "baudrate": int(dati.get("baudrate", 115200)),
                    "timeout_s": float(dati.get("timeout_s", 2.0)),
                    "inter_byte_timeout_s": float(dati.get("inter_byte_timeout_s", 0.1)),
                }
            )
            nuova["serial"] = seriale
            nuova.pop("serial_disabled", None)
            if tcp := self.config.get("tcp"):
                nuova["tcp_disabled"] = tcp
        elif scelta == "tcp":
            tcp = dict(self.config.get("tcp") or self.config.get("tcp_disabled") or {})
            host = str(dati.get("host", "")).strip()
            if not host:
                raise WorkflowError("indicare l'indirizzo del lettore")
            tcp.update(
                {
                    "host": host,
                    "port": int(dati.get("port_tcp", 8080)),
                    "timeout_s": float(dati.get("timeout_s", 2.0)),
                }
            )
            nuova["tcp"] = tcp
            nuova.pop("tcp_disabled", None)
            if seriale := self.config.get("serial"):
                nuova["serial_disabled"] = seriale
        else:
            raise WorkflowError(f"trasporto sconosciuto: {scelta}")
        return nuova

    def applica_collegamento(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Cambia trasporto e riavvia il lettore.

        E' anche la prova del collegamento: se il lettore risponde con la sua
        versione, il cavo e i parametri sono giusti. Il servizio va fermato
        prima, perche' la config non si sostituisce a trasporto aperto.
        """
        nuova = self._config_trasporto(dati)
        sostituzione = getattr(self.backend, "replace_config", None)
        if sostituzione is None:
            raise WorkflowError("questo backend non consente di cambiare configurazione")

        self.backend.stop()
        risposta = sostituzione(nuova)
        if not risposta.ok:
            raise WorkflowError((risposta.error or {}).get("message", "config rifiutata"))
        self.config = nuova
        self.lims_cfg = dict(self.config.get("lims", {}) or {})

        avvio = self.backend.start()
        if not avvio.ok:
            raise WorkflowError(
                (avvio.error or {}).get("message", "il lettore non risponde con questi parametri")
            )
        configurazione = self.backend.configure(self._reader_settings())
        return {
            "trasporto": "tcp" if "tcp" in nuova else "seriale",
            "avvio": avvio.to_dict(),
            "configurazione_ok": bool(configurazione.ok),
            "configurazione_errore": (configurazione.error or {}).get("message", ""),
        }

    def imposta_avanzate(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Regione radio, timeout del driver e taratura del trasmettitore."""
        esito: dict[str, Any] = {}

        regione = _intero_o_nulla(dati.get("region"))
        if regione is not None:
            if not 0 <= regione <= 0xFF:
                raise WorkflowError("la regione deve stare in un byte (0-255)")
            self.config.setdefault("reader", {})["region"] = regione
            risposta = self.backend.configure(self._reader_settings())
            if not risposta.ok:
                raise WorkflowError((risposta.error or {}).get("message", "regione non applicata"))
            esito["regione"] = f"0x{regione:02X}"

        for chiave in ("boot_timeout_ms", "response_timeout_ms", "max_power_dbm"):
            valore = _intero_o_nulla(dati.get(chiave))
            if valore is not None:
                self.config.setdefault("reader", {})[chiave] = valore
                esito[chiave] = valore
        if esito.get("boot_timeout_ms") or esito.get("response_timeout_ms"):
            # Questi entrano nel driver alla costruzione del reader, non a caldo.
            esito["nota_timeout"] = "i timeout valgono dal prossimo collegamento"

        taratura = {
            chiave: dati.get(chiave)
            for chiave in (
                "power_mode",
                "antenna_dwell_ms",
                "rssi_filter_dbm",
                "max_rssi_reporting",
                "duty_cycle_full_ms",
                "duty_cycle_period_ms",
            )
            if dati.get(chiave) not in (None, "")
        }
        if dati.get("disable_rssi_filter"):
            taratura["disable_rssi_filter"] = True
            taratura.pop("rssi_filter_dbm", None)
        if taratura:
            from rfid_silion.service import ReaderTuning

            try:
                impostazioni = ReaderTuning.from_mapping(taratura)
            except ValueError as exc:
                raise WorkflowError(str(exc)) from exc
            risposta = self.backend.tune_reader(impostazioni)
            if not risposta.ok:
                raise WorkflowError((risposta.error or {}).get("message", "taratura non applicata"))
            esito["taratura"] = (risposta.data or {}).get("applied", {})
            self.config["tuning"] = {**(self.config.get("tuning") or {}), **taratura}

        inventario = {
            chiave: dati.get(chiave)
            for chiave in (
                "timeout_ms",
                "metadata_flags",
                "duration_s",
                "max_unique_epcs",
                "presence_threshold",
            )
            if dati.get(chiave) not in (None, "")
        }
        if inventario:
            sezione = dict(self.config.get("inventory") or {})
            for chiave, valore in inventario.items():
                sezione[chiave] = float(valore) if chiave == "duration_s" else int(valore)
            self.config["inventory"] = sezione
            esito["inventario"] = sezione

        if not esito:
            raise WorkflowError("nessun parametro da applicare")
        return esito

    # -- anagrafiche: laboratorio, operatori, destinatari --------------------
    _CAMPI_LABORATORIO = (
        "nome",
        "codice",
        "indirizzo",
        "cap",
        "citta",
        "provincia",
        "telefono",
        "email",
        "referente",
        "email_referente",
        "pec",
    )
    _CAMPI_OPERATORE = (
        "codice",
        "cognome",
        "nome",
        "email",
        "ruolo",
        "pin_service",
        "pin_username",
    )
    _CAMPI_DESTINATARIO = (
        "nome",
        "codice",
        "email",
        "pec",
        "referente",
        "indirizzo",
        "citta",
        "encryption_certificate",
    )

    def imposta_anagrafiche(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Aggiorna laboratorio, operatori e destinatari in memoria.

        Non scrive su disco: il salvataggio e' un gesto separato e volontario,
        come per il resto delle impostazioni.
        """
        if "laboratorio" in dati:
            sezione = dict(self.config.get("laboratorio") or {})
            grezzo = dati.get("laboratorio") or {}
            for campo in self._CAMPI_LABORATORIO:
                if campo in grezzo:
                    sezione[campo] = str(grezzo.get(campo) or "").strip()
            for campo in ("email", "email_referente", "pec"):
                if sezione.get(campo):
                    _controlla_email(sezione[campo], f"laboratorio.{campo}")
            self.config["laboratorio"] = sezione
            # Le etichette leggono ancora `lims.lab_name`: si tiene allineato,
            # altrimenti cambiare il nome qui non cambierebbe cio' che si stampa.
            self.lims_cfg["lab_name"] = sezione.get("nome", "")
            self.config.setdefault("lims", {})["lab_name"] = sezione.get("nome", "")

        if "operatori" in dati:
            self.config["operatori"] = self._normalizza(
                dati.get("operatori"), self._CAMPI_OPERATORE, "operatore"
            )
            # L'operatore in servizio potrebbe essere stato tolto dall'elenco.
            attivi = [
                _etichetta_operatore(voce)
                for voce in _voci_attive(self.config.get("operatori"))
            ]
            if self.operatore and self.operatore not in attivi:
                self.operatore = ""

        if "destinatari" in dati:
            self.config["destinatari"] = self._normalizza(
                dati.get("destinatari"), self._CAMPI_DESTINATARIO, "destinatario"
            )

        return self.descrivi()

    def _normalizza(
        self, righe: Any, campi: tuple[str, ...], genere: str
    ) -> list[dict[str, Any]]:
        """Ripulisce un elenco di anagrafiche, scartando le righe vuote."""
        pulite: list[dict[str, Any]] = []
        for indice, riga in enumerate(righe or [], start=1):
            if not isinstance(riga, Mapping):
                continue
            voce = {campo: str(riga.get(campo) or "").strip() for campo in campi}
            voce["attivo"] = bool(riga.get("attivo", True))
            if not any(voce[campo] for campo in campi):
                continue  # riga lasciata in bianco: si ignora, non e' un errore
            if voce.get("email"):
                _controlla_email(voce["email"], f"{genere} {indice}")
            if voce.get("pec"):
                _controlla_email(voce["pec"], f"{genere} {indice} PEC")
            identita = (
                _etichetta_operatore(voce) if genere == "operatore" else voce.get("nome")
            )
            if not identita:
                raise WorkflowError(
                    f"{genere} {indice}: serve almeno "
                    + ("il cognome o la codifica" if genere == "operatore" else "il nome")
                )
            pulite.append(voce)

        etichette = [
            _etichetta_operatore(v) if genere == "operatore" else v["nome"] for v in pulite
        ]
        duplicati = {e for e in etichette if etichette.count(e) > 1}
        if duplicati:
            # Due righe indistinguibili nel menu a tendina sono una trappola:
            # l'operatore ne sceglie una a caso e il registro non lo dice.
            raise WorkflowError(
                f"{genere}i con la stessa identita': {', '.join(sorted(duplicati))}"
            )
        return pulite

    def destinatario(self, nome: str) -> dict[str, Any] | None:
        """Il destinatario configurato con questo nome, se c'e'."""
        for voce in _voci_attive(self.config.get("destinatari")):
            if str(voce.get("nome", "")).strip() == str(nome).strip():
                return voce
        return None

    def salva_impostazioni(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Scrive la configurazione su disco, preservando quello che non tocca."""
        if self.config_path is None:
            raise WorkflowError(
                "questa sessione non ha un file di configurazione: le modifiche "
                "valgono solo fino alla chiusura"
            )
        import yaml

        nuova = self._config_trasporto(dati) if dati.get("trasporto") else copy.deepcopy(self.config)
        testo = yaml.safe_dump(nuova, sort_keys=False, allow_unicode=True)
        # Scrittura in due tempi: un'interruzione a meta' lascerebbe il
        # laboratorio senza configurazione invece che con quella vecchia.
        provvisorio = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        provvisorio.write_text(testo, encoding="utf-8")
        provvisorio.replace(self.config_path)
        self.config = nuova
        self.lims_cfg = dict(self.config.get("lims", {}) or {})
        log.info("Configurazione salvata in %s", self.config_path)
        return {"salvato": str(self.config_path)}

    # -- archivio pazienti ---------------------------------------------------
    #: Quanti pazienti per pagina. Cinquanta stanno in una schermata da scorrere
    #: una volta; di piu' vuol dire che si scorre invece di cercare.
    PAGINA_ARCHIVIO = 50

    def cerca_paziente(
        self,
        query: str = "",
        *,
        offset: int = 0,
        ordine: str = "recenti",
        limite: int | None = None,
    ) -> dict[str, Any]:
        """L'elenco dei pazienti, filtrato o no.

        Query vuota vuol dire **tutti**: l'archivio si sfoglia, non solo si
        interroga. Il totale viaggia insieme alla pagina perche' senza di esso
        «50 pazienti» e «50 dei 1284 in archivio» si leggono uguale e vogliono
        dire cose molto diverse.
        """
        limite = int(limite or self.PAGINA_ARCHIVIO)
        offset = max(0, int(offset))
        risultati = self.db.search_patients(
            query, limit=limite, offset=offset, order=ordine
        )
        totale = self.db.count_patients(query)
        return {
            "risultati": risultati,
            "totale": totale,
            "offset": offset,
            "ordine": ordine if ordine in self.db.ORDINI_PAZIENTI else "recenti",
            "filtrato": bool(str(query).strip()),
            "altri": max(0, totale - offset - len(risultati)),
        }

    def storico_paziente(self, patient_id: int) -> dict[str, Any]:
        """Storico completo, gia' riassunto per accettazione.

        Il riassunto si fa qui e non nell'interfaccia: «quanti pezzi, dove,
        quando, con che esito» e' una risposta, e va costruita una volta sola.
        """
        try:
            storico = self.db.patient_history(int(patient_id))
        except Exception as exc:  # noqa: BLE001
            raise WorkflowError(str(exc)) from exc

        for accettazione in storico["accettazioni"]:
            contenitori = accettazione["contenitori"]
            attivi = [c for c in contenitori if c["state"] != ContainerState.VOIDED.value]
            spediti = [c for c in attivi if c.get("sent_at")]
            spedizioni: dict[int, dict[str, Any]] = {}
            for contenitore in attivi:
                if not contenitore.get("shipment_id"):
                    continue
                voce = spedizioni.setdefault(
                    contenitore["shipment_id"],
                    {
                        "shipment_id": contenitore["shipment_id"],
                        "destinazione": contenitore["destinazione"],
                        "inviata": contenitore.get("sent_at"),
                        "sigillata": contenitore.get("sealed_at"),
                        "supervisore": contenitore.get("supervisore") or "",
                        "sigillo_ok": contenitore.get("sealing_ok"),
                        "sigillo_dettaglio": contenitore.get("sealing_detail") or "",
                        "stato": contenitore.get("shipment_state"),
                        "pezzi": 0,
                    },
                )
                voce["pezzi"] += 1
            accettazione["riassunto"] = {
                "pezzi": len(attivi),
                "annullati": len(contenitori) - len(attivi),
                "scritti": len([c for c in attivi if c["epc"]]),
                "spediti": len(spediti),
                "spedizioni": sorted(spedizioni.values(), key=lambda v: v["shipment_id"]),
                # «Tutto a buon fine» ha un significato preciso: ogni contenitore
                # attivo e' stato scritto, e' partito, e il sigillo che lo
                # accompagnava era completo. Se manca un pezzo si dice quale.
                "completo": bool(attivi)
                and len(spediti) == len(attivi)
                and all(voce["sigillo_ok"] == 1 for voce in spedizioni.values()),
            }
        return storico

    def traccia_contenitore(self, epc: str) -> dict[str, Any]:
        """Ogni operazione registrata su un contenitore, per una verifica esterna."""
        record = self.db.find_container_by_epc(str(epc).strip().upper())
        return {
            "epc": str(epc).strip().upper(),
            "contenitore": None
            if record is None
            else {
                "etichetta": record.label,
                "paziente": record.display_name,
                "codice_fiscale": record.codice_fiscale,
                "accettazione": record.accession_id,
                "stato": record.state.value,
                "tid": record.tid,
            },
            "eventi": self.db.container_trace(epc),
        }

    def registro_misure(self, limite: int = 40) -> dict[str, Any]:
        """Le misure gia' fatte, rilette dal diario.

        Non c'e' nessun archivio nuovo: adattamento, profilazione e campagna
        finiscono gia' nel diario di prototipazione, con la loro ora e i loro
        numeri. Mancava soltanto rileggerli in forma di scheda invece che di
        righe di log — che e' la differenza fra un dato conservato e un dato
        consultabile.
        """
        from rfid_silion.diario import leggi_diario

        # I percorsi del diario sono relativi alla cartella da cui il programma
        # e' stato avviato, come tutti quelli di `logs/`: `_percorso` li
        # risolverebbe rispetto al file di configurazione, che sta in `src/app/`
        # e non e' dove il diario viene scritto.
        cartella = Path((self.config.get("diario", {}) or {}).get("dir", "logs/diario"))
        if not cartella.is_dir():
            return {"misure": [], "cartella": str(cartella)}

        # Solo il canale `api`: la stessa misura compare anche come record
        # `radio`, ma li' c'e' il dato grezzo senza l'interpretazione — dove sta
        # la risonanza, quanto e' lontana dalla banda. Tenerli entrambi
        # significherebbe mostrare ogni misura due volte, una delle quali muta.
        interessanti = {
            ("api", "profila_tag"): "profilazione",
            ("api", "campagna"): "campagna",
            ("api", "prova_lettura"): "prova di lettura",
            ("api", "diagnostica_antenna"): "adattamento",
            ("api", "salute"): "salute",
        }
        misure: list[dict[str, Any]] = []
        # Dal file piu' recente all'indietro: si smette appena si ha abbastanza.
        for file in sorted(cartella.glob("diario_*.jsonl"), reverse=True):
            try:
                record = list(leggi_diario(file))
            except OSError:
                continue
            for voce in reversed(record):
                genere = interessanti.get((voce.get("canale"), voce.get("nome")))
                if genere is None:
                    continue
                scheda = self._scheda_misura(genere, voce)
                if scheda is not None:
                    misure.append(scheda)
                if len(misure) >= int(limite):
                    return {"misure": misure, "cartella": str(cartella)}
        return {"misure": misure, "cartella": str(cartella)}

    @staticmethod
    def _scheda_misura(genere: str, voce: Mapping[str, Any]) -> dict[str, Any] | None:
        """Una riga di diario ridotta a cio' che si guarda mesi dopo."""
        dati = voce.get("dati") or {}
        corpo = dati.get("dati") if isinstance(dati.get("dati"), Mapping) else dati
        risposta = dati.get("risposta") if isinstance(dati.get("risposta"), Mapping) else {}
        scheda: dict[str, Any] = {
            "genere": genere,
            "quando": voce.get("t", ""),
            "durata_ms": voce.get("durata_ms"),
            "ok": dati.get("ok", dati.get("stato") == 200),
        }

        if genere == "adattamento":
            fonte = corpo if corpo.get("measurements") else (risposta.get("data") or {})
            misure = fonte.get("measurements") or []
            if not misure:
                return None
            richiesta = fonte.get("request") or {}
            scheda.update(
                {
                    "antenna": richiesta.get("antenna") or risposta.get("antenna"),
                    "punti": len(misure),
                    "vswr_peggiore": fonte.get("worst_vswr"),
                    "risonanza": fonte.get("risonanza"),
                    "da_khz": min(m["frequency_khz"] for m in misure),
                    "a_khz": max(m["frequency_khz"] for m in misure),
                    # Cosa c'era sull'antenna. Senza, tre curve diverse non si
                    # distinguono da tre antenne diverse.
                    "nota": risposta.get("nota") or corpo.get("nota") or "",
                }
            )
            return scheda

        if genere == "profilazione":
            profilo = (risposta or {}).get("profilo") or {}
            if not profilo:
                return None
            scheda.update(
                {
                    "user_bytes": profilo.get("user_bytes"),
                    "tier": profilo.get("tier"),
                    "adatto": profilo.get("suitable"),
                    "epc": profilo.get("epc", ""),
                }
            )
            return scheda

        if genere == "campagna":
            risultati = (risposta or {}).get("risultati") or []
            scheda.update(
                {
                    "configurazioni": len(risultati),
                    "consigliata": ((risposta or {}).get("consigliata") or {}).get(
                        "configurazione", ""
                    ),
                }
            )
            return scheda if risultati else None

        if genere == "prova di lettura":
            if not risposta:
                return None
            scheda.update(
                {
                    "tag": risposta.get("tag"),
                    "letture_al_secondo": risposta.get("letture_al_secondo"),
                    "gen2": risposta.get("gen2") or {},
                }
            )
            return scheda

        if genere == "salute":
            corpo_salute = risposta.get("data") if isinstance(risposta.get("data"), Mapping) else {}
            scheda.update(
                {
                    "antenne": (corpo_salute or {}).get("antennas_connected"),
                    "firmware": ((corpo_salute or {}).get("firmware_info") or {}),
                }
            )
            return scheda
        return None

    # -- registro ------------------------------------------------------------
    def registro(self, limite: int = 100) -> dict[str, Any]:
        return {"eventi": list(self.db.recent_events(limit=int(limite)))}

    def parco_tag(self) -> dict[str, Any]:
        """I tag censiti, raggruppati per stato."""
        gruppi = {}
        for stato_tag in TagState:
            gruppi[stato_tag.value] = self.db.tags_by_state(stato_tag)
        return {"per_stato": {k: len(v) for k, v in gruppi.items()}, "gruppi": gruppi}

    # -- etichette -----------------------------------------------------------
    def etichetta(self, container_id: int) -> dict[str, Any]:
        """ZPL di un contenitore, pronto per l'anteprima o per la stampa."""
        record = self.db.get_container(int(container_id))
        voce = next(
            (item for item in self.pending if item["container_id"] == int(container_id)), None
        )
        # Le avvertenze stanno nel payload in corso di scrittura, non nell'archivio:
        # se il contenitore e' di un'accettazione precedente si stampa senza.
        contenuto = content_from_record(
            record,
            external_ref=self.external_ref,
            flags=voce["payload"].flags if voce else SpecimenFlags.NONE,
            lab_name=str(self.lims_cfg.get("lab_name", "")),
        )
        modello = self.lims_cfg.get("label", {}) or {}
        template = LabelTemplate(
            width_mm=float(modello.get("width_mm", 50)),
            height_mm=float(modello.get("height_mm", 30)),
            dpi=int(modello.get("dpi", 203)),
            copies=int(modello.get("copies", 1)),
        )
        return {
            "zpl": render_zpl(contenuto, template),
            "etichetta": record.label,
            # I campi servono all'anteprima: nel browser non si puo' rasterizzare
            # lo ZPL, e mostrare un disegno inventato sarebbe peggio che mostrare
            # i dati che ci finiranno dentro.
            "contenuto": {
                "paziente": contenuto.display_name,
                "codice_fiscale": contenuto.codice_fiscale,
                "accettazione": contenuto.accession_id,
                "contenitore": contenuto.label,
                "epc": contenuto.epc,
                "riferimento": contenuto.external_ref,
                "data_prelievo": (
                    contenuto.data_prelievo.isoformat() if contenuto.data_prelievo else ""
                ),
                "laboratorio": contenuto.lab_name,
                "avvertenze": contenuto.warnings,
            },
            "modello": {
                "larghezza_mm": template.width_mm,
                "altezza_mm": template.height_mm,
                "dpi": template.dpi,
                "copie": template.copies,
            },
            "stampante": str(modello.get("printer_host", "")),
        }

    def stampa_etichetta(self, container_id: int) -> dict[str, Any]:
        modello = self.lims_cfg.get("label", {}) or {}
        host = str(modello.get("printer_host", "")).strip()
        if not host:
            raise WorkflowError(
                "nessuna stampante configurata: impostare lims.label.printer_host"
            )
        prodotto = self.etichetta(container_id)
        stampante = NetworkLabelPrinter(host, int(modello.get("printer_port", 9100)))
        stampante.send(prodotto["zpl"])
        return {"stampata": prodotto["etichetta"], "stampante": host}
