/* Regressioni asincrone: fotocamera condivisa, recupero e conferma della foto. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = name => fs.readFileSync(path.join(__dirname, "../webui/static/", name), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise(r => {resolve=r;}); return {promise,resolve}; };

class Element {
  constructor() {
    this.events={}; this.value=""; this.checked=false; this.hidden=true; this.children=[];
    this.videoWidth=1280; this.videoHeight=720; this.readyState=2;
    this.classes=new Set();this.classList={toggle:(name,on)=>on?this.classes.add(name):this.classes.delete(name)};
    this.style={setProperty:(key,value)=>{this.style[key]=value;}};
  }
  addEventListener(name, fn) { this.events[name]=fn; }
  replaceChildren(...nodes) {this.children=nodes;}
  append(...nodes) {this.children.push(...nodes);}
  play() {return Promise.resolve();}
  decode() {return Promise.resolve();}
  toDataURL() {return "data:image/jpeg;base64,AAA=";}
  getBoundingClientRect() {return {left:0,top:0,width:1280,height:720};}
  setPointerCapture() {}
  getContext() { return {drawImage:(...args)=>{this.lastDraw=args;},clearRect(){},beginPath(){},arc(){},fill(){},fillText(){},moveTo(){},lineTo(){},closePath(){},stroke(){},setLineDash(){},strokeRect(){}}; }
}

function bench() {
  const elements=new Map(), $=id=>{
    if(!elements.has(id)) elements.set(id,new Element());
    return elements.get(id);
  };
  const created=[],document=new Element(), window=new Element(); document.createElement=()=>{const e=new Element();created.push(e);return e;};
  const state={spedizione:{shipment_id:7,attesi:3,contenitori:[]}, collegato:true, occupato:false};
  const timers=new Map(); let serial=0, stops=0, reads=0, saves=0, frames=0;
  let camera=null, analysis=null, recovery=null, stable=false, activeDevice="cam1";
  let cameraError=null;
  let aiAnalysis=null,aiDetected=false;
  let savedConfig={};
  const storage=new Map(),payloads=[];
  const constraints=[];
  const track={stop:()=>stops++,getSettings:()=>({deviceId:activeDevice}),addEventListener(){}};
  const stream={getTracks:()=>[track],getVideoTracks:()=>[track]};
  const calls=[];
  const context={window,document,$,stato:state,TOKEN:"test",AbortController,DOMException,Image:Element,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    navigator:{mediaDevices:{getUserMedia:options=>{
      constraints.push(options); activeDevice=options.video.deviceId?.exact || "cam1";
      if(cameraError) return Promise.reject(cameraError);
      return camera||Promise.resolve(stream);
    },enumerateDevices:async()=>[
      {kind:"videoinput",deviceId:"cam1",label:"Integrata"},
      {kind:"audioinput",deviceId:"mic"},
      {kind:"videoinput",deviceId:"cam2",label:"USB"}
    ]}},
    setTimeout:(fn,ms)=>{timers.set(++serial,{fn,ms});return serial;},clearTimeout:id=>timers.delete(id),
    chiama:async action=>{assert.equal(action,"recupera_visivo");reads++;if(recovery) await recovery;return {concorde:true,trovati:3,attesi:3,passes:[]};},
    fetch:async (url, options)=>{
      if(url==="/api/interrompi") {calls.push("interrompi");return {ok:true};}
      const data=JSON.parse(options.body); calls.push(data.azione);payloads.push(data);
      let body={ok:true};
      if(["analizza","anteprima_configurazione"].includes(data.azione)) {
        frames++; if(analysis) await analysis;
        body={sessione:"test",dimensioni:[1280,720],area:[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],
          confermati:[[.2,.2],[.4,.4],[.6,.6]],cerchi:[[.2,.2,.06],[.4,.4,.07],[.6,.6,.08]],
          origine_area:"bordo automatico",coperchio:"non determinabile",
          stabile:stable,qualita:"leggibile",rfid:reads?{concorde:true}:null};
        if(data.modello!=="cerchi") {
          body={...body,motore:data.modello,dimensioni:[900,600],cerchi:[],
            conteggio:aiDetected?3:null,qualita:aiDetected?"leggibile":"da riconoscere",
            confermati:aiDetected?body.confermati:[],stabile:aiDetected&&stable};
        }
      } else if(data.azione==="analizza_modello") {
        if(aiAnalysis)await aiAnalysis;aiDetected=true;
        body={sessione:"ai",dimensioni:[900,600],area:[[0,0],[1,0],[1,1],[0,1]],
          confermati:[[.2,.2],[.4,.4],[.6,.6]],oggetti:[{contorni:[[[.1,.1],[.3,.1],[.3,.3]]]}],
          motore:data.modello,modello:"Modello di prova",conteggio:3,tempo_secondi:4,
          nota:"Riflessi esclusi",stabile:false,qualita:"leggibile",immagine_base64:"FOTO_RETTIFICATA"};
      } else if(data.azione==="prepara_foto") {body={token:"foto",foto:{jpeg:"AAA="},centri:[[.2,.2],[.4,.4],[.6,.6]]};}
      else if(data.azione==="invalida") {aiDetected=false;}
      else if(data.azione==="conferma_foto") {saves++;body={prova:{stato:"concordante",confermati:[1,2,3]}};}
      else if(data.azione==="correggi") {body={sessione:"corretta"};}
      else if(data.azione==="impostazioni") {body={config:savedConfig,console_modelli:"/sam2-auto.html?t=test&port=8772"};}
      return {ok:true,json:async()=>body};
    }
  };
  vm.createContext(context);vm.runInContext(source("camera.js"),context);vm.runInContext(source("visual.js"),context);
  return {$,window,document,state,stream,calls,constraints,storage,payloads,created,
    click:id=>$("#visivo-"+id).events.click(),
    get reads(){return reads;},get saves(){return saves;},get frames(){return frames;},get stops(){return stops;},
    set camera(v){camera=v;},set analysis(v){analysis=v;},set recovery(v){recovery=v;},set stable(v){stable=v;},
    set cameraError(v){cameraError=v;},
    set aiAnalysis(v){aiAnalysis=v;},
    set config(v){savedConfig=v;},
    cycle(){for(const [id,t] of timers){if(t.ms===600){timers.delete(id);t.fn();break;}}}
  };
}

(async()=>{
  const selection=bench();
  await selection.click("aggiorna-camera");
  assert.deepEqual(selection.$("#visivo-camera").children.map(o=>o.textContent),["Predefinita","Integrata","USB"]);
  selection.$("#visivo-camera").value="cam2";
  await selection.$("#visivo-camera").events.change();
  assert.equal(selection.constraints.length,0,"scegliere a webcam spenta non la attiva");
  await selection.click("avvia"); await tick();
  assert.equal(selection.constraints[0].video.deviceId.exact,"cam2");
  selection.$("#visivo-camera").value="cam1";
  await selection.$("#visivo-camera").events.change(); await tick();
  assert.equal(selection.stops,1,"cambiare webcam rilascia quella precedente");
  assert.equal(selection.constraints[1].video.deviceId.exact,"cam1");
  assert.equal(selection.$("#visivo-camera").value,"cam1");
  await selection.click("ferma");
  await selection.click("avvia"); await tick();
  assert.equal(selection.constraints[2].video.deviceId.exact,"cam1","la scelta resta al riavvio");

  const unavailable=bench();
  unavailable.cameraError=new DOMException("assente","OverconstrainedError");
  unavailable.$("#visivo-camera").value="scollegata";
  await unavailable.click("avvia");
  assert.match(unavailable.$("#visivo-stato").textContent,/scegli un'altra/);
  assert.equal(unavailable.$("#visivo-avvia").disabled,false);
  unavailable.cameraError=null;
  unavailable.$("#visivo-camera").value="cam2";
  await unavailable.click("avvia"); await tick();
  assert.equal(unavailable.$("#visivo-video-box").hidden,false);

  const switching=bench(), firstConsent=deferred();
  switching.camera=firstConsent.promise;
  const firstOpening=switching.click("avvia");
  await tick();
  switching.camera=null; switching.$("#visivo-camera").value="cam2";
  await switching.$("#visivo-camera").events.change(); await tick();
  let discarded=0;
  firstConsent.resolve({getTracks:()=>[{stop:()=>discarded++}]});
  await firstOpening;
  assert.equal(discarded,1,"un vecchio permesso viene rilasciato dopo il cambio webcam");
  assert.equal(switching.$("#visivo-video-box").hidden,false);
  assert.equal(switching.$("#visivo-camera").value,"cam2");
  assert.equal(switching.stops,0,"il permesso tardivo non spegne la nuova webcam");

  const b=bench();
  await b.click("avvia");await tick();
  assert.equal(b.reads,0,"nessun recupero prima del conteggio stabile");
  b.stable=true;b.cycle();await tick();
  assert.equal(b.reads,1);
  b.cycle();await tick();assert.equal(b.reads,1,"nessun ciclo automatico infinito");
  await b.click("foto");assert.equal(b.saves,0,"lo scatto non è ancora una conferma");
  const frames=b.frames;b.cycle();await tick();assert.equal(b.frames,frames,"la foto da confermare resta ferma");
  await b.click("conferma-foto");assert.equal(b.saves,1);
  b.cycle();await tick();assert.equal(b.frames,frames,"il coperchio può essere posato dopo la foto");
  await b.click("ferma");assert.equal(b.stops,1);

  const late=bench(), consent=deferred();late.camera=consent.promise;
  const opening=late.click("avvia");await tick();await late.click("ferma");consent.resolve(late.stream);await opening;
  assert.equal(late.stops,1);assert.equal(late.frames,0,"un permesso tardivo non avvia l'analisi");

  const shared=bench();await shared.click("avvia");await tick();
  await shared.window.WebcamLocale.apri("ricezione",{video:true},()=>{});
  assert.equal(shared.stops,1,"la Ricezione rilascia la webcam del Sigillo");
  const oldFrames=shared.frames;shared.cycle();await tick();assert.equal(shared.frames,oldFrames);

  const interrupted=bench(), scanning=deferred();interrupted.recovery=scanning.promise;interrupted.stable=true;
  await interrupted.click("avvia");await tick();assert.equal(interrupted.reads,1);
  const skip=interrupted.click("salta");await tick();
  assert(interrupted.calls.includes("interrompi"));assert(!interrupted.calls.includes("salta"),"attendere il rilascio del lettore prima di salvare Salta");
  scanning.resolve();await skip;assert(interrupted.calls.includes("salta"));
  assert.equal(interrupted.state.occupato,false);

  const manual=bench();await manual.click("avvia");await tick();
  const inflight=deferred();manual.analysis=inflight.promise;manual.cycle();await tick();
  const editing=manual.click("correggi");inflight.resolve();await editing;
  manual.$("#visivo-overlay").events.pointerdown({clientX:1100,clientY:600});
  await manual.click("conferma-correzioni");
  assert(manual.calls.includes("correggi"));
  const ai=bench();ai.$("#visivo-modello").value="qwen";
  const profile={versione:2,modo:"rettangolo",area:[.1,.1,.9,.9],dimensioni:[1280,720]};
  ai.storage.set("rfid.sam2.area.v2.cam1",JSON.stringify(profile));
  await ai.click("avvia");await tick();
  assert.equal(ai.reads,0,"l'anteprima non invia immagini al modello e non avvia RFID");
  assert(!ai.calls.includes("analizza_modello"));
  const waiting=deferred();ai.aiAnalysis=waiting.promise;
  const recognizing=ai.click("riconosci");await tick();
  await ai.click("riconosci");
  assert.equal(ai.calls.filter(c=>c==="analizza_modello").length,1,"doppio clic: un solo invio");
  waiting.resolve();await recognizing;await tick();
  assert.match(ai.$("#visivo-nota-modello").textContent,/Riflessi esclusi/);
  assert(ai.$("#visivo-video-box").classes.has("visivo-video--foto"));
  assert.equal(ai.$("#visivo-video-box").style["--visivo-rapporto"],"900 / 600","lo scatto conserva le proporzioni della foto rettificata");
  const shot=ai.payloads.find(p=>p.azione==="analizza_modello");
  assert.deepEqual(shot.profilo.area,[0,0,1,1],"ritaglio prima dell'invio");
  assert.deepEqual(shot.profilo.dimensioni,[1024,576]);
  assert.deepEqual(ai.created[0].lastDraw.slice(1,5),[128,72,1024,576]);
  await ai.click("correggi");
  assert.equal(ai.$("#visivo-overlay").lastDraw[0].src,"data:image/jpeg;base64,FOTO_RETTIFICATA",
    "la correzione usa la foto rettificata, non il video originale");
  await ai.click("conferma-correzioni");
  ai.$("#visivo-modello").value="sam2";
  await ai.$("#visivo-modello").events.change();await tick();
  assert.equal(ai.$("#visivo-rianalizza").disabled,false,"il cambio modello conserva lo scatto");
  await ai.click("rianalizza");await tick();
  const compared=ai.payloads.filter(p=>p.azione==="analizza_modello")[1];
  assert.equal(compared.modello,"sam2");
  assert.equal(compared.immagine_base64,shot.immagine_base64);
  assert.deepEqual(compared.profilo,shot.profilo);
  ai.$("#visivo-modello").value="yolo";
  await ai.$("#visivo-modello").events.change();await tick();
  assert.equal(ai.$("#visivo-rianalizza").disabled,false,"YOLO è un motore del servizio, non i cerchi classici");
  await ai.click("rianalizza");await tick();
  const yolo=ai.payloads.filter(p=>p.azione==="analizza_modello").at(-1);
  assert.equal(yolo.modello,"yolo");
  assert.equal(yolo.immagine_base64,shot.immagine_base64,"YOLO confronta la stessa foto");
  ai.$("#visivo-modello").value="sam2";
  await ai.$("#visivo-modello").events.change();await tick();
  await ai.click("rianalizza");await tick();
  ai.stable=true;ai.cycle();await tick();assert.equal(ai.reads,1);
  const lateAI=bench();lateAI.$("#visivo-modello").value="qwen";
  lateAI.storage.set("rfid.sam2.area.v2.cam1",JSON.stringify(profile));
  await lateAI.click("avvia");await tick();
  const lateResult=deferred();lateAI.aiAnalysis=lateResult.promise;
  const old=lateAI.click("riconosci");await tick();await lateAI.click("ferma");
  lateResult.resolve();await old;
  assert.equal(lateAI.$("#visivo-nota-modello").textContent,undefined,"risposta tardiva scartata dopo Stop");
  assert.equal(lateAI.reads,0);
  const fourK=bench();fourK.$("#visivo-modello").value="sam2";
  fourK.$("#visivo-video").videoWidth=3840;fourK.$("#visivo-video").videoHeight=2160;
  fourK.storage.set("rfid.sam2.area.v2.cam1",JSON.stringify({...profile,area:[.25,.25,.75,.75],dimensioni:[3840,2160]}));
  await fourK.click("avvia");await tick();await fourK.click("riconosci");await tick();
  assert.deepEqual(fourK.payloads.find(p=>p.azione==="analizza_modello").profilo.dimensioni,[1920,1080],
    "il ritaglio 4K conserva i pixel della borsa prima del limite Full HD");
  const noProfile=bench();noProfile.$("#visivo-modello").value="sam2";
  await noProfile.click("avvia");await tick();await noProfile.click("riconosci");await tick();
  assert.match(noProfile.$("#visivo-stato").textContent,/Questa webcam non ha un'area salvata/);
  assert.equal(noProfile.$("#visivo-rianalizza").disabled,true,"uno scatto senza profilo non diventa rianalizzabile");
  noProfile.cycle();await tick();noProfile.cycle();await tick();
  assert.match(noProfile.$("#visivo-stato").textContent,/Questa webcam non ha un'area salvata/,
    "il controllo continuo non sovrascrive l'errore del pulsante");
  assert(!noProfile.calls.includes("analizza_modello"));
  const archived=bench();archived.$("#visivo-modello").value="sam2";archived.state.spedizione.stato="received";
  await archived.click("avvia");await tick();
  assert.equal(archived.$("#visivo-riconosci").disabled,true);
  assert.equal(archived.$("#visivo-blocco").hidden,false);
  assert.match(archived.$("#visivo-blocco").textContent,/già archiviata o conclusa/);
  await archived.click("riconosci");archived.cycle();await tick();
  assert(!archived.calls.includes("analizza_modello")&&!archived.calls.includes("analizza"),
    "nessun polling o riconoscimento su una spedizione ricevuta");
  assert.equal(archived.$("#visivo-configura-modelli").disabled,false);
  const changed=bench();await changed.click("avvia");await tick();
  changed.state.spedizione.esportata="2026-10-02";
  changed.window.aggiornaVisivoSpedizione();
  assert.equal(changed.stops,1,"una spedizione archiviata durante l'anteprima spegne la webcam");
  const settings=bench();settings.state.spedizione=null;settings.stable=true;
  await settings.window.caricaImpostazioniVisive();
  settings.$("#visivo-modello").value="cerchi";
  await settings.click("config-avvia");await tick();
  assert(settings.$("#visivo-anteprima-impostazioni").children.includes(settings.$("#visivo-video-box")));
  assert(settings.calls.includes("anteprima_configurazione"));
  assert.equal(settings.reads,0,"la calibrazione non avvia RFID anche con conteggio stabile");
  assert.equal(settings.$("#visivo-calibra-aperta").disabled,false,"si calibra senza spedizione");
  await settings.click("calibra-aperta");assert(settings.calls.includes("calibra"));
  await settings.click("configura-modelli");
  assert.equal(settings.stops,1,"prima del riquadro area si rilascia la webcam di anteprima");
  assert.equal(settings.$("#visivo-calibrazione").hidden,false);
  assert.match(settings.$("#visivo-calibrazione").src,/impostazioni=1&device=cam1&motore=cerchi/);
  settings.window.fermaConfigurazioneVisiva();
  assert.equal(settings.$("#visivo-calibrazione").src,"about:blank","uscire dalle Impostazioni scarica la calibrazione e rilascia la webcam");
  assert.equal(settings.$("#visivo-calibrazione").hidden,true);
  const saved=bench();saved.config={camera:"cam2",modello:"qwen"};
  saved.$("#visivo-camera").value="cam1";saved.$("#visivo-modello").value="sam2";
  await saved.click("avvia");await tick();
  assert.equal(saved.constraints[0].video.deviceId.exact,"cam2","il Sigillo rilegge la webcam salvata prima dell'acquisizione");
  assert.equal(saved.$("#visivo-modello").value,"qwen","il Sigillo usa il modello salvato");
  const html=source("index.html"),config=html.slice(html.indexOf('id="scheda-controllo-visivo"'),html.indexOf('id="scheda-configurazione"'));
  for(const id of ["camera","modello","salva","box-mark","lid-mark","lid-check","min","max","soglia","calibra-aperta","calibra-chiusa","configura-modelli"]){
    assert(config.includes(`id="visivo-${id}"`),`${id} nelle Impostazioni Controllo Visivo`);
    assert.equal(html.split(`id="visivo-${id}"`).length,2,`${id} presente una sola volta`);
  }
  console.log("PASS impostazioni visive: controlli unici nella sottosezione, anteprima e calibrazione senza spedizione/RFID, riquadro area e rilascio all'uscita");
  console.log("PASS blocchi Sigillo: errore di scatto persistente, rianalisi senza profilo disabilitata, spedizione archiviata e cambio di stato");
  console.log("PASS SAM/Qwen nel Sigillo: scatto esplicito, calibrazione, ritaglio 4K, doppio clic, correzione rettificata, rianalisi identica, RFID stabile e risposta tardiva");
  console.log("PASS controllo visivo UI: scelta e cambio webcam, webcam assente, cambio durante consenso, stabilità, recupero unico, foto confermata, permesso tardivo, webcam esclusiva, interruzione e correzione durante analisi");
})().catch(error=>{console.error(error);process.exitCode=1;});
