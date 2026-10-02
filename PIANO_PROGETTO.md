# PIANO PROGETTO — Applicazione lettore RFID SIM7200 (Silion) con 3 antenne

> Stato al 2 ottobre 2026: WebUI operativa per accettazione, spedizione e
> ricezione, SQLite schema 9, archivio distinte e QR webcam integrati.
> Completato il riconoscimento SAM 2 / Qwen nel Sigillo con configurazione
> centralizzata nelle Impostazioni Controllo Visivo. Test: 754/754 runner,
> 781/781 pytest e sei suite UI. Il collaudo fisico completo del nuovo
> percorso visivo/RFID resta da svolgere. Per la ripresa usare
> [la memoria del 2 ottobre](NOTE_SESSIONE_2026-10-02.md); le sezioni
> successive conservano anche la pianificazione storica degli step hardware.

## 1. Obiettivo
Realizzare un'applicazione per pilotare in lettura e scrittura un lettore RFID UHF **SIM7200**
(Silion, modulo Impinj E710) montato su baseboard **SLD1090**, con **3 antenne SLP1027**
(8 dBi, polarizzazione circolare, HPBW 68°) disposte a formare un **parallelepipedo di lettura**:

- **2 antenne "in piano" affiancate** (orizzontali) → formano la **base** del volume
  (pavimento), posizionate una di fianco all'altra.
- **1 antenna "a 90° in verticale"** → costituisce una **parete laterale** e definisce
  l'**altezza** del volume.

### 1.1 Obiettivo di integrazione nel framework principale

Il sottosistema RFID deve evolvere come **black box autonoma in modalità service**.
Driver, protocollo, trasporto e logica operativa restano indipendenti dalla GUI;
il framework principale accederà soltanto a contratti input/output versionati,
lifecycle, health ed eventi serializzabili. La GUI sarà aperta come strumento di
configurazione, calibrazione, diagnostica e collaudo, non come motore applicativo.
Ogni modifica e prova hardware futura deve preservare questo confine. Il contratto
corrente è descritto in `docs/CONTRATTO_SERVICE.md`.

Il tag viene considerato "dentro il parallelepipedo" quando è rilevato in modo stabile da
**almeno una** delle antenne e, idealmente, quando almeno 2 antenne lo coprono (ridondanza
spaziale). Lo **Step 1** verifica il funzionamento di base di lettura/scrittura nel volume.

```
        vista laterale (asse X = larghezza, Z = altezza)

     z
     ↑    ┌──────────┐  ← antenna 3 VERTICALE (parete, altezza)
     │    │          │
     │    │   VOLUME │
     │    │          │
     │    └──────────┘
     │
     └────────────────────→ x
        ┌────────┬─────────┐
        │ Ant 1  │  Ant 2  │  ← 2 antenne ORIZZONTALI affiancate (base)
        └────────┴─────────┘   (vista dall'alto: base 2×antenna)

   Dimensioni (SLP1027 lato 220 mm):
     larghezza = 2 × 220 = 440 mm  (2 antenne affiancate)
     profondità = 220 mm
     altezza = 220 mm  (1 antenna verticale)
```

> Nota geometrica: l'antenna verticale garantisce lettura dei tag orientati
> verticalmente (es. tag su oggetti in piedi) che le antenne orizzontali di base
> leggerebbero male. La base doppia offre copertura orizzontale estesa.

---

## 2. Hardware in uso (da MANUALI/Offerta-Parti)

| Parte | Modello | Caratteristiche rilevanti |
|---|---|---|
| Modulo RFID | **SIM7200** | Impinj E710, UHF Gen2 (ISO 18000-6C), **4 porte SMA**, RF 5–30 dBm (33 dBm full-band), UART 9600–921600 bps (default 115200), cache 1000 tag, 2 GPI / 2 GPO 3.3V TTL, alimentazione 5V |
| Baseboard | **SLD1090** | ospita il SIM7200; interfacce **USB, RS232, Ethernet TCP, WiFi, 4G, RS485**; IP default 192.168.1.100/24 (gw .254 operativo; il reset ripristina gw .1.1); porta TCP server 8080; alimentazione 9–24V o PoE; 4 IN / 4 OUT GPIO (3 relè) |
| Antenna | **SLP1027** | 8 dBi, polarizzazione circolare, **HPBW 68°**, 220×220×24 mm, IP67, connettore SMA femmina laterale; specificata **902–928 MHz** (banda FCC) |
| Cavi | 3D RF cable | cavi coassiali antenna→modulo |

### 2.1 Avvertenza regulator (Italia / EU)
- Il modulo SIM7200 versione CE opera in **865–868 MHz** (regione `0x08`).
- L'antenna SLP1027 è tarata su **902–928 MHz**: in banda EU è fuori dall'ottimo,
  con perdita di guadagno e ROS peggiore. Per test in laboratorio è accettabile;
  in produzione verificare antenna tarata EU o regione `0xFF` (full-band) solo se
  conforme ai limiti EIRP ETSI EN 302 208.
- Rispettare il limite EIRP: partire da **20 dBm** e salire solo se necessario.

> ## ⚠️ AZIONE FORNITORE — antenna SLP1027 non tarata per banda EU
>
> **Problema.** L'antenna SLP1027 fornita è specificata per **902–928 MHz**
> (banda FCC), mentre il modulo SIM7200 versione CE deve operare in
> **865–868 MHz** (regione `0x08`, obbligatoria in Italia per conformità ETSI).
> Questo disallineamento significa che l'antenna lavora **fuori risonanza**:
>
> | Effetto | Conseguenza |
> |---|---|
> | VSWR più alto a 865–868 MHz | parte della potenza riflessa verso il modulo |
> | Guadagno ridotto vs 8 dBi nominali | gittata di lettura più corta |
> | Return loss peggiore | possibile allarme `0x0505 High return loss` |
> | L'antenna **funziona comunque** | prestazioni ridotte, non incompatibilità |
>
> **Decisione per lo Step 1 (laboratorio).** Si procede con regione `0x08` e
> antenna SLP1027 così com'è, accettando il calo di prestazioni (compensabile
> con potenza più alta entro i limiti EIRP e tag mantenuti vicini).
>
> **Azione per la produzione.** Richiedere a Silion:
> 1. conferma che la SLP1027 è utilizzabile a 865–868 MHz (curve Gain/VSWR
>       in quella banda) oppure fornitura della **versione EU** dell'antenna;
> 2. in alternativa, valutare antenna **broadband 860–960 MHz**.
>
> **Non impostare mai regione FCC/full-band in Italia** solo per favorire
> l'antenna: il modulo trasmetterebbe fuori banda EU → **non conforme ETSI**.
>
> **Stato:** ☐ da inoltrare a Silion  •  **Referente:** Rixlab

### 2.2 Geometria del parallelepipedo (antenne SLP1027, HPBW 68°)
Con beamwidth a metà potenza di 68°, il cono di illuminazione di ciascuna antenna
copre un cerchio di diametro ≈ `2·d·tan(34°)` a distanza `d`.
Per avere copertura uniforme tra le due piastre orizzontali si suggerisce distanza
**pavimento–tetto ≈ 30–50 cm** (config: `geometry.suggested_floor_to_ceiling_mm`).

---

## 3. Stack tecnologico scelto

| Livello | Scelta | Motivo |
|---|---|---|
| Linguaggio | **Python 3.10+** | multipiattaforma, seriale facile, prototipazione veloce |
| Trasporto | **pyserial** (USB/RS232) **oppure socket TCP** | la SLD1090 espone USB, RS232, Ethernet, WiFi; il protocollo Silion è trasporto-agnostico |
| Protocollo | **Implementazione nativa del protocollo Silion** (no DLL) | le DLL `ModuleAPI*.dll`/`ModuleAPI_J.jar` + `PCOMM.DLL` sono Windows-only; implementando il protocollo a livello di byte si resta liberi e multipiattaforma |
| GUI Step 1 | **CLI interattivo** + log | primo step: massima velocità di debug |
| GUI Step futuro | **PyQt/Tkinter + dashboard** | step successivo |
| Config | **YAML** | parametri antenne, potenze, regione, trasporto |

Dipendenze: solo `pyserial` e `pyyaml` per lo Step 1.

---

## 4. Architettura del software

```
src/
├── rfid_silion/
│   ├── protocol.py      # Frame, CRC-16 CCITT, build/parse pacchetto
│   ├── transports.py    # Transport ABC + SerialTransport + TcpTransport
│   ├── reader.py        # SIM7200Reader + reader_from_config()
│   ├── tags.py          # Parsing tag buffer, Metadata Flags, dataclass Tag
│   └── errors.py        # Status code → eccezioni
├── app/
│   ├── step1_test_rw.py     # CLI test read/write nel parallelepipedo
│   └── config.yaml          # parametri (trasporto, antenne, potenze, regione)
└── tests/
    └── test_protocol.py     # unit test CRC/frame (senza hardware)
```

### 4.1 Livello trasporto (`transports.py`)
Il modulo SIM7200 è pilotabile via UART TTL diretta o, tramite baseboard SLD1090,
via **USB / RS232 / TCP / WiFi / 4G**. Il protocollo Silion è identico su tutti i mezzi:
l'I/O grezzo è astratto da `Transport` (metodi `open/close/write/read/flush_input`).
`SerialTransport` (pyserial) e `TcpTransport` (socket) coprono i casi pratici.

### 4.2 Livello protocollo (`protocol.py`)
Dal manuale "Protocol Introduction":
- **Frame Host→Reader:** `0xFF | DataLen(1) | CmdCode(1) | Data(N) | CRC16(2, MSB first)`
- **Frame Reader→Host:** `0xFF | DataLen(1) | CmdCode(1) | Status(2) | Data(N) | CRC16(2)`
- `DataLen` = numero di byte del solo campo `Data` (esclude Header, DataLen, Cmd, Status, CRC).
- **CRC-16 CCITT**, poly `0x1021`, init `0xFFFF`, calcolato su tutti i byte **escluso l'header 0xFF e i 2 byte di CRC**.

### 4.3 Comandi implementati (codici dal manuale)

| Cmd  | Nome                  | Step 1 | Note |
|------|-----------------------|:------:|------|
| 0x04 | Boot Firmware         | ✅ | obbligatorio: passa da Bootloader ad App Firmware |
| 0x97 | Set Current Region    | ✅ | EU=0x08 (Italia) |
| 0x91 | Set Antenna Ports     | ✅ | potenze + antenne per inventory/accesso |
| 0x61 | Get Antenna Ports     | ✅ | diagnostica connessione antenne (option 0x05) |
| 0x22 | Synchronous Inventory | ✅ | conteggio tag nel tempo |
| 0x29 | Get Tag Buffer        | ✅ | recupero EPC + metadati (antenna ID, RSSI) |
| 0x21 | Single Tag Inventory  | ✅ | tag singolo |
| 0x28 | Read Tag Data         | ✅ | lettura banche RESERVED/EPC/TID/USER |
| 0x24 | Write Tag Data        | ✅ | scrittura banca (max 32 word = 64 B) |
| 0x23 | Write Tag EPC         | ✅ | scrittura EPC (aggiorna PC) |
| 0x25 | Lock Tag              | ⏳ Step 2 | opcode reale = 0x25 (NON 0x82) |
| 0x26 | Kill Tag              | ⏳ Step 2 | opcode reale = 0x26 (0x65 = "get tabella freq-hop") |
| 0xAA48 | Asynchronous Inventory | ⏳ Step 3 | framing ESTESO (magic "Moduletech" + SubCRC + 0xBB); stop = 0xAA49 |

Banche memoria Gen2: `0x00 RESERVED`, `0x01 EPC`, `0x02 TID`, `0x03 USER`.

### 4.4 Gestione delle 3 antenne
- Numerazione logica = numerazione fisica (non SLR11xx).
  - **Antenna 1** = base, sx (orizzontale)
  - **Antenna 2** = base, dx (orizzontale, affiancata ad antenna 1)
  - **Antenna 3** = parete verticale a 90° (definisce l'altezza)
- `set_antennas_for_inventory([1,2,3])` → comando 0x91 option 0x02 (il lettore cicla le antenne nell'ordine dato).
- Potenze indipendenti per antenna (Read/Write power in unità da 0.01 dBm, es. 3000 = 30 dBm) via 0x91 option 0x03.
- Per il **tag access** (lettura/scrittura 0x28/0x24) si imposta una singola antenna con 0x91 option 0x00. Lo Step 1 prova l'operazione su ciascuna antenna e riporta quale riesce.

### 4.5 GUI di controllo (`app/gui.py`)
Interfaccia Tkinter + matplotlib che consente, senza editare YAML/CLI:
- scelta **trasporto** (seriale USB/RS232 oppure TCP/IP SLD1090), elenco COM
  aggiornabile e inserimento manuale dei relativi parametri;
- impostazione **regione** e **potenze Read/Write** per antenna (slider 5–30 dBm);
- **Inventory**, **Read USER**, **Write USER**, **Read+Verify** e cambio EPC
  manuale/AUTO protetto (operazioni in thread separato);
- **polling continuo** (1 s) per aggiornamento live;
- visualizzazione:
  - **grafico lineare** a barre: RSSI letto e potenza configurata per ciascuna antenna;
  - **grafico 3D del volume** (parallelepipedo 44×22×22 cm) con le 3 antenne posizionate
    (2 affiancate sulla base + 1 verticale in altezza), piastre colorate per potenza,
    e **posizione stimata del tag** (centroide pesato sull'RSSI lineare) con linee
    antenna→tag colorate per RSSI.
Avvio di collaudo raccomandato: `python run.py service-gui` oppure
`avvia_test_grafico_rfid.bat`; driver e hardware restano nel processo service.

---

## 5. Piano a step

### STEP 1 — Prova di lettura/scrittura nel parallelepipedo (QUESTO STEP)
Obiettivi:
1. Connessione (seriale o TCP) + `Boot Firmware` (0x04) → lettore in App layer.
2. `Set Region` EU (0x08).
3. `Set Antenna Ports` potenze per antenne 1,2,3 (partire conservative 20 dBm = 2000).
4. `Get Antenna Ports` option 0x05 → verificare fisicamente connesse 1,2,3.
5. **Inventario**: `Synchronous Inventory` (0x22) su antenne [1,2,3], timeout 1000 ms, poi `Get Tag Buffer` (0x29) con Metadata Flags = `0x0007` (ReadCount + RSSI + AntennaID) → per ogni tag si sa quale antenna lo ha letto e con che RSSI.
6. **Lettura**: `Read Tag Data` (0x28) banca USER, addr 0, 2 word — prima su antenna 1, poi 2, poi 3; si registra l'antenna con successo.
7. **Scrittura**: `Write Tag Data` (0x24) banca USER addr 0, dato `0x12345678`; poi rilettura di verifica.
8. **Scrittura EPC**: `Write Tag EPC` (0x23) opzionale.
9. Report: tabella tag × antenna × RSSI + esito read/write.

Criteri di successo Step 1:
- almeno un tag letto in modo stabile da ≥2 antenne entro il volume;
- read e write USER riuscite e verificate con rilettura;
- nessun errore CRC/status diverso da `0x0000` / `0x0400` (no tag).

Deliverable Step 1:
- `src/rfid_silion/*` (protocollo + trasporti + reader)
- `src/app/step1_test_rw.py` (CLI guidata)
- `src/app/config.yaml`
- log di test in `logs/step1_<timestamp>.json`

### STEP 1-bis — Tracciabilità dei campioni (implementato, da validare su hardware)
Pacchetto `src/lims/`, sopra il contratto `RFIDService`. Vedi
[`docs/SCHEMA_DATI_TAG.md`](docs/SCHEMA_DATI_TAG.md).
- ✅ Profilazione del tag: lettura TID e **misura** della USER memory
  (`python run.py tag-profile`). È il prerequisito di tutto il resto: la
  capacità dipende dal chip, e nessun datasheet dei tag è mai entrato nel progetto.
- ✅ Schema dati versionato: EPC pseudonimo di 12 byte in chiaro, payload del
  campione cifrato AES-256-GCM e legato al TID del chip.
- ✅ Archivio SQLite (pazienti, accettazioni, reperti, contenitori, spedizioni,
  traccia delle operazioni) con registro di unicità degli EPC.
- ✅ Orchestrazione scrittura/lettura e schermate di accettazione e ricezione
  (`python run.py lims`), con rilevamento dei contenitori mancanti.
- ☐ Da fare su hardware reale: misurare i tag effettivi, provare un ciclo
  completo, misurare la perdita di lettura con contenitori pieni di liquido.

### STEP 2 — Affidabilità e sicurezza
- ✅ Lock (0x25) e gestione access password — le operazioni permanenti
  richiedono consenso esplicito; kill (0x26) resta volutamente non implementato.
- Calibrazione potenze per antenna (mappa RSSI vs posizione nel volume).
- **Filtri Select per singolo EPC.** Ora è un limite concreto, non teorico: senza
  Select i comandi di lettura agiscono sul primo tag che risponde, quindi il
  payload cifrato si legge solo a tag singolo. Alternativa da valutare:
  l'*embedded read* (`META_TAG_DATA = 0x0080`), già parsato in `tags.py` ma mai
  richiesto, che restituirebbe la USER memory di tutti i tag in un inventory.
- Test ripetuti (N cicli) per stimare tasso di successo per antenna e posizione.

### STEP 3 — Continuo e GUI
- Asynchronous Inventory (0xAA48 / stop 0xAA49, framing esteso) con callback tag in tempo reale.
- Dashboard (PyQt): mappa 3D del volume, posizione stimata del tag da RSSI triangolato tra le 3 antenne, statistiche.
- GPIO/trigger della SLD1090 (4 IN / 4 OUT, 3 relè) per fotocellule e attuatori.
- ✅ Persistenza DB (SQLite) — realizzata in `lims.db` per il dominio campioni;
  resta da valutare la persistenza delle *letture grezze* di inventory.

### STEP 4 — Produzione
- Packaging, avvio automatico, watchdog trasporto, configurazione remota.

---

## 6. Configurazione Step 1 (`config.yaml`)

```yaml
# trasporto: 'serial' oppure 'tcp'
serial:
  port: COM3            # o /dev/ttyUSB0
  baudrate: 115200
  timeout_s: 2.0
# tcp:
#   host: 192.168.1.100  # IP default SLD1090
#   port: 8080
#   timeout_s: 2.0
reader:
  region: 0x08          # EU 865-867
  boot_timeout_ms: 3000
antennas:
  # id, ruolo, read_power_cdbm, write_power_cdbm  (cdBm = dBm*100)
  - {id: 1, role: floor,     read_power: 2000, write_power: 2000}
  - {id: 2, role: ceiling,   read_power: 2000, write_power: 2000}
  - {id: 3, role: wall_90,   read_power: 2000, write_power: 2000}
inventory:
  antennas: [1, 2, 3]
  timeout_ms: 1000
  metadata_flags: 0x0007   # ReadCount | RSSI | AntennaID
tag_access:
  bank_user: 0x03
  bank_epc:  0x01
  write_data_hex: "12345678"
  access_password_hex: "00000000"
  timeout_ms: 1000
```

---

## 7. Rischi e note
- **Region EU**: in Italia si opera 865–868 MHz (regione 0x08). L'antenna SLP1027 è tarata
  902–928 MHz → prestazioni ridotte in banda EU (vedi §2.1). Verificare potenze massime
  EIRP conforme ETSI (prudenza: partire da 20 dBm).
- **Antenne vicine**: rischio accoppiamento tra antenne → usare potenze moderate e, se
  necessario, abilitare una antenna alla volta nei test diagnostici.
- **Lunghezza pacchetto ≤ 255 byte** (limite protocollo): la write max 32 word = 64 byte rientra.
- **CRC**: implementato esattamente come `CalcCRC` del manuale (skip header, poly 0x1021,
  init 0xFFFF). Nota: `1D 0B` è il CRC del Boot Firmware `0x04`; il `1D 0C` che compare nei
  sample NON è un typo ma il CRC del comando `0x03` (Get Version, `FF 00 03 1D 0C`) — due
  comandi diversi.
- **Boot obbligatorio**: al power-on il lettore è in Bootloader; senza 0x04 i comandi tag
  falliscono con `0x0101` (unavailable command).
- **Trasporto TCP**: il lettore fa da **server TCP sulla porta 8080** (IP default 192.168.1.100),
  come da manuale "Basic Steps of Command Development" §1.2 (confermare comunque sull'hardware).
  In alternativa è disponibile la nuova **HTTP API JSON** (`POST http://<IP>/moduleapi/...`,
  porta 80; o server locale `127.0.0.1:20085`), senza CRC/framing.
  Per la connessione via USB la SLD1090 si presenta come dispositivo "HDSC".
- **GPIO/trigger**: la SLD1090 offre 4 IN / 4 OUT (3 relè) utili in Step 3 per trigger
  esterni (es. fotocellula) e attuatori.
