# PROGETTO-RFID — Pilotaggio lettore SIM7200 (Silion) con 3 antenne

Applicazione per pilotare in lettura/scrittura un lettore RFID UHF **SIM7200** (modulo
Impinj E710 su baseboard **SLD1090**) con **3 antenne SLP1027** (8 dBi, polarizzazione
circolare, HPBW 68°) disposte a formare un parallelepipedo di lettura
(2 in piano + 1 verticale a 90°).

La documentazione originale del lettore è in `MANUALI/` e `ULTIMI MANUALI/` (protocollo,
SDK, esempi; non versionate). Il driver qui implementato **non usa le DLL ModuleAPI**
(Windows-only) ma parla direttamente il protocollo binario Silion, quindi è
multipiattaforma e supporta sia **seriale (USB/RS232)** sia **TCP/IP** (la SLD1090
espone entrambi).

## Documentazione

| Documento | Contenuto |
|---|---|
| [`docs/MANUALE_UTENTE.md`](docs/MANUALE_UTENTE.md) | manuale operativo (bozza) |
| [`docs/DOCUMENTAZIONE_TECNICA.md`](docs/DOCUMENTAZIONE_TECNICA.md) | architettura, protocollo, diagnostica (bozza) |
| [`docs/CONTRATTO_SERVICE.md`](docs/CONTRATTO_SERVICE.md) | API black box, lifecycle, DTO ed eventi |
| [`docs/SCHEMA_DATI_TAG.md`](docs/SCHEMA_DATI_TAG.md) | **cosa viene scritto nel tag**: EPC pseudonimo, payload cifrato, chiavi |
| [`docs/AFFIDABILITA_LETTURA.md`](docs/AFFIDABILITA_LETTURA.md) | **come si legge tutto e solo quello che c'è**: sigillo, campagna di misura, leve radio |
| [`docs/PIANO_VALIDAZIONE_SERVICE_HARDWARE.md`](docs/PIANO_VALIDAZIONE_SERVICE_HARDWARE.md) | collaudo service su seriale/TCP |
| [`docs/REPORT_REVISIONE.md`](docs/REPORT_REVISIONE.md) | analisi e revisione codice 2026-07 |
| [`PIANO_PROGETTO.md`](PIANO_PROGETTO.md) | pianificazione completa (hardware, step 1→4, rischi) |
| [`CHANGELOG.md`](CHANGELOG.md) | storico modifiche |
| [`docs/ACCETTAZIONE_GIORNALIERA.md`](docs/ACCETTAZIONE_GIORNALIERA.md) | accettazione, spedizioni e ricezione |
| [`docs/QR_WEBCAM.md`](docs/QR_WEBCAM.md) | acquisizione QR dalla webcam |
| [`docs/SAM2_LOCALE.md`](docs/SAM2_LOCALE.md) | prova conteggio e calibrazione webcam |
| [`docs/SAM2_OPENVINO.md`](docs/SAM2_OPENVINO.md) | SAM su GPU Intel |
| [`docs/OPENROUTER_VISION.md`](docs/OPENROUTER_VISION.md) | confronto Qwen/SAM sullo stesso scatto |
| [`docs/RICONOSCIMENTO_SIGILLO.md`](docs/RICONOSCIMENTO_SIGILLO.md) | riconoscimento nel Sigillo e Impostazioni Controllo Visivo |
| [`NOTE_SESSIONE_2026-10-02.md`](NOTE_SESSIONE_2026-10-02.md) | chiusura del 2 ottobre, verifiche e ripresa del lavoro |

## Collaborazione

Il repository contiene codice, configurazione e documentazione. Foto, log,
database, chiavi, ambienti Python e pesi dei modelli sono esclusi da Git.
Ogni postazione installa le proprie dipendenze e configura credenziali e
calibrazione webcam localmente. Per proporre modifiche creare un ramo dedicato,
eseguire `python run.py tests` e aprire una pull request; i test completi
sono eseguiti anche dalla CI su Windows e Linux.

## Requisiti e installazione

Python ≥ 3.10, poi:
```bash
pip install -r requirements.txt     # pyserial, pyyaml, matplotlib, numpy, cryptography
```
Il launcher non modifica automaticamente l'ambiente: in caso di dipendenze
mancanti mostra il comando da eseguire. L'installazione esplicita può essere
richiesta anche con `python run.py --install-deps`.

## Avvio

**Doppio clic su `AVVIA.bat`** (Windows) o `./avvia.sh` (Linux/macOS): apre un menu
con tutto, senza bisogno di conoscere Python. La voce 6 è quella di tutti i giorni.

```bash
python run.py            # menu interattivo
python run.py webui      # interfaccia operativa nel browser  <-- uso quotidiano
python run.py health     # health check: diagnosi rapida del lettore
python run.py service    # processo JSON-RPC 2.0 su stdin/stdout
python run.py tests      # autotest senza hardware (protocollo, trasporti, reader, service, lims, webui)

python run.py tag-profile # misura TID e USER memory di un tag: da fare per primo
python run.py campaign    # campagna di misura: tara potenza e parametri radio

# Strumenti da banco (Tkinter)
python run.py gui         # GUI di controllo standalone
python run.py service-gui # GUI di collaudo tramite service
python run.py step1       # CLI Step 1: test read/write nel parallelepipedo
python run.py lims        # accettazione, sigillo e ricezione, versione Tkinter

# Windows: host service puro
avvia_service_rfid.bat
# Windows: GUI di collaudo tramite processo service
avvia_test_grafico_rfid.bat
# Windows: smoke test hardware in sola lettura, con report JSONL
testa_service_hardware.bat
```

## Tracciabilità dei campioni

Il pacchetto `src/lims/` porta i dati di paziente, reperto e contenitore **dentro
il tag**, così che un campione arrivi al laboratorio successivo autosufficiente,
senza rete condivisa né database spedito a parte. Lo pseudonimo del contenitore
sta nell'EPC in chiaro; l'anagrafica sta nella USER memory, cifrata e legata al
TID del chip. Il formato completo è in
[`docs/SCHEMA_DATI_TAG.md`](docs/SCHEMA_DATI_TAG.md).

**Il primo passo è `python run.py tag-profile`.** Quanti dati entrano in un tag
dipende dal chip, non dal lettore, e nel progetto non è mai entrato un datasheet
dei tag: il comando legge il TID e *misura* la USER memory, poi dice se quei tag
sono utilizzabili. Il valore misurato va riportato in `lims.user_memory_bytes`.

Con 64 byte di USER memory il payload utile è di 44 byte: bastano per codice
fiscale, nome, data, materiale, fissativo, sede e avvertenze, in una sola
scrittura.

`python run.py webui` copre i tre momenti del percorso: **accettazione**
(registrazione e scrittura guidata, un contenitore per volta), **sigillo e
spedizione** (certificazione del contenuto alla chiusura ed esportazione della
distinta cifrata), **ricezione** (importazione della distinta e riconciliazione).

A destinazione la scatola si legge **ancora chiusa**: il filtro Select isola un
tag per volta, così ogni payload viene attribuito al suo contenitore e le
avvertenze — rischio biologico in testa — si conoscono *prima* di sollevare il
coperchio.

## Interfaccia operativa

`python run.py webui` apre l'interfaccia nel browser. Ascolta solo su
`127.0.0.1` e richiede un token che cambia a ogni avvio: nessun altro programma
sulla macchina, e nessuno sulla rete del laboratorio, può usare il lettore
mentre è aperta.

> **Il token cambia a ogni avvio.** Un indirizzo salvato nei preferiti o riaperto
> dalla cronologia contiene quello vecchio e non funziona: va usato l'indirizzo
> stampato nella finestra del terminale, che è anche in `logs/webui_url.txt`
> finché l'interfaccia è aperta. Se capita, la pagina lo dice esplicitamente
> invece di limitarsi a non funzionare. Non ha niente a che vedere con il
> lettore: il token viene controllato prima di toccare l'hardware.
>
> Per una postazione dedicata si può fissare `webui.token` in `config.yaml`:
> l'indirizzo resta sempre lo stesso e si può tenere un collegamento sul desktop.
> **Una sola interfaccia per volta**: la porta è esclusiva, e un secondo avvio si
> ferma spiegando che ce n'è già una aperta.

Non scarica niente — nessun CDN, nessun carattere da rete, nessuna compilazione:
il laboratorio è isolato e deve restarlo. Le due GUI Tkinter restano come
strumenti da banco.

Sette schermate: **accettazione** con la scena animata della postazione di
scrittura, **sigillo** con il conteggio `trovati / attesi` in grande,
**ricezione** con la riconciliazione, **strumenti** (potenze, Gen2, profilazione
tag, campagna, e il grafico del return loss per frequenza con la banda ETSI
evidenziata — ogni scheda spiega cosa misura, come si esegue la prova e come si
legge il risultato), **archivio**, **registro** e **impostazioni**.
Date e ore in formato italiano.

Il controllo visivo facoltativo del **Sigillo** riconosce i contenitori con
SAM 2 locale, Qwen3.8 27B o cerchi classici e conserva la prova insieme alla
distinta. Videocamera, modello, area e prospettiva, marcatori e riferimenti
del coperchio si configurano in **Impostazioni → Impostazioni Controllo Visivo**.
Lo scatto usa le scelte salvate; l'identificazione dei contenitori resta RFID.
Guida: [Riconoscimento nel Sigillo](docs/RICONOSCIMENTO_SIGILLO.md).

Verifiche al 2 ottobre 2026: **754/754 test del runner**, **781/781 pytest**
e sei suite UI Node superati. Il collaudo fisico completo del nuovo Sigillo
è ancora da svolgere; dettagli nella [nota di chiusura](NOTE_SESSIONE_2026-10-02.md).

**Archivio** risponde alla domanda che arriva da fuori mesi dopo. Si cerca per
cognome, nome, codice fiscale o numero di accettazione, e per ogni accettazione
si legge **quanti pezzi, quanti spediti, in quale laboratorio, quando (data e
ora), chi ha supervisionato e con che esito il sigillo**. Da un EPC si risale al
paziente e a ogni operazione registrata.

**Impostazioni** raccoglie il collegamento al lettore e i parametri avanzati:

- **seriale (USB o RS232)** con l'elenco delle porte del sistema e il
  riconoscimento di quella probabile — su USB la baseboard si presenta come porta
  seriale, quindi USB e RS232 sono lo stesso collegamento e cambia solo il cavo;
- **rete (TCP/IP)** con indirizzo, porta e attesa;
- l'API **HTTP+JSON** dei firmware recenti è elencata ma non selezionabile: il
  driver non la implementa, ed è annotata perché il manuale la promette;
- **avanzate**: banda di lavoro (con l'avvertenza che in Italia l'unica ammessa è
  865–868 MHz), risparmio energetico, permanenza per antenna, duty cycle, filtro
  RSSI e modo di riporto, parametri di inventory e timeout del driver;
- **questo laboratorio**: nome, sigla, indirizzo, email e referente — compaiono in
  alto, sulle etichette e nella distinta;
- **operatori**: chi può lavorare alla postazione. In alto si sceglie da un menu
  invece di digitare, perché il registro deve poter dire chi ha scritto un tag
  anche fra due anni;
- **laboratori destinatari**: riempiono il menu del sigillo, e la loro email
  prepara il messaggio con cui si manda la distinta (l'allegato va messo a mano:
  una pagina web non può allegare un file, e l'interfaccia lo dichiara).

«Collega con questi parametri» ferma e riapre il lettore: **è anche la prova del
collegamento**, perché se il lettore risponde con la sua versione il cavo e i
parametri sono giusti. «Salva come predefinito» riscrive `config.yaml`.

La scena non è decorativa e non è mossa da un timer: ogni suo stato corrisponde a
un passo realmente riportato dal driver. Dopo una scrittura riuscita
l'interfaccia **non** avanza da sola: aspetta di vedere il tag lasciare il piatto.
Avanzare a tempo significherebbe dare per fatto un gesto dell'operatore.

## Affidabilità di lettura

L'obiettivo che governa il progetto è vedere *esattamente* ciò che c'è nella
scatola al momento della chiusura — senza perdere niente e senza contare quello
che sta fuori. Il sigillo verifica contro la distinta invece di scoprire, ripete
la lettura variando potenza, antenne, sessione e modalità RF, e **non dichiara
mai completo un insieme che non lo è**.

Prima di fidarsi delle impostazioni va fatta la taratura:
`python run.py campaign` misura due tassi insieme — quanto si legge dentro il
contenitore e quanto si legge dei tag di controllo posti **fuori** — e consiglia
la potenza più bassa che legge tutto senza leggere il tavolo accanto.

Ragionamento, leve radio e cosa resta da misurare sull'hardware in
[`docs/AFFIDABILITA_LETTURA.md`](docs/AFFIDABILITA_LETTURA.md).

## Configurazione

Tutto in `src/app/config.yaml`: trasporto (**una** tra le sezioni `serial:` /
`tcp:` — la selezione è per presenza di chiave e `serial` ha priorità), regione
(EU `0x08` per l'Italia), potenze per antenna in **centesimi di dBm**
(2000 = 20 dBm), parametri inventory/accesso tag, logging.

## Integrazione come service

`rfid_silion.RFIDService` è il confine headless destinato al futuro framework
principale. Incapsula driver, protocollo e trasporto ed espone lifecycle, health,
eventi e contratti input/output JSON-safe versionati (`SERVICE_API_VERSION =
"1.2"`). `RFIDRPCDispatcher` e `python run.py service` rendono lo stesso contratto
utilizzabile da un processo esterno via JSON-RPC 2.0 su JSON Lines, senza aprire
porte di rete. La GUI usa già questo livello e rimane uno strumento di
configurazione, calibrazione, diagnostica e collaudo.
Il polling `events(EventRequest)` segnala eventuali sequenze perse per
overflow della cronologia, permettendo al framework di riallinearsi esplicitamente.

```bash
python run.py service
# quindi inviare una richiesta JSON per riga su stdin
```

Specifica completa in
[`docs/CONTRATTO_SERVICE.md`](docs/CONTRATTO_SERVICE.md).

Per un framework Python same-process, `app.gui.launch_gui(..., service=service)`
collega la finestra alla stessa istanza con ownership condivisa: chiudere la GUI
non chiude il reader e non crea una seconda connessione hardware.

Se si desidera isolare driver e hardware in un processo dedicato,
`RFIDProcessClient` avvia e controlla l'host JSONL implementando lo stesso
`RFIDBackend`. La medesima istanza client può essere passata alla GUI: il
subprocess resta l'unico proprietario di COM/socket.

## GUI di controllo

```bash
python run.py service-gui [--debug]
# oppure, su Windows, doppio clic su avvia_test_grafico_rfid.bat
```
Questa modalità avvia il service in un processo dedicato e collega la GUI allo
stesso contratto usato dal futuro framework. Consente di aggiornare o inserire
manualmente la porta COM, scegliere TCP/IP, configurare le potenze, eseguire
inventory singolo/continuo/a durata, leggere e scrivere USER, cambiare l'EPC e
visualizzare RSSI e volume 3D. Il pulsante **AUTO 96 bit** genera soltanto un
candidato EPC casuale; la scrittura avviene esclusivamente premendo
**Scrivi EPC**, dopo inventory con un solo tag e conferma esplicita. Le sessioni
lunghe usano un accumulatore limitato per evitare crescita indefinita della
memoria.

## Step 1 — test read/write (CLI)

1. Collegare SIM7200+SLD1090 via USB (dispositivo "HDSC") o Ethernet.
2. Collegare le 3 antenne SLP1027 alle porte 1, 2, 3.
3. Impostare `src/app/config.yaml` (porta COM o IP).
4. Posizionare un tag UHF Gen2 nel parallelepipedo.
5. `python src/app/step1_test_rw.py --config src/app/config.yaml`.

Il test è **in sola lettura per impostazione predefinita**. Usare `--write`
per abilitare la scrittura USER e `--write-epc` per modificare l'EPC; entrambe
sono bloccate se l'inventory non rileva esattamente un EPC.

Report in `logs/step1_<timestamp>.json` (include i contatori diagnostici);
exit code 1 in caso di errore fatale.

## Diagnostica e log

- Ogni sessione scrive un log in `logs/` (`gui_*.log`, `step1_*.log`, `health_*.log`, `service_*.log`).
- **Checkpoint strutturati** nei punti chiave, greppabili:
  `findstr CHECKPOINT logs\*.log` (Windows) / `grep CHECKPOINT logs/*.log`.
- **Contatori runtime** (`reader.diag`): comandi, timeout, errori trasporto/frame/status,
  byte spuri scartati — inclusi in tutti i report JSON.
- **Health check**: `python run.py health` → sintesi `[OK]/[FAIL]/[WARN]` +
  `logs/health_<ts>.json`; exit code 0/1/2 (usabile in automazione).
- **Modalità debug**: flag `--debug` o variabile `RFID_DEBUG=1` → log DEBUG con
  dump esadecimale di ogni frame TX/RX.

## Test (senza hardware)

```bash
python run.py tests                  # 411 test, tutti senza hardware
python -m pytest                     # suite + copertura minima 65%
python src/tests/test_protocol.py    # CRC/frame/parser tag
python src/tests/test_reader.py      # reader con trasporto finto
python src/tests/test_transports.py  # seriale/TCP con socket e porte finte
python src/tests/test_service.py     # API headless con reader finto
python src/tests/test_rpc.py         # JSON-RPC/JSONL senza hardware
python src/tests/test_client.py      # client subprocess e lifecycle senza hardware
python src/tests/test_webui.py       # interfaccia web su HTTP vero, backend simulato
```

## Troubleshooting rapido

| Sintomo | Prima cosa da provare |
|---|---|
| `0x0101 Unavailable command` | riconnettere (il boot firmware è automatico alla connessione) |
| `Transport timeout` | porta COM/IP corretti? lettore alimentato? `python run.py health` |
| `0x0400 No tag found` | non è un guasto: avvicinare il tag / alzare la potenza |
| `CRC mismatch` / `Resync` frequenti | controllare cavi e baud rate (115200) |
| Scrittura FAIL | tag protetto (`0x0424`), energia insufficiente (`0x042B`) — vedi manuale §14 |

Tabella completa e procedura di segnalazione: `docs/MANUALE_UTENTE.md` §11–16.

## Antenne e parallelepipedo

- Antenna 1 = base sx (orizzontale), Antenna 2 = base dx, Antenna 3 = parete
  verticale a 90°. Volume: 440 × 220 × 220 mm.
- Lo Step 1 inventaria ciclando le 3 antenne e, per read/write, prova ciascuna
  antenna registrando quale riesce (e l'RSSI), per mappare la copertura.

## Note regolamentari (Italia)

Regione EU = `0x08` (865–868 MHz). L'antenna SLP1027 è tarata 902–928 MHz → in
banda EU ha prestazioni ridotte (accettabile in lab). Rispettare i limiti EIRP
ETSI: partire da 20 dBm.
