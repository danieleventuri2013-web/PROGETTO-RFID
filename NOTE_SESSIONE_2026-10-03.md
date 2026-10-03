# YOLO locale nel riconoscimento — 3 ottobre 2026

Richiesta dell'utente: aggiungere YOLO v8/v11, più veloce, accanto a SAM 2 e
OpenRouter; partire dal modello base, poi valutare l'addestramento con le
immagini disponibili; confrontare accuratezza e velocità sulle foto di
`C:\Users\Daniele\Pictures\RFID` (esempio1–6, quantità 8, 8, 8, 8, 6, 5).
Guida completa: [docs/YOLO.md](docs/YOLO.md).

## Fatto

- Motore `src/app/yolo_engine.py` nel servizio della porta 8772
  (`/api/yolo/automatico`), scelta `yolo` in banco webcam, Sigillo e
  Impostazioni. Stesso contratto, ritaglio e prospettiva di SAM/Qwen.
- Ultralytics 8.4.171 installato in `.venv-openvino` con `--no-deps`
  (`requirements-yolo.txt`); `.venv-sam2` non è stato modificato. Pesi in
  `models/yolo/` (ora in `.gitignore`), compresi YOLOE e MobileCLIP (~600 MB)
  scaricati per la prova esplorativa.
- `run-yolo.bat` (`--senza-sam`), opzioni `--yolo*`, `--scarica-yolo`.
  Predefinito: `contenitori-yolo11n.pt` se presente, altrimenti `yolo11n.pt`.
- Strumenti `tools/confronta_yolo.py` e `tools/addestra_yolo.py`.

## Risultati

- Modello base COCO (v8n, 11n, v8s, 11s; CPU e OpenVINO GPU): **0/6**,
  0,07–0,5 s. Vede al più qualche «clock». YOLOE con prompt testuale: 0/6
  (1/6 abbassando la soglia). Senza addestramento non è utilizzabile.
- Addestramento YOLO11n, etichette dai contorni SAM del 2 ottobre più il
  barattolo «EEEC» di esempio2 aggiunto a mano. Leave-one-out: **5/6** grezzo
  (esempio4: 9, doppio riquadro su un barattolo inclinato, già marcato
  incerto); **6/6** con la regola dei riquadri annidati, introdotta dopo
  quell'errore e quindi non stima indipendente. 0,16–0,30 s per foto.
- Modello finale su tutte e sei: CPU 0,20–0,35 s, GPU OpenVINO 0,09 s. Su
  CPU conta 9 su esempio2 (falso positivo nell'angolo, confidenza 0,252,
  marcato incerto); soglia volutamente non ritoccata.
- SAM 2 e Qwen non rieseguiti: riportate le misure del 2 ottobre (SAM GPU
  5/6 in 40 s; Qwen3.8 12/12 in 3,4 s).

## Scene sintetiche (richiesta successiva dell'utente)

L'utente ha chiesto come addestrare meglio partendo dalle 6 foto, citando
Roboflow; spiegato che le varianti di una foto le fa già Ultralytics e
proposto un generatore locale di disposizioni nuove. Indicazione esplicita:
adottarlo **solo se vantaggioso**. Criteri fissati prima dei risultati.

- `tools/scene_sintetiche.py`: ritagli SAM incollati sul foglio libero delle
  foto reali. La rimozione dei contenitori (inpainting, riempimenti stimati)
  è stata provata e scartata: lasciava dischi grigi innaturali.
- Leave-one-out con 100 scene per fold, generate solo dalle 5 foto di
  addestramento: 6/6. Robustezza su 48 varianti ruotate/specchiate delle foto
  escluse: 47/48 grezzo, 48/48 con regola, 0 errori non segnalati (prima
  33/48, 40/48, 3/1). Adottato: `contenitori-sintetiche-yolo11n.pt` è il
  predefinito; resta `contenitori-yolo11n.pt` come riserva.
- Il fold di esempio5 è stato interrotto da Claude Code per memoria di
  sistema scarsa; ripreso su richiesta con `--riprendi` dall'epoca 7.
- Su richiesta YOLO è anche il motore preselezionato in banco webcam e
  Impostazioni (le scelte già salvate nell'archivio o nel browser restano).
- Non eseguito un controllo con sole foto reali e stessa durata: parte del
  miglioramento può venire dal maggior numero di aggiornamenti.

## Validazione

- `test_yolo.py` 7/7 senza ultralytics né pesi; casi YOLO aggiunti alle
  suite UI Node del banco webcam e del Sigillo. Runner **762/762** in 37
  gruppi, pytest **789 passati**, sei suite UI Node e sintassi JS superate.
  Ruff pulito sui file toccati; restano 2 segnalazioni preesistenti in
  `tools/demo_frontend.py` e `tools/conta_foto_openrouter.py`.
  Log: `logs/test-yolo-suite.txt`, `logs/test-yolo-pytest.txt`.
- Catena reale verificata: servizio `--senza-sam --riusa-token` sulla 8772 →
  ponte WebUI con foto vera (esempio4 → 8, esempio6 → 5, pesi addestrati
  scelti da soli), poi arrestato. La WebUI 8770 non era attiva e non è stata
  avviata; nessun collaudo con la webcam reale.

## Da riprendere

1. Collaudo con la webcam reale della postazione: scegliere YOLO nelle
   Impostazioni Controllo Visivo e confrontarlo con SAM/Qwen sullo stesso scatto.
2. Raccogliere qualche centinaio di scatti reali (disposizioni, quantità,
   contenitori e luci diversi) per un addestramento vero, con un insieme di
   prova separato. Etichette: proposta automatica + correzione dell'operatore.
3. Valutare se rendere predefinito l'avvio YOLO su GPU Intel
   (`--yolo contenitori-yolo11n_openvino_model --yolo-dispositivo intel:gpu`).
