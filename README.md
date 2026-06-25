# PROGETTO-RFID — Pilotaggio lettore SIM7200 (Silion) con 3 antenne

Applicazione per pilotare in lettura/scrittura un lettore RFID UHF **SIM7200** (modulo
Impinj E710 su baseboard **SLD1090**) con **3 antenne SLP1027** (8 dBi, polarizzazione
circolare, HPBW 68°) disposte a formare un parallelepipedo di lettura
(2 in piano + 1 verticale a 90°).

La documentazione originale del lettore è in `MANUALI/` (protocollo seriale, SDK C/C#/Java,
codice di esempio, schede hardware/offerta in `MANUALI/Offerta-Parti`). Il driver qui
implementato **non usa le DLL ModuleAPI** (Windows-only) ma parla direttamente il protocollo
seriale/binario Silion, quindi è multipiattaforma e supporta sia **seriale (USB/RS232)**
sia **TCP/IP** (la SLD1090 espone entrambi).

## Piano del progetto
Vedi **[`PIANO_PROGETTO.md`](PIANO_PROGETTO.md)** per la pianificazione completa
(hardware, architettura, comandi, step 1→4, rischi).

## Hardware (da MANUALI/Offerta-Parti)
- **SIM7200**: modulo RFID UHF, Impinj E710, 4 porte SMA, 5–30 dBm, UART 115200 default.
- **SLD1090**: baseboard con USB / RS232 / Ethernet TCP / WiFi / 4G / RS485; IP default 192.168.1.100.
- **SLP1027**: antenna 8 dBi circolare, HPBW 68°, 220×220×24 mm (tarata 902–928 MHz; in EU 865–868 prestazioni ridotte — vedi note).

## Struttura
```
MANUALI/                          documentazione originale Silion
  Offerta-Parti/                  SIM7200, SLD1090, SLP1027, quotazione
PIANO_PROGETTO.md                 pianificazione progetto
requirements.txt                  pyserial, pyyaml
src/
  rfid_silion/
    protocol.py                   frame + CRC-16 CCITT + parser
    transports.py                 Transport ABC + SerialTransport + TcpTransport
    errors.py                     status code -> eccezioni
    tags.py                       parsing tag buffer (0x29)
    reader.py                     classe SIM7200Reader + reader_from_config()
  app/
    config.yaml                   parametri (trasporto, antenne, potenze, regione)
    step1_test_rw.py              CLI test read/write nel parallelepipedo
    gui.py                        GUI Tkinter + matplotlib (controllo + grafici)
  tests/
    test_protocol.py              unit test CRC/frame (senza hardware)
```

## Installazione
```bash
pip install -r requirements.txt
```

## Test del protocollo (senza lettore)
```bash
python src/tests/test_protocol.py
```

## GUI di controllo (consigliata)
```bash
python src/app/gui.py --config src/app/config.yaml
```
Permette di impostare trasporto (seriale/TCP), potenze per antenna, eseguire
inventory/read/write/verify e visualizzare RSSI + potenza per antenna e la
posizione stimata del tag nel volume 3D. Polling continuo opzionale.

## Step 1 — test read/write (CLI)
1. Collegare il SIM7200+SLD1090 via USB (dispositivo "HDSC") o via cavo Ethernet.
2. Collegare le 3 antenne SLP1027 alle porte 1, 2, 3 del modulo.
3. Modificare `src/app/config.yaml`: scegliere `serial:` o `tcp:` e impostare porta/IP.
4. Posizionare un tag UHF Gen2 nel parallelepipedo.
5. Eseguire:
```bash
python src/app/step1_test_rw.py --config src/app/config.yaml
# solo lettura (non scrive):
python src/app/step1_test_rw.py --skip-write --skip-epc
```
Il report viene salvato in `logs/step1_<timestamp>.json`.

## Antenne e parallelepipedo
- Antenna 1 = base sx (orizzontale)
- Antenna 2 = base dx (orizzontale, affiancata ad antenna 1)
- Antenna 3 = parete verticale a 90° (definisce l'altezza)

Volume: 440 × 220 × 220 mm (2 antenne affiancate come base + 1 verticale in altezza).

Lo Step 1 inventaria ciclando le 3 antenne e, per read/write, prova ciascuna antenna
registrando quale riesce (e l'RSSI), per verificare la copertura del volume.

## Note regolamentari (Italia)
Regione EU = `0x08` (865–868 MHz). L'antenna SLP1027 è tarata 902–928 MHz → in banda EU
ha prestazioni ridotte (accettabile in lab). Rispettare i limiti EIRP ETSI: partire da 20 dBm.
