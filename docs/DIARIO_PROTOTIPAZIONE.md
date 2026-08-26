# Il diario di prototipazione

Finché il lettore non è collegato in modo stabile — e finché il flusso di lavoro
cambia ancora — la domanda ricorrente non è «funziona?» ma **«cosa è successo
davvero l'ultima volta?»**.

Il diario risponde a quella. Registra, in ordine di tempo e in un solo file:

* i **tasti premuti** e i **dati digitati** nell'interfaccia;
* ogni **operazione** con la sua richiesta, la sua risposta e quanto è durata;
* ogni **risposta della radio**: EPC, RSSI, antenna, conteggio di letture per
  tag, contenuto letto e scritto nei banchi di memoria;
* ogni **evento** trasmesso al browser.

E poi permette di **rimettere in piedi quella scena senza il lettore**, che è la
parte che serve adesso.

---

## Dove sta

```
logs/diario/
  diario_20260826_160147_8a9a7ce8.jsonl   ← una sessione
  ultimo.txt                              ← il nome di quella corrente
  sale.txt                                ← per lo pseudonimo (vedi sotto)
```

Un record per riga, JSON:

```json
{"t": "2026-08-26T16:01:52.480+02:00", "seq": 42, "sessione": "8a9a7ce8",
 "canale": "radio", "nome": "inventory", "durata_ms": 41.2,
 "dati": {"ok": true, "dati": {"tags": [{"epc": "0100…", "rssi": -45,
          "antenna_id": 2, "read_count": 1}], "unique_epcs": ["0100…"]}}}
```

I cinque **canali** dicono da dove arriva il record:

| canale | cosa | da dove |
|---|---|---|
| `ui` | clic, campi compilati, cambio schermata, stato della scena | il browser, via `POST /api/traccia` |
| `api` | operazione, richiesta, risposta, stato HTTP, durata | `WebUIServer._esegui` |
| `radio` | ogni chiamata del contratto `RFIDBackend` | `BackendTracciato` |
| `evento` | ciò che il server ha trasmesso al browser (SSE) | `EventBus.publish` |
| `nota` | annotazioni del programma (avvio, chiusura) | ovunque serva |

## Leggerlo

```
python run.py diario                      riassunto dell'ultima sessione
python run.py diario --timeline           la sequenza, riga per riga
python run.py diario --canale ui          solo il browser
python run.py diario --epc 0100010000…    solo i record che nominano quel tag
python run.py diario <file>               una sessione precisa
```

Il riassunto risponde alle domande che si fanno davvero dopo una prova:

```
Per canale
  radio       355
  api         190
  ui           98

Operazioni piu' usate
  sorveglia_scatola             88  media 45 ms
  scrivi                         3  media 22 ms

Tasti piu' premuti
  Scrivi il tag                                           3
  Apri la scatola e riempi                                1

Tag visti dalla radio (331 inventory)
  EPC                          letture   antenne   RSSI  memoria
  0100010000000601018BB9A7     265/329     1 2 3    -50  USER 60 byte  TID E280…

Errori (3)
  15:24:01.716  api registra -> 400: codice fiscale con carattere di controllo errato
  15:24:01.729  api conferma_conteggio -> 400: registrare prima un'accettazione
```

L'ultimo blocco è quello che vale il diario: **una cascata di tre errori che
vengono da un codice fiscale sbagliato**, e che a schermo erano tre messaggi
separati a mezzo secondo l'uno dall'altro.

**I tag li conta la ricostruzione, non un conteggio a parte.** Contare gli EPC
distinti di un log darebbe due tag dove ce n'era uno che ha cambiato EPC durante
la scrittura — ed è l'errore più facile da fare qui.

## Rimettere in piedi il banco

Da un diario si estraggono i tag realmente visti — EPC, TID, memoria USER,
antenne che li vedevano, RSSI, quanto erano difficili da leggere — e si rimonta
la stessa scena senza lettore:

```
python run.py diario --scenario banco.yaml
python run.py webui --simulato banco.yaml
```

Lo scenario è un YAML **fatto per essere modificato a mano**: si parte da quello
che è successo davvero e si cambia il caso che si vuole provare.

```yaml
tag:
  - epc: "0100010000000601018BB9A7"
    tid: "E28011902000000000000001"
    user_byte: 86
    rssi: -50
    antenne: []          # vuoto = risponde da tutte
    visibile_ogni: 1     # 3 = un ciclo su tre, cioè un tag difficile
    nel_campo: true
    # visto 265 volte su 329 cicli, memoria USER: 60 byte
```

Senza argomento, `--simulato` monta sei tag vergini con un numero di serie
nuovo a ogni avvio — perché un tag si scrive una volta sola, e con TID fissi la
seconda sessione di prova troverebbe tutto già assegnato.

Nell'interfaccia compare il riquadro **Banco di prova**: da lì si appoggiano e
si tolgono i campioni dall'antenna. È l'unico modo di provare quello che accade
*mentre* qualcosa cambia — il riempimento della scatola, la sorveglianza del
piatto — senza avere in mano trenta contenitori e un lettore acceso.

### Cosa viene ricostruito, e con che fedeltà

* **L'identità del tag** segue i cambi di EPC: un tag scritto durante la
  sessione resta uno, non due.
* **La difficoltà resta difficoltà.** Un tag visto in meno di una lettura su tre
  risponde a un ciclo su tre anche nel simulato: altrimenti la simulazione
  sarebbe più gentile della realtà proprio dove serve severità.
* **Un'antenna mai interrogata non diventa un muro.** Si limita la visibilità
  solo se c'è stata una vera occasione mancata: un tag scritto alla postazione,
  dove si interroga la sola antenna 3, non ha mai avuto modo di rispondere alle
  antenne di lettura, e dichiararlo sordo lo renderebbe introvabile in un
  sigillo che nella realtà lo troverebbe.

## Riservatezza

**Il diario contiene dati dei pazienti**, ed è voluto: senza quelli non si
ragiona su un caso vero. Vive in `logs/` accanto all'archivio, che contiene gli
stessi dati.

Quello che **non** contiene mai, da nessun canale: PIN, password di accesso ai
tag, token della postazione. Del gesto resta traccia, del segreto no — e la
regola vale anche se qualcuno chiama la rotta a mano, perché il diario non deve
dipendere dalla buona educazione del browser.

Con `diario.pseudonimo: true` nomi e codici fiscali diventano un codice stabile
(`PZ-413AFC4FBE`). Stabile davvero: il sale sta in un file accanto al diario
invece di cambiare a ogni avvio, quindi due sessioni restano confrontabili. Il
sale serve perché un codice fiscale ha poca entropia e un hash senza sale si
rovescia con un elenco di nomi. La descrizione del campione resta in chiaro
anche in quella modalità: è un dato sul reperto, non sull'identità, e senza non
si capisce più niente del caso.

## Configurazione

```yaml
diario:
  attivo: true          # spegne tutto
  dir: logs/diario
  pseudonimo: false     # true quando il sistema esce dal banco
  max_mb: 50            # oltre, apre un file nuovo
  interfaccia: true     # false spegne solo il browser, la radio resta tracciata
```

## Cosa non fa

* **Non ferma mai il lavoro.** Se la scrittura fallisce — disco pieno,
  chiavetta staccata — il diario si spegne dopo il primo errore e basta.
  Insistere significherebbe una traccia di stack per ogni tag letto, e sarebbe
  il diario a rendere illeggibile il log invece del contrario.
* **Non registra le operazioni di stato** (`stato`, `traccia`): comparirebbero
  a decine senza dire niente.
* **Non conserva i blob interi.** Una distinta cifrata in base64 viene troncata
  con il conto dei caratteri: `…(in tutto 200000 caratteri)`.
