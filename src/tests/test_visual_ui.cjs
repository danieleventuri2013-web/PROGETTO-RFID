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
  }
  addEventListener(name, fn) { this.events[name]=fn; }
  replaceChildren(...nodes) {this.children=nodes;}
  append(...nodes) {this.children.push(...nodes);}
  play() {return Promise.resolve();}
  decode() {return Promise.resolve();}
  toDataURL() {return "data:image/jpeg;base64,AAA=";}
  getBoundingClientRect() {return {left:0,top:0,width:1280,height:720};}
  setPointerCapture() {}
  getContext() { return {drawImage(){},clearRect(){},beginPath(){},arc(){},fill(){},fillText(){},moveTo(){},lineTo(){},closePath(){},stroke(){}}; }
}

function bench() {
  const elements=new Map(), $=id=>{
    if(!elements.has(id)) elements.set(id,new Element());
    return elements.get(id);
  };
  const document=new Element(), window=new Element(); document.createElement=()=>new Element();
  const state={spedizione:{shipment_id:7,attesi:3,contenitori:[]}, collegato:true, occupato:false};
  const timers=new Map(); let serial=0, stops=0, reads=0, saves=0, frames=0;
  let camera=null, analysis=null, recovery=null, stable=false, activeDevice="cam1";
  let cameraError=null;
  const constraints=[];
  const track={stop:()=>stops++,getSettings:()=>({deviceId:activeDevice}),addEventListener(){}};
  const stream={getTracks:()=>[track],getVideoTracks:()=>[track]};
  const calls=[];
  const context={window,document,$,stato:state,TOKEN:"test",AbortController,DOMException,Image:Element,
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
      const data=JSON.parse(options.body); calls.push(data.azione);
      let body={ok:true};
      if(data.azione==="analizza") {
        frames++; if(analysis) await analysis;
        body={sessione:"test",dimensioni:[1280,720],area:[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],
          confermati:[[.2,.2],[.4,.4],[.6,.6]],cerchi:[[.2,.2,.06],[.4,.4,.07],[.6,.6,.08]],
          origine_area:"bordo automatico",coperchio:"non determinabile",
          stabile:stable,qualita:"leggibile",rfid:reads?{concorde:true}:null};
      } else if(data.azione==="prepara_foto") {body={token:"foto",foto:{jpeg:"AAA="},centri:[[.2,.2],[.4,.4],[.6,.6]]};}
      else if(data.azione==="conferma_foto") {saves++;body={prova:{stato:"concordante",confermati:[1,2,3]}};}
      else if(data.azione==="correggi") {body={sessione:"corretta"};}
      else if(data.azione==="impostazioni") {body={config:{}};}
      return {ok:true,json:async()=>body};
    }
  };
  vm.createContext(context);vm.runInContext(source("camera.js"),context);vm.runInContext(source("visual.js"),context);
  return {$,window,document,state,stream,calls,constraints,
    click:id=>$("#visivo-"+id).events.click(),
    get reads(){return reads;},get saves(){return saves;},get frames(){return frames;},get stops(){return stops;},
    set camera(v){camera=v;},set analysis(v){analysis=v;},set recovery(v){recovery=v;},set stable(v){stable=v;},
    set cameraError(v){cameraError=v;},
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
  const opening=late.click("avvia");await late.click("ferma");consent.resolve(late.stream);await opening;
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
  console.log("PASS controllo visivo UI: scelta e cambio webcam, webcam assente, cambio durante consenso, stabilità, recupero unico, foto confermata, permesso tardivo, webcam esclusiva, interruzione e correzione durante analisi");
})().catch(error=>{console.error(error);process.exitCode=1;});
