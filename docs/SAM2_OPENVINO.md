# SAM 2 con la GPU Intel locale

Ambiente separato `.venv-openvino`, OpenVINO 2026.4.1, pesi SAM 2.1 Tiny
già locali. Il banco resta indipendente da RFID e archivio. Le immagini
non vengono inviate a servizi di riconoscimento esterni.

Dispositivo provato: Intel UHD Graphics 615 integrata, driver
31.0.101.2140, su Core m3-8100Y. `Core.available_devices` espone CPU e GPU;
il piccolo modello di verifica è stato compilato ed eseguito su `GPU.0`.
La disponibilità della GPU va verificata nuovamente su altri computer.

## Installazione e conversione

Eseguire da `C:\PROGETTO-RFID`, dopo l'installazione del banco SAM 2.
Il nuovo ambiente riusa Torch/Transformers già installati tramite un file
`.pth`: non modifica le dipendenze di `.venv-sam2` e non duplica i pesi.

```powershell
./.venv-sam2/Scripts/python.exe -m venv .venv-openvino --without-pip
./.venv-sam2/Scripts/python.exe -m pip --python ./.venv-openvino/Scripts/python.exe install -r requirements-openvino.txt
$samDeps = (Resolve-Path .venv-sam2/Lib/site-packages).Path
Set-Content .venv-openvino/Lib/site-packages/sam2-locale.pth -Value $samDeps -Encoding ascii
./.venv-openvino/Scripts/python.exe tools/prova_openvino_dispositivi.py
./.venv-openvino/Scripts/python.exe tools/converti_sam2_openvino.py
```

Il convertitore salva `encoder-eager.xml/.bin` e `decoder.xml/.bin` in
`models/sam2/openvino/`, con pesi F32. Il runtime può elaborare in F16 o
F32. Ogni profilo ha una cache locale dei modelli compilati. La prima
compilazione può richiedere alcuni minuti; avviene prima di accettare le
foto e non viene ripetuta ad ogni scatto.

È necessaria attenzione esplicita durante la conversione della codifica:
la prima esportazione SDPA con le versioni locali ha cambiato le feature
e contato 7 anziché 8 nella prima foto. Non viene usata dal banco.
L'esportazione eager è stata confrontata su questa foto con gli stessi
input PyTorch: errore massimo sulle maschere a bassa risoluzione circa
0,000016, sugli score inferiore a 0,000001, in OpenVINO CPU F32.
Questo controllo numerico non sostituisce le prove sulle foto in GPU F16.

## Avvio e confronto

Prova reale GPU F16 sulle sei foto del 2 ottobre 2026: conteggi
8,7,8,8,6,5, tempi 38,96–40,89 s, media 40,06 s, encoder e decoder GPU.0.
La prima foto sulla CPU originale richiedeva 88,80 s; sulla GPU 40,89 s.
Il caricamento/compilazione iniziale (100,20 s a cache fredda) è escluso.
La seconda foto perde un campione anche con l'automatismo CPU originale:
7/8, limite della finestra interna attuale. Il confronto manuale precedente
con altra ROI contava 8. Una richiesta HTTP JPEG sulla sesta foto ha
restituito 5 in 52,15 s complessivi. Successiva prova webcam C920 sullo
scatto con 17 campioni: conteggio17 in49,88s, bordo2,95s e conteggio46,93s.
Una sola immagine, non un tempo medio garantito.

È disponibile anche [Qwen via OpenRouter](OPENROUTER_VISION.md) nel selettore
della pagina webcam; **Rianalizza lo scatto** confronta i modelli sulla stessa foto.

Su questo computer ambiente e conversione sono già preparati. Per gli
avvii successivi si può usare `run-sam2-openvino.bat`.

```powershell
./.venv-openvino/Scripts/python.exe src/app/sam2_preview.py --motore openvino --dispositivo GPU --precisione f16 --riusa-token
```

`--riusa-token` conserva il collegamento esistente; calibrazione per
webcam e browser invariata. Ricaricare la pagina per vedere il motore
nel risultato. Non avviare due server sulla stessa porta: arrestare
prima soltanto il servizio SAM, lasciando attiva la WebUI principale.

Il risultato e i log riportano i dispositivi effettivi: encoder e decoder
devono mostrare `GPU.0`. Non viene usato AUTO e non esiste fallback
silenzioso sulla CPU. Senza GPU disponibile l'avvio fallisce esplicitamente.
Per il confronto selezionare `--dispositivo CPU --precisione f32`;
`--codifica torch` esegue invece soltanto la codifica in PyTorch CPU e il
decoder sul dispositivo OpenVINO scelto.

```powershell
./.venv-openvino/Scripts/python.exe tools/prova_sam2_openvino.py 'C:\Users\Daniele\Pictures\RFID\esempio1.png' --precisione f16 --attesi 8 --output demo-output/openvino-prova
```

Lo script riporta tempi, conteggio e dispositivo reale e salva contorni
PNG e JSON. `--attesi` controlla il risultato dopo l'inferenza, senza
fornire il numero atteso al modello. Codifica e decoder usano lo stesso
processor, tre maschere, griglia 16×16 e filtri della versione PyTorch.
La rettifica prospettica e i filtri dei contorni restano sulla CPU.

Ritorno al banco originale:

```powershell
./.venv-sam2/Scripts/python.exe src/app/sam2_preview.py --riusa-token
```

Riferimenti: [SAM 2 immagini con OpenVINO](https://docs.openvino.ai/2024/notebooks/segment-anything-2-image-with-output.html),
[requisiti OpenVINO](https://docs.openvino.ai/2026/about-openvino/release-notes-openvino/system-requirements.html).
