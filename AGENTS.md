# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

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

python run.py                            # interactive menu (GUI / Step1 / tests / health)
python run.py gui                        # GUI directly  (run.bat / ./run.sh = same)
python run.py step1                      # CLI Step 1 read/write test
python run.py tests                      # unit tests, no hardware (protocol + reader)
python run.py health                     # reader health check (JSON report in logs/)

python src/tests/test_protocol.py        # framing/CRC/tag-parser tests directly
python src/tests/test_reader.py          # reader tests (FakeTransport, counters)
python src/app/step1_test_rw.py --config src/app/config.yaml --skip-write --skip-epc
python src/app/gui.py --config src/app/config.yaml
```

- `--debug` (gui/step1/health) or env `RFID_DEBUG=1` forces DEBUG logging with
  hex TX/RX frame dumps. All apps log to per-session files under `logs/` via
  `rfid_silion.diagnostics.setup_logging`.

- `run.py` is the entry point for everything: it injects `src/` into `PYTHONPATH` and
  **auto-installs missing deps** via pip before launching. Modules under `src/app/` and
  `src/tests/` also self-insert `src/` into `sys.path`, so they run standalone too.
- **Only the protocol tests run without hardware.** `step1` and `gui` need the physical reader
  connected (serial or TCP).
- Tests are plain `assert` functions collected by a custom `_run_all()` (not a pytest project —
  pytest is not a declared dependency). To run one in isolation, call the function, or use
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
4. **`tags.py`** — `Tag` dataclass + `parse_tag_buffer()` for the `0x29` Get Tag Buffer payload,
   driven by the metadata-flags bitmask.
5. **`errors.py`** — `status_to_exception()` maps Silion status codes to `SilionError`
   subclasses. `0x0400` → `NoTagError` (non-fatal: "no tag in field").
6. **`diagnostics.py`** — cross-cutting, no I/O: `setup_logging()` (single logging
   config for all apps), `checkpoint(log, name, **fields)` (greppable
   `CHECKPOINT name | k=v` lines), `DiagCounters` (per-reader runtime counters:
   timeouts vs frame_errors vs status_errors; exposed as `reader.diag`, embedded in
   every JSON report). `reader.health_check()` returns a JSON-able status dict.
   Exception split: `SilionTimeoutError` (reader silent) is a subclass of
   `SilionFrameError` (corrupt frame) — catch order matters for diagnostics.

Apps: `app/step1_test_rw.py` (scripted CLI test → JSON report in `logs/`),
`app/gui.py` (Tkinter + matplotlib live control + 3D volume view), `app/health_check.py`
(quick CLI diagnosis → JSON, exit 0/1/2), `app/config.yaml` (single source of runtime
config). GUI note: all reader operations are serialized behind `self._lock` (the 1 s
polling skips a cycle instead of queueing); GUI "Salva config" merges into the existing
YAML and parks the inactive transport section as `serial_disabled`/`tcp_disabled`
(transport selection is by key presence, `serial` wins).

## Critical, non-obvious behaviors

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

Step 1 (read/write validation in the volume) is implemented. Future steps (see `PIANO_PROGETTO.md`
§5) are **not** built yet: lock/kill (real opcodes **`0x25`/`0x26`** per the EX10 2024-12 doc — not
`0x82`/`0x65`), async inventory (**`0xAA48`/`0xAA49`**, which uses the *extended* `Moduletech`
frame format that `protocol.py` does not yet build/parse), PyQt dashboard, SLD1090 GPIO triggers
(GPI `0x66` / GPO `0x96`), SQLite persistence.

## Hardware reference

Original Silion docs are in `MANUALI/` (protocol, SDKs, sample code, `Offerta-Parti/` datasheets);
newer revisions and DEMO links are in `ULTIMI MANUALI/` (EX10 protocol 2024-12, current
API/DEMO, SLD1090/SLP1027 datasheets). SLD1090 default IP is `192.168.1.100`; the reader acts as a
**TCP server on port 8080** (per "Basic Steps of Command Development" §1.2 — still worth confirming
on the specific unit). A newer firmware also exposes an **HTTP+JSON API**
(`POST http://<IP>/moduleapi/...` on port 80, or a local server on `127.0.0.1:20085`) that is
CRC-free — a candidate alternative transport. Over USB the SLD1090 enumerates as device "HDSC".
