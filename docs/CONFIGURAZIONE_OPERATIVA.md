# Configurazione operativa RFID

Aggiornamento 5 settembre 2026. I nuovi comandi sono nella WebUI
(`python run.py webui`); il pannello desktop resta uno strumento tecnico.

Per elenco pazienti, bozze, più campioni e ripresa dei tag mancanti, vedere
[Accettazione giornaliera](ACCETTAZIONE_GIORNALIERA.md).

## Collegamento e antenne

Collega avvia il firmware e applica i parametri radio salvati: non censisce
antenne, non misura il tag e non avvia la sorveglianza del piatto di scrittura.
Vale sia per seriale sia per TCP. Il censimento precedente viene indicato come
da rilevare dopo un cambio di collegamento.

In Impostazioni → Configurazione:

1. Usa **Rileva hardware** quando vuoi interrogare le caratteristiche del lettore.
2. In **Antenne — chi legge e chi scrive**, seleziona le porte per ciascun uso.
   La stessa porta può avere entrambi i ruoli. Un rilevamento di connessione
   negativo non impedisce l'assegnazione manuale; una porta inesistente, se
   nota dal censimento, viene rifiutata.
3. Premi **Applica e salva**. Il file YAML conserva i ruoli al riavvio.
   Le potenze restano regolabili negli strumenti e vanno salvate come predefinite.
4. Se l'applicazione radio fallisce, correggi il problema e premi
   **Applica configurazione radio salvata**. Salvato su disco non significa
   applicato al lettore. Una configurazione invalidata blocca le operazioni radio.

Conservare la regione EU 0x08. La configurazione operativa non modifica la banda
per compensare un'antenna poco adatta.

## Modelli di tag diversi

In **Sede e profili tag** puoi salvare più modelli con nomi distinti.
Caricare un profilo riempie i box; **Salva sede e profilo tag** lo rende operativo.
Non viene imposto a tutti i modelli il minimo misurato su un altro tipo di tag.

| Memoria | Lettura | Scrittura |
| --- | --- | --- |
| EPC | Sempre, identifica il contenitore | Sempre, pseudonimo riferito al database |
| TID | Facoltativa in solo EPC; necessaria per autenticare USER | Non prevista |
| USER | Facoltativa | Facoltativa; comporta sempre rilettura di verifica |

Per un tag piccolo senza USER: capacità USER 0, box USER disattivati.
Il codice EPC collega il contenitore alla riga del database e quindi al paziente;
non contiene nome o codice fiscale. Il formato applicativo richiede spazio per
l'EPC generato: la compatibilità non riguarda tag con EPC arbitrariamente piccolo.
Il percorso solo EPC non richiede una chiave per scrivere il tag; distinta e
trasferimenti mantengono i propri requisiti crittografici.

**Misura il tag** è una diagnosi esplicita con un solo tag sul piatto. Il risultato
si approva separatamente e poi si salva nel profilo del modello. Una misura
fallita per timeout, silenzio o accesso negato non viene trasformata in capacità
zero. Con USER insufficiente l'approvazione disattiva USER; una misura capiente
non la abilita automaticamente. La misura non è una certificazione del lotto.

Senza TID univoco il sistema può seguire l'EPC, ma non riconoscere con certezza
lo stesso chip fisico se un altro apparecchio ne cambia il codice. USER cifrata
lega i dati a EPC e TID: questa proprietà non esiste nel profilo solo EPC.

## Scrittura e prototipazione

Appoggia un solo tag sulla postazione, premi **Scrivi** e attendi la verifica.
L'animazione segue gli eventi della scrittura, senza inventari periodici del
piatto. Dopo il risultato rimuovi il contenitore e premi **Prossimo contenitore**.
La sorveglianza della scatola di spedizione resta un'attività distinta.

**Prototipazione** abilita il riuso dei tag senza una conferma a ogni giro e
mostra una segnalazione permanente in testata. La scelta persiste nel YAML;
è disattivata per impostazione iniziale. Usarla con campioni e dati di prova:
riscrivere un tag già spedito renderebbe non più leggibile il vecchio riferimento.
Le assegnazioni precedenti con TID restano nello storico, non vengono cancellate.
Nessuna procedura aggiunge lock permanenti per consentire le prove.

I tentativi conservano nel database EPC precedente, EPC riservato e fase.
Se la risposta si perde, il comando esplicito successivo verifica il tag presente
e recupera lo stesso EPC, anche dopo riavvio. Un tag o una configurazione diversi
non sono accettati come continuazione alla cieca. Non ci sono retry automatici
di una scrittura dall'esito ambiguo. Stato definitivo e assegnazione del chip
vengono registrati insieme dopo la verifica radio.

## Sedi separate

**Funzione della sede**: spedizione, ricezione oppure entrambe (default).
La scelta nasconde i flussi non pertinenti e blocca le relative operazioni API;
non è solo una preferenza grafica. In sola ricezione la lista delle antenne di
scrittura può essere vuota. Prima di cambiare ruolo occorre chiudere o annullare
le attività in corso. Non è un sistema di autorizzazioni per utenti diversi.

La ricezione solo EPC deve avere la distinta importata o i dati corrispondenti:
il tag non trasporta da solo l'anagrafica. Non è stato introdotto un database
centralizzato o un servizio di sincronizzazione fra sedi.

## Email con più colli

In **Sigillo → Invia più colli via email**, apri la configurazione della casella
mittente e salva almeno l'indirizzo. Il destinatario viene dall'anagrafica della
sede. Seleziona i colli già chiusi diretti alla stessa sede e premi **Prepara email
per i colli selezionati**: l'anteprima mostra indirizzi, testo e allegati.
Ogni collo mantiene la propria distinta cifrata; i dati dei pazienti restano
negli allegati, non nel testo del messaggio.

- **Scarica email con allegati (.eml)** prepara il messaggio per un client di
  posta compatibile. Il download non invia nulla.
- **Scarica tutte le distinte (.zip)** e **Copia testo per la webmail** permettono
  l'invio manuale. Allega le distinte alla mail, poi registra l'invio eseguito
  con **Conferma invio dalla webmail**.
- **Invia email** usa la casella SMTP configurata, con STARTTLS oppure SSL/TLS.
  L'invio deve essere abilitato e la credenziale disponibile nella variabile
  d'ambiente indicata (predefinita `RFID_MAIL_PASSWORD`) prima dell'avvio.
  La password non viene salvata nel YAML. Una sede con PEC abilitata usa il
  proprio flusso PEC per l'invio dal programma.

L'accettazione del messaggio da parte di SMTP non prova la consegna al destinatario
né la partenza del materiale. Conferma la partenza e la ricezione nei rispettivi
flussi. Con esito SMTP incerto o invio interrotto, il sistema blocca il reinvio
della stessa bozza: verifica la posta prima di preparare un nuovo invio.

## Archivio delle distinte in ricezione

Importa uno o più file `.rfidman`, oppure un pacchetto `.zip` contenente soltanto
distinte, senza cartelle: massimo 50 distinte e 2 MiB per gruppo. L'importazione
può precedere l'arrivo dei colli; ogni file mostra il proprio esito. Importare di
nuovo lo stesso documento non duplica la ricezione e non cambia la distinta attiva.

Appoggia una scatola alla volta e premi **Leggi il collo e trova la distinta**.
Un solo EPC di collo associato a una distinta ancora aperta richiama gli attesi
e confronta il contenuto. Se manca il tag del collo, seleziona manualmente la
distinta e premi **Leggi il volume**. Più colli nel campo, una distinta ambigua
o una scatola diversa da quella selezionata richiedono una nuova verifica.

Conferma il confronto e poi esporta il verbale. Per aggiornare una ricezione già
confermata, selezionala nell'archivio e rileggi il volume: il nuovo confronto
richiede una nuova conferma prima dell'esportazione. Dopo una lettura fallita,
anche un riavvio richiede una nuova lettura prima di poter confermare.

L'elenco si aggiorna automaticamente dopo la conferma e dopo una nuova lettura:
mostra rispettivamente «ricezione completata» e «da verificare». Se il rinnovo
dell'elenco fallisce, un avviso invita a premere **Aggiorna**; l'operazione già
registrata resta valida.

## Verifiche e limiti

Per usare SAM 2 locale o Qwen nella Sigillatura con area e prospettiva
salvate nel banco webcam, configurare videocamera, modello e calibrazione in
**Impostazioni → Impostazioni Controllo Visivo** e seguire
[Riconoscimento dei campioni nel Sigillo](RICONOSCIMENTO_SIGILLO.md).

Chiusura del 2 ottobre 2026: **754/754 test runner**, **781/781 pytest**,
sei suite UI Node e Ruff superati. Calibrazione disponibile senza spedizione;
il nuovo percorso completo webcam/RFID richiede ancora un collaudo fisico.
Memoria e prossime prove: [nota del 2 ottobre](../NOTE_SESSIONE_2026-10-02.md).

Ripresa del 1 ottobre 2026: **724/724 test automatici superati**, inclusi scambio
HTTP di due colli, allegati, duplicati, errori radio, riavvio e migrazione schema 9.
Superati Ruff sui file Python interessati e i controlli sintattici JavaScript.
Nessun invio email reale o operazione sul lettore fisico in questa ripresa.
Il controllo visivo è rimasto indisponibile per un errore dell'integrazione browser.

Collaudo concluso il 6 settembre 2026: 689/689 test automatici superati,
compresi 20 cicli di riuso con archivio, recupero dopo riavvio e profili
con capacità USER differenti. Verificati anche sintassi JavaScript e
salvataggio/ripristino di profilo, ruolo sede e antenne tramite browser
su database temporaneo e backend simulato.

Eseguire `python run.py tests` per la suite senza hardware e
`python src/tests/test_operativita.py` per i nuovi casi operativi.
Per l'aggiornamento dell'archivio nell'interfaccia eseguire
`node src/tests/test_exchange_ui.cjs` (DOM e risposte server simulati).
Prima dell'uso al banco seguire [la checklist radio](VERIFICA_FORNITORE_20260905.md).
Il software non può garantire riscrivibilità di un EPC bloccato permanentemente,
durata illimitata della memoria o azzeramento del flag Gen2 con RF continua.
