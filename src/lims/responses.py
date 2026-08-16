"""Lettura delle `ServiceResponse` senza conoscere il driver.

`RFIDService` e `RFIDProcessClient` restituiscono la stessa forma
(`docs/CONTRATTO_SERVICE.md:130-149`), quindi questi helper funzionano
identici in processo e attraverso il canale JSON-RPC.

Le chiavi di `data["results"]` sono stringhe anche quando rappresentano numeri di
antenna: `_json_safe` normalizza le chiavi con `str()` prima di serializzare.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "ServiceCallError",
    "antenna_results",
    "best_antenna",
    "first_read",
    "inventory_epcs",
    "inventory_tags",
    "raise_for_status",
    "response_error",
]


class ServiceCallError(RuntimeError):
    """Un'operazione del servizio e' fallita.

    Conserva la risposta completa: la diagnosi per antenna serve all'operatore
    per capire se il problema e' il tag, la posizione o l'antenna.
    """

    def __init__(self, message: str, response: Any = None):
        super().__init__(message)
        self.response = response


def response_error(response: Any) -> str:
    """Messaggio leggibile dell'errore di una risposta fallita."""
    error = getattr(response, "error", None) or {}
    if isinstance(error, Mapping):
        message = error.get("message") or error.get("type")
        if message:
            return str(message)
    operation = getattr(response, "operation", "operazione")
    return f"{operation} fallita senza dettagli"


def raise_for_status(response: Any, context: str = "") -> Any:
    """Solleva `ServiceCallError` se la risposta non e' andata a buon fine."""
    if getattr(response, "ok", False):
        return response
    prefisso = f"{context}: " if context else ""
    raise ServiceCallError(f"{prefisso}{response_error(response)}", response)


def _data(response: Any) -> Mapping[str, Any]:
    data = getattr(response, "data", None)
    return data if isinstance(data, Mapping) else {}


def antenna_results(response: Any) -> dict[int, Mapping[str, Any]]:
    """Esiti per antenna, con le chiavi riportate a interi."""
    results = _data(response).get("results") or {}
    normalizzati: dict[int, Mapping[str, Any]] = {}
    for chiave, valore in results.items():
        try:
            normalizzati[int(chiave)] = valore
        except (TypeError, ValueError):
            continue
    return normalizzati


def first_read(response: Any) -> tuple[int, bytes]:
    """Primo esito di lettura riuscito, come `(antenna, dati)`.

    Le antenne sono esaminate in ordine crescente perche' `read_try_all_antennas`
    le prova tutte e piu' d'una puo' riuscire: senza un ordine stabile lo stesso
    tag darebbe risultati diversi da un giro all'altro.
    """
    esiti = antenna_results(response)
    for antenna in sorted(esiti):
        esito = esiti[antenna]
        if esito.get("ok") and esito.get("data"):
            return antenna, bytes.fromhex(str(esito["data"]))
    raise ServiceCallError("nessuna antenna ha restituito dati", response)


def best_antenna(response: Any) -> int | None:
    """Antenna con RSSI migliore nell'ultimo inventory, se disponibile."""
    migliore: tuple[int, int] | None = None
    for tag in inventory_tags(response):
        rssi = tag.get("rssi")
        antenna = tag.get("antenna_id")
        if rssi is None or antenna is None:
            continue
        if migliore is None or rssi > migliore[1]:
            migliore = (int(antenna), int(rssi))
    return migliore[0] if migliore else None


def inventory_epcs(response: Any) -> list[str]:
    """EPC unici visti dall'ultimo inventory, in maiuscolo."""
    return [str(epc).upper() for epc in _data(response).get("unique_epcs", [])]


def inventory_tags(response: Any) -> list[dict[str, Any]]:
    """Record grezzi dei tag, con metadati per antenna (RSSI, conteggi)."""
    return [dict(tag) for tag in _data(response).get("tags", []) if isinstance(tag, Mapping)]
