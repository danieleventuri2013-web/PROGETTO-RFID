@echo off
REM Avvia il service RFID JSON-RPC/JSONL senza aprire la GUI.
REM Le risposte restano su stdout; log e messaggi del launcher vanno su stderr.
setlocal
cd /d "%~dp0"
1>&2 echo Avvio service RFID - config predefinita: src\app\config.yaml
1>&2 echo Per terminare: Ctrl+C oppure chiudere stdin dal processo chiamante.
python run.py service %*
set "RFID_SERVICE_RC=%ERRORLEVEL%"
1>&2 echo Service RFID terminato con codice %RFID_SERVICE_RC%.
endlocal & exit /b %RFID_SERVICE_RC%
