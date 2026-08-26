# Il flusso di lavoro, dal vassoio alla scatola chiusa

Documento operativo. Descrive come si lavora davvero al banco e perché il
programma è fatto così. Il riferimento è `flusso-di-lavoro.txt`, che è la
descrizione del laboratorio; qui c'è la traduzione in gesti e schermate.

> **Niente di quanto segue è stato validato su hardware.** Le soglie del
> riempimento e del sigillo sono valori prudenti di partenza, non misure. Vanno
> tarate contro la campagna di misura (`python run.py campaign`) prima di
> considerarle buone.

---

## 1. Arrivano i contenitori

Arrivano già etichettati dal reparto, in ordine sparso, ognuno con sopra nome,
cognome, identificativo, data, ora e descrizione del campione. Il programma non
li conosce: li conoscerà uno alla volta, man mano che vengono trascritti.

## 2. Si trascrive, uno per volta

**Schermata Accettazione.** Si prende un contenitore, si trascrive quello che
c'è scritto sull'etichetta, si appoggia il contenitore sull'antenna di
scrittura e si preme *Scrivi il tag*.

Tre cose che il programma fa da solo, e il perché:

* **Con un contenitore solo non chiede la conferma del numero.** La domanda
  «li hai contati?» esiste perché il totale finisce dentro il chip e dopo la
  prima scrittura non si corregge più. Con un vasetto in mano quella domanda
  non ha oggetto. Da due in su torna, e resta obbligatoria.
* **Quando il contenitore lascia l'antenna, il modulo si azzera da solo** ed è
  pronto per il successivo. Non è un cronometro a deciderlo: è la lettura che
  ha visto il contenitore andare via. Chiedere «Nuova accettazione» dopo ogni
  provetta sarebbe una cerimonia per trenta volte al giorno.
* **Se quel paziente ha già altri contenitori registrati oggi, lo dice.** È una
  nota, non un ostacolo: quattro campioni dello stesso paziente sono normali, e
  il sistema lo scopre solo adesso — prima non aveva modo di saperlo. Serve
  perché l'operatore lo sappia subito invece che alla distinta.

**Data e ora del prelievo** vengono dall'etichetta del reparto, non
dall'orologio della postazione: un campione può essere stato prelevato ieri. La
data si precompila a oggi perché è così quasi sempre; l'ora resta vuota finché
non la si legge, perché una mezzanotte inventata è peggio di un campo in bianco.

### Quando un paziente ha più campioni

Quanti campioni ci siano dello stesso paziente **non si sa in anticipo**: si
scopre prendendoli dal vassoio, uno alla volta. Perciò il modo normale di
lavorare resta quello: ogni contenitore si trascrive per conto suo, e quando il
paziente ricompare il programma lo dice.

Se invece si sa già che quei quattro campioni sono stati presi nella stessa
seduta, si registrano insieme — ma **descrizione, materiale, fissativo, sede e
avvertenze possono essere tutti diversi**, e quasi sempre lo sono. Il pulsante
*+ Un altro campione di questo paziente* aggiunge un reperto con i suoi campi:

| | campione 1 | campione 2 | campione 3 |
|---|---|---|---|
| descrizione | pezzo operatorio, colon | linfonodo sentinella | margine di resezione |
| materiale | biopsia | agobiopsia | pezzo operatorio |
| fissativo | formalina 10% | a fresco | a fresco |
| sede | colon | polmone | — |
| avvertenze | rischio biologico | — | urgente |

**Un campione, un contenitore.** Non c'è nessun numero da digitare: se il
paziente ha un secondo vasetto si aggiunge un secondo campione, con la sua
descrizione. Un numero da solo non direbbe comunque *quali* sono i campioni, e
quanti siano non si sa prima di averli in mano.

**La numerazione è dell'accettazione, non del reperto**: quei tre vasetti sono
1/3, 2/3 e 3/3. Non 1/1, 1/1 e 1/1, che sarebbero tre etichette in cui il
paziente si perde — e l'EPC porta già l'indice riferito all'accettazione.

Ogni tag riceve **i codici del proprio reperto**: scriverli tutti uguali sarebbe
un errore che a destinazione nessuno può più correggere. Durante la scrittura la
cartella mostra la descrizione del contenitore che si ha in mano, che con quattro
vasetti dello stesso paziente è l'unica cosa che li distingue.

Se ci si accorge di un campione dimenticato **dopo** aver registrato, quel
campione è una nuova accettazione — che è poi la strada normale. I tag già
scritti conservano il totale che avevano: è scritto nel chip e non si riscrive.

## 3. Si possono scrivere più tag di quanti ne entrino in una scatola

Sessanta tag scritti e poi venti per scatola: è il caso normale. La barra in
alto porta sempre il **residuo da spedire** — quanti campioni sono scritti e non
ancora dentro una scatola. Diventa ambra appena c'è qualcosa in attesa.

È il numero che impedisce di finire la giornata con un campione scritto e mai
partito, che è il modo in cui si perde un campione senza che nessuno se ne
accorga.

## 4. Si riempie la scatola, un campione per volta

**Schermata Sigillo → *Apri la scatola e riempi*.** Si appoggia la scatola sulle
antenne di lettura e si infilano i campioni **uno alla volta**.

Ogni campione riconosciuto compare nell'elenco con nome, cognome e descrizione,
accompagnato da un **segnale acustico**. Nessun pulsante dice «l'ho messo
dentro»: la postazione se ne accorge leggendo, e ogni riga corrisponde a una
lettura vera.

Cosa può succedere, e come viene detto:

| Situazione | Cosa vedi | Cosa fare |
|---|---|---|
| campione preparato qui | riga verde, bip acuto, conteggio +1 | niente, continua |
| tag mai scritto | riquadro rosso «campione non inizializzato», doppio bip grave | quel contenitore non è stato preparato: toglilo e trattalo a parte |
| contenitore di un'altra scatola | riquadro rosso col nome del paziente | è già in un'altra spedizione: va cercato lì |
| contenitore annullato | riquadro rosso | non doveva più esistere |
| tag sul coperchio | identifica la scatola, non si conta | niente |

**La quantità si conferma con letture ripetute, non con una.** Un tag può
sparire per un giro perché un altro vasetto gli è finito davanti o perché il
liquido lo scherma. Le due soglie sono asimmetriche di proposito:

* per **entrare** bastano poche letture — il bip deve arrivare subito dopo il
  gesto, se arriva tre secondi dopo l'operatore ha già messo il campione
  successivo e non sa più a quale si riferisce;
* per **uscire** ne servono di più. Togliere una riga dall'elenco è la
  direzione pericolosa: fa credere di dover aggiungere un campione che c'è già.
  Se invece resta di troppo, è il sigillo a scoprirlo.

Se la lettura arriva a un contenitore appoggiato **accanto** alla scatola, il
pulsante *Non è nella scatola* lo toglie. Resta escluso finché non lascia
davvero il campo: se poi lo si mette dentro per davvero, sparisce e ricompare, e
viene riconosciuto da capo.

La potenza durante il riempimento è **bassa di proposito**: a coperchio aperto
il tavolo accanto è pieno di contenitori di altre spedizioni, e meno portata
significa meno vicini di casa.

## 5. Si controlla e si chiude

L'elenco mostra quanti campioni ci sono e chi sono. Se ne manca uno, lo si
riposiziona e la lettura successiva lo trova. Quando il conto torna: *Ho finito:
chiudi la scatola*, poi si chiude il coperchio e si preme *Chiudo la scatola e
certifico*.

**Il riempimento e il sigillo sono due cose diverse e servono entrambe:**

* il riempimento sa **cosa è entrato**, ma a coperchio aperto — quello che vede
  potrebbe stare accanto alla scatola invece che dentro;
* il sigillo certifica **cosa c'è dentro** a scatola chiusa, ma solo rispetto a
  un elenco che qualcuno deve avergli dato.

Se un contenitore era appoggiato fuori invece che dentro, il riempimento lo
conta e il sigillo no: la spedizione non parte, e si scopre perché.

## 6. Si stampa la distinta

*Stampa la distinta* — disponibile solo a sigillo completo, perché mandare un
documento che dichiara contenitori non verificati sposterebbe il problema a
destinazione, dove nessuno può più controllare.

Il foglio porta **tutto**: identificativo del paziente, cognome, nome, sesso,
data di nascita, data e ora del prelievo, descrizione del campione, materiale,
fissativo, sede, numero di contenitore, EPC. E un **QR** con le stesse identiche
righe, per non ridigitarne trenta all'arrivo.

Porta tutto perché i due laboratori **non condividono nessun archivio**: quello
che non è scritto sul foglio, dall'altra parte non esiste.

> **Il foglio contiene dati sanitari in chiaro**, QR compreso. È la conseguenza
> diretta di non avere un archivio condiviso. Va nella busta agganciata alla
> scatola, non incollato all'esterno.

Dettagli del formato: `docs/DISTINTA_QR.md`.

## 7. Il residuo, sempre sotto gli occhi

Dopo ogni chiusura di scatola il residuo si aggiorna. Le scatole già sigillate
non spariscono: restano in *Scatole da finire*, con scritto cosa manca —
distinta da mandare, partenza da confermare. Si riprendono da lì.

## 8. Il giro si chiude quando la roba è arrivata

**Al destinatario.** Si apre la distinta (dal file, oppure leggendo il QR sul
foglio con un lettore di codici a barre 2D), si appoggia la scatola **ancora
chiusa** sulle antenne e si legge. Il confronto dice cosa è arrivato e cosa no.
Poi *Conferma la ricezione* e ***Esporta il verbale per il mittente***.

**Al mittente.** Schermata Archivio → *Importa un verbale*. Solo allora quella
spedizione smette di essere «partita» e diventa «arrivata».

Prima del verbale il mittente sa una cosa sola: di aver spedito. Le ricevute PEC
provano che il *documento* è arrivato, non che le provette ci siano.

**Il riepilogo del transito** (Archivio → *Genera il riepilogo*) elenca cosa è
passato fra i due centri in un periodo, con l'esito di ogni spedizione. Può dire
«tutto a buon fine» solo se ogni spedizione ha un verbale che dice che è
arrivata intera; il resto si chiama **non confermato**, mai «arrivato». Esce
anche in CSV, con punto e virgola e BOM perché è lì che verrà aperto.

---

## Quando l'hardware non c'è

Tutto quanto sopra si prova senza lettore:

```
python run.py webui --simulato
```

Compare in Strumenti il **Banco di prova**: da lì si appoggiano e si tolgono i
campioni dall'antenna. Con `--simulato <file>` si parte dai tag di una sessione
vera. Vedi `docs/DIARIO_PROTOTIPAZIONE.md`.
