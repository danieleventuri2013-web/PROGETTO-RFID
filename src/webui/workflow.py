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
import logging
import re
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from lims.codec import FIXATIVES, MATERIALS, SITES, SpecimenFlags, TagPayload
from lims.crypto import (
    Keyring,
    PayloadAuthenticationError,
    PayloadFormatError,
    UnknownKeyError,
    max_plaintext_bytes,
)
from lims.db import LimsDatabase
from lims.labels import LabelTemplate, NetworkLabelPrinter, content_from_record, render_zpl
from lims.manifest import build_manifest, open_manifest, reconcile, seal_manifest
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
from lims.sealing import ClosureProof, SealingPolicy, SealingSession, default_passes
from lims.tagio import TagIO
from rfid_silion.service import AntennaPower, ReaderSettings

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
        # Distinta ricevuta, sul lato destinatario
        self.distinta: Any = None
        # Campagna di misura: gli EPC dichiarati dentro e fuori dal contenitore
        self.campagna_epcs: dict[str, list[str]] = {"dentro": [], "fuori": []}

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
        )

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

    def chiudi(self) -> None:
        self.db.close()

    # -- descrizione della postazione ---------------------------------------
    def descrivi(self) -> dict[str, Any]:
        """Tutto quello che serve al frontend per disegnarsi la prima volta."""
        geometria = self.config.get("geometry", {}) or {}
        webui = self.config.get("webui", {}) or {}
        return {
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
            "watch_interval_ms": int(webui.get("watch_interval_ms", 900)),
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
        return {
            "avvio": avvio.to_dict(),
            "configurazione_ok": bool(configurazione.ok),
            "configurazione_errore": (configurazione.error or {}).get("message", ""),
        }

    def disconnetti(self) -> dict[str, Any]:
        return {"stop": self.backend.stop().to_dict()}

    def stato(self) -> dict[str, Any]:
        return self.backend.snapshot()

    # -- accettazione --------------------------------------------------------
    def registra_accettazione(self, dati: Mapping[str, Any]) -> dict[str, Any]:
        """Registra paziente, accettazione e reperto; pianifica i contenitori."""
        try:
            paziente = Patient(
                codice_fiscale=validate_codice_fiscale(str(dati.get("codice_fiscale", ""))),
                cognome=str(dati.get("cognome", "")),
                nome=str(dati.get("nome", "")),
                sesso=Sex(str(dati.get("sesso", "X")).upper()),
            )
        except (ValueError, TypeError) as exc:
            raise WorkflowError(str(exc)) from exc

        totale = int(dati.get("contenitori", 1))
        if not 1 <= totale <= 255:
            raise WorkflowError("il numero di contenitori deve essere compreso tra 1 e 255")

        flags = SpecimenFlags.NONE
        for nome_flag in dati.get("avvertenze", []) or []:
            try:
                flags |= SpecimenFlags[str(nome_flag).upper()]
            except KeyError as exc:
                raise WorkflowError(f"avvertenza sconosciuta: {nome_flag}") from exc

        materiale = int(dati.get("material_code", 0))
        fissativo = int(dati.get("fixative_code", 0))
        sede = int(dati.get("site_code", 0))
        riferimento = str(dati.get("external_ref", "")).strip()

        # Cinque inserimenti che devono restare uno solo: se un'altra richiesta
        # si infilasse in mezzo, due accettazioni potrebbero prendersi lo stesso
        # numero.
        with self._scrittura:
            try:
                patient_id, accession_id, specimen_id, container_ids = self._inserisci(
                    paziente, dati, materiale, fissativo, sede, totale, riferimento
                )
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc

        self.specimen_id = specimen_id
        self.accession_id = accession_id
        self.conteggio_confermato = False
        self.external_ref = riferimento
        self.pending = [
            {
                "container_id": container_id,
                "index": indice,
                "total": totale,
                "epc": "",
                "tid": "",
                "stato": "da_scrivere",
                "payload": TagPayload(
                    codice_fiscale=paziente.codice_fiscale,
                    accession_id=accession_id,
                    container_index=indice,
                    container_total=totale,
                    display_name=paziente.display_name,
                    data_prelievo=_oggi(),
                    material_code=materiale,
                    fixative_code=fissativo,
                    site_code=sede,
                    flags=flags,
                ),
            }
            for indice, container_id in enumerate(container_ids, start=1)
        ]
        del patient_id
        return self.stato_accettazione()

    def _inserisci(
        self,
        paziente: Patient,
        dati: Mapping[str, Any],
        materiale: int,
        fissativo: int,
        sede: int,
        totale: int,
        riferimento: str,
    ) -> tuple[int, int, int, list[int]]:
        patient_id = self.db.upsert_patient(paziente)
        accession_id = self.db.next_accession_id()
        case_id = self.db.create_case(
            Case(
                accession_id=accession_id,
                patient_id=patient_id,
                data_prelievo=_oggi(),
                reparto=str(dati.get("reparto", "")).strip(),
                medico=str(dati.get("medico", "")).strip(),
                note=f"rif. esterno: {riferimento}" if riferimento else "",
            )
        )
        specimen_id = self.db.add_specimen(
            Specimen(
                case_id=case_id,
                descrizione=str(dati.get("descrizione", "")).strip(),
                material_code=materiale,
                fixative_code=fissativo,
                site_code=sede,
            )
        )
        return patient_id, accession_id, specimen_id, self.db.plan_containers(
            specimen_id, totale
        )

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
                }
                for voce in self.pending
            ],
            "scritti": sum(1 for voce in self.pending if voce["epc"]),
            "totale": len(self.pending),
            "prossimo": None
            if prossimo is None
            else {
                "container_id": prossimo["container_id"],
                "index": prossimo["index"],
                "total": prossimo["total"],
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
                voce = {
                    "container_id": record.container_id,
                    "index": record.index,
                    "total": record.total,
                    "epc": record.epc or "",
                    "tid": record.tid or "",
                    "stato": "scritto" if record.epc else "da_scrivere",
                    "payload": replace(
                        base, container_index=record.index, container_total=record.total
                    ),
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
        self.conteggio_confermato = True
        return {"conteggio_confermato": True}

    def sorveglia_piatto(self) -> dict[str, Any]:
        """Guarda se c'e' un contenitore sulla postazione di scrittura.

        E' quello che permette alla scena di reagire da sola quando l'operatore
        appoggia il contenitore, senza un pulsante per dire «l'ho messo». La
        lettura serve comunque: la guardia di scrittura pretende un inventory
        con un tag solo.
        """
        from rfid_silion.service import InventoryRequest
        from lims.responses import inventory_epcs

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

        return {
            "accettazione": accettazione,
            "annullati": len(annullati),
            "gia_scritti": [
                {"etichetta": f"{voce['index']}/{voce['total']}", "epc": voce["epc"]}
                for voce in scritti
            ],
        }

    def annulla_contenitore(self, container_id: int, motivo: str, *, tag_guasto: bool = False):
        """Annulla un contenitore rotto o con tag guasto, e ne crea il sostituto."""
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
        risposta = self.stato_accettazione()
        risposta["sostituto"] = sostituto
        return risposta

    # -- sigillo e spedizione -----------------------------------------------
    def prepara_spedizione(self, destinazione: str) -> dict[str, Any]:
        nome = str(destinazione).strip()
        configurati = _voci_attive(self.config.get("destinatari"))
        if configurati and nome and not self.destinatario(nome):
            raise WorkflowError(
                f"destinatario sconosciuto: {nome}. "
                "Va aggiunto in Impostazioni prima di poter spedire."
            )
        if not nome:
            raise WorkflowError("scegliere il laboratorio destinatario")

        with self._scrittura:
            pronti = self.db.connection.execute(
                "SELECT id FROM containers WHERE state=? ORDER BY id",
                (ContainerState.PROVISIONED.value,),
            ).fetchall()
            if not pronti:
                raise WorkflowError("non ci sono contenitori scritti in attesa di spedizione")
            try:
                shipment_id = self.db.create_shipment(
                    Shipment(destinazione=nome, data=_oggi())
                )
                self.db.add_to_shipment(shipment_id, [riga["id"] for riga in pronti])
            except Exception as exc:  # noqa: BLE001
                raise WorkflowError(str(exc)) from exc

        self.shipment_id = shipment_id
        self.sealing_record = None
        return self.stato_spedizione()

    def stato_spedizione(self) -> dict[str, Any]:
        if self.shipment_id is None:
            return {"shipment_id": None, "contenitori": [], "sigillo": None}
        contenuto = self.db.shipment_contents(self.shipment_id)
        riga = self.db.connection.execute(
            "SELECT destinazione FROM shipments WHERE id=?", (self.shipment_id,)
        ).fetchone()
        nome_destinatario = riga["destinazione"] if riga else ""
        return {
            "shipment_id": self.shipment_id,
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
            "sigillo": self.sealing_record.to_dict() if self.sealing_record else None,
        }

    def sigilla(
        self,
        *,
        on_progress: Callable[[int, int, int], None] | None = None,
        stop_event: Any = None,
    ) -> dict[str, Any]:
        """Certifica il contenuto della scatola chiusa."""
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        contenuto = self.db.shipment_contents(self.shipment_id)
        attesi = [record.epc for record in contenuto if record.epc]
        if not attesi:
            raise WorkflowError("la spedizione non contiene contenitori con EPC assegnato")

        antenne = tuple(self.lims_cfg.get("read_antennas") or (1, 2))
        potenze = tuple(self.lims_cfg.get("seal_powers_cdbm") or (2000, 2500, 2900))
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
        record = sessione.run(
            closure_proof=ClosureProof.OPERATOR,
            on_progress=on_progress,
            stop_event=stop_event,
        )
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

    def esporta_distinta(self) -> tuple[bytes, str]:
        """Distinta cifrata pronta da scaricare: `(contenuto, nome file)`."""
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        distinta = build_manifest(
            self.db,
            self.shipment_id,
            lab_id=int(self.lims_cfg.get("lab_id", 0)),
            operator=self.operatore,
            sealing=self.sealing_record,
            box_epc=getattr(self.sealing_record, "box_epc", ""),
        )
        blob = seal_manifest(distinta, self.keyring)
        self.db.set_shipment_state(self.shipment_id, ShipmentState.SENT)
        nome = f"distinta_{self.shipment_id}_{_oggi():%Y%m%d}.rfidman"
        return blob, nome

    def bozza_email(self) -> dict[str, Any]:
        """Testo e destinatari del messaggio con cui si manda la distinta.

        **L'allegato va messo a mano.** Una pagina web non puo' allegare un file
        a un messaggio del programma di posta: puo' solo preparare indirizzo,
        oggetto e testo. Dirlo qui, invece di lasciar credere che sia partito
        tutto, e' l'unico modo perche' nessuno spedisca una mail vuota.
        """
        if self.shipment_id is None:
            raise WorkflowError("preparare prima la spedizione")
        stato = self.stato_spedizione()
        destinatario = stato.get("destinatario") or {}
        laboratorio = self.laboratorio()
        contenitori = stato.get("attesi", 0)
        nome_file = f"distinta_{self.shipment_id}_{_oggi():%Y%m%d}.rfidman"

        righe = [
            f"Spedizione {self.shipment_id} del {_oggi():%d/%m/%Y}"
            f" da {laboratorio['nome'] or 'laboratorio mittente'}.",
            "",
            f"Contenitori spediti: {contenitori}.",
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
        try:
            self.distinta = open_manifest(bytes(blob), self.keyring)
        except UnknownKeyError as exc:
            raise WorkflowError(
                f"{exc} — la chiave va concordata con il laboratorio mittente"
            ) from exc
        except PayloadAuthenticationError as exc:
            raise WorkflowError(f"distinta non autentica: {exc}") from exc
        except PayloadFormatError as exc:
            raise WorkflowError(f"non e' una distinta di questo sistema: {exc}") from exc

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

    def leggi_volume(self) -> dict[str, Any]:
        """Legge la scatola arrivata e la confronta con la distinta."""
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
        return risposta

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
        from rfid_silion.service import Gen2Settings

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
        return risposta.to_dict()

    def diagnostica_antenna(self, antenna: int) -> dict[str, Any]:
        """Return loss per frequenza: il dato da portare al fornitore.

        Le SLP1027 sono caratterizzate dal datasheet **solo da 900 MHz in su**,
        mentre la banda EU sta 35 MHz piu' in basso. Questa misura dice come si
        comportano davvero dove le usiamo.
        """
        from rfid_silion.service import AntennaDiagnosticsRequest

        risposta = self.backend.antenna_diagnostics(
            AntennaDiagnosticsRequest(antenna=int(antenna))
        )
        if not risposta.ok:
            raise WorkflowError((risposta.error or {}).get("message", "misura non riuscita"))
        return risposta.to_dict()

    def profila_tag(self) -> dict[str, Any]:
        """Misura TID e USER memory del tag sulla postazione di scrittura."""
        from lims.profiler import profile_tag, summarize

        accesso = self.config.get("tag_access", {}) or {}
        profilo = profile_tag(
            self.backend,
            antennas=tuple(self.lims_cfg.get("write_antennas") or (1,)),
            access_password_hex=accesso.get("access_password_hex", "00000000"),
            timeout_ms=accesso.get("timeout_ms", 1000),
        )
        return {"profilo": profilo.to_dict(), "riassunto": summarize(profilo)}

    def rileva_controllo(self, posizione: str) -> dict[str, Any]:
        """Fotografa gli EPC presenti, per dichiararli dentro o fuori.

        La campagna sceglie la potenza piu' bassa che legge tutto *dentro* senza
        leggere niente *fuori*: senza i tag di controllo fuori, sceglierebbe
        sempre la potenza massima e il volume di lettura non sarebbe definito.
        """
        from rfid_silion.service import InventoryRequest
        from lims.responses import inventory_epcs

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

    def esegui_campagna(
        self,
        parametri: Mapping[str, Any],
        *,
        on_progress: Callable[[int, int, Any], None] | None = None,
        stop_event: Any = None,
    ) -> dict[str, Any]:
        from lims.campaign import CampaignConfig, ReadCampaign, grid_passes

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
    )
    _CAMPI_OPERATORE = ("codice", "cognome", "nome", "email")
    _CAMPI_DESTINATARIO = ("nome", "codice", "email", "referente", "indirizzo", "citta")

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
            for campo in ("email", "email_referente"):
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
    def cerca_paziente(self, query: str) -> dict[str, Any]:
        return {"risultati": self.db.search_patients(query)}

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
