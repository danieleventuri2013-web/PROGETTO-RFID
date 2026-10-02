# Verifica nuovi tag e recupero della scrittura — 1 ottobre 2026

- Prova richiesta dall'utente sul lettore reale COM5, antenna di scrittura 3,
  regione EU invariata. Prima dello spessore: inventory riuscito ma accessi
  EPC/TID intermittenti. Dopo lo spessore aggiunto dall'utente: 5 inventory,
  5 letture TID e 5 letture EPC riusciti su 5, senza scritture sul tag.
- EPC osservato: `E28011B0A503007B2852B3B2`; TID serializzato:
  `E28011B0200073B2429503D9`; PC `3000`. Famiglia Impinj M800,
  coerente con M830. Profilazione riuscita: USER assente.
- Profilo applicato tramite WebUI: capacità USER da 16 a 0 byte, modalità
  solo EPC mantenuta, lettura TID attiva e USER disattivata.
  Backup precedente in `logs/config_prima_tag_spessore_20261001_102516.yaml`.
- Il successivo tentativo dell'utente ha trovato il blocco «ripristinare il
  profilo originale»: l'impronta persistita includeva USER anche in solo EPC.
- Corretto `src/lims/tagio.py`: i nuovi tentativi solo EPC normalizzano la
  capacità a zero. Quelli storici sono riconosciuti ricostruendo l'hash per
  le capacità ammesse (byte pari 0–8192), mantenendo invariati payload,
  modalità e revisione. Il controllo dell'identità del tag resta attivo.
- Nessuna cancellazione o modifica manuale dei tentativi reali. Verificata
  su copia SQLite la compatibilità del tentativo del contenitore 1 con la
  correzione. EPC riservato conservato: `0100010000000101033B9A1B`.
- Aggiunte regressioni per tentativi nuovi e storici, scrittura non avvenuta
  e risposta persa dopo cambio EPC; verificati i blocchi per payload,
  revisione, modalità o tag diversi. `test_operativita.py`: 15/15.
- Suite completa eseguita: unico fallimento nel test che confrontava il
  numero di CRLF prima/dopo la sostituzione del profilo nel YAML. Il profilo
  WebUI completo è più lungo del vecchio registro CLI: corretto il test per
  verificare la convenzione CRLF, senza imporre lo stesso numero di righe.
  Rieseguito `test_config_misura.py`: 15/15, e operatività: 15/15.
  Log della prima esecuzione completa: `logs/test-nuovi-tag-suite.txt`.
- WebUI riavviata con il codice aggiornato; riconnessa a COM5 e ripristinato
  l'operatore DV. Accettazione 1 ripresa: 0 di 3 scritti, conteggio confermato.
  La nuova scheda usa l'indirizzo aggiornato in `logs/webui_url.txt`.
- La scrittura fisica dopo la correzione resta da verificare al prossimo
  comando dell'utente. Non considerare questa sessione una convalida del lotto.

Rapporti delle letture: `logs/verifica_tag_spessore_20261001*.json`.
Documentazione chip: https://support.impinj.com/hc/article_attachments/30450110027027

## Ripresa dopo il limite — email con più colli

Le note precedenti si fermavano alle 10:34. I file e il log
`logs/test-email-multicollo.txt` documentavano lavoro successivo sugli invii email
e sulle distinte attese, interrotto durante la suite. La richiesta di ripresa è
stata applicata a quel lavoro, preservando tutte le modifiche locali precedenti.

- Nuovi moduli già presenti alla ripresa: `src/lims/mail_dispatch.py`,
  `src/webui/exchange.py`, `src/webui/static/exchange.js`,
  `src/tests/test_exchange.py`; integrazioni in Workflow, server, HTML e SQLite 9.
- Email cumulative per colli della stessa destinazione, distinte cifrate immutabili,
  bozza EML, ZIP e SMTP TLS. Archivio degli invii e gestione degli esiti incerti.
- Ricezione con importazione multipla, archivio persistente, riconoscimento del
  collo e confronto del contenuto. La selezione manuale resta disponibile.
- Il fallimento dell'ultima suite riguardava la rilettura dopo conferma. Il blocco
  era già rimosso nel codice; completata la correzione imponendo una nuova conferma
  prima di esportare il nuovo confronto. Una lettura fallita impedisce di usare
  la precedente per una conferma, anche dopo riavvio.
- Aggiunta protezione HTTP per richieste con distinta cambiata in un'altra finestra.
  Interfaccia aggiornata: conferma disabilitata durante la rilettura, verbale
  disabilitato dopo un nuovo confronto, rilettura disponibile dall'archivio.
- Pannello degli invii email spostato fuori dal pannello del sigillo attivo:
  l'archivio dei colli rimane accessibile anche senza una spedizione selezionata.
- Suite completa `python run.py tests`: **724/724**, 34 gruppi, uscita 0.
  Log: `logs/test-ripresa-email-suite.txt`. Exchange 12/12, riscontro 14/14;
  inclusa prova HTTP di due colli con selezione, riconoscimento e conferma.
- Ruff e sintassi dei JavaScript superati. Test eseguiti fuori sandbox per un
  problema di permessi delle directory temporanee Python 3.14, con TEMP e TMP
  indirizzati a `C:\PROGETTO-RFID\tmp\ripresa-tests`.
- Il controllo visivo è stato tentato su due server temporanei simulati, ma
  l'integrazione browser non ha aperto la pagina in due tentativi. Non dichiarare
  completata la verifica grafica. I server di anteprima sono stati arrestati.
- Nessun invio email reale, nessuna operazione RF, nessuna migrazione manuale
  dell'archivio reale e nessun riavvio della postazione reale durante la ripresa.
  Nessun commit o push. Non avviare prove fisiche o invii senza richiesta specifica.

Guida aggiornata: `docs/CONFIGURAZIONE_OPERATIVA.md`, sezioni email e ricezione.
Resta da controllare visivamente il flusso e collaudare, quando richiesto,
la casella effettiva e i colli sul lettore reale.

## Prova QR con webcam richiesta dall'utente

- Creata `src/app/qr_webcam.py`, avviabile con
  `python run.py qr-webcam --distinta "<file.rfidman>"` o voce 12 del menu.
  Anteprima compatta, riquadro verde sui QR leggibili, acquisizione manuale
  con Spazio/Invio, raccolta multiparte, dati estratti e confronto con il file.
  R ricomincia, Esc chiude. Le immagini rimangono in memoria.
- Riutilizzati parser RFQ1, firma del circuito e decifratura delle distinte.
  Confrontati EPC e campi condivisi; sesso, nascita e ora del prelievo sono
  mostrati dal QR ma esplicitamente esclusi dal confronto perché assenti nel file.
- Distinta indicata dall'utente nei Download: decifrata correttamente, v1,
  spedizione 1, tre contenitori. Nessuna importazione nel database.
- Installati OpenCV 5.0.0.93 e ZXing-C++ 3.1.1. Dipendenze facoltative
  raccolte in `requirements-qr.txt`; istruzioni in `docs/QR_WEBCAM.md`.
- `src/tests/test_qr_webcam.py`: 8/8, inclusi parti fuori ordine, duplicati,
  firma errata, chiavi ruotate, cognomi composti e differenze rispetto al file.
  Prova ottica aggiuntiva superata su QR generati dal progetto, ruotati e
  multiparte, oltre a immagine senza codice.
- Suite completa: **732/732**, 35 gruppi, uscita 0.
  Log `logs/test-qr-webcam-suite.txt`. Ruff e compilazione Python superati.
- Webcam 0 reale provata su richiesta: fotogrammi 640×480, rilascio verificato.
  Prova della finestra Tk: anteprima creata e thread arrestato alla chiusura.
- Applicazione avviata con la distinta dell'utente e lasciata disponibile
  per la scansione del foglio. PID di avvio in `logs/qr-webcam.pid`, eventuali
  errori in `logs/qr-webcam.err`. Non è stata ancora verificata l'acquisizione
  del QR dal foglio fisico dell'utente: i test ottici hanno usato simboli di prova.
- Nessuna operazione RFID, email o modifica dell'archivio reale per questo lavoro.

### Correzione dell'anteprima nera

- L'utente ha segnalato il riquadro nero. Verificato che la USB Camera inviava
  fotogrammi interamente a zero sia con DirectShow sia con Media Foundation.
  Dopo l'intervento dell'utente («prova ora?»), nuova verifica: immagini visibili,
  640×480 e circa 14 fps. Non attribuire il nero alla sola interfaccia.
- Separati acquisizione continua e decoder QR in due thread. Aggiunti selezione
  della webcam, scelta backend, pulsante di riavvio, contatore/fps e avviso per
  fotogrammi neri. La finestra aggiornata è stata riaperta con la stessa distinta.
- Test aggiuntivo facoltativo (`--ottico`): il video continua mentre il decoder
  è bloccato; rilevamento del nero e rilascio dei due thread verificati.

## Webcam integrata nella Ricezione

- Dopo la conferma dell'utente «ok funziona», sostituito il campo della pistola
  QR nella Ricezione con webcam del browser: scelta videocamera, anteprima,
  bordo verde, pulsante Acquisisci e barra spaziatrice. File principale:
  `src/webui/static/qr-camera.js`; caricamento dati in `app.js`.
- Fotogrammi JPEG inviati al solo server della WebUI per ZXing-C++/Pillow;
  `src/webui/qr_camera.py` limita dimensioni e concorrenza. L'endpoint richiede
  token, operatore e ruolo ricezione; non registra immagini nel diario.
- Acquisizione esplicita, raccolta multiparte senza duplicati, firma con tutte
  le chiavi del portachiavi, tabella dei dati estratti e checklist attesi.
  La webcam si spegne a ricezione QR completa, uscita, cambio scheda o comando.
- Corretto il confronto RFID del solo QR: ora `leggi_volume` usa gli EPC del
  foglio e riporta mancanti e inattesi. RFQ1 non porta l'identità completa
  della spedizione: per conferma definitiva e verbale serve il file cifrato.
  Un QR coincidente mantiene la distinta selezionata, uno diverso la disattiva;
  una raccolta incompleta non cambia il contesto. File/collo selezionato in
  seguito azzera i risultati QR nell'interfaccia.
- Test completi **735/735**, 35 gruppi, uscita 0. Log:
  `logs/test-ricezione-webcam-suite.txt`. Le nuove prove HTTP coprono ruolo,
  token, operatore, multiparte, confronto e assenza di immagini nel diario.
- `python src/tests/test_qr_webcam.py --ottico`: superate anche decodifica JPEG
  come dal browser, QR ruotato, coordinate, immagini invalide e limiti pixel.
  `node src/tests/test_qr_camera_ui.cjs`: passati acquisizione manuale,
  Spazio, richieste duplicate, QR non più attuale, spegnimento e permesso tardivo.
  Ruff e controllo sintassi JS superati.
- WebUI reale verificata come radio `stopped`, `ready=false` e riavviata per
  applicare l'aggiornamento; PID in `logs/webui-webcam.pid`, URL attuale in
  `logs/webui_url.txt`, log `logs/webui-webcam.out/.err`. Non usare vecchi token.
- Verificata visivamente in Chrome la nuova Ricezione. Selezionato operatore
  DV e premuto Attiva webcam. Chrome attende il consenso alla fotocamera:
  collaudo del foglio fisico nella WebUI non ancora concluso. Il collegamento
  di automazione al browser si è poi interrotto. Il test standalone era già
  stato confermato funzionante dall'utente. Nessuna prova RFID o invio email
  effettuati per questa integrazione; nessuna nuova ricezione confermata.
- Conferma successiva dell'utente: «ok funziona». Il funzionamento della webcam
  integrata nella Ricezione è quindi confermato dall'utente; la precedente
  attesa del consenso nel browser non è più un punto aperto. La conferma non
  estende il collaudo alle operazioni RFID, all'invio email o al verbale.

## Ripresa del 2 ottobre 2026 — archivio ricezioni

- Ripreso il controllo email/ricezione rimasto aperto. L'inventario browser
  non restituisce browser disponibili; il controllo Windows segnala pipe nativa
  assente. La verifica grafica del flusso email resta quindi da completare.
- Corretto un mancato aggiornamento dell'elenco delle distinte: dopo la conferma
  mostra subito «ricezione completata» e dopo la rilettura «da verificare».
  Aggiornata anche la barra del workflow dopo la lettura.
- Se il rinnovo dell'elenco fallisce, l'avviso invita a riprovare con Aggiorna;
  l'esito dell'operazione già salvata non viene trattato come un fallimento.
  Modifiche in `src/webui/static/app.js` e `exchange.js`.
- Nuovo `node src/tests/test_exchange_ui.cjs`: tre scenari superati con DOM e
  risposte server simulati (conferma/rilettura, errore archivio e recupero,
  conferma rifiutata). Superati anche test UI webcam e controllo visivo e
  controllo sintassi dei due JavaScript modificati.
- Test Python mirati: exchange **12/12**, WebUI HTTP **63/63**. Log HTTP in
  `logs/test-ripresa-20261002-webui.txt`. Il primo tentativo in sandbox ha
  incontrato il problema noto dei permessi TemporaryDirectory Python 3.14;
  riesecuzione fuori sandbox riuscita con TEMP/TMP in `tmp/ripresa-20261002`.
  La suite completa 735/735 resta il risultato della sessione precedente:
  non è stata rieseguita per questa correzione JavaScript.
- Guide e changelog aggiornati. Nessuna operazione RF, invio email reale,
  modifica dell'archivio reale o riavvio della postazione. Nessun commit/push.
  Per caricare il JavaScript aggiornato nella postazione basta ricaricare la pagina.

## 2 ottobre 2026 — scelta webcam nel Sigillo

- Spostato il menu Videocamera fuori dai dettagli di calibrazione, accanto ad
  Attiva webcam; aggiunto Aggiorna videocamere. L'elenco viene caricato anche
  quando si abilita il controllo visivo e rinnovato dopo il consenso del browser.
- La selezione cambia subito la webcam se attiva; a webcam spenta prepara il
  prossimo avvio. Rilascia il vecchio stream e azzera gli angoli manuali.
  Attende la conclusione di analisi/recupero precedenti prima di riaprire.
- Gestiti errori di accesso e dispositivo assente senza bloccare il riavvio.
  Test UI Sigillo e Ricezione superati, inclusi cambio durante il consenso,
  selezione di una seconda camera, riavvio ed errore recuperabile; sintassi JS OK.
- Verifica con dispositivi simulati: nessuna nuova prova fisica, operazione RFID
  o modifica delle prove archiviate. Per applicare basta ricaricare la pagina.

## 2 ottobre 2026 — prova ottica autonoma richiesta dall'utente

- La webcam nella sigillatura mostrava solo il video perché la spedizione 1
  era già received: l'analisi rispondeva «spedizione non modificabile».
  L'utente ha poi richiesto esplicitamente la sola prova del riconoscimento,
  senza procedura, su una borsa rettangolare rossa con fondo bianco e 8 campioni.
- Creata `src/app/vision_preview.py`, servizio solo ottico su localhost:8771;
  pagina e script `prova-visiva.html/.js`. Bordo giallo, cerchi verdi numerati
  sui coperchi dei contenitori, conteggio dal decoder (attesi solo per confronto).
  Nessun database o RFID. Guida `docs/PROVA_RICONOSCIMENTO.md`.
- Il motore `lims/vision.py` restituisce i raggi dei cerchi, usati anche per
  la sovrapposizione nella UI visiva. Prova autonoma HTTP con 8 cerchi sintetici
  e test JS della sovrapposizione superati; test UI Sigillo/Ricezione/Exchange OK.
  Ottica sintetica precedente (20 cerchi misti e coperchio) superata.
- Server di prova avviato; PID in `logs/prova-visiva.pid`, URL/token ottico in
  `logs/prova_visiva_url.txt`. La pagina può usare l'origine WebUI 8770 con
  port=8771 per il decoder autonomo. Il token ottico non è quello RFID.
- Selezionata C920 nel browser, ma il collegamento a Chrome si è interrotto
  durante l'avvio; il recupero non trova più il browser. Non dichiarare 8/8
  rilevati sulla borsa reale. Le prove dirette OpenCV su index 1 non hanno
  restituito un fotogramma e non hanno salvato immagini. Nessuna operazione RF
  o modifica dell'archivio reale; il server WebUI originale non è stato riavviato.

### Campionamento lento e calibrazione dimensioni

- Dopo la segnalazione di artefatti, rallentata la prova a 1,5 secondi per
  fotogramma (regolabile 1–2,5). Conferma dopo tre rilevamenti consecutivi con
  corrispondenza uno a uno e raggio coerente; i rilevamenti assenti non persistono.
- La sovrapposizione usa lo stesso fotogramma analizzato, fermo tra le analisi.
  Due pulsanti consentono di indicare centro/circonferenza del più piccolo e
  del più grande; applicati limiti dei raggi con margine del 20%. Non è training
  di un classificatore e non si impone il numero atteso.
- Test JS: intervallo, filtro di artefatto transitorio, riordino, scomparsa,
  calibrazione e consenso tardivo superati. Prova HTTP sintetica 8/8 superata.
- Browser tornato disponibile: osservati bordo giallo e conteggio ancora
  incompleto (4–5/8), con calibrazione aperta dall'utente. Non dichiarare la
  prova fisica riuscita. Il filtro temporale non elimina falsi cerchi persistenti.
- Solo file statici aggiornati: nessun riavvio dei server o operazione RFID.

### Full HD nella prova ottica

- L'utente ha chiesto più risoluzione perché il campionamento a 1 secondo non
  riconosce tutti i contenitori. La prova richiedeva e riduceva a 1280×720.
- Aggiunto menu Full HD/HD, Full HD predefinito. Ora la richiesta webcam è
  1920×1080 con 15 fps ideali; analisi fino a Full HD senza ingrandire video
  nativo più piccolo. Il server già accettava immagini di queste dimensioni.
- JPEG 96%, con riduzione della qualità solo per rispettare il limite del
  fotogramma; indicatore di dimensioni reali/analizzate, qualità e tempo.
  Cambio risoluzione con applyConstraints; reset del filtro temporale.
- Test UI della conservazione 1920×1080, fallback reale a 720p e cambio HD
  superati; test HTTP/ottico con 8 cerchi Full HD superato. Sintassi JS OK.
- Due tentativi di controllo Chrome hanno bloccato il collegamento; non è
  stata misurata la nuova risoluzione della C920 o confermato il conteggio reale.
  Aggiornamento statico: serve ricaricare la pagina. Nessun riavvio del server,
  nessuna operazione RFID o modifica dell'archivio.

### Prova SAM 2 locale assistita dagli otto campioni

- Su richiesta dell'utente installato SAM 2.1 Hiera Tiny, tramite Transformers,
  in `.venv-sam2` (Python 3.13), con PyTorch/Torchvision CPU. Pesi ufficiali
  scaricati in `models/sam2/tiny`; entrambe le cartelle escluse da Git.
  Versioni collaudate: torch 2.14.1+cpu, torchvision 0.29.1+cpu,
  transformers 5.18.0. L'ambiente del lettore non è stato modificato.
- `src/app/sam2_preview.py`: servizio localhost:8772, token dedicato e URL
  in `logs/sam2_url.txt`, PID in `logs/sam2.pid`, log `logs/sam2.out/.err`.
  Foto e codifica in memoria; inferenza con `local_files_only=True`, verificata
  anche con variabili HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE. Nessun invio di foto.
- Pagina `sam2.html/.js`: webcam Full HD, Ferma foto, clic indipendente per
  contenitore, contorni e correzioni positive/negative per oggetto selezionato.
  Conteggio indicato manualmente distinto dalle regioni ottenute; contorni
  quasi duplicati, fuori dal clic iniziale o troppo estesi segnalati.
  Non è training, conteggio automatico né inseguimento nel video.
- Test contratto/HTTP e UI superati; precedente prova cerchi ancora verde.
  Inferenza reale offline su Full HD sintetico: 8 contorni distinti da 8 prompt,
  18,7 s (codifica 11,62 s) sulla CPU; verificato riuso codifica stessa foto.
  Questo non prova gli otto contenitori della borsa reale. Guida
  `docs/SAM2_LOCALE.md`. Browser intermittente durante la preparazione.
- Browser recuperato: aperta la nuova pagina sull'origine WebUI 8770 con
  servizio SAM 8772, selezionata C920 e fermata la foto reale 1920×1080 della
  posizione attuale. Lasciata aperta per i clic dell'utente, ancora 0 campioni
  indicati e nessuna segmentazione della borsa reale effettuata. Il server
  ottico precedente e la WebUI non sono stati riavviati; archivio invariato.

### Conteggio delle sei foto dell'utente e modelli con visione

- Letti soltanto i PNG `esempio1`–`esempio6` da
  `C:\Users\Daniele\Pictures\RFID`, senza modificare gli originali.
  Quantità confermate sulle immagini: **8, 8, 8, 8, 6, 5**.
- Nuovo `tools/conta_foto_sam2.py`: SAM 2 automatico offline con griglia di
  prompt, filtri di geometria circolare e deduplicazione. Nessun clic sui
  contenitori o numero atteso passato al modello. Finestra di ricerca unica
  [.05,.18,.9,.86], scelta per l'interno della stessa borsa in tutte le foto:
  non è un rilevamento automatico dell'area né un classifier addestrato.
- Corretto nello script il sistema di coordinate della griglia. Nella libreria
  installata `Sam2ImageProcessor.generate_crop_boxes` usa longest-edge, mentre
  SAM 2 ridimensiona i due assi a 1024. La prima prova ritagliata cercava solo
  nella parte superiore e produceva 4 nella prima foto. La griglia quadrata
  locale usa correttamente l'intera area; decoder/filtri AMG sono della libreria.
  Nessuna modifica ai pacchetti o alla pagina guidata dai clic.
- Inferenza reale completata sulle sei foto: SAM **8,8,8,8,6,5**, coincidono con
  la verifica visiva; tutti i 43 rilevamenti corrispondono a contenitori visibili,
  nessun mancante/falso nei risultati finali. Sono foto ripetute degli stessi
  oggetti: 43 NON è il numero di contenitori fisici distinti. Tempi 67,6–102 s
  per foto sulla CPU. Valutazione sullo stesso piccolo gruppo usato per la prova,
  non un dato di accuratezza generale o un dataset indipendente.
- Output finali `demo-output/conteggio-foto-20261002/sam_corretto/`; report
  complessivo `demo-output/conteggio-foto-20261002/report.html`, `RISULTATI.md`
  e `verifica_algoritmo.json`, con PNG numerati e maschere NPZ. Le prove precedenti
  nelle altre sottocartelle sono conservate per audit, non sono l'esito finale.
- Hough originale non delimita il bordo in nessuna foto; sull'intera foto
  produce 37,22,22,30,33,31 candidati con artefatti. SAM iniziale su intera foto
  produceva 9 nella prima, includendo un angolo esterno: la regione tagliata dal
  bordo è esclusa dai criteri finali per campioni interamente visibili.
- Utente ha chiesto consiglio su Ollama/llama.cpp: suggerito Qwen3-VL 2B
  quantizzato per una prima prova sul PC m3/16GB; 4B come confronto più oneroso.
  Documentazione ufficiale Ollama/llama.cpp verificata. Nessun Qwen, Ollama o
  llama.cpp installato o avviato per questa richiesta; confronto ancora da fare.
- Ruff/compilazione script superati, sei inferenze e contorni realmente
  verificati. Nessuna operazione RFID, email, database, commit o riavvio WebUI.
- Su richiesta di confronto con una modalità già presente, eseguito Hough
  con parametri predefiniti e la stessa ROI di SAM sulle sei foto: candidati
  **16,9,11,18,18,15**, contro SAM **8,8,8,8,6,5**. I numeri Hough includono
  falsi cerchi/duplicati, non sono quantità affidabili. Tempi Hough 0,06–0,34 s
  contro SAM 68–102 s; confronto di accuratezza/velocità sullo stesso materiale.
  Report `demo-output/conteggio-foto-20261002/confronto.html`, JSON
  `confronto_metodi.json` e PNG Hough della stessa area. Collegato anche dal
  report principale; verificati i riferimenti alle immagini. Non sono stati
  installati altri modelli. SAM assistito usa lo stesso modello, non è un
  algoritmo indipendente per il confronto.

### Perimetro della borsa/scatola esterna dalle sei foto

- L'utente ha confermato che vuole il contenitore esterno, non i bordi dei
  singoli campioni; l'inquadratura resterà simile. Mantenuto SAM 2 Tiny.
- `src/app/sam2_borsa.py` e `tools/rileva_borsa_sam2.py`: quattro riferimenti
  normalizzati sulle pareti [.5,.12], [.92,.5], [.5,.88], [.06,.5], scelti
  una volta e uguali per tutte le foto. Una codifica e tre maschere SAM per
  foto; selezione per area 55–98%, score >=.8, componente principale >=95%
  dei pixel e presenza dei riferimenti nella sagoma esterna riempita.
  Non è addestramento né un detector semantico universale.
- Nella quarta foto SAM escludeva un pixel del riferimento laterale per
  un riflesso: verifica spostata sulla sagoma riempita, appropriata per il
  perimetro esterno. La prova definitiva è stata rieseguita sulle sei foto.
- Sagoma rilevata in tutte e sei, tempi CPU finali 10,19 / 14,10 / 17,18 /
  14,08 / 12,13 / 20,11 s. Il contorno segue il corpo trasparente e può
  essere irregolare attorno a maniglie/agganci: non misura precisamente
  il bordo tessile rosso. Nessun errore in pixel misurato rispetto a un
  bordo annotato; i punteggi SAM non sono una misura di accuratezza reale.
- Lati vicini al limite della foto segnalati con fascia conservativa 1%
  del lato corto, minimo 2 px; nessun tratto esterno ricostruito. Azzurro
  per il bordo, arancione per i segmenti nella fascia del ritaglio.
- Output `demo-output/conteggio-foto-20261002/bordi/finale/report.html`,
  `panoramica.png`, sei PNG annotati, maschere PNG e JSON normalizzati.
  Link aggiunto al report principale. Gli esiti di conteggio precedenti
  8,8,8,8,6,5 sono sovrapposti senza rieseguire/cambiare la ricerca: tutti
  i 43 rilevamenti risultano interamente dentro la sagoma scelta.
- Verificate visivamente le sei annotazioni, riferimenti HTML esistenti,
  4/4 test `test_sam2_borsa.py` e Ruff. `tools/prova_bordo_sam2.py` conserva
  la prima esplorazione di box/pareti/box+pareti, non è il comando finale.
- Foto originali inalterate. Prova solo da cartella, nessuna integrazione
  nel video o sostituzione automatica della ROI di conteggio. Nessun RFID,
  database, email, riavvio del server webcam, commit o nuovo modello.

### Scatto automatico e calibrazione della camera fissa

- Utente vuole uno scatto seguito da bordo e conteggio automatici; il campo
  originale è più ampio delle foto e va selezionata/salvata la parte della
  borsa. Camera futura fissa: aggiunta modalità a quattro angoli sul bordo
  superiore per correggere la prospettiva. Utente non conosce ancora le
  misure, ha chiesto campi in cm e può misurare altezza camera dal bordo.
- Nuova pagina `src/webui/static/sam2-auto.html/.js`, separata dalla procedura
  RFID e dalla prova manuale che resta disponibile con link reciproci.
  Anteprima, selezione rettangolare per trascinamento o quattro clic in senso
  orario, salva/annulla/reset; campi larghezza, lunghezza, altezza camera in cm.
  Misure facoltative; le prime due entrambe compilate o entrambe vuote.
  Salvataggio localStorage `rfid.sam2.area.v2.<deviceId>`, per webcam/origine/
  browser, con controllo del rapporto dell'immagine; un cambio fisico della
  camera va ricalibrato dall'operatore. Foto non persistite nel browser.
- Ritaglio ai pixel della sorgente, ingrandito a schermo. In prospettiva viene
  ritagliato prima anche l'invio (bbox del quadrilatero con piccolo margine),
  ricalcolando i punti: non si riduce tutta una eventuale sorgente 4K perdendo
  inutilmente il dettaglio della borsa. Inviato JPEG entro i limiti Full HD.
- `src/app/sam2_geometry.py`: quattro angoli convessi in senso orario,
  omografia OpenCV, rapporto dai cm o stimato dai lati; margine esterno 4%
  per SAM. Correzione solo sul piano indicato, non distorsione radiale della
  lente né parallasse da quote diverse. Altezza camera salvata e restituita
  come metadato, non usata in una compensazione 3D inventata.
- `src/app/sam2_automatico.py`: bordo, bbox interno con profilo
  [.05,.18,.90,.86], AMG 16×16 e filtri/duplicati. Generatore e filtri
  estratti senza modificarli in `src/app/sam2_count.py`, condivisi con
  `tools/conta_foto_sam2.py`. Escluse regioni tagliate dalla finestra o fuori
  dalla borsa (copertura almeno95%). Bordo assente => conteggio null/non
  determinabile, non falso zero. Finestra tratteggiata visibile: campioni
  fuori dal profilo interno non ricercati; verificare posizionamento.
- Nuove API nel banco ottico `/api/sam2/prepara` e `/api/sam2/automatico`:
  token/origine/JPEG/FullHD e lock condivisi. 409 se già impegnato, niente
  accodamento CPU. Stato con secondi trascorsi, tempi separati a fine analisi;
  nuova foto scarta risposte precedenti. Timeout browser300s non interrompe
  l'inferenza già partita nel server.
- `tools/prova_sam2_automatico.py` collauda lo stesso motore da file.
  Inferenze reali: foto1 8 in88,80s; stessa foto1 rettificata 8 in87,48s;
  HTTP reale foto5 JPEG ritagliata da scena FullHD sintetica con artefatti
  esterni 6 in126,62s (HTTP126,93). Tre prove, media100,97s, due sulla stessa
  foto: non un nuovo collaudo completo delle sei né un dato di accuratezza
  generale. Ultima scena larga costruita per test, non vera nuova foto webcam.
- Report `demo-output/sam2-automatico-20261002/report.html`, `tempi.json`,
  tre JSON/PNG, scena sintetica e ritaglio. Annotazioni visivamente corrette.
- 5/5 test geometria/conteggio/API (`test_sam2_automatico.py`), incluso lock
  condiviso durante una richiesta attiva; UI nuova testata per pixel del
  ritaglio, persistenza, cambio webcam/aspect ratio, quattro angoli/misure,
  rettifica prima di SAM, risposte obsolete e consenso tardivo.
  Regressioni SAM manuale/HTTP/borsa (4/4), prova ottica HTTP/UI e Ruff passati.
- Riavviato SOLO il servizio SAM8772 per caricare nuove API, conservando
  token/URL con `--riusa-token`. PID del launcher18780 in `logs/sam2.pid`;
  log nuovi `sam2_automatico_stdout.log`/`stderr.log`; nuovo collegamento
  `logs/sam2_auto_url.txt`. WebUI8770 invariata e senza riavvio. Nessuna
  operazione RFID, DB, email o commit, nessun altro modello installato.
- Chrome: pagina automatica tab425117675, URL sulla WebUI8770 con port8772
  e camera=c920. Verificati DOM e campi reali; dopo permesso webcam i nomi
  dispositivi si sono aggiornati, C920 selezionata al reload. L'area/calibrazione
  definitiva non è stata impostata dall'agente e nessun conteggio reale della
  nuova webcam è stato effettuato: l'utente deve indicare gli angoli quando
  la camera è nella posizione fissa. Misure reali lasciate vuote.
- Ultima verifica browser: C920 attivata, risoluzione effettiva1920×1080;
  modalità "Quattro angoli e correzione prospettica" selezionata. Scheda
  marcata deliverable e lasciata aperta con anteprima, pronta per la selezione
  dell'utente. Nessun punto/area o misura reale inserito dall'agente.
- Successivo riscontro dell'utente: «ok funziona bene», riconoscimento di
  17 elementi in circa94s. Chiede confronto CPU più veloce/GPU. Verificato
  codice: modello forzato CPU, PyTorch CPU, 2thread, generatore device=-1;
  griglia16×16=256prompt, l'onere non è proporzionale ai17 oggetti finali.
  CPU del PC riconfermata dal registro: Intel m3-8100Y,4processori logici.
  Consigliata GPU NVIDIA/CUDA per il salto principale, con adattamento di
  ambiente e codice; nessun benchmark GPU disponibile né tempo garantito.
  Consultate fonti ufficiali PyTorch/SAM2/Transformers e NVIDIA (RTX5060Ti
  esiste in16GB/8GB, RTX3060 in12GB/8GB). Non modificati parametri, servizio,
  calibrazione o modello in risposta a questa domanda sull'hardware.

## Ripresa: GPU Intel, Qwen e collaborazione GitHub — 2 ottobre 2026

- Installato OpenVINO 2026.4.1 nell'ambiente isolato `.venv-openvino`,
  riusando le dipendenze di `.venv-sam2` tramite un file `.pth`.
  GPU Intel UHD Graphics 615 effettivamente utilizzata: encoder e decoder
  `GPU.0`, senza fallback automatico CPU. Guida `docs/SAM2_OPENVINO.md`.
- Conversione `tools/converti_sam2_openvino.py`: encoder eager e decoder
  in `models/sam2/openvino/`. Il primo encoder SDPA produceva risultati
  inaccurati e NON viene selezionato. L'encoder eager ha errore massimo
  delle maschere 1,5974e-5 rispetto a PyTorch su CPU F32 nella foto1.
- Benchmark GPU F16 automatico sulle sei foto: 8,7,8,8,6,5;
  media40,06s, intervallo38,96–40,89s. Caricamento/compilazione fredda100,20s
  esclusi. La foto2 dà7 anche nel motore automatico CPU originale: limite
  della ROI interna. Non alterati griglia16×16 e filtri. HTTP foto6 JPEG:
  5 campioni in52,15s complessivi. Nessun benchmark GPU sui17 della webcam.
- Nuovi motori/script: `src/app/sam2_openvino.py`, dispositivi/convertitore/
  confronto numerico e benchmark sotto `tools/`; `run-sam2-openvino.bat`.
  Report e immagini in `demo-output/openvino-20261002/`, esclusi da Git.
- Confronto OpenRouter reale: sei foto distinte, verità8,8,8,8,6,5, due
  richieste per foto. Qwen3.8 27B/DekaLLM12/12 corretti, media3,44s;
  Qwen3 VL235B instradato automaticamente9/12, media5,02s (tre falsi zeri
  di Alibaba); fissando Parasail12/12, media8,08s. Totale36 richieste,
  costo0,017785208USD. Nessun numero atteso/filename/contorno SAM nel prompt.
- `src/app/openrouter_vision.py`, `tools/conta_foto_openrouter.py` e
  `tools/rapporto_confronto_vision.py`; report locali sotto
  `demo-output/openrouter-20261002/`. API key già nell'ambiente, mai
  stampata/salvata nei file né inviata al browser. Guide nuove in docs.
- Pagina `sam2-auto.html`: selettore SAM locale/Qwen3.8 27B,
  **Rianalizza lo scatto** per stesso JPEG già ritagliato/rettificato.
  Calibrazione precedente conservata; Qwen sovrappone centri blu numerati,
  non segmentazioni inventate. Nessun invio video continuo: richiesta
  remota solo su scatto/rianalisi espliciti. Provider DekaLLM senza fallback,
  thinking disattivato e risposta strutturata validata.
- Nuova API `/api/qwen/automatico`: token/origine, limiti JPEG/FullHD e
  lock condiviso con SAM/preparazione. Errori rete/modello espliciti,
  nessun falso zero. Quattro test offline OpenRouter e cinque automatico
  superati; test UI verificano cambio modello e riuso degli stessi byte.
- Riavviato solo SAM8772 conservando token/URL; launcher3988,
  processo servizio18684, avvio16:37:07. Log `sam2_openvino_stdout.log`/
  `stderr.log`. WebUI8770 invariata. Richiesta Qwen HTTP reale foto6:
  5 campioni, cinque centri,3,63s. Questa è una prova da file sull'API
  usata dalla webcam, non un nuovo scatto reale dei17 campioni.
- L'utente ha poi chiesto commit di tutto il progetto e collaborazione
  su nuovo account `danieleventuri2013-web` con `Willmat79`. Browser
  confermato sul nuovo account, vecchio remote ancora sul precedente;
  avviato accesso GitHub CLI tramite device login. Il browser di collaudo
  precedente non è più disponibile: nessuna nuova calibrazione inventata.
- Esclusi anche `.tmp/`, `tmp/`, `.env*` e kernel.errors.txt. Controllati
 197 file candidati e storia Git: nessuna chiave API/token/chiave privata
  riconosciuta. Foto, log, DB, ambienti e pesi restano locali. Aggiornata
  CI per dipendenze ottiche e test Node; corrette dieci segnalazioni Ruff
  preesistenti (import, zip esplicito e binding di callback nei test).
- Verifica pre-commit: runner747/747; pytest773/773, coverage82,38%;
  tutte le sei suite UI Node e Ruff superati. Dopo la verifica completa
  aggiunto e superato il quinto test Qwen, per scatti FullHD rumorosi.
- Prova vera webcam: la calibrazione C920 è stata ripristinata nel browser
  nuovo (ritaglio1127×805 su1920×1080,48×26,5cm, altezza64cm). Lo scatto
  Qwen dell'utente ha esposto HTTP413: PNG troppo grande. Corretto SOLO
  l'invio webcam in JPEG95, con qualità adattiva fino75 e limite1,2MB,
  mantenendo la risoluzione; benchmark da file resta PNG. Nuovo test con
  PNG FullHD oltre5MB dimostra JPEG limitato senza perdere pixel.
- Secondo riavvio soloSAM8772 per la correzione413: launcher12428;
  token e foto congelata nel browser conservati. Console aperta tab425118294
  Chrome browser2, sulla WebUI8770, camera=c920&motore=qwen.
- Accesso CLI confermato come `danieleventuri2013-web`, repository privato
  `danieleventuri2013-web/PROGETTO-RFID` creato per la collaborazione richiesta.
  I codici temporanei del login non vengono conservati nella documentazione.
- Pubblicato commit5cc72fa nel repository privato, ramo `codex` come
  default. `origin` è il nuovo account; il vecchio remote è conservato
  come `precedente`. Autore usa email noreply del nuovo account.
  GitHub CI del primo commit completata con successo su tutti i job.
- Invito `Willmat79` con permesso write inviato e verificato (335812327),
  dopo conferma esplicita dell'utente «si lettura e scrittura». La prima
  richiesta write era stata fermata dall'auto-review, senza azioni svolte;
  push effettuato separatamente e livello di accesso chiesto all'utente.
  L'invitato deve accettare su GitHub; non dichiararlo già membro attivo.
- Console vera webcam riaperta, calibrata automaticamente dai dati già
  salvati dall'utente; scatto congelato presente. Qwen iniziale18 in8,691s:
  l'utente conferma che18 è il riflesso sulla parete superiore. SAM2 sulla
  stessa foto17 in49,88s (bordo2,95, conteggio46,93), OpenVINO GPU F16.
- Prompt webcam rinforzato: contare contenitori sul fondo, escludere copie
  speculari/traslucide/parziali sulle pareti, segnalare dubbi. Nessun numero
  atteso né coordinate del riflesso nel prompt. Benchmark da file invariato.
  Nota del modello adesso mostrata, invece di sostituirla con sola legenda.
- Due richieste Qwen webcam dopo modifica:17 in4,197s e17 in3,652s,
  centri17 verificati e riflesso escluso. Prima rianalisi operata dall'utente;
  seconda dall'agente sulla medesima foto congelata con Rianalizza lo scatto.
  Non una prova su due scene nuove né una garanzia su altri riflessi.
- Ultimo riavvio soltantoSAM/Qwen8772 per prompt: launcher14060, avvio
  17:02:37, log usuali. Console tab425118294 Chrome2 lasciata aperta con
  risultatoQwen17; area, token, foto e misure invariati. TestQwen5/5,
  UI automatico e Ruff nuovamente superati dopo modifica.
- Pubblicata correzione riflessi nel commitdca7596. Il collega segnalava404:
  verificato via API che Willmat79 aveva già accettato l'invito e possedeva
  write. L'utente ha autorizzato il repository pubblico come rimedio.
  Ora `danieleventuri2013-web/PROGETTO-RFID` è PUBLIC, scrittura Willmat79
  conservata; pagina verificata HTTP200 senza login e `git ls-remote`
  senza credential helper riuscito su HEAD/codex. URL da condividere:
  https://github.com/danieleventuri2013-web/PROGETTO-RFID .
  Codice e storia visibili a chiunque; foto, log, DB, chiavi e modelli
  continuano a essere esclusi. Non è cambiata la proprietà dell'account.
