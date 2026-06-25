# Note di sessione — 2026-06-25

Handoff per la prossima sessione. Riassume audit, fix applicati, cosa resta da
verificare **sull'hardware** e i prossimi passi.

## Cosa è stato fatto
Audit multi-agente (orchestratore + 2 worker + verificatore) del driver RFID
SIM7200 contro i nuovi manuali in `ULTIMI MANUALI` (riferimento autorevole =
**EX10 Module Communication Prorocol-2024-12.pdf**), seguito da un cross-check
**byte-per-byte col DEMO ufficiale** (`Bilingual Demo_260203/ReaderManager.exe` +
`ModuleAPI.dll` + `Command Develop Kit`). Il protocollo è ora allineato al
software di riferimento del produttore.

## Bug corretti (codice) — tutti verificati, 9/9 test passano
1. **🔴 `tags.py` `parse_tag_buffer`** — leggeva un campo "Tag Data Length"
   inesistente (`pos += tag_data_len // 8`) → `IndexError` su ogni inventory
   reale. Layout corretto del record: `[metadati] | EpcLen(2,bit) | PC(2) | EPC(N)
   | TagCRC(2)`, N = EpcLen/8−4. Rompeva **Step 1 CLI e GUI**. Ora con test.
2. **🔴 `tags.py` Antenna ID a nibble** — il byte antenna è `(TX<<4 | RX)`;
   l'antenna logica è il nibble basso (0→16): `0x11`→1, `0x22`→2, `0x33`→3.
   Il codice salvava il byte grezzo (17/34/51), rompendo `ANT_CENTERS[1/2/3]`
   della GUI e la mappa per-antenna dello Step 1. Fix: `& 0x0F`. (Trovato col
   parser C del vendore `ParseNextTag`.)
3. **🟠 `reader.py` `get_antenna_connection` (0x61 opt 0x05)** — non saltava il
   byte Option ripetuto in testa alla risposta → diagnostica antenne sfasata.
   Fix: parsing da `resp.data[1:]`.
4. **🟠 `errors.py`** — codici init `0xFFxx` reali (`0xFF11..0xFF17 / 0xFF1F /
   0xFFFF`) + `0x0404`. (I codici `0x90xx` del DEMO sono lato host/DLL, namespace
   diverso → restano gestiti come `SilionFrameError`/timeout, NON in `errors.py`.)
5. **🟢 Porta TCP = 8080** (era `1001`, valore inventato). Il lettore fa da server
   TCP su `192.168.1.100:8080` (manuale "Basic Steps of Command Development" §1.2).
   Aggiornato in `transports.py`, `reader.py`, `gui.py`, `config.yaml`.
6. **Doc** (`CLAUDE.md`, `PIANO_PROGETTO.md`): Lock=`0x25`, Kill=`0x26`,
   async=`0xAA48/0xAA49`, porta 8080, chiarimento CRC (`1D 0C` = CRC di `0x03`,
   non un typo di `0x04`).

## Confermato CORRETTO dal DEMO (nessuna modifica necessaria)
Formato frame, CRC (sorgente `CRC16test.cpp`), boot `0x04`→`1D 0B`, regioni
(EU=`0x08`, NA=`0x01`, Cina=`0x06`), comandi `0x22 / 0x29 / 0x28 (bank,addr,wc) /
0x24 (addr prima di bank) / 0x23 / 0x91 (opt 00/02/03) / 0x61 / 0x72`.
Comando potenza `0x91/0x03` (id+rp+wp) valido; il DEMO usa `0x04` (= potenza **+
tempo di setup/dwell** antenna, 2 byte extra `01F4`) — forma più ricca, non un bug.

## DA VERIFICARE SULL'HARDWARE (non verificabile su carta)
- [ ] Porta TCP **8080** effettiva sulla SLD1090.
- [ ] Comando potenza: `0x91/0x03` applica davvero la potenza? Se no, passare a
      `0x04` = per-antenna `id + readPwr(2) + writePwr(2) + setupTime(2, es. 0x01F4)`.
- [ ] Inventory reale: EPC, antenna 1/2/3 e RSSI corretti (parser ora fixato).
- [ ] Eventuale status `0x0505` "high return loss" (antenna SLP1027 tarata
      902–928, fuori banda EU 865–868).

## Prossimi passi
- **Step 2** — Lock `0x25` (`timeout|opt|accessPwd(4)|mask(2)|action(2)[+filtro]`)
  e Kill `0x26` (`timeout|opt|killPwd(4)|RFU(1)`). Formati byte già noti
  (PIANO §4.3 + memory). Prima scrivere la password con `0x24` su reserved bank.
- **Step 3** — Async/Express inventory `0xAA48` (start) / `0xAA49` (stop): usa il
  **framing ESTESO** (`0xFF|len|0xAA|"Moduletech"|0xAA|0x48|data|SubCrc|0xBB|CRC`)
  che `protocol.py` NON costruisce ancora → serve un builder/parser dedicato.
  Alternativa: **Active-Upload** (push TCP/HTTP/MQTT) o HTTP API.
- **Transport alternativo** — `HttpTransport` (HTTP+JSON `/moduleapi/...`, porta 80
  o server locale `127.0.0.1:20085`), senza CRC/framing; evita la porta binaria.
- **GPIO SLD1090** — GPI `0x66` / GPO `0x96` (per trigger/attuatori, Step 3).

## Stato
Tutti i file compilano; **9/9 test** passano. Progetto non è un repo git → nessun
commit. Estrazioni testo dei PDF/DOC in `scratchpad/` (temporanee).

## Riferimenti
- Protocollo autorevole: `ULTIMI MANUALI/EX10 Module Communication Prorocol-2024-12.pdf`
- DEMO ufficiale: `ULTIMI MANUALI/Bilingual Demo_260203/` (`ReaderManager.exe`)
- Esempi byte-level: `ULTIMI MANUALI/Command Develop Kit/.../Basic Steps of Command Development`
