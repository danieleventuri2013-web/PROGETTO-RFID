# Conteggio Qwen dalla webcam

Nella pagina `sam2-auto.html` il campo **Modello di riconoscimento** permette
di scegliere SAM 2 locale oppure Qwen3.8 27B tramite OpenRouter.
**Scatta e conta** acquisisce la foto; **Rianalizza lo scatto** usa la stessa
immagine già ritagliata e rettificata, anche dopo il cambio del modello.
Area e misure della webcam conservano la calibrazione esistente.
Gli scatti webcam sono inviati come JPEG ad alta qualità, entro 1,2 MB
prima della codifica base64: mantiene i pixel e riduce il rischio HTTP413
dei PNG Full HD. La qualità può scendere da95 a75 per rispettare il limite;
non viene ridotta la risoluzione. Il confronto da file conserva PNG.
SAM mostra contorni; Qwen mostra centri approssimativi numerati in blu.

Il server legge `OPENROUTER_API_KEY` dall'ambiente all'avvio. La chiave non
viene inviata al browser né salvata nel repository. Senza chiave SAM rimane
disponibile e una richiesta Qwen restituisce un errore esplicito.
Qwen invia a OpenRouter soltanto la foto dello scatto o della rianalisi
richiesta dall'operatore; non invia un flusso video continuo.
Utilizza `qwen/qwen3.8-27b`, provider DekaLLM senza fallback e thinking
disabilitato, con risposta JSON contenente conteggio, centri e incertezza.
Errori di rete o risposte incoerenti non diventano conteggi zero.

Avvio SAM su Intel GPU: `run-sam2-openvino.bat`; avvio CPU originale:
`./.venv-sam2/Scripts/python.exe src/app/sam2_preview.py --riusa-token`.
Entrambi offrono Qwen quando la variabile d'ambiente è presente.
Per installazione e calibrazione vedere [SAM locale](SAM2_LOCALE.md) e
[OpenVINO](SAM2_OPENVINO.md). OpenRouter richiede connessione Internet e
credito API, indipendentemente dal motore SAM locale.

## Confronto sulle foto del 2 ottobre 2026

Sei foto distinte, quantità verificate 8,8,8,8,6,5; due richieste per foto.
Al modello non sono stati forniti nomi dei file, quantità attese o contorni SAM.

| Modello e provider | Conteggi corretti | Media |
| --- | --- | --- |
| Qwen3.8 27B, DekaLLM | 12/12 | 3,44 s |
| Qwen3 VL 235B, instradamento automatico | 9/12 | 5,02 s |
| Qwen3 VL 235B, Parasail fisso | 12/12 | 8,08 s |

Le tre risposte errate del secondo gruppo sono zeri restituiti da Alibaba
senza indicare incertezza; non attribuiamo il problema al solo modello.
Costo complessivo delle 36 richieste: circa 0,0178 USD; Qwen3.8 circa
0,00793 USD per le 12 prove. I tempi comprendono invio e risposta API.
Le ripetizioni non aumentano il numero di scene distinte: questi risultati
non garantiscono accuratezza su altri allestimenti o sullo scatto con 17 campioni.

Strumenti: `tools/conta_foto_openrouter.py` e
`tools/rapporto_confronto_vision.py`. Foto, risultati e report locali sotto
`demo-output/` sono esclusi da Git. Test offline API, validazione e UI:
`src/tests/test_openrouter_vision.py`, `src/tests/test_sam2_auto_ui.cjs`.

Riferimenti: [modello e pesi Qwen](https://huggingface.co/Qwen/Qwen3.8-27B),
[API immagini OpenRouter](https://openrouter.ai/docs/guides/overview/multimodal/image-understanding).
