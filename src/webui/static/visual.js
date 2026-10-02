/* Controllo avanzato facoltativo. Qwen invia solo lo scatto esplicito a OpenRouter. */
(() => {
  "use strict";
  const el = id => $("#visivo-" + id);
  const video = el("video"), canvas = el("overlay"), frame = document.createElement("canvas");
  let stream = null, generation = 0, timer = null, sid = null, contentKey = "";
  let result = null, area = null, editing = "", centres = [], frozen = null, frozenImage = null;
  let session = null, pending = null, recovering = false, tried = false, sealedPhoto = false;
  let request = null, analysisFlight = null, recoveryFlight = null;
  let opening = false;
  let aiBusy=false, aiFlight=null, aiShot=null, consoleModels=null;
  let erroreScatto=null;
  let configurando=false;
  function bloccoSpedizione() {
    const s=stato.spedizione;
    if(!s?.shipment_id)return "Apri o seleziona una scatola da sigillare prima di cercare i campioni.";
    if(s.esportata||s.consegna_pec||!["open","sealed"].includes(s.stato||"open"))
      return "Questa spedizione è già archiviata o conclusa. Per cercare i campioni nel Sigillo, apri una nuova scatola o seleziona una spedizione ancora da verificare. Puoi consultare la prova conservata.";
    return null;
  }
  const modello=()=>["sam2","qwen"].includes(el("modello").value)?el("modello").value:"cerchi";
  function profiloCamera() {
    try {return JSON.parse(localStorage.getItem("rfid.sam2.area.v2."+el("camera").value));} catch(_e) {return null;}
  }
  const profilo=()=>modello()==="cerchi"?null:profiloCamera();
  function datiScena() {
    const saved=profilo();let prepared=saved, bounds=null;
    if(saved) {
      if(!Array.isArray(saved.area)||saved.area.length!==4||!Array.isArray(saved.dimensioni)
        ||Math.abs(saved.dimensioni[0]/saved.dimensioni[1]-video.videoWidth/video.videoHeight)>=.02)
        throw new Error("Inquadratura cambiata: ripeti la configurazione dell'area.");
      bounds=saved.area.slice();
      if(saved.modo==="prospettiva") {
        const mx=(bounds[2]-bounds[0])*.06,my=(bounds[3]-bounds[1])*.06;
        bounds=[Math.max(0,bounds[0]-mx),Math.max(0,bounds[1]-my),Math.min(1,bounds[2]+mx),Math.min(1,bounds[3]+my)];
      }
      bounds=bounds.map((v,i)=>Math.round(v*(i%2?video.videoHeight:video.videoWidth)));
    }
    const photo=scatto(bounds);
    if(bounds) {
      const [left,top,right,bottom]=bounds,w=right-left,h=bottom-top;
      const point=([x,y])=>[(x*video.videoWidth-left)/w,(y*video.videoHeight-top)/h];
      const a=point(saved.area.slice(0,2)),b=point(saved.area.slice(2));
      prepared={...saved,area:[...a,...b].map(v=>Math.max(0,Math.min(1,v))),
        punti:saved.punti?.map(point)||null,dimensioni:[frame.width,frame.height]};
    }
    return {immagine_base64:photo,modello:modello(),profilo:prepared,...(area?.length===4?{area_manuale:area}:{})};
  }
  function controlliAI() {
    const blocco=bloccoSpedizione();
    el("blocco").hidden=!blocco;el("blocco").textContent=blocco||"";
    const disabled=aiBusy||recovering||!!pending||sealedPhoto||!!blocco;
    const configDisabled=aiBusy||recovering||!!pending;
    el("modello").disabled=configDisabled;
    el("riconosci").disabled=disabled||!stream||modello()==="cerchi";
    el("rianalizza").disabled=disabled||!stream||!aiShot||modello()==="cerchi";
    el("configura-modelli").disabled=aiBusy||recovering||!!pending;
    el("correggi").disabled=disabled;
    for(const id of ["area","auto-area","calibra-aperta","calibra-chiusa"])
      el(id).disabled=configDisabled||!configurando||!stream;
    el("salva").disabled=configDisabled;
    el("config-avvia").disabled=configDisabled||opening||!!stream;
    el("config-ferma").disabled=!configurando||(!stream&&!opening);
    for(const id of ["camera","aggiorna-camera"])
      el(id).disabled=aiBusy||recovering||!!pending;
    for(const id of ["conferma-correzioni","recupera","coperchio","nuovo","salta"])
      el(id).disabled=aiBusy||recovering||!!pending||!!blocco;
    if(aiBusy)el("foto").disabled=true;
    const p=profiloCamera();
    const label=Array.from(el("camera").children||[]).find(o=>o.value===el("camera").value)?.textContent||"Predefinita";
    const nomeModello={sam2:"SAM 2 locale",qwen:"Qwen3.8 27B",cerchi:"Cerchi classici"}[modello()];
    el("profilo").textContent=modello()==="cerchi"?"Rilevamento classico continuo dei cerchi."
      :p?`${p.modo==="prospettiva"?"Quattro angoli e prospettiva":"Area rettangolare"} salvati per questa webcam. ${modello()==="qwen"?"Solo lo scatto richiesto viene inviato a OpenRouter; verifica i punti numerati.":"SAM 2 locale: verifica i contorni e la finestra di ricerca tratteggiata."}`
      :"Area da configurare in Impostazioni → Impostazioni Controllo Visivo.";
    el("profilo").textContent=`${label} · ${nomeModello}. ${el("profilo").textContent}`;
    el("area-salvata").textContent=p?`${label}: ${p.modo==="prospettiva"?"quattro angoli e prospettiva":"ritaglio rettangolare"}, sorgente ${p.dimensioni?.join("×")||"—"}. ${p.larghezza_cm&&p.lunghezza_cm?`${p.larghezza_cm}×${p.lunghezza_cm} cm.`:"Dimensioni reali non indicate."}`:`${label}: nessuna area salvata in questo browser.`;
  }
  const note = text => { el("stato").textContent = text; el("config-stato").textContent=text; };
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
  function scatto(bounds=null) {
    if (!stream || video.readyState < 2 || !video.videoWidth) throw new Error("Attendere l'anteprima della webcam.");
    const [x,y,right,bottom]=bounds||[0,0,video.videoWidth,video.videoHeight],w=right-x,h=bottom-y;
    const scale = Math.min(1, 1920/w, 1080/h);
    frame.width = Math.round(w*scale); frame.height = Math.round(h*scale);
    frame.getContext("2d").drawImage(video,x,y,w,h,0,0,frame.width,frame.height);
    for(const q of [.96,.9,.82,.74]) {
      const jpeg=frame.toDataURL("image/jpeg",q).split(",")[1];
      if(jpeg.length<=2800000) return jpeg;
    }
    throw new Error("Foto troppo grande: restringi l'area della webcam.");
  }
  function stop() {
    generation++; clearTimeout(timer); request?.abort();
    opening = false;
    el("avvia").disabled = false;
    window.WebcamLocale.ferma("sigillo"); stream = null; video.srcObject = null;
    el("video-box").hidden = true;
    el("foto").disabled = true;
    controlliAI();
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
    el("video-box").classList.toggle("visivo-video--foto",!!frozenImage);
    el("video-box").style.setProperty("--visivo-rapporto",`${w} / ${h}`);
    if (frozenImage) ctx.drawImage(frozenImage, 0, 0, w, h);
    const poly = area || result.area;
    if (poly?.length) {
      ctx.strokeStyle = "#ffcc32"; ctx.lineWidth = 4; ctx.beginPath();
      poly.forEach(([x,y],i) => i ? ctx.lineTo(x*w,y*h) : ctx.moveTo(x*w,y*h));
      if (poly.length === 4) ctx.closePath(); ctx.stroke();
    }
    if(result.borsa?.contorno?.length) {
      ctx.beginPath();result.borsa.contorno.forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));
      ctx.closePath();ctx.strokeStyle="#00c9fc";ctx.lineWidth=3;ctx.stroke();
    }
    if(result.area_campioni?.length===4) {
      const [x0,y0,x1,y1]=result.area_campioni;
      ctx.setLineDash([10,6]);ctx.strokeStyle="#ffcc32";
      ctx.strokeRect(x0*w,y0*h,(x1-x0)*w,(y1-y0)*h);ctx.setLineDash([]);
    }
    ctx.font = `${Math.max(18, w/45)}px sans-serif`;
    centres.forEach(([x,y],i) => {
      const circle = !result.manuale && result.cerchi?.[i];
      if(!result.manuale) for(const contour of result.oggetti?.[i]?.contorni||[]) {
        ctx.beginPath(); contour.forEach(([u,v],n)=>n?ctx.lineTo(u*w,v*h):ctx.moveTo(u*w,v*h));
        ctx.closePath(); ctx.strokeStyle="#52ff9d"; ctx.lineWidth=3; ctx.stroke();
      }
      if (circle) {
        ctx.beginPath(); ctx.arc(x*w,y*h,circle[2]*Math.min(w,h),0,2*Math.PI);
        ctx.strokeStyle = "#52ff9d"; ctx.lineWidth = 3; ctx.stroke();
      }
      ctx.beginPath(); ctx.arc(x*w,y*h,Math.max(12,w/60),0,2*Math.PI);
      ctx.fillStyle = result.motore==="qwen"?"#234e71":"#18342ddd"; ctx.fill();
      ctx.strokeStyle = result.motore==="qwen"?"#00c9fc":"#52ff9d"; ctx.stroke();
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
  async function start(settings=false) {
    stop();
    chiudiCalibrazione();configurando=settings;
    el(settings?"anteprima-impostazioni":"anteprima-sigillo").append(el("video-box"));
    const turn = generation;
    opening = true; el("avvia").disabled = true;
    try {
      if (recoveryFlight) await recoveryFlight.catch(()=>{});
      if (analysisFlight) await analysisFlight.catch(()=>{});
      if (aiFlight) await aiFlight.catch(()=>{});
      if (turn !== generation) return;
      if(!settings)await impostazioni();
      if(turn!==generation)return;
      const device=el("camera").value;
      note(settings?"Anteprima di calibrazione: nessuna operazione RFID.":"Consenti la fotocamera nel browser. Il confronto RFID richiede un conteggio stabile.");
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
      erroreScatto=null;
      aiShot=null; controlliAI();
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
    if(!configurando&&bloccoSpedizione()) {controlliAI();note(bloccoSpedizione());return;}
    try {
      if (!editing && !pending && !sealedPhoto) {
        request = new AbortController();
        analysisFlight = api(configurando?"anteprima_configurazione":"analizza", datiScena(), request.signal);
        const response = await analysisFlight;
        if (turn !== generation) return;
        session = response.sessione;
        if (editing || pending || sealedPhoto) return;
        result = response; centres = result.confermati;
        if(configurando) {draw();controlliAI();return;}
        if(result.qualita==="da riconoscere") {frozen=frozenImage=null;}
        draw();
        const rfid = result.rfid;
        el("conteggi").textContent = `Attesi ${stato.spedizione?.attesi ?? "—"} · Visibili ${result.conteggio===null?"non determinabile":centres.length} (${result.manuale ? "corretti" : "automatici"}) · RFID ${rfid?.trovati ?? stato.presenza?.trovati ?? "—"} · Estranei ${rfid?.unexpected?.length ?? "—"}`;
        if (!recovering && !erroreScatto) note(result.qualita==="da riconoscere"?"Premi Scatta e cerca campioni. Se hai spostato il contenuto è necessario un nuovo scatto."
          : result.qualita==="incerta"?"Il modello segnala incertezza: verifica o correggi i marcatori prima del confronto RFID."
          : result.qualita !== "leggibile" ? "Bordo o immagine non determinabile: illumina la scatola oppure indica i quattro angoli."
          : !result.stabile ? "Attendo che conteggio e posizione siano stabili…"
          : centres.length !== stato.spedizione?.attesi ? "Il numero visivo differisce dalla distinta: verifica contenuto e marcatori."
          : rfid?.concorde ? "Conteggio visivo e identità RFID concordano. Puoi confermare il contenuto e scattare."
          : "Conteggio stabile. " + (!stato.collegato ? "Collega il lettore RFID per il confronto." : "Verifica RFID disponibile."));
        el("foto").disabled = !rfid?.concorde || recovering || !result.stabile;
        controlliAI();
        if (result.stabile && centres.length === stato.spedizione?.attesi && !tried && !recovering && stato.collegato && !stato.occupato) {
          recupera().catch(e => note(e.message));
        }
      }
    } catch(e) { if(turn===generation) {el("foto").disabled=true; if(!erroreScatto)note(e.message);} }
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
    controlliAI();
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
    } finally {recovering=false; stato.occupato=false; stato.presenzaPausa=originalPause;controlliAI();}
  }
  async function freeze() {
    await analysisFlight?.catch(()=>{});
    if(modello()!=="cerchi" && frozenImage) {draw();return;}
    frozen=scatto(); frozenImage=new Image();
    frozenImage.src="data:image/jpeg;base64,"+frozen;
    await frozenImage.decode(); draw();
  }
  async function impostazioni() {
    const response=await api("impostazioni"); const c=response.config;
    consoleModels=response.console_modelli;
    if(c.modello) el("modello").value=c.modello;
    else try {const selected=localStorage.getItem("rfid.visione.motore.v1");if(selected)el("modello").value=selected;}catch(_e){}
    el("box-mark").checked=!!c.marcatori_scatola; el("lid-mark").checked=!!c.marcatori_coperchio;
    el("lid-check").checked=!!c.controlla_coperchio;
    el("min").value=(c.raggio_min ?? .012)*100; el("max").value=(c.raggio_max ?? .16)*100;
    el("soglia").value=c.sensibilita ?? 30;
    if(c.camera) { const o=document.createElement("option"); o.value=c.camera; o.textContent="Videocamera salvata"; el("camera").append(o); el("camera").value=c.camera; }
    else if(Object.prototype.hasOwnProperty.call(c,"camera"))el("camera").value="";
    await aggiornaCamere();
    el("riferimenti").textContent=`Riferimento aperto: ${response.calibrata_aperta?"salvato":"da configurare"}. Riferimento coperchio: ${response.calibrata_chiusa?"salvato":"da configurare"}.`;
    el("config-stato").textContent="Impostazioni caricate. Salva dopo aver modificato videocamera, modello o parametri.";
    controlliAI();
  }
  window.caricaImpostazioniVisive=handle(impostazioni);
  function chiudiCalibrazione() {
    el("calibrazione").src="about:blank";el("calibrazione").hidden=true;
    el("chiudi-calibrazione").hidden=true;controlliAI();
  }
  window.fermaConfigurazioneVisiva=()=>{chiudiCalibrazione();if(configurando){stop();configurando=false;}};
  bind("vai-impostazioni",()=>mostra("impostazioni","controllo-visivo"));
  bind("config-avvia",()=>start(true));bind("config-ferma",()=>{stop();note("Anteprima di calibrazione spenta.");});
  bind("chiudi-calibrazione",chiudiCalibrazione);
  async function nuova() {
    await interrompi();
    await recoveryFlight?.catch(()=>{});
    editing="attesa"; await analysisFlight?.catch(()=>{});
    await api("invalida");
    session=null; result=null; pending=null; sealedPhoto=false; tried=false;
    aiShot=null;
    editing=""; frozen=frozenImage=null;
    el("conferma-foto").hidden=true; el("foto-salvate").replaceChildren();
    note("Prova precedente invalidata. Riprendi con la scatola aperta e ferma.");
    controlliAI();
  }
  async function riconosci(reuse=false) {
    if(aiBusy||recovering||pending||sealedPhoto||!stream||modello()==="cerchi") return;
    if(bloccoSpedizione()) {controlliAI();note(bloccoSpedizione());return;}
    const turn=generation, chosen=modello();
    if(reuse&&!aiShot) throw new Error("Acquisisci prima uno scatto da confrontare.");
    erroreScatto=null;aiBusy=true;editing="riconoscimento";clearTimeout(timer);controlliAI();
    const controller=new AbortController();request=controller;
    const timeout=setTimeout(()=>controller.abort(),chosen==="qwen"?130000:310000);
    note(chosen==="qwen"?"Qwen: invio dello scatto ritagliato a OpenRouter…":"SAM 2: riconoscimento locale dei campioni sullo scatto…");
    try {
      await analysisFlight?.catch(()=>{});if(turn!==generation)return;clearTimeout(timer);
      const candidato=reuse?aiShot:datiScena();
      if(!candidato.profilo) throw new Error("Questa webcam non ha un'area salvata. Apri Impostazioni → Impostazioni Controllo Visivo e configura l'area, poi riattiva la webcam nel Sigillo.");
      aiShot=candidato;
      const photo={...aiShot,modello:chosen};
      tried=false;pending=null;el("foto").disabled=true;
      aiFlight=api("analizza_modello",photo,controller.signal);
      const response=await aiFlight;if(turn!==generation)return;
      session=response.sessione;result=response;centres=response.confermati;area=null;
      frozen=response.immagine_base64;frozenImage=new Image();frozenImage.src="data:image/jpeg;base64,"+frozen;
      await frozenImage.decode();if(turn!==generation)return;draw();
      el("nota-modello").textContent=`${response.modello}: ${response.conteggio??"non determinabile"} campioni in ${response.tempo_secondi??"—"} s. ${response.nota||""} ${(response.avvisi||[]).join(" ")}`;
    } catch(e) {if(turn===generation) {session=null;result=null;centres=[];erroreScatto=e.name==="AbortError"?"Attesa terminata: il servizio potrebbe essere ancora impegnato.":e.message;note(erroreScatto);}}
    finally {clearTimeout(timeout);aiFlight=null;aiBusy=false;controlliAI();if(turn===generation) {editing="";clearTimeout(timer);if(stream)loop(turn);}}
  }
  bind("riconosci",()=>riconosci(false));bind("rianalizza",()=>riconosci(true));
  bind("configura-modelli",async()=>{
    if(!consoleModels) {const settings=await api("impostazioni");consoleModels=settings.console_modelli;}
    if(!consoleModels)throw new Error("Avvia prima il banco SAM 2 / Qwen per configurare l'area.");
    const device=el("camera").value;
    if(!device)throw new Error("Seleziona la videocamera nell'elenco prima di configurare la sua area.");
    stop();configurando=false;
    el("calibrazione").src=consoleModels+"&impostazioni=1&device="+encodeURIComponent(device)+"&motore="+encodeURIComponent(modello());
    el("calibrazione").hidden=false;el("chiudi-calibrazione").hidden=false;
    note("Configura l'area qui sotto. Salva area nel riquadro, poi chiudi la configurazione.");
  });
  el("modello").addEventListener("change",handle(async()=>{
    if(aiBusy||recovering) return;
    editing="attesa";clearTimeout(timer);await analysisFlight?.catch(()=>{});if(!bloccoSpedizione())await api("invalida");
    erroreScatto=null;
    session=null;result=null;centres=[];frozen=frozenImage=null;area=null;tried=false;pending=null;
    el("nota-modello").textContent="Modello cambiato: puoi rianalizzare lo stesso scatto oppure acquisirne uno nuovo.";
    try{localStorage.setItem("rfid.visione.motore.v1",modello());}catch(_e){}
    editing="";controlliAI();if(stream)loop(generation);
  }));
  bind("avvia", start);
  bind("aggiorna-camera", () => aggiornaCamere());
  el("camera").addEventListener("change", handle(async () => {
    area = null;aiShot=null;
    chiudiCalibrazione();controlliAI();
    if (stream || opening) await start(configurando);
  }));
  bind("ferma", () => {stop(); note("Webcam spenta. Le prove confermate rimangono archiviate.");});
  bind("salva", async()=>{
    await api("configura", {camera:el("camera").value, modello:modello(),marcatori_scatola:el("box-mark").checked,
      marcatori_coperchio:el("lid-mark").checked, controlla_coperchio:el("lid-check").checked,
      raggio_min:Number(el("min").value)/100, raggio_max:Number(el("max").value)/100, sensibilita:Number(el("soglia").value)});
    session=null; area=null; note("Configurazione salvata. Se cambia la camera o la modalità dei marcatori, ripeti la calibrazione.");
    controlliAI();
  });
  bind("area", async()=>{editing="area"; area=[]; await freeze(); note("Clicca i quattro angoli interni: alto sinistra, alto destra, basso destra, basso sinistra.");});
  bind("auto-area", ()=>{area=null; editing=""; frozen=frozenImage=null; session=null; tried=false;});
  bind("correggi", async()=>{if(!result?.area) throw new Error("Individua prima il bordo della scatola."); editing="centri"; await freeze(); el("conferma-correzioni").hidden=false; note("Clicca per aggiungere/togliere; trascina un numero per spostarlo. Poi conferma i marcatori.");});
  bind("conferma-correzioni", async()=>{
    const response=await api("correggi", {centri:centres}); session=response.sessione;
    if(result) result.manuale=true;
    editing=""; if(modello()==="cerchi")frozen=frozenImage=null; el("conferma-correzioni").hidden=true; tried=false;
  });
  for(const tipo of ["aperta","chiusa"]) bind("calibra-"+tipo, async()=>{
    const roi=area || result?.area; if(!roi || roi.length!==4) throw new Error("Indica prima i quattro angoli della scatola.");
    await api("calibra", {tipo, area:roi,...datiScena()}); session=null;
    el("riferimenti").textContent=`Riferimento ${tipo} salvato.`;
    note(`Riferimento ${tipo} salvato. Il conteggio sarà ripetuto.`);
  });
  bind("recupera", recupera); bind("interrompi", interrompi); bind("nuovo", nuova);
  bind("foto", async()=>{
    editing="foto";
    try {
      await analysisFlight?.catch(()=>{});
      pending=await api("prepara_foto", datiScena());controlliAI();
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
    controlliAI();
    note("Foto del contenuto conservata. Ora posiziona il coperchio e, se previsto, verifica il coperchio. Poi certifica la scatola chiusa.");
  });
  bind("coperchio", async()=>{
    const response=await api("foto_coperchio", datiScena());
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
      if(configurando) {
        sid=stato.spedizione?.shipment_id;contentKey=key;
        el("attivo").checked=false;el("pannello").hidden=true;el("foto-salvate").replaceChildren();
        controlliAI();return;
      }
      stop(); sid=stato.spedizione?.shipment_id; contentKey=key; session=null; sealedPhoto=false;
      result=null; tried=false; pending=null; area=null;
      aiShot=null;controlliAI();
      el("attivo").checked=false; el("pannello").hidden=true; el("foto-salvate").replaceChildren();
    }
    if(!configurando&&bloccoSpedizione()&&stream)stop();
    controlliAI();
  };
  document.addEventListener("visibilitychange",()=>{if(document.hidden) {chiudiCalibrazione();stop();}});
  window.addEventListener("pagehide",()=>{chiudiCalibrazione();stop();});
  window.addEventListener("storage",e=>{if(e.key?.startsWith("rfid.sam2.area.v2."))controlliAI();});

  function mostraProva(prova,target) {
    target.replaceChildren();
    const p=document.createElement("p");
    p.textContent=prova ? `Controllo: ${prova.stato}; operatore ${prova.operatore||"—"}; ${prova.quando||""}. Visibili: ${prova.confermati?.length ?? "—"}, RFID: ${prova.rfid?.trovati ?? "—"}. Coperchio: ${prova.coperchio?.stato||"non richiesto"}.` : "Nessuna prova visiva allegata.";
    if(prova?.sigillo_finale) p.textContent+=` Confronto RFID finale: ${prova.sigillo_finale.concorde ? "concordante" : "non concordante"}.`;
    if(prova?.riconoscimento?.modello) p.textContent+=` Ricerca campioni: ${prova.riconoscimento.modello}.`;
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
