# Contratto service RFID — API 1.1

Stato: contratto applicativo, valido dalla versione di sviluppo 2026-08-15.
La 1.1 aggiunge `lock` in modo puramente additivo rispetto alla 1.0.
Pubblico: framework principale, GUI di configurazione/collaudo e futuri adapter HTTP/IPC.

## Obiettivo architetturale

Il sottosistema RFID deve comportarsi come una **black box autonoma**. Il framework
principale non deve conoscere frame Silion, CRC, porte seriali, socket TCP o comandi
del reader. Tutti questi dettagli restano sotto `RFIDService`.

```text
Framework principale / GUI di settaggio
                 │
                 ▼
       RFIDService (API 1.0)
       DTO + lifecycle + eventi
                 │
                 ▼
 SIM7200Reader → protocollo → trasporto → hardware
```

La GUI Tkinter è un client del servizio: serve per configurazione, calibrazione,
diagnostica e prove. Non è il motore del sistema e non costituisce il contratto di
integrazione.

## Punto di ingresso

Il confine pubblico è `rfid_silion.service.RFIDService`. Il costruttore riceve la
configurazione già caricata come mapping; la scelta seriale/TCP resta incapsulata
nel factory del reader.

```python
from rfid_silion import InventoryRequest, RFIDService

service = RFIDService(config)
start = service.start()
if start.ok:
    batch = service.inventory(InventoryRequest(antennas=(1, 2, 3)))
service.stop()
```

`RFIDService` resta sincrono e thread-safe. `RFIDRPCDispatcher` lo espone tramite
JSON-RPC 2.0 senza dipendenze dal canale I/O; `service_host` fornisce già un
processo locale multipiattaforma su JSON Lines (stdin/stdout). Un futuro endpoint
HTTP o socket locale potrà riusare lo stesso dispatcher senza spostare logica
hardware nel framework.

## Processo service JSON-RPC

Avvio con la configurazione standard:

```bash
python run.py service
```

Su Windows lo stesso host si avvia con `avvia_service_rfid.bat`. Per la
prova hardware automatica e in sola lettura usare `testa_service_hardware.bat`:
le risposte vengono salvate in `logs/service_hardware_smoke_<timestamp>.jsonl`.

Avvio diretto o con configurazione differente:

```bash
python -m rfid_silion.service_host --config src/app/config.yaml
```

Ogni riga di stdin è una richiesta JSON-RPC 2.0 e ogni risposta occupa una riga
di stdout. I log vanno su stderr e nel file `logs/service_<timestamp>.log`, quindi
non contaminano il canale dati. Esempio discovery, che non richiede una versione
API già nota:

```json
{"jsonrpc":"2.0","id":1,"method":"rfid.describe"}
```

Le altre richieste dichiarano la versione del contratto:

```json
{"jsonrpc":"2.0","id":2,"api_version":"1.0","method":"rfid.start","params":{}}
{"jsonrpc":"2.0","id":3,"api_version":"1.0","method":"rfid.inventory","params":{"antennas":[1,2,3],"timeout_ms":1000,"metadata_flags":7}}
```

I metodi remoti sono `rfid.describe`, `start`, `stop`, `replace_config`,
`configure`, `inventory`, `read`, `write`, `generate_epc`, `write_epc`, `lock`,
`verify`, `health`, `snapshot` ed `events`, tutti con prefisso `rfid.`. Sono
supportati batch e notification
JSON-RPC. Un errore operativo del lettore produce un normale `result` contenente
`ServiceResponse.ok=false`; gli errori JSON-RPC sono riservati a envelope,
versione, metodo o parametri non validi.

**Compatibilita' fra minor version.** Un client che dichiara `api_version: "1.0"`
resta servito da un service 1.1: la 1.x aggiunge soltanto, quindi il service
offre tutto quello che il client conosce. Il contrario no — chi dichiara una
minor version superiore a quella del service viene respinto con
`-32001`, perche' potrebbe usare metodi che qui non esistono.

Il canale stdio non apre porte di rete e eredita identità e permessi del processo
framework. Un eventuale adapter HTTP futuro dovrà aggiungere autenticazione,
autorizzazione e limiti di richiesta senza modificare il dispatcher o il driver.

## Lifecycle

Gli stati pubblici sono:

- `stopped`: nessun trasporto aperto;
- `starting`: apertura e boot firmware in corso;
- `ready`: operazioni RFID consentite;
- `stopping`: chiusura in corso;
- `error`: avvio o arresto non riuscito.

`start()` e `stop()` sono idempotenti. Il boot firmware obbligatorio viene eseguito
da `start()`. Ogni operazione richiesta fuori dallo stato `ready` produce una
risposta fallita serializzabile, non espone eccezioni del protocollo al chiamante.

## Operazioni pubbliche

| Metodo | Input | Output principale |
|---|---|---|
| `start()` | nessuno | firmware, trasporto descritto, stato |
| `stop()` | nessuno | arresto/idempotenza |
| `replace_config(config)` | mapping | config sostituita solo da fermo |
| `configure(settings)` | `ReaderSettings` | regione e potenze applicate |
| `inventory(request)` | `InventoryRequest` | tag, EPC unici, metadati per antenna |
| `read(request)` | `ReadRequest` | esito e dati per ogni antenna |
| `write(request)` | `WriteRequest` | esito per antenna; scrittura protetta |
| `generate_epc(request)` | `EpcGenerationRequest` | candidato EPC casuale, senza I/O hardware |
| `write_epc(request)` | `WriteEpcRequest` | cambio EPC protetto ed esito per antenna |
| `lock(request)` | `LockRequest` | blocco/sblocco banche (0x25); permanente solo su consenso |
| `verify(request, expected)` | `ReadRequest`, hex atteso | dati e `match` per antenna |
| `health()` | flag check antenne | report diagnostico completo |
| `snapshot()` | nessuno | stato leggero, contatori, ultimo evento |
| `events(EventRequest)` | ultima sequenza nota | `ServiceResponse` con eventi, cursore e stato retention |
| `recent_events(seq)` | ultima sequenza nota | scorciatoia compatibile: sola lista eventi |

Le banche memoria sono esposte tramite `MemoryBank` (`RESERVED`, `EPC`, `TID`,
`USER`), così i client non devono importare costanti del protocollo. Allo stesso
modo `lock` usa `LockTarget` (le quattro banche piu' `kill_password` e
`access_password`) e `LockMode` (`no_action`, `lock`, `unlock`, `permalock`,
`permaunlock`), senza esporre i bit di Mask e Action.

**Lock e irreversibilita'.** `lock` e' soggetto alla stessa guardia della
scrittura: richiede un `expected_epc` osservato da solo nell'ultimo inventory.
`permalock` e `permaunlock` non sono annullabili da nessuna password e su nessun
tag: il servizio li esegue solo se la richiesta porta `allow_permanent: true`.
Un lock riuscito emette l'evento `tag.locked`.

## Forma stabile delle risposte

Ogni metodo operativo restituisce `ServiceResponse`, convertibile con `to_dict()`:

```json
{
  "operation": "inventory",
  "ok": true,
  "state": "ready",
  "data": {},
  "error": null,
  "timestamp": "2026-07-22T12:00:00.000+02:00",
  "api_version": "1.0"
}
```

In errore, `error` contiene almeno `type` e `message`. Quando disponibili, gli
esiti parziali per antenna rimangono in `data.results`. Byte, dataclass, enum e
chiavi numeriche vengono normalizzati in valori JSON-safe; i byte sono stringhe
esadecimali maiuscole.

Compatibilità: campi nuovi potranno essere aggiunti in API 1.x. Rimozioni, cambi
di significato o campi obbligatori incompatibili richiederanno una nuova versione
major di `SERVICE_API_VERSION`.

## Eventi

Il servizio mantiene una cronologia limitata e sequenziale. `subscribe(listener)`
consente consumo in-process. Il polling affidabile usa `events(EventRequest)`,
disponibile sia su `RFIDService` sia su `RFIDProcessClient`;
`recent_events(after_sequence)` resta una scorciatoia per la sola lista.

Eventi iniziali:

- `state.changed` — transizione lifecycle;
- `operation.completed` — risposta conclusa con successo;
- `operation.failed` — risposta conclusa con errore;
- `inventory.tags` — batch di tag rilevati, anche vuoto;
- `tag.epc.changed` — comando di cambio EPC riuscito, ancora da verificare con inventory;
- `tag.locked` — lock riuscito; `data.permanent` elenca le banche rese definitive.

Ogni evento contiene `sequence`, `kind`, `state`, `data`, `timestamp` e
`api_version`. Il framework deve memorizzare l'ultima sequenza consumata ed essere
idempotente rispetto a eventuali riconsegne dell'adapter futuro.

La risposta `events` aggiunge `first_available_sequence`, `last_sequence`,
`next_after_sequence` e `history_truncated`. Se `history_truncated=true`,
il consumer ha perso almeno un evento: deve riallinearsi con `snapshot()` e,
quando serve lo stato dei tag, con un nuovo inventory. Il polling non produce a
sua volta eventi e quindi non fa avanzare la sequenza.

## Regola di sicurezza della scrittura

`WriteRequest.expected_epc` e `WriteEpcRequest.expected_epc` sono obbligatori.
Ogni scrittura viene accettata soltanto se l'ultimo inventory ha rilevato
**esattamente un EPC** e questo coincide con quello atteso. La GUI esegue anche
un inventory fresco immediatamente prima della scrittura. Il vincolo appartiene
al servizio e quindi vale per qualunque client futuro, non solo per la GUI.

`write_epc` usa l'operazione dedicata del reader (comando Silion `0x23`), che
aggiorna EPC e PC in modo coerente, e si arresta al primo tentativo riuscito
perché l'identità del tag è già cambiata. Dopo il successo il service invalida
l'EPC osservato: non è possibile ripetere la scrittura usando un inventory
ormai vecchio. La risposta imposta `verification_required=true`; il client deve
eseguire un nuovo inventory e verificare il nuovo EPC.

`generate_epc` produce soltanto un candidato casuale e non riutilizza valori già
generati nella stessa sessione service. Con il default di 12 byte (96 bit) il
rischio statistico di collisione esterna è molto basso, ma il service non
possiede il registro aziendale degli identificativi. La garanzia di unicità
applicativa dovrà quindi essere fornita dal futuro framework tramite persistenza
e controllo prima dell'assegnazione.

## Posizionamento e dati antenna

Il contratto inventory conserva `antenna_id`, RSSI e gli altri metadati realmente
forniti dal reader. Con l'hardware attuale una lettura identifica l'antenna e la
potenza ricevuta, non una coordinata interna X/Y sulla superficie dell'antenna.
Qualunque futura stima spaziale o vista 3D focalizzata sui tag dovrà essere un
modulo separato che consuma eventi inventory e produce stime con confidenza,
senza modificare protocollo, trasporto o lifecycle del servizio.

## Client Python del processo service

`RFIDProcessClient` è l'adapter pronto per il framework Python quando si vuole
tenere il driver in un processo separato. Avvia `service_host`, negozia API 1.0,
serializza le richieste, drena stdout/stderr senza deadlock e applica timeout.
Implementa strutturalmente `RFIDBackend`, quindi può essere usato dal framework
e iniettato nella GUI senza cambiare DTO o risposte.

```python
from pathlib import Path

from app.gui import launch_gui
from rfid_silion import RFIDProcessClient

config_path = Path("src/app/config.yaml")
with RFIDProcessClient(config_path) as backend:
    assert backend.start().ok
    launch_gui(config, config_path, service=backend)
    assert backend.ready  # la GUI non arresta il subprocess né il reader
```

Il client serializza una richiesta alla volta sul canale JSONL e il subprocess è
l'unico owner del trasporto. `close()` prova prima lo stop remoto, chiude stdin
e termina il processo in modo controllato se non risponde. Le ultime righe di log
stderr sono disponibili con `stderr_tail()`.

## GUI di collaudo tramite processo service

Per le prove hardware correnti si usa `python run.py service-gui` oppure, su
Windows, `avvia_test_grafico_rfid.bat`. Il launcher avvia un processo service
separato, collega la GUI tramite `RFIDProcessClient` e lascia alla finestra il
controllo del lifecycle per poter cambiare seriale/TCP e riconnettersi. Driver,
protocollo e trasporto restano comunque confinati nel processo service.
## Apertura GUI dal framework e proprietà hardware

Per un framework Python nello stesso processo è disponibile `app.gui.launch_gui`.
Passando l'istanza `RFIDService`, la GUI crea un binding **shared** per default:
può usare configurazione, inventory e diagnostica, ma chiusura e pulsante
"Scollega GUI" non fermano il backend del framework.

```python
from pathlib import Path

from app.gui import launch_gui
from rfid_silion import RFIDService

service = RFIDService(config)
assert service.start().ok
launch_gui(config, Path("src/app/config.yaml"), service=service)
assert service.ready  # la chiusura della finestra non chiude COM/socket
```

Con `service=None` la modalità è standalone e la GUI possiede il lifecycle. I
parametri espliciti sono:

- `owns_service=False`: backend condiviso, non viene mai fermato dal detach;
- `owns_service=True`: la GUI è proprietaria e chiude il backend;
- `auto_connect=True`: collegamento automatico; è il default con service iniettato;
- `auto_connect=False`: apre la finestra senza avviare/collegare il backend.

Se il service condiviso è già `ready`, i campi seriale/TCP sono bloccati e la
config di trasporto visualizzata non sostituisce quella attiva. Regione e potenze
possono invece essere applicate tramite il contratto service.

L'host JSONL su stdio ha un solo processo padre e non è un bus multi-client. Se
GUI e framework dovranno vivere in processi separati, sarà necessario un adapter
IPC/HTTP multi-client sopra `RFIDRPCDispatcher`; la GUI non dovrà mai aprire una
seconda connessione diretta al reader.
## Regole per le prossime modifiche

1. Nuove funzioni hardware entrano prima nel driver, poi vengono esposte con DTO
   e risposte del service; GUI e framework non chiamano direttamente il reader.
2. Gli input/output del confine restano serializzabili e versionati.
3. Errori tecnici sono tradotti nel contratto, mantenendo dettagli diagnostici.
4. Ogni nuova operazione ha test senza hardware con reader/trasporto finto.
5. Le prove hardware validano il comportamento, ma non introducono dipendenze
   dalla GUI nella logica di dominio.
6. Adapter di processo/rete e persistenza sono livelli esterni sostituibili.

## Stato corrente e passi ancora aperti

Sono implementati il confine headless, lifecycle, health, eventi, configurazione,
inventory, read/write USER protetta, generazione e scrittura EPC protetta, verify,
binding owned/shared, dispatcher JSON-RPC,
host JSONL locale e client Python di processo; la GUI usa lo stesso confine.
Restano da validare con hardware reale seriale e TCP, scegliere nel framework la
policy di supervisione del processo e l'eventuale evoluzione da stdio a IPC/HTTP,
aggiungere recovery/retry calibrati sul campo e stabilizzare gli eventuali
contratti di localizzazione.