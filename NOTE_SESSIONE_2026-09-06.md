# Memoria di lavoro — 6 settembre 2026

## Punto di ripresa

L'implementazione richiesta è completata. L'utente ha visionato il risultato e
confermato: «ok sembra tutto ok», chiedendo di memorizzare il lavoro.
Non ripartire dal vecchio piano né trattare la WebUI e SQLite come funzionalità
ancora da costruire. Alcune descrizioni storiche in AGENTS.md/CLAUDE.md sono superate.

## Accettazione giornaliera completata

- Schermata principale con pazienti della giornata di registrazione (fuso italiano),
  ricerca, filtri, contatori e sezione separata per i giorni precedenti incompleti.
- Elenco e dettaglio affiancati su desktop; navigazione elenco/dettaglio su tablet;
  compatibilità con tema chiaro e scuro.
- Nome e cognome obbligatori; codice fiscale facoltativo, validato e univoco se
  presente. ID interno persistente per distinguere gli omonimi, senza fusioni
  automatiche delle persone.
- Più campioni per paziente e più contenitori per campione. Conservati dati del
  prelievo, materiale, fissativo, sede e avvertenze nelle sezioni del modulo.
- Salvataggio della bozza separato dalla scrittura: si possono preparare più
  pazienti, scrivere i tag nell'ordine desiderato e tornare all'elenco.
- Stati da preparare, da scrivere, parziale, completato, annullato; evidenza degli
  errori e distinzione fra EPC riservato e tag realmente verificato.
- Ripresa dei contenitori mancanti anche dopo il riavvio. Conferma del numero
  persistita per singola accettazione, non soltanto per la selezione attiva.
- Lotto congelato al primo tentativo di scrittura: anagrafica, campioni e totale
  non si modificano più. Nuovi campioni richiedono una nuova accettazione dello
  stesso paziente. La sostituzione di un contenitore resta distinta e tracciata.
- Protezioni contro bozze modificate da altre finestre, selezioni radio superate,
  doppio salvataggio e scorciatoia Invio che attiverebbe la scrittura mentre
  l'operatore sta usando un altro pulsante.

## Flessibilità RFID già realizzata e da preservare

- Collegamento seriale/TCP senza censimento automatico dell'hardware o del tag.
  Rilevamenti manuali nelle impostazioni.
- Scelta persistente delle antenne per lettura e scrittura e profili tag nominati,
  con configurazione delle memorie richieste e della capacità USER.
- Supporto a tag diversi, inclusi quelli senza USER memory; non vincolare il
  progetto al chip attualmente disponibile. In modalità Solo EPC, il codice
  identifica il contenitore/accettazione e il database risale al paziente.
- Riscrittura EPC consentita dalla modalità di prototipazione; tentativi e recupero
  di risposte perse tracciati nel database. Non rendere indiscriminata la riscrittura.
- Nessuna interrogazione periodica del piatto per animare la scrittura: avanzamento
  manuale al contenitore successivo. Accesso radio temporaneamente in S0, con
  ripristino dell'assetto, per evitare interferenze con l'inventory in S2.
- Modalità della sede: spedizione, ricezione oppure entrambe.
- Regione EU mantenuta. Le considerazioni su antenne/manuali/video del fornitore
  sono raccolte in `docs/VERIFICA_FORNITORE_20260905.md`.

## Persistenza e compatibilità

- Database SQLite allo schema 8: migrazione transazionale dalla versione 7,
  conservazione degli ID, dei progressivi e dei collegamenti; rollback su errore.
- Codice fiscale assente rappresentato come NULL nel database e stringa vuota
  nelle strutture applicative.
- Payload USER v2 per pazienti senza CF, senza aumentare la dimensione rispetto
  al v1. I tag con CF continuano a usare v1; il lettore applicativo legge entrambi.
- Etichette e distinte accettano CF assente. Aggiornare le postazioni destinatarie
  prima di inviare tag USER v2 a software che conosce soltanto v1.

## File da consultare

- `docs/ACCETTAZIONE_GIORNALIERA.md`: guida operativa, stati e compatibilità.
- `docs/CONFIGURAZIONE_OPERATIVA.md`: hardware, antenne, memorie, profili e prototipo.
- `src/lims/acceptance.py`: archivio della giornata e salvataggio delle bozze.
- `src/webui/static/accettazione.js` e `accettazione.css`: nuova interfaccia.
- `src/webui/workflow.py`, `server.py`, `static/app.js`, `static/index.html`:
  integrazione con scrittura, navigazione e API.
- `src/lims/db.py`, `model.py`, `codec.py`: schema, anagrafica e payload.
- `src/tests/test_giornata.py`: 15 regressioni specifiche; registrate in `run.py`.
- `src/tests/test_operativita.py`: regressioni delle precedenti modifiche RFID.

## Verifiche concluse e limiti

- `python run.py tests`: **704/704 test superati**, inclusi i 15 della giornata.
- Ruff sui file Python interessati e controllo sintattico dei JavaScript: superati.
- Controllo visivo nel browser con dati fittizi e lettore simulato: inserimento
  senza CF, più campioni, modifica di bozza, scrittura parziale e ritorno alla
  giornata; disposizione desktop e tablet, tema chiaro/scuro.
- Server e scheda di collaudo chiusi. Nessuna migrazione sull'archivio reale e
  nessuna operazione sull'hardware reale in questo collaudo.
- Non dedurre dall'assenso dell'utente una validazione RF completa: resta la prova
  finale con il lettore fisico e i tag scelti per la sede.
- Non sono stati eseguiti commit o push per questo lavoro. Preservare le modifiche
  locali esistenti, comprese quelle delle precedenti sessioni RFID.

Avvio operativo: `python run.py webui`. Test mirati: `python src/tests/test_giornata.py`.
Alla prossima richiesta, partire da questo stato e dal riscontro dell'utente;
non introdurre altre modifiche o prove RF automaticamente.
