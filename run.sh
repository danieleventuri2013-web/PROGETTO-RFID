#!/usr/bin/env bash
# Launcher RFID SIM7200 (Linux/macOS)
# Avvia la GUI di default; argomento opzionale: gui | step1 | tests
set -e
cd "$(dirname "$0")"
exec python3 run.py "$@"
