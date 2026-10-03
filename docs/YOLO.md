# YOLO locale per il conteggio dei contenitori

YOLO (Ultralytics, YOLOv8 / YOLO11) è il terzo motore del riconoscimento
accanto a SAM 2 e Qwen. Gira sul PC, nello stesso servizio visivo della
porta 8772, e risponde in decimi di secondo. Usa gli stessi ritagli,
la stessa prospettiva e lo stesso contratto JSON degli altri motori: nel banco
webcam (`sam2-auto.html`) e nel Sigillo basta sceglierlo nell'elenco
**Riconoscimento** / **Modello di riconoscimento**, dove è la scelta
predefinita: una scelta già salvata nelle Impostazioni o nel browser resta
valida finché non la si cambia. Mostra riquadri verdi numerati con classe e
confidenza. Non invia immagini all'esterno.

**In breve:** il modello base COCO non riconosce questi contenitori: 0/6 foto
corrette. Addestrato su cinque foto, con etichette ricavate da SAM, ha
contato la sesta, mai vista, 5 volte su 6. Aggiungendo all'addestramento
scene sintetiche composte in locale dalle stesse cinque foto: **6 su 6**, e
**47 su 48** anche con le foto escluse ruotate e capovolte (48/48 con la
regola dei riquadri annidati), contro 33/48 senza scene sintetiche. Circa
0,15–0,2 s per foto su CPU e 0,11–0,14 s sulla GPU Intel, contro 40 s di
SAM 2 e 3,4 s di Qwen. `contenitori-sintetiche-yolo11n.pt` è il predefinito
del servizio.

## Installazione

Si installa nell'ambiente del servizio visivo `.venv-openvino`, che riusa
Torch e OpenCV di `.venv-sam2`. `--no-deps` evita che pip aggiunga
`opencv-python` accanto a `opencv-python-headless`.

```powershell
$py = (Resolve-Path .venv-openvino/Scripts/python.exe).Path
./.venv-sam2/Scripts/python.exe -m pip --python $py install --no-deps ultralytics==8.4.171
./.venv-sam2/Scripts/python.exe -m pip --python $py install -r requirements-yolo.txt
./.venv-openvino/Scripts/python.exe src/app/sam2_preview.py --scarica-yolo yolo11n.pt yolov8n.pt
```

I pesi stanno in `models/yolo/` (esclusa da Git). Durante l'analisi il
motore usa soltanto file locali: pesi assenti sono un errore esplicito, non
un download silenzioso.

## Avvio

`run-sam2-openvino.bat` carica anche YOLO. `run-yolo.bat` avvia soltanto
YOLO (`--senza-sam`), senza caricare e compilare SAM: pronto in circa 30 s
a freddo (in gran parte l'import di PyTorch), con lo stesso collegamento
della WebUI. Sono due avvii alternativi della stessa porta 8772: arrestare
prima il servizio già attivo. Opzioni di `sam2_preview.py`:

| Opzione | Effetto |
| --- | --- |
| `--yolo PESI` | pesi in `models/yolo`; predefinito il primo presente fra `contenitori-sintetiche-yolo11n.pt` e `contenitori-yolo11n.pt`, altrimenti `yolo11n.pt`; `nessuno` disattiva YOLO |
| `--yolo-dispositivo cpu\|intel:gpu\|intel:cpu` | `intel:*` usa OpenVINO e richiede la cartella `*_openvino_model` esportata |
| `--yolo-confidenza 0.25` | soglia sotto cui un riquadro non è contato |
| `--yolo-classi cup bowl` | conta solo queste classi del modello; vuoto = tutte |
| `--yolo-prompt jar "container lid"` | solo pesi YOLOE: oggetti descritti a parole |
| `--senza-sam` | avvia soltanto YOLO (e Qwen se c'è la chiave): circa 30 s a freddo, senza il caricamento di SAM (100 s misurati a cache fredda) |

Senza ultralytics o senza pesi il servizio parte comunque: SAM e Qwen
restano disponibili e la richiesta YOLO risponde 503 con il motivo.
La rettifica prospettica funziona anche con `--senza-sam`. Il servizio
esegue una sola analisi alla volta, qualunque sia il motore (409 se occupato).

## Cosa segnala il risultato

- `incerto` è vero con i pesi base COCO (non sono addestrati su questi
  contenitori), senza rilevamenti oppure con un riquadro sotto 0,5 di
  confidenza. Nel Sigillo un risultato incerto chiede la verifica o la
  correzione dei marcatori prima del confronto RFID, come per Qwen.
- I riquadri grandi più del 12% della foto sono la borsa o il piano: esclusi
  e conteggiati negli avvisi.
- `classi_rilevate` riporta cosa ha visto il modello. Con COCO sono «clock» e
  «bowl»: il motivo per cui il modello base non è utilizzabile.

## Confronto sulle sei foto del 2 ottobre

Stesse foto di [OpenVINO](SAM2_OPENVINO.md) e [Qwen](OPENROUTER_VISION.md),
quantità verificate 8, 8, 8, 8, 6, 5. YOLO misurato il 3 ottobre 2026 con
`tools/confronta_yolo.py`, tre ripetizioni per foto, avvio escluso.
SAM 2 e Qwen **non sono stati rieseguiti**: sono le misure del 2 ottobre.
CPU Core m3-8100Y, GPU Intel UHD 615.

| Motore | Corrette | Conteggi | Tempo per foto |
| --- | --- | --- | --- |
| YOLOv8n COCO, CPU | 0/6 | 1, 1, 2, 0, 2, 2 («clock») | 0,19 s |
| YOLO11n COCO, CPU | 0/6 | 0, 0, 0, 0, 0, 0 | 0,18 s |
| YOLOv8s COCO, CPU | 0/6 | 2, 2, 1, 0, 0, 1 | 0,50 s |
| YOLO11s COCO, CPU | 0/6 | 0, 0, 0, 0, 0, 0 | 0,41 s |
| YOLO11n COCO, OpenVINO GPU FP16 | 0/6 | 0, 0, 0, 0, 0, 0 | 0,075 s |
| YOLOE-11s/m, prompt «jar, container lid, …» (prova esplorativa) | 0/6 a soglia 0,25; 1/6 con 11m a 0,10 | 11m a 0,10: 7, 5, 7, 7, 6, 1 | 0,7 s (s) – 1,7 s (m) |
| **YOLO11n addestrato, foto esclusa (LOO), regola annidati** | **6/6** | 8, 8, 8, 8, 6, 5 | 0,16–0,30 s |
| YOLO11n addestrato, foto esclusa (LOO), senza regola | 5/6 | 8, 8, 8, 9, 6, 5 | 0,16–0,30 s |
| **YOLO11n + scene sintetiche, foto esclusa (LOO)** | **6/6** anche senza regola | 8, 8, 8, 8, 6, 5 | 0,2–0,3 s |
| SAM 2.1 Tiny, OpenVINO GPU F16 (2/10) | 5/6 | 8, 7, 8, 8, 6, 5 | 40,06 s |
| Qwen3.8 27B, OpenRouter (2/10) | 12/12 | due richieste per foto | 3,44 s |

Il modello base COCO non ha una classe per un barattolo visto dall'alto:
sulle foto vede al più qualche orologio. YOLOE (vocabolario aperto) migliora
poco: con una soglia bassa trova alcuni contenitori, ma ne perde molti e
scambia per contenitore anche la borsa. Senza addestramento YOLO non è
utilizzabile per questo conteggio, per quanto veloce.

Report e immagini annotate: `demo-output/yolo-20261003/` (escluso da Git).

## Addestramento pilota

`tools/addestra_yolo.py` crea il dataset YOLO (una classe, `contenitore`)
dai contorni del conteggio automatico SAM già salvati, più un file JSON con
i soli riquadri che SAM aveva perso. Sulle sei foto SAM ne aveva perso uno:
il barattolo «EEEC» di `esempio2`, aggiunto a mano (`aggiunte.json`).
Prima di addestrare lo script verifica che le etichette di ogni foto
coincidano con la quantità attesa.

La sola stima onesta con sei foto è il **leave-one-out**: sei
addestramenti, ognuno su cinque foto, e conteggio sulla sesta che il modello
non ha mai visto. Poi `--finale` addestra su tutte e salva
`models/yolo/contenitori-yolo11n.pt`. Il servizio ora preferisce la versione
addestrata anche con le scene sintetiche (sezione successiva).

```powershell
./.venv-openvino/Scripts/python.exe tools/addestra_yolo.py `
  C:\Users\Daniele\Pictures\RFID\esempio1.png ... esempio6.png `
  --sam demo-output/openvino-20261002/gpu-corretto `
  --aggiunte demo-output/yolo-20261003/addestramento/aggiunte.json `
  --attesi 8 8 8 8 6 5 --epoche 100 --loo --finale contenitori-yolo11n.pt `
  --output demo-output/yolo-20261003/addestramento
```

YOLO11n, 640 px, 100 epoche, capovolgimenti orizzontali e verticali (la
vista dall'alto non ha un verso), rotazione ±10°, CPU: 10–17 minuti per
addestramento (più lenti quando la CPU era occupata da altre prove).

Risultati, 3 ottobre 2026 (`report.json` e `valutazione.json`):

| Foto esclusa | Attesi | Conteggio | Confidenze | Addestramento |
| --- | --- | --- | --- | --- |
| esempio1 | 8 | 8 | 0,70–0,98 | 12,7 min |
| esempio2 | 8 | 8 (SAM ne contava 7) | 0,80–0,96 | 11,5 min |
| esempio3 | 8 | 8 | 0,75–0,98 | 10,1 min |
| esempio4 | 8 | **9**, marcato incerto; 8 con la regola | 0,78–0,97; doppione 0,27 | 10,6 min |
| esempio5 | 6 | 6 | 0,84–0,99 | 16,7 min |
| esempio6 | 5 | 5 | 0,90–0,97 | 10,7 min |

L'errore di `esempio4`: il barattolo inclinato in basso a destra ha ricevuto
due riquadri, uno sul solo coperchio e uno su coperchio e corpo. Tutti gli
otto contenitori erano trovati; il nono era un doppione. La soppressione dei
doppioni di YOLO (NMS) guarda la sovrapposizione relativa (IoU), che per due
riquadri annidati di misura diversa resta sotto la soglia. Il riquadro in più
aveva confidenza 0,27, sotto 0,5, quindi il risultato era già **incerto** e
l'operatore sarebbe stato avvisato.

Regola aggiunta in `riassumi_rilevamenti`: un riquadro contenuto per almeno
l'85% in uno più sicuro è lo stesso contenitore, perché visti dall'alto due
recipienti non stanno uno dentro l'altro. Riapplicata ai pesi dei sei
addestramenti (`--solo-valutazione`) dà 6/6. **La regola è nata guardando
questo errore**: il 6/6 non è una stima indipendente quanto il 5/6. Per
prudenza (un piccolo recipiente davanti a uno grande potrebbe annidarsi in
prospettiva) un riquadro scartato così rende il risultato incerto e lo dice
negli avvisi. `contenimento=None` la disattiva.

### Il modello finale

`contenitori-yolo11n.pt` (5,5 MB, solo foto reali) è addestrato su tutte e sei le foto: misurarlo su
quelle foto dice solo la velocità. Con `tools/confronta_yolo.py`, cinque
ripetizioni per foto:

| Dispositivo | Tempo per foto | Conteggi sulle foto di addestramento |
| --- | --- | --- |
| CPU (PyTorch) | 0,20–0,35 s | 8, **9**, 8, 8, 6, 5 |
| GPU Intel UHD 615, OpenVINO FP16 | 0,09 s (0,22 s le prime due foto) | 8, 8, 8, 8, 6, 5 |

Il 9 su CPU è un falso positivo nell'angolo vuoto in alto a sinistra di
`esempio2`, con confidenza 0,252, appena sopra la soglia di 0,25. In FP16 la
stessa confidenza scende sotto soglia. Il risultato era marcato incerto
(«confidenza bassa sul campione 1»). La soglia non è stata alzata per far
tornare il numero: sarebbe tarata sulle stesse foto. Il caso mostra quanto è
ancora instabile un modello addestrato su sei immagini.

Nel servizio reale (`--senza-sam`, avviato subito dopo due ore di
addestramento sulla stessa CPU) le prime chiamate dalla WebUI hanno richiesto
1,2–1,8 s per foto; misurato poi da solo, 0,32–0,53 s. Sul Core m3 senza
ventola i tempi CPU variano con il carico e il riscaldamento: la GPU Intel è
la scelta più stabile.

Per usare la GPU Intel: `run-yolo.bat --yolo contenitori-sintetiche-yolo11n_openvino_model
--yolo-dispositivo intel:gpu` (la cartella si crea con `tools/confronta_yolo.py
--dispositivo intel:gpu --pesi contenitori-sintetiche-yolo11n.pt`).

## Scene sintetiche

Le varianti di una stessa foto (rotazioni, luminosità, sfocature: quelle di
Roboflow) le applica già Ultralytics durante l'addestramento, e mostrano
sempre la stessa disposizione degli stessi barattoli.
`tools/scene_sintetiche.py` crea invece **disposizioni nuove**, in locale:

- ritaglia i 43 contenitori dai contorni SAM (il barattolo aggiunto a mano
  diventa un'ellisse);
- usa come sfondo la foto vera, **con** i suoi contenitori e le loro
  etichette, e aggiunge ritagli negli spazi liberi del foglio: impronta per
  almeno il 97% su pixel chiari e poco saturi, nessuna sovrapposizione con
  i contenitori veri (riquadro allargato del 15% per corpo e ombra dei
  barattoli inclinati), nessun ritaglio sulle pareti della borsa, dove
  nascono i riflessi;
- ogni ritaglio ha rotazione libera, scala 0,85–1,15, specchio, luce e
  colore leggermente variati, bordo sfumato e un'ombra morbida; la scena ha
  luce globale e grana variabili. Le etichette nascono dal ritaglio, esatte.

Prima ho provato a rimuovere i contenitori veri per avere scatole vuote:
l'inpainting di OpenCV lasciava macchie a ventaglio, i riempimenti stimati
dal fondo dischi grigi dai bordi netti (ombre dei barattoli inclinati,
griglia e pareti). Il modello li avrebbe imparati come se fossero reali:
scartato. Per scene con pochi contenitori servono **foto vere della scatola
vuota** (`--sfondi-vuoti`), che sono anche il modo più semplice per
migliorare il generatore.

```powershell
./.venv-openvino/Scripts/python.exe tools/scene_sintetiche.py <foto...> `
  --sam demo-output/openvino-20261002/gpu-corretto `
  --aggiunte demo-output/yolo-20261003/addestramento/aggiunte.json `
  --quante 200 --output demo-output/scene   # anteprima.jpg per il controllo a occhio
```

Nel leave-one-out `tools/addestra_yolo.py --sintetiche 100` genera le scene
di ogni fold **solo dalle cinque foto di addestramento**: ritagli e sfondo
della foto esclusa non entrano mai. `--robustezza` conta poi ogni foto
esclusa nei suoi otto orientamenti (rotazioni e specchi: per una vista
dall'alto sono la stessa scena), 48 prove in tutto.

Criteri fissati **prima** di vedere i risultati, per decidere se adottare il
nuovo modello: leave-one-out non peggiore, robustezza migliore della base,
nessun aumento degli errori non segnalati. Altrimenti sarebbe rimasto il
modello delle sole foto reali.

YOLO11n, 100 scene sintetiche per fold più le cinque foto reali, 12 epoche
(circa 2,5 volte gli aggiornamenti dell'addestramento senza scene), 23–35
minuti per fold sulla CPU. Il quinto fold è stato interrotto da una carenza
di memoria del sistema e ripreso dall'epoca 7 (`--riprendi`).

| Foto esclusa | Attesi | Conteggio | Confidenze (senza scene) |
| --- | --- | --- | --- |
| esempio1 | 8 | 8 | 0,91–0,98 (0,70–0,98) |
| esempio2 | 8 | 8 | 0,91–0,99 (0,80–0,96) |
| esempio3 | 8 | 8 | 0,91–0,99 (0,75–0,98) |
| esempio4 | 8 | 8, senza doppione | 0,93–0,98 (doppione 0,27) |
| esempio5 | 6 | 6 | 0,92–0,99 (0,84–0,99) |
| esempio6 | 5 | 5 | 0,89–0,96 (0,90–0,97) |

Tempi di addestramento in `log-sintetiche.txt`; risultati in
`report-sintetiche.json`. Robustezza, `robustezza.json`: ogni foto esclusa nei suoi otto orientamenti,
contata dal modello del proprio fold.

| Modello | Grezzo | Con regola annidati | Errori non segnalati (grezzo / regola) |
| --- | --- | --- | --- |
| Solo foto reali | 33/48 | 40/48 | 3 / 1 |
| **Con scene sintetiche** | **47/48** | **48/48** | **0 / 0** |

Senza scene sintetiche gli errori erano tutti un contenitore in più (fino a
due), variabili con l'orientamento della stessa foto. Con le scene l'unico
errore grezzo è un 9 su `esempio3` specchiata, marcato incerto. Il modello
finale (`contenitori-sintetiche-yolo11n.pt`, scene da tutte e sei le foto)
conta 8, 8, 8, 8, 6, 5 senza avvisi su CPU (0,14–0,22 s) e su GPU Intel
OpenVINO (0,11–0,14 s): il falso positivo nell'angolo di `esempio2` del
modello precedente non c'è più. Misura sulle foto di addestramento: vale solo
per la velocità e per l'assenza di regressioni evidenti.

Parte del miglioramento può venire dal numero maggiore di aggiornamenti, non
solo dalla varietà delle scene: un addestramento di controllo con le sole
foto reali e la stessa durata non è stato eseguito.

## Limiti e passo successivo

Sei foto della **stessa borsa**, stessa luce, stessi barattoli; le scene
sintetiche ricombinano quegli stessi 43 barattoli sugli stessi sei sfondi. Il
leave-one-out dice che il modello non ha imparato a memoria la singola foto,
non che funzioni con altre scatole, altri contenitori, altra luce o con la
webcam C920 e la sua prospettiva. Il conteggio resta un ausilio alla verifica
dell'operatore; l'identificazione dei contenitori resta RFID.

Per un modello da usare davvero servono foto reali della postazione: indicativamente
qualche centinaio di scatti della webcam definitiva, con disposizioni,
quantità, contenitori e luci diverse, inclusi bordi, riflessi e campioni
vicini. Le etichette si ottengono come qui: proposta automatica (SAM o il
YOLO già addestrato), correzione dell'operatore, controllo che il numero
coincida con la distinta. Un insieme di foto mai usato per l'addestramento
misura poi l'accuratezza.
