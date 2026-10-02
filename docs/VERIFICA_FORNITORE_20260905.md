# Materiale fornitore e verifica al banco

Revisione software: 5 settembre 2026. Nessuna scrittura su hardware reale è
stata eseguita durante l'implementazione.

## Video nella cartella richiesta

Cartella: `ULTIMI MANUALI/SLP1027 and SLP1056 testing/SLP1027 and SLP1056 testing`.
Esaminati fotogrammi campionati dei tre MP4 (non una trascrizione dell'audio):

- `SLP1027 testing vedio.mp4`: antenna bianca con bottiglia contenente liquido
  appoggiata sopra; il software mostra conteggi di inventario.
- `SLP1027 testing vedio2.mp4`: ulteriori posizionamenti e più bottiglie sul piano.
- `SLP1056 testing vedio.mp4`: antenna nera, bottiglie ravvicinate e spostate
  rispetto al centro e ai bordi.

Questi esempi sono utili per progettare prove di posizione/orientamento e
interazione con liquidi. Non dimostrano una sequenza completa di scrittura e
rilettura verificata, non certificano la copertura del volume a tre antenne e
non consentono di ricavare con affidabilità tutti i parametri radio usati.
Non sono quindi una ragione per cambiare banda o potenza automaticamente.

## Tag silente dopo una risposta

Il manuale locale `EX10 Module Communication Prorocol-2024-12.pdf`, sezioni 6.1
e 6.5 (pagine PDF 99–101 e 121), descrive i target di accesso: con target
dinamico A-B viene usato A per l'accesso, e con B-A viene usato B.
Un inventario può cambiare il flag A/B del tag; lettura e scrittura successive
possono quindi non trovarlo pur essendo fermo sull'antenna.

Lo [standard GS1 Gen2](https://www.gs1.org/sites/default/files/docs/epc/Gen2_Protocol_Standard.pdf)
specifica che S0 perde la persistenza senza alimentazione, ma conserva il flag
mentre il chip resta alimentato. Aspettare un tempo fisso fra i comandi non
dimostra che la RF sia stata spenta. S0/statico A è l'assetto di accesso adottato,
non una garanzia universale di reset del tag.

Interventi software: eliminato il polling del piatto dalla WebUI, operazioni
radio serializzate, assetto di accesso circoscritto alle antenne di scrittura,
ripristino controllato, verifica obbligatoria del risultato. Anche modifiche a
profilo, operatore e contesto non possono sovrapporsi a una scrittura.
Il simulatore include il caso RF continua per impedire falsi test positivi su S0.

La scrittura può richiedere più energia della semplice lettura (manuale 6.1).
Il silenzio da solo non distingue sessione, margine RF, password, orientamento
o errore di comunicazione: la causa sul lettore fisico resta da verificare.

## Checklist fisica ancora da eseguire

1. Regione EU 0x08; un solo tag sul piatto e altri campioni fuori dal campo.
   Annotare modello tag, capacità e lock, firmware, antenna, trasporto e potenze.
2. Collegare e controllare il log: nessun censimento o profilazione automatici.
   Eseguire separatamente Rileva hardware e, se utile, Misura il tag.
3. Profilo solo EPC con USER disattivata: 20 cicli espliciti scrittura/verifica
   sullo stesso chip in prototipazione, prima seriale e poi LAN. Ripetere per
   ciascun modello candidato. Nessun lock permanente o kill.
4. Confrontare tag fermo e tag rimosso/riposizionato fra i cicli. Se solo il
   secondo caso riesce, raccogliere TX/RX e chiedere al fornitore una procedura
   documentata di interruzione RF/reset flag per questo firmware.
5. Ripetere sulle posizioni centro/bordo, orientamenti e contenitori reali
   con liquido, senza alzare indiscriminatamente la potenza.
6. Provare un tag con USER adeguata: verifica di EPC e payload, poi tornare al
   profilo senza USER. Verificare che il cambio non imponga misure automatiche.
7. Provare ricezione su un archivio di test separato con distinta importata.

Non provocare disconnessioni durante una scrittura su dati reali: i casi di
risposta persa e recupero sono stati collaudati mediante fault injection simulata.
