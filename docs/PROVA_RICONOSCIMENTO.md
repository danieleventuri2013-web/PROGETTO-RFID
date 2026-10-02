# Prova autonoma di riconoscimento

Per la prova alternativa con AI locale e campioni indicati manualmente,
vedere [SAM 2 locale](SAM2_LOCALE.md): foto ferma, clic separati per oggetto
e contorni dal modello. Il rilevamento dei cerchi descritto sotto resta distinto.

Avvio: `python src/app/vision_preview.py`. Aprire l'indirizzo stampato o
salvato in `logs/prova_visiva_url.txt`, consentire la webcam e scegliere quella
che inquadra la borsa. Dipendenze ottiche: `requirements-qr.txt`.

Il bordo della borsa è giallo; ogni coperchio rotondo riconosciuto ha un cerchio
verde numerato. Il conteggio proviene dai cerchi nell'immagine: il campo
«Campioni presenti» serve solo al confronto e non viene inviato al decoder.
Immagini in memoria, analisi locale; il banco non apre database o lettori RFID.

Inquadrare tutto il fondo, possibilmente dall'alto. Se il bordo non è
determinabile, «Indica il bordo della borsa» consente di selezionare quattro
angoli in ordine lungo il perimetro. Questa scelta delimita l'area di ricerca;
il conteggio dei coperchi resta automatico. Raggio minimo/massimo e soglia
cerchi si possono regolare dal pannello del rilevamento.

L'analisi lenta usa di default un fotogramma ogni 1,5 secondi; il menu consente
1 o 2,5 secondi. I cerchi vengono confermati solo dopo tre rilevamenti consecutivi
compatibili per posizione e raggio. Un oggetto che scompare viene tolto al
fotogramma successivo. Il disegno usa il fotogramma analizzato, fermo fra due
analisi, per evitare sovrapposizioni su un video che nel frattempo è cambiato.
Un falso cerchio persistente può comunque essere confermato: il filtro temporale
non sostituisce una buona delimitazione dell'area e la calibrazione delle dimensioni.

Per calibrare, premere «Indica il campione più piccolo», cliccare il centro del
coperchio più piccolo e poi la sua circonferenza. Ripetere con il campione più
grande. Il raggio minimo include un margine del 20% verso il basso; quello massimo
un margine del 20% verso l'alto. È una calibrazione geometrica, non un modello
addestrato; i campioni indicati non vengono aggiunti al conteggio.

Con WebUI già aperta su `127.0.0.1:8770`, la stessa pagina è accessibile come
`/prova-visiva.html?t=<token della prova>&port=8771`. Il servizio ottico su 8771
accetta soltanto la propria origine e quella WebUI locale. Il token della prova
è distinto da quello RFID. Non usare questa pagina con il token RFID.
`&camera=c920&auto=1` seleziona la C920, se presente, e avvia la webcam.

Verifiche: `python src/tests/test_vision_preview.py` e
`node src/tests/test_vision_preview_ui.cjs`. Otto cerchi sintetici riconosciuti;
token, JPEG invalidi e parametri controllati. La UI verifica numeri e cerchi
sovrapposti, conteggio indipendente dagli attesi e rilascio dei permessi tardivi.
Questi risultati non attestano il conteggio sulla borsa reale.

## Risoluzione e dettaglio

La prova richiede di default 1920×1080 (Full HD), a 15 fps ideali della webcam.
La frequenza dei fotogrammi analizzati rimane quella del menu «Analizza ogni»:
un intervallo di 1 secondo non aumenta la risoluzione dell'immagine.
Il menu Risoluzione consente anche 1280×720; la selezione viene applicata alla
webcam attiva senza riaprirla. Il browser può fornire una risoluzione inferiore:
il riepilogo mostra la risoluzione effettiva e quella usata nell'analisi.

I fotogrammi Full HD non vengono più ridotti a 720p. Il JPEG usa qualità 96%;
solo se supera il limite di 2,8 milioni di caratteri base64 si riduce la qualità
di compressione, conservando la risoluzione. Non vengono ingrandite immagini
native più piccole per simulare dettaglio aggiuntivo. Tempo dell'analisi e qualità
JPEG vengono mostrati nel riepilogo. Più pixel non correggono sfocatura, riflessi
o un punto di vista che nasconde parte del coperchio.
