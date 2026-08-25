# La postazione su tavoletta: comandare il lettore con le dita

Stato: 2026-08-25. L'interfaccia è provata a tutte le misure con Chrome; il
collegamento dalla tavoletta va provato sulla rete vera del laboratorio.

Una sola interfaccia, due postazioni. Non due programmi, non due copie del
codice: la stessa pagina che si adatta a chi la sta usando.

| | **Banco** | **Tavoletta** |
|---|---|---|
| Apparecchio | PC Windows, schermo da 15" in su | tavoletta Android da 10" in su |
| Comando | mouse e tastiera | solo dita |
| Dove sta | scrivania dell'accettazione | accanto alle antenne |
| Serve per | accettazione, impostazioni, archivio | scrittura, sigillo, ricezione |

Le funzioni sono le stesse e **nessuna schermata sparisce**: cambiano le misure
dei comandi e la disposizione delle colonne. Se domani accanto alle antenne
c'è solo la tavoletta, da lì si fa tutto — compreso tarare le potenze.

---

## 1. Come si sceglie la postazione

In ordine di precedenza:

1. **`?modo=tavoletta` nell'indirizzo.** È quello che finisce nel collegamento
   salvato sulla schermata Home: l'apparecchio si porta dietro la sua modalità
   e non dipende da cosa ha scelto qualcun altro.
2. **La scelta salvata su quell'apparecchio** (Impostazioni → Postazione).
   Resta anche dopo un riavvio del programma.
3. **Il tipo di puntatore.** Un apparecchio il cui puntatore più preciso è un
   dito parte già in modalità tavoletta.

Sul PC si può passare a «Tavoletta» per vedere com'è fatta, senza avere la
tavoletta sotto mano. Si torna indietro dallo stesso posto.

---

## 2. Farci arrivare la tavoletta

La tavoletta **non ha il lettore attaccato**: lo comanda attraverso il PC, sulla
rete locale. Servono tre cose, e vanno fatte una volta sola.

### 2.1 Mettere il programma in ascolto anche fuori dal PC

In `src/app/config.yaml`:

```yaml
webui:
  host: 0.0.0.0            # prima era 127.0.0.1
  port: 8770
  token: 'k7Qm-2xVb9La-Rt4Wz8Pn'   # generane uno a caso, lungo
```

**Le due righe vanno insieme.** `host: 0.0.0.0` significa «ascolta su tutte le
schede di rete»: da quel momento chiunque sia sulla stessa rete e conosca il
token può comandare il lettore. Il token fisso serve perché il collegamento
salvato sulla tavoletta contiene il token, e con un token nuovo a ogni avvio
smetterebbe di funzionare ogni mattina.

Questa è una decisione, non una comodità: va presa su una rete di laboratorio,
non sul Wi-Fi degli ospiti. Se il PC ha un firewall, la porta 8770 va aperta
per la rete privata.

### 2.2 Leggere l'indirizzo

All'avvio, `python run.py webui` stampa anche gli indirizzi per la tavoletta:

```
   Dalla tavoletta (stessa rete Wi-Fi), in Chrome:

   http://192.168.1.42:8770/?modo=tavoletta&t=k7Qm-2xVb9La-Rt4Wz8Pn
```

Se ce n'è più di uno (Wi-Fi, cavo, macchine virtuali), **il primo è quello
giusto quasi sempre**. Gli stessi indirizzi sono in Impostazioni → Postazione,
con un bottone per copiarli.

### 2.3 Installare il collegamento sulla tavoletta

Su Android, in **Chrome** (non nel browser del produttore):

1. aprire l'indirizzo;
2. menu ⋮ → **Aggiungi a schermata Home**;
3. aprirla dall'icona, non dal browser.

Aperta dall'icona, l'interfaccia parte a schermo pieno, senza barra degli
indirizzi: sessanta punti di altezza in più proprio dove l'altezza è la misura
scarsa. L'icona è il vetrino del marchio, viola sopra e rosa sotto.

> Non è un'app dal Play Store e non c'è niente da installare: è la stessa
> pagina, con un collegamento suo. Se il PC è spento, l'icona apre una pagina
> che non risponde — è normale, il lettore è attaccato al PC.

---

## 3. Cosa cambia davvero in modalità tavoletta

Non è «il banco con i bottoni più grandi». Cambiano tre cose fisiche, e da
quelle discende tutto il resto.

### 3.1 Lo spazio scarso è l'altezza

Un 10" in orizzontale dà circa **960×600 punti**: in larghezza ce n'è, in
altezza no. Al banco, sotto i 1100 punti di larghezza le colonne si
incolonnano — giusto su un portatile stretto, dove sotto c'è sempre altro
schermo. Sulla tavoletta, a colonna unica **la scena del banco di scrittura
riempirebbe da sola tutto lo schermo**, e il comando che scrive il tag
finirebbe sotto la piega.

In modalità tavoletta le colonne restano affiancate, la scena ha un tetto in
altezza e il conteggio del sigillo si misura sull'altezza dello schermo invece
che sulla larghezza.

### 3.2 Il dito non ha la punta

52 punti di lato come minimo, 64 per il comando che accende il trasmettitore.
È la misura di un polpastrello guantato, non una raccomandazione. In pratica:

- i **cursori delle potenze** passano da 16 a 48 punti di altezza, con il
  pomello grande come un dito — è la leva che si usa per tarare le antenne, e
  al banco è fatta per la punta di una freccia;
- il **selettore dell'operatore** passa da 27 a 46;
- i **passi del conteggio contenitori** (− e +) diventano 64×64;
- le **caselle di spunta** delle tabelle restano della loro misura, ma il
  bersaglio si allarga a tutta la cella.

### 3.3 Non c'è il passaggio del mouse

Niente si scopre avvicinandosi. Lo stato premuto sostituisce il passaggio del
mouse e dura quanto il dito; l'attesa dei 300 ms del doppio tocco è tolta, così
i comandi non sembrano in ritardo; il testo dei bottoni non si seleziona, così
un tocco tenuto un attimo di più — con i guanti succede — non apre il menu di
selezione del sistema sopra il comando.

### 3.4 Il verbo non scorre mai

È l'unico elemento pensato solo per la tavoletta.

Al banco di scrittura l'operatore tiene un contenitore in mano e guarda il
piatto, non lo schermo. Se il comando che accende il trasmettitore scorre via
con la colonna, va cercato: si posa il contenitore, si scorre, si riprende.
Tre gesti in più per ogni tag, moltiplicati per i contenitori di ogni
accettazione.

Perciò la pulsantiera del campione si incolla al fondo del suo cartellino e ci
resta. **Non è una barra nuova**: è la stessa che c'è al banco, che qui smette
di scorrere. Nessun bottone duplicato — un secondo «Scrivi il tag» sarebbe un
secondo modo di sbagliare.

### 3.5 Altre differenze minori

- Gli avvisi passano in alto al centro: in basso c'è il verbo, a destra la
  colonna che si sta leggendo.
- Aprendo la pagina **non** viene messo il fuoco sul codice fiscale: sulla
  tavoletta farebbe salire la tastiera di sistema, che copre mezzo schermo per
  un campo che nessuno ha ancora chiesto di compilare. Al banco resta, perché
  è quello che fa partire il lettore di codici a barre senza toccare niente.
- La tastiera di Android **restringe** la pagina invece di coprirla.
- Lo zoom con due dita resta permesso: è l'unico ingrandimento disponibile a
  chi non vede bene.

---

## 4. I caratteri su Android

Nessun font viene scaricato: il laboratorio è isolato dalla rete. Al banco le
tre facce sono Bahnschrift (numeri ed etichette), Segoe UI Variable (prosa) e
Consolas (EPC e TID) — tutte già presenti su Windows.

**Su Android nessuna di queste esiste.** Senza rimedio, tutto il registro da
strumento decadrebbe al Roboto di sistema, e le etichette maiuscole spaziate —
disegnate per una condensata — diventerebbero più larghe dello spazio che
hanno. Per questo le liste dei caratteri hanno una coda Android:
`sans-serif-condensed` è il nome con cui Chrome per Android espone Roboto
Condensed, e tiene in piedi il registro condensato. Al banco non cambia niente:
Bahnschrift viene prima ed esiste sempre.

---

## 5. Cosa conviene fare da dove

| Attività | Dove |
|---|---|
| Accettazione paziente | **banco** — è digitazione, e c'è il lettore di codici a barre |
| Scrittura dei tag | tavoletta o banco, indifferente |
| Sigillo e spedizione | **tavoletta** — si sta in piedi davanti alla scatola |
| Ricezione | **tavoletta** |
| Taratura potenze, campagna | **tavoletta**, perché si sta accanto alle antenne mentre si guarda l'effetto |
| Impostazioni, anagrafiche | **banco** — sono tabelle da compilare |
| Archivio e registro | **banco** |

Le schermate dense (impostazioni, archivio, registro) restano usabili sulla
tavoletta — le tabelle a sei colonne si scorrono di lato con un dito invece di
tagliare il testo — ma compilarle a dito è una fatica evitabile se un PC c'è.

---

## 6. Da verificare sull'apparecchio vero

Tutto quello che è scritto qui è stato provato con Chrome a 960×600, 1280×800 e
600×960, con puntatore grosso simulato. Restano da confermare sulla tavoletta
vera, alla prima prova con le antenne:

- che il collegamento salvato sulla schermata Home apra davvero a schermo pieno
  (dipende dalla versione di Android e da Chrome);
- che `sans-serif-condensed` esista su quel modello — se no le etichette
  restano leggibili ma perdono la condensata;
- la portata del Wi-Fi fra la tavoletta e il PC accanto alle antenne: il flusso
  degli eventi è una connessione aperta, e un Wi-Fi che cade a metà sigillo si
  vede come una scena che non avanza;
- se i guanti in uso funzionano sul touchscreen (il nitrile sottile sì, i
  guanti spessi no: non è un problema di software).
