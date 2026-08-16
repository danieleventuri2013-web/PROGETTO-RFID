#!/usr/bin/env bash
# ===========================================================================
#  PROGETTO-RFID — avvio unico (Linux / macOS)
#
#  Equivalente di AVVIA.bat. L'ordine delle voci e' quello in cui vanno usate
#  la prima volta: 1 e 2 non richiedono hardware, dalla 3 in poi serve il lettore.
# ===========================================================================
set -u
cd "$(dirname "$0")"

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo
    echo "  [ERRORE] Python non e' installato, o non e' nel PATH."
    echo "  Installarlo dal gestore pacchetti oppure da https://www.python.org/downloads/"
    echo
    exit 2
fi

pausa() {
    echo
    echo "---------------------------------------------------------------------------"
    read -r -p "Premere INVIO per tornare al menu..." _
}

while true; do
    clear
    cat <<'MENU'
===========================================================================
                PROGETTO-RFID  -  Tracciabilita' campioni
===========================================================================

  SENZA LETTORE
    1)  Verifica del programma  (test automatici, nessun hardware)
    2)  Anteprima di un'etichetta stampata  (file ZPL in logs/)

  CON IL LETTORE COLLEGATO
    3)  Diagnosi del lettore e delle antenne
    4)  Profilazione dei tag        <-- il primo passo con tag nuovi
    5)  Campagna di misura          <-- taratura potenza e parametri radio

  USO QUOTIDIANO
    6)  Interfaccia operativa nel browser   <-- questa, tutti i giorni

  STRUMENTI
    7)  GUI di collaudo del lettore
    8)  Test read/write da riga di comando  (Step 1)
    9)  Service JSON-RPC per il framework principale
   10)  Tracciabilita' campioni - GUI Tkinter  (collaudo)

    0)  Esci
MENU
    echo
    read -r -p "  Scelta: " scelta

    case "$scelta" in
        1)
            clear
            echo "--- Verifica del programma: nessun hardware richiesto ---"; echo
            "$PY" run.py tests
            pausa
            ;;
        2)
            clear
            echo "--- Anteprima di un'etichetta ---"; echo
            echo "Genera un'etichetta di esempio e la salva come file ZPL, senza stampante."
            echo
            "$PY" - <<'PYEOF'
import datetime
import sys
from pathlib import Path

sys.path.insert(0, "src")
from lims.codec import SpecimenFlags
from lims.labels import FileLabelPrinter, LabelContent, print_container_label

Path("logs").mkdir(exist_ok=True)
contenuto = LabelContent(
    display_name="DELLA VALLE GIANFRANCO",
    codice_fiscale="MRTMTT25D09F205Z",
    accession_id=987654,
    container_index=2,
    container_total=3,
    epc="0100A5000F12060203DEADBE",
    external_ref="CHIR-2026-0042",
    material_code=3,
    fixative_code=1,
    site_code=2,
    data_prelievo=datetime.date.today(),
    flags=SpecimenFlags.INFECTIOUS,
    lab_name="Anatomia Patologica",
)
percorso = Path("logs/etichetta_esempio.zpl")
print_container_label(FileLabelPrinter(percorso), contenuto)
print("Etichetta salvata in", percorso.resolve())
PYEOF
            pausa
            ;;
        3)
            clear
            echo "--- Diagnosi del lettore ---"; echo
            "$PY" run.py health --with-inventory
            pausa
            ;;
        4)
            clear
            echo "--- Profilazione dei tag ---"; echo
            echo "Misura quanti dati entrano nei tag che hai in mano: modello del chip,"
            echo "dimensione della memoria utente, presenza del numero di serie di fabbrica."
            echo
            read -r -p "  Posizionare UN SOLO tag davanti alle antenne, poi INVIO..." _
            "$PY" run.py tag-profile
            pausa
            ;;
        5)
            clear
            cat <<'CAMPAGNA'
--- Campagna di misura ---

Tara la configurazione radio sui dati invece che a sentimento.

  PRIMA di procedere:
    - caricare il contenitore con i campioni, chiuso come in esercizio;
    - mettere di proposito qualche tag DI CONTROLLO FUORI dal contenitore,
      alle distanze che si vogliono escludere.

  Senza tag di controllo esterni la campagna sceglierebbe sempre la potenza
  massima, che e' anche quella che legge il tavolo accanto.

  Esempio:
    python run.py campaign --inside-from-shipment 1 --outside AABB... CCDD...
CAMPAGNA
            pausa
            ;;
        6)
            clear
            echo "--- Interfaccia operativa ---"; echo
            echo "Si apre nel browser. L'indirizzo vale solo su questa macchina e"
            echo "contiene un token che cambia a ogni avvio."
            echo "Per chiudere: tornare qui e premere CTRL+C."; echo
            "$PY" run.py webui
            pausa
            ;;
        7) clear; "$PY" run.py service-gui; pausa ;;
        8) clear; "$PY" run.py step1; pausa ;;
        9)
            clear
            echo "--- Service JSON-RPC su stdin/stdout ---"; echo
            echo "Una richiesta JSON per riga. Per uscire: CTRL+C."; echo
            "$PY" run.py service
            pausa
            ;;
        10)
            clear
            echo "--- Tracciabilita' campioni, GUI Tkinter di collaudo ---"; echo
            "$PY" run.py lims
            pausa
            ;;
        0) exit 0 ;;
        *) ;;
    esac
done
