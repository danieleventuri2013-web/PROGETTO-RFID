# Posizionamento, nome e identità visiva

Documento di lavoro per la proposta a RIXLAB. Redatto il 16 agosto 2026.
La versione presentabile è l'Artifact collegato in fondo.

---

## 1. Cosa esiste già

Sì, prodotti di tracciabilità campioni esistono, e alcuni sono di aziende molto grandi.
Ma sono **quasi tutti la stessa cosa**, e non è la nostra.

### Gli incumbent internazionali

| Prodotto | Azienda | Cosa fa |
|---|---|---|
| **CEREBRO** | Leica Biosystems | Identificazione, tracciamento e workflow del campione *dentro* il laboratorio, con catena di custodia documentata |
| **VANTAGE** | Roche / Ventana | Workflow di anatomia patologica integrato col LIS |
| Specimen tracking | General Data Company | Verifica dell'identità di contenitore, cassetta e vetrino a ogni passo |
| **Specimen Track / Lab Track** | GAO RFID | Tracciamento RFID di dati, posizione e storia del campione |
| Specimen Tracking | SonitorONE | Catena di custodia automatizzata via RTLS |
| LIS con RFID | LigoLab | Sistema informativo di laboratorio con barcode e RFID |
| Histology block tracking | AssetPulse | RFID sui blocchetti di istologia |

### In Italia

| Prodotto | Azienda |
|---|---|
| **SpyTrace** | AHSI — sistema di tracciabilità dei campioni |
| **TDHistoCyto** | Technidata — software per anatomia patologica |

Implementazioni RFID già in esercizio al **Policlinico Campus Bio-Medico** di Roma e
all'**Istituto Nazionale dei Tumori** di Milano. Uno studio della **Mayo Clinic** documenta
RFID passivi a 13,56 MHz su biopsie gastrointestinali.

### La cosa importante

**Tutti tracciano *dentro* il laboratorio, e tutti sono legati al LIS.** Il tag o il codice
a barre è un *puntatore* a un database: fuori dalla rete che ospita quel database,
l'etichetta non dice più niente.

Nessuno di questi prodotti risolve il problema che abbiamo risolto noi:

> **certificare cosa c'è dentro una scatola chiusa nel momento in cui la si chiude, e farla
> arrivare a un altro laboratorio che può leggerla senza condividere alcuna rete.**

Quello è il buco di mercato, ed è dove sta il progetto.

---

## 2. Posizionamento

**In una frase:**

> Non è tracciabilità dei campioni. È **certificazione del trasporto fra laboratori**.

**I tre argomenti che lo reggono**, in ordine di forza:

1. **I dati viaggiano nel tag, non in un database.**
   Anagrafica del paziente, reperto e numerazione stanno cifrati *dentro* il chip, legati
   al numero di serie di fabbrica del chip stesso. Il laboratorio destinatario legge la
   scatola ancora chiusa e sa cosa è arrivato, **senza rete condivisa, senza VPN, senza
   integrazione fra i due LIS**. È la differenza fra spedire un campione e spedire un
   campione che sa dire chi è.

2. **Il sigillo certifica un insieme chiuso.**
   Alla chiusura della scatola il sistema non «scopre» cosa c'è: **verifica** un elenco
   noto, con passate multiple che variano potenza, antenne e parametri radio, e si ferma
   solo con un criterio dichiarato. O li trova tutti, o dice **quali** mancano, con nome e
   paziente. Non esiste una schermata verde con un contenitore mancante.

3. **Privacy per costruzione, non per adempimento.**
   EPC pseudonimo in chiaro, payload cifrato AES-256-GCM legato al TID del chip: un tag
   clonato su un altro chip **non supera l'autenticazione**. Distinta di spedizione cifrata.
   Nessun dato di paziente in chiaro, mai — né sul tag, né nell'email di trasmissione.
   È *privacy by design* ex art. 25 GDPR, ed è un argomento di vendita prima che un
   adempimento.

**Argomenti di supporto:** driver nativo multipiattaforma senza dipendenza dalle DLL del
produttore; interfaccia che istruisce l'operatore con la scena animata del banco; archivio
che risponde in una riga alla domanda «cosa è stato spedito, dove, quando, chi ha
supervisionato».

---

## 3. Il nome

### Metodo

Ho fatto una **verifica preliminare** su ogni candidato: ricerca web mirata su settore
medicale, software di laboratorio e prodotti concorrenti. Ha già scartato due nomi che
sembravano ottimi, quindi è servita.

> **Non è una ricerca di anteriorità.** Quella la fa un mandatario marchi sulle banche dati
> ufficiali. Io posso escludere le collisioni evidenti, non garantire che un nome sia libero.
> Le istruzioni per la verifica vera sono al §5.

### Scartati, con il motivo

| Nome | Perché no |
|---|---|
| **TESSERA** | Collide con la **Tessera Sanitaria** e il Sistema TS. In sanità italiana è inusabile, anche se l'idea (la *tessera hospitalis*, il gettone spezzato le cui metà devono combaciare) era perfetta. |
| **SIGILLUM** | **LEAF srls** ha già «Sigillum»: smart packaging con **tag NFC non clonabili e crittografia asimmetrica** per l'autenticità del prodotto. È quasi il nostro stesso concetto. Conflitto diretto. Esistono anche *Sigillum Sign* (firma digitale) e un gestionale notarile omonimo. |
| **CAPSA** | Esiste già **Colasoft Capsa**, analizzatore di rete. Settore diverso, ma un software con quel nome c'è. |
| **CEREBRO**, **VANTAGE** | Occupati dagli incumbent (Leica, Roche). |
| Qualunque `*Track` / `*Trace` | Zona affollatissima: SpyTrace, Specimen Track, Lab Track, TrackCore. Un nome così ci mette **dentro** la categoria sbagliata, che è esattamente quello che non vogliamo. |
| **INTEGRA** | Integra LifeSciences è un grande gruppo del medicale. |

### Candidati che passano la verifica preliminare

#### Direzione A — il passaggio di consegne

**1. TESTIMONE** ← *la mia prima scelta*

Il testimone è insieme **l'oggetto che passa di mano nella staffetta** e **chi attesta**.
Le due cose che fa il prodotto, in una parola sola: accompagna il campione da un laboratorio
all'altro, e certifica.

- Parola italiana comune usata in un campo del tutto estraneo (software) → **marchio forte**,
  come «Apple» per i computer: arbitrario, quindi distintivo e difendibile.
- Registro serio, adatto al contesto clinico.
- Nessuna collisione trovata in ambito software o medicale.
- *Contro:* quattro sillabe, e all'estero non si capisce da sola.

**2. STAFFETTA**

La corsa a squadre: il campione passa da un laboratorio all'altro e **niente cade**.

- Stessa logica di marchio arbitrario, stessa forza.
- Nessuna collisione trovata.
- *Contro:* registro sportivo, forse troppo leggero per l'anatomia patologica.

#### Direzione B — il plico sigillato

**3. PLICO** ← *la scelta se conta la spendibilità internazionale*

Il **plico sigillato** è già un concetto italiano di integrità e custodia: si usa in ambito
giuridico e amministrativo per l'involucro chiuso il cui contenuto è garantito.

- **Due sillabe**, facilissimo da pronunciare in qualunque lingua.
- Dice esattamente cosa fa il prodotto senza essere descrittivo del software.
- Nessuna collisione trovata in ambito medicale o di laboratorio.
- *Contro:* in Italia è parola d'uso comune; fuori d'Italia non significa niente — il che
  può essere un vantaggio (nome puro) o uno svantaggio (nessuna evocazione).

#### Direzione C — dal mondo dell'istologia

**4. VIRAGGIO**

Il viraggio è il cambio di colore che **rivela** ciò che l'occhio non vede — esattamente il
mestiere dell'ematossilina e dell'eosina, e la metafora su cui è costruita tutta l'identità
visiva del programma.

- Coerente al 100% con il sistema di design già realizzato.
- *Contro:* lungo, e fuori dal laboratorio non dice nulla.

### La raccomandazione

**TESTIMONE** se il mercato è italiano e conta la serietà del registro.
**PLICO** se RIXLAB pensa all'export o vuole un nome corto da mettere ovunque.

Entrambi vanno verificati (§5) prima di investirci un euro.

---

## 4. Identità visiva

Il programma ha già un sistema di design coerente, e il marchio deve nascere da lì invece di
essere appiccicato sopra.

**La tavolozza viene dall'istologia**: ematossilina `#3B2A63` ed eosina `#D9536B`, i due
coloranti con cui è colorato ogni vetrino al mondo. Non è decorazione a tema: c'è un legame
che regge il progetto — *l'eosina rende visibile il tessuto invisibile, il campo RF rende
visibile il tag invisibile*. Nell'interfaccia l'eosina è riservata **solo** all'attività
radio, e un'occhiata allo schermo dice se il lettore sta parlando con un tag.

### Tre concept

**A — L'insieme chiuso** *(preferito)*

Un rettangolo con angoli arrotondati — la scatola — e dentro una griglia di punti, tutti
pieni. Uno dei punti è in eosina: quello che il sistema sta leggendo in questo momento.
È **letteralmente la schermata del sigillo** già realizzata nel programma, ridotta a segno.

Perché funziona: dice *insieme completo* e *verifica in corso* insieme, si legge a 16 px,
funziona in monocromatico, e nasce dal prodotto invece di illustrarlo.

**B — Le due metà che combaciano**

Due semicerchi separati da una fessura verticale, che insieme formano un cerchio: la *tessera
hospitalis*, il gettone spezzato le cui metà devono combaciare per provare l'identità.
Il laboratorio che spedisce e quello che riceve. Elegante ma più astratto: richiede
spiegazione.

**C — Il testimone**

Un cilindro inclinato — il testimone della staffetta — con una banda in eosina. Diretto, ma
funziona solo se il nome scelto è TESTIMONE, e rischia di sembrare sportivo.

### Tipografia

**Bahnschrift** per il logotipo: il grottesco condensato derivato dal DIN, già usato nel
programma per i numeri grandi e le etichette. Registro da pannello strumenti, non da app di
consumo. È già installato su ogni Windows, quindi nessun costo di licenza e nessun font da
distribuire.

---

## 5. Come fare la verifica vera sul nome

Prima di stampare qualsiasi cosa. Sono tutte gratuite e le fai in un'ora.

1. **TMview** — <https://www.tmdn.org/tmview/> — la banca dati che aggrega EUIPO, tutti gli
   uffici nazionali UE e molti extra-UE. È il primo posto dove guardare.
2. **UIBM** — <https://uibm.mise.gov.it/> — marchi italiani.
3. **EUIPO eSearch plus** — <https://euipo.europa.eu/eSearch/> — marchi dell'Unione Europea.
4. **WIPO Global Brand Database** — <https://branddb.wipo.int/> — copertura mondiale.
5. **Registro Imprese** — che non esista una società con quella denominazione nel settore.
6. **Domini** `.it`, `.eu`, `.com`.

**Classi di Nizza da controllare:** **classe 9** (software) e **classe 42** (servizi di
sviluppo software). Se il prodotto venisse venduto insieme all'hardware, anche la
**classe 10** (apparecchi medicali).

**Poi fatti fare una ricerca di anteriorità da un mandatario marchi.** Costa qualche
centinaio di euro e copre anche i marchi *simili*, non solo quelli identici — che è dove
nascono i problemi veri.

### Una trappola da conoscere

**MEDITECH** è anche il nome di *Medical Information Technology, Inc.*, uno dei maggiori
produttori mondiali di cartella clinica elettronica. Qualunque nome di prodotto che le
somigli è da escludere in partenza.

---

## 6. Fonti

- [Leica Biosystems CEREBRO](https://www.leicabiosystems.com/us/tracking-and-workflow/cerebro/)
- [Roche / Ventana VANTAGE](https://www.medical-xprt.com/software/vantage-workflow-software-778617)
- [General Data Company — Lab Specimen Tracking](https://www.general-data.com/solutions/healthcare/histology-lab-sciences/lab-specimen-tracking)
- [GAO RFID — Lab Specimen Tracking](https://gaorfid.com/lab-specimen-track/)
- [SonitorONE — Specimen Tracking](https://sonitor.com/solutions/specimen-tracking/)
- [LigoLab — Pathology Specimen Tracking](https://www.ligolab.com/post/how-pathology-specimen-tracking-system-software-is-reducing-lab-errors-enhancing-patient-safety)
- [AssetPulse — Histology Block Tracking](https://www.assetpulse.com/blog/histology-block-tracking/)
- [AHSI SpyTrace](https://www.ahsi.it/en/prodotto/spytrace-sistema-tracciabilita-dei-campioni)
- [Technidata TDHistoCyto](https://www.technidata-web.com/it/soluzioni/disciplines/anatomia-patologica)
- [Campus Bio-Medico — tracciabilità e digital pathology](https://www.policlinicocampusbiomedico.it/news/tracciabilita-e-digital-pathology-rivoluzionano-la-diagnostica-patologica)
- [Studio Mayo Clinic — RFID su biopsie prostatiche](https://www.sciencedirect.com/science/article/abs/pii/S1092913413000294)
- [LEAF srls — Sigillum smart packaging](https://leafsrls.com/projects/sigillum/) *(collisione)*
- [Sistema Tessera Sanitaria](https://sistemats1.sanita.finanze.it/portale/it/tessera-sanitaria) *(collisione)*
- [EUIPO — disponibilità del marchio](https://www.euipo.europa.eu/it/trade-marks/before-applying/availability)
- [Classi di Nizza](https://www.jacobacci.com/pubblicazioni/classi-di-nizza-marchio)
