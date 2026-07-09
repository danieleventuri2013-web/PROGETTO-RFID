# Documentazione tecnica — Driver e app RFID SIM7200 (bozza)

Versione driver: 0.2.0 · Data: 2026-07-09 · Pubblico: sviluppatori/manutentori

---

## 1. Scopo del sistema

Pilotare in lettura/scrittura un lettore RFID UHF **Silion SIM7200** (modulo
Impinj E710 su baseboard **SLD1090**) con **3 antenne SLP1027** disposte a
parallelepipedo (2 orizzontali affiancate = base, 1 verticale = altezza), per
verificare la copertura del volume di lettura (Step 1 del piano di progetto).

Decisione architetturale centrale: il driver parla **direttamente il
protocollo binario Silion** (non usa le DLL `ModuleAPI*` del produttore,
Windows-only). Ne consegue codice multipiattaforma e lo **stesso protocollo su
seriale (USB/RS232) e TCP/IP** — cambia solo il livello trasporto.

Riferimento protocollo autorevole: `ULTIMI MANUALI/EX10 Module Communication
Prorocol-2024-12.pdf` (audit di allineamento completato 2026-06).

## 2. Architettura a livelli

```
┌────────────────────────────────────────────────────────────┐
│ APP  src/app/                                              │
│   gui.py            GUI Tkinter + matplotlib               │
│   step1_test_rw.py  CLI test read/write → JSON             │
│   health_check.py   CLI diagnosi rapida → JSON             │
│   config.yaml       unica fonte di configurazione          │
├────────────────────────────────────────────────────────────┤
│ DRIVER  src/rfid_silion/                                   │
│   reader.py       SIM7200Reader: 1 metodo per comando,     │
│                   contatori diag, health_check()           │
│   tags.py         parser Get Tag Buffer (0x29) → Tag       │
│   errors.py       status code → eccezioni                  │
│   protocol.py     framing + CRC-16 (puro, nessun I/O)      │
│   diagnostics.py  logging, checkpoint, DiagCounters        │
├────────────────────────────────────────────────────────────┤
│ TRASPORTO  transports.py                                   │
│   Transport (ABC) ── SerialTransport (pyserial)            │
│                   └─ TcpTransport (socket, porta 8080)     │
└────────────────────────────────────────────────────────────┘
```

Regole di dipendenza: le app usano **solo** `reader.py` (+ costanti di
`protocol.py`); `protocol.py` e `tags.py` non fanno I/O; solo
`transports.py` tocca l'hardware/rete.

## 3. Protocollo (sintesi operativa)

- Frame Host→Reader: `0xFF | DataLen | Cmd | Data[N] | CRC16(MSB,LSB)`
- Frame Reader→Host: `0xFF | DataLen | Cmd | Status(2) | Data[N] | CRC16`
- `DataLen` conta **solo** il campo Data. CRC-16 CCITT (poly `0x1021`, init
  `0xFFFF`) su tutto **tranne** header `0xFF` e i 2 byte CRC.
- **Boot obbligatorio**: al power-on il lettore è nel bootloader; senza
  `boot_firmware()` (0x04) ogni comando tag risponde `0x0101`.

Comandi implementati:

| Cmd | Metodo | Note |
|---|---|---|
| 0x04 | `boot_firmware()` | obbligatorio; ritorna versioni (cache in `firmware_info`) |
| 0x97 | `set_region(0x08)` | EU per l'Italia — NON usare FCC/full-band |
| 0x91/00 | `set_antenna_for_access(tx,rx)` | antenna singola per read/write |
| 0x91/02 | `set_antennas_for_inventory([...])` | ciclo antenne inventory |
| 0x91/03 | `set_antennas_power([...])` | potenze in **cdBm** (2000 = 20 dBm) |
| 0x61/05 | `get_antenna_connection()` | antenne fisicamente connesse |
| 0x22 | `sync_inventory()` | inventario sincrono → n. tag archiviati |
| 0x29 | `get_tag_buffer()` | scarica i tag → `parse_tag_buffer` |
| 0x28 | `read_tag_data()` | Option 0x05 (password, no select) → primo tag |
| 0x24 | `write_tag_data()` | dati pari, ≤ 64 byte |
| 0x23 | `write_tag_epc()` | aggiorna anche il PC |

Punti insidiosi documentati (vedi anche CLAUDE.md): identità antenna = intero
1/2/3 (il byte nei metadati è `(TX<<4|RX)`, nibble basso, 0→16); metadata
flags `0x0007` = ReadCount|RSSI|AntennaID; RSSI è un byte signed; il record
0x29 **non** ha un campo "tag data length" con i flag di default.

## 4. Gestione errori (gerarchia)

```
Exception
├─ SilionError            status != 0 dal lettore (errors.py, tabella STATUS_MESSAGES)
│   └─ NoTagError         0x0400 "nessun tag" — NON è un guasto
├─ SilionFrameError       framing: header/CRC/lunghezza/cmd-echo errati, buffer 0x29 troncato
│   └─ SilionTimeoutError lettore muto: nessuna risposta entro il timeout
├─ TransportNotOpenError  I/O richiesto prima di open()
└─ ValueError             input non valido, sollevato PRIMA di toccare il filo
```

Regole:
- La validazione input avviene **prima** dell'I/O → un `ValueError` garantisce
  che nulla è stato trasmesso.
- `SilionTimeoutError` è sottoclasse di `SilionFrameError`: il codice esistente
  che intercettava quest'ultima continua a funzionare.
- `read/write_try_all_antennas` non propagano errori per-antenna: registrano
  l'esito (`{"ok": False, "error": ...}`) e proseguono con l'antenna successiva.

## 5. Sistema di diagnostica (`diagnostics.py`)

### 5.1 Logging

`setup_logging(level, log_dir, file_prefix, force_debug)` — configurazione
unica per tutte le app: console + file per-sessione
`logs/<prefix>_<YYYYmmdd_HHMMSS>.log`. Livello da `config.yaml → logging.level`.

**Modalità debug**: `--debug` sulle CLI/GUI oppure `RFID_DEBUG=1` in ambiente.
A livello DEBUG il reader logga il **dump esadecimale TX/RX di ogni frame**
(`log.debug("TX %s"...)` in `reader.py`) — il primo strumento per confrontare
il traffico con il manuale.

### 5.2 Checkpoint

`checkpoint(logger, nome, **campi)` emette una riga:

```
2026-07-09 10:31:02 INFO rfid_silion.reader: CHECKPOINT inventory | stored=3 parsed=3 epcs=1
```

Formato stabile e greppabile (`findstr CHECKPOINT logs\*.log`). Checkpoint
attivi:

| Nome | Dove | Campi |
|---|---|---|
| `boot_firmware` | reader | firmware, hardware |
| `inventory` | reader | stored, parsed, epcs |
| `read_try_all_antennas` | reader | ok=[...], fail=[...] |
| `write_try_all_antennas` | reader | ok=[...], fail=[...] |
| `health_check` | reader | ok, antennas |
| `step1_done` | step1 | ok, report |
| `health_check_cli` | health_check | ok, warnings |

Linee guida per aggiungerne: nomi `snake_case` stabili (sono un'API per gli
script di analisi log), valori senza spazi, un checkpoint per *esito di fase*,
non per riga di codice.

### 5.3 Contatori (`DiagCounters`)

Ogni `SIM7200Reader` espone `reader.diag`, aggiornato da `_command()`:

| Campo | Significato | Se cresce → sospetta |
|---|---|---|
| `commands_sent` / `responses_ok` | scambi totali / riusciti | — |
| `timeouts` | nessuna risposta | cavo/porta/IP errati, lettore spento |
| `frame_errors` | CRC/lunghezza/cmd errati | rumore, baud errato, altro device sulla porta |
| `status_errors` | comando rifiutato (status ≠ 0/0x0400) | parametri, regione, boot mancante |
| `no_tag_events` | 0x0400 | non è un guasto: nessun tag in campo |
| `bytes_discarded` | byte spuri pre-header | rumore seriale persistente |
| `last_error`/`last_error_at` | ultimo errore | punto di partenza della diagnosi |

Lo snapshot (`diag.snapshot()`) è incluso nel JSON di Step 1
(`"diagnostics"`), nel report health e nel report diagnostico della GUI.

### 5.4 Health check

- **API**: `reader.health_check()` → dict `{timestamp, transport, booted,
  firmware_info, antennas_connected|antennas_error, counters, ok}`. Interroga
  solo 0x61/05 (non modifica lo stato).
- **CLI**: `python run.py health` (o `src/app/health_check.py
  [--with-inventory] [--debug]`) → sintesi `[OK]/[FAIL]/[WARN]` a video +
  `logs/health_<ts>.json`. Exit code: `0` OK, `1` problemi (es. antenna
  configurata non rilevata), `2` connessione/boot falliti. Pensato per
  automazione/controllo di gestione (es. cron + raccolta dei JSON).
- **GUI**: pulsante "Report diagnostico" → `logs/diagnostica_gui_<ts>.json`.

### 5.5 Robustezza I/O

`_recv_frame` si riallinea sull'header `0xFF` scartando fino a 64 byte spuri
(conteggiati). Oltre → `SilionFrameError`. Il flush dell'input prima di ogni
comando (`_command`) resta la prima difesa contro risposte orfane.

## 6. Configurazione (`src/app/config.yaml`)

| Sezione | Chiavi | Note |
|---|---|---|
| `serial` | port, baudrate (115200), timeout_s, inter_byte_timeout_s | trasporto attivo se presente |
| `tcp` | host (192.168.1.100), port (8080), timeout_s | usato solo se `serial` assente |
| `serial_disabled` / `tcp_disabled` | come sopra | sezione "parcheggiata" dalla GUI (trasporto non attivo) |
| `reader` | region (0x08 EU), max_power_dbm | regione EU obbligatoria in Italia |
| `antennas` | lista {id, role, read_power, write_power} | potenze in **cdBm**; `role` è solo descrittivo |
| `inventory` | antennas, timeout_ms, metadata_flags (0x0007) | |
| `tag_access` | bank_*, access_password_hex, timeout_ms, write_data_hex, read_user_words | |
| `logging` | dir (logs), level (INFO) | `--debug`/`RFID_DEBUG` forzano DEBUG |
| `geometry` | dati antenna/volume | usati solo come documentazione |

**Selezione trasporto = presenza di chiave** (`serial` vince su `tcp`): per
questo il salvataggio dalla GUI rinomina la sezione inattiva in `*_disabled`.
Attenzione: `yaml.safe_dump` non conserva i commenti del file.

## 7. Test

```
python run.py tests                  # tutti (21), senza hardware
python src/tests/test_protocol.py    # framing/CRC/parser tag (10)
python src/tests/test_reader.py      # reader con FakeTransport (11)
```

- Plain `assert` + runner `_run_all()`; compatibili con pytest se installato.
- `test_protocol.py` è anche il **registro dei frame/CRC noti-buoni** dal
  manuale (es. `FF 00 04 1D 0B`; ricordare: `1D 0C` è il CRC di 0x03, non un
  typo di 0x04).
- `FakeTransport` (in `test_reader.py`) è il pattern per testare nuovi
  comandi senza hardware: precaricare la risposta con `make_response(cmd,
  status, data)` e verificare `t.tx` contro `build_packet(...)`.

## 8. Estensioni previste (non implementate)

| Step | Cosa | Vincoli noti |
|---|---|---|
| 2 | Lock `0x25` / Kill `0x26` | opcodes verificati sul manuale 2024-12 (NON 0x82/0x65); prima scrivere la password su reserved bank con 0x24 |
| 3 | Inventory asincrono `0xAA48`/`0xAA49` | richiede **framing esteso** (`"Moduletech"`+SubCRC+`0xBB`) che `protocol.py` non costruisce; in alternativa Active-Upload push |
| — | `HttpTransport` | API HTTP+JSON `/moduleapi` (porta 80 o 127.0.0.1:20085), CRC-free; solo su firmware Hualong/Hc32f460 — da confermare |
| — | GPIO SLD1090 | GPI `0x66` / GPO `0x96` |
| — | Persistenza SQLite | prevista dal piano §5 |

## 9. Limiti noti / da verificare su hardware

- Porta TCP 8080 sulla SLD1090 specifica (da manuale, non ancora provata).
- Efficacia reale di `0x91/03` per le potenze (il DEMO usa `0x04` con
  setup-time extra).
- Possibile status `0x0505` high return loss: le SLP1027 sono tarate
  902–928 MHz, fuori banda EU (accettato per il laboratorio).
- Tkinter e thread: le operazioni girano su thread worker e aggiornano la UI
  via `root.after` (corretto), ma `messagebox` è ancora chiamato da worker in
  `_guard` (rilievo #19 del report, rischio basso).
