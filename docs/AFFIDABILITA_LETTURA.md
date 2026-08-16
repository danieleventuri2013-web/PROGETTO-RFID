# Affidabilità di lettura: leggere tutto quello che c'è, e solo quello

Stato: 2026-08-15. Progettato e testato senza hardware; le soglie vanno tarate
sulla campagna di misura reale.

Questo documento raccoglie il ragionamento dietro l'obiettivo principale del
progetto:

> Essere sicuri con probabilità prossima al 100% che il lettore veda esattamente
> ciò che c'è nella scatola al momento della chiusura.

---

## 1. Due errori opposti

Il requisito ha due facce, e una soluzione che ne cura una sola non serve.

| Errore | Cosa succede | Difesa principale |
|---|---|---|
| **Falso negativo** | un contenitore c'è ma non viene letto | passate ripetute che cambiano le condizioni radio |
| **Falso positivo** | un contenitore non c'è ma viene contato | verifica a insieme chiuso contro la distinta |

Il falso positivo è il più insidioso perché non si manifesta: il conteggio torna,
e nessuno sospetta niente. A 30 dBm il lettore vede tag a un paio di metri, quindi
un vassoio di un'altra spedizione appoggiato lì accanto entra nel conteggio senza
dare segno.

---

## 2. Verificare, non scoprire

La scelta di fondo di `lims.sealing` è che il sigillo **non è una scoperta**:
sappiamo esattamente quali EPC devono esserci, dalla distinta in `shipment_items`.

Cercare 34 EPC noti è un problema molto più facile che elencare «tutto quello che
c'è», e soprattutto permette un criterio di arresto onesto: o si trovano tutti, o
si dice quali mancano. **Il sistema non dichiara mai completo un insieme che non
lo è** — è la promessa da cui dipende tutto il resto, e ha un test dedicato
(`test_non_dichiara_mai_completo_un_insieme_incompleto`).

Da qui discende anche la difesa contro i falsi positivi: un EPC che non è sulla
distinta finisce in «fuori distinta», mai nel conteggio. È gratuita e non dipende
dalla radio.

---

## 3. Decorrelare i fallimenti

Un tag perso in una configurazione si ritrova spesso in un'altra. Ripetere venti
volte la stessa passata identica aggiunge molto meno di quattro passate diverse.
Ogni passata cambia una condizione, a rotazione:

| Leva | Perché aiuta |
|---|---|
| **Potenza** (20 → 25 → 29 dBm) | a piena potenza i tag vicini saturano il ricevitore e mascherano quelli lontani: una passata debole a volte trova ciò che una forte perde |
| **Sottoinsiemi di antenne** | cambia la geometria del campo, e dice *quale* antenna vede un tag |
| **Session Gen2** | in S0 il flag di inventario decade subito e i tag forti continuano a rispondere affamando i deboli; S2 lo mantiene |
| **Target dinamico A→B** | il modulo ribalta da solo il target: ogni tag risponde una volta per passata |
| **RF MODE `0x71`** | −93 dBm contro i −88 del default: **5 dB di sensibilità in più**, al prezzo del throughput |
| **Modalità tag densi `0xAA58`** | il modulo gestisce internamente collisioni e tempi; il manuale la descrive per «tag impilati e difficili da leggere» |

Le prime cinque si combinano; la modalità densa no — in quel modo sessione,
target, Q e RF mode **non sono impostabili**, quindi è una *strategia
alternativa* da confrontare, non un'aggiunta.

---

## 4. Criterio di arresto

Il sigillo è valido solo se valgono tutte insieme:

1. `trovati == attesi`;
2. l'insieme è rimasto invariato per *K* passate consecutive (default 2);
3. l'accumulatore non ha scartato EPC per limite di capienza (`evicted_epcs == 0`);
4. nessun errore di lettura.

La terza non è pedanteria: se l'accumulatore avesse buttato via EPC per capienza,
l'insieme «trovato» sarebbe incompleto senza che nessuno se ne accorga.

Altrimenti si continua fino al limite di passate o di tempo, e si dichiara
**esattamente quali mancano**, con il loro tasso di rilevamento.

---

## 5. Confinamento spaziale

Tre difese contro il conteggio di ciò che sta fuori, in ordine di robustezza:

1. **Insieme chiuso** — vedi §2. Non dipende dalla radio, e da sola risolve il
   caso più frequente.
2. **Consenso fra antenne** (`SealingPolicy.min_antennas`) — un tag dentro il
   volume è visto da più antenne, uno appoggiato fuori tipicamente da una sola.
3. **Potenza** — è la leva che definisce fisicamente il volume di lettura, e va
   tarata (§6), non scelta.

**L'RSSI da solo non è un discriminante affidabile** e non va usato come criterio
di accettazione: un tag appena fuori, ben orientato, può essere più forte di uno
dentro immerso nel liquido. Entra nella diagnosi, non nella decisione.

### Limite del montaggio attuale

Nel prototipo le tre antenne sono **complanari a pavimento**: due affiancate in
lettura con il contenitore sopra, la terza poco distante per la scrittura. Questa
disposizione non dà discriminazione in altezza, quindi il consenso fra antenne
distingue meno bene un tag dentro il volume da uno appoggiato accanto. Con la
terza antenna in verticale il criterio diventerebbe più netto — e la geometria
tornerebbe quella del «parallelepipedo» previsto in `PIANO_PROGETTO.md`.

---

## 6. La campagna di misura

`python run.py campaign`. Non misura «quanto si legge»: misura **due tassi
insieme**.

Procedura:

1. si carica il contenitore con i campioni veri, chiuso come in esercizio;
2. si posizionano di proposito alcuni **tag di controllo fuori**, alle distanze
   che si vogliono escludere — sul banco accanto, sotto il piano, nella scatola
   successiva;
3. si spazza la griglia potenza × sessione × RF mode × antenne;
4. per ogni configurazione si misura il *tasso di rilevamento dentro* e il *tasso
   di fuga fuori*.

La configurazione consigliata è la **potenza più bassa che legge tutto il
contenuto senza leggere niente di esterno**. Non quella che legge di più: quella
è anche la configurazione che legge il tavolo accanto. Fra le configurazioni
valide si sceglie la potenza minima perché lascia più margine di rumore, definisce
un volume più netto e scalda meno.

Il report va archiviato: serve come taratura, come evidenza al collaudo dal
cliente, e come termine di paragone quando qualcosa cambia.

**Prima di misurare** il comando toglie di mezzo le impostazioni che falserebbero
il risultato: modalità di risparmio energetico (`0x98`, che spegne la radio fra un
comando e l'altro) e filtro RSSI ereditato (`0xAA5B`, che scarterebbe proprio i
tag deboli che la campagna deve trovare).

---

## 7. Il vincolo fisico che precede il software

Le antenne SLP1027 sono a **polarizzazione circolare**, axial ratio < 3 dB: è
esattamente la proprietà che rende la lettura indipendente dall'orientamento dei
tag, e quindi la premessa del «100% con campioni in verticale».

Ma le curve del datasheet **partono da 900 MHz** e la banda EU è 865–868. L'axial
ratio peggiora monotonicamente scendendo di frequenza — 0,61 dB a 928 MHz,
1,56 dB a 902, ancora in salita ripida al bordo sinistro del grafico. Fuori banda
l'antenna diventa progressivamente ellittica, cioè perde proprio la
caratteristica su cui poggia l'obiettivo.

L'azione fornitore aperta in `PIANO_PROGETTO.md` §2.1 (antenna versione EU o
broadband 860–960 MHz) non è quindi un miglioramento: è un **prerequisito**.

Il comando `0xAA4A` (`RFIDService.antenna_diagnostics`) misura il return loss
frequenza per frequenza e calcola il VSWR. Il manuale indica VSWR < 7 come soglia
di accettabilità. È lo strumento che permette di portare al fornitore un dato
invece di un'ipotesi — e in seguito diventa un controllo periodico di salute
dell'impianto.

---

## 8. Cosa resta da misurare sull'hardware

Nell'ordine:

1. `0x67`/`0x71` — regione e SKU del modulo: conferma che `0xAA58` (dichiarata
   per CE, non FCC) sia davvero disponibile su questa unità;
2. `0xAA4A` sulle antenne alle frequenze EU — **il dato da portare al fornitore**;
3. `run.py tag-profile` sui tag reali: modello, USER memory, TID serializzato;
4. `run.py campaign` con i tag di controllo esterni, per scegliere la
   configurazione con i numeri;
5. da lì escono le soglie di `SealingPolicy` (`min_antennas`,
   `min_detection_rate`, `stable_passes`) e quelle di salute dei tag, che oggi
   sono valori di partenza prudenti e **non** misure.

## 9. Nota di onestà

Il 100% con lettura statica non è garantibile per costruzione: dipende da come i
contenitori sono disposti, da quanto liquido c'è e da come i tag si schermano.

Quello che si può garantire è diverso e altrettanto utile: **non dichiarare mai
completo un insieme che non lo è.** Se la campagna mostrasse che con 50
contenitori il tasso di rilevamento resta sotto la soglia, le leve residue sono
fisiche — antenna EU, distanziatori, disposizione, lettura a gruppi — non
software, e la campagna serve proprio a stabilirlo con i numeri.
