"""Il lettore simulato, dove i test se lo aspettano da sempre.

L'implementazione e' in `rfid_silion.simulazione`: e' stata spostata li' quando
ha smesso di essere solo attrezzatura da test ed e' diventata anche il banco su
cui gira l'interfaccia operativa senza hardware (`python run.py webui
--simulato`). Un programma che si avvia in produzione non deve importare dalla
cartella dei test.

Questo modulo resta perche' una ventina di test lo importano per nome, e
rinominare l'import in tutti quanti avrebbe cambiato molti file senza cambiare
niente.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rfid_silion.simulazione import FakeTagBackend, SimulatedTag

__all__ = ["FakeTagBackend", "SimulatedTag"]
