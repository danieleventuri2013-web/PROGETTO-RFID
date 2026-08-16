# Schema dei dati sul tag — versione 1

Stato: prima definizione, 2026-08-15. Non ancora validata su hardware.
Pubblico: chi implementa la lettura nel **laboratorio destinatario**, chi mantiene
`src/lims/`, chi deve valutare la conformita' del trattamento dei dati.

Questo documento e' il contratto fra chi scrive il tag e chi lo legge. Un
contenitore per campione istologico deve arrivare a destinazione autosufficiente:
niente rete condivisa, niente database spedito a parte. Tutto cio' che serve per
riconoscere paziente, reperto e numerazione sta fisicamente nel tag.

---

## 1. Perche' i dati sono cifrati

Nome, cognome, codice fiscale e tipo di reperto sono dati sanitari, categoria
particolare ai sensi dell'art. 9 GDPR. Un tag UHF risponde a chiunque abbia un
lettore a qualche metro, senza contatto e senza lasciare traccia: in chiaro,
chiunque nel corridoio o durante il trasporto potrebbe fare l'inventario dei
pazienti passando accanto a una scatola.

La soluzione adottata tiene insieme le due esigenze:

| Cosa | Dove | Visibilita' |
|---|---|---|
| Pseudonimo del contenitore (laboratorio, accettazione, *n* di *N*) | banca EPC | in chiaro |
| Anagrafica e dati clinici | banca USER | cifrata (AES-256-GCM) |

Chi non ha la chiave vede solo numeri: puo' contare i contenitori di una
spedizione e accorgersi se ne manca uno, ma non sa a chi appartengono.

---

## 2. Banca EPC — lo pseudonimo (12 byte, in chiaro)

| Offset | Byte | Campo | Note |
|---|---|---|---|
| 0 | 1 | versione schema EPC | `0x01` |
| 1 | 2 | id laboratorio di origine | uint16 big-endian |
| 3 | 4 | id accettazione | uint32 big-endian |
| 7 | 1 | indice contenitore *n* | 1..255 |
| 8 | 1 | totale contenitori *N* | 1..255, *n* ≤ *N* |
| 9 | 3 | coda casuale | impedisce di indovinare gli EPC vicini |

Implementazione: `lims.codec.build_epc` / `lims.codec.parse_epc`.

La numerazione sta qui, e non solo nel payload, per una ragione operativa
precisa: **il controllo dei contenitori mancanti funziona senza la chiave**. Un
laboratorio che riceve una scatola puo' verificare la completezza con un solo
inventory, anche prima di aver concordato le chiavi.

Un EPC con un primo byte diverso da `0x01`, o di lunghezza diversa da 12 byte,
non appartiene a questo sistema: va segnalato come tag estraneo, non ignorato.

---

## 3. Banca USER — il payload sigillato

### 3.1 Struttura esterna

```
+--------+--------+--------+--------+---------------------------+----------------+
| byte 0 | byte 1 |    byte 2-3     |        ciphertext         |  tag GCM (16)  |
+--------+--------+--------+--------+---------------------------+----------------+
  magic    key_id    lunghezza del
  +schema  +revis.   testo in chiaro
```

- **byte 0**: nibble alto `0xA` (riconoscimento), nibble basso = versione sigillo (`1`)
- **byte 1**: nibble alto `key_id` (0..15), nibble basso `revision` (0..15)
- **byte 2-3**: lunghezza del testo in chiaro, uint16 big-endian

La lunghezza dichiarata non e' ridondante. La USER memory e' piu' grande del
payload, e il resto sono byte a zero: senza sapere dove finisce il payload quegli
zeri entrerebbero nel calcolo di autenticazione e ogni rilettura fallirebbe. E
non basterebbe tagliare gli zeri finali, perche' il tag GCM e' casuale e puo'
legittimamente terminare con uno zero.

Ingombro totale: **20 byte** di intestazione e autenticazione. Su un tag con
64 byte di USER memory restano **44 byte** di payload utile.

### 3.2 Cifratura

- Algoritmo: **AES-256-GCM**
- **Nonce**: `SHA256("RFID-LIMS-v1|nonce|" || EPC || revisione)[:12]` — non viene
  memorizzato, si ricalcola. L'EPC e' unico per contenitore, quindi i nonce non
  collidono; le riscritture dello stesso contenitore si distinguono per
  revisione, che **va incrementata a ogni riscrittura**.
- **Dati autenticati (AAD)**: `"RFID-LIMS-v1" | intestazione | EPC | TID`

Il **TID** e' l'identificativo di fabbrica del chip, non riscrivibile. Legandolo
al payload si ottiene una difesa concreta: un tag clonato copiando EPC e USER
memory su un chip vergine **non supera la verifica di autenticita'**. In una
catena di custodia di campioni istologici questo distingue uno scambio di
provetta da una semplice rilettura.

Il TID usato nell'AAD e' **sempre costituito dai primi 12 byte** del banco TID.
La lunghezza deve essere fissa e identica nei due laboratori: il destinatario non
ha modo di sapere quanti byte avesse letto il mittente. Ne segue un requisito
sull'hardware: **il tag deve avere un TID serializzato di almeno 12 byte**. Lo
verifica `python run.py tag-profile`.

### 3.3 Testo in chiaro (18 byte fissi + nome)

| Offset | Byte | Campo | Note |
|---|---|---|---|
| 0 | 1 | versione schema payload | `0x01` |
| 1 | 11 | codice fiscale | 16 caratteri impacchettati in base 36 |
| 12 | 2 | data di prelievo | giorni dal 2020-01-01; `0xFFFF` = non nota |
| 14 | 1 | codice materiale | codebook §4 |
| 15 | 1 | codice fissativo | codebook §4 |
| 16 | 1 | codice sede anatomica | codebook §4 |
| 17 | 1 | flag | vedi §3.4 |
| 18 | var | `COGNOME NOME` | ASCII maiuscolo, troncato allo spazio residuo |

Il payload **non ripete** accettazione e numerazione: sono gia' nell'EPC, e
l'AAD garantisce che EPC e payload appartengano allo stesso contenitore. Una
seconda copia sarebbe solo una possibile contraddizione senza arbitro. Per questo
`lims.codec.unpack_payload` richiede l'`EpcInfo` del tag di provenienza.

Il codice fiscale italiano e' di **16 caratteri alfanumerici**: in ASCII
occuperebbe 16 byte, ma le combinazioni possibili sono 36¹⁶, cioe' 83 bit.
Interpretandolo come intero in base 36 stanno in 11 byte. I 5 byte risparmiati
vanno al nome del paziente.

In scrittura il codice fiscale viene verificato anche nel **carattere di
controllo** (`lims.model.validate_codice_fiscale`): su un tag che viaggia senza
database di riscontro, un codice sbagliato non e' piu' correggibile a
destinazione.

Solo il nome viene troncato quando lo spazio non basta. Tutti i campi
identificativi strutturati sono preservati per intero, sempre.

### 3.4 Flag

| Bit | Nome | Significato |
|---|---|---|
| 0x01 | `URGENT` | esame urgente |
| 0x02 | `FROZEN` | campione congelato |
| 0x04 | `BIOBANK` | destinato a biobanca |
| 0x08 | `INFECTIOUS` | rischio biologico noto |
| 0x10 | `DECALCIFIED` | gia' decalcificato |
| 0x20 | `UNDER_VACUUM` | sottovuoto |

`INFECTIOUS` va mostrato **prima** che l'operatore apra il contenitore.

---

## 4. Codebook condivisi

Materiali, fissativi e sedi anatomiche viaggiano come codici numerici a un byte;
le descrizioni stanno in `lims.codec` (`MATERIALS`, `FIXATIVES`, `SITES`) con un
`CODEBOOK_VERSION`.

Regola di compatibilita': **aggiungere voci e' sempre ammesso, riassegnare un
codice gia' usato non lo e' mai** — cambierebbe il significato dei tag gia' in
circolazione, compresi quelli in transito.

---

## 5. Versionamento

Tre numeri di versione indipendenti, ciascuno nel primo byte del proprio blocco:

| Versione | Dove | Valore attuale |
|---|---|---|
| schema EPC | EPC byte 0 | `0x01` |
| schema sigillo | USER byte 0, nibble basso | `1` |
| schema payload | testo in chiaro byte 0 | `0x01` |

Un lettore che incontra una versione che non conosce **deve fallire in modo
esplicito**, mai interpretare i byte alla cieca. Fra organizzazioni diverse
l'unica compatibilita' possibile e' quella dichiarata.

---

## 6. Gestione delle chiavi

Senza rete fra i laboratori la chiave simmetrica va consegnata **una volta, fuori
banda**. E' il costo reale del requisito "i dati viaggiano solo nel tag", e va
messo in conto nella procedura, non nel software.

- Chiave: 32 byte casuali (AES-256), una per circuito di laboratori.
- `key_id` (0..15) viaggia in chiaro nell'intestazione: permette la rotazione
  senza rendere illeggibili i tag gia' emessi con la chiave precedente.
- Conservazione: file JSON separato dal database, con permessi ristretti
  (`lims.crypto.Keyring.save`). Su Windows `chmod` non riproduce le ACL: la
  protezione va garantita dai permessi della cartella.
- Le chiavi non compaiono mai nei log ne' nelle rappresentazioni testuali.

Il laboratorio destinatario distingue tre esiti diversi, con tre messaggi diversi:

| Esito | Significato |
|---|---|
| `chiave_mancante` | tag nostro, cifrato con una chiave che non abbiamo |
| `non_autenticato` | chiave giusta ma EPC o TID non corrispondono: possibile clone |
| `estraneo` | non e' un tag di questo sistema |

---

## 7. Protezione del tag

Dopo la scrittura il contenuto va reso non modificabile per errore:

1. **Password di accesso** nella banca RESERVED (word 2..3), al posto del valore
   di fabbrica `00000000` — `lims.tagio.TagIO.set_access_password`.
2. **Lock** di EPC e USER con il comando `0x25` — `TagIO.lock_container`.

Il lock da solo non basta: si sblocca con la password di accesso, quindi finche'
quella resta a zero chiunque puo' riscrivere il tag.

Le operazioni **permanenti** (`permalock`, `permaunlock`) non sono annullabili da
nessuna password e su nessun tag. Per questo il servizio le esegue solo con
`allow_permanent=true` esplicito. In laboratorio si usa il lock revocabile: un
contenitore puo' dover essere ri-etichettato.

---

## 8. Limite noto: lettura di piu' tag insieme

I comandi di lettura usano l'opzione `0x05`, che agisce sul **primo tag che
risponde**. Con piu' tag nel campo non si puo' scegliere quale interrogare,
perche' il filtro Select non e' implementato (Step 2 di `PIANO_PROGETTO.md`).

Conseguenza pratica: **il payload cifrato si legge solo a tag singolo**. Il
controllo di completezza della spedizione funziona comunque su tutta la scatola,
perche' si basa sull'EPC in chiaro.

Due strade per superarlo, da valutare dopo la prova su hardware:

- implementare il filtro Select per EPC (opzione `0x01`);
- usare l'**embedded read** (`META_TAG_DATA = 0x0080`, gia' parsato in
  `tags.py` ma mai richiesto): l'inventory restituirebbe la USER memory di tutti
  i tag in un colpo solo. Da verificare sul firmware.

---

## 9. Bilancio dello spazio

Per un tag con 64 byte (512 bit) di USER memory:

```
64 byte USER memory
 −  4 byte  intestazione in chiaro
 − 16 byte  tag di autenticazione GCM
 ─────────
 = 44 byte  payload utile
 − 18 byte  campi fissi
 ─────────
 = 26 byte  per "COGNOME NOME"
```

Il tutto entra in **una sola scrittura**, perche' il comando `0x24` accetta al
massimo 64 byte per volta. Oltre quella soglia `lims.tagio` spezza
automaticamente in blocchi.

| USER memory | Payload utile | Verdetto |
|---|---|---|
| assente | — | tag inutilizzabile: solo lo pseudonimo EPC |
| < 38 byte | < 18 byte | insufficiente anche per i soli campi fissi |
| 64 byte | 44 byte | **dimensione di riferimento** |
| ≥ 128 byte | ≥ 108 byte | spazio per un profilo esteso, scrittura a blocchi |

`python run.py tag-profile` misura la dimensione reale del tag e assegna il
verdetto, invece di dedurlo da una tabella di modelli.
