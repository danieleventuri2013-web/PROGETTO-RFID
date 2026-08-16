@echo off
REM Smoke test hardware del service RFID. Non scrive alcun dato sui tag.
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "RFID_TEST_TS=%%I"
set "RFID_TEST_REPORT=logs\service_hardware_smoke_%RFID_TEST_TS%.jsonl"

1>&2 echo Test service RFID in sola lettura.
1>&2 echo Verificare prima: antenne collegate, regione EU e porta COM/IP in src\app\config.yaml.
1>&2 echo Report JSONL: %RFID_TEST_REPORT%
type "tools\service_hardware_smoke.jsonl" | python run.py service %* > "%RFID_TEST_REPORT%"
set "RFID_SERVICE_RC=%ERRORLEVEL%"
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; try { $failed = Get-Content -LiteralPath $env:RFID_TEST_REPORT | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.error -or ($_.result -and -not $_.result.ok) }; if ($failed) { exit 1 } else { exit 0 } } catch { exit 2 }"
set "RFID_RESULT_RC=%ERRORLEVEL%"
if not "%RFID_SERVICE_RC%"=="0" (set "RFID_TEST_RC=%RFID_SERVICE_RC%") else (set "RFID_TEST_RC=%RFID_RESULT_RC%")

echo.
echo ===== RISPOSTE SERVICE RFID =====
type "%RFID_TEST_REPORT%"
echo.
if "%RFID_TEST_RC%"=="0" (
  echo Smoke test completato. Verificare i campi ok/error nelle risposte.
) else (
  echo Smoke test terminato con codice %RFID_TEST_RC%.
)
if /I not "%RFID_NO_PAUSE%"=="1" pause
endlocal & exit /b %RFID_TEST_RC%
