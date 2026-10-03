/* Configurazione del ritaglio e singolo scatto automatico; nessun dato RFID. */
(() => {
  "use strict";
  const el=id=>document.getElementById(id), video=el("video"), canvas=el("overlay");
  const full=document.createElement("canvas"), frame=document.createElement("canvas");
  const params=new URLSearchParams(location.search), token=params.get("t");
  if(params.get("impostazioni")==="1") {
    document.title="Area e prospettiva della videocamera";
    document.querySelector("h1").textContent="Area e prospettiva della videocamera";
    el("camera").closest("label").hidden=true;el("motore").closest(".comandi").hidden=true;
    el("manuale").parentElement.hidden=true;
    el("descrizione").textContent="Attiva la videocamera scelta nelle Impostazioni, indica l'area e salva. Poi torna al Sigillo per cercare i campioni.";
    el("scatta").closest(".comandi").hidden=true;
    for(const id of ["descrizione-modelli","conteggio","risultati","legenda"])el(id).hidden=true;
  }
  const endpoint=params.get("port")==="8772"?"http://127.0.0.1:8772/api/sam2/automatico":"/api/sam2/automatico";
  try{const wanted=params.get("motore")??localStorage.getItem("rfid.visione.motore.v1");el("motore").value=["qwen","sam2"].includes(wanted)?wanted:"yolo";}catch(_e){el("motore").value="yolo";}
  el("manuale").href="sam2.html"+location.search;
  let stream=null, opening=false, generation=0, revision=0, raf=null, busy=false, request=null, deviceMissing=false;
  let profile=null, draft=null, corners=[], dragging=null, configuring=false, frozen=false, result=null, started=0;
  let snapshot=null,snapshotCorrection=null;
  const note=text=>{el("stato").textContent=text;};
  const key=()=>"rfid.sam2.area.v2."+el("camera").value;
  function validArea(a){return Array.isArray(a)&&a.length===4&&a.every(v=>Number.isFinite(v)&&v>=0&&v<=1)&&a[2]-a[0]>=.03&&a[3]-a[1]>=.03;}
  function validCorners(points){
    if(!Array.isArray(points)||points.length!==4||points.some(p=>!Array.isArray(p)||p.length!==2||p.some(v=>!Number.isFinite(v)||v<0||v>1)))return false;
    const area=points.reduce((sum,a,i)=>{const b=points[(i+1)%4];return sum+a[0]*b[1]-b[0]*a[1];},0)/2;
    return area>=.02&&points.every((a,i)=>{const b=points[(i+1)%4],c=points[(i+2)%4];return (b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0])>.0001;});
  }
  function compatible(p){return p&&p.versione===2&&validArea(p.area)&&(p.modo==="rettangolo"||(p.modo==="prospettiva"&&validCorners(p.punti)))&&Array.isArray(p.dimensioni)&&p.dimensioni.length===2&&p.dimensioni.every(v=>Number.isFinite(v)&&v>=64)&&Math.abs(p.dimensioni[0]/p.dimensioni[1]-video.videoWidth/video.videoHeight)<.02;}
  function loadProfile(){
    profile=null;
    try{const saved=JSON.parse(localStorage.getItem(key()));if(compatible(saved))profile=saved;}catch(_e){/* Memoria del browser non disponibile o profilo non valido. */}
    if(profile){el("modo-area").value=profile.modo;el("larghezza").value=profile.larghezza_cm??"";el("lunghezza").value=profile.lunghezza_cm??"";el("altezza-camera").value=profile.altezza_camera_cm??"";}
    describe();
  }
  function box(source,a){
    const w=source.videoWidth||source.width,h=source.videoHeight||source.height;
    const x=Math.round(a[0]*w),y=Math.round(a[1]*h),right=Math.round(a[2]*w),bottom=Math.round(a[3]*h);
    return [x,y,right-x,bottom-y];
  }
  function crop(source){
    const [x,y,w,h]=box(source,profile.area),scale=Math.min(1,1920/w,1080/h);
    frame.width=Math.max(1,Math.round(w*scale));frame.height=Math.max(1,Math.round(h*scale));
    frame.getContext("2d").drawImage(source,x,y,w,h,0,0,frame.width,frame.height);
  }
  function describe(){
    if(!profile){el("profilo").textContent="Area da configurare: trascina un rettangolo attorno alla borsa sull’inquadratura completa.";return;}
    const [,,w,h]=box(video,profile.area);
    const mode=profile.modo==="prospettiva"?"Quattro angoli salvati · rettifica prospettica allo scatto":"Ritaglio rettangolare salvato";
    el("profilo").textContent=`${mode} per questa webcam · ${w}×${h} pixel nell’inquadratura ${video.videoWidth}×${video.videoHeight}.`;
  }
  function update(){
    el("avvia").disabled=deviceMissing||!!stream||opening;el("ferma").disabled=!stream&&!opening;
    el("camera").disabled=busy||opening||configuring;
    el("motore").disabled=busy||opening||configuring;
    el("configura").disabled=!stream||busy||configuring;
    el("salva").disabled=!configuring||(el("modo-area").value==="prospettiva"?!validCorners(corners):!validArea(draft));
    el("annulla-angolo").disabled=!configuring||!corners.length;
    el("annulla-area").disabled=!configuring;
    el("reset-area").disabled=!profile||busy||configuring;
    ["modo-area","larghezza","lunghezza","altezza-camera"].forEach(id=>{el(id).disabled=busy||(!configuring&&!!profile);});
    el("scatta").disabled=!stream||!profile||configuring||busy||frozen;
    el("riconta").disabled=!frozen||!snapshot||configuring||busy;
    el("nuova").disabled=!frozen||configuring;
    canvas.classList.toggle("configura",configuring);
    el("conteggio").textContent=`Campioni riconosciuti: ${result ? (result.conteggio===null?"non determinabile":result.conteggio) : "—"}`;
  }
  function invalidate(){revision++;result=null;el("risultati").replaceChildren();update();}
  function outline(ctx,poly,w,h){ctx.beginPath();poly.forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));ctx.closePath();ctx.stroke();}
  function render(){
    if(configuring){canvas.width=full.width;canvas.height=full.height;}
    else if(frozen){canvas.width=frame.width;canvas.height=frame.height;}
    else if(stream&&video.readyState>=2&&video.videoWidth){
      if(profile){crop(video);canvas.width=frame.width;canvas.height=frame.height;}
      else{canvas.width=video.videoWidth;canvas.height=video.videoHeight;}
    }else return;
    const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
    ctx.drawImage(configuring?full:(frozen||profile?frame:video),0,0,w,h);
    if(configuring&&draft){
      const [x,y,right,bottom]=draft;
      ctx.fillStyle="#101f2490";
      ctx.fillRect(0,0,w,y*h);ctx.fillRect(0,bottom*h,w,(1-bottom)*h);
      ctx.fillRect(0,y*h,x*w,(bottom-y)*h);ctx.fillRect(right*w,y*h,(1-right)*w,(bottom-y)*h);
      ctx.strokeStyle="#ffd34e";ctx.lineWidth=4;ctx.strokeRect(x*w,y*h,(right-x)*w,(bottom-y)*h);
    }
    const guides=configuring&&el("modo-area").value==="prospettiva"?corners:(!frozen&&profile?.modo==="prospettiva"?profile.punti.map(([x,y])=>[(x-profile.area[0])/(profile.area[2]-profile.area[0]),(y-profile.area[1])/(profile.area[3]-profile.area[1])]):[]);
    if(guides.length){
      ctx.strokeStyle="#ffd34e";ctx.lineWidth=4;ctx.beginPath();guides.forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));if(guides.length===4)ctx.closePath();ctx.stroke();ctx.font="bold 22px sans-serif";
      guides.forEach(([x,y],i)=>{ctx.fillStyle="#ffd34e";ctx.beginPath();ctx.arc(x*w,y*h,7,0,Math.PI*2);ctx.fill();ctx.fillText(String(i+1),x*w+10,y*h-10);});
    }
    if(!frozen||!result)return;
    ctx.lineWidth=3;ctx.setLineDash([]);
    if(result.borsa?.rilevata){ctx.strokeStyle="#00d6ed";outline(ctx,result.borsa.contorno,w,h);}
    if(result.area_campioni){const [x,y,r,b]=result.area_campioni;ctx.strokeStyle="#c8e4e0";ctx.setLineDash([10,8]);ctx.strokeRect(x*w,y*h,(r-x)*w,(b-y)*h);ctx.setLineDash([]);}
    ctx.font="bold 22px sans-serif";
    (result.oggetti||[]).forEach(obj=>{
      ctx.strokeStyle="#22dd77";(obj.contorni||[]).forEach(poly=>outline(ctx,poly,w,h));
      const [x,y]=obj.centro;ctx.fillStyle=result.tipo_overlay==="centri"?"#234e71":"#123e29";ctx.beginPath();ctx.arc(x*w,y*h,15,0,Math.PI*2);ctx.fill();
      if(result.tipo_overlay==="centri"){ctx.strokeStyle="#00c9fc";ctx.stroke();}
      ctx.fillStyle="white";ctx.fillText(String(obj.id),x*w-6,y*h+7);
    });
  }
  function preview(){if(!frozen&&!configuring)render();raf=requestAnimationFrame(preview);}
  async function cameras(selected=el("camera").value){
    const devices=await navigator.mediaDevices.enumerateDevices();
    const options=devices.filter(d=>d.kind==="videoinput"&&d.deviceId).map((d,i)=>{const o=document.createElement("option");o.value=d.deviceId;o.textContent=d.label||`Webcam ${i+1}`;return o;});
    if(options.length){el("camera").replaceChildren(...options);el("camera").value=options.some(o=>o.value===selected)?selected:options[0].value;}
  }
  function stop(){
    generation++;opening=false;stream?.getTracks().forEach(t=>t.stop());stream=null;video.srcObject=null;
    if(raf!==null)cancelAnimationFrame(raf);raf=null;update();
  }
  async function start(){
    if(opening||stream)return;
    const turn=++generation;opening=true;update();note("Attendo il consenso alla webcam…");let timer;
    try{
      const pending=navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:el("camera").value?{exact:el("camera").value}:undefined,width:{ideal:1920},height:{ideal:1080},frameRate:{ideal:15}}});
      pending.then(s=>{if(turn!==generation)s.getTracks().forEach(t=>t.stop());},()=>{});
      const s=await Promise.race([pending,new Promise((_,reject)=>{timer=setTimeout(()=>{if(turn===generation)stop();reject(new Error("Consenso non ricevuto: riprova Attiva webcam."));},20000);})]);
      if(turn!==generation)return;
      stream=s;video.srcObject=s;await video.play();if(turn!==generation)return;
      await cameras(s.getVideoTracks()[0].getSettings().deviceId);
      loadProfile();el("qualita").textContent=`Webcam: ${video.videoWidth}×${video.videoHeight}. L’analisi usa solo il ritaglio, senza ingrandimento artificiale dei pixel.`;
      if(raf===null)preview();note(params.get("impostazioni")==="1"?(profile?"Area salvata ripristinata. Puoi modificarla oppure chiudere la configurazione.":"Seleziona l’area sull’inquadratura completa e salva."):(profile?"Area salvata ripristinata. Premi Scatta e conta.":"Seleziona l’area sull’inquadratura completa prima di scattare."));
    }catch(e){if(turn===generation){stop();note(e.message);}else if(!opening&&!stream)note(e.message);}
    finally{clearTimeout(timer);if(turn===generation)opening=false;update();}
  }
  function configure(){
    if(!stream||busy||video.readyState<2)return;
    full.width=video.videoWidth;full.height=video.videoHeight;full.getContext("2d").drawImage(video,0,0);
    frozen=false;configuring=true;draft=profile?.modo==="rettangolo"?profile.area.slice():null;corners=profile?.punti?.map(p=>p.slice())||[];invalidate();render();selectionNote();
  }
  function selectionNote(){const names=["alto sinistra","alto destra","basso destra","basso sinistra"];note(el("modo-area").value==="prospettiva"?(corners.length<4?`Clicca l’angolo ${names[corners.length]} del bordo superiore (${corners.length+1}/4).`:"Quattro angoli indicati: verifica l’ordine e premi Salva area."):"Trascina il rettangolo che comprende tutta la borsa, poi premi Salva area.");}
  function point(e){const r=canvas.getBoundingClientRect();return [Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)),Math.max(0,Math.min(1,(e.clientY-r.top)/r.height))];}
  canvas.addEventListener("pointerdown",e=>{if(!configuring||busy)return;if(el("modo-area").value==="prospettiva"){if(corners.length===4)corners=[];corners.push(point(e));render();update();selectionNote();return;}dragging={start:point(e),previous:draft,id:e.pointerId};canvas.setPointerCapture(e.pointerId);draft=null;update();});
  canvas.addEventListener("pointermove",e=>{if(!dragging||e.pointerId!==dragging.id)return;const [x,y]=point(e),[sx,sy]=dragging.start;draft=[Math.min(x,sx),Math.min(y,sy),Math.max(x,sx),Math.max(y,sy)];render();update();});
  canvas.addEventListener("pointerup",e=>{if(!dragging||e.pointerId!==dragging.id)return;const [x,y]=point(e),[sx,sy]=dragging.start;draft=[Math.min(x,sx),Math.min(y,sy),Math.max(x,sx),Math.max(y,sy)];dragging=null;render();update();});
  canvas.addEventListener("pointercancel",()=>{if(dragging){draft=dragging.previous;dragging=null;render();update();}});
  function save(){
    if(!configuring)return;
    const mode=el("modo-area").value;
    if(mode==="prospettiva"){if(!validCorners(corners)){note("Indica quattro angoli in senso orario, senza incrociare i lati.");return;}draft=[Math.min(...corners.map(p=>p[0])),Math.min(...corners.map(p=>p[1])),Math.max(...corners.map(p=>p[0])),Math.max(...corners.map(p=>p[1]))];}
    if(!validArea(draft))return;
    const dimensions=[el("larghezza").value.trim(),el("lunghezza").value.trim()],cameraHeight=el("altezza-camera").value.trim();
    if(dimensions.some(v=>v)&&!dimensions.every(v=>v&&Number.isFinite(Number(v))&&Number(v)>=.1&&Number(v)<=1000)){note("Compila entrambe le dimensioni in cm oppure lasciale vuote.");return;}
    if(dimensions.every(v=>v)&&(Number(dimensions[0])/Number(dimensions[1])<.2||Number(dimensions[0])/Number(dimensions[1])>5)){note("Rapporto larghezza/lunghezza fuori intervallo (0,2–5).");return;}
    if(cameraHeight&&(!Number.isFinite(Number(cameraHeight))||Number(cameraHeight)<.1||Number(cameraHeight)>1000)){note("Altezza della camera non valida.");return;}
    const [,,w,h]=box(full,draft);if(Math.min(w,h)<100){note("Area troppo piccola: seleziona almeno 100 pixel per lato.");return;}
    const saved={versione:2,modo:mode,area:draft.slice(),punti:mode==="prospettiva"?corners.map(p=>p.slice()):null,dimensioni:[full.width,full.height],larghezza_cm:dimensions[0]?Number(dimensions[0]):null,lunghezza_cm:dimensions[1]?Number(dimensions[1]):null,altezza_camera_cm:cameraHeight?Number(cameraHeight):null};
    try{localStorage.setItem(key(),JSON.stringify(saved));}catch(_e){note("Impossibile salvare l’area nel browser. Consenti la memoria locale e riprova.");return;}
    profile=saved;configuring=false;dragging=null;describe();invalidate();render();note(params.get("impostazioni")==="1"?"Area salvata. Chiudi la configurazione e torna al Sigillo per cercare i campioni.":"Area salvata. L’anteprima ora mostra solo il ritaglio. Premi Scatta e conta.");
  }
  async function capture(reuse=false){
    if(params.get("impostazioni")==="1"||busy||configuring||(reuse?(!frozen||!snapshot):(!stream||!profile||frozen||video.readyState<2)))return;
    if(!reuse&&!compatible(profile)){profile=null;describe();update();note("L’inquadratura è cambiata: seleziona di nuovo l’area.");return;}
    if(!reuse){crop(video);snapshot=null;snapshotCorrection=null;}let photo=reuse?snapshot:null;
    const mode=["qwen","yolo"].includes(el("motore").value)?el("motore").value:"sam2";
    const selectedEndpoint=mode==="sam2"?endpoint:endpoint.replace("/sam2/automatico",`/${mode}/automatico`);
    const perspective=!reuse&&profile.modo==="prospettiva";
    let correctedPoints=null;
    if(perspective){
      // Ritaglia prima di inviare: conserva il dettaglio anche con sorgenti 4K.
      const [l,t,r,b]=profile.area,px=(r-l)*.06,py=(b-t)*.06;
      const expanded=[Math.max(0,l-px),Math.max(0,t-py),Math.min(1,r+px),Math.min(1,b+py)];
      const [x,y,w,h]=box(video,expanded),scale=Math.min(1,1920/w,1080/h);
      full.width=Math.round(w*scale);full.height=Math.round(h*scale);
      full.getContext("2d").drawImage(video,x,y,w,h,0,0,full.width,full.height);
      correctedPoints=profile.punti.map(([u,v])=>[(u*video.videoWidth-x)/w,(v*video.videoHeight-y)/h]);
    }
    const source=perspective?full:frame;
    if(!reuse)for(const q of [.96,.90,.82,.74]){photo=source.toDataURL("image/jpeg",q).split(",")[1];if(photo.length<=2800000)break;}
    if(photo.length>2800000){note("Foto troppo grande: riduci l’area o la risoluzione della webcam.");return;}
    frozen=true;invalidate();render();busy=true;started=Date.now();const rev=revision,controller=new AbortController();request=controller;update();
    const waiting=()=>note(`${{qwen:"Qwen: invio dello scatto a OpenRouter e conteggio visuale",yolo:"YOLO: rilevamento locale dei contenitori"}[mode]??"SAM 2: analisi locale del bordo e dei campioni"}. Trascorsi ${Math.round((Date.now()-started)/1000)} s.`);
    waiting();const ticker=setInterval(()=>{if(rev===revision)waiting();},1000),timeout=setTimeout(()=>controller.abort(),{qwen:120000,yolo:60000}[mode]??300000);
    try{
      let correction=reuse?snapshotCorrection:null;
      if(perspective){
        const prepared=await fetch(endpoint.replace("/automatico","/prepara"),{method:"POST",headers:{"Content-Type":"application/json","X-RFID-Token":token||""},body:JSON.stringify({immagine_base64:photo,calibrazione:{punti:correctedPoints,larghezza_cm:profile.larghezza_cm,lunghezza_cm:profile.lunghezza_cm,altezza_camera_cm:profile.altezza_camera_cm}}),signal:controller.signal});
        const data=await prepared.json();if(!prepared.ok)throw new Error(data.errore||"Rettifica non riuscita");if(rev!==revision)return;
        const image=new Image();image.src="data:image/jpeg;base64,"+data.immagine_base64;await image.decode();if(rev!==revision)return;
        frame.width=data.dimensioni[0];frame.height=data.dimensioni[1];frame.getContext("2d").drawImage(image,0,0);photo=data.immagine_base64;correction=data.correzione;render();
      }
      snapshot=photo;snapshotCorrection=correction;
      const response=await fetch(selectedEndpoint,{method:"POST",headers:{"Content-Type":"application/json","X-RFID-Token":token||""},body:JSON.stringify({immagine_base64:photo}),signal:controller.signal});
      const data=await response.json();if(!response.ok)throw new Error(data.errore||"Analisi non riuscita");if(rev!==revision)return;
      result=data;const messages=[data.nota,correction?.nota,...(correction?.avvisi||[]),...(data.avvisi||[])].filter(Boolean);
      el("risultati").replaceChildren(...messages.map(text=>{const p=document.createElement("p");p.textContent=text;return p;}));
      const timing=data.tempo_bordo_secondi==null?"":` · bordo ${data.tempo_bordo_secondi} s · conteggio ${data.tempo_conteggio_secondi??"—"} s`;
      note(`Analisi completata in ${data.tempo_secondi} s${timing} · ${data.modello||"SAM 2"}. Verifica ${data.tipo_overlay==="centri"?"i punti numerati":"i contorni"}.`);render();
    }catch(e){if(rev===revision)note(e.name==="AbortError"?"Attesa terminata. Il server potrebbe essere ancora impegnato: attendi prima di riprovare.":e.message);}
    finally{clearInterval(ticker);clearTimeout(timeout);busy=false;if(request===controller)request=null;update();}
  }
  el("avvia").addEventListener("click",start);el("ferma").addEventListener("click",()=>{stop();note(busy?"Webcam spenta; l’analisi della foto continua.":"Webcam spenta; il risultato resta disponibile.");});
  el("configura").addEventListener("click",configure);el("salva").addEventListener("click",save);
  el("modo-area").addEventListener("change",()=>{draft=null;corners=[];dragging=null;render();update();if(configuring)selectionNote();});
  el("annulla-angolo").addEventListener("click",()=>{corners.pop();render();update();selectionNote();});
  el("annulla-area").addEventListener("click",()=>{configuring=false;dragging=null;draft=null;invalidate();render();note("Selezione annullata; area precedente conservata.");});
  el("reset-area").addEventListener("click",()=>{if(busy||configuring)return;try{localStorage.removeItem(key());}catch(_e){}profile=null;frozen=false;invalidate();describe();render();note("Area cancellata. Seleziona una nuova area sull’inquadratura completa.");});
  el("motore").addEventListener("change",()=>{try{localStorage.setItem("rfid.visione.motore.v1",el("motore").value);}catch(_e){}invalidate();render();note(frozen?"Modello cambiato. Premi Rianalizza lo scatto per confrontare la stessa foto.":"Modello selezionato. Premi Scatta e conta quando l’inquadratura è pronta.");});
  el("scatta").addEventListener("click",()=>capture(false));el("riconta").addEventListener("click",()=>capture(true));el("nuova").addEventListener("click",()=>{frozen=false;request?.abort();invalidate();render();note(busy?"Il server potrebbe completare ancora l’analisi precedente: attendi prima di un nuovo scatto.":"Anteprima pronta per un nuovo scatto.");});
  el("camera").addEventListener("change",async()=>{stop();profile=null;frozen=false;configuring=false;invalidate();await start();});
  window.addEventListener("pagehide",()=>{request?.abort();stop();});update();
  cameras().then(()=>{
    const device=params.get("device");
    if(device&&Array.from(el("camera").options||[]).some(o=>o.value===device))el("camera").value=device;
    else if(device&&params.get("impostazioni")==="1") {
      deviceMissing=true;update();note("Videocamera selezionata non disponibile. Chiudi questo riquadro e scegli un'altra videocamera nelle Impostazioni.");return;
    } else {
      const wanted=params.get("camera");if(wanted){const o=Array.from(el("camera").options||[]).find(o=>o.textContent.toLowerCase().includes(wanted.toLowerCase()));if(o)el("camera").value=o.value;}
    }
    if(params.get("auto")==="1")start();
  }).catch(e=>note(e.message));
})();
