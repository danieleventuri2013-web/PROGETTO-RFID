# La distinta stampata e il suo QR

**Questo documento è il contratto con il laboratorio che riceve.** Cambiare il
formato senza avvisarlo rompe la rilettura dei fogli già stampati e già partiti.
Vale la stessa regola di `docs/SCHEMA_DATI_TAG.md`.

---

## Perché sul foglio c'è tutto

I due laboratori **non condividono nessun archivio**. Quello che non viaggia col
pacco, a destinazione non esiste: né il nome del paziente, né la data del
prelievo, né la descrizione del campione.

Da qui due scelte:

* **la tabella è completa e leggibile a occhio.** È la copia autorevole. Se il
  lettore di codici non funziona, o la stampa è sbiadita, il foglio si legge lo
  stesso;
* **il QR porta le stesse identiche righe.** È una comodità — non ridigitare
  trenta righe all'arrivo — non un secondo documento.

Il foglio si stampa **solo a sigillo completo**: un documento che dichiara
contenitori non verificati sposterebbe il problema a destinazione, dove nessuno
può più controllare.

> **Contiene dati sanitari in chiaro**, QR compreso. È la conseguenza diretta di
> non avere un archivio condiviso, ed è una scelta consapevole: il foglio va
> nella busta agganciata alla scatola, non incollato all'esterno.

## Le colonne

Nell'ordine, che è **parte del formato**:

| # | chiave | cosa |
|---|---|---|
| 1 | `codice_fiscale` | identificativo del paziente |
| 2 | `cognome` | |
| 3 | `nome` | |
| 4 | `sesso` | `M`/`F`/`X`; se manca si deduce dal codice fiscale |
| 5 | `data_nascita` | `AAAAMMGG`, vuoto se sull'etichetta non c'era |
| 6 | `data_prelievo` | `AAAAMMGG`, dall'etichetta del reparto |
| 7 | `ora_prelievo` | `HHMM` |
| 8 | `descrizione` | il campione, come l'ha scritto il reparto |
| 9 | `materiale` | dal codebook (`lims.codec`) |
| 10 | `fissativo` | dal codebook |
| 11 | `sede` | dal codebook |
| 12 | `etichetta` | contenitore *i/N* |
| 13 | `epc` | 24 caratteri esadecimali |

Sul foglio ogni campione occupa **due righe**: sopra chi è, sotto cosa è.
Tredici colonne piene non stanno su un A4 in verticale — un EPC da 24 caratteri
finirebbe incolonnato uno per riga. Nessun dato viene lasciato fuori.

## Il formato del QR

```
RFQ1:1/1:A1B2C3D4:<payload base45>
└──┬─┘ └┬┘ └───┬──┘
   │    │      └─ identificativo, 8 caratteri esadecimali: lega fra loro le
   │    │         parti dello stesso codice
   │    └─ parte / totale (una cifra ciascuna: al massimo 9 parti)
   └─ firma del formato e sua versione
```

L'intestazione ha **lunghezza fissa** (18 caratteri) e si legge tagliandola a
misura: dentro non c'è nessun separatore univoco, perché ogni carattere della
modalità alfanumerica del QR è anche un carattere valido della Base45.

Dentro il payload, prima della Base45:

```
[ firma HMAC-SHA256 troncata, 8 byte ] [ zlib( tabella UTF-8 ) ]
```

La tabella usa `\x1f` fra i campi e `\x1e` fra le righe: sono caratteri di
controllo, quindi non possono comparire nei dati e non serve nessuna sequenza di
fuga — che è il posto in cui questi formati si rompono sempre. Gli accenti
restano dove sono: dentro il blocco compresso ci stanno, e un cognome storpiato
sulla distinta è un cognome sbagliato.

## Perché Base45 e perché comprimere

Un QR ha una **modalità alfanumerica** che usa 5,5 bit per carattere invece
degli 8 della modalità byte, ma accetta solo 45 simboli: cifre, lettere
maiuscole e nove segni di interpunzione. Base45 (RFC 9285) esiste esattamente
per quello — è la codifica dei certificati COVID europei, nata dallo stesso
problema.

Misurato su **trenta pazienti diversi** (non su una tabella ripetitiva, che
comprimerebbe troppo bene e direbbe una bugia):

| passaggio | dimensione |
|---|---|
| tabella in chiaro | 3.352 caratteri (111 per riga) |
| compressa con `zlib` | 1.493 byte — il 45% |
| in Base45 | 2.240 caratteri |
| **QR a correzione M** | **versione 33, 149×149 moduli, ~39 mm di lato** |

Senza comprimere servirebbe la **versione 40 al livello di correzione più
debole** (L), cioè il simbolo più grande possibile con la ridondanza minima: su
carta comune è un codice che non si legge. La compressione è ciò che rende il
foglio stampabile.

Le capienze della norma, per riferimento (caratteri alfanumerici):

| correzione | versione 40 |
|---|---|
| L (7%) | 4.296 |
| M (15%) | 3.391 |
| Q (25%) | 2.420 |
| H (30%) | 1.852 |

## Più di un codice

Oltre i 2.200 caratteri di payload il codice si spezza da solo in parti
(`1/2`, `2/2`), stampate affiancate. Il taglio avviene su multipli di tre
caratteri, così ogni pezzo resta Base45 valido per conto suo.

In ricezione si leggono uno dopo l'altro: la schermata dice quali mancano
(«manca la parte 2 su 2: leggere anche quel codice») e ricompone da sola. **Non
si accetta una distinta letta a metà**: sarebbe peggio di una non letta, perché
sembra completa.

Oltre nove parti il programma si rifiuta: quella non è più una scatola, sono più
spedizioni.

## La firma

HMAC-SHA256 troncato a 8 byte, con la chiave del circuito (`lims.crypto`, la
stessa della distinta cifrata). Serve a scoprire un QR rifatto o corrotto, non a
resistere a un attacco con mesi di calcolo.

Se il portachiavi è vuoto il QR viaggia **senza** firma, e chi lo rilegge lo
scopre (`firma_verificata: null`): meglio una distinta non firmata che nessuna
distinta, purché non si spacci per firmata.

## Il lettore

Un lettore di codici a barre **2D con emulazione tastiera**: si comporta come
una tastiera, digita il contenuto e chiude con Invio. Nessun driver, nessuna
fotocamera, nessuna app. È lo stesso genere di apparecchio che già legge il
codice fiscale in accettazione.

Un dettaglio che conta: **lo spazio è un carattere valido della Base45** (è il
36° dell'alfabeto), e un testo può cominciare con uno. Chi lo ripulisse coi
metodi soliti corromperebbe il contenuto in silenzio, che è il modo peggiore di
sbagliare. Si tolgono solo ritorno a capo e tabulazione, che sono ciò che un
lettore aggiunge come terminatore.

## Il codice

* `src/lims/qr.py` — codificatore QR in Python puro, nessuna dipendenza (stessa
  ragione di `webui/icone.py`: il laboratorio è isolato dalla rete).
* `src/lims/base45.py` — RFC 9285.
* `src/lims/tabella.py` — le colonne, la codifica, la rilettura.
* `src/tests/test_lims_qr.py` — verifica le **sedici capienze pubblicate** nella
  norma e rilegge i simboli prodotti smontandoli. Se disposizione, maschera o
  intreccio fossero sbagliati, il testo non tornerebbe.
