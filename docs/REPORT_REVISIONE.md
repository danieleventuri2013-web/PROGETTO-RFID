# Report di revisione — PROGETTO-RFID

Data: 2026-07-09 · Branch: `feature/diagnostics-logging-documentation` · Driver v0.2.0

Revisione completa del progetto orientata a **controllabilità, manutenibilità e
diagnosticabilità**. Questo documento copre l'analisi iniziale (Fase 1) e la
revisione del codice (Fase 2) con l'esito di ogni rilievo.

---

## Fase 1 — Analisi del progetto

### Stack e struttura

| Aspetto | Valore |
|---|---|
| Linguaggio | Python ≥ 3.10 (testato su 3.14) |
| GUI | Tkinter + matplotlib (grafico barre RSSI + volume 3D) |
| Dipendenze | pyserial, pyyaml, matplotlib, numpy (`requirements.txt`) |
| Database | **nessuno** (previsto SQLite in step futuri, non implementato) |
| API/backend | nessuno — comunicazione binaria diretta col lettore |
| `.env` / credenziali | nessuno — unica config: `src/app/config.yaml` (nessun segreto) |
| Script | `run.py` (launcher multipiattaforma), `run.bat` / `run.sh` |
| Test | `src/tests/` — plain `assert` con runner custom `_run_all()` (non pytest) |
| CI/deploy | assenti (progetto da laboratorio) |

### Struttura cartelle

```
run.py / run.bat / run.sh      launcher (menu: gui | step1 | tests | health)
src/rfid_silion/               DRIVER (stack a livelli, dal basso verso l'alto)
  transports.py                  I/O grezzo: SerialTransport / TcpTransport
  protocol.py                    framing + CRC-16 CCITT (nessun I/O)
  errors.py                      status code Silion -> eccezioni
  tags.py                        parser Get Tag Buffer (0x29)
  reader.py                      SIM7200Reader: un metodo per comando
  diagnostics.py                 [NUOVO] logging, checkpoint, contatori
src/app/                       APPLICAZIONI (usano solo reader.py)
  config.yaml                    unica fonte di configurazione runtime
  gui.py                         GUI Tkinter di controllo
  step1_test_rw.py               CLI test read/write -> report JSON in logs/
  health_check.py                [NUOVO] diagnosi rapida CLI -> JSON
src/tests/                     test senza hardware (protocollo + reader)
docs/                          [NUOVO] questa documentazione
MANUALI/, ULTIMI MANUALI/      doc del produttore (non versionate, ~230 MB)
```

### Punto di avvio e flusso dati

`run.py` → menu → app. Flusso tipico (Step 1): `config.yaml` →
`reader_from_config()` → `SIM7200Reader` → `boot_firmware(0x04)` →
`set_region(0x97)` → `set_antennas_power(0x91/03)` → inventory
(`0x22`+`0x29` → `parse_tag_buffer`) → read/write (`0x28`/`0x24`) per ogni
antenna → report JSON in `logs/`.

### Moduli critici (e perché)

1. **`tags.py::parse_tag_buffer`** — parser byte-level del record tag; un
   disallineamento corrompe EPC/RSSI/antenna di *tutto* l'inventario. Storia:
   già rotto e corretto nell'audit 2026-06; per questo ha test di regressione
   dedicati (ora anche sul caso "buffer troncato").
2. **`reader.py::_command/_recv_frame`** — ogni comando passa di qui; è il punto
   giusto per diagnostica e robustezza (resync, timeout, contatori).
3. **`protocol.py::crc16/build_packet/parse_response`** — un CRC errato =
   nessuna comunicazione. Confermato corretto contro il DEMO del produttore.
4. **`gui.py::save_config`** — unico punto che *scrive* la configurazione.

### Punti deboli emersi (sintesi)

- Nessuna diagnostica strutturata: log sparsi, nessun contatore, nessun health
  check, GUI senza log su file.
- Validazione input assente sia nel driver sia nella GUI.
- Concorrenza GUI: lock dichiarato ma mai usato.
- `save_config` distruttivo (perdeva metà del file di config).
- Nessun test sul livello reader/trasporto (solo protocollo puro).
- Documentazione tecnica e manuale utente assenti.

---

## Fase 2 — Revisione del codice (rilievi)

Gravità: 🔴 critica · 🟠 alta · 🟡 media · 🟢 bassa.
Stato: **RISOLTO** in questo branch oppure **APERTO** (con motivazione).

| # | File / funzione | Problema | Gravità | Esito |
|---|---|---|---|---|
| 1 | `gui.py::save_config` | Riscriveva `config.yaml` con sole 3 sezioni: perdeva `inventory`, `tag_access`, `logging`, `geometry` → il successivo Step 1 crashava (`KeyError`). Ruoli antenna salvati diversi da quelli originali. | 🟠 alta | **RISOLTO**: merge con la config esistente; la sezione trasporto inattiva è parcheggiata come `serial_disabled`/`tcp_disabled` (la selezione trasporto è per presenza di chiave). |
| 2 | `gui.py` (polling + pulsanti) | `self._lock` creato ma **mai usato**: col polling a 1 s e un'operazione lenta (>1 s), due thread scrivevano sulla stessa seriale → frame corrotti/risposte incrociate. | 🟠 alta | **RISOLTO**: tutte le operazioni reader sotto lock; l'inventory da polling usa acquire non bloccante (salta il giro invece di accodarsi). |
| 3 | `gui.py::_connect` | Se `boot_firmware()` falliva dopo `open()`, il trasporto restava aperto → porta COM bloccata fino a chiusura app. | 🟡 media | **RISOLTO**: close nel ramo di errore. |
| 4 | `gui.py::do_read/do_write/do_verify` | Nessuna validazione dei campi hex (dati, password): `bytes.fromhex` esplodeva con messaggio criptico; nessun controllo 4 byte password / ≤64 byte dati. | 🟡 media | **RISOLTO**: helper `_hex_bytes`/`_password_bytes` con messaggi chiari nel log GUI. |
| 5 | `gui.py::do_verify` | Chiamava `do_read()` e poi rileggeva: doppio giro di letture, log duplicato, esito FAIL non mostrato. | 🟢 bassa | **RISOLTO**: verifica autonoma, mostra anche i FAIL. |
| 6 | `reader.py::_recv_frame` | Il commento prometteva «scarta finché non trova header» ma il codice sollevava subito: un solo byte spurio sulla seriale faceva fallire il comando. | 🟡 media | **RISOLTO**: resync fino a 64 byte, conteggiato in `diag.bytes_discarded` + warning nel log. |
| 7 | `reader.py` / `protocol.py` | Timeout e frame corrotto = stessa eccezione (`SilionFrameError`) → impossibile distinguere «lettore muto» da «rumore» in diagnosi. | 🟡 media | **RISOLTO**: nuova `SilionTimeoutError` (sottoclasse, retrocompatibile) + contatori separati. |
| 8 | `reader.py::boot_firmware`, `sync_inventory` | Nessun guard sulla lunghezza della risposta → `IndexError` anonimo su risposta corta/corrotta. | 🟡 media | **RISOLTO**: controlli espliciti con dump esadecimale nel messaggio. |
| 9 | `reader.py` (tutti i comandi) | Nessuna validazione input: antenna 0/5, potenza 9000 cdBm, `word_count` 200, timeout 0 finivano sul filo e producevano status criptici. | 🟡 media | **RISOLTO**: validazioni con `ValueError` esplicito *prima* dell'I/O (antenne 1–4, potenze 1–3300 cdBm con warning >3000, word 1–96, timeout 1–65535, banche 0–3, EPC pari ≤62 B). |
| 10 | `reader.py::read/write_try_all_antennas` | Un timeout su una antenna (es. cavo staccato) abortiva l'intero ciclo invece di provare le successive — contro lo scopo della funzione (mappare la copertura). | 🟡 media | **RISOLTO**: `SilionFrameError` catturata per-antenna e registrata come esito FAIL. |
| 11 | `tags.py::parse_tag_buffer` | Buffer troncato → `IndexError` anonimo (il parser è il modulo a più alto rischio del progetto). | 🟡 media | **RISOLTO**: guard `_need()` su ogni campo → `SilionFrameError` con campo+offset; byte residui a fine parsing loggati come warning. Test di regressione aggiunto. |
| 12 | `transports.py` | `write`/`read` su trasporto non aperto → `AttributeError: 'NoneType'` anonimo. | 🟢 bassa | **RISOLTO**: `TransportNotOpenError` con messaggio esplicito. |
| 13 | `transports.py::TcpTransport.read` | `recv()==b""` (connessione chiusa dal lettore) indistinguibile dal timeout. | 🟢 bassa | **MITIGATO**: warning esplicito nel log (il chiamante vede comunque timeout). |
| 14 | `step1_test_rw.py` | Codice morto: import `os`, `time`, `SIM7200Reader`, `NoTagError` inutilizzati; variabile `s` mai usata. Exit code sempre 0 anche su errore fatale. | 🟢 bassa | **RISOLTO**: pulizia + exit 1 su errore (utile per script/automazione). |
| 15 | `gui.py` | Import morti (`logging` solo per basicConfig, `math` mai usato). | 🟢 bassa | **RISOLTO**. |
| 16 | assenza di log su file (GUI) e di contatori runtime | In caso di problema sul campo non restava traccia. | 🟠 alta | **RISOLTO**: `diagnostics.py` (logging centralizzato su file, checkpoint, `DiagCounters`), health check, report diagnostico dalla GUI. |
| 17 | assenza di test sul livello reader | `_command`, resync, contatori, validazioni non coperti. | 🟡 media | **RISOLTO**: `test_reader.py` con `FakeTransport` (11 test). |
| 18 | `run.py::ensure_deps` | Auto-`pip install --upgrade` all'avvio senza chiedere: comportamento invasivo (può aggiornare pacchetti di sistema). | 🟡 media | **APERTO** (scelta di progetto dichiarata in CLAUDE.md; suggerimento: chiedere conferma o usare un venv — non modificato per non cambiare la logica concordata). |
| 19 | `gui.py::_guard` e messagebox da thread worker | Tkinter non è formalmente thread-safe: `messagebox`/`showwarning` da thread secondari funziona su Windows ma non è garantito. | 🟢 bassa | **APERTO** (rischio basso in pratica; refactor consigliato: instradare via `root.after`). |
| 20 | `errors.py` | Codici estesi `0xEExx`/`0xAAxx`/`0x500F`/`0x50FF` non mappati (mostrati come "Unknown status"). | 🟢 bassa | **APERTO** (serve il framing esteso dello Step 3; già annotato nel codice). |
| 21 | `config.yaml::access_password_hex` | Password di accesso tag in chiaro nella config. | 🟢 bassa | **ACCETTATO**: è la default `00000000` di laboratorio. Regola aggiunta al manuale: non committare password reali; nessuna password è scritta nei log. |

### Verifica

- 21/21 test verdi (`python run.py tests`): 10 protocollo + 11 reader.
- `py_compile` pulito su tutti i file; smoke-import di GUI e CLI ok.
- **Non verificabile senza hardware**: comportamento reale di resync/health
  check sul lettore fisico — vedi checklist in `NOTE_SESSIONE_2026-06-25.md`.

### Prossimi miglioramenti consigliati (fuori scope di questa revisione)

1. Verifica su hardware (porta TCP 8080, potenza `0x91/03`, inventory reale).
2. Step 2 (Lock `0x25` / Kill `0x26`) e Step 3 (inventory asincrono, framing esteso).
3. `HttpTransport` (API HTTP+JSON `/moduleapi`, senza CRC).
4. Migrazione test a pytest (i test attuali sono già compatibili).
5. Persistenza SQLite delle letture (previsto dal piano, §5 PIANO_PROGETTO.md).
6. Chiusura rilievi aperti #18–#20.
---

## Aggiornamento v0.3.0 — 2026-07-22

È stato eseguito un secondo passaggio completo sui rilievi ancora aperti:

- trasporti: errori I/O normalizzati, disconnessione TCP distinta dal timeout,
  timeout temporanei applicati anche al socket/seriale e nuovo contatore
  `transport_errors`;
- protocollo/reader/parser: controlli rigorosi sulle lunghezze e sulle opzioni
  di risposta, configurazione separata dei timeout, potenza massima applicata
  dal driver, parser tag fail-fast su buffer ambigui o residui;
- GUI: un solo worker I/O, aggiornamenti Tk tramite coda, chiusura ordinata,
  inventory singolo/continuo/temporizzato con accumulatore limitato;
- sicurezza operativa: Step 1 read-only per default e scritture abilitate solo
  esplicitamente dopo inventory con un unico EPC; conferma aggiuntiva in GUI;
- qualità: packaging `pyproject.toml`, Ruff, copertura minima e CI
  multipiattaforma.

Verifica automatica finale: **32/32 test superati**, Ruff senza rilievi,
branch coverage **70,80%** (soglia 65%). Restano necessariamente da eseguire
le prove con lettore fisico per porta TCP 8080, RF/potenze e comportamento
reale delle tre antenne.
