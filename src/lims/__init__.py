"""Tracciabilita' dei campioni istologici sopra il servizio RFID.

Questo pacchetto e' un **client** di `rfid_silion.service`: non importa mai
`reader`, `protocol` o `transports`, coerentemente con il confine dichiarato in
`docs/CONTRATTO_SERVICE.md`. Tutto l'hardware resta dietro `RFIDBackend`.

Livelli, dal basso:

* `model`   — entita' di dominio (paziente, accettazione, reperto, contenitore)
* `codec`   — schema binario di EPC e payload, codebook condivisi
* `crypto`  — sigillo AEAD del payload, legato all'EPC e al TID del chip
* `db`      — persistenza SQLite e registro di unicita' degli EPC
* `tagio`   — orchestrazione delle operazioni sul tag tramite il servizio
* `profiler`— scoperta delle capacita' di memoria di un tag sconosciuto
"""

from __future__ import annotations

__version__ = "0.1.0"

from .codec import (
    CODEBOOK_VERSION,
    EPC_SIZE,
    FIXATIVES,
    MATERIALS,
    PAYLOAD_FIXED_SIZE,
    SITES,
    EpcInfo,
    SpecimenFlags,
    TagPayload,
    build_epc,
    describe_fixative,
    describe_material,
    describe_site,
    pack_payload,
    parse_epc,
    unpack_payload,
)
from .model import (
    Case,
    Container,
    ContainerState,
    Patient,
    Sex,
    Shipment,
    ShipmentState,
    Specimen,
    normalize_name,
    validate_codice_fiscale,
)

__all__ = [
    "__version__",
    "CODEBOOK_VERSION",
    "EPC_SIZE",
    "PAYLOAD_FIXED_SIZE",
    "MATERIALS",
    "FIXATIVES",
    "SITES",
    "EpcInfo",
    "SpecimenFlags",
    "TagPayload",
    "build_epc",
    "parse_epc",
    "pack_payload",
    "unpack_payload",
    "describe_material",
    "describe_fixative",
    "describe_site",
    "Patient",
    "Case",
    "Specimen",
    "Container",
    "ContainerState",
    "Shipment",
    "ShipmentState",
    "Sex",
    "normalize_name",
    "validate_codice_fiscale",
]
