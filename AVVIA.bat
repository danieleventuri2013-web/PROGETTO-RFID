@echo off
REM ===========================================================================
REM  PROGETTO-RFID - avvio unico
REM
REM  Doppio clic su questo file: apre un menu con tutto quello che si puo' fare.
REM  Non serve conoscere Python ne' aprire un terminale.
REM
REM  L'ordine delle voci non e' casuale: e' quello in cui vanno usate la prima
REM  volta. 1 e 2 non richiedono hardware; dalla 3 in poi il lettore serve.
REM ===========================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
title PROGETTO-RFID

REM --- Trova un interprete Python utilizzabile -------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo   [ERRORE] Python non e' installato, o non e' nel PATH.
    echo.
    echo   Scaricarlo da https://www.python.org/downloads/ e durante
    echo   l'installazione spuntare "Add Python to PATH".
    echo.
    pause
    exit /b 2
)

:menu
cls
echo ===========================================================================
echo                 PROGETTO-RFID  -  Tracciabilita' campioni
echo ===========================================================================
echo.
echo   SENZA LETTORE
echo     1^)  Verifica del programma  (test automatici, nessun hardware)
echo     2^)  Anteprima di un'etichetta stampata  (file ZPL in logs\)
echo.
echo   CON IL LETTORE COLLEGATO
echo     3^)  Diagnosi del lettore e delle antenne
echo     4^)  Profilazione dei tag        ^<-- il primo passo con tag nuovi
echo     5^)  Campagna di misura          ^<-- taratura potenza e parametri radio
echo.
echo   USO QUOTIDIANO
echo     6^)  Interfaccia operativa nel browser   ^<-- questa, tutti i giorni
echo.
echo   STRUMENTI
echo     7^)  GUI di collaudo del lettore  (Tkinter)
echo     8^)  Test read/write da riga di comando  (Step 1)
echo     9^)  Service JSON-RPC per il framework principale
echo    10^)  Tracciabilita' campioni - GUI Tkinter  (collaudo)
echo.
echo     0^)  Esci
echo.
set "scelta="
set /p "scelta=  Scelta: "

if "%scelta%"=="1" goto test
if "%scelta%"=="2" goto etichetta
if "%scelta%"=="3" goto health
if "%scelta%"=="4" goto profilo
if "%scelta%"=="5" goto campagna
if "%scelta%"=="6" goto webui
if "%scelta%"=="7" goto gui
if "%scelta%"=="8" goto step1
if "%scelta%"=="9" goto service
if "%scelta%"=="10" goto lims
if "%scelta%"=="0" exit /b 0
goto menu

:webui
cls
echo --- Interfaccia operativa ---
echo.
echo Si apre nel browser. L'indirizzo vale solo su questa macchina e contiene
echo un token che cambia a ogni avvio: nessun altro programma puo' usare il
echo lettore mentre l'interfaccia e' aperta.
echo.
echo Per chiudere: tornare qui e premere CTRL+C.
echo.
%PY% run.py webui
goto fine

:test
cls
echo --- Verifica del programma: nessun hardware richiesto ---
echo.
%PY% run.py tests
goto fine

:etichetta
cls
echo --- Anteprima di un'etichetta ---
echo.
echo Genera un'etichetta di esempio e la salva come file ZPL, senza stampante.
echo Il file si puo' inviare alla stampante o incollare in un visualizzatore ZPL.
echo.
%PY% -c "import sys, datetime; sys.path.insert(0,'src'); from pathlib import Path; from lims.labels import LabelContent, FileLabelPrinter, print_container_label; from lims.codec import SpecimenFlags; Path('logs').mkdir(exist_ok=True); c = LabelContent(display_name='DELLA VALLE GIANFRANCO', codice_fiscale='MRTMTT25D09F205Z', accession_id=987654, container_index=2, container_total=3, epc='0100A5000F12060203DEADBE', external_ref='CHIR-2026-0042', material_code=3, fixative_code=1, site_code=2, data_prelievo=datetime.date.today(), flags=SpecimenFlags.INFECTIOUS, lab_name='Anatomia Patologica'); p = Path('logs/etichetta_esempio.zpl'); print_container_label(FileLabelPrinter(p), c); print('Etichetta salvata in', p.resolve())"
goto fine

:health
cls
echo --- Diagnosi del lettore ---
echo.
%PY% run.py health --with-inventory
goto fine

:profilo
cls
echo --- Profilazione dei tag ---
echo.
echo Misura quanti dati entrano nei tag che hai in mano: modello del chip,
echo dimensione della memoria utente, presenza del numero di serie di fabbrica.
echo.
echo Il valore misurato viene scritto da solo in src\app\config.yaml
echo (lims.user_memory_bytes). Vale sempre il tag PEGGIORE del lotto: la soglia
echo scende da sola e non sale, percio' conviene provarne tre o quattro dello
echo stesso lotto uno dopo l'altro.
echo.
echo   Posizionare UN SOLO tag sull'antenna di scrittura, gli altri lontani,
echo   poi premere INVIO.
echo.
pause >nul
%PY% run.py tag-profile
goto fine

:campagna
cls
echo --- Campagna di misura ---
echo.
echo Tara la configurazione radio sui dati invece che a sentimento.
echo.
echo   PRIMA di procedere:
echo     - caricare il contenitore con i campioni, chiuso come in esercizio;
echo     - mettere di proposito qualche tag DI CONTROLLO FUORI dal contenitore,
echo       alle distanze che si vogliono escludere.
echo.
echo   Senza tag di controllo esterni la campagna sceglierebbe sempre la
echo   potenza massima, che e' anche quella che legge il tavolo accanto.
echo.
echo   ---------------------------------------------------------------------
echo   QUESTA VOCE NON LANCIA LA CAMPAGNA: da riga di comando servirebbero gli
echo   EPC dei tag scritti a mano. Si fa dalla voce 6 (interfaccia browser),
echo   scheda STRUMENTI, riquadro "Campagna di misura": i tre bottoni
echo   "Rileva i tag dentro" / "Rileva i tag fuori" / "Avvia la campagna"
echo   trovano gli EPC da soli e mostrano l'avanzamento.
echo   ---------------------------------------------------------------------
echo.
echo   In alternativa, a mano:
echo     python run.py campaign --inside-from-shipment 1 --outside AABB... CCDD...
echo.
pause
goto fine

:lims
cls
echo --- Tracciabilita' campioni ---
echo.
echo Tre schede: accettazione, sigillo e spedizione, ricezione.
echo.
%PY% run.py lims
goto fine

:gui
cls
%PY% run.py service-gui
goto fine

:step1
cls
%PY% run.py step1
goto fine

:service
cls
echo --- Service JSON-RPC su stdin/stdout ---
echo.
echo Una richiesta JSON per riga. Per uscire: CTRL+C.
echo.
%PY% run.py service
goto fine

:fine
echo.
echo ---------------------------------------------------------------------------
pause
goto menu
