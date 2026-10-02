/* Verifica cerchi sovrapposti, conteggio indipendente dagli attesi e permessi tardivi. */
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const source=fs.readFileSync(path.join(__dirname,"../webui/static/prova-visiva.js"),"utf8");
const tick=()=>new Promise(r=>setImmediate(r));
function bench(width=1280,height=720){
  const elements=new Map(),arcs=[],numbers=[],calls=[],timers=new Map(),constraints=[],encoded=[];let serial=0,stops=0,capture=null,scene=null;
  const context2d={clearRect(){},drawImage(){},beginPath(){},moveTo(){},lineTo(){},closePath(){},stroke(){},fillRect(){},
    arc:(...a)=>arcs.push(a),fillText:n=>numbers.push(n)};
  class Element{
    constructor(){this.events={};this.value="";this.videoWidth=width;this.videoHeight=height;this.readyState=2;}
    addEventListener(name,fn){this.events[name]=fn;}
    replaceChildren(...options){this.options=options;}
    getContext(){return context2d;}
    play(){return Promise.resolve();}
    toDataURL(type,quality){encoded.push({width:this.width,height:this.height,type,quality});return "data:image/jpeg;base64,AAA=";}
    getBoundingClientRect(){return {left:0,top:0,width:1280,height:720};}
  }
  const el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  el("attesi").value="99";el("min").value="1.2";el("max").value="16";el("soglia").value="30";
  const track={stop:()=>stops++,getSettings:()=>({deviceId:"cam"}),addEventListener(){},applyConstraints:async options=>{
    constraints.push(options);el("video").videoWidth=options.width.ideal;el("video").videoHeight=options.height.ideal;
  }};
  const stream={getTracks:()=>[track],getVideoTracks:()=>[track]};
  const doc=new Element(),win=new Element();doc.getElementById=el;doc.createElement=()=>new Element();
  const context={document:doc,window:win,location:{search:"?t=test"},URLSearchParams,AbortController,DOMException,performance:{now:()=>0},
    navigator:{mediaDevices:{enumerateDevices:async()=>[{kind:"videoinput",deviceId:"cam",label:"C920"}],getUserMedia:options=>{constraints.push(options.video);return capture||Promise.resolve(stream);}}},
    setTimeout:(fn,ms)=>{timers.set(++serial,{fn,ms});return serial;},clearTimeout:id=>timers.delete(id),
    fetch:async(url,options)=>{calls.push({url,data:JSON.parse(options.body)});return {ok:true,json:async()=>scene||({dimensioni:[1280,720],area:[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],origine_area:"bordo automatico",centri:[[.3,.4],[.6,.4]],cerchi:[[.3,.4,.05],[.6,.4,.08]]})};}};
  vm.createContext(context);vm.runInContext(source,context);
  return {el,arcs,numbers,calls,stream,doc,encoded,constraints,set capture(p){capture=p;},set scene(v){scene=v;},get stops(){return stops;},
    cycle(){const entry=Array.from(timers).find(([,t])=>t.ms===1500);assert(entry,"l'analisi usa intervalli di 1,5 secondi");timers.delete(entry[0]);entry[1].fn();}};
}
(async()=>{
  const b=bench();await tick();await b.el("avvia").events.click();await tick();
  assert.equal(b.calls[0].url,"/api/anteprima");
  assert(!("attesi" in b.calls[0].data),"il numero atteso non guida il rilevamento");
  assert.match(b.el("qualita-video").textContent,/1280×720.*meno pixel/);
  assert.equal(b.el("conteggio").textContent,"Riconosciuti 0 / 99");assert.equal(b.arcs.length,0,"un singolo fotogramma non basta");
  b.cycle();await tick();assert.equal(b.arcs.length,0);
  b.cycle();await tick();assert.equal(b.el("conteggio").textContent,"Riconosciuti 2 / 99");
  assert.deepEqual(b.numbers,["1","2"]);
  assert.equal(b.arcs.length,2);assert.equal(b.arcs[0][2],36);assert.equal(b.arcs[1][2],57.6);
  b.el("ferma").events.click();assert.equal(b.stops,1);
  const transient=bench();await tick();await transient.el("avvia").events.click();await tick();
  transient.scene={dimensioni:[1280,720],area:[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],centri:[[.3,.4],[.6,.4],[.8,.6]],cerchi:[[.6,.4,.08],[.3,.4,.05],[.8,.6,.07]]};
  transient.cycle();await tick();transient.scene=null;transient.cycle();await tick();
  assert.equal(transient.el("conteggio").textContent,"Riconosciuti 2 / 99","il falso cerchio di un solo frame viene scartato");
  assert.deepEqual(transient.numbers,["1","2"],"i cerchi riordinati mantengono la corrispondenza");
  transient.scene={dimensioni:[1280,720],area:[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],centri:[],cerchi:[]};
  transient.cycle();await tick();assert.equal(transient.el("conteggio").textContent,"Riconosciuti 0 / 99","i campioni scomparsi non rimangono nel conteggio");
  const calibration=bench();await tick();await calibration.el("avvia").events.click();await tick();
  calibration.el("piccolo").events.click();
  calibration.el("overlay").events.pointerdown({clientX:10,clientY:10});
  assert.match(calibration.el("stato").textContent,/dentro il bordo giallo/);
  calibration.el("overlay").events.pointerdown({clientX:300,clientY:300});
  calibration.el("overlay").events.pointerdown({clientX:336,clientY:300});
  assert.equal(calibration.el("min").value,"4.0");
  calibration.cycle();await tick();assert.equal(calibration.calls.at(-1).data.raggio_min,.04);
  calibration.el("grande").events.click();
  calibration.el("overlay").events.pointerdown({clientX:500,clientY:300});
  calibration.el("overlay").events.pointerdown({clientX:560,clientY:300});
  assert.equal(calibration.el("max").value,"10.0");
  const fullhd=bench(1920,1080);await tick();await fullhd.el("avvia").events.click();await tick();
  assert.equal(fullhd.constraints[0].width.ideal,1920);assert.equal(fullhd.constraints[0].height.ideal,1080);
  assert.equal(fullhd.encoded[0].width,1920);assert.equal(fullhd.encoded[0].height,1080,"Full HD non viene ridotto a 720p");
  assert.equal(fullhd.encoded[0].quality,.96);assert.match(fullhd.el("qualita-video").textContent,/Analisi: 1920×1080/);
  fullhd.el("risoluzione").value="720";await fullhd.el("risoluzione").events.change();
  fullhd.cycle();await tick();assert.equal(fullhd.encoded.at(-1).width,1280);assert.equal(fullhd.encoded.at(-1).height,720);
  assert.equal(fullhd.stops,0,"cambiare risoluzione non rilascia la webcam");
  const late=bench();let resolve;late.capture=new Promise(r=>{resolve=r;});
  await tick();const starting=late.el("avvia").events.click();late.el("ferma").events.click();
  resolve(late.stream);await starting;await tick();assert.equal(late.stops,1);assert.equal(late.calls.length,0);
  console.log("PASS prova visiva UI: Full HD senza riduzione, qualità JPEG e cambio risoluzione, campionamento lento, filtro temporale, calibrazione, sovrapposizione e consenso tardivo");
})().catch(e=>{console.error(e);process.exitCode=1;});
