@echo off
REM Avvia la GUI di collaudo collegata a un processo service separato.
REM Consente seriale/TCP, inventory, USER read/write e cambio EPC protetto.
setlocal
cd /d "%~dp0"
python run.py service-gui %*
set "RFID_GUI_RC=%ERRORLEVEL%"
if not "%RFID_GUI_RC%"=="0" (
  echo.
  echo GUI RFID terminata con codice %RFID_GUI_RC%.
  pause
)
endlocal & exit /b %RFID_GUI_RC%
