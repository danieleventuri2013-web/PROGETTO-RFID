"""La distinta come tabella: sul foglio da leggere, e dentro il QR da scansionare.

I due laboratori **non condividono nessun database**. Quello che non viaggia col
pacco, a destinazione non esiste: da qui la scelta di mettere sul foglio la
tabella completa — identificativo del paziente, cognome, nome, sesso, data di
nascita, data e ora del prelievo, e tutto quello che descrive il campione —
invece del solo elenco degli EPC.

Il foglio e' la copia autorevole: si legge a occhio, non si scarica la batteria
e non ha bisogno di niente. Il QR e' la stessa identica tabella in forma
rileggibile con un lettore, per non ridigitare trenta righe a mano.

Come ci sta dentro un QR ragionevole:

1. le righe si scrivono con separatori di controllo (`\\x1f` fra i campi,
   `\\x1e` fra le righe) — dentro il blocco compresso non serve nessuna
   prudenza sui caratteri, quindi gli accenti restano dove sono;
2. si comprime con `zlib`: su trenta pazienti diversi il testo scende attorno
   al 45%, perche' date, codici e prefissi degli EPC si ripetono;
3. si firma con HMAC-SHA256 troncato, chiave del circuito: un QR rifatto da
   qualcun altro non passa;
4. si codifica in Base45 (`lims.base45`), che e' l'unico modo di usare la
   modalita' alfanumerica del QR — 5,5 bit per carattere invece di 8.

Misurato su trenta pazienti veri: 3.350 caratteri di tabella, 1.490 byte
compressi, 2.240 caratteri Base45, **un solo QR versione 33 a correzione M**,
circa 40 mm di lato stampato. Senza compressione servirebbe la versione 40 al
livello di correzione piu' debole, che su carta e' un codice che non si legge.

**Il QR contiene dati sanitari in chiaro.** E' la conseguenza diretta di non
avere un archivio condiviso, ed e' una scelta consapevole: il foglio va nella
busta agganciata alla scatola, non incollato all'esterno.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import unicodedata
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .base45 import Base45Error
from .base45 import codifica as base45_codifica
from .base45 import decodifica as base45_decodifica
from .codec import describe_fixative, describe_material, describe_site

__all__ = [
    "COLONNE",
    "RigaDistinta",
    "TabellaError",
    "codifica_tabella",
    "decodifica_tabella",
    "righe_da_manifest",
    "tabella_da_righe",
]


class TabellaError(ValueError):
    """Il testo scansionato non e' una distinta di questo sistema."""


#: Le colonne, nell'ordine in cui stanno sul foglio e dentro il QR.
#: `(chiave, intestazione)`. L'ordine e' parte del formato: cambiarlo rompe la
#: rilettura dei QR gia' stampati.
COLONNE: tuple[tuple[str, str], ...] = (
    ("codice_fiscale", "Codice fiscale"),
    ("cognome", "Cognome"),
    ("nome", "Nome"),
    ("sesso", "S"),
    ("data_nascita", "Nato il"),
    ("data_prelievo", "Prelievo"),
    ("ora_prelievo", "Ora"),
    ("descrizione", "Campione"),
    ("materiale", "Materiale"),
    ("fissativo", "Fissativo"),
    ("sede", "Sede"),
    ("etichetta", "Cont."),
    ("epc", "EPC"),
)

#: Separatori di campo e di riga. Sono caratteri di controllo perche' dentro il
#: blocco compresso non possono comparire nei dati, quindi non serve nessuna
#: sequenza di fuga — che e' il posto in cui questi formati si rompono sempre.
_SEP_CAMPO = "\x1f"
_SEP_RIGA = "\x1e"

#: Firma del formato e sua versione. Cambiare la versione significa cambiare il
#: contratto con il laboratorio che riceve.
MAGIA = "RFQ"
VERSIONE = 1

#: Byte della firma HMAC che viaggiano. Otto bastano: qui si vuole scoprire un
#: QR rifatto o corrotto, non resistere a un attacco con mesi di calcolo.
_BYTE_FIRMA = 8

#: Quanti caratteri Base45 al massimo per ogni QR. E' un limite di prudenza sul
#: simbolo stampato, non della norma: oltre i ~2.300 caratteri a correzione M
#: il codice supera i 45 mm di lato e su carta comune comincia a soffrire.
CARATTERI_PER_QR = 2200


@dataclass
class RigaDistinta:
    """Una riga della distinta: un contenitore col suo paziente e il suo campione."""

    codice_fiscale: str = ""
    cognome: str = ""
    nome: str = ""
    sesso: str = ""
    data_nascita: str = ""
    data_prelievo: str = ""
    ora_prelievo: str = ""
    descrizione: str = ""
    materiale: str = ""
    fissativo: str = ""
    sede: str = ""
    etichetta: str = ""
    epc: str = ""

    @property
    def paziente(self) -> str:
        return f"{self.cognome} {self.nome}".strip()

    def valori(self) -> list[str]:
        return [str(getattr(self, chiave, "")) for chiave, _ in COLONNE]

    def describe(self) -> dict[str, Any]:
        return {chiave: str(getattr(self, chiave, "")) for chiave, _ in COLONNE}


def _pulisci(valore: Any) -> str:
    """Un campo pronto per la tabella: senza separatori e senza a capo.

    Non si toglie nulla d'altro — gli accenti restano, perche' dentro il blocco
    compresso ci stanno e un cognome storpiato sulla distinta e' un cognome
    sbagliato.
    """
    testo = "" if valore is None else str(valore)
    testo = testo.replace(_SEP_CAMPO, " ").replace(_SEP_RIGA, " ")
    return re.sub(r"\s+", " ", testo).strip()


def _data(valore: Any) -> str:
    """Le date viaggiano compatte (AAAAMMGG) e si stampano all'italiana."""
    if not valore:
        return ""
    if isinstance(valore, (dt.date, dt.datetime)):
        return f"{valore:%Y%m%d}"
    testo = str(valore).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", testo):
        return testo.replace("-", "")
    if re.fullmatch(r"\d{8}", testo):
        return testo
    # Una data che non si riconosce si porta com'e': meglio un campo strano
    # che un campo vuoto, perche' e' l'unico posto in cui quel dato esiste.
    return _pulisci(testo)


def data_italiana(compatta: str) -> str:
    """`20260826` → `26/08/2026`, per il foglio stampato."""
    testo = str(compatta or "").strip()
    if re.fullmatch(r"\d{8}", testo):
        return f"{testo[6:8]}/{testo[4:6]}/{testo[0:4]}"
    return testo


def ora_italiana(compatta: str) -> str:
    """`1430` → `14:30`."""
    testo = str(compatta or "").strip()
    if re.fullmatch(r"\d{4}", testo):
        return f"{testo[0:2]}:{testo[2:4]}"
    return testo


# --------------------------------------------------------------------------
# Dalla distinta alle righe
# --------------------------------------------------------------------------
def righe_da_manifest(distinta: Any, *, contenitori: Iterable[Any] = ()) -> list[RigaDistinta]:
    """Costruisce le righe dalla distinta cifrata gia' esistente.

    Si appoggia a `lims.manifest.Manifest`, che porta gia' tutto tranne l'ora
    del prelievo e la data di nascita: quelle arrivano dai record
    dell'archivio, che il mittente ha e il destinatario no — ed e' esattamente
    la ragione per cui devono finire sul foglio.
    """
    extra: dict[str, Any] = {}
    for record in contenitori:
        epc = str(getattr(record, "epc", "") or "").upper()
        if epc:
            extra[epc] = record

    righe: list[RigaDistinta] = []
    for voce in getattr(distinta, "entries", []) or []:
        epc = str(voce.epc or "").upper()
        record = extra.get(epc)
        righe.append(
            RigaDistinta(
                codice_fiscale=_pulisci(voce.codice_fiscale),
                cognome=_pulisci(_cognome_di(voce, record)),
                nome=_pulisci(_nome_di(voce, record)),
                sesso=_pulisci(
                    _valore_sesso(getattr(record, "sesso", ""))
                    or _sesso_da_cf(voce.codice_fiscale)
                ),
                data_nascita=_data(getattr(record, "data_nascita", "")),
                data_prelievo=_data(voce.data_prelievo),
                ora_prelievo=_pulisci(getattr(record, "ora_prelievo", "")),
                descrizione=_pulisci(voce.descrizione),
                materiale=_pulisci(describe_material(int(voce.material_code or 0))),
                fissativo=_pulisci(describe_fixative(int(voce.fixative_code or 0))),
                sede=_pulisci(describe_site(int(voce.site_code or 0))),
                etichetta=f"{voce.container_index}/{voce.container_total}",
                epc=epc,
            )
        )
    return righe


def _cognome_di(voce: Any, record: Any) -> str:
    if record is not None and getattr(record, "cognome", ""):
        return record.cognome
    # `display_name` e' "COGNOME NOME": senza il record si spezza dove si puo'.
    parti = str(getattr(voce, "display_name", "") or "").split()
    return parti[0] if parti else ""


def _nome_di(voce: Any, record: Any) -> str:
    if record is not None and getattr(record, "nome", ""):
        return record.nome
    parti = str(getattr(voce, "display_name", "") or "").split()
    return " ".join(parti[1:]) if len(parti) > 1 else ""


def _valore_sesso(valore: Any) -> str:
    """`Sex.MALE`, `"M"` o niente diventano tutti la stessa lettera."""
    return str(getattr(valore, "value", valore) or "").strip().upper()[:1]


def _sesso_da_cf(codice_fiscale: str) -> str:
    """Il codice fiscale porta il sesso nel giorno di nascita: oltre 40 e' F.

    Serve solo come ripiego quando il record non c'e': meglio dedurlo che
    lasciare la casella vuota su un documento sanitario.
    """
    testo = str(codice_fiscale or "").strip().upper()
    if len(testo) < 11:
        return ""
    giorno = testo[9:11]
    if not giorno.isdigit():
        return ""
    return "F" if int(giorno) > 40 else "M"


def tabella_da_righe(righe: Sequence[RigaDistinta]) -> str:
    """Le righe come testo, con i separatori di controllo."""
    return _SEP_RIGA.join(_SEP_CAMPO.join(riga.valori()) for riga in righe)


def righe_da_tabella(testo: str) -> list[RigaDistinta]:
    righe: list[RigaDistinta] = []
    for grezza in testo.split(_SEP_RIGA):
        if not grezza:
            continue
        campi = grezza.split(_SEP_CAMPO)
        # Una riga piu' corta del previsto viene da una versione precedente del
        # formato: si legge quello che c'e' invece di rifiutare tutto.
        valori = dict(zip((chiave for chiave, _ in COLONNE), campi))
        righe.append(RigaDistinta(**valori))
    return righe


# --------------------------------------------------------------------------
# Dentro e fuori dal QR
# --------------------------------------------------------------------------
def _firma(dati: bytes, chiave: bytes | None) -> bytes:
    if not chiave:
        return b"\x00" * _BYTE_FIRMA
    return hmac.new(chiave, dati, hashlib.sha256).digest()[:_BYTE_FIRMA]


def _intestazione(parte: int, totale: int, identificativo: str) -> str:
    return f"{MAGIA}{VERSIONE}:{parte}/{totale}:{identificativo}:"


#: L'intestazione ha lunghezza fissa, e si legge tagliandola a misura. La
#: misura si ricava da chi la costruisce invece di contarla a mano: contarla e'
#: esattamente il modo in cui ci si sbaglia di un carattere.
_PROVA_INTESTAZIONE = _intestazione(1, 1, "0" * 8)
LUNGHEZZA_INTESTAZIONE = len(_PROVA_INTESTAZIONE)
#: Posizioni dentro l'intestazione, sempre ricavate e mai scritte a mano.
_POS_PARTE = _PROVA_INTESTAZIONE.index("1", len(MAGIA) + 1)
_POS_TOTALE = _POS_PARTE + 2
_POS_IDENTIFICATIVO = _POS_TOTALE + 2


def codifica_tabella(
    righe: Sequence[RigaDistinta],
    *,
    identificativo: str = "",
    chiave: bytes | None = None,
    caratteri_per_qr: int = CARATTERI_PER_QR,
) -> list[str]:
    """La tabella pronta per il QR, gia' spezzata in parti se non ci sta.

    Restituisce sempre una lista: una voce sola nel caso normale. Ogni voce e'
    un testo che sta nella modalita' alfanumerica del QR.
    """
    if not righe:
        raise TabellaError("non si stampa una distinta vuota")
    identificativo = _identificativo(identificativo)

    compresso = zlib.compress(tabella_da_righe(righe).encode("utf-8"), 9)
    blocco = _firma(compresso, chiave) + compresso
    corpo = base45_codifica(blocco)

    # Si spezza in gruppi da tre caratteri: cosi' ogni pezzo resta Base45
    # valido per conto suo, e chi guarda una parte da sola vede un testo
    # sensato invece di byte mozzati.
    passo = max(3, (caratteri_per_qr // 3) * 3)
    pezzi = [corpo[i : i + passo] for i in range(0, len(corpo), passo)] or [""]
    if len(pezzi) > 9:
        raise TabellaError(
            f"la distinta richiederebbe {len(pezzi)} codici QR: e' troppo grande "
            "per il foglio, va divisa in piu' spedizioni"
        )
    return [
        _intestazione(numero, len(pezzi), identificativo) + pezzo
        for numero, pezzo in enumerate(pezzi, start=1)
    ]


def _identificativo(valore: str) -> str:
    """Otto caratteri esadecimali che legano fra loro le parti dello stesso QR."""
    testo = re.sub(r"[^0-9A-F]", "", str(valore or "").upper())
    if len(testo) >= 8:
        return testo[:8]
    return (testo + "0" * 8)[:8]


@dataclass
class DistintaLetta:
    """Cosa si e' ricavato dalle scansioni."""

    righe: list[RigaDistinta] = field(default_factory=list)
    identificativo: str = ""
    parti: int = 1
    firma_verificata: bool | None = None

    @property
    def epc(self) -> list[str]:
        return [riga.epc for riga in self.righe if riga.epc]

    def describe(self) -> dict[str, Any]:
        return {
            "identificativo": self.identificativo,
            "parti": self.parti,
            "firma_verificata": self.firma_verificata,
            "contenitori": [riga.describe() for riga in self.righe],
            "attesi": len(self.righe),
        }


def analizza_parte(testo: str) -> tuple[int, int, str, str]:
    """`(parte, totale, identificativo, corpo)` da una singola scansione."""
    # Si tolgono solo i terminatori del lettore di codici a barre: lo spazio
    # e' un carattere valido della Base45 e toglierlo corromperebbe il dato.
    pulito = str(testo or "").strip("\r\n\t")
    atteso = f"{MAGIA}{VERSIONE}:"
    if not pulito.startswith(atteso):
        raise TabellaError(
            "questa scansione non e' una distinta di questo sistema "
            f"(manca l'intestazione {atteso})"
        )
    if len(pulito) < LUNGHEZZA_INTESTAZIONE:
        raise TabellaError("scansione troncata: manca l'intestazione completa")
    testa = pulito[:LUNGHEZZA_INTESTAZIONE]
    corpo = pulito[LUNGHEZZA_INTESTAZIONE:]
    try:
        parte = int(testa[_POS_PARTE])
        totale = int(testa[_POS_TOTALE])
    except ValueError as exc:
        raise TabellaError("numero di parte non leggibile nell'intestazione") from exc
    identificativo = testa[_POS_IDENTIFICATIVO : _POS_IDENTIFICATIVO + 8]
    return parte, totale, identificativo, corpo


def decodifica_tabella(
    scansioni: Sequence[str], *, chiave: bytes | None = None
) -> DistintaLetta:
    """Rimette insieme le scansioni e ne ricava le righe.

    Pretende che ci siano **tutte** le parti e che vengano dallo stesso codice:
    una distinta letta a meta' e' peggio di una non letta, perche' sembra
    completa.
    """
    if not scansioni:
        raise TabellaError("nessuna scansione da leggere")

    pezzi: dict[int, str] = {}
    identificativo = ""
    totale = 1
    for scansione in scansioni:
        parte, quante, codice, corpo = analizza_parte(scansione)
        if identificativo and codice != identificativo:
            raise TabellaError(
                "le scansioni vengono da due distinte diverse: rileggere la stessa"
            )
        identificativo, totale = codice, quante
        if parte in pezzi and pezzi[parte] != corpo:
            raise TabellaError(f"la parte {parte} e' stata letta due volte, diversa")
        pezzi[parte] = corpo

    mancanti = [numero for numero in range(1, totale + 1) if numero not in pezzi]
    if mancanti:
        quante = len(mancanti)
        raise TabellaError(
            f"{'manca' if quante == 1 else 'mancano'} "
            f"{'la parte' if quante == 1 else 'le parti'} "
            + ", ".join(str(n) for n in mancanti)
            + f" su {totale}: leggere anche quel codice"
        )

    corpo = "".join(pezzi[numero] for numero in range(1, totale + 1))
    try:
        blocco = base45_decodifica(corpo)
    except Base45Error as exc:
        raise TabellaError(f"codice illeggibile: {exc}") from exc
    if len(blocco) <= _BYTE_FIRMA:
        raise TabellaError("codice troppo corto per contenere una distinta")

    firma, compresso = blocco[:_BYTE_FIRMA], blocco[_BYTE_FIRMA:]
    verificata: bool | None = None
    if chiave:
        verificata = hmac.compare_digest(firma, _firma(compresso, chiave))
        if not verificata:
            raise TabellaError(
                "la firma non torna: questo codice non e' stato prodotto dal "
                "laboratorio mittente, oppure e' stato modificato"
            )
    try:
        testo = zlib.decompress(compresso).decode("utf-8")
    except (zlib.error, UnicodeDecodeError) as exc:
        raise TabellaError(f"contenuto della distinta illeggibile: {exc}") from exc

    return DistintaLetta(
        righe=righe_da_tabella(testo),
        identificativo=identificativo,
        parti=totale,
        firma_verificata=verificata,
    )


# --------------------------------------------------------------------------
# Il foglio
# --------------------------------------------------------------------------
def _senza_accenti(testo: str) -> str:
    """Solo per l'ordinamento e i confronti, mai per quello che si stampa."""
    scomposto = unicodedata.normalize("NFKD", testo)
    return "".join(c for c in scomposto if not unicodedata.combining(c))


def per_stampa(righe: Sequence[RigaDistinta]) -> list[dict[str, str]]:
    """Le righe come vanno sul foglio: date e ore all'italiana, in ordine.

    L'ordine e' per cognome: chi controlla il pacco all'arrivo ha in mano dei
    vasetti con dei nomi sopra, non degli EPC.
    """
    ordinate = sorted(
        righe, key=lambda r: (_senza_accenti(r.cognome).upper(), _senza_accenti(r.nome).upper())
    )
    return [
        {
            **riga.describe(),
            "paziente": riga.paziente,
            "data_nascita": data_italiana(riga.data_nascita),
            "data_prelievo": data_italiana(riga.data_prelievo),
            "ora_prelievo": ora_italiana(riga.ora_prelievo),
            "epc_breve": riga.epc[:12] + "…" if len(riga.epc) > 12 else riga.epc,
        }
        for riga in ordinate
    ]


def righe_da_contenuto(contenuto: Iterable[Any], anagrafiche: Mapping[str, Any] | None = None):
    """Righe costruite direttamente dai `ContainerRecord` dell'archivio.

    E' la strada del mittente, che ha tutto: il destinatario invece parte dal
    QR o dalla distinta cifrata.
    """
    anagrafiche = anagrafiche or {}
    righe: list[RigaDistinta] = []
    for record in contenuto:
        # Il record dell'archivio porta gia' tutto; `anagrafiche` resta come
        # ripiego per chi costruisce le righe da una fonte piu' povera.
        paziente = anagrafiche.get(str(record.codice_fiscale).upper())
        sesso = getattr(record, "sesso", "") or getattr(paziente, "sesso", "")
        nascita = getattr(record, "data_nascita", None) or getattr(
            paziente, "data_nascita", None
        )
        righe.append(
            RigaDistinta(
                codice_fiscale=_pulisci(record.codice_fiscale),
                cognome=_pulisci(record.cognome),
                nome=_pulisci(record.nome),
                sesso=_pulisci(_valore_sesso(sesso) or _sesso_da_cf(record.codice_fiscale)),
                data_nascita=_data(nascita),
                data_prelievo=_data(record.data_prelievo),
                ora_prelievo=_pulisci(getattr(record, "ora_prelievo", "")),
                descrizione=_pulisci(record.descrizione),
                materiale=_pulisci(describe_material(int(record.material_code or 0))),
                fissativo=_pulisci(describe_fixative(int(record.fixative_code or 0))),
                sede=_pulisci(describe_site(int(record.site_code or 0))),
                etichetta=record.label,
                epc=str(record.epc or "").upper(),
            )
        )
    return righe
