# Accettazione giornaliera

La nuova schermata è nella WebUI, avviata con `python run.py webui`.
Il pannello tecnico Tkinter resta separato. Aggiornamento: 6 settembre 2026.

## Lavorare dalla giornata

1. Selezionare l'operatore e aprire **Accettazione**. La schermata mostra i
   pazienti registrati oggi, i campioni e quanti tag mancano o sono verificati.
2. Premere **Nuovo paziente**. Nome e cognome sono obbligatori; il codice fiscale
   è facoltativo, ma viene validato quando inserito. L'ID interno (`P000001`, ecc.)
   distingue gli omonimi. I suggerimenti non uniscono automaticamente le persone.
3. Inserire uno o più campioni, con descrizione, materiale, fissativo, sede,
   avvertenze e numero di contenitori per campione. I dati aggiuntivi e del
   prelievo sono nella sezione espandibile. Per registrare solo il paziente,
   disattivare **Prepara anche i campioni**.
4. **Salva paziente e campioni** registra una bozza nel database, senza attivare
   il lettore. Si possono preparare altri pazienti prima di scrivere qualsiasi tag.
5. Selezionare un paziente e scegliere **Scrivi tag** sul lotto desiderato.
   Per più contenitori, confermare il totale prima della prima scrittura.
   Presentare un solo tag all'antenna e premere **Scrivi il tag**.
6. **Torna all'elenco pazienti** consente di interrompere la sequenza tra due
   scritture. Il dettaglio mostra i tag già verificati e quelli mancanti;
   **Riprendi scrittura** riparte dai mancanti, anche dopo il riavvio.

La data della giornata è quella di registrazione, nel fuso italiano, non quella
del prelievo. Il pulsante **Oggi** riporta alla giornata corrente. Ricerca e filtri
operano sull'elenco e sugli arretrati; i contatori descrivono l'intera giornata
prima dei filtri. **Aggiorna** rilegge anche il dettaglio selezionato.

Le accettazioni precedenti incomplete sono nella sezione **Da completare dei
giorni precedenti**, evidenziata in ambra. Quando ce ne sono, un avviso in testa
alla giornata riporta quanti pazienti e quanti tag restano da scrivere, con un
pulsante che apre il primo da completare; il numero totale dei tag da scrivere
(giornata più arretrati) compare anche come badge sulla voce **Accettazione**
della barra di navigazione. Il dettaglio di un paziente conserva anche gli altri
suoi lotti, ciascuno con la propria data. Su desktop elenco e dettaglio sono
affiancati; su tablet si passa fra i due con **Elenco pazienti**.

## Stati e protezioni

| Stato | Significato |
| --- | --- |
| Da preparare | Paziente salvato, senza contenitori preparati. |
| Da scrivere | Contenitori preparati, nessun tag ancora verificato. |
| Parziale | Solo una parte dei tag è verificata. |
| Completato | Tutti i contenitori attivi hanno un tag verificato. |
| Annullato | Bozza annullata, conservata nello storico. |
| Da verificare | Errore di scrittura da esaminare nel dettaglio. |

Un EPC riservato dopo un tentativo fallito non conta come tag verificato.
I contenitori annullati non incrementano i progressi.

La bozza è modificabile prima del primo tentativo di scrittura. Quando il lotto
viene avviato, anagrafica, campioni e numerazione sono congelati: nuovi campioni
vanno in una nuova accettazione dello stesso paziente. Questo evita discrepanze
con i dati già finiti, o potenzialmente finiti, sul chip. La sostituzione di un
contenitore guasto resta un'operazione distinta e tracciata.

Il sistema rifiuta il salvataggio di una bozza modificata nel frattempo da un'altra
finestra. Le operazioni radio rimangono serializzate; una finestra con una selezione
superata non può scrivere sul lotto selezionato da un'altra.

## Tag senza USER e compatibilità

Le impostazioni dei profili tag rimangono indipendenti dalla giornata: in modalità
**Solo EPC** non occorre USER memory. Il codice EPC identifica il contenitore e la
sua accettazione, da cui il database risale al paziente. La riscrittura EPC di
prototipazione rimane controllata dall'apposita impostazione.

Se si usa USER memory, i pazienti senza codice fiscale utilizzano il payload v2,
senza aumentare l'occupazione rispetto al v1. I tag con codice fiscale continuano
ad essere scritti nel formato v1, che resta leggibile. Anche etichette e distinte
supportano il codice fiscale assente. Aggiornare le postazioni destinatarie prima
di inviare tag USER v2: il software precedente non conosce quel formato.

## Archivio e collaudo

Al primo avvio il database viene aggiornato allo schema 8. La migrazione conserva
ID, collegamenti e progressivi dei pazienti; è transazionale. Prima di aggiornare
una postazione in esercizio, chiudere l'applicazione e conservare un backup
dell'archivio secondo la normale procedura della sede.

Test specifici: `python src/tests/test_giornata.py`, inclusi omonimi, CF facoltativo,
bozze concorrenti, ripresa dopo riavvio, migrazione e rollback, tag parziali,
congelamento, etichette e distinte. La suite generale è `python run.py tests`.
Il collaudo visivo usa dati fittizi e un lettore simulato, su desktop e tablet.
Collaudo del 6 settembre 2026: 704 test superati, inclusi 15 test della giornata;
controlli Ruff e sintassi JavaScript superati. Verificati nel browser inserimento
senza CF, modifica di più campioni, scrittura parziale e ritorno all'elenco.
Non sostituisce la prova finale con il lettore fisico e i tag scelti dalla sede.
