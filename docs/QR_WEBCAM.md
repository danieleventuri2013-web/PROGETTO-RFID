# Prova QR della distinta con la webcam

## Nella Ricezione della WebUI

Apri la WebUI sul PC con `python run.py webui`, seleziona l'operatore e vai in
**Ricezione → Attiva webcam**. Consenti l'accesso alla fotocamera nel browser.
Inquadra il foglio: il bordo verde segnala un QR leggibile. Premi **Acquisisci**
o **Spazio** per caricarlo. I codici multiparte vengono raccolti senza duplicati;
l'elenco dei campioni cambia solo quando tutte le parti sono state acquisite.
**Nuova scansione** prepara la raccolta di un altro foglio.

I dati estratti sono visibili nella tabella espandibile e nell'elenco dei campioni
attesi. **Leggi la scatola** confronta gli EPC RFID con quelli del QR, segnalando
mancanti ed estranei. Il QR RFQ1 non contiene l'identità completa della spedizione:
per confermare la ricezione ed esportare il verbale serve la distinta cifrata
selezionata. Se il foglio coincide con il file già selezionato, resta collegato;
se è diverso, la vecchia ricezione viene deselezionata.

La webcam si spegne con **Spegni webcam**, al completamento dell'acquisizione,
uscendo dalla Ricezione o passando a un'altra scheda del browser. L'anteprima
è gestita dal browser; il server locale decodifica i fotogrammi in memoria, senza
salvarli o registrarli nel diario. Le dipendenze sono in `requirements-qr.txt`.
Per questa funzione servono Pillow e ZXing-C++; OpenCV serve alla prova autonoma.

Usa l'indirizzo `http://127.0.0.1:8770/` completo del token generato all'avvio
(oppure localhost). I browser richiedono HTTPS quando la pagina è aperta tramite
un indirizzo di rete remoto. Se la fotocamera è occupata, chiudi la finestra
**Prova QR** o le altre app che la usano. Se l'immagine è nera, controlla il
copriobiettivo e il tasto privacy.

## Prova autonoma

Avvio dalla cartella del progetto:

```powershell
python -m pip install -r requirements-qr.txt
python run.py qr-webcam --distinta "C:\percorso\distinta.rfidman"
```

Si apre una finestra con anteprima della webcam e risultati testuali.
Il file di riferimento viene decifrato con le chiavi già configurate in
`src/app/config.yaml`. Il test non apre il lettore RFID, non importa nulla
nel database e non salva le immagini della webcam.

1. Inquadra il QR sul foglio, interamente visibile, fermo e ben illuminato.
2. Quando compare il riquadro verde, premi **Spazio**, **Invio** o **Acquisisci QR**.
   Il riquadro segnala un codice otticamente leggibile; l'accettazione dei dati
   avviene solo con il comando manuale.
3. Se la distinta ha più QR, acquisiscili uno alla volta. La finestra indica
   le parti mancanti; leggere due volte la stessa parte non la duplica.
4. Il risultato mostra i dati di ogni contenitore, la verifica della firma QR
   e il confronto con il file: EPC mancanti, estranei e campi differenti.
5. **R / Nuova scansione** cancella la raccolta in memoria. **Esc / Chiudi**
   chiude l'applicazione e rilascia la webcam.

In alto puoi scegliere l'indice della webcam e il sistema di acquisizione Windows
(Auto, DirectShow o Media Foundation), poi premere **Avvia / riavvia webcam**.
Il contatore dei fotogrammi e gli fps indicano se il video si sta aggiornando.
La ricerca del QR lavora separatamente, senza fermare l'anteprima.
Se compare l'avviso di immagini nere, controlla copriobiettivo e tasto privacy:
la webcam può risultare collegata e inviare comunque fotogrammi completamente neri.

Il confronto usa EPC, nome completo, codice fiscale, data del prelievo,
descrizione, materiale, fissativo, sede e numerazione del contenitore.
Sesso, data di nascita e ora del prelievo vengono mostrati dal QR, ma non
confrontati: il formato del file distinta non contiene questi campi.
Una corrispondenza dei dati non certifica l'arrivo fisico del materiale.

Senza `--distinta`, l'app mostra i dati decodificati senza confronto.
Se non è disponibile il portachiavi, la firma QR viene dichiarata non verificata;
una firma errata con le chiavi disponibili viene rifiutata.
Il file `.rfidman` deve sempre superare la propria decifratura e autenticazione.
Sono supportate le distinte v1 e le v2 con certificati e chiave destinatario
già configurati, senza creare nuove chiavi.

Per un'altra webcam o configurazione:

```powershell
python run.py qr-webcam --camera 1 --config "C:\percorso\config.yaml" --distinta "C:\percorso\distinta.rfidman"
```

Per una prova da immagini, senza attivare la webcam:

```powershell
python run.py qr-webcam --distinta "C:\percorso\distinta.rfidman" --immagine parte1.png parte2.png
```

In modalità immagine, l'uscita è 0 per lettura/confronto riusciti, 1 per
differenze rispetto alla distinta, 2 per errore o parti mancanti.

Le dipendenze webcam restano facoltative per il resto del progetto.
Riferimenti: [acquisizione OpenCV](https://docs.opencv.org/4.x/d8/dfe/classcv_1_1VideoCapture.html)
e [decoder ZXing-C++ per Python](https://github.com/zxing-cpp/zxing-cpp/blob/master/wrappers/python/README.md).

Test senza webcam: `python src/tests/test_qr_webcam.py`.
Test anche del decoder ottico: `python src/tests/test_qr_webcam.py --ottico`.
Regressioni dei comandi e del ciclo webcam, con sorgente simulata:
`node src/tests/test_qr_camera_ui.cjs`.
