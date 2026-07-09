# Changelog

Formato ispirato a [Keep a Changelog](https://keepachangelog.com/it/1.1.0/).
Sezioni: Added / Changed / Fixed / Security / Diagnostics / Documentation.

## [0.2.0] — 2026-07-09 (branch `feature/diagnostics-logging-documentation`)

Revisione per controllabilità, manutenibilità e diagnosticabilità.
Dettaglio completo dei rilievi in `docs/REPORT_REVISIONE.md`.

### Added
- `src/rfid_silion/diagnostics.py`: modulo unico di diagnostica —
  `setup_logging()` (console + file per-sessione in `logs/`), `checkpoint()`
  (righe di log strutturate `CHECKPOINT nome | k=v`), `DiagCounters`
  (contatori runtime: comandi, timeout, errori frame/status, byte scartati).
- `SIM7200Reader.health_check()`: report sintetico JSON-serializzabile
  (trasporto, firmware, antenne connesse, contatori).
- `src/app/health_check.py`: CLI di diagnosi rapida (`python run.py health`)
  con exit code 0/1/2 e report JSON in `logs/health_<ts>.json`.
- GUI: pulsante **"Report diagnostico"** → salva health check + contatori in
  `logs/diagnostica_gui_<ts>.json` (per l'assistenza).
- Flag `--debug` (GUI, Step 1, health check) e variabile d'ambiente
  `RFID_DEBUG=1`: livello DEBUG con dump esadecimale dei frame TX/RX.
- `SilionTimeoutError` (sottoclasse di `SilionFrameError`): distingue
  «lettore muto» da «frame corrotto».
- `TransportNotOpenError` e `Transport.describe()` nei trasporti.
- Validazione input nel driver (prima dell'I/O): antenne 1–4, potenze
  1–3300 cdBm (warning oltre 3000 = 30 dBm), `word_count` 1–96, timeout
  1–65535 ms, banche 0–3, EPC pari ≤ 62 byte, dati write 1–64 byte pari.
- Resync sull'header 0xFF in `_recv_frame`: fino a 64 byte spuri scartati
  (rumore seriale) invece di fallire al primo byte errato.
- `src/tests/test_reader.py`: 11 test del reader con trasporto finto
  (roundtrip, resync, timeout, contatori, validazioni, health check).
- Test di regressione `parse_tag_buffer` su buffer troncato.
- `rfid_silion.__version__` (= 0.2.0) ed export completi nel package.
- `run.py`: voce di menu 4 / argomento `health`.

### Changed
- Step 1: logging via `diagnostics.setup_logging`; il report JSON include i
  contatori diagnostici (`"diagnostics"`); exit code 1 su errore fatale
  (prima sempre 0); rimossi import e variabili inutilizzati.
- GUI: logging su file (`logs/gui_<ts>.log`) oltre che nel riquadro Log;
  tutte le operazioni verso il lettore serializzate con lock (il polling
  salta il giro se il precedente è ancora in corso, invece di accodarsi).
- `read_try_all_antennas` / `write_try_all_antennas`: un timeout/errore di
  frame su una antenna non interrompe più il ciclo sulle successive.
- `run.py tests` esegue entrambi i moduli di test (protocollo + reader).

### Fixed
- **GUI `save_config` perdeva le sezioni non gestite dalla GUI**
  (`inventory`, `tag_access`, `logging`, `geometry`) rompendo il successivo
  Step 1: ora fa merge con la config esistente; la sezione del trasporto non
  attivo viene parcheggiata come `serial_disabled`/`tcp_disabled`.
- GUI: trasporto lasciato aperto (porta COM bloccata) se il boot falliva
  durante la connessione.
- GUI: input esadecimali non validati (dati scrittura / password accesso).
- GUI `do_verify`: doppia lettura ridondante; ora mostra anche gli esiti FAIL.
- Driver: `IndexError` anonimi su risposte corte/corrotte (boot, sync
  inventory, tag buffer) sostituiti da errori espliciti con dump esadecimale.
- Trasporti: I/O su trasporto non aperto ora dà `TransportNotOpenError`
  invece di `AttributeError: 'NoneType'`.
- TCP: la chiusura della connessione da parte del lettore viene loggata
  (prima era indistinguibile da un timeout).

### Security
- Nessuna credenziale nel codice; nessuna password scritta nei log.
- Regola documentata (manuale §18): non committare password di accesso reali
  in `config.yaml` (la default di laboratorio è `00000000`).

### Diagnostics
- Checkpoint attivi: `boot_firmware`, `inventory`, `read_try_all_antennas`,
  `write_try_all_antennas`, `health_check`, `step1_done`, `health_check_cli`.
  Estrazione: `findstr CHECKPOINT logs\*.log` (Windows) / `grep` (Linux).
- Contatori runtime per sessione in ogni report JSON (Step 1, health, GUI).

### Documentation
- `docs/REPORT_REVISIONE.md` — analisi progetto + esito revisione codice.
- `docs/DOCUMENTAZIONE_TECNICA.md` — bozza documentazione tecnica.
- `docs/MANUALE_UTENTE.md` — bozza manuale utente.
- `README.md` — sezioni diagnostica, health check, troubleshooting.
- `CLAUDE.md` — comandi e architettura aggiornati.

## [0.1.0] — 2026-06-25

### Added
- Driver nativo protocollo Silion (frame 0xFF + CRC-16 CCITT) su seriale e
  TCP; comandi boot/region/antenne/potenze/inventory/read/write.
- App Step 1 (CLI test read/write nel parallelepipedo) e GUI Tkinter con
  grafico RSSI e volume 3D.
- Test protocollo senza hardware (CRC e frame dal manuale).

### Fixed (audit 2026-06 vs manuale EX10 2024-12 e DEMO ufficiale)
- `parse_tag_buffer`: campo "Tag Data Length" inesistente (rompeva ogni
  inventory reale); antenna ID = nibble basso del byte `(TX<<4|RX)`.
- `get_antenna_connection`: skip del byte Option ripetuto in testa.
- Codici di init firmware `0xFFxx` reali in `errors.py`.
- Porta TCP di default corretta a 8080.
