# Prova SAM 2 locale con clic manuali

SAM 2.1 Hiera Tiny, eseguito sulla CPU tramite Transformers. Installazione in
un ambiente separato Python 3.13; i modelli restano in `models/sam2/tiny`, esclusi
da Git. Nessuna modifica alle dipendenze del lettore. Riferimenti ufficiali:
[SAM 2 Meta](https://github.com/facebookresearch/sam2) e
[SAM 2 in Transformers](https://huggingface.co/docs/transformers/model_doc/sam2).

Installazione PowerShell dalla cartella del progetto:

```powershell
py -3.13 -m venv .venv-sam2
./.venv-sam2/Scripts/python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
./.venv-sam2/Scripts/python.exe -m pip install -r requirements-sam2.txt
./.venv-sam2/Scripts/python.exe src/app/sam2_preview.py --download
./.venv-sam2/Scripts/python.exe src/app/sam2_preview.py
```

Il download recupera soltanto configurazione e pesi ufficiali; non invia foto.
Il servizio carica solo file locali e ascolta su `127.0.0.1:8772`. Il collegamento
con token distinto è scritto in `logs/sam2_url.txt`. Si può usare l'origine
webcam già autorizzata della WebUI con:
`http://127.0.0.1:8770/sam2.html?t=<token SAM>&port=8772&camera=c920&auto=1`.
Non usare il token RFID. Il servizio SAM non apre database o lettori RFID.

1. Attivare la webcam, scegliere la C920 e lasciare i campioni in posizione.
2. Premere **Ferma foto per segnare i campioni**.
3. Con **Indica un nuovo campione**, cliccare dentro ciascun contenitore.
   Ogni clic crea un oggetto indipendente, numerato in blu. Non occorre
   disegnare manualmente il contorno. La prova accetta da 1 a 20 oggetti.
4. Premere **Disegna contorni con SAM 2**. Il primo passaggio codifica la foto
   sulla CPU; i passaggi successivi sulla stessa foto riusano la codifica.
5. Verificare visivamente ogni contorno verde/arancione. Se sbagliato,
   selezionare il numero del campione e aggiungere punti interni (positivi)
   oppure punti da escludere (negativi), quindi ripetere l'analisi.
   **Annulla ultimo clic** consente di correggere una selezione sbagliata.
6. Per una posizione diversa premere **Nuova foto**, che cancella i vecchi punti.

I punti blu provengono dall'utente; i contorni sono ottenuti dal modello.
“Contorni SAM” conta soltanto regioni con un contorno, comprendenti il primo
clic, non troppo estese e non quasi identiche a una precedente (>80% IoU).
È un controllo geometrico per segnalare risultati sospetti, non una
certificazione: anche regioni diverse possono appartenere allo stesso oggetto.
Otto clic non dimostrano un conteggio automatico riuscito. Il modello non è
addestrato dai clic e questa prova non segue gli oggetti nel video.

Foto e codifica in memoria; nessun salvataggio di immagini. La foto resta
disponibile dopo lo spegnimento della webcam. Se viene chiesta una nuova foto
durante l'analisi, la vecchia risposta è scartata; l'operazione CPU già iniziata
può finire nel server. Analisi serializzate, token/origini e dimensioni limitati.
In caso di attesa superiore a 180 secondi attendere il termine nel server prima
di riprovare; non vengono lanciati processi di inferenza paralleli.

Verifiche:

```powershell
python src/tests/test_sam2_preview.py
node src/tests/test_sam2_ui.cjs
./.venv-sam2/Scripts/python.exe src/tests/test_sam2_preview.py --modello
```

Le prime due non richiedono il modello. L'ultima esegue SAM su una foto Full HD
sintetica e verifica il riuso della codifica; non prova la borsa reale.

## Conteggio sperimentale da una cartella di foto

`tools/conta_foto_sam2.py` esegue una griglia di prompt generati dal programma,
senza clic sui contenitori, e usa decoder/filtri AMG di Transformers. Le regioni
ottenute vengono filtrate per area, circolarità, solidità e proporzioni; regioni
annidate dello stesso oggetto sono deduplicate. Le regioni tagliate dal bordo
della foto sono escluse: questa prova riguarda contenitori interamente visibili.
Il numero atteso non è un ingresso del motore.

```powershell
./.venv-sam2/Scripts/python.exe -u tools/conta_foto_sam2.py 'C:\Users\Daniele\Pictures\RFID' --output demo-output/conteggio-foto-20261002/sam_corretto --griglia 16 --roi .05 .18 .9 .86 --riprendi
```

La ROI è una finestra relativa unica, scelta per queste fotografie della stessa
borsa: non è un rilevamento automatico dell'area, né un'annotazione dei singoli
contenitori. Omettere `--roi` per analizzare tutta la foto; una griglia più densa
richiede più tempo sulla CPU. Salvati maschere NPZ, risultati JSON, PNG con
contorni numerati e HTML. Le foto originali restano inalterate. Il salvataggio
è specifico di questa prova da cartella; la pagina webcam continua a lavorare
solo in memoria.

Correzione locale della griglia: nella versione installata di Transformers,
`Sam2ImageProcessor.generate_crop_boxes` normalizza i punti con una scala
uniforme longest-edge. SAM 2 ridimensiona invece i due assi a 1024, come
`Sam2Processor._normalize_coordinates`. Lo script costruisce la griglia nel
sistema quadrato del modello; altrimenti una foto rettangolare verrebbe
campionata soltanto in parte. Nessuna modifica ai pacchetti installati o alla
pagina assistita, che già usa Sam2Processor per i clic manuali.

Questo conteggio combina SAM con criteri geometrici per coperchi rotondi:
non identifica semanticamente qualunque contenitore. Confrontare sempre le
annotazioni, soprattutto in caso di sovrapposizioni o nuovi tipi di campioni.

## Perimetro della borsa/scatola esterna nelle foto

La prova `tools/rileva_borsa_sam2.py` usa lo stesso modello con quattro punti
relativi sulle pareti, scelti una volta per l'inquadratura delle foto fornite.
SAM propone tre maschere: si sceglie la regione ampia, con score almeno .8,
che ha una componente principale dominante. I buchi interni vengono riempiti
per estrarre soltanto il perimetro esterno e verificare che la sagoma comprenda
tutti i riferimenti, anche quando un riflesso ha escluso il pixel del prompt.
Le regioni piccole o estese a tutta la foto sono rifiutate. Se nessuna maschera
rispetta questi criteri viene segnalato "bordo non determinabile".

```powershell
./.venv-sam2/Scripts/python.exe -u tools/rileva_borsa_sam2.py 'C:\Users\Daniele\Pictures\RFID' --output demo-output/conteggio-foto-20261002/bordi/finale --conteggi demo-output/conteggio-foto-20261002/sam_corretto
python src/tests/test_sam2_borsa.py
```

L'opzione `--conteggi` sovrappone i risultati precedenti dei campioni, senza
rieseguire né cambiare la loro ricerca. Il bordo è azzurro; i tratti al limite
della foto sono arancioni e rendono il perimetro incompleto/non verificabile.
La prossimità al limite usa una fascia dell'1% del lato corto (minimo 2 pixel),
così piccoli errori di segmentazione non nascondono un possibile ritaglio.
Salvati report
HTML, contorni normalizzati JSON, maschera PNG e immagine annotata. Gli
originali restano inalterati. La ricerca del bordo non richiede il numero
dei campioni e non usa i loro clic.

Il contorno è una stima della sagoma esterna del corpo trasparente: maniglie,
agganci e bordo rosso possono renderlo irregolare. Non è una misura precisa
del bordo tessile rosso. Il punteggio del modello non è una misura dell'errore
reale in pixel. Se cambia la borsa o la camera occorre rivedere i riferimenti
in `src/app/sam2_borsa.py`; questi punti non addestrano SAM. Lasciare margine
intorno alla borsa per analizzare tutti i lati. La prova resta da cartella:
non è ancora integrata nel video e non sostituisce la ROI del conteggio.

## Scatto automatico, area salvata e correzione prospettica

La pagina `sam2-auto.html`, servita dal banco SAM su 8772 o dalla WebUI su
8770 con `port=8772`, offre uno scatto seguito da bordo e conteggio automatici.
Il collegamento completo viene scritto in `logs/sam2_auto_url.txt` all'avvio
del servizio. Dalla pagina manuale è disponibile il collegamento alla prova
automatica. Non occorre segnare i singoli campioni.

1. Attivare la webcam. La sorgente richiesta è Full HD, con risoluzione
   effettiva mostrata nella pagina.
2. Scegliere **Ritaglio rettangolare** oppure **Quattro angoli e correzione
   prospettica**, poi **Seleziona area sull'inquadratura completa**.
3. Ritaglio: trascinare un rettangolo che comprenda la borsa con un piccolo
   margine. Prospettiva: cliccare i quattro angoli del bordo superiore sullo
   stesso piano, nell'ordine alto sinistra, alto destra, basso destra, basso
   sinistra. È possibile annullare l'ultimo punto.
4. Campi facoltativi in cm: **larghezza**, **lunghezza**, **altezza della
   camera dal bordo superiore**. Larghezza e lunghezza vanno indicate entrambe
   o lasciate entrambe vuote; in quest'ultimo caso le proporzioni sono stimate.
5. **Salva area**. La calibrazione è conservata nel localStorage di questo
   browser per il deviceId della webcam e l'origine della pagina. Non contiene
   foto. Un diverso rapporto dell'inquadratura richiede nuova selezione; un
   cambio fisico di posizione della camera va ricalibrato dall'operatore.
6. **Scatta e conta automaticamente** congela la foto, corregge la prospettiva
   se configurata e avvia il riconoscimento. Bordo azzurro, campioni verdi
   numerati, finestra interna di ricerca tratteggiata. Il tempo trascorso viene
   aggiornato durante l'attesa; il risultato mostra tempi separati.
7. **Torna all'anteprima** per lo scatto successivo. **Seleziona area** permette
   anche di modificare le misure salvate senza dover cambiare i punti.

Il ritaglio usa i pixel della sorgente senza ingrandimento artificiale per SAM.
L'anteprima riempie la pagina con l'area selezionata. In modalità prospettiva
l'invio usa prima un ritaglio attorno al quadrilatero con piccolo margine,
ricalcolando le coordinate dei quattro angoli; non riduce inutilmente l'intera
sorgente prima della selezione. Anche una sorgente più grande può conservare
il dettaglio della zona selezionata entro i limiti Full HD del servizio.
In modalità prospettiva
l'anteprima mostra il ritaglio con il quadrilatero; la foto congelata viene
rettificata con `cv2.getPerspectiveTransform` / `warpPerspective` e le dimensioni
reali fissano il rapporto tra i lati. Un margine del 4% fuori dal rettangolo
segnato consente a SAM di distinguere il bordo. La rettifica corregge il piano
scelto, non la distorsione radiale dell'obiettivo né la parallasse dovuta ad
altezze diverse dei campioni. L'altezza della camera è conservata come dato
di calibrazione e non viene usata in una compensazione 3D approssimativa.

Il motore `src/app/sam2_automatico.py` rileva prima la borsa con gli stessi
riferimenti della prova su foto. Poi esegue una griglia AMG 16×16 nell'interno
del rettangolo rilevato, usando il profilo relativo [.05,.18,.90,.86]. Sono
condivisi con lo script da cartella i filtri circolari e la deduplicazione
(`src/app/sam2_count.py`). Escluse regioni tagliate dalla finestra e regioni
che non ricadono almeno al 95% nella sagoma della borsa. Questo è un profilo
per la stessa borsa con campioni rotondi; non un detector universale. I
campioni fuori dalla finestra interna non sono ricercati, quindi verificare
la finestra tratteggiata e l'assenza di campioni tagliati/esterni.

Se il bordo non è determinabile non viene mostrato un falso conteggio zero:
il risultato è **non determinabile**. API dedicate `/api/sam2/prepara` e
`/api/sam2/automatico`, stesso token/origini/dimensioni e lock della prova
manuale. Foto solo in memoria, nessun archivio RFID. Il browser interrompe
l'attesa dopo 300 secondi ma l'inferenza già avviata può terminare nel server;
una nuova foto scarta la risposta precedente, senza accodare analisi CPU.

```powershell
node src/tests/test_sam2_auto_ui.cjs
python src/tests/test_sam2_automatico.py
./.venv-sam2/Scripts/python.exe -u tools/prova_sam2_automatico.py 'C:\Users\Daniele\Pictures\RFID\esempio1.png' --output demo-output/sam2-automatico-20261002/foto1
```

Il collaudo da foto usa lo stesso motore della pagina automatica e salva
JSON/PNG soltanto come verifica esplicita da riga di comando. Per aggiornare
il servizio conservando il collegamento delle pagine già aperte si può
riavviarlo con `--riusa-token`; l'avvio normale genera un nuovo token.

Collaudo del flusso automatico del 2 ottobre:

- Foto 1: 8 campioni, 88,80 s complessivi (bordo 7,44 s, conteggio 81,35 s).
- Stessa foto rettificata con quattro angoli e proporzioni stimate: 8 campioni,
  87,48 s (bordo 8,27 s, conteggio 79,20 s).
- API reale su foto 5 ritagliata da una scena Full HD sintetica più ampia,
  con cerchi esterni esclusi prima dell'invio: 6 campioni, 126,62 s sul motore,
  126,93 s inclusa la richiesta HTTP. Non è una foto webcam dell'inquadratura
  ampia reale. Tre prove, media 100,97 s: due riguardano la stessa prima foto.

Report `demo-output/sam2-automatico-20261002/report.html`, PNG e JSON dei tre
casi, contorni verificati visivamente. Le misure effettive della borsa e
l'altezza della camera non sono ancora state fornite: la calibrazione finale
va effettuata sulla posizione fissa, usando i campi e quattro angoli della
pagina. I tempi sopra escludono l'avvio del servizio/caricamento del modello.

## Accelerazione con la GPU Intel

Il banco dispone anche del motore OpenVINO in un ambiente separato. La
configurazione dell'area e la correzione prospettica sono condivise con
la versione CPU. Installazione, avvio e verifiche in
[SAM2_OPENVINO.md](SAM2_OPENVINO.md).
