# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Memoria di lavoro aggiornata

Ultima sessione: [NOTE_SESSIONE_2026-10-03.md](NOTE_SESSIONE_2026-10-03.md) —
YOLO locale come terzo motore del riconoscimento (`docs/YOLO.md`): il modello
base COCO non riconosce i contenitori (0/6); addestrato sulle sei foto conta
la foto esclusa 5/6; con scene sintetiche generate in locale
(`tools/scene_sintetiche.py`) 6/6 e 47/48 sulle varianti ruotate: è il
modello predefinito. Prima ancora,
il 2 ottobre: [NOTE_SESSIONE_2026-10-02.md](NOTE_SESSIONE_2026-10-02.md).

Prima di riprendere, leggere [NOTE_SESSIONE_2026-10-01.md](NOTE_SESSIONE_2026-10-01.md),
in particolare la ripresa dopo il limite: email con più colli, distinte attese,
SQLite schema 9. La successiva app `qr-webcam` è ora integrata nella Ricezione:
735/735 test superati, guida `docs/QR_WEBCAM.md`. Webcam nella Ricezione
confermata funzionante dall'utente («ok funziona»); limiti del collaudo nelle note.
La [sessione del 6 settembre](NOTE_SESSIONE_2026-09-06.md) documenta l'accettazione
giornaliera confermata dall'utente. Le note storiche sottostanti vanno lette insieme
a questi aggiornamenti e ai rispettivi limiti del collaudo.

## What this is

A cross-platform Python driver + apps to control a **Silion SIM7200** UHF RFID reader
(Impinj E710 module on an **SLD1090** baseboard) with **3 SLP1027 antennas** arranged as a
reading "parallelepiped" (2 horizontal antennas side-by-side = base, 1 vertical antenna = height).

The driver speaks the **native Silion binary serial protocol directly** — it deliberately does
**not** use the vendor `ModuleAPI*.dll` / `.jar` (Windows-only). This is the central design
decision: it keeps the code multiplatform and lets the same protocol run unchanged over
**serial (USB/RS232)** or **TCP/IP** (the SLD1090 exposes both).

Code comments, docstrings, and user-facing strings are in **Italian** — match that when editing.

## Commands

```bash
pip install -r requirements.txt          # pyserial, pyyaml, matplotlib, numpy

python run.py                            # interactive menu
python run.py webui                      # operational web UI on 127.0.0.1 (the everyday one)
python run.py gui                        # standalone Tkinter GUI (bench tool)
python run.py service-gui                # test GUI through a dedicated service process
python run.py step1                      # CLI Step 1 read/write test
python run.py tests                      # unit tests, no hardware (protocol/reader/transport/service)
python run.py health                     # reader health check (JSON report in logs/)
python run.py service                    # JSON-RPC 2.0 JSONL host on stdin/stdout
python run.py tag-profile                # measure a tag's TID + USER memory (needs hardware)
python run.py lims                       # intake / sealing / receiving GUI
python run.py campaign                   # read-reliability tuning campaign (needs hardware)
python run.py diario                     # replay the prototyping journal (no hardware)
python run.py webui --simulato [file]    # the whole web UI on a simulated reader
python run.py webui --config <file>      # ...against another config (keeps the real archive out)

python src/tests/test_protocol.py        # framing/CRC/tag-parser tests directly
python src/tests/test_reader.py          # reader tests (FakeTransport, counters)
python src/tests/test_transports.py      # transport tests (fake serial/socket)
python src/tests/test_service.py         # headless service contract tests
python src/tests/test_rpc.py             # JSON-RPC dispatcher/JSONL host tests
python src/tests/test_webui.py           # web UI routes/flow over real HTTP, no hardware
python src/tests/test_flusso_continuo.py # the operational flow of `flusso-di-lavoro.txt`, over HTTP
python src/tests/test_diario.py          # journal: radio capture, UI channel, redaction
python src/tests/test_lims_qr.py         # QR encoder + Base45 (published capacities, read-back)
python src/app/step1_test_rw.py --config src/app/config.yaml  # read-only default
python src/app/gui.py --config src/app/config.yaml
```

- `--debug` (gui/step1/health) or env `RFID_DEBUG=1` forces DEBUG logging with
  hex TX/RX frame dumps. All apps log to per-session files under `logs/` via
  `rfid_silion.diagnostics.setup_logging`.

- `run.py` is the entry point for everything: it injects `src/` into `PYTHONPATH` and
  checks dependencies without mutating the environment. `--install-deps` explicitly installs
  only missing packages. Modules under `src/app/` and
  `src/tests/` also self-insert `src/` into `sys.path`, so they run standalone too.
- **All tests under `src/tests/` run without hardware.** `step1` and `gui` need the physical reader
  connected (serial or TCP).
- Tests are plain `assert` functions collected by a custom `_run_all()` and are also
  configured for pytest with coverage in `pyproject.toml`. To run one in isolation, call the
  function, or use
  `pytest src/tests/test_protocol.py::test_name` if you have pytest installed. The tests double
  as the canonical record of known-good frames/CRCs from the manual.

## Architecture (layers, bottom-up)

The driver is a strict stack under `src/rfid_silion/`; the apps under `src/app/` only use the
top layer. Understanding the boundaries matters because the protocol is identical across
transports — only the bottom layer changes.

1. **`transports.py`** — `Transport` ABC (`open/close/write/read/flush_input`) with
   `SerialTransport` (pyserial) and `TcpTransport` (socket). Raw byte I/O only; knows nothing
   about frames. `read()` returns `b""` on timeout.
2. **`protocol.py`** — pure framing/CRC, no I/O. `build_packet(cmd, data)` and
   `parse_response(buf)`, plus all `CMD_*` / `BANK_*` / `META_*` constants. Frame layout:
   `0xFF | DataLen | Cmd | [Status(2) on responses] | Data | CRC16(MSB,LSB)`. CRC is
   **CRC-16 CCITT** (poly `0x1021`, init `0xFFFF`) computed over **everything except the `0xFF`
   header and the 2 CRC bytes**. `DataLen` counts only the `Data` field.
3. **`reader.py`** — `SIM7200Reader(transport)`: one method per command, each wraps
   `_command()` (flush → send → read one full frame → verify cmd echo) and calls
   `check_status()`. `reader_from_config(cfg)` is the factory. Context-manager (`with`) capable.
4. **`service.py`** — versioned headless application boundary (`RFIDService`, API 1.4):
   JSON-safe DTOs/responses, lifecycle, health, events and guarded writes. `RFIDBackend` /
   `RFIDServiceBinding` distinguish injectable owned/shared lifecycle. **`rpc.py`** maps
   JSON-RPC 2.0 to this boundary without I/O; **`service_host.py`** exposes JSON Lines on
   stdin/stdout; **`client.py`** manages that subprocess while implementing `RFIDBackend`.
   The framework can pass the same client instance to the GUI so one host alone owns hardware.
   GUI and framework adapters must not import reader/protocol/transports.
5. **`tags.py`** — `Tag` dataclass + `parse_tag_buffer()` for the `0x29` Get Tag Buffer payload,
   driven by the metadata-flags bitmask.
   **`diario.py`** - the prototyping journal: append-only JSONL of everything
   that happens (keys pressed, data typed, every radio exchange with
   RSSI/antenna). `BackendTracciato` wraps an `RFIDBackend`, so it sits on the
   seam the architecture already defines and works over `RFIDService` *and*
   over the RPC client.
   **`simulazione.py`** - `FakeTagBackend`/`SimulatedTag`, promoted out of
   `tests/` because it is also the bench the web UI runs on without hardware;
   `tests/fake_backend.py` re-exports it, so every existing test is untouched.
   **`scenario.py`** - rebuilds that bench from a journal: same tags, same
   TIDs, same USER memory, same difficulty. See `docs/DIARIO_PROTOTIPAZIONE.md`.
   **`src/lims/`** sits *above* this boundary (a client of `RFIDService`, API 1.2) and
   implements sample traceability: `model` (patient/case/specimen/container),
   `codec` (12-byte pseudonymous EPC + sample payload, versioned binary schema),
   `crypto` (AES-256-GCM sealing, AAD binds payload to EPC **and TID**),
   `db` (SQLite archive; `containers.epc UNIQUE` is the EPC uniqueness registry,
   **perpetual** — tags are write-once and never come back), `tagio`
   (provision/survey orchestration), `profiler` (TID decode + USER-memory
   measurement), `sealing` (closed-set verification of a sealed box — the core
   requirement), `riempimento` (the opposite and complementary half: it
   *builds* that set while the box is open, recognising each sample as it goes
   in), `campaign` (power/Gen2 tuning against *two* objectives: read
   everything inside, nothing outside), `labels` (ZPL), `manifest` (encrypted
   shipping manifest + reconciliation), `qr`/`base45`/`tabella` (the printed
   distinta and its QR, see `docs/DISTINTA_QR.md`), `riscontro` (the arrival
   report the recipient sends back, which is the only way the sender ever
   learns how it went). It must never import
   reader/protocol/transports. Wire format is documented in
   `docs/SCHEMA_DATI_TAG.md` — that file is the contract with the receiving lab,
   so changing it breaks tags already in transit. Read-reliability reasoning is in
   `docs/AFFIDABILITA_LETTURA.md`; the five measurement panels that tune it
   are in `docs/STRUMENTI_DI_MISURA.md`.
   **`src/webui/`** sits above *both*: `workflow.py` orchestrates `lims.*` and
   returns JSON-safe dicts (no HTTP, no Tk — testable without a port);
   `server.py` is a stdlib `ThreadingHTTPServer` bound to `127.0.0.1` with a
   per-session token. Two channels, deliberately distinct: `POST /rpc` hands the
   body straight to `RFIDRPCDispatcher` (the service contract), `POST /api/<op>`
   hits the workflow layer, `GET /api/eventi` is SSE. `static/` is the
   frontend — no CDN, no build step, no downloaded fonts (the lab is offline).
6. **`errors.py`** — `status_to_exception()` maps Silion status codes to `SilionError`
   subclasses. `0x0400` → `NoTagError` (non-fatal: "no tag in field").
7. **`diagnostics.py`** — cross-cutting, no I/O: `setup_logging()` (single logging
   config for all apps), `checkpoint(log, name, **fields)` (greppable
   `CHECKPOINT name | k=v` lines), `DiagCounters` (per-reader runtime counters:
   timeouts vs frame_errors vs status_errors; exposed as `reader.diag`, embedded in
   every JSON report). `reader.health_check()` returns a JSON-able status dict.
   Exception split: `SilionTimeoutError` (reader silent) and `SilionTransportError`
   (I/O/disconnect) are subclasses of `SilionFrameError` — catch order matters.

Apps: `app/step1_test_rw.py` (scripted CLI test → JSON report in `logs/`),
`app/gui.py` (Tkinter + matplotlib live control + 3D volume view), `app/health_check.py`
(quick CLI diagnosis → JSON, exit 0/1/2), `app/config.yaml` (single source of runtime
config). GUI note: all RFID operations pass through `RFIDService` on one executor worker;
UI callbacks are delivered through a Tk-owned queue. A framework can call
`app.gui.launch_gui(..., service=service)`; shared ownership never stops or reopens the
hardware transport when the window detaches. GUI "Salva config" merges
into the existing
YAML and parks the inactive transport section as `serial_disabled`/`tcp_disabled`
(transport selection is by key presence, `serial` wins).

## Web UI (`src/webui/`)

The everyday interface. Tkinter GUIs stay as bench tools.

- **One radio operation at a time.** `RFIDService` has its own lock, but the real
  invariant is at flow level: the write guard uses global `_observed_epcs`, so two
  overlapping operations corrupt each other. The server serialises with an explicit
  lock and answers `409` — it does not queue. Operations that don't touch the radio
  (`stato_accettazione`, `registro`, …) stay available *during* a long seal; that
  split is in the operations table in `server.py`, second tuple element.
- **The scene is driven by real events, never a timer.** `TagIO.provision(on_step=)`
  and `SealingSession.run(on_progress=)` push through SSE. After a successful write
  the interface does *not* advance on a timeout: the pad watcher has to see the
  written EPC leave the field. Advancing on a clock would assert an operator gesture
  that may not have happened.
- **The write station polls the pad** (`sorveglia`) so no button says "I've put it
  down". That inventory is needed anyway — the write guard demands exactly one tag.
- **The write station carries its own radio posture, and only its own antennas.**
  Sealing, filling and the campaign each set their Gen2 before every pass; the
  write station did not, so it inherited whatever the last measurement panel had
  left on the module — and "Metti i valori consigliati" leaves S2, which stops
  tag writing dead (see the Target-A note below). `Workflow._assetto_accesso`
  applies session S0 + static target A **and the powers of the write antennas
  alone**, restoring both in `finally` (tested on the failure path). `q` and
  `rf_mode` are left untouched: they are the operator's tuning, and access
  commands use Q=2 regardless. `antenne_scrittura()` refuses to fall back to
  "every antenna" — during a write only the station may transmit, or the
  one-tag-in-field guard would be looking at half the bench.
- **`authorized_rewrite` is reachable from the UI**, as a button that appears
  only when `ProvisionResult.error_code == "tag_gia_scritto"` — a code, not a
  message match, so improving the wording can't silently remove the button. It
  is the prototype's answer to burning a tag per test: Gen2 EPC memory rewrites
  ~10⁵ times, the guard is ours. `db.assign_tag` closes the previous open
  assignment when the rewrite is authorised, or the tag's history would have two
  live rows and `active_assignment` would stop meaning anything.
- **`LimsDatabase(single_thread=False)`** in the workflow: every HTTP request lands on
  a different thread. `Workflow._scrittura` (RLock) wraps the multi-row sequences
  (intake, count change, void, shipment prep); single statements rely on SQLite's own
  atomicity. Long radio operations are already mutually exclusive.
- **Palette is hematoxylin & eosin**, and `--eosina` is reserved for RF activity and
  the active state — nothing else. A glance at the screen says whether the reader is
  talking to a tag. `--allarme` is deliberately darker and more saturated so "missing
  sample" can't be confused with "reading".
- **No downloaded fonts, no CDN, no build step.** Bahnschrift (numbers/labels), Segoe
  UI Variable (prose), Consolas (EPC/TID) — all already on Windows.
- **Anagrafiche live in `config.yaml`, not the DB**: `laboratorio` (own lab),
  `operatori`, `destinatari`. They are configuration, not clinical records — they
  belong with the transport settings and travel with the installation. The operator
  select rejects a name outside the list once one is configured; the seal's
  destination dropdown is fed by `destinatari` (it was a free-text field before).
  `lims.lab_name` is still read as a fallback so installed configs keep their
  printed name.
- **The archive lists everyone by default.** An empty query means *all*, not
  *none*: whoever is chasing a case from three months ago often can't remember
  the surname, only that it happened, and an empty table in front of a full
  archive sends them guessing. Opening the screen loads the first page;
  `cerca_paziente` returns the page **plus the archive total**, because "50
  patients" and "50 of 1284" read the same and mean different things. Ordering is
  a two-question toggle — *più recenti* for "what came through here lately",
  *alfabetico* for "find me this surname I can't spell" — and `_filtro_pazienti`
  is shared by list and count so the total can never describe a different set
  from the rows under it.
- **Archive answers the outside question.** `db.search_patients` /
  `patient_history` / `container_trace`; `workflow.storico_paziente` builds the
  per-accession summary (pieces, written, shipped, where, when, who supervised,
  seal outcome). "All went well" means: every active container written, shipped,
  and its seal complete — computed in one place, not re-derived in the UI.
  Schema v3 adds `shipments.operator/sealed_at/sent_at/sealing_ok/sealing_detail`;
  **that migration is a Python callable, not a script**, because
  `ALTER TABLE ADD COLUMN` isn't repeatable and a half-applied script would make
  the archive permanently unopenable.
- **Filling and sealing are two different things and both are needed.** The
  filling (`lims/riempimento.py`) knows *what went in*, but with the lid open:
  what it sees might be sitting next to the box. The seal certifies *what is
  inside* the closed box, but only against a list somebody had to give it. A
  container left beside the box is counted by the first and missed by the
  second, so the shipment doesn't leave and you find out why.
- **The filling thresholds are asymmetric on purpose.** Entering takes few
  sightings (the beep must land right after the gesture, or the operator has
  already moved on); leaving takes more. Dropping a row makes the operator
  believe a sample is missing that is already in; keeping one too many is
  caught by the seal. Sightings are **not** reset by a single missed round:
  a tag shielded by the jar in front alternates, and zeroing the count on
  every gap would mean never recognising exactly the difficult tags that
  repeated reads exist for.
- **Filling power is deliberately low** (`min(seal_powers_cdbm)`). A false
  positive here silently adds to the shipment a container that is on the next
  table. The seal would then hunt for it in vain: the error surfaces, but late.
- **Correcting a wrongly-added sample excludes it, it doesn't forget it.** The
  tag is still in the field (that's why it was read), so forgetting it means it
  walks back in on the next round. It stays excluded until it physically leaves.
- **A sealed shipment no longer blocks the next box.** Writing more tags than
  fit in one transport container is the normal case; the sealed one waits for
  the courier under *Scatole da finire*, with what it still needs.
- **`annulla_accettazione` never pretends to un-write a tag.** Written containers
  stay — they exist physically, with a label on them. Only the unwritten ones are
  voided.
- **Dates and times are Italian in the UI** (`dataOra` / `data` in `app.js`);
  storage stays ISO 8601.
- **Collection date and time come from the ward label, not from the clock.** A
  sample may have been taken yesterday. The date pre-fills to today because it
  usually is; the time stays empty until read, because an invented midnight is
  worse than a blank field.
- **One accession can hold several different specimens of the same patient.**
  How many samples a patient has is not knowable in advance: you find out
  taking them off the tray. So one transcription per container stays the
  normal path. But when they *are* registered together, description,
  material, fixative, site and warnings are usually **all different** -
  `db.plan_accession` creates one `Specimen` per reperto and each tag gets its
  own codes. Writing them alike would be an error nobody can correct at the
  far end.
- **Container numbering belongs to the accession, not to the specimen.** Two
  jars of colon plus one lymph node are 1/3, 2/3, 3/3 - not 1/1, 1/1, 1/1.
  The EPC already carries an accession-scoped index; `plan_containers` takes
  `start_index`/`accession_total` for exactly this.
- **One campione, one container: the UI has no count field.** A number alone
  would not say *which* samples they are, and how many a patient has is not
  knowable before holding them. A second jar is a second campione, with its
  own description. `contenitori` per reperto still exists in the API (and in
  the tests) and defaults to 1; `adjust_container_count` stays reachable but
  is no longer offered in the UI, and refuses outright when the accession has
  more than one reperto.
- **With one container the count confirmation isn't asked.** That question
  exists because the total goes into the chip irreversibly; with one jar in
  hand it has no object. It is still logged (`count_confirm`, "conferma
  implicita"). From two up it is mandatory as before.
- **The station returns to the form by itself** once the written container
  leaves the pad, not on a timer but on the reading that saw it go. Asking
  "Nuova accettazione" after every jar would be a ceremony thirty times a day.
- **Everything the recipient will ever know is on the printed sheet.** The two
  archives don't talk: the distinta carries the full table (patient identifier,
  name, sex, birth date, collection date and time, sample, codes, EPC) plus a
  QR with the same rows. It contains health data in the clear, QR included:
  that is the direct consequence of having no shared archive, and it belongs in
  the envelope attached to the box, not taped outside. See `docs/DISTINTA_QR.md`.
- **"Tutto a buon fine" has one meaning and no shades.** Only a shipment whose
  arrival report says it arrived whole counts as arrived. Sent-and-never-
  confirmed is called *non confermato*, never *arrivato*: that difference is
  the entire reason `lims/riscontro.py` exists.
- **Settings screen owns the transport.** `applica_collegamento` is stop →
  `replace_config` → `start` (the config can't be swapped with the transport open),
  so it doubles as the connection test: if the reader answers with its version, the
  cable and parameters are right. `salva_impostazioni` rewrites `config.yaml` via a
  temp file + `replace`, parking the inactive section as `serial_disabled` /
  `tcp_disabled` — transport is selected by key presence, so leaving both would pick
  one at random. Serial ports are enumerated with `serial.tools.list_ports`; the
  SLD1090 shows up as "HDSC", which is why **USB and RS232 are the same transport**
  and the UI says so instead of offering a fake third option. The HTTP+JSON API is
  listed as an explicitly unavailable card — it exists in newer firmware but
  `HttpTransport` is not implemented, and hiding it would send the operator looking
  for a option the manual promises.
- **Strumenti lives inside Impostazioni, in a second tab.** `Impostazioni` has
  `data-scheda` panels (*Configurazione* / *Misure*); `mostra("impostazioni",
  "misure")` deep-links to the measurement side. These panels tune the prototype
  and do **not** affect the operational flows: `SealingSession._apply` and
  `inventario_campagna` set their own Gen2 every pass, which is why the panel
  says so out loud. Rationale for all five: `docs/STRUMENTI_DI_MISURA.md`.
- **The hardware census runs at connect, and it gates what the tools may offer.**
  `RFIDService.identify` (API 1.4) asks the module what it is — `0x03` version,
  `0x10` serial, `0x71` available bands, `0x72` temperature, `0x61/0x05`
  connected antennas — and `Workflow.connetti` stores it in `self.hardware`,
  which `descrivi()` then carries to every screen. **This is not cosmetic**:
  EX10 2024-12 §2.2 and §10.1 say a module certified for a single region refuses
  other bands (`0x010B`) *and* refuses per-frequency lists (the N field must be
  0). So on such a unit the wide antenna sweep is impossible — not a feature to
  enable, the certified firmware. The UI disables it and says the curve needs a
  real antenna analyser, instead of letting the operator hunt a fault that isn't
  there. A failed census does **not** fail the connection, and when nothing is
  known nothing is forbidden: it's the module that says no, not us.
- **The manual's numbers are the manual's, not ours.** VSWR threshold 7 ("It
  should usually be less than 7"), test power fixed at 20 dBm (the frame field is
  marked *invalid*), `VSWR=(RL+1)/(RL-1)` with `RL=10^(VL/10/20)` — pinned by a
  test against the manual's worked example (`VL=0x78` → 1.67). CE_LOW is
  865–867 MHz per appendix 2, not 865–868. A full sweep can take ~25 s, hence the
  30 s timeout.
- **The SLP1027 is a broadband patch, not a narrow resonator** — the datasheet's
  anechoic curves are 1.16–1.24 VSWR flat across 902–928 MHz. So the *minimum* is
  shallow and its exact position moves with noise; the solid number, and the one
  a supplier recognises, is the **usable bandwidth** (`_larghezza_banda`, under
  VSWR 2), which answers the datasheet's own "VSWR ≤1.3 over 902–928" in kind.
  `simulazione._vswr_modello` fits a single-resonator model to the published
  points instead of inventing a curve; below 900 MHz it is **extrapolation and
  says so**.
- **The antenna sweep must be able to leave the EU band, or it answers nothing.**
  A high VSWR at 866 MHz doesn't distinguish "bad antenna" from "good antenna
  tuned elsewhere" — and the SLP1027 is specified 902–928 MHz, so the second is
  the expected case. `diagnostica_antenna` therefore takes `da_khz/a_khz/passo_khz`
  (explicit `frequencies_khz`, no region code needed) and reports the **resonance
  minimum** plus its offset from the EU band centre: that offset is the number
  that goes in the supplier request. A minimum landing on the sweep **edge** is
  reported as an edge, never as a resonance — the real one is further out, and
  quoting it would give the supplier a frequency that doesn't exist. Curves
  accumulate on one graph instead of replacing each other, because three
  same-model antennas that disagree mean a cable, not a design. Colours are set
  via `element.style.stroke`: the CSS class default would otherwise win.
- **Region switching restores in `finally`, and it's tested on the failure path.**
  Firmware certified for one region refuses the others (`0x010B`); the UI treats
  that as a question, not an error, and offers to switch for the length of the
  sweep. A measurement that died halfway and left the station transmitting out of
  the ETSI band would be a silent one.
- **Every Gen2 apply reads back** (`read_gen2_settings`). The module can accept an
  RF mode it doesn't support and substitute it silently — without the read-back
  you'd believe you were measuring at max sensitivity while you weren't, and
  every number collected after that would be worthless.
- **`lims.modalita_scrittura: payload | solo_epc`** — tags whose USER memory can't
  hold the payload still run the whole workflow. In `solo_epc`, `provision` writes
  the EPC and stops (no seal, no USER write), and `survey_field` marks the tag
  `solo_epc`, **not** `illeggibile` — calling it broken would send the operator
  hunting a fault that isn't there. What is lost is the payload↔TID binding, i.e.
  the anti-clone defence; what remains is the perpetual EPC registry and the
  signed distinta. **The mode is never selected automatically**: profiling
  proposes, the operator confirms once, `app.config_misura` writes it into
  `config.yaml` line-by-line (comments survive), and a fixed banner says so on
  every screen. A defence that lapses in silence is one nobody decided. This mode
  only makes sense *after* the printed distinta carries the patient data.
- **Profiling lowers the byte threshold freely and raises it only on confirmation.**
  The threshold must hold the worst tag of the batch; raising it would make a
  smaller tag already written and in transit unreadable.
- **Under `prefers-reduced-motion` the RF arcs are painted statically** (`app.css`).
  They start at `opacity: 0` and only appear inside the animation, so killing
  animations would remove the only sign that the reader is transmitting.
- **Two stations, one page.** `data-postazione="banco|tavoletta"` on `<html>`
  selects the sizing; `static/tocco.css` is loaded last and every rule in it is
  scoped under `:root[data-postazione="tavoletta"]`, so the PC cannot regress
  from a tablet change. Resolution order is `?modo=` → localStorage →
  `matchMedia("(pointer: coarse)")` — the URL wins because that's what the
  Android home-screen shortcut carries. Layout overrides key on the *attribute*
  plus width/orientation, never on `pointer:`, so tablet mode is previewable
  (and testable) on a desktop; only the hover-neutralisation block keys on
  `(hover: none)`. The scarce axis on a 10" tablet is **height** (≈960×600), which
  is why `app.css`'s `max-width: 1100px` single-column rule is *undone* there.
  Rationale and the bench guide: `docs/POSTAZIONE_TAVOLETTA.md`.
- **The manifest is generated, not a static file** (`server.manifesto()`), because
  `start_url` must carry the token; the route is token-checked like the API, and
  `app.js` injects the `<link rel=manifest>` with the token it already has. The
  icons (`webui/icone.py`) are drawn in pure Python — no Pillow, no binary blobs
  in the repo — and are deliberately public: they are pixels, and Android fetches
  them without the page's token.
- **`webui.host: 0.0.0.0` is what lets a tablet in**, and it must go together with
  a fixed `webui.token`: the saved shortcut embeds the token, so a per-boot token
  breaks it every morning. `server.url` renders loopback when the host is a
  wildcard (`http://0.0.0.0:8770` is not openable); `indirizzi()` returns the LAN
  URLs, which only the server can know.

## Critical, non-obvious behaviors

- **Framework integration uses the service contract.** External callers use
  `RFIDRPCDispatcher` or `python run.py service`; JSON-RPC methods other than `rfid.describe`
  must declare `api_version: "1.0"`. Stdout is JSONL data only; logs go to stderr/files.
  Event consumers use `events(EventRequest)` and must resync when
  `history_truncated` is true; polling itself must not emit another event.

- **EPC changes are service operations.** `generate_epc` creates a candidate only;
  `write_epc` requires one freshly observed expected EPC, uses command `0x23`,
  consumes the stale target guard, and requires post-write inventory verification.
  Global uniqueness belongs to the future framework registry, not random generation.

- **Boot is mandatory.** At power-on the reader is in the bootloader; you must send
  `boot_firmware()` (`0x04`) first or every tag command fails with status `0x0101`
  ("unavailable command"). Both `step1` and the GUI's connect do this automatically.
- **Transport is selected implicitly by config keys, not a mode flag.** `reader_from_config`
  uses `serial:` if that key exists, else `tcp:`. In `config.yaml` the `serial:` block is active
  and `tcp:` is commented out — to switch to Ethernet you comment `serial:` and uncomment `tcp:`.
- **Powers are in centi-dBm (cdBm).** `2000` = 20.00 dBm in `config.yaml` and in
  `set_antennas_power`. The GUI shows plain-dBm sliders (5–30) and multiplies by 100.
- **Region must be EU `0x08`** for Italy (865–868 MHz). Do **not** set FCC/full-band to "help"
  the antenna — that transmits out of the EU band and is not ETSI-compliant. (The SLP1027 is
  tuned 902–928 MHz, so it underperforms in the EU band — accepted for lab; see
  `PIANO_PROGETTO.md` §2.1 for the supplier action item.)
- **Antenna identity is the integer id 1/2/3, not the `role` string.** Roles in config
  (`floor_left`, `wall_90`, …) are descriptive only and unused by driver logic. Physical layout:
  **1 = base left (horizontal), 2 = base right (horizontal), 3 = vertical wall (height)**. The
  GUI hardcodes these positions in `ANT_CENTERS` for the 3D plot.
- **Inventory vs access antenna selection are different `0x91` options.** Inventory cycles a list
  of `(tx, rx)` pairs (`set_antennas_for_inventory`, option `0x02`); read/write target a single
  antenna (`set_antenna_for_access`, option `0x00`). Step 1's design is to **retry read/write on
  each antenna in turn** (`read_try_all_antennas` / `write_try_all_antennas`) to map which
  antenna covers the tag.
- **Read/write use Option `0x05`** (access password, no Select filter) → they act on the *first
  responding tag*. `write_data` must be even-length (word-aligned) and ≤ 64 bytes (32 words).
  `ReadRequest.select_epc` (service) → `read_try_all_antennas(select_epc=)` → the Select
  filter targets one EPC among many. `TagIO.survey_field` uses it whenever more than one
  tag is in the field: without it a full box would attribute every payload to whichever
  tag answered first, and the count would still add up. **`0x23` takes the filter too**
  (§6.1, Tag Singulation "same as command 0x22", example 3) — `service.write_epc` passes
  `expected_epc`, which the observed-EPC guard has already required anyway.
- **Access commands always query Target A, and that is what breaks writing in S2.**
  EX10 2024-12 says it twice, identically, for Write EPC §6.1 p.101 and Read §6.5
  p.121: *"If Target A-B is set, the module will use Target A"*. In session **S2**
  the tag's inventoried flag stays on B while it is powered, and every inventory
  round puts it there — so the next access command finds nothing and answers
  `0x0400 "No tag found"` about a tag sitting still in front of the antenna. In
  **S0** the flag does not persist and the problem disappears. Measured
  2026-08-27: with S2 + dynamic A↔B the EPC write failed 5/5 and the TID read
  succeeded about half the time; the day before, in S0, it wrote 6/6 on the same
  antenna at the same power. Re-inventorying before the access does **not** fix
  it (it is what sets the flag to B); `Workflow._assetto_accesso` does, by
  putting the module in S0 for the length of the operation.
- **Writing needs more power than reading** — the manual states it flatly (p.99,
  "Required power: Writing tag > Reading tag"). Worth remembering when the write
  station is the lowest-powered antenna on the bench and the SLP1027 is tuned
  902–928 MHz but used at 865–867.
- **Inventory metadata flags `0x0007`** = ReadCount|RSSI|AntennaID; this is what makes per-antenna
  RSSI available. RSSI is a signed byte. The GUI estimates tag position as an RSSI-weighted
  (linear-power) centroid of the reading antennas.
- **CRC `1D 0C` is not a typo (common confusion):** `1D 0B` is the CRC of Boot Firmware `0x04`;
  `1D 0C` is the CRC of command `0x03` (Get Version, `FF 00 03 1D 0C`) — two different commands.
  `test_protocol.py::test_crc_boot_firmware` documents this.
- **`parse_tag_buffer` (`tags.py`) is the highest-risk parser** — the `0x29` per-tag record is
  `[metadata per active flags, ascending bit order] | EPCLength(2,bits) | PC(2) | EPC(N) | TagCRC(2)`,
  N = EPCLength/8−4. There is **no** standalone "tag data length" field at the default flags
  (a `0x0080`/BIT7 "embedded tag data" field only appears inside the metadata block when that bit
  is set). It has dedicated regression tests; keep them green if you touch it.

## Project status

Everything below is implemented and unit-tested, but **nothing has been validated on
hardware**: no tag has ever been profiled, and no tag datasheet exists in the repo.
Treat every threshold in `SealingPolicy` and in the tag-health logic as a prudent
starting value, not a measurement.

Implemented: the full driver command set needed for dense reading — extended
`Moduletech` framing, Gen2 parameters (`0x9B`), antenna standing-wave diagnostics
(`0xAA4A`), dense async inventory (`0xAA58`/`0xAA59`), Select filter, embedded read,
lock (`0x25`) - plus the whole sample workflow in `src/lims/`: continuous
intake, one-sample-at-a-time box filling with audible confirmation, closed-set
sealing, printed distinta with QR, and the arrival report that closes the loop.

**The prototyping journal is on by default** (`diario:` in `config.yaml`) and
the whole UI runs without hardware (`python run.py webui --simulato`). That is
how the flow above was exercised end to end while the reader is still on the
bench: see `docs/DIARIO_PROTOTIPAZIONE.md` and `docs/FLUSSO_OPERATIVO.md`.

Deliberately not implemented: kill (`0x26`).

Still missing (see `PIANO_PROGETTO.md` §5): PyQt dashboard, SLD1090 GPIO triggers
(GPI `0x66` / GPO `0x96`) — which is what would turn the box-closure proof from the
operator's word into a physical sensor reading — and `HttpTransport`.

Prototype rig: **all three antennas are floor-mounted** — 1 and 2 side by side with the
container resting on top (read), 3 further away (single-tag write). Coplanar antennas give
no height discrimination, so `SealingPolicy.min_antennas` is a weaker discriminator here
than it would be with the third antenna vertical.

## Hardware reference

Original Silion docs are in `MANUALI/` (protocol, SDKs, sample code, `Offerta-Parti/` datasheets);
newer revisions and DEMO links are in `ULTIMI MANUALI/` (EX10 protocol 2024-12, current
API/DEMO, SLD1090/SLP1027 datasheets). SLD1090 default IP is `192.168.1.100`; the reader acts as a
**TCP server on port 8080** (per "Basic Steps of Command Development" §1.2 — still worth confirming
on the specific unit). A newer firmware also exposes an **HTTP+JSON API**
(`POST http://<IP>/moduleapi/...` on port 80, or a local server on `127.0.0.1:20085`) that is
CRC-free — a candidate alternative transport. Over USB the SLD1090 enumerates as device "HDSC".
