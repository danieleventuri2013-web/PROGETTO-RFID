/* Foto ferma e prompt manuali SAM 2: nessuna identità o procedura RFID. */
(() => {
  "use strict";
  const el=id=>document.getElementById(id), video=el("video"), canvas=el("overlay"), frame=document.createElement("canvas");
  const params=new URLSearchParams(location.search), token=params.get("t");
  el("automatico").href="sam2-auto.html"+location.search;
  const endpoint=params.get("port")==="8772" ? "http://127.0.0.1:8772/api/sam2" : "/api/sam2";
  let stream=null, generation=0, revision=0, frozen=false, photo=null, objects=[], history=[], result=null, busy=false, request=null, opening=false;
  const note=text=>{el("stato").textContent=text;};
  function update(selected=el("campione").value) {
    el("campione").replaceChildren(...objects.map((_,i)=>{const o=document.createElement("option");o.value=String(i);o.textContent=`Campione ${i+1}`;return o;}));
    el("campione").value=objects[Number(selected)] ? selected : String(Math.max(0,objects.length-1));
    el("avvia").disabled=!!stream||opening;el("ferma").disabled=!stream&&!opening;
    el("foto").disabled=!stream||frozen||busy;el("riprendi").disabled=!frozen;
    el("analizza").disabled=!frozen||!objects.length||busy;
    el("annulla").disabled=!history.length||busy;el("azzera").disabled=!objects.length||busy;
    el("azione").disabled=el("campione").disabled=busy;
    el("conteggio").textContent=`Campioni indicati: ${objects.length} · Contorni SAM: ${result ? result.contorni_distinti : "—"}`;
  }
  function invalidate() {revision++;result=null;el("risultati").replaceChildren();}
  function draw() {
    canvas.width=frame.width||video.videoWidth||1280;canvas.height=frame.height||video.videoHeight||720;
    const ctx=canvas.getContext("2d"), w=canvas.width,h=canvas.height;
    if(!frozen)return;
    ctx.drawImage(frame,0,0,w,h);ctx.lineWidth=3;
    (result?.oggetti||[]).forEach(obj=>{
      ctx.strokeStyle=obj.avvisi.length ? "#ffb52e" : "#39ff85";
      ctx.fillStyle=obj.avvisi.length ? "#ffb52e25" : "#39ff8525";
      (obj.contorni||[]).forEach(poly=>{ctx.beginPath();poly.forEach(([x,y],i)=>i?ctx.lineTo(x*w,y*h):ctx.moveTo(x*w,y*h));ctx.closePath();ctx.fill();ctx.stroke();});
    });
    ctx.font="bold 24px sans-serif";
    objects.forEach((obj,i)=>obj.punti.forEach(([x,y],j)=>{
      ctx.fillStyle=obj.etichette[j] ? "#2196ff" : "#ff4b58";
      ctx.beginPath();ctx.arc(x*w,y*h,7,0,Math.PI*2);ctx.fill();
      if(j===0){ctx.strokeStyle="#102b42";ctx.lineWidth=4;ctx.strokeText(String(i+1),x*w+10,y*h-10);ctx.fillStyle="white";ctx.fillText(String(i+1),x*w+10,y*h-10);}
    }));
  }
  function clear() {objects=[];history=[];invalidate();update();draw();}
  function stop() {
    generation++;revision++;request?.abort();opening=false;
    stream?.getTracks().forEach(t=>t.stop());stream=null;video.srcObject=null;
    // La foto resta disponibile anche a webcam spenta.
    update();
  }
  async function cameras(selected=el("camera").value) {
    const devices=await navigator.mediaDevices.enumerateDevices();
    const options=devices.filter(d=>d.kind==="videoinput"&&d.deviceId).map((d,i)=>{const o=document.createElement("option");o.value=d.deviceId;o.textContent=d.label||`Webcam ${i+1}`;return o;});
    if(options.length){el("camera").replaceChildren(...options);el("camera").value=options.some(o=>o.value===selected)?selected:options[0].value;}
  }
  async function start() {
    if(opening||stream)return;
    const turn=++generation;opening=true;update();note("Attendo il consenso alla webcam…");
    let pending,timer;
    try{
      pending=navigator.mediaDevices.getUserMedia({audio:false,video:{deviceId:el("camera").value?{exact:el("camera").value}:undefined,width:{ideal:1920},height:{ideal:1080},frameRate:{ideal:15}}});
      pending.then(s=>{if(turn!==generation)s.getTracks().forEach(t=>t.stop());},()=>{});
      const s=await Promise.race([pending,new Promise((_,reject)=>{timer=setTimeout(()=>{if(turn===generation)stop();reject(new Error("Consenso non ricevuto: riprova Attiva webcam."));},20000);})]);
      if(turn!==generation)return;
      stream=s;video.srcObject=s;await video.play();
      if(turn!==generation)return;
      await cameras(s.getVideoTracks()[0].getSettings().deviceId);
      note(frozen?"Foto conservata. Premi Nuova foto per aggiornare la posizione.":"Inquadra la borsa e premi Ferma foto.");
    }catch(e){if(turn===generation){stop();note(e.message);}else if(!opening&&!stream)note(e.message);}
    finally{clearTimeout(timer);if(turn===generation)opening=false;update();}
  }
  function capture() {
    if(!stream||video.readyState<2||!video.videoWidth){note("Attendi che la webcam mostri un'immagine.");return;}
    const scale=Math.min(1,1920/video.videoWidth,1080/video.videoHeight);
    frame.width=Math.round(video.videoWidth*scale);frame.height=Math.round(video.videoHeight*scale);
    frame.getContext("2d").drawImage(video,0,0,frame.width,frame.height);
    for(const q of [.96,.90,.82,.74]){photo=frame.toDataURL("image/jpeg",q).split(",")[1];if(photo.length<=2800000)break;}
    if(photo.length>2800000){photo=null;note("Foto troppo grande: riduci la risoluzione della webcam.");return;}
    frozen=true;clear();el("qualita").textContent=`Foto ferma: ${frame.width}×${frame.height} · SAM 2.1 Tiny sulla CPU.`;
    note("Clicca una volta dentro ciascuno degli otto campioni. Poi premi Disegna contorni con SAM 2.");
  }
  canvas.addEventListener("pointerdown",e=>{
    if(!frozen||busy)return;
    const r=canvas.getBoundingClientRect(), point=[(e.clientX-r.left)/r.width,(e.clientY-r.top)/r.height];
    if(point.some(v=>!Number.isFinite(v)||v<0||v>1))return;
    const action=el("azione").value;
    if(action==="nuovo"){
      if(objects.length>=20){note("Massimo 20 campioni in questa prova.");return;}
      history.push(JSON.stringify(objects));objects.push({punti:[point],etichette:[1]});
      el("campione").value=String(objects.length-1);
    }else{
      const obj=objects[Number(el("campione").value)];
      if(!obj){note("Indica prima un nuovo campione.");return;}
      if(obj.punti.length>=16){note("Massimo 16 punti per campione.");return;}
      history.push(JSON.stringify(objects));obj.punti.push(point);obj.etichette.push(action==="positivo"?1:0);
    }
    invalidate();update(action==="nuovo"?String(objects.length-1):el("campione").value);draw();note(`Indicati ${objects.length} campioni. Puoi analizzarli o aggiungere altri clic.`);
  });
  async function analyze() {
    if(busy||!frozen||!objects.length)return;
    busy=true;const rev=revision;request=new AbortController();update();note("SAM 2 sta analizzando la foto sulla CPU. Il primo passaggio può richiedere diversi secondi…");
    const controller=request, timeout=setTimeout(()=>controller.abort(),180000);
    try{
      const response=await fetch(endpoint,{method:"POST",headers:{"Content-Type":"application/json","X-RFID-Token":token||""},body:JSON.stringify({immagine_base64:photo,oggetti:objects}),signal:controller.signal});
      const data=await response.json();if(!response.ok)throw new Error(data.errore||"Analisi non riuscita");
      if(rev!==revision)return;
      result=data;el("risultati").replaceChildren(...data.oggetti.map(obj=>{const p=document.createElement("p");p.textContent=`Campione ${obj.id}: ${obj.avvisi.length?obj.avvisi.join("; "):"contorno ottenuto — verifica visivamente"}`;return p;}));
      note(`Analisi completata in ${data.tempo_secondi} s. Controlla i contorni; per correggere seleziona il campione e aggiungi un punto dentro o da escludere.`);draw();
    }catch(e){if(rev===revision)note(e.name==="AbortError"?"Attesa terminata. Il server potrebbe essere ancora impegnato: attendi prima di riprovare.":e.message);}
    finally{clearTimeout(timeout);busy=false;if(request===controller)request=null;update();}
  }
  el("avvia").addEventListener("click",start);el("ferma").addEventListener("click",()=>{stop();note("Webcam spenta; la foto ferma resta disponibile.");});
  el("foto").addEventListener("click",capture);
  el("riprendi").addEventListener("click",()=>{revision++;request?.abort();frozen=false;photo=null;clear();note(stream?"Premi Ferma foto quando l'inquadratura è pronta.":"Attiva la webcam per scattare una nuova foto.");});
  el("analizza").addEventListener("click",analyze);
  el("annulla").addEventListener("click",()=>{if(busy||!history.length)return;objects=JSON.parse(history.pop());invalidate();update();draw();});
  el("azzera").addEventListener("click",()=>{if(!busy)clear();});
  el("camera").addEventListener("change",async()=>{if(stream){stop();await start();}});
  window.addEventListener("pagehide",stop);update();
  cameras().then(()=>{
    const wanted=params.get("camera");if(wanted){const o=Array.from(el("camera").options||[]).find(o=>o.textContent.toLowerCase().includes(wanted.toLowerCase()));if(o)el("camera").value=o.value;}
    if(params.get("auto")==="1")start();
  }).catch(e=>note(e.message));
})();
