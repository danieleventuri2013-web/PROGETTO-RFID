# Chiusura giornata — 2 ottobre 2026

L'utente ha richiesto commit e push delle modifiche e ha concluso il lavoro
per oggi. Questa è la memoria da leggere alla prossima ripresa; le prove e
la cronologia dettagliate sono in [NOTE_SESSIONE_2026-10-01.md](NOTE_SESSIONE_2026-10-01.md).

## Funzionalità completate

- Riconoscimento degli oggetti nella Sigillatura con gli stessi motori del
  banco webcam: SAM 2 locale e Qwen3.8 27B via OpenRouter, oltre ai cerchi
  classici. Contorni SAM, centri numerati Qwen, tempi e note del modello.
- Riuso dell'area per dispositivo e della prospettiva; ritaglio prima del
  limite Full HD, foto rettificata anche per le correzioni manuali. Il video
  continuo non viene inviato ai modelli. Le risposte tardive sono scartate.
- Controllo locale del movimento: una variazione rilevata invalida stabilità,
  correzioni e riscontro RFID precedente. La prova cifrata conserva foto e
  metadati del riconoscimento e accompagna la distinta fino alla Ricezione.
- Tutta la configurazione in **Impostazioni → Impostazioni Controllo Visivo**:
  videocamera, modello, area/prospettiva, parametri dei cerchi, marcatori e
  relativi riferimenti della scatola aperta/con coperchio. Nel Sigillo restano
  attivazione per la scatola, riepilogo, comandi operativi e collegamento
  diretto alle Impostazioni. Prima di acquisire si rileggono le scelte salvate.
- Calibrazione incorporata nella pagina e disponibile senza spedizione
  aperta. Anteprima senza RFID; prove archiviate immutate. Uscire dalle
  Impostazioni scarica il riquadro e rilascia la webcam. La pagina di area
  può essere incorporata solo dalla stessa origine; le altre restano bloccate.
- «Scatta e cerca campioni»: blocco spiegato sulle spedizioni concluse o
  archiviate, errore dello scatto persistente durante l'anteprima, nessuna
  rianalisi di una foto priva di profilo valido.

Guida operativa: [Riconoscimento nel Sigillo](docs/RICONOSCIMENTO_SIGILLO.md).

## Validazione e limiti

- Runner del progetto: **754/754**, 36 gruppi.
- Pytest: **781/781**, copertura **84,23%** con `--cov=src`.
- Sei suite Node UI, controllo sintassi JavaScript, Ruff e controllo diff
  superati; verificati anche gli identificativi HTML unici.
- Prova del motore SAM attraverso la nuova API del Sigillo, con database
  temporaneo e foto già disponibile: 5 campioni, 41,21 s di inferenza,
  42,52 s HTTP. OpenVINO GPU F16, senza RFID e senza invio a OpenRouter.
- Nuova sottosezione e riquadro area verificati nel browser e con screenshot.
  La verifica finale della navigazione al Sigillo è rimasta solo automatica
  perché il collegamento di automazione Chrome è diventato indisponibile.
- Non è stato eseguito un nuovo collaudo fisico completo webcam/RFID del
  Sigillo. Non è stata salvata una calibrazione inventata o modificata la
  prova della spedizione reale. I conteggi restano un ausilio alla verifica
  dell'operatore; l'identificazione dei contenitori resta RFID.

Log locali: `logs/test-impostazioni-visive-suite.txt`,
`logs/test-impostazioni-visive-pytest.txt`, `logs/test-visivo-impostazioni.txt`.
Report SAM: `demo-output/sigillo-modelli-20261002/verifica_sam_sigillo.json`.
Log, foto, database, chiavi e modelli rimangono esclusi da Git.

## Da riprendere

1. Verificare la disponibilità della videocamera prevista: nell'ultimo menu
   Chrome era presente USB Camera (0bda:5803), mentre la C920 non compariva.
   La USB non aveva un'area salvata; il profilo della C920 non va trasferito
   a una camera differente. I profili esistenti restano nel browser originale.
2. Aprire le Impostazioni Controllo Visivo, scegliere/salvare camera e modello,
   quindi verificare o configurare l'area sul reale allestimento dei campioni.
3. Per il collaudo del Sigillo usare una spedizione ancora modificabile:
   la spedizione 1 osservata era `received` con distinta già archiviata.
4. Eseguire il percorso reale fino a riconoscimento, confronto RFID, foto
   confermata e certificazione finale quando l'hardware è disponibile.

## Stato della postazione e Git

- Ultimo server WebUI avviato: porta 8770, PID 14540; banco SAM/Qwen sulla
  porta 8772. I PID sono solo riferimenti storici: verificarli alla ripresa.
  Non sono stati arrestati servizi alla chiusura della giornata.
- Lettore verificato `stopped`, `ready=false` prima dell'ultimo riavvio;
  operatore DV ripristinato. L'indirizzo corrente della WebUI è soltanto
  nel file locale `logs/webui_url.txt`, poiché il token cambia all'avvio.
- Ramo di lavoro e pubblicazione: `codex`, remote `origin` sul repository
  `danieleventuri2013-web/PROGETTO-RFID`. Il remote `precedente` è conservato.
  Repository pubblico e collaborazione già documentati nella nota precedente.
- Indicazione esplicita dell'utente: per questo progetto usare solo l'account
  `danieleventuri2013-web`. La regola è registrata in AGENTS.md e non modifica
  l'autenticazione degli altri progetti.
- Accesso di `danieleventuri2013-web` rinnovato con login guidato e conferma
  dell'utente. Il primo push aveva rilevato il token non valido; il rinnovo
  ha confermato l'identità corretta. Nessun codice o token di accesso è
  conservato nei documenti o nel repository.
- Commit di chiusura: «Integra riconoscimento nel Sigillo e impostazioni
  del controllo visivo». Identificativo e risultato del push sono riportati
  nella risposta di chiusura e verificabili con `git log`.
