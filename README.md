# PROGETTO-RFID — Pilotaggio lettore SIM7200 (Silion) con 3 antenne

Applicazione per pilotare in lettura/scrittura un lettore RFID UHF **SIM7200** (modulo
Impinj E710 su baseboard **SLD1090**) con **3 antenne SLP1027** (8 dBi, polarizzazione
circolare, HPBW 68°) disposte a formare un parallelepipedo di lettura
(2 in piano + 1 verticale a 90°).

La documentazione originale del lettore è in `MANUALI/` e `ULTIMI MANUALI/` (protocollo,
SDK, esempi; non versionate). Il driver qui implementato **non usa le DLL ModuleAPI**
(Windows-only) ma parla direttamente il protocollo binario Silion, quindi è
multipiattaforma e supporta sia **seriale (USB/RS232)** sia **TCP/IP** (la SLD1090
espone entrambi).

## Documentazione

| Documento | Contenuto |
|---|---|
| [`docs/MANUALE_UTENTE.md`](docs/MANUALE_UTENTE.md) | manuale operativo (bozza) |
| [`docs/DOCUMENTAZIONE_TECNICA.md`](docs/DOCUMENTAZIONE_TECNICA.md) | architettura, protocollo, diagnostica (bozza) |
| [`docs/REPORT_REVISIONE.md`](docs/REPORT_REVISIONE.md) | analisi e revisione codice 2026-07 |
| [`PIANO_PROGETTO.md`](PIANO_PROGETTO.md) | pianificazione completa (hardware, step 1→4, rischi) |
| [`CHANGELOG.md`](CHANGELOG.md) | storico modifiche |

## Requisiti e installazione

Python ≥ 3.10, poi:
```bash
pip install -r requirements.txt     # pyserial, pyyaml, matplotlib, numpy
```
(`run.py` installa comunque da solo le dipendenze mancanti al primo avvio.)

## Avvio

```bash
python run.py            # menu interattivo (run.bat / ./run.sh equivalenti)
python run.py gui        # GUI di controllo (consigliata)
python run.py step1      # CLI Step 1: test read/write nel parallelepipedo
python run.py health     # health check: diagnosi rapida del lettore
python run.py tests      # autotest senza hardware (protocollo + reader)
```

## Configurazione

Tutto in `src/app/config.yaml`: trasporto (**una** tra le sezioni `serial:` /
`tcp:` — la selezione è per presenza di chiave e `serial` ha priorità), regione
(EU `0x08` per l'Italia), potenze per antenna in **centesimi di dBm**
(2000 = 20 dBm), parametri inventory/accesso tag, logging.

## GUI di controllo

```bash
python src/app/gui.py --config src/app/config.yaml [--debug]
```
Trasporto seriale/TCP, potenze per antenna, inventory/read/write/verify,
polling continuo, grafico RSSI + volume 3D con posizione stimata del tag,
pulsante **"Report diagnostico"** (salva un JSON per l'assistenza in `logs/`).

## Step 1 — test read/write (CLI)

1. Collegare SIM7200+SLD1090 via USB (dispositivo "HDSC") o Ethernet.
2. Collegare le 3 antenne SLP1027 alle porte 1, 2, 3.
3. Impostare `src/app/config.yaml` (porta COM o IP).
4. Posizionare un tag UHF Gen2 nel parallelepipedo.
5. `python src/app/step1_test_rw.py --config src/app/config.yaml`
   (opzioni: `--skip-write --skip-epc --debug`).

Report in `logs/step1_<timestamp>.json` (include i contatori diagnostici);
exit code 1 in caso di errore fatale.

## Diagnostica e log

- Ogni sessione scrive un log in `logs/` (`gui_*.log`, `step1_*.log`, `health_*.log`).
- **Checkpoint strutturati** nei punti chiave, greppabili:
  `findstr CHECKPOINT logs\*.log` (Windows) / `grep CHECKPOINT logs/*.log`.
- **Contatori runtime** (`reader.diag`): comandi, timeout, errori frame/status,
  byte spuri scartati — inclusi in tutti i report JSON.
- **Health check**: `python run.py health` → sintesi `[OK]/[FAIL]/[WARN]` +
  `logs/health_<ts>.json`; exit code 0/1/2 (usabile in automazione).
- **Modalità debug**: flag `--debug` o variabile `RFID_DEBUG=1` → log DEBUG con
  dump esadecimale di ogni frame TX/RX.

## Test (senza hardware)

```bash
python run.py tests                  # tutti
python src/tests/test_protocol.py    # CRC/frame/parser tag (frame noti dal manuale)
python src/tests/test_reader.py      # reader con trasporto finto
```

## Troubleshooting rapido

| Sintomo | Prima cosa da provare |
|---|---|
| `0x0101 Unavailable command` | riconnettere (il boot firmware è automatico alla connessione) |
| `Transport timeout` | porta COM/IP corretti? lettore alimentato? `python run.py health` |
| `0x0400 No tag found` | non è un guasto: avvicinare il tag / alzare la potenza |
| `CRC mismatch` / `Resync` frequenti | controllare cavi e baud rate (115200) |
| Scrittura FAIL | tag protetto (`0x0424`), energia insufficiente (`0x042B`) — vedi manuale §14 |

Tabella completa e procedura di segnalazione: `docs/MANUALE_UTENTE.md` §11–16.

## Antenne e parallelepipedo

- Antenna 1 = base sx (orizzontale), Antenna 2 = base dx, Antenna 3 = parete
  verticale a 90°. Volume: 440 × 220 × 220 mm.
- Lo Step 1 inventaria ciclando le 3 antenne e, per read/write, prova ciascuna
  antenna registrando quale riesce (e l'RSSI), per mappare la copertura.

## Note regolamentari (Italia)

Regione EU = `0x08` (865–868 MHz). L'antenna SLP1027 è tarata 902–928 MHz → in
banda EU ha prestazioni ridotte (accettabile in lab). Rispettare i limiti EIRP
ETSI: partire da 20 dBm.
