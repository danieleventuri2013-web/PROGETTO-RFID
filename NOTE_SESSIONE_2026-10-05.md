# YOLO riaddestrato con le foto del 5 ottobre 2026

Richiesta dell'utente: continuare l'addestramento YOLO con sei foto nuove in
`C:\Users\Daniele\Pictures\RFID`. Indicate dall'utente come addestramento:
`contenitore-vuoto` (0), `contenitore-n16` (16), `contenitore-n16-ribaltato`
(16); come prova: `contenitori-n15` (15), `contenitori-n18` (18),
`contenitori-n16-ribaltato-2` (16). Dettagli in [docs/YOLO.md](docs/YOLO.md),
sezione «Foto del 5 ottobre».

## Fatto

- Misura **prima** dell'addestramento sulle foto di prova, otto orientamenti:
  il predefinito di allora (`contenitori-sintetiche-yolo11n.pt`) 4/24 con 3
  errori non segnalati; quello delle sole foto reali 5/24, 0 non segnalati.
- Etichette con il nuovo `tools/etichette_yolo_sam.py` (YOLO propone, due
  flaconcini aggiunti a mano su `contenitore-n16`, SAM contorna). Verificate
  a occhio: 16/16 su entrambe.
- `scene_sintetiche.py`: riscalatura dei ritagli sulla larghezza dello
  sfondo, sfondi vuoti con zona esplicita. `addestra_yolo.py --prova`.
- Addestramento continuato dal modello precedente, 9 foto reali + 200 scene,
  12 epoche, 46 minuti CPU → `models/yolo/contenitori-20261005-yolo11n.pt`
  (+ cartella OpenVINO). Criterio fissato prima: >5/24, non più errori non
  segnalati, nessuna regressione. Risultato 10/24 grezzo, 8/24 con regola
  annidati, 0 non segnalati, 9/9 sulle foto di addestramento: **adottato**
  come predefinito (`PESI_ADDESTRATI` in `src/app/yolo_engine.py`).

## Limiti emersi

- `contenitori-n15.png` sul disco è uno screenshot con riquadri e numeri già
  disegnati (13 su 15): misura falsata, chiedere lo scatto originale.
- Foto con sei flaconi coricati: da 12–15 si è passati a 19–25 (riquadri
  doppi coperchio/corpo), sempre marcato incerto. Un solo coricato
  nell'addestramento: servono foto con molti coricati.
- La regola dei riquadri annidati toglie flaconcini veri davanti a barattoli
  grandi in scene affollate (n15: 15 → 12). Soglia non ritoccata.

## Validazione

`python run.py tests`: 762 PASS, exit 0 (`logs/test-yolo-20261005.txt`);
`test_yolo.py` 8/8 anche con pytest. Ruff pulito sui file toccati. Servizio
e WebUI non riavviati: il nuovo predefinito vale dal prossimo avvio di
`run-yolo.bat` / `run-sam2-openvino.bat`.

## Da riprendere

1. Scatto originale di `contenitori-n15` senza annotazioni, rimisurare.
2. Foto con molti contenitori coricati e scene affollate sul bordo griglia;
   aggiungerle all'addestramento con `etichette_yolo_sam.py`, tenendo sempre
   qualche foto fuori come prova.
