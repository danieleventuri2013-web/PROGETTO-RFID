# Changelog

Formato ispirato a [Keep a Changelog](https://keepachangelog.com/it/1.1.0/).
Sezioni: Added / Changed / Fixed / Security / Diagnostics / Documentation.

## [Unreleased]

### Added — GPU Intel e confronto Qwen dalla webcam

- SAM 2 con OpenVINO su Intel GPU, ambiente separato, dispositivi effettivi
  dichiarati e conversione eager verificata numericamente. Sei foto:
  media 40,06 s; limite preesistente della ROI nella seconda foto documentato.
- Qwen3.8 27B tramite OpenRouter selezionabile accanto a SAM 2 nella prova
  webcam; ritaglio/rettifica conservati, centri numerati e rianalisi dello
  stesso scatto. Chiave soltanto nel server; errori API senza falsi zeri.
  Confronto su sei foto, due richieste ciascuna: 12/12 conteggi corretti,
  media 3,44 s. Guide `docs/SAM2_OPENVINO.md` e `docs/OPENROUTER_VISION.md`.

### Added — prova SAM 2 locale assistita

- Pagina `sam2-auto.html`: area della webcam salvata per dispositivo, ritaglio
  oppure quattro angoli con correzione prospettica, campi facoltativi in cm
  per larghezza/lunghezza/altezza camera. Uno scatto avvia bordo e conteggio
  automatici senza indicare i campioni; tempi e contorni sovrapposti.
  Rettifica del piano tramite omografia, senza fingere compensazioni della
  lente/parallasse in altezza. Tre prove del motore/API: 8,8,6, 87–127 s,
  media 101 s. Test geometrici, HTTP e UI automatici superati.
- Perimetro sperimentale della borsa/scatola esterna nelle foto con quattro
  riferimenti fissi sulle pareti (`tools/rileva_borsa_sam2.py`). Sei foto
  analizzate, contorno azzurro e lati prossimi al ritaglio arancioni; contorni
  dei campioni precedenti sovrapposti senza cambiare la ROI di conteggio.
  Report locale, maschere e coordinate JSON; 4/4 test dei criteri geometrici.
- Conteggio sperimentale da cartella di foto con SAM automatico, filtri
  geometrici e deduplicazione (`tools/conta_foto_sam2.py`). Corretta la griglia
  per il ridimensionamento quadrato del modello su foto rettangolari.
  Sei foto reali verificate: 8,8,8,8,6,5, senza prompt manuali sui contenitori;
  finestra di ricerca comune, tempi CPU 68–102 s. Report e PNG numerati locali.
- SAM 2.1 Tiny su CPU in ambiente Python separato: foto ferma Full HD,
  clic distinti per campione, contorni sovrapposti e correzioni con punti
  positivi/negativi. Il numero indicato resta distinto dalle regioni ottenute.
- Servizio ottico su localhost:8772 con token proprio, immagini in memoria
  e riuso della codifica della stessa foto; nessun RFID o database.
  Guida `docs/SAM2_LOCALE.md`. Segnalati contorni duplicati o troppo estesi.
- Test UI/HTTP superati e inferenza reale offline su immagine sintetica Full HD:
  8 regioni da 8 prompt, 18,7 secondi sulla CPU della postazione. Il risultato
  non attesta il riconoscimento della borsa reale né il conteggio automatico.

### Added — prova ottica autonoma dei campioni

- Risoluzione selezionabile Full HD/HD, predefinita 1920×1080, con risoluzione
  effettiva, fotogramma analizzato e tempo mostrati. Rimosso il ridimensionamento
  fisso a 720p; JPEG a qualità 96% con limite di dimensione. Test ottico Full HD
  con 8 cerchi e regressioni UI del cambio risoluzione superati.
- Campionamento lento regolabile (predefinito 1,5 secondi), cerchi confermati
  dopo tre fotogrammi e sovrapposizione sull'immagine effettivamente analizzata.
  Calibrazione dei raggi tramite indicazione del campione più piccolo/grande.
- `python src/app/vision_preview.py`: webcam, bordo della borsa e cerchi verdi
  numerati sovrapposti al video, con conteggio indipendente dal numero atteso.
  Funziona senza procedura di spedizione, database o lettore RFID.
- Area manuale facoltativa e parametri cerchi regolabili; guida
  `docs/PROVA_RICONOSCIMENTO.md`. Test HTTP con 8 cerchi sintetici e test UI
  della sovrapposizione superati. Il motore restituisce anche i raggi rilevati.

### Fixed — scelta webcam nella sigillatura

- Menu Videocamera visibile accanto ad Attiva webcam, con aggiornamento
  dell'elenco anche prima dell'avvio. Il cambio selezione riapre la webcam
  attiva sul dispositivo scelto e azzera gli angoli manuali della precedente.
- Gestiti dispositivo assente, accesso negato e consenso tardivo durante il
  cambio. Regressioni simulate in `test_visual_ui.cjs`.

### Fixed — aggiornamento archivio ricezioni

- L'elenco delle distinte aggiorna lo stato dopo la conferma e la rilettura,
  senza richiedere un cambio schermata o un aggiornamento manuale.
- Un errore nel rinnovo dell'elenco viene segnalato separatamente dall'esito
  dell'operazione già salvata; regressioni in `test_exchange_ui.cjs`.

### Added — webcam nella Ricezione

- Sostituito il campo per la pistola QR con anteprima del browser, scelta
  videocamera, bordo verde e acquisizione tramite pulsante o Spazio.
- Raccolta multiparte e tabella dei dati estratti; confronto degli EPC attesi
  con la lettura del volume. Il solo QR non abilita la conferma e il verbale:
  serve la distinta cifrata della spedizione. Un foglio diverso deseleziona
  la ricezione precedente.
- Decodifica locale dei JPEG in memoria, senza immagini nel diario;
  spegnimento della webcam al completamento o all'uscita dalla schermata.

### Added — prova QR con webcam

- `python run.py qr-webcam --distinta <file.rfidman>`: finestra con anteprima,
  riquadro sui QR leggibili e acquisizione manuale con Spazio/Invio.
- Raccolta di QR multiparte, firma verificata con le chiavi del circuito e
  confronto dei dati con la distinta cifrata. Nessun accesso al lettore RFID
  o scrittura nel database. Prova da immagini con `--immagine`.
- Dipendenze facoltative in `requirements-qr.txt`; guida in `docs/QR_WEBCAM.md`.

### Added — invii email con più colli e distinte attese

- In Sigillo, selezione di più colli per la stessa sede e anteprima del messaggio
  con una distinta cifrata per collo. Bozza `.eml`, pacchetto `.zip`, testo per
  webmail e invio SMTP con TLS; credenziale in variabile d'ambiente.
- Archivio degli invii con blocco del reinvio quando l'esito SMTP è incerto.
  Preparazione, accettazione SMTP, partenza fisica e ricezione restano stati distinti.
- In Ricezione, importazione di più distinte o ZIP, selezione manuale e
  riconoscimento tramite EPC del collo. Duplicati, colli estranei, letture ambigue
  e distinte destinate a un'altra sede sono gestiti senza confondere le ricezioni.
- SQLite schema 9 con migrazione transazionale; guida in
  `docs/CONFIGURAZIONE_OPERATIVA.md`.
- Rilettura di una ricezione con nuova conferma obbligatoria prima del verbale;
  una lettura fallita invalida la prova precedente per la conferma anche dopo riavvio.
  Le richieste da finestre con una distinta superata vengono rifiutate.
- Suite completa: 724/724; SMTP e lettore simulati. Controllo visivo della ripresa
  non eseguito per indisponibilità dell'integrazione browser.

### Changed — rifinitura UX dei due flussi quotidiani (`src/webui/static/`)

- **Ricezione — checklist degli attesi per paziente**: la lista «Campioni
  attesi» raggruppa i contenitori per paziente (nome, codice fiscale,
  accettazione) con etichetta, materiale ed EPC. Compare ora con **ogni**
  percorso di import (file `.rfidman`, QR sul foglio, ripristino dopo ricarica)
  — con l'import da file prima non appariva. Dopo la lettura ogni riga prende
  il suo esito: **✓ Arrivato** o **✗ Mancante**, e il titolo riassume il
  conteggio. I punti della scatola e gli elenchi «Non arrivati» / «non in
  distinta» restano come avviso operativo.
- **Accettazione — gli arretrati si vedono subito**: quando ci sono tag dei
  giorni precedenti ancora da scrivere, un avviso in testa alla giornata dice
  quanti pazienti e quanti tag restano, con un pulsante che apre il primo da
  completare. Il totale dei tag da scrivere (giornata più arretrati) compare
  come badge numerico sulla voce **Accettazione** della barra di navigazione.
  La sezione «Da completare» è ora distinta in ambra.
- **Il motivo di annullamento di un contenitore** si scrive in un dialogo
  modale coerente con gli altri (`chiediTesto()`), non più in un `prompt()`
  nativo del browser.

### Added — interfaccia operativa nel browser (`src/webui/`)

- **`python run.py webui`**: la postazione di lavoro, che sostituisce le due GUI
  Tkinter nell'uso quotidiano (restano come strumenti da banco). Sola libreria
  standard: nessuna dipendenza nuova, nessun CDN, nessun carattere scaricato,
  nessuna compilazione — il laboratorio è isolato e deve restarlo.
- **Due canali distinti, di proposito.** `POST /rpc` passa il corpo a
  `RFIDRPCDispatcher` così com'è, riusando validazione, negoziazione di versione e
  mappatura degli errori già collaudate; `POST /api/<operazione>` va al livello di
  flusso sopra `lims.*`; `GET /api/eventi` è SSE.
- **Una sola operazione radio alla volta**, con `409` a chi arriva mentre è
  occupato — non una coda: metterla in coda farebbe credere all'operatore di aver
  avviato qualcosa che parte dopo, su un tag che nel frattempo ha tolto dal piatto.
  Le operazioni che non toccano la radio restano disponibili durante un sigillo.
- **Solo `127.0.0.1` e solo con token** rigenerato a ogni avvio, come prescrive
  `docs/CONTRATTO_SERVICE.md` per qualunque adapter HTTP.
- **La scena del banco**: disegno SVG della postazione che cambia stato insieme al
  lavoro e *costituisce* l'istruzione. Nessun fotogramma parte da un timer: ogni
  stato corrisponde a un passo realmente riportato dal backend. Dopo una scrittura
  riuscita l'interfaccia non avanza a tempo — aspetta che la sorveglianza del
  piatto veda il tag andarsene.
- **La postazione sorveglia il piatto**: nessun pulsante per dire «l'ho appoggiato».
  La lettura serve comunque, perché la guardia di scrittura pretende un inventory
  con un tag solo.
- Schermate: accettazione, sigillo (conteggio `trovati / attesi` in evidenza, mai
  verde se i due numeri non coincidono), ricezione (tre errori distinti per la
  distinta, avvertenze sanitarie *prima* di aprire la scatola), strumenti
  (potenze, Gen2, profilazione tag, campagna guidata in tre passi, e il grafico
  del return loss per frequenza con la banda ETSI marcata e la soglia VSWR — è il
  disegno da portare al fornitore delle antenne), registro e parco tag.
- Anteprima e stampa dell'etichetta subito dopo la scrittura, con il contenitore
  ancora in mano. L'anteprima mostra i dati e lo ZPL sorgente, dichiarando di non
  essere un'anteprima di stampa: nel browser lo ZPL non si rasterizza, e un
  disegno inventato sarebbe peggio.
- `src/tests/test_webui.py`: 21 test che parlano al server via HTTP vero, come farà
  il browser — percorso completo dall'accettazione alla riconciliazione compreso.

### Added — anagrafiche, archivio pazienti, annullo

- **Impostazioni → Questo laboratorio**: nome, sigla, indirizzo, telefono, email e
  referente. La sigla compare in alto, il nome sulle etichette e nella distinta.
  `lims.lab_name` continua a funzionare come ripiego: le configurazioni installate
  non perdono il nome stampato.
- **Impostazioni → Operatori**: elenco di chi puo' lavorare alla postazione. In
  alto si sceglie da un menu invece di digitare, e un nome fuori elenco viene
  rifiutato — il registro deve poter dire chi ha scritto un tag anche fra due
  anni, e un nome scritto a mano ogni volta diverso lo rende inutilizzabile.
  Si mostra la **sigla**, o il **cognome** se la sigla manca.
- **Impostazioni → Laboratori destinatari**: riempiono il menu a tendina del
  sigillo (prima era un campo libero) e la loro email prepara il messaggio di
  spedizione. Due voci con la stessa identita' vengono rifiutate: nel menu
  sarebbero indistinguibili.
- **«Prepara l'email al destinatario»**: apre il programma di posta con indirizzo,
  oggetto e testo gia' scritti. **Dichiara che l'allegato va messo a mano**, perche'
  una pagina web non puo' allegare un file — dirlo dopo significherebbe una mail
  vuota gia' partita. Nel corpo non finisce nessun dato di paziente.
- **Testata divisa in tre zone** con filetti veri: laboratorio, operatore in
  servizio, stato del lettore. Senza operatore scelto il menu si colora d'ambra.
- **Archivio pazienti** (nuova schermata): ricerca unica su cognome, nome, codice
  fiscale o numero di accettazione, e per ogni accettazione **quanti pezzi, quanti
  scritti, quanti spediti, dove, quando (data e ora), chi ha supervisionato e con
  che esito il sigillo**. Il verdetto «tutto a buon fine» ha un significato preciso:
  ogni contenitore attivo scritto, partito, e sigillo completo. `traccia_contenitore`
  risale da un EPC al paziente e a ogni operazione registrata, per una verifica
  esterna.
- **Schema archivio v3**: `shipments` guadagna `operator`, `sealed_at`, `sent_at`,
  `sealing_ok`, `sealing_detail`. Prima quei dati stavano solo in `tag_events`,
  mescolati a tutto il resto: se un'autorita' chiede conto di un campione la
  risposta deve stare in una riga, non in una ricostruzione.
  La migrazione e' scritta come funzione e aggiunge **solo le colonne mancanti**:
  `ALTER TABLE ADD COLUMN` non e' ripetibile, e uno script interrotto a meta'
  avrebbe impedito per sempre di riaprire l'archivio.
- **«Annulla l'accettazione»** nella postazione di scrittura. Annulla i contenitori
  non ancora scritti e torna al modulo; **quelli gia' scritti restano**, perche' il
  tag e' scritto una volta sola e quei contenitori esistono ormai nel mondo fisico,
  con l'etichetta addosso. Il dialogo lo dice prima di procedere.
- **Date e ore in formato italiano** in tutta l'interfaccia (`16/08/2026, 19:13`).
  L'archivio continua a conservare ISO 8601, che e' giusto per un file.

### Fixed — due interfacce sulla stessa porta

**La causa vera dei «token non valido» inspiegabili.** `HTTPServer` imposta
`SO_REUSEADDR`, e **su Windows quel flag permette a un secondo processo di legarsi
a una porta gia' occupata** invece di fallire. Avviando l'interfaccia due volte
restavano due server vivi sulla 8770, ognuno con il suo token: il browser finiva
su uno dei due a caso e l'indirizzo stampato dall'altro veniva rifiutato. Sembrava
un problema di token, ed era un problema di porta.

- La porta ora e' **esclusiva** (`SO_EXCLUSIVEADDRUSE` su Windows,
  `allow_reuse_address = False` ovunque): la seconda istanza non parte.
- `run.py webui` prende la porta **prima** di annunciare l'indirizzo, e se e'
  occupata spiega cosa fare invece di stampare un indirizzo che non funzionera'.
- L'indirizzo attivo viene scritto in `logs/webui_url.txt` e rimosso alla chiusura:
  se la finestra del terminale viene chiusa per sbaglio, resta un posto dove leggerlo.
- **`webui.token` in `config.yaml`**: token fisso opzionale. Vuoto (predefinito) ne
  genera uno nuovo a ogni avvio; valorizzato rende l'indirizzo stabile, così su una
  postazione dedicata si puo' tenere un collegamento sul desktop.

### Fixed — un token scaduto adesso si spiega

- **Il token cambia a ogni avvio**, quindi un indirizzo salvato nei preferiti non
  funziona più. La pagina si caricava lo stesso (non è segreta: è il *comando* a
  essere protetto) e poi falliva una chiamata alla volta con un avviso criptico.
  Ora un `401` porta una schermata che copre tutto e dice cosa fare; senza token
  nell'indirizzo compare subito, senza nemmeno provare.
- Il flusso SSE si chiude invece di ritentare all'infinito contro un token rifiutato.
- **Metodi HTTP non previsti** (i browser mandano `OPTIONS` da soli, per estensioni
  o service worker) ricevevano la pagina `501` in inglese della libreria standard,
  che sembra un guasto del lettore. Ora è un `405` con la spiegazione.
- `run.py webui` stampa l'indirizzo con `flush=True` e dentro una cornice: senza,
  avviando da `.bat` o da un IDE lo stdout resta nel buffer e l'indirizzo compare
  solo alla chiusura del programma — cioè quando non serve più.

### Added — schermata Impostazioni

- **Collegamento al lettore**: seriale con elenco delle porte di sistema
  (`serial.tools.list_ports`) e riconoscimento di quella probabile, oppure TCP/IP.
  Su USB la baseboard si presenta come porta seriale: **USB e RS232 sono lo stesso
  trasporto** e l'interfaccia lo dice, invece di offrire una terza voce finta.
- L'API **HTTP+JSON** dei firmware recenti compare come scheda esplicitamente non
  disponibile: esiste nel manuale ma `HttpTransport` non c'è, e nasconderla farebbe
  cercare all'operatore una voce che il manuale promette.
- «Collega con questi parametri» è stop → `replace_config` → `start`, ed è **anche
  la prova del collegamento**: se il lettore risponde con la sua versione, cavo e
  parametri sono giusti.
- «Salva come predefinito» riscrive `config.yaml` con scrittura in due tempi
  (file temporaneo + `replace`): un'interruzione a metà lascia la configurazione
  vecchia, non nessuna configurazione. La sezione di trasporto inattiva viene
  parcheggiata come `serial_disabled`/`tcp_disabled`, perché il trasporto si sceglie
  per presenza della chiave e lasciarle entrambe ne sceglierebbe una a caso.
- **Avanzate**: banda di lavoro (comando `0x97`) con avvertenza esplicita che in
  Italia l'unica ammessa è 865–868 MHz e conferma obbligatoria per qualunque altra;
  risparmio energetico, permanenza per antenna, duty cycle, filtro RSSI e modo di
  riporto, parametri di inventory, timeout del driver e potenza massima.
- **`ReaderTuning.duty_cycle_full_ms` / `duty_cycle_period_ms`**: `set_duty_cycle`
  esisteva nel driver ma non era raggiungibile da nessuna interfaccia. I due valori
  si impostano insieme, e la coppia incompleta viene rifiutata invece che ignorata.

### Changed — la calibrazione spiega cosa fa

Ogni scheda di **Strumenti** dice ora cosa misura, come si esegue la prova e come
si legge il risultato: perché la potenza più bassa è quella giusta, cosa sono
sessione/target/Q/modalità RF in parole piane, perché la profilazione dei tag va
fatta per prima e con un tag solo, perché i tag di controllo *fuori* dal contenitore
non sono facoltativi, e come si legge il grafico del return loss.

### Added — il filtro Select arriva fino al contratto del servizio

- `ReadRequest.select_epc` → `read_try_all_antennas(select_epc=)`. Prima il filtro
  esisteva solo in `reader.read_tag_data` e non era raggiungibile da `lims`.
- **`TagIO.survey_field` ora legge il payload di ogni tag anche con la scatola
  piena**, isolandolo per EPC. Prima si arrendeva con più di un tag nel campo, e il
  laboratorio destinatario poteva contare i contenitori ma non sapere *cosa* fosse
  arrivato — che era metà del motivo per cui i dati viaggiano nel tag.
  Senza filtro il comando colpisce il primo tag che risponde: il payload finirebbe
  attribuito al contenitore sbagliato, e il conteggio tornerebbe lo stesso.

### Changed

- `LimsDatabase(single_thread=False)` per l'uso da più thread, che serve al server
  HTTP. Chi lo disattiva si prende l'onere di serializzare le sequenze:
  `webui.workflow` lo fa con un lock sulle operazioni che scrivono più righe.
- `TagIO.provision(on_step=…)`: i passi si possono seguire mentre accadono invece
  di ricostruirli dopo da `ProvisionResult.steps`.
- `geometry.antenna_positions_mm` in `config.yaml`: la scena disegna il banco vero
  (tre antenne a pavimento), non un montaggio ideale.
- `AVVIA.bat` / `avvia.sh`: l'interfaccia operativa diventa la voce principale.

### Added — affidabilità di lettura e flusso operativo completo

- **Leve radio nel driver**, tutte assenti prima e tutte verificate byte per byte
  contro gli esempi del manuale EX10 2024-12:
  - formato esteso «Moduletech» (`build_extended_packet`/`parse_extended_response`).
    Il SubCRC non è documentato come formula: ricavato dagli esempi (somma dei byte
    modulo 256) e riprodotto su tutti e cinque, che i test conservano come casi noti;
  - `0x9B` parametri Gen2: session, target con ribaltamento automatico A↔B, Q,
    e **RF MODE `0x71` (−93 dBm, 5 dB in più del default)**, con rilettura di
    conferma perché il manuale avverte che una modalità non supportata viene
    accettata e poi silenziosamente sostituita;
  - `0xAA4A` diagnostica antenna: return loss per frequenza e VSWR. È lo strumento
    che misura quanto le antenne siano disadattate in banda EU invece di dedurlo;
  - `0xAA58`/`0xAA59` inventory asincrono in modalità tag densi, con il ciclo di
    ascolto dei pacchetti auto-caricati;
  - **filtro Select** su `read_tag_data`/`write_tag_data`: si punta un EPC preciso
    anche con altri tag nel campo;
  - **embedded read** nell'inventory: la memoria di *tutti* i tag in un giro solo;
  - `0x95`, `0x98`, `0x9A`, `0xAA5B`: le impostazioni che, lasciate al valore di
    fabbrica, sabotano in silenzio la lettura ripetuta.
- **`lims.sealing`** — certificazione del contenuto di una scatola alla chiusura.
  Verifica a insieme chiuso contro la distinta, passate multiple che variano
  potenza, antenne, sessione e modalità RF per decorrelare i fallimenti, criterio
  di arresto esplicito e record di sigillo con l'evidenza per tag. La promessa che
  regge tutto: **non dichiarare mai completo un insieme che non lo è.**
- **`lims.campaign`** e `run.py campaign` — taratura sui dati con due obiettivi
  opposti: massimo dentro il contenitore, **zero fughe** dai tag di controllo
  posti fuori. La configurazione consigliata è la potenza più bassa che legge
  tutto senza leggere il tavolo accanto.
- **`lims.labels`** — etichette ZPL con Data Matrix dell'EPC; è l'unica parte
  leggibile senza lettore RFID. Interfaccia astratta di stampa, con invio TCP 9100
  e scrittura su file per l'anteprima.
- **`lims.manifest`** — distinta di spedizione cifrata (AES-256-GCM, nonce
  esplicito) e riconciliazione all'arrivo, dove un contenitore inatteso conta
  quanto uno mancante.
- **Tag sul coperchio**: schema EPC dedicato per l'identità della scatola. Il
  sigillo lo riconosce, non lo conta fra i contenitori e non lo scambia per un
  intruso; due coperchi nel campo invalidano il sigillo.
- **Scrittura unica, annullamento e sostituzione** (schema DB v2): un tag scritto
  non si riscrive salvo autorizzazione esplicita che lascia traccia; un contenitore
  rotto o un tag guasto si annullano e si sostituiscono **alla stessa posizione**
  «n di N»; un EPC non torna mai disponibile.
- **Variazione del numero di contenitori in corso d'opera**: si corregge
  liberamente ciò che non è ancora scritto, e le discrepanze sui già scritti
  vengono elencate invece che nascoste. La conferma si chiede alla **prima
  scrittura**, quando l'operatore ha i campioni davanti.
- Terza schermata in `run.py lims`: sigillo, spedizione, esportazione della
  distinta; e importazione con riconciliazione in ricezione.
- `docs/AFFIDABILITA_LETTURA.md`.

### Changed
- `SERVICE_API_VERSION` a **1.2** (`configure_gen2`, `tune_reader`,
  `antenna_diagnostics`), sempre in modo additivo.
- `TagReadAccumulator` conta i cicli e traccia `first_seen_cycle`: da lì il tasso
  di rilevamento per tag.
- `parse_tag_record` estratta da `parse_tag_buffer` per riusarla sui pacchetti
  asincroni invece di duplicare il parser più delicato del driver.
- `survey_field(expected_epcs=…)` confronta con la distinta: senza, un'accettazione
  di cui non arriva **nessun** contenitore restava invisibile.
- `config.yaml` allineato al montaggio reale del prototipo: tre antenne a
  pavimento, 1 e 2 in lettura con il contenitore sopra, 3 per la scrittura.

### Added — tracciabilità campioni (`src/lims/`)
- Nuovo pacchetto `lims`, client del solo contratto `RFIDService`: non importa
  mai `reader`, `protocol` o `transports`. Porta i dati di paziente, reperto e
  contenitore **dentro il tag**, così che un campione arrivi al laboratorio
  successivo autosufficiente, senza rete condivisa né database spedito a parte.
- `lims.codec`: schema binario versionato. EPC pseudonimo di 12 byte in chiaro
  (laboratorio, accettazione, *n* di *N*, coda casuale) e payload del campione.
  Codice fiscale di 16 caratteri impacchettato in 11 byte in base 36, con
  verifica del carattere di controllo. Codebook di materiali, fissativi e sedi.
- `lims.crypto`: sigillo AES-256-GCM del payload. Il nonce deriva da EPC e
  revisione invece di occupare spazio sul tag; i dati autenticati comprendono il
  **TID**, l'identificativo di fabbrica non riscrivibile, così che un tag clonato
  su un chip diverso non superi la verifica. Portachiavi con `key_id` per la
  rotazione, tenuto fuori dal database e fuori dai log.
- `lims.db`: archivio SQLite (pazienti, accettazioni, reperti, contenitori,
  spedizioni, traccia delle operazioni). Il vincolo `UNIQUE` su `containers.epc`
  è il registro di unicità che `generate_epc` non può garantire da solo.
- `lims.tagio`: orchestrazione. `provision()` segue l'ordine imposto dalle
  guardie del servizio (inventory → TID → cambio EPC → inventory → payload →
  verifica), spezzando la scrittura in blocchi da 64 byte quando serve.
  `survey_field()` legge un carico e segnala i contenitori mancanti **dal solo
  EPC**, senza bisogno della chiave.
- `lims.profiler` e `python run.py tag-profile`: nel progetto non è mai entrato
  un datasheet dei tag, e la capacità dipende dal chip. Il comando legge il TID
  (costruttore e modello secondo ISO/IEC 15963) e **misura** la USER memory per
  ricerca binaria, con verdetto sull'idoneità. Non richiede modifiche al driver.
- `app/lims_gui.py` e `python run.py lims`: schermata di **accettazione**
  (registrazione paziente/reperto e scrittura guidata, un contenitore per volta)
  e di **ricezione** (lettura del volume con evidenza dei contenitori mancanti e
  dei tag non riconosciuti).
- `docs/SCHEMA_DATI_TAG.md`: il contratto verso il laboratorio destinatario.

### Added — driver
- Comando **Lock Tag `0x25`** (`protocol.build_lock_bits`, `reader.lock_tag`,
  `service.lock`, `rfid.lock`), con la disposizione dei bit presa dalla Figura 6
  del manuale EX10 2024-12 §6.3. Il manuale vieta l'opzione `0x05` per questo
  comando: si usa `0x00`. Le operazioni permanenti richiedono
  `allow_permanent=true`, perché non sono annullabili da nessuna password.
- `TagIO.set_access_password()`: scrive la password nella banca RESERVED al
  posto del valore di fabbrica `00000000`. Il lock da solo non basta, perché si
  sblocca con la password.

### Changed
- `SERVICE_API_VERSION` passa a **1.1** (aggiunta puramente additiva di `lock`).
  Il controllo di versione in `rpc.py` non è più a uguaglianza esatta: un client
  che dichiara `"1.0"` resta servito, come il contratto promette per le 1.x.
  Prima il solo passaggio a 1.1 li avrebbe respinti tutti.
- Nuova dipendenza `cryptography`; sezione `lims:` in `config.yaml`.

### Added
- `rfid_silion.service`: confine black box API 1.0 con DTO JSON-safe,
  lifecycle idempotente, health, snapshot ed eventi sequenziali.
- Contratto `docs/CONTRATTO_SERVICE.md` per la futura integrazione nel framework
  principale tramite adapter di processo/HTTP/IPC.
- `RFIDRPCDispatcher`: adapter JSON-RPC 2.0 trasporto-agnostico con discovery,
  negoziazione API, batch, notification ed errori standard.
- `service_host.py` e `python run.py service`: processo locale JSON Lines su
  stdin/stdout, con log separati su stderr/file.
- `RFIDProcessClient`: adapter Python sincrono che gestisce il subprocess,
  negozia API 1.0 e implementa `RFIDBackend` per framework e GUI condivisa.
- `EventRequest` e `RFIDBackend.events()`: polling uniforme con cursore,
  limiti della cronologia e rilevamento esplicito degli eventi persi.
- `avvia_service_rfid.bat` e `testa_service_hardware.bat`: avvio service
  e smoke test hardware in sola lettura con report JSONL.
- `avvia_test_grafico_rfid.bat` e `python run.py service-gui`: banco prova
  grafico collegato a un processo service separato.
- `EpcGenerationRequest` / `WriteEpcRequest` e RPC `generate_epc` /
  `write_epc`: candidato EPC casuale e cambio EPC protetto con verifica richiesta.
- Test headless del service, del canale RPC e del client subprocess; suite portata
  a 56 test.
- Piano di collaudo hardware black-box per seriale/TCP, prove negative,
  sicurezza write, stabilità ed architettura GUI a singolo owner.

### Changed
- La GUI offre elenco porte COM aggiornabile con campo manuale e controlli EPC
  manuale/AUTO; AUTO compila soltanto il candidato e la scrittura resta separata,
  confermata e verificata.
- La GUI dipende esclusivamente da `RFIDService` per connessione, configurazione,
  inventory, read/write, verifica e diagnostica; non usa più direttamente
  protocollo, trasporti o `SIM7200Reader`.
- `RFIDBackend` e `RFIDServiceBinding` introducono backend iniettabile e
  ownership lifecycle owned/shared; la GUI aperta dal framework usa la stessa
  istanza e non chiude né duplica il trasporto.
- Le scritture del service richiedono un `expected_epc` e un ultimo inventory
  contenente esclusivamente quel tag; la GUI ripete un inventory di sicurezza
  immediatamente prima della scrittura.
- `run.py tests` include i test del contratto JSON-RPC e dell'host JSONL.
## [0.3.0] — 2026-07-22

### Added
- Packaging e toolchain in `pyproject.toml`: metadati installabili, dipendenze
  di sviluppo, Ruff, pytest e branch coverage minima 65%.
- CI GitHub Actions su Windows/Linux e Python 3.10/3.13.
- `TagReadAccumulator`: aggregazione incrementale con limite configurabile
  degli EPC per sessioni inventory lunghe.
- Test dedicati ai trasporti seriale/TCP; suite portata a 32 test.

### Changed
- GUI con executor I/O singolo e coda thread-safe verso Tkinter; inventory
  singolo, continuo o temporizzato, selezione antenne e stop esplicito.
- `run.py` non installa o aggiorna più pacchetti implicitamente; usare
  `--install-deps` per autorizzare l'installazione delle sole dipendenze mancanti.
- Step 1 è read-only per default; `--write` e `--write-epc` abilitano
  esplicitamente le operazioni distruttive.
- Regione selezionabile dalla GUI limitata a EU `0x08`.

### Fixed
- Disconnessione TCP distinta da un normale timeout; errori seriali/TCP
  normalizzati e conteggiati separatamente.
- Timeout boot/risposta applicati al trasporto e limite di potenza configurato
  applicato dal driver.
- Validazione rigorosa delle risposte antenna, inventory e read, e dei buffer
  tag troncati, disallineati o con byte residui.
- Health check con inventory opzionale: gli errori dell'inventory non vengono
  più classificati come errori di connessione.
- Chiusura GUI ordinata e nessun accesso ai widget Tk dai worker.
- Scritture GUI/Step 1 bloccate senza una sessione inventory con un solo EPC;
  conferma esplicita in GUI.

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
