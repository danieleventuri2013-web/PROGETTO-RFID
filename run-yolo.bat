@echo off
cd /d "%~dp0"
echo Avvio del solo YOLO locale (senza SAM 2), stesso collegamento della WebUI...
".venv-openvino\Scripts\python.exe" src\app\sam2_preview.py --senza-sam --riusa-token %*
if errorlevel 1 pause
