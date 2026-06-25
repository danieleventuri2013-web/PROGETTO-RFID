@echo off
REM Launcher RFID SIM7200 (Windows)
REM Avvia la GUI di default; argomento opzionale: gui | step1 | tests
setlocal
cd /d "%~dp0"
python run.py %*
if errorlevel 1 pause
endlocal
