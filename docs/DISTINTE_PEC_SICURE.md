# Distinte sicure tra unita' locali e ospedali

Questa procedura e' pensata per molte unita' mittenti e uno o due ospedali
destinatari, senza un server applicativo centrale. L'operatore esegue un solo
comando, **Archivia e invia via PEC**; chi riceve salva l'allegato e lo apre
dalla schermata **Ricezione**.

## Garanzie fornite

- La distinta nasce esclusivamente dai contenitori associati alla spedizione e
  deve coincidere, per EPC e TID, con il sigillo RFID valido.
- I dati sanitari sono cifrati con AES-256-GCM. La chiave casuale del singolo
  file e' cifrata con la chiave pubblica RSA dell'ospedale destinatario.
- La postazione mittente firma la busta completa con Ed25519 e allega il proprio
  certificato X.509, emesso dalla CA interna del circuito.
- SHA-256 e' calcolato sugli esatti byte finali, firma compresa. File, hash,
  messaggio MIME inviato e ricevute PEC integrali restano nel database locale.
- Esportazioni successive restituiscono gli stessi byte: una distinta gia'
  archiviata non viene rigenerata ne' sostituita.
- La partenza fisica e' bloccata fino alla ricevuta PEC di avvenuta consegna.
  L'unica eccezione e' una deroga motivata con PIN di un responsabile.

La firma e' una prova tecnica e di audit del sistema, non una firma elettronica
qualificata. La PEC certifica affidamento e consegna del messaggio; la firma
applicativa certifica integrita' e postazione di origine della distinta.

## Preparazione una tantum

La CA del circuito va gestita offline. La sua chiave privata non deve essere
installata sulle postazioni. L'amministratore emette:

1. per ogni unita' mittente, un certificato Ed25519 di firma;
2. per ogni ospedale, un certificato RSA di cifratura;
3. per i destinatari, la copia del certificato pubblico della CA;
4. una lista locale dei seriali revocati, aggiornata quando una postazione o una
   chiave non e' piu' attendibile.

Il `CN` oppure l'`OU` del certificato di firma deve coincidere esattamente con
`laboratorio.codice`. Le chiavi private devono essere PKCS#8 cifrate. Le loro
password e la password PEC si salvano nel portachiavi di Windows, mai nel YAML.
Con il pacchetto `keyring` installato si possono registrare, ad esempio:

```powershell
keyring set RFID-LIMS-KEYS station-signing-key
keyring set RFID-LIMS-KEYS recipient-decryption-key
keyring set RFID-LIMS-PEC unita-a@pec.example.it
```

Il file `src/app/config.yaml` contiene gia' tutti i campi. Esempio mittente:

```yaml
laboratorio:
  nome: Unita locale A
  codice: UL-A

destinatari:
- nome: Anatomia patologica - Ospedale 1
  codice: AP-OSP1
  pec: anatomia@pec.ospedale1.it
  encryption_certificate: C:/RFID/certificati/ospedale1-encryption.pem
  attivo: true

pec:
  enabled: true
  sender: unita-a@pec.example.it
  username: unita-a@pec.example.it
  credential_service: RFID-LIMS-PEC
  smtp_host: smtp.pec.example.it
  smtp_port: 465
  smtp_mode: ssl
  imap_host: imap.pec.example.it
  imap_port: 993

security:
  station_certificate: C:/RFID/certificati/UL-A.pem
  station_private_key: C:/RFID/chiavi/UL-A-private.pem
  station_credential_service: RFID-LIMS-KEYS
  station_credential_username: station-signing-key
```

Esempio ospedale destinatario:

```yaml
laboratorio:
  nome: Anatomia patologica - Ospedale 1
  codice: AP-OSP1

security:
  recipient_certificate: C:/RFID/certificati/ospedale1-encryption.pem
  recipient_private_key: C:/RFID/chiavi/ospedale1-private.pem
  trusted_ca: C:/RFID/certificati/ca-circuito.pem
  revoked_serials: []
  recipient_credential_service: RFID-LIMS-KEYS
  recipient_credential_username: recipient-decryption-key
```

SMTP deve usare TLS con verifica del certificato; sono supportati `ssl` e
`starttls`. IMAP usa TLS e serve solo a importare le ricevute tecniche PEC.

## Uso quotidiano del mittente

1. Accettare e scrivere i contenitori.
2. Selezionare i soli contenitori che devono partire.
3. Chiudere la scatola ed eseguire il sigillo RFID.
4. Premere **Archivia e invia via PEC**.
5. Premere **Aggiorna ricevute PEC** finche' compare
   **consegna PEC certificata**.
6. Consegnare la scatola al trasportatore e confermare la partenza.

Il corpo PEC contiene soltanto identificativi tecnici, conteggio e SHA-256; i
dati dei pazienti restano nell'allegato cifrato. Un errore di rete dopo l'invio
produce lo stato **esito incerto** e impedisce un secondo invio automatico, per
evitare duplicati: prima va verificata la casella PEC.

## Uso quotidiano del destinatario

1. Salvare l'allegato `.rfidman` ricevuto sulla PEC istituzionale.
2. Aprire **Ricezione** e scegliere il file.
3. Verificare a video SHA-256, mittente e stato **firma verificata**.
4. Appoggiare la scatola chiusa sul volume RFID e avviare il confronto.
5. Chiudere la ricezione; se manca o avanza un contenitore, la motivazione e'
   obbligatoria.

La postazione rifiuta automaticamente file alterati, certificati non emessi
dalla CA configurata, certificati revocati, documenti destinati a un altro
ospedale e duplicati gia' importati.

## Conservazione e backup

`lims.manifest_archive` conserva una copia esatta su filesystem; il database
conserva inoltre blob, SHA-256, MIME inviato e ricevute. Il valore
`lims.manifest_retention_days` vuoto significa **nessuna cancellazione
automatica**. Qualunque futura procedura di scarto deve essere esplicita,
tracciata e coerente con i tempi di conservazione dell'ente.

Database, archivio distinte, certificati pubblici e lista revoche devono essere
inclusi nel backup della postazione. Le chiavi private non vanno copiate in un
backup non cifrato; il loro recupero va progettato e provato dall'amministratore.

