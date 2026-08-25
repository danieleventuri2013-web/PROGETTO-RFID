# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

python src/tests/test_protocol.py        # framing/CRC/tag-parser tests directly
python src/tests/test_reader.py          # reader tests (FakeTransport, counters)
python src/tests/test_transports.py      # transport tests (fake serial/socket)
python src/tests/test_service.py         # headless service contract tests
python src/tests/test_rpc.py             # JSON-RPC dispatcher/JSONL host tests
python src/tests/test_webui.py           # web UI routes/flow over real HTTP, no hardware
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
4. **`service.py`** — versioned headless application boundary (`RFIDService`, API 1.0):
   JSON-safe DTOs/responses, lifecycle, health, events and guarded writes. `RFIDBackend` /
   `RFIDServiceBinding` distinguish injectable owned/shared lifecycle. **`rpc.py`** maps
   JSON-RPC 2.0 to this boundary without I/O; **`service_host.py`** exposes JSON Lines on
   stdin/stdout; **`client.py`** manages that subprocess while implementing `RFIDBackend`.
   The framework can pass the same client instance to the GUI so one host alone owns hardware.
   GUI and framework adapters must not import reader/protocol/transports.
5. **`tags.py`** — `Tag` dataclass + `parse_tag_buffer()` for the `0x29` Get Tag Buffer payload,
   driven by the metadata-flags bitmask.
   **`src/lims/`** sits *above* this boundary (a client of `RFIDService`, API 1.2) and
   implements sample traceability: `model` (patient/case/specimen/container),
   `codec` (12-byte pseudonymous EPC + sample payload, versioned binary schema),
   `crypto` (AES-256-GCM sealing, AAD binds payload to EPC **and TID**),
   `db` (SQLite archive; `containers.epc UNIQUE` is the EPC uniqueness registry,
   **perpetual** — tags are write-once and never come back), `tagio`
   (provision/survey orchestration), `profiler` (TID decode + USER-memory
   measurement), `sealing` (closed-set verification of a sealed box — the core
   requirement), `campaign` (power/Gen2 tuning against *two* objectives: read
   everything inside, nothing outside), `labels` (ZPL), `manifest` (encrypted
   shipping manifest + reconciliation). It must never import
   reader/protocol/transports. Wire format is documented in
   `docs/SCHEMA_DATI_TAG.md` — that file is the contract with the receiving lab,
   so changing it breaks tags already in transit. Read-reliability reasoning is in
   `docs/AFFIDABILITA_LETTURA.md`.
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
- **Archive answers the outside question.** `db.search_patients` /
  `patient_history` / `container_trace`; `workflow.storico_paziente` builds the
  per-accession summary (pieces, written, shipped, where, when, who supervised,
  seal outcome). "All went well" means: every active container written, shipped,
  and its seal complete — computed in one place, not re-derived in the UI.
  Schema v3 adds `shipments.operator/sealed_at/sent_at/sealing_ok/sealing_detail`;
  **that migration is a Python callable, not a script**, because
  `ALTER TABLE ADD COLUMN` isn't repeatable and a half-applied script would make
  the archive permanently unopenable.
- **`annulla_accettazione` never pretends to un-write a tag.** Written containers
  stay — they exist physically, with a label on them. Only the unwritten ones are
  voided.
- **Dates and times are Italian in the UI** (`dataOra` / `data` in `app.js`);
  storage stays ISO 8601.
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
  tag answered first, and the count would still add up.
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
lock (`0x25`) — plus the whole sample workflow in `src/lims/`.

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
