/* Prova ottica autonoma. Nessuna operazione RFID o archivio. */
(() => {
  "use strict";
  const el = id => document.getElementById(id), video = el("video"), overlay = el("overlay");
  const frame = document.createElement("canvas"), params = new URLSearchParams(location.search), token = params.get("t");
  const endpoint = params.get("port") === "8771" ? "http://127.0.0.1:8771/api/anteprima" : "/api/anteprima";
  let stream = null, generation = 0, timer = null, request = null, result = null, area = null, editing = false, opening = false;
  let tracks = [], nextId = 0, lastArea = null, sampled = false, selection = "", centre = null;
  let revision = 0;
  const note = text => { el("stato").textContent = text; };
  const dimensions = () => el("risoluzione").value === "720" ? [1280,720] : [1920,1080];
  function videoQuality(width = 0, height = 0, jpeg = 0, elapsed = 0) {
    el("qualita-video").textContent = `Webcam: ${video.videoWidth}×${video.videoHeight}` +
      (width ? ` · Analisi: ${width}×${height} · JPEG ${Math.round(jpeg*100)}% · ${Math.round(elapsed)} ms` : "") +
      (video.videoWidth < dimensions()[0] || video.videoHeight < dimensions()[1] ? " · La webcam sta fornendo meno pixel della risoluzione richiesta." : "");
  }
  function inside(p, polygon) {
    if (!polygon?.length) return true;
    let value = false;
    for (let i=0,j=polygon.length-1;i<polygon.length;j=i++) {
      const [x,y]=polygon[i], [a,b]=polygon[j];
      if ((y>p[1]) !== (b>p[1]) && p[0] < (a-x)*(p[1]-y)/(b-y)+x) value=!value;
    }
    return value;
  }
  function resetDetections() { tracks = []; lastArea = null; result = null; revision++; }
  function stable(data) {
    const border = data.area;
    if (!border || data.qualita === "non determinabile") {
      resetDetections(); return {...data, cerchi:[], centri:[], candidati:0};
    }
    if (!lastArea || border.some((p,i)=>Math.hypot(p[0]-lastArea[i][0],p[1]-lastArea[i][1]) > .03)) tracks = [];
    lastArea = border;
    const available = new Set(tracks);
    const current = (data.cerchi || []).map(circle=>{
      const [x,y,r] = circle;
      let match = null, distance = Infinity;
      for (const track of available) {
        const [a,b,s] = track.circle, d = Math.hypot(x-a,y-b);
        if (d < Math.min(.035,Math.max(.01,r*.4)) && Math.abs(r-s) <= Math.max(r,s)*.2 && d < distance) {
          match = track; distance = d;
        }
      }
      if (match) { available.delete(match); return {id:match.id, circle, seen:match.seen+1}; }
      return {id:++nextId, circle, seen:1};
    });
    tracks = current;
    const confirmed = current.filter(t=>t.seen>=3).sort((a,b)=>a.id-b.id).map(t=>t.circle);
    return {...data, cerchi:confirmed, centri:confirmed.map(([x,y])=>[x,y]), candidati:current.length};
  }
  async function cameras(selected = el("camera").value) {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const options = devices.filter(d=>d.kind==="videoinput" && d.deviceId).map((d,i)=>{
      const o=document.createElement("option");o.value=d.deviceId;o.textContent=d.label||`Webcam ${i+1}`;
      return o;
    });
    if (!options.length) return;
    el("camera").replaceChildren(...options);
    el("camera").value = options.some(o=>o.value===selected) ? selected : options[0].value;
  }
  function stop() {
    generation++; clearTimeout(timer); request?.abort();
    opening = false;
    stream?.getTracks().forEach(t => t.stop()); stream = null; video.srcObject = null;
    resetDetections(); sampled=false; overlay.getContext("2d").clearRect(0,0,overlay.width,overlay.height);
    el("piccolo").disabled = el("grande").disabled = true;
    el("avvia").disabled = false; el("ferma").disabled = true; el("manuale").disabled = true;
  }
  function draw() {
    overlay.width = video.videoWidth || 1280; overlay.height = video.videoHeight || 720;
    const ctx = overlay.getContext("2d"), w = overlay.width, h = overlay.height;
    if (sampled) ctx.drawImage(frame,0,0,w,h);
    const border = area || result?.area;
    if (border?.length) {
      ctx.strokeStyle = "#ffce32"; ctx.lineWidth = 5; ctx.beginPath();
      border.forEach(([x,y],i) => i ? ctx.lineTo(x*w,y*h) : ctx.moveTo(x*w,y*h));
      if (border.length === 4) ctx.closePath(); ctx.stroke();
    }
    if (editing) {
      if (centre) {ctx.beginPath();ctx.arc(centre[0]*w,centre[1]*h,8,0,2*Math.PI);ctx.strokeStyle="#42b8ff";ctx.stroke();}
      return;
    }
    ctx.font = "bold 28px sans-serif";
    (result?.cerchi || []).forEach(([x,y,r], i) => {
      ctx.beginPath(); ctx.arc(x*w,y*h,r*Math.min(w,h),0,2*Math.PI);
      ctx.strokeStyle = "#39ff85"; ctx.lineWidth = 4; ctx.stroke();
      ctx.fillStyle = "#173f2dcc"; ctx.fillRect(x*w-16,y*h-20,34,38);
      ctx.fillStyle = "white"; ctx.fillText(String(i+1),x*w-9,y*h+8);
    });
  }
  async function loop(turn) {
    if (turn !== generation || !stream) return;
    const started = performance.now();
    try {
      if (editing || video.readyState < 2 || !video.videoWidth) return;
      const [width,height] = dimensions();
      const scale = Math.min(1, width/video.videoWidth, height/video.videoHeight);
      frame.width = Math.round(video.videoWidth*scale); frame.height = Math.round(video.videoHeight*scale);
      frame.getContext("2d").drawImage(video,0,0,frame.width,frame.height);
      sampled = true;
      let image, jpeg;
      for (const quality of [.96,.90,.82,.74]) {
        image = frame.toDataURL("image/jpeg",quality).split(",")[1]; jpeg = quality;
        if (image.length <= 2_800_000) break;
      }
      if (image.length > 2_800_000) throw new Error("Fotogramma troppo grande: scegli HD oppure riduci il rumore illuminando la borsa.");
      const version = revision;
      const controller = new AbortController(); request = controller;
      const deadline = setTimeout(()=>controller.abort(),8000);
      let response;
      try { response = await fetch(endpoint, {method:"POST", headers:{"Content-Type":"application/json","X-RFID-Token":token},
        signal:controller.signal, body:JSON.stringify({immagine_base64:image,
          raggio_min:Number(el("min").value)/100,raggio_max:Number(el("max").value)/100,sensibilita:Number(el("soglia").value),
          ...(area?.length===4 ? {area_manuale:area} : {})})}); }
      finally { clearTimeout(deadline); }
      const data = await response.json();
      if (turn !== generation || editing || version !== revision) return;
      if (!response.ok) throw new Error(data.errore);
      videoQuality(frame.width,frame.height,jpeg,performance.now()-started);
      result = stable(data); draw();
      el("conteggio").textContent = `Riconosciuti ${result.centri.length} / ${el("attesi").value}`;
      note(data.area ? `Borsa: ${data.origine_area}. ${result.centri.length} coperchi stabili; ${result.candidati-result.centri.length} in verifica. Conferma su 3 fotogrammi.`
        : "Bordo non determinabile: inquadra tutta la borsa oppure premi Indica il bordo della borsa.");
    } catch(e) { if (turn === generation) {resetDetections();draw();el("conteggio").textContent=`Riconosciuti — / ${el("attesi").value}`;note(e.name === "AbortError" ? "Analisi troppo lenta: riprovo sul prossimo fotogramma." : e.message);} }
    finally {
      if (turn === generation && stream) {
        const interval = Math.max(1000,Math.min(2500,Number(el("intervallo").value)||1500));
        timer = setTimeout(()=>loop(turn),Math.max(200,interval-(performance.now()-started)));
      }
    }
  }
  async function start() {
    stop(); const turn = generation; el("avvia").disabled=true; el("ferma").disabled=false;
    opening = true;
    note("Apertura webcam…");
    try {
      const camera = el("camera").value;
      const [width,height] = dimensions();
      const capture = navigator.mediaDevices.getUserMedia({audio:false,video:{width:{ideal:width},height:{ideal:height},frameRate:{ideal:15},...(camera?{deviceId:{exact:camera}}:{})}}).then(media=>{
        if(turn!==generation){media.getTracks().forEach(t=>t.stop());throw new DOMException("Apertura annullata","AbortError");}
        return media;
      });
      let deadline;
      let media;
      try { media = await Promise.race([capture,new Promise((_,reject)=>{deadline=setTimeout(()=>reject(new Error("La webcam non risponde: controlla il consenso di Chrome, scegli la C920 e riprova.")),20000);})]); }
      finally { clearTimeout(deadline); }
      if (turn !== generation) {media.getTracks().forEach(t=>t.stop());return;}
      stream=media; video.srcObject=media; await video.play();
      await cameras(media.getVideoTracks()[0].getSettings().deviceId);
      if(turn !== generation) return;
      videoQuality();
      result=null; area=null; editing=false; opening=false; el("manuale").disabled=false;
      tracks=[]; lastArea=null; nextId=0; sampled=false; selection=""; centre=null;
      el("piccolo").disabled=el("grande").disabled=false;
      media.getVideoTracks()[0].addEventListener("ended",()=>{if(turn===generation){stop();note("Webcam scollegata.");}});
      loop(turn);
    } catch(e) {if(turn===generation){stop();note(e.message);}}
  }
  el("avvia").addEventListener("click",start);
  el("ferma").addEventListener("click",()=>{stop();note("Webcam spenta.");});
  el("camera").addEventListener("change",()=>{if(stream||opening)start();});
  el("risoluzione").addEventListener("change",async()=>{
    if (opening) return start();
    if (!stream) return;
    const turn=generation,track=stream.getVideoTracks()[0],[width,height]=dimensions();
    resetDetections();editing=false;selection="";centre=null;
    try {
      await track.applyConstraints({width:{ideal:width},height:{ideal:height},frameRate:{ideal:15}});
      if(turn===generation) {resetDetections();sampled=false;draw();videoQuality();note("Risoluzione aggiornata. Attendo tre fotogrammi coerenti.");}
    } catch(e) {if(turn===generation){videoQuality();note(`La webcam non ha applicato la risoluzione richiesta: ${e.message}`);}}
  });
  el("manuale").addEventListener("click",()=>{editing=true;selection="area";centre=null;area=[];resetDetections();draw();note("Clicca i quattro angoli del fondo bianco, in ordine lungo il bordo, iniziando in alto a sinistra.");});
  el("automatico").addEventListener("click",()=>{editing=false;selection="";centre=null;area=null;resetDetections();draw();});
  for (const size of ["piccolo","grande"]) el(size).addEventListener("click",()=>{
    if (!sampled) return;
    editing=true;selection=size;centre=null;tracks=[];revision++;
    draw();note(`Campione più ${size}: clicca il centro del coperchio, poi un punto sulla circonferenza.`);
  });
  for (const id of ["min","max","soglia"]) el(id).addEventListener("change",()=>{resetDetections();draw();});
  overlay.addEventListener("pointerdown",e=>{
    if(!editing)return; const rect=overlay.getBoundingClientRect(),p=[(e.clientX-rect.left)/rect.width,(e.clientY-rect.top)/rect.height];
    if (selection === "area") {
      area.push(p);draw();
      if(area.length===4){editing=false;selection="";note("Bordo indicato: riprendo il conteggio.");}
    } else {
      if (!centre) {
        if (!inside(p,area || result?.area)) {note("Clicca il centro di un coperchio dentro il bordo giallo della borsa.");return;}
        centre=p;draw();note("Ora clicca un punto sulla circonferenza dello stesso coperchio.");return;
      }
      const radius = Math.hypot((p[0]-centre[0])*overlay.width,(p[1]-centre[1])*overlay.height)/Math.min(overlay.width,overlay.height)*100;
      const value = radius*(selection==="piccolo"?.8:1.2);
      if (value < (selection==="piccolo"?.5:1) || value > (selection==="piccolo"?20:40)) {centre=null;draw();note("Dimensione fuori intervallo: clicca di nuovo centro e circonferenza.");return;}
      const field = selection==="piccolo"?"min":"max";
      if ((field==="min" && value>=Number(el("max").value)) || (field==="max" && value<=Number(el("min").value))) {centre=null;draw();note("Dimensioni incoerenti: regola l'altro limite o indica nuovamente il coperchio.");return;}
      el(field).value=value.toFixed(1);editing=false;selection="";centre=null;resetDetections();draw();
      el("calibrazione").textContent=`Dimensioni cercate: raggio ${el("min").value}–${el("max").value}% del lato corto. Margine incluso sui campioni indicati.`;
      note("Dimensione calibrata. Attendo tre fotogrammi coerenti per confermare i campioni.");
    }
  });
  document.addEventListener("visibilitychange",()=>{if(document.hidden)stop();});
  window.addEventListener("pagehide",stop);
  cameras().then(()=>{
    if(params.get("camera")==="c920") {
      const preferred=Array.from(el("camera").options).find(o=>o.textContent.includes("C920"));
      if(preferred) el("camera").value=preferred.value;
    }
    if(params.get("auto")==="1") return start();
  }).catch(e=>note(e.message));
})();
