# Gli strumenti di misura: cosa chiede ogni pannello, e come si legge la risposta

Stato: 2026-08-26. **Niente di quello che c'è qui è stato validato su hardware.**
Le curve che si vedono sul banco simulato servono a provare l'interfaccia, non a
prevedere cosa faranno le antenne vere.

Gli strumenti stanno in **Impostazioni › Misure**. Sono la parte del programma
che serve a *tarare il prototipo*, non a farlo funzionare: i flussi operativi
(accettazione, sigillo, ricezione) impostano già la radio da soli e non
dipendono da niente di quello che si tocca qui.

Cinque pannelli, e ognuno esiste per rispondere a una domanda precisa.

---

## 1. Adattamento delle antenne — «sono cattive, o accordate altrove?»

È la domanda che ha fatto nascere questa pagina.

Il **VSWR** (rapporto d'onda stazionaria) dice quanta della potenza che il
lettore manda all'antenna torna indietro invece di partire. 1,0 vuol dire che
parte tutta; più il numero sale, più ne torna. Sopra 7 il pannello lo segnala,
ed è una soglia prudente, non una misura.

Il punto è che **un VSWR alto a 866 MHz non dice se l'antenna è sbagliata**. Sono
due guasti diversi e si curano in modi opposti:

| Cosa si vede | Cosa vuol dire | Cosa si fa |
|---|---|---|
| curva alta ovunque, nessun minimo | antenna guasta, cavo interrotto, connettore ossidato | si cambia il pezzo |
| curva con un minimo netto, ma **fuori** dalla banda EU | antenna buona, accordata dove non serve | si chiede al fornitore la stessa antenna tarata per l'Europa |

Le **SLP1027** del prototipo sono specificate 902–928 MHz. In Italia si lavora a
865–868 MHz. Il secondo caso è quindi quello atteso, ed è esattamente il motivo
per cui questo pannello guarda anche fuori dalla banda: per distinguere i due
casi bisogna **vedere dove sta il minimo**, e per vederlo bisogna spazzare largo.

### In che condizione si misura — e perché conta più dei numeri

Questa misura **non ha niente a che vedere con la lettura dei tag**. Non serve
nessun tag, e metterne uno non cambia il risultato: un tag passivo è uno
scatteratore troppo piccolo per caricare l'antenna. Quello che il comando misura
è l'*impedenza vista al connettore* — quanta potenza torna indietro invece di
partire.

Quello che **sì** sposta la curva è tutto ciò che sta nel **campo vicino**
dell'antenna: a 866 MHz la lunghezza d'onda è circa 35 cm, e il campo reattivo
si estende all'incirca **sei centimetri**. Dentro quei sei centimetri contano
metallo, acqua, una mano, il piano d'appoggio. Fuori, quasi niente.

Da cui la regola: **antenna nella posizione di lavoro, a vuoto, e niente si
muove durante la spazzata.**

E da cui una distinzione che è facile perdere e costosa da sbagliare — **sono
due misure diverse che rispondono a due domande diverse**:

| Condizione | Cosa misura | A chi serve |
|---|---|---|
| **a vuoto**, in posizione di lavoro | l'antenna (più cavo e connettori) | è la curva **da mandare al fornitore**: l'unica confrontabile con il suo datasheet |
| **con la scatola piena sopra** | il sistema che avremo davvero | serve a noi: trenta flaconcini di liquido nel campo vicino spostano e smorzano la curva, ed è questo che spiega perché leggere è difficile |

Mandare al fornitore la seconda significherebbe dargli un numero che descrive il
nostro liquido, non la sua antenna. È lo stesso errore, di categoria, del minimo
di bordo spacciato per risonanza. **Vale la pena farle entrambe; vale molto la
pena non confonderle.**

Due dettagli che cambiano la lettura:

- si misura l'antenna **più il suo cavo e i suoi connettori**, perché il comando
  guarda dalla porta del modulo. Un SMA ossidato si legge come un'antenna
  scadente — ed è esattamente il motivo per cui le tre antenne si confrontano fra
  loro;
- le tre antenne sono complanari e vicine: quelle inattive ma collegate sono
  elementi parassiti per quella in misura. Non è un problema, **purché le tre
  misure siano fatte tutte nella stessa configurazione**, altrimenti il confronto
  non confronta niente.

Per questo il pannello ha un campo **«In che condizione»**: la nota viaggia con
la misura, finisce nell'etichetta della curva e nel registro. Tre curve diverse
possono essere tre antenne diverse **oppure la stessa antenna con tre cose
diverse sul tavolo**, e fra un mese, senza quella riga, non si distinguono più.

### Come si usa

1. Scegli l'antenna, lascia l'intervallo su **banda configurata** e misura.
   Ottieni il verdetto che conta per l'esercizio: il VSWR peggiore dentro la
   banda ETSI.
2. Cambia l'intervallo in **860–960 MHz** e misura di nuovo. Le curve **si
   sovrappongono** invece di sostituirsi.
3. Ripeti per le tre antenne.

### Come si legge

- La **fascia evidenziata** è la banda ETSI 865–868 MHz. È dove il sistema deve
  funzionare per legge; tutto il resto della curva è contesto che serve a
  capire, non a scegliere.
- Sotto il grafico compare la frase da portare al fornitore, del tipo
  *«Minimo a 913,5 MHz (VSWR 1,25), 47 MHz sopra il centro della banda europea»*.
- Se il minimo cade **al bordo** della spazzata il programma lo dice e non lo
  chiama risonanza: la risonanza vera sta più in là, e riferire quel numero
  significherebbe portare al fornitore una frequenza che non esiste. Allarga
  l'intervallo.
- **Tre curve che non si somigliano** su antenne uguali indicano un cavo o un
  connettore, non un problema di progetto. È il confronto a dirlo, non il
  singolo numero.

### Il cambio di regione

Il firmware è certificato per una regione e può rifiutare di misurare sulle
altre (stato `0x010B`). Non è un guasto: il programma lo dice e **chiede** se
commutare la regione per il tempo della spazzata.

Se accetti, in quei secondi **il modulo trasmette fuori dalla banda ETSI**. Ha
senso su un banco di prototipazione e in nessun altro posto. La regione viene
rimessa a posto subito dopo, **anche se la misura fallisce a metà** — c'è un
test apposta, perché una misura interrotta che lasciasse la postazione fuori
banda sarebbe un guaio silenzioso.

---

## 2. Parametri Gen2 — «è servito a qualcosa?»

Quattro tendine senza ritorno non insegnano niente. Adesso ogni applicazione
**rilegge dal modulo** i valori effettivi.

Serve perché il modulo può accettare una modalità RF che non supporta e
**sostituirla in silenzio**: senza rilettura si crederebbe di stare misurando in
massima sensibilità mentre si è in modalità normale, e tutti i numeri raccolti
sarebbero da buttare.

**Imposta i valori consigliati** applica in un colpo la configurazione di
riposo, e dice il perché di ognuno:

| Parametro | Valore | Perché |
|---|---|---|
| sessione | **S2** | il flag resta a lungo: un tag già letto tace e lascia parlare gli altri |
| target | **A↔B alternato** | ripulisce le popolazioni miste senza restare fermi su metà campo |
| Q | **automatico** | il numero di contenitori cambia a ogni scatola; fissarlo sarebbe indovinare |
| modalità RF | **0x71** (massima sensibilità) | i tag sotto liquido sono il caso difficile |

> Questi comandi servono a **sperimentare**. Il sigillo cambia sessione e
> modalità a ogni passata per conto suo (`SealingSession._apply`), e la campagna
> ha la sua griglia: quello che imposti qui non cambia come funziona il sigillo.

### La prova di lettura

Il misuratore accanto risponde a «e adesso legge meglio?». Fa un gruppo breve
di inventory e restituisce letture al secondo, tag distinti, tasso di
rilevamento per singolo tag, RSSI minimo/mediano/massimo.

**Tiene la misura precedente accanto a quella nuova**, perché «14 letture al
secondo» da solo non vuol dire niente: vale il confronto prima/dopo. In
continuo, si cambia un parametro e si guarda il numero muoversi.

Il **tasso per tag** è la colonna più utile: un tag al 30% mentre gli altri sono
al 100% è il contenitore in fondo alla scatola, ed è quello che decide se la
configurazione regge.

---

## 3. Profilazione del tag — «cosa ci si può scrivere?»

La profilazione legge TID e USER memory di un tag e misura **quanti byte si
riesce davvero a scrivere e rileggere**. Poi propone cosa cambiare in
configurazione, e se accetti **lo scrive in `config.yaml`** conservando i
commenti: prima andava ricopiato a mano.

- la soglia **scende** liberamente: deve reggere il tag peggiore del lotto;
- la soglia **sale** solo con conferma esplicita, perché alzarla renderebbe
  illeggibile un tag più piccolo già scritto e in viaggio.

### Tag senza USER memory: la modalità ridotta

Se sul tag il campione cifrato non ci sta, il programma propone
`modalita_scrittura: solo_epc`. In quella modalità:

- sul tag si scrive **solo l'EPC** — lo pseudonimo di 12 byte;
- i dati del paziente viaggiano **sulla distinta stampata**, in chiaro e nel QR;
- accettazione, sigillo, distinta e ricezione funzionano **tutti**.

**Cosa si perde:** il legame fra campione e numero di serie del chip, cioè la
difesa contro un tag copiato. Restano il registro perpetuo degli EPC e la firma
sulla distinta.

Questa modalità ha senso **adesso** e non l'avrebbe avuta prima del foglio con il
QR: è quel foglio a portare i dati che il chip non porta più.

Il passaggio **non è mai automatico**. Una difesa che decade in silenzio è una
difesa che nessuno ha deciso, e mesi dopo nessuno saprebbe più perché. Si
conferma una volta, resta scritto in configurazione, e da lì in poi una **fascia
fissa in testata** lo ricorda su ogni schermata.

Un tag letto in modalità ridotta risulta `solo_epc`, **non** `illeggibile`:
chiamarlo rotto manderebbe a cercare un guasto che non c'è.

---

## 4. Salute del lettore — «è il cavo, è il rumore o è il tag?»

Il pannello mostra trasporto, versione del firmware, antenne collegate e i
contatori del lettore letti in italiano. I contatori distinguono tre guasti che
dall'esterno si somigliano tutti:

| Contatore | Cosa vuol dire | Dove si va a guardare |
|---|---|---|
| **timeout** | il lettore non ha risposto | cavo, porta, alimentazione |
| **errori di frame** | ha risposto male | rumore elettrico, cavo lungo o schermato male |
| **status rifiutati** | ha capito e ha detto di no | comando non supportato, parametro fuori intervallo |
| **nessun tag in campo** | non è un guasto | — |

Il confronto più utile è **antenne configurate contro antenne collegate**:
un'antenna che sta in `config.yaml` e non risulta collegata è la causa più
comune di «non legge», e da sola non dà nessun altro segno.

---

## 5. Registro delle misure — «cosa avevo misurato l'altra volta?»

Nessun archivio nuovo: adattamento, profilazione, campagna e prove di lettura
**finiscono già nel diario di prototipazione**
(vedi `docs/DIARIO_PROTOTIPAZIONE.md`). Mancava solo rileggerle in forma di
scheda, con data e condizioni, invece che come righe di log.

Ogni scheda porta con sé **le condizioni in cui è stata fatta**: una misura
senza le sue condizioni non si confronta con niente.

---

## Cosa resta da fare sull'hardware vero

1. Misurare le tre antenne in banda EU e in 860–960 MHz, e **scrivere il numero
   della risonanza**. È quello che va nella richiesta al fornitore, e finché non
   esiste la richiesta non si può scrivere (vedi la nota in `PIANO_PROGETTO.md`
   §2.1).
2. Profilare i tag veri e lasciare che la profilazione scriva la soglia
   misurata: `user_memory_bytes: 86` oggi in configurazione è un valore
   plausibile, non una misura.
3. Rifare la prova di lettura con la scatola piena e i flaconcini su liquido, che
   è il caso difficile vero.
4. Solo dopo, rivedere le soglie di `SealingPolicy`.
