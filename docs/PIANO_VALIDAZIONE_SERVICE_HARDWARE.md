# Piano di validazione hardware del service RFID

Stato: pronto per le sessioni con SIM7200/SLD1090 e tre antenne SLP1027.
Riferimento contrattuale: `docs/CONTRATTO_SERVICE.md`, API service 1.0.

## Scopo

Dimostrare che il sottosistema funziona come black box anche con hardware reale.
L'accettazione non deve dipendere da chiamate dirette a `SIM7200Reader` o da un
risultato visivo della GUI: richieste, risposte ed eventi devono attraversare il
processo JSON-RPC avviato con `python run.py service`.
Su Windows il collaudo iniziale si esegue con `testa_service_hardware.bat`,
che usa lo stesso processo e salva tutte le risposte senza scrivere sui tag.

## Prerequisiti

- regione EU `0x08`; non usare FCC/full-band in Italia;
- lettore alimentato e tre antenne collegate: 1 base sinistra, 2 base destra,
  3 parete verticale;
- un tag di prova EPC noto e, per le scritture, un tag sacrificabile;
- configurazione seriale e configurazione TCP verificate separatamente;
- directory `logs/` scrivibile e orologio del PC corretto.

## Regole di esecuzione

1. Avviare un solo proprietario hardware alla volta. GUI standalone, Step 1 e
   processo service non devono aprire contemporaneamente la stessa COM/socket.
2. Salvare richieste e risposte JSONL, log `service_*.log` e report health per
   ogni sessione.
3. Non eseguire write senza inventory fresco con un solo EPC coincidente con
   `expected_epc`.
4. Ripetere i casi essenziali prima via seriale e poi via TCP.
5. Annotare modello/seriale hardware, firmware, cablaggio, potenze e posizione
   fisica del tag per rendere la prova riproducibile.

## Sequenza minima per ogni trasporto

| Ordine | Chiamata RPC | Evidenza attesa |
|---:|---|---|
| 1 | `rfid.describe` | API 1.0, elenco metodi, nessun accesso hardware |
| 2 | `rfid.snapshot` prima dello start | `state=stopped`, `ready=false` |
| 3 | `rfid.start` | boot riuscito, firmware/hardware version, trasporto corretto |
| 4 | secondo `rfid.start` | successo idempotente, `already_started=true` |
| 5 | `rfid.configure` | regione 8 e potenze applicate in cdBm |
| 6 | `rfid.health` | `ok=true`, antenne 1/2/3 rilevate o anomalia motivata |
| 7 | inventory antenna 1 | tag e `antenna_id=1` quando coperto |
| 8 | inventory antenna 2 | tag e `antenna_id=2` quando coperto |
| 9 | inventory antenna 3 | tag e `antenna_id=3` quando coperto |
| 10 | inventory 1/2/3 | batch JSON-safe, EPC unici e RSSI coerenti |
| 11 | `rfid.read` USER | esito distinto per ogni antenna, dati hex |
| 12 | `rfid.events` | sequenze crescenti, cursore coerente, `history_truncated=false` in consumo regolare |
| 13 | `rfid.stop` | trasporto chiuso, stato `stopped` |
| 14 | secondo `rfid.stop` | successo idempotente, `already_stopped=true` |

## Prove negative obbligatorie

| Caso | Azione | Risultato accettabile |
|---|---|---|
| API errata | inviare `api_version` diversa da 1.0 | errore RPC `-32001`, nessun I/O hardware |
| Metodo ignoto | inviare `rfid.unknown` | errore `-32601` |
| Parametro ignoto | aggiungere un campo non previsto | errore `-32602` |
| Operazione da fermo | inventory prima di start | `ServiceResponse.ok=false`, stato `stopped` |
| Nessun tag | inventory a volume vuoto | successo con lista tag vuota, non guasto |
| Più tag | presentare almeno due EPC e richiedere write | write rifiutata |
| EPC diverso | inventory tag A, write con `expected_epc` B | write rifiutata |
| EPC nuovo uguale al vecchio | `write_epc` con valori uguali | parametri rifiutati, nessun I/O |
| EPC non valido | lunghezza dispari, vuota o oltre 62 byte | parametri rifiutati |
| Disconnessione | scollegare USB/rete durante inventory | errore classificato, processo vivo o arresto controllato |
| Frame disturbato | se riproducibile, introdurre rumore seriale | contatori frame/resync aggiornati |
| EOF processo | chiudere stdin mentre il reader è ready | `finally` chiude il trasporto e libera COM/socket |

## Scrittura e verifica

Eseguire soltanto sul tag sacrificabile:

1. inventory immediatamente precedente con un unico EPC;
2. `rfid.write` sulla banca USER con `expected_epc` esatto;
3. controllare almeno un esito antenna `ok=true`;
4. `rfid.verify` sugli stessi word/address;
5. richiedere `rfid.health` e `rfid.events` dopo la prova;
6. conservare dati precedenti e nuovi nel report, senza password reali.

Per provare anche il cambio EPC, sempre sul tag sacrificabile:

1. chiamare `rfid.generate_epc` con `byte_length=12`, oppure predisporre
   manualmente un EPC valido e diverso da quello corrente;
2. ripetere un inventory immediatamente prima del comando e controllare che sia
   presente un solo EPC;
3. chiamare `rfid.write_epc` con `new_epc`, `expected_epc` e le antenne di prova;
4. verificare `verification_required=true` e l'evento `tag.epc.changed`;
5. eseguire un nuovo inventory e confermare che compaia il nuovo EPC;
6. verificare che un secondo tentativo senza inventory fresco venga rifiutato.

Il candidato casuale a 96 bit non è una garanzia di unicità globale: durante il
collaudo annotare gli EPC usati; nel futuro framework la persistenza dovrà
escludere duplicati prima dell'assegnazione.

## Stabilità e prestazioni

- inventory continuo per almeno 30 minuti per trasporto;
- nessuna crescita non limitata della memoria/event history;
- registrare cicli, tag/s, timeout, errori trasporto/frame/status e resync;
- verificare stop entro un tempo concordato anche dopo timeout;
- ripetere almeno tre cicli completi start → inventory → stop;
- verificare che la sequenza eventi resti monotona durante tutta la sessione.
- forzare in test una cronologia corta e verificare `history_truncated=true`,
  quindi riallineare il consumer con snapshot e inventory.

Le soglie definitive di latenza, retry e disponibilità saranno fissate dopo la
prima raccolta dati reale; non vanno inventate prima della misura.

## GUI di configurazione e diagnostica

Per il collaudo manuale avviare `avvia_test_grafico_rfid.bat` oppure
`python run.py service-gui`. Verificare aggiornamento/selezione della porta COM,
inserimento manuale, collegamento TCP/IP, inventory, lettura/scrittura USER e
cambio EPC manuale/AUTO. Il pulsante AUTO deve soltanto compilare il campo; la
scrittura deve richiedere **Scrivi EPC**, conferma e verifica automatica.
La modalità same-process è implementata: il framework passa la propria istanza a
`launch_gui(..., service=service)`. Il binding condiviso non ferma il reader alla
chiusura e blocca i campi seriale/TCP mentre il backend è attivo.

Validare sull'hardware questa sequenza:

1. il framework avvia il service e completa un inventory;
2. apre la GUI con la stessa istanza e collegamento automatico;
3. la GUI esegue health, configurazione potenze e inventory;
4. si chiude la finestra;
5. `service.ready` resta vero e il framework completa un nuovo inventory;
6. log e contatori non mostrano una seconda apertura del trasporto.

Validare anche la modalità con isolamento di processo: il framework crea un solo
`RFIDProcessClient`, avvia il service, passa la medesima istanza a
`launch_gui(..., service=client)` e verifica che la chiusura della GUI non fermi
né duplichi il subprocess. Se GUI e framework dovranno invece essere essi stessi
processi distinti e client concorrenti, resterà necessario un adapter IPC/HTTP
multi-client sopra `RFIDRPCDispatcher`. In ogni modalità deve esistere un solo
owner del trasporto; la GUI non deve aprire una seconda connessione al reader.

## Criteri di accettazione

La validazione è superata solo se:

- tutti i casi minimi passano su seriale e TCP, oppure l'eventuale limite TCP è
  documentato con evidenza del dispositivo specifico;
- boot, stop ed EOF non lasciano porta COM/socket occupati;
- risposte ed eventi sono sempre JSON serializzabili e conformi ad API 1.0;
- errori e assenza tag sono distinguibili nei report;
- le protezioni write resistono ai casi multi-tag/EPC errato;
- il framework può controllare il lifecycle senza importare protocollo, reader o
  trasporti;
- log e stdout JSONL restano separati;
- la modalità GUI condivisa same-process è provata; se richiesto dal framework,
  anche l'adapter multi-processo è scelto e validato con singolo owner.

## Dati da riportare a fine sessione

- data/operatore e commit o snapshot del codice;
- trasporto, porta/IP e firmware/hardware version;
- regione, potenze per antenna e configurazione usata;
- EPC di prova anonimizzato se necessario;
- esito di ogni caso, tempi osservati e contatori diagnostici;
- percorsi dei log/report allegati;
- anomalie riproducibili, passi esatti e decisioni conseguenti.
