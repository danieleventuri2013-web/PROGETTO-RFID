@echo off
cd /d "%~dp0"
echo Avvio SAM 2 locale sulla GPU Intel con OpenVINO...
".venv-openvino\Scripts\python.exe" src\app\sam2_preview.py --motore openvino --dispositivo GPU --precisione f16 --riusa-token %*
if errorlevel 1 pause
