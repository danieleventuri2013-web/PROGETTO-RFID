# Riconoscimento dei campioni nella Sigillatura

In **Sigillo**, abilita **Controllo avanzato visivo / RFID**. La ricerca usa gli
stessi motori della prova webcam: SAM 2 locale (OpenVINO sulla GPU Intel,
se avviato così) oppure Qwen3.8 27B tramite OpenRouter. Rimane disponibile
il rilevamento classico continuo dei cerchi.

La scatola selezionata deve essere ancora da verificare: una spedizione già
ricevuta, inviata o con distinta archiviata permette di consultare la prova,
ma non di aggiungere un nuovo riconoscimento. Il pannello mostra il motivo e
disabilita i comandi relativi alla nuova prova. Per una prova ottica senza
spedizione usare **Impostazioni → Impostazioni Controllo Visivo**.

1. Avvia la WebUI con `python run.py webui` e il banco con
   `run-sam2-openvino.bat`. Per Qwen il banco deve trovare
   `OPENROUTER_API_KEY` nell'ambiente di avvio.
2. Seleziona l'operatore e apri **Impostazioni → Impostazioni Controllo Visivo**.
   Scegli una videocamera specifica e il modello (SAM 2, Qwen3.8 27B o cerchi classici),
   poi premi **Salva impostazioni controllo visivo**.
3. L'area salvata oggi nel banco viene riutilizzata per la stessa webcam,
   nello stesso browser e all'indirizzo della WebUI (`127.0.0.1:8770`). Se
   manca, premi **Configura area e prospettiva** nella stessa sottosezione:
   il riquadro di calibrazione si apre sotto i parametri. Attiva la webcam,
   salva il rettangolo o i quattro angoli e le misure, poi chiudi il riquadro.
   La calibrazione è disponibile anche senza spedizione aperta e non invia
   fotografie ai modelli. Uscendo dalla sottosezione la webcam si spegne.
4. Apri una spedizione da verificare e abilita il controllo visivo nel Sigillo.
   Il riepilogo mostra videocamera, modello e area; il pulsante
   **Impostazioni Controllo Visivo** riporta alla configurazione.
   Attiva la webcam. Con la scatola aperta e i campioni tutti visibili,
   premi **Scatta e cerca campioni**. Il ritaglio precede l'eventuale limite
   Full HD; la rettifica usa gli stessi quattro angoli e le misure in cm.
   SAM mostra bordo e contorni, con la finestra di ricerca tratteggiata;
   Qwen mostra centri blu numerati e la nota sui campioni e sui riflessi.
5. **Rianalizza lo stesso scatto** ripete l'analisi della foto con il modello
   configurato. Per cambiare modello torna alle Impostazioni, salva la scelta
   e acquisisci un nuovo scatto nel Sigillo.
   Controlla i numeri; **Correggi marcatori** permette di aggiungere,
   togliere o spostare un centro sulla foto ritagliata/rettificata.
6. Quando la scena è stabile e il numero corrisponde alla distinta, il
   recupero RFID segue il flusso già previsto. Una concordanza permette
   di acquisire una nuova foto del contenuto e confermarla. Segui poi i
   controlli del coperchio e la certificazione finale della scatola chiusa.

In **Marcatori, cerchi e riferimenti del coperchio**, sempre nelle
**Impostazioni Controllo Visivo**, sono raccolti i parametri dei cerchi,
le opzioni dei marcatori e la stampa, la selezione del bordo interno e i
riferimenti della scatola aperta/con coperchio. Prima salva i parametri,
poi usa **Attiva anteprima di calibrazione** e memorizza i riferimenti.
L'anteprima non esegue recuperi RFID e non modifica le prove conservate.

La prova cifrata conserva foto, centri automatici/confermati, eventuali
correzioni manuali, modello, tempo e nota, insieme al riscontro RFID.
Questi dati accompagnano la distinta e sono consultabili in Ricezione.

SAM e Qwen analizzano solo lo scatto esplicito. Durante l'anteprima la
WebUI confronta localmente la scena con la foto di riferimento, anche
vicino ai centri: uno spostamento rilevato annulla stabilità, correzioni
e riscontro precedente. Un cambio di area, modello o contenuto richiede
una nuova verifica. Le risposte di uno scatto superato vengono scartate.
In caso di errore o bordo assente compare un risultato non determinabile;
l'incertezza dichiarata dal modello richiede verifica/correzione manuale.

La soglia del confronto di scena è un controllo di movimento, non una
garanzia di rilevare ogni sostituzione. Contorni e centri non identificano
gli EPC: il sigillo continua a dipendere dalle identità lette via RFID.
La rettifica corregge il piano indicato, non lente o parallasse in altezza.
Con Qwen lo scatto viene inviato a OpenRouter solo premendo uno dei due
comandi di analisi; chiave API e video continuo restano fuori dal browser
e dalle richieste remote, rispettivamente.

L'attesa massima è circa 310 secondi per SAM e 130 per Qwen. Interrompere
la webcam scarta il risultato nel browser; il servizio può finire l'analisi
già avviata e restare occupato fino alla sua conclusione.
Gli errori di scatto rimangono visibili durante l'anteprima: per una webcam
senza area salvata, configurare prima il suo profilo. Un'altra webcam non
eredita automaticamente la calibrazione del dispositivo usato in precedenza.

Guide: [SAM locale](SAM2_LOCALE.md), [GPU Intel](SAM2_OPENVINO.md),
[Qwen e OpenRouter](OPENROUTER_VISION.md).
