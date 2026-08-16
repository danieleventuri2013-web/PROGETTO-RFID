# Manuale utente — Programma di controllo lettore RFID SIM7200 (bozza)

Versione programma: 0.3.0 · Data: 2026-07-22 · Pubblico: operatori/utenti finali

---

## 1. Che cos'è questo programma

Il programma controlla un lettore RFID UHF **SIM7200** con **3 antenne**
disposte a formare un "parallelepipedo di lettura" (2 antenne orizzontali
affiancate sotto + 1 verticale di lato). Permette di:

- rilevare i tag RFID presenti nel volume (**Inventory**);
- leggere e scrivere dati nella memoria dei tag (**Read/Write**);
- vedere a schermo la potenza del segnale (RSSI) per antenna e la posizione
  stimata del tag nel volume 3D;
- salvare report di prova e report diagnostici per l'assistenza.

## 2. Requisiti

- PC Windows, Linux o macOS con **Python 3.10 o superiore**.
- Lettore SIM7200 su baseboard SLD1090, collegato via **USB** (il dispositivo
  compare come "HDSC") o via **cavo di rete** (IP di fabbrica `192.168.1.100`).
- Le 3 antenne collegate alle porte **1, 2, 3** del modulo.
- Un tag RFID UHF Gen2 per le prove.

## 3. Installazione

1. Installare Python da <https://www.python.org> (spuntare "Add to PATH").
2. Aprire un terminale nella cartella del programma ed eseguire:
   ```
   pip install -r requirements.txt
   ```
   Il launcher segnala le dipendenze mancanti senza modificare l'ambiente.
   In alternativa, autorizzarne esplicitamente l'installazione con
   `python run.py --install-deps`.

## 4. Avvio del programma

Doppio clic su **`run.bat`** (Windows) o eseguire `./run.sh` (Linux/macOS),
oppure da terminale:

```
python run.py            menu con le scelte qui sotto
python run.py gui        interfaccia grafica standalone
python run.py service-gui interfaccia grafica tramite service (consigliata)
python run.py step1      test automatico di lettura/scrittura
python run.py health     controllo rapido "sta tutto funzionando?"
python run.py tests      autotest interni (non serve il lettore)
```

Su Windows è anche possibile fare doppio clic su
**`avvia_test_grafico_rfid.bat`**. Il launcher apre la GUI e avvia dietro di essa
il processo service che sarà usato dal futuro framework.

## 5. Collegare il lettore

**Via USB (consigliato per iniziare):** collegare il cavo, annotare la porta
COM (Gestione Dispositivi → Porte COM e LPT, es. `COM3`). Nella GUI scegliere
"Seriale", premere **Aggiorna porte**, selezionare la COM rilevata e premere
**Connetti**. Il campo resta editabile per inserire manualmente una porta non
elencata.

**Via rete:** collegare il cavo Ethernet, impostare il PC sulla stessa rete
(es. IP `192.168.1.10`), nella GUI scegliere "TCP/IP", host `192.168.1.100`,
porta `8080`, premere **Connetti**.

Quando la connessione riesce compare il pallino verde "**● connesso**" e nel
log la versione del firmware. Il programma esegue da solo l'avvio del firmware
del lettore (obbligatorio a ogni accensione).

## 6. Usare l'interfaccia grafica

La finestra è divisa in quattro zone:

1. **Connessione** (in alto): scelta seriale/TCP e pulsante Connetti/Disconnetti.
2. **Parametri lettore e antenne**: regione (lasciare **EU** in Italia) e due
   cursori per antenna (potenza di lettura e scrittura, 5–30 dBm). Premere
   **"Applica regione + potenze"** per inviarli al lettore, **"Salva config"**
   per ricordarli ai prossimi avvii.
3. **Test lettura/scrittura**: i pulsanti operativi (vedi sotto).
4. **Grafici + Log**: a sinistra il segnale ricevuto per antenna, a destra il
   volume 3D con la posizione stimata dei tag; in basso il registro messaggi.

## 7. Cercare i tag (Inventory)

Selezionare le antenne desiderate, quindi usare:

- **Inventory singolo** per un solo ciclo;
- **Avvia inventario** per una sessione continua;
- **Avvia per durata** per fermarsi automaticamente dopo i secondi indicati;
- **Stop inventario** per interrompere una sessione continua.

La tabella aggrega le letture per EPC, antenna e RSSI. Il numero massimo di EPC
mantenuti in memoria è configurabile; gli EPC più vecchi vengono rimossi se il
limite viene superato.

## 8. Leggere i dati di un tag (Read)

Premere **Read USER**: legge le prime parole della memoria utente del tag,
provando ciascuna antenna. Nel log compare per ogni antenna `OK <dati>` o
`FAIL <motivo>`. Il campo "Parole da leggere" indica quante parole da 16 bit
leggere (di norma 2).

## 9. Scrivere dati su un tag (Write)

1. Scrivere nel campo **"Dati scrittura USER (hex)"** i dati in esadecimale
   (solo cifre 0–9 e lettere A–F, in numero **pari** — es. `12345678`).
2. Premere **Write USER**.
3. Premere **Read + Verify** per rileggere e confrontare: `match=True`
   significa che i dati sono stati scritti correttamente.

La "Password accesso" va toccata solo se i tag sono protetti (8 cifre
esadecimali; `00000000` = nessuna password).

> La scrittura agisce sul **primo tag che risponde**. La GUI la abilita solo
> dopo una sessione Inventory che abbia rilevato esattamente un EPC e richiede
> una conferma esplicita. Lasciare comunque nel volume soltanto il tag target.

### Cambiare l'EPC del tag

Usare soltanto un tag di prova o sacrificabile e seguire questa sequenza:

1. lasciare un solo tag nel volume ed eseguire **Inventory singolo**;
2. inserire il nuovo EPC esadecimale nel campo **Nuovo EPC (hex)** oppure premere
   **AUTO 96 bit** per generare un candidato casuale di 12 byte;
3. controllare attentamente vecchio e nuovo EPC, quindi premere **Scrivi EPC**;
4. confermare l'avviso: la GUI ripete un inventory di sicurezza, cambia l'EPC e
   prova automaticamente a rileggere il nuovo valore.

**AUTO non scrive nulla da solo.** Il service non riutilizza un candidato nella
stessa sessione e usa 96 bit casuali per default. Questo non costituisce una
garanzia di
unicità aziendale o globale. Il futuro framework dovrà registrare gli EPC già
assegnati e rifiutare duplicati. Se la verifica automatica non trova il nuovo
EPC, non ripetere subito la scrittura: identificare prima il tag e controllare il
log.

### Test Step 1 da terminale

`python run.py step1` esegue per default inventory e letture senza scrivere.
Le scritture vanno abilitate esplicitamente con `--write` o `--write-epc`
e vengono bloccate se l'inventory non rileva esattamente un EPC.

## 10. Salvare ed esportare i risultati

- Il test automatico (`python run.py step1`) salva un **report completo** in
  `logs/step1_<data_ora>.json` (leggibile con qualunque editor di testo).
- Ogni sessione scrive un **file di log** in `logs/` (`gui_...log`,
  `step1_...log`, `health_...log`).
- Il pulsante **"Report diagnostico"** della GUI salva
  `logs/diagnostica_gui_<data_ora>.json`.

## 11. Errori comuni e loro significato

| Messaggio nel log | Significato | Cosa fare |
|---|---|---|
| `0x0101: Unavailable command...` | Il lettore non è stato avviato (boot) | Disconnettere e riconnettere; il boot è automatico |
| `0x0400: No tag found` | Nessun tag nel campo antenna | Non è un guasto: avvicinare il tag, alzare la potenza |
| `0x0424: Memory locked` | Il tag è protetto da scrittura | Serve la password corretta del tag |
| `0x042B: Insufficient power` | Il tag riceve troppo poca energia per scrivere | Avvicinare il tag / alzare la potenza di scrittura |
| `0x0505: High return loss` | Problema antenna (cavo/connettore) | Controllare cavi e connettori; vedi anche §18 |
| `Transport timeout reading...` | Il lettore non risponde | Vedi §13 "il programma non si connette" |
| `CRC mismatch` / `Bad header` | Comunicazione disturbata | Controllare cavo/baud rate; riprovare |
| `Trasporto ... non aperto` | Operazione richiesta senza connessione | Premere prima Connetti |
| `valore esadecimale non valido` | Campo dati/password scritto male | Usare solo 0–9 e A–F, lunghezza pari |

## 12. Messaggi di avviso (non bloccanti)

- `Antenne non rilevate come connesse: [...]` — un'antenna configurata non
  risulta collegata: controllare il cavo su quella porta.
- `Resync: scartati N byte spuri` — la comunicazione ha avuto un disturbo ma
  il programma si è riallineato da solo; se compare spesso, controllare i cavi.
- `read_power ... supera 30 dBm` — potenza oltre il limite della versione
  standard (e dei limiti di legge EU): abbassarla.
- `nessun tag nel campo (non è un guasto)` — l'health check non ha trovato tag:
  normale se il volume è vuoto.

## 13. Il programma non parte o non si connette

**Non parte:** verificare Python (`python --version`, serve ≥ 3.10); eseguire
`pip install -r requirements.txt`; provare `python run.py tests` (se i test
passano, il software è integro e il problema è il collegamento al lettore).

**Non si connette (seriale):** controllare che la porta COM sia giusta e non
usata da altri programmi; scollegare/ricollegare l'USB; alimentazione del
lettore.

**Non si connette (rete):** `ping 192.168.1.100`; PC nella stessa sottorete;
porta `8080`; provare l'USB per escludere un problema di rete.

In tutti i casi: eseguire `python run.py health` e conservare il file
`logs/health_....json` per l'assistenza.

## 14. Un dato non viene salvato sul tag

1. Fare prima un **Inventory**: il tag viene visto? Se no, avvicinarlo o
   alzare la potenza.
2. Guardare il motivo del FAIL nel log (tabella §11): tag protetto
   (`0x0424`), energia insufficiente (`0x042B`), nessun tag (`0x0400`).
3. Provare **Read + Verify**: se `match=False` la scrittura non è andata a
   buon fine — ripetere con il tag più vicino all'antenna.
4. Ricordare: dati in esadecimale, lunghezza pari, massimo 64 byte.

## 15. Compare un errore non previsto

1. Annotare (o fotografare) il messaggio nel riquadro Log.
2. Premere **"Report diagnostico"** (o eseguire `python run.py health`).
3. Chiudere e riavviare il programma; se persiste, spegnere e riaccendere il
   lettore, poi riconnettere.
4. Inviare all'assistenza i file indicati al §16.

## 16. Generare un report tecnico per l'assistenza

Consegnare all'assistenza questi file dalla cartella `logs/`:

1. `diagnostica_gui_<data>.json` (pulsante **Report diagnostico**) **oppure**
   `health_<data>.json` (`python run.py health`);
2. il file di log della sessione con il problema (`gui_...log` o
   `step1_...log`);
3. se utile, il report `step1_<data>.json`.

Per un log più dettagliato (consigliato se il problema è di comunicazione),
avviare con l'opzione debug: `python src/app/gui.py --debug` — il log include
i dati grezzi scambiati con il lettore.

## 17. Domande frequenti

**Posso usare più tag insieme?** Sì per l'Inventory. Lettura e scrittura invece
agiscono sul primo tag che risponde: per operare su un tag preciso, tenerne
uno solo nel volume.

**Perché il tag si vede con un'antenna sola?** Normale: dipende da posizione e
orientamento del tag. Il test Step 1 serve proprio a mappare la copertura.

**Posso cambiare la regione radio?** In Italia deve restare **EU
(865–868 MHz)**. Le altre voci trasmettono fuori banda e non sono conformi.

**Che differenza c'è tra "Salva config" e "Applica"?** *Applica* invia i
parametri al lettore adesso; *Salva config* li memorizza nel file
`config.yaml` per i prossimi avvii.

**I numeri delle potenze cosa sono?** dBm (5–30). Nel file di configurazione
sono in centesimi di dBm (2000 = 20 dBm).

## 18. Buone pratiche d'uso

- Partire da **20 dBm** e alzare solo se necessario (limiti ETSI in EU).
- Lasciare la regione su **EU**.
- Un solo tag nel volume durante le scritture.
- Dopo ogni scrittura importante fare **Read + Verify**.
- Non scollegare l'USB durante un'operazione; usare prima **Disconnetti**.
- Ogni tanto svuotare la cartella `logs/` (conservando i report utili).
- Non salvare in `config.yaml` password di tag reali se il file viene
  condiviso o versionato.
- Nota hardware nota: le antenne in dotazione (SLP1027) sono ottimizzate per
  la banda USA; in banda EU la portata è ridotta — è una limitazione
  accettata del laboratorio, non un guasto.

## 19. Contatti e riferimenti assistenza *(da compilare)*

| Campo | Valore |
|---|---|
| Referente interno | ____________________________ |
| Telefono / e-mail | ____________________________ |
| Fornitore hardware | ____________________________ |
| Contratto / n. ordine | ____________________________ |
| Note | ____________________________ |
