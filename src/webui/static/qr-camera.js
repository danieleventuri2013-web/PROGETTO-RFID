/* Webcam della ricezione. Fotogrammi in memoria; acquisizione sempre manuale. */
(() => {
  "use strict";
  const video = $("#qr-camera-video"), bordi = $("#qr-camera-bordi");
  const avvia = $("#qr-camera-avvia"), acquisisci = $("#qr-camera-acquisisci");
  const ferma = $("#qr-camera-ferma"), scelta = $("#qr-camera-scelta");
  const nota = $("#qr-camera-stato"), riquadro = $("#qr-camera-riquadro");
  const frame = document.createElement("canvas");
  let stream = null, generazione = 0, timer = null, codici = [], vistoIl = 0;
  let occupato = false, completato = false, abort = null;

  function spegni() {
    generazione++;
    clearTimeout(timer);
    abort?.abort();
    abort = null;
    window.WebcamLocale?.ferma("ricezione");
    stream?.getTracks().forEach(t => t.stop());
    stream = null;
    video.srcObject = null;
    codici = [];
    vistoIl = 0;
    acquisisci.disabled = true;
    avvia.disabled = false;
    ferma.disabled = true;
    scelta.disabled = true;
    riquadro.hidden = true;
    nota.textContent = "Webcam spenta.";
  }
  window.fermaWebcamRicezione = spegni;
  window.azzeraWebcamRicezione = () => {
    spegni();
    completato = false;
    stato.distintaQr = null;
    stato.scansioni = [];
    dipingiParti([]);
    $("#errore-scansione").textContent = "";
    $("#scansione-nota").textContent = "";
    $("#qr-camera-dati").hidden = true;
  };

  async function accendi() {
    spegni();
    const turno = generazione;
    avvia.disabled = true;
    ferma.disabled = false;
    nota.textContent = "Apertura webcam… Consenti l'accesso quando il browser lo richiede.";
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error("Webcam disponibile su localhost o HTTPS: apri la postazione sul PC con localhost.");
      const constraints = { audio: false, video: scelta.value
        ? { deviceId: { exact: scelta.value }, width: { ideal: 1280 }, height: { ideal: 720 } }
        : { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } } };
      const media = window.WebcamLocale ? await window.WebcamLocale.apri("ricezione", constraints, spegni)
        : await navigator.mediaDevices.getUserMedia(constraints);
      if (turno !== generazione) { media.getTracks().forEach(t => t.stop()); return; }
      stream = media;
      video.srcObject = media;
      riquadro.hidden = false;
      await video.play();
      if (turno !== generazione) return;
      const devices = await navigator.mediaDevices.enumerateDevices();
      if (turno !== generazione) return;
      const attiva = media.getVideoTracks()[0].getSettings().deviceId;
      scelta.replaceChildren(...devices.filter(d => d.kind === "videoinput").map((d, i) => {
        const opzione = document.createElement("option");
        opzione.value = d.deviceId; opzione.textContent = d.label || `Videocamera ${i + 1}`;
        opzione.selected = d.deviceId === attiva; return opzione;
      }));
      ferma.disabled = false; scelta.disabled = false;
      media.getVideoTracks()[0].addEventListener("ended", () => {
        if (turno === generazione) { spegni(); nota.textContent = "Webcam scollegata. Ricollegala e premi Attiva webcam."; }
      });
      analizza(turno);
    } catch (e) {
      if (turno !== generazione) return;
      spegni();
      nota.textContent = e.name === "NotAllowedError" ? "Accesso alla webcam negato: consenti la fotocamera per questa pagina e riprova."
        : e.name === "NotReadableError" ? "Webcam occupata: chiudi l'app Prova QR o le altre applicazioni che la usano."
        : e.name === "NotFoundError" ? "Nessuna webcam disponibile." : e.message;
    }
  }

  async function analizza(turno) {
    if (turno !== generazione || !stream) return;
    try {
      if (video.readyState < 2 || !video.videoWidth) return;
      const scala = Math.min(1, 1280 / video.videoWidth, 720 / video.videoHeight);
      frame.width = Math.round(video.videoWidth * scala); frame.height = Math.round(video.videoHeight * scala);
      const ctx = frame.getContext("2d", { willReadFrequently: true });
      ctx.drawImage(video, 0, 0, frame.width, frame.height);
      const iniziato = performance.now();
      const controller = new AbortController();
      abort = controller;
      const timeout = setTimeout(() => controller.abort(), 8000);
      let result;
      try {
        const response = await fetch("/api/decodifica_qr_camera", { method: "POST",
          headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
          body: JSON.stringify({ immagine_base64: frame.toDataURL("image/jpeg", 0.9).split(",")[1] }),
          signal: controller.signal });
        result = await response.json();
        if (!response.ok) throw new Error(result.errore || "Decodifica non riuscita");
      } finally { clearTimeout(timeout); }
      if (turno !== generazione) return;
      codici = result.codici;
      vistoIl = iniziato;
      bordi.width = result.larghezza; bordi.height = result.altezza;
      const penna = bordi.getContext("2d");
      penna.clearRect(0, 0, bordi.width, bordi.height);
      penna.strokeStyle = "#39da77"; penna.lineWidth = 4;
      for (const codice of codici) {
        penna.beginPath(); codice.vertici.forEach(([x, y], i) => i ? penna.lineTo(x, y) : penna.moveTo(x, y));
        penna.closePath(); penna.stroke();
      }
      acquisisci.disabled = occupato || completato || !codici.length || performance.now() - vistoIl > 1500;
      const pixels = ctx.getImageData(0, 0, frame.width, frame.height).data;
      let massimo = 0;
      for (let i = 0; i < pixels.length; i += 128) massimo = Math.max(massimo, pixels[i], pixels[i+1], pixels[i+2]);
      nota.textContent = completato ? "Dati acquisiti. Premi Nuova scansione per un altro foglio."
        : massimo <= 3 ? "Immagine nera: controlla copriobiettivo o tasto privacy della webcam."
        : codici.length ? `${codici.length} QR leggibili. Premi Acquisisci o Spazio.` : "Video attivo: inquadra il QR, avvicina e tieni fermo il foglio.";
    } catch (e) {
      if (turno !== generazione) return;
      codici = []; acquisisci.disabled = true;
      bordi.getContext("2d").clearRect(0, 0, bordi.width, bordi.height);
      nota.textContent = e.name === "AbortError" ? "Decodifica troppo lenta: avvicina il QR e riprova." : e.message;
    } finally {
      if (turno === generazione && stream) timer = setTimeout(() => analizza(turno), 350);
    }
  }

  async function cattura() {
    if (occupato || completato || !stream || !codici.length || performance.now() - vistoIl > 1500) return;
    occupato = true; acquisisci.disabled = true;
    try {
      completato = await leggiScansione(codici.map(c => c.testo));
      if (completato) {
        const contenitore = $("#qr-camera-tabella"); contenitore.replaceChildren();
        const table = document.createElement("table"); table.className = "tabella";
        const campi = [["paziente", "Paziente"], ["codice_fiscale", "Codice fiscale"], ["sesso", "Sesso"],
          ["data_nascita", "Nascita"], ["data_prelievo", "Prelievo"], ["ora_prelievo", "Ora"],
          ["descrizione", "Campione"], ["materiale", "Materiale"], ["fissativo", "Fissativo"],
          ["sede", "Sede"], ["etichetta", "Contenitore"], ["epc", "EPC"]];
        const head = table.createTHead().insertRow();
        for (const [, nome] of campi) { const th = document.createElement("th"); th.textContent = nome; head.append(th); }
        const body = table.createTBody();
        for (const riga of stato.distintaQr.righe) {
          const row = body.insertRow();
          for (const [campo] of campi) row.insertCell().textContent = riga[campo] || "—";
        }
        contenitore.append(table); $("#qr-camera-dati").hidden = false;
        spegni(); nota.textContent = "Distinta acquisita: i dati sono caricati nella ricezione.";
      }
    } finally { occupato = false; }
  }
  avvia.addEventListener("click", accendi);
  scelta.addEventListener("change", accendi);
  ferma.addEventListener("click", spegni);
  acquisisci.addEventListener("click", cattura);
  $("#qr-camera-nuova").addEventListener("click", () => {
    if (occupato) return;
    completato = false; stato.scansioni = []; dipingiParti([]);
    $("#errore-scansione").textContent = "";
    $("#scansione-nota").textContent = "Inquadra il nuovo foglio e acquisisci tutte le parti. L'elenco cambia solo a lettura completa.";
    $("#qr-camera-dati").hidden = true;
  });
  document.addEventListener("keydown", e => {
    if (e.code !== "Space" || e.repeat || e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || schermataAttiva() !== "ricezione") return;
    if (e.target.closest("input, textarea, select, button, summary, a, [contenteditable], [role=dialog]") || document.querySelector("dialog[open]")) return;
    if (!stream) return;
    e.preventDefault(); cattura();
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden) spegni(); });
  window.addEventListener("pagehide", spegni);
})();
