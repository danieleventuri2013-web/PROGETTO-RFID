/* Controllo visivo facoltativo. Le immagini viaggiano solo verso la postazione. */
(() => {
  "use strict";
  const el = id => $("#visivo-" + id);
  const video = el("video"), canvas = el("overlay"), frame = document.createElement("canvas");
  let stream = null, generation = 0, timer = null, sid = null, contentKey = "";
  let result = null, area = null, editing = "", centres = [], frozen = null, frozenImage = null;
  let session = null, pending = null, recovering = false, tried = false, sealedPhoto = false;
  let request = null, analysisFlight = null, recoveryFlight = null;
  let opening = false;
  const note = text => { el("stato").textContent = text; };
  const identity = () => ({ shipment_id: stato.spedizione?.shipment_id, ...(session ? { sessione: session } : {}) });
  async function api(action, data = {}, signal) {
    const response = await fetch("/api/controllo_visivo", { method: "POST",
      headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
      body: JSON.stringify({ ...identity(), azione: action, ...data }), signal });
    const body = await response.json();
    if (!response.ok) throw new Error(body.errore || "Controllo non riuscito");
    return body;
  }
  function handle(fn) { return async () => { try { await fn(); } catch (e) { note(e.message); } }; }
  function bind(id, fn) { el(id).addEventListener("click", handle(fn)); }
  function scatto() {
    if (!stream || video.readyState < 2 || !video.videoWidth) throw new Error("Attendere l'anteprima della webcam.");
    const scale = Math.min(1, 1920/video.videoWidth, 1080/video.videoHeight);
    frame.width = Math.round(video.videoWidth*scale); frame.height = Math.round(video.videoHeight*scale);
    frame.getContext("2d").drawImage(video, 0, 0, frame.width, frame.height);
    return frame.toDataURL("image/jpeg", .9).split(",")[1];
  }
  function stop() {
    generation++; clearTimeout(timer); request?.abort();
    opening = false;
    el("avvia").disabled = false;
    window.WebcamLocale.ferma("sigillo"); stream = null; video.srcObject = null;
    el("video-box").hidden = true;
    el("foto").disabled = true;
    if (recovering) interrompi().catch(e => note(e.message));
  }
  window.fermaWebcamSigillo = stop;
  window.aggiornaRecuperoVisivo = esito => {
    if (!recovering || esito.shipment_id !== stato.spedizione?.shipment_id) return;
    el("conteggi").textContent = `Attesi ${esito.attesi} · Visibili ${centres.length} · RFID ${esito.trovati} · Mancanti ${esito.attesi-esito.trovati}`;
    note(`Passata RFID ${esito.passata}: recuperati ${esito.trovati}/${esito.attesi}.`);
  };
  async function interrompi() {
    tried = true;
    if (recovering) await fetch("/api/interrompi", { method: "POST", headers: { "X-RFID-Token": TOKEN } });
  }
  function draw() {
    if (!result) return;
    [canvas.width, canvas.height] = result.dimensioni;
    const ctx = canvas.getContext("2d"), w = canvas.width, h = canvas.height;
    if (frozenImage) ctx.drawImage(frozenImage, 0, 0, w, h);
    const poly = area || result.area;
    if (poly?.length) {
      ctx.strokeStyle = "#ffcc32"; ctx.lineWidth = 4; ctx.beginPath();
      poly.forEach(([x,y],i) => i ? ctx.lineTo(x*w,y*h) : ctx.moveTo(x*w,y*h));
      if (poly.length === 4) ctx.closePath(); ctx.stroke();
    }
    ctx.font = `${Math.max(18, w/45)}px sans-serif`;
    centres.forEach(([x,y],i) => {
      const circle = !result.manuale && result.cerchi?.[i];
      if (circle) {
        ctx.beginPath(); ctx.arc(x*w,y*h,circle[2]*Math.min(w,h),0,2*Math.PI);
        ctx.strokeStyle = "#52ff9d"; ctx.lineWidth = 3; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(x*w,y*h,Math.max(12,w/60),0,2*Math.PI);
      ctx.fillStyle = "#18342ddd"; ctx.fill(); ctx.strokeStyle = "#52ff9d"; ctx.stroke();
      ctx.fillStyle = "white"; ctx.fillText(String(i+1), x*w-6, y*h+6);
    });
  }
  async function aggiornaCamere(turn = generation) {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const devices = await navigator.mediaDevices.enumerateDevices();
    if (turn !== generation) return;
    const selected = stream?.getVideoTracks()[0]?.getSettings().deviceId || el("camera").value;
    const options = devices.filter(d => d.kind === "videoinput" && d.deviceId).map((d, i) => {
      const op = document.createElement("option");
      op.value = d.deviceId; op.textContent = d.label || `Videocamera ${i + 1}`;
      return op;
    });
    const defaultOption = document.createElement("option");
    defaultOption.value = ""; defaultOption.textContent = "Predefinita";
    if (selected && !options.some(op => op.value === selected)) {
      const saved = document.createElement("option");
      saved.value = selected; saved.textContent = "Videocamera selezionata (non rilevata)";
      options.push(saved);
    }
    el("camera").replaceChildren(defaultOption, ...options);
    el("camera").value = selected;
  }
  async function start() {
    stop();
    const turn = generation;
    const device = el("camera").value;
    opening = true; el("avvia").disabled = true;
    try {
      if (recoveryFlight) await recoveryFlight.catch(()=>{});
      if (analysisFlight) await analysisFlight.catch(()=>{});
      if (turn !== generation) return;
      note("Consenti la fotocamera nel browser. Il recupero RFID sarà automatico dopo un conteggio stabile.");
      const media = await window.WebcamLocale.apri("sigillo", { audio:false, video: {
        ...(device ? {deviceId:{exact:device}} : {}), width:{ideal:1920}, height:{ideal:1080} } }, stop);
      if (turn !== generation) return;
      stream = media; video.srcObject = media; await video.play();
      if (turn !== generation) return;
      await aggiornaCamere(turn);
      if (turn !== generation) return;
      media.getVideoTracks()[0].addEventListener("ended", () => { if(turn===generation) {stop(); note("Webcam scollegata.");} });
      el("video-box").hidden = false;
      editing = ""; frozen = frozenImage = null; tried = false;
      session = null; pending = null;
      el("conferma-foto").hidden = true;
      loop(turn);
    } catch (e) {
      if (turn !== generation) return;
      stop();
      note(e.name === "NotAllowedError" ? "Accesso alla webcam negato: consenti la fotocamera nel browser e riprova."
        : ["NotFoundError", "OverconstrainedError"].includes(e.name) ? "Webcam non disponibile: aggiorna l'elenco e scegli un'altra videocamera."
        : e.name === "NotReadableError" ? "Webcam occupata: chiudi le altre applicazioni che la usano oppure scegli un'altra videocamera." : e.message);
    } finally {
      if (turn === generation) { opening = false; el("avvia").disabled = false; }
    }
  }
  async function loop(turn) {
    if (turn !== generation || !stream) return;
    try {
      if (!editing && !pending && !sealedPhoto) {
        request = new AbortController();
        analysisFlight = api("analizza", {immagine_base64:scatto(), ...(area?.length===4 ? {area_manuale:area} : {})}, request.signal);
        const response = await analysisFlight;
        if (turn !== generation) return;
        session = response.sessione;
        if (editing || pending || sealedPhoto) return;
        result = response; centres = result.confermati;
        draw();
        const rfid = result.rfid;
        el("conteggi").textContent = `Attesi ${stato.spedizione?.attesi ?? "—"} · Visibili ${centres.length} (${result.manuale ? "corretti" : "automatici"}) · RFID ${rfid?.trovati ?? stato.presenza?.trovati ?? "—"} · Estranei ${rfid?.unexpected?.length ?? "—"}`;
        if (!recovering) note(result.qualita !== "leggibile" ? "Bordo o immagine non determinabile: illumina la scatola oppure indica i quattro angoli."
          : !result.stabile ? "Attendo che conteggio e posizione siano stabili…"
          : centres.length !== stato.spedizione?.attesi ? "Il numero visivo differisce dalla distinta: verifica contenuto e marcatori."
          : rfid?.concorde ? "Conteggio visivo e identità RFID concordano. Puoi confermare il contenuto e scattare."
          : "Conteggio stabile. " + (!stato.collegato ? "Collega il lettore RFID per il confronto." : "Verifica RFID disponibile."));
        el("foto").disabled = !rfid?.concorde || recovering || !result.stabile;
        if (result.stabile && centres.length === stato.spedizione?.attesi && !tried && !recovering && stato.collegato && !stato.occupato) {
          recupera().catch(e => note(e.message));
        }
      }
    } catch(e) { if(turn===generation) {el("foto").disabled=true; note(e.message);} }
    finally { analysisFlight=null; if(turn===generation && stream) timer=setTimeout(()=>loop(turn),600); }
  }
  function recupera() {
    if (recoveryFlight) return recoveryFlight;
    recoveryFlight = eseguiRecupero().finally(() => {recoveryFlight=null;});
    return recoveryFlight;
  }
  async function eseguiRecupero() {
    if (recovering || !stream || !session) return;
    tried = true; recovering = true;
    const originalPause=stato.presenzaPausa;
    stato.presenzaPausa=true; stato.occupato=true; el("foto").disabled=true;
    $("#sigilla").disabled=true;
    note("Recupero RFID: cambio antenne e parametri, massimo 30 passate / 120 secondi…");
    try {
      await stato.presenzaRichiesta;
      const esito = await chiama("recupera_visivo", identity());
      el("passate").replaceChildren();
      for(const pass of esito.passes || []) {
        const p=document.createElement("p");
        p.textContent=`Passata ${pass.pass}: ${pass.label}; ${pass.epcs?.length || 0} EPC, nuovi: ${(pass.nuovi || []).join(", ") || "nessuno"}`;
        el("passate").append(p);
      }
      for(const avviso of esito.avvisi || []) {
        const p=document.createElement("p"); p.textContent=avviso; el("passate").append(p);
      }
      note(esito.concorde ? "Tutti gli EPC attesi recuperati. Conferma il contenuto e scatta la foto."
        : `Recuperati ${esito.trovati}/${esito.attesi}. ${esito.error || esito.stop_reason}. Mancanti: ${(esito.missing || []).join(", ") || "nessuno"}. Controlla i marcatori o riposiziona la scatola e ripeti.`);
      if(result) result.rfid=esito;
      el("foto").disabled = !esito.concorde;
    } finally {recovering=false; stato.occupato=false; stato.presenzaPausa=originalPause;}
  }
  async function freeze() {
    await analysisFlight?.catch(()=>{});
    frozen=scatto(); frozenImage=new Image();
    frozenImage.src="data:image/jpeg;base64,"+frozen;
    await frozenImage.decode(); draw();
  }
  async function impostazioni() {
    const response=await api("impostazioni"); const c=response.config;
    el("box-mark").checked=!!c.marcatori_scatola; el("lid-mark").checked=!!c.marcatori_coperchio;
    el("lid-check").checked=!!c.controlla_coperchio;
    el("min").value=(c.raggio_min ?? .012)*100; el("max").value=(c.raggio_max ?? .16)*100;
    el("soglia").value=c.sensibilita ?? 30;
    if(c.camera) { const o=document.createElement("option"); o.value=c.camera; o.textContent="Videocamera salvata"; el("camera").append(o); el("camera").value=c.camera; }
    await aggiornaCamere();
  }
  async function nuova() {
    await interrompi();
    await recoveryFlight?.catch(()=>{});
    editing="attesa"; await analysisFlight?.catch(()=>{});
    await api("invalida");
    session=null; result=null; pending=null; sealedPhoto=false; tried=false;
    editing=""; frozen=frozenImage=null;
    el("conferma-foto").hidden=true; el("foto-salvate").replaceChildren();
    note("Prova precedente invalidata. Riprendi con la scatola aperta e ferma.");
  }
  bind("avvia", start);
  bind("aggiorna-camera", () => aggiornaCamere());
  el("camera").addEventListener("change", handle(async () => {
    area = null;
    if (stream || opening) await start();
  }));
  bind("ferma", () => {stop(); note("Webcam spenta. Le prove confermate rimangono archiviate.");});
  bind("salva", async()=>{
    await api("configura", {camera:el("camera").value, marcatori_scatola:el("box-mark").checked,
      marcatori_coperchio:el("lid-mark").checked, controlla_coperchio:el("lid-check").checked,
      raggio_min:Number(el("min").value)/100, raggio_max:Number(el("max").value)/100, sensibilita:Number(el("soglia").value)});
    session=null; area=null; note("Configurazione salvata. Se cambia la camera o la modalità dei marcatori, ripeti la calibrazione.");
  });
  bind("area", async()=>{editing="area"; area=[]; await freeze(); note("Clicca i quattro angoli interni: alto sinistra, alto destra, basso destra, basso sinistra.");});
  bind("auto-area", ()=>{area=null; editing=""; frozen=frozenImage=null; session=null; tried=false;});
  bind("correggi", async()=>{if(!result?.area) throw new Error("Individua prima il bordo della scatola."); editing="centri"; await freeze(); el("conferma-correzioni").hidden=false; note("Clicca per aggiungere/togliere; trascina un numero per spostarlo. Poi conferma i marcatori.");});
  bind("conferma-correzioni", async()=>{
    const response=await api("correggi", {centri:centres}); session=response.sessione;
    editing=""; frozen=frozenImage=null; el("conferma-correzioni").hidden=true; tried=false;
  });
  for(const tipo of ["aperta","chiusa"]) bind("calibra-"+tipo, async()=>{
    const roi=area || result?.area; if(!roi || roi.length!==4) throw new Error("Indica prima i quattro angoli della scatola.");
    await api("calibra", {tipo, area:roi, immagine_base64:scatto()}); session=null;
    note(`Riferimento ${tipo} salvato. Il conteggio sarà ripetuto.`);
  });
  bind("recupera", recupera); bind("interrompi", interrompi); bind("nuovo", nuova);
  bind("foto", async()=>{
    editing="foto";
    try {
      await analysisFlight?.catch(()=>{});
      pending=await api("prepara_foto", {immagine_base64:scatto(), ...(area ? {area_manuale:area}: {})});
      frozenImage=new Image(); frozenImage.src="data:image/jpeg;base64,"+pending.foto.jpeg; await frozenImage.decode();
      centres=pending.centri; draw(); el("conferma-foto").hidden=false;
      note("Nuovo fotogramma acquisito. Verifica l'immagine e i numeri, poi conferma questa foto.");
    } catch(e) {editing=""; throw e;}
  });
  bind("conferma-foto", async()=>{
    const saved=await api("conferma_foto", {token:pending?.token}); pending=null; sealedPhoto=true; editing="";
    frozen=frozenImage=null; el("conferma-foto").hidden=true; el("foto").disabled=true;
    canvas.getContext("2d").clearRect(0,0,canvas.width,canvas.height);
    mostraProva(saved.prova, el("foto-salvate"));
    note("Foto del contenuto conservata. Ora posiziona il coperchio e, se previsto, verifica il coperchio. Poi certifica la scatola chiusa.");
  });
  bind("coperchio", async()=>{
    const response=await api("foto_coperchio", {immagine_base64:scatto(), ...(area ? {area_manuale:area}: {})});
    mostraProva(response.prova,el("foto-salvate"));
    note(`Coperchio: ${response.prova.coperchio.stato}. Gli agganci devono essere confermati dall'operatore nella sigillatura.`);
  });
  bind("salta", async()=>{await interrompi(); await recoveryFlight?.catch(()=>{}); editing="attesa"; await analysisFlight?.catch(()=>{}); await api("salta"); stop(); editing=""; sealedPhoto=false; el("attivo").checked=false; el("pannello").hidden=true;});
  bind("prova", async()=>{const response=await api("stato"); mostraProva(response.prova,el("foto-salvate"));});
  bind("ricevuta", async()=>{
    const response=await chiama("foto_visiva_ricevuta",{inbound_id:stato.distinta?.inbound_id});
    mostraProva(response.prova,el("ricevuta-foto"));
  });
  el("attivo").addEventListener("change",handle(async()=>{
    el("pannello").hidden=!el("attivo").checked;
    if(el("attivo").checked) await impostazioni(); else stop();
  }));
  let drag=null;
  canvas.addEventListener("pointerdown",e=>{
    const r=canvas.getBoundingClientRect(), p=[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];
    if(editing==="area") {
      area.push(p); draw();
      if(area.length===4) {editing=""; frozen=frozenImage=null; session=null; tried=false; note("Bordo confermato manualmente. Puoi memorizzare il riferimento aperto.");}
    } else if(editing==="centri") {
      const i=centres.findIndex(c=>Math.hypot(c[0]-p[0],c[1]-p[1])<.03);
      if(i<0) {centres.push(p); draw();} else {drag={i,p}; canvas.setPointerCapture(e.pointerId);}
    }
  });
  canvas.addEventListener("pointerup",e=>{
    if(!drag) return;
    const r=canvas.getBoundingClientRect(), p=[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];
    if(Math.hypot(p[0]-drag.p[0],p[1]-drag.p[1])<.01) centres.splice(drag.i,1); else centres[drag.i]=p;
    drag=null; draw();
  });
  window.aggiornaVisivoSpedizione=()=>{
    const key=JSON.stringify((stato.spedizione?.contenitori||[]).map(c=>[c.container_id,c.epc]));
    if(sid!==stato.spedizione?.shipment_id || contentKey!==key) {
      stop(); sid=stato.spedizione?.shipment_id; contentKey=key; session=null; sealedPhoto=false;
      result=null; tried=false; pending=null; area=null;
      el("attivo").checked=false; el("pannello").hidden=true; el("foto-salvate").replaceChildren();
    }
  };
  document.addEventListener("visibilitychange",()=>{if(document.hidden) stop();});
  window.addEventListener("pagehide",stop);

  function mostraProva(prova,target) {
    target.replaceChildren();
    const p=document.createElement("p");
    p.textContent=prova ? `Controllo: ${prova.stato}; operatore ${prova.operatore||"—"}; ${prova.quando||""}. Visibili: ${prova.confermati?.length ?? "—"}, RFID: ${prova.rfid?.trovati ?? "—"}. Coperchio: ${prova.coperchio?.stato||"non richiesto"}.` : "Nessuna prova visiva allegata.";
    if(prova?.sigillo_finale) p.textContent+=` Confronto RFID finale: ${prova.sigillo_finale.concorde ? "concordante" : "non concordante"}.`;
    target.append(p);
    for(const [key,title] of [["foto_contenuto","Contenuto prima della chiusura"],["foto_coperchio","Coperchio posizionato"]]) {
      const photo=prova?.[key];
      if(typeof photo?.jpeg!=="string" || photo.jpeg.length>350000 || !/^[A-Za-z0-9+/=]+$/.test(photo.jpeg)) continue;
      const figure=document.createElement("figure"), caption=document.createElement("figcaption"), img=new Image();
      caption.textContent=title; img.alt=title; img.className="visivo-foto"; img.src="data:image/jpeg;base64,"+photo.jpeg;
      figure.append(img,caption); target.append(figure);
    }
    const print=document.createElement("button"); print.type="button"; print.className="bottone"; print.textContent="Stampa prova visiva";
    print.addEventListener("click",()=>stampa(target.cloneNode(true),"Prova visiva")); target.append(print);
  }
  window.mostraProvaVisiva=mostraProva;
  function stampa(node,title) {
    const popup=window.open("","_blank"); if(!popup) throw new Error("Consenti la finestra di stampa.");
    popup.document.title=title;
    const style=popup.document.createElement("style"); style.textContent="body{font:16px sans-serif}img{max-width:100%}button{display:none}figure{break-inside:avoid}svg{display:block}article{display:inline-block;margin:1cm}@media print{button{display:none!important}}";
    popup.document.head.append(style); popup.document.body.append(popup.document.importNode(node,true));
    const button=popup.document.createElement("button"); button.textContent="Stampa"; button.style.display="block";
    button.onclick=()=>popup.print(); popup.document.body.prepend(button);
  }
  bind("marcatori",async()=>{
    const response=await api("marcatori"), wrapper=document.createElement("div");
    const instructions=document.createElement("p"); instructions.textContent="ArUco 4×4/50. Stampa al 100%. Scatola 0–3: esterno dei quattro angoli, visibili anche a coperchio montato. Coperchio 4–5: due estremità distanti. Non coprire gli agganci. Ricalibra dopo ogni spostamento dei marcatori."; wrapper.append(instructions);
    for(const marker of response.marcatori) {const article=document.createElement("article"); const p=document.createElement("p"); p.textContent=`${marker.id}: ${marker.nome}`; article.append(p); const img=new Image(); img.src="data:image/svg+xml;charset=utf-8,"+encodeURIComponent(marker.svg); article.append(img); wrapper.append(article);}
    stampa(wrapper,"Marcatori della scatola");
  });
})();
