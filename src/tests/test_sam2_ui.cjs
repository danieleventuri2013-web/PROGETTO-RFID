/* Foto ferma, oggetti separati e correzioni; risposte obsolete e consenso tardivo. */
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync(require("node:path").join(__dirname,"../webui/static/sam2.js"),"utf8");
const tick=()=>new Promise(r=>setImmediate(r));
function bench(){
  const elements=new Map(),calls=[],timers=new Map();let serial=0,stops=0,media=null,reply=null;
  const ctx={drawImage(){},beginPath(){},lineTo(){},moveTo(){},closePath(){},fill(){},stroke(){},arc(){},strokeText(){},fillText(){}};
  class El{
    constructor(){this.events={};this.value="";this.videoWidth=1920;this.videoHeight=1080;this.readyState=2;}
    addEventListener(n,f){this.events[n]=f;}replaceChildren(...a){this.options=a;}
    getContext(){return ctx;}play(){return Promise.resolve();}toDataURL(){return "data:image/jpeg;base64,AAA=";}
    getBoundingClientRect(){return {left:0,top:0,width:1000,height:600};}
  }
  const el=id=>{if(!elements.has(id))elements.set(id,new El());return elements.get(id);};
  el("azione").value="nuovo";const win=new El();
  const stream={getTracks:()=>[{stop:()=>stops++}],getVideoTracks:()=>[{getSettings:()=>({deviceId:"cam"})}]};
  const context={document:{getElementById:el,createElement:()=>new El()},window:win,location:{search:"?t=token&port=8772"},URLSearchParams,AbortController,
    navigator:{mediaDevices:{enumerateDevices:async()=>[{kind:"videoinput",deviceId:"cam",label:"C920"}],getUserMedia:()=>media||Promise.resolve(stream)}},
    setTimeout:(f,ms)=>{timers.set(++serial,{f,ms});return serial;},clearTimeout:i=>timers.delete(i),
    fetch:async(url,options)=>{calls.push({url,data:JSON.parse(options.body)});return reply||{ok:true,json:async()=>({indicati:8,contorni_distinti:7,tempo_secondi:12,oggetti:[]})};}};
  vm.createContext(context);vm.runInContext(source,context);
  return {el,calls,win,stream,timers,get stops(){return stops;},set media(p){media=p;},set reply(p){reply=p;},
    click(x=200,y=200){el("overlay").events.pointerdown({clientX:x,clientY:y});}};
}
(async()=>{
  const b=bench();await tick();b.click();assert.match(b.el("conteggio").textContent,/indicati: 0/);
  await b.el("avvia").events.click();b.el("foto").events.click();
  for(let i=0;i<8;i++)b.click(100+i*100,200);
  assert.match(b.el("conteggio").textContent,/indicati: 8.*SAM: —/);assert.equal(b.calls.length,0);
  await b.el("analizza").events.click();
  assert.equal(b.calls[0].url,"http://127.0.0.1:8772/api/sam2");assert.equal(b.calls[0].data.oggetti.length,8);
  assert(b.calls[0].data.oggetti.every(o=>o.punti.length===1));assert(!("attesi" in b.calls[0].data));
  assert.match(b.el("conteggio").textContent,/indicati: 8.*SAM: 7/);
  b.el("campione").value="2";b.el("azione").value="negativo";b.click(300,250);
  assert.match(b.el("conteggio").textContent,/SAM: —/);await b.el("analizza").events.click();
  assert.deepEqual(b.calls[1].data.oggetti[2].etichette,[1,0]);
  b.el("annulla").events.click();await b.el("analizza").events.click();assert.equal(b.calls[2].data.oggetti[2].punti.length,1);
  b.el("ferma").events.click();assert.equal(b.stops,1);assert.equal(b.el("analizza").disabled,false,"la foto resta analizzabile a webcam spenta");
  let resolve;b.reply=new Promise(r=>{resolve=r;});const request=b.el("analizza").events.click();
  b.el("riprendi").events.click();resolve({ok:true,json:async()=>({indicati:8,contorni_distinti:8,tempo_secondi:1,oggetti:[]})});await request;
  assert.match(b.el("conteggio").textContent,/indicati: 0.*SAM: —/,"non mostra il risultato della vecchia foto");
  const late=bench();await tick();let permission;late.media=new Promise(r=>{permission=r;});
  const opening=late.el("avvia").events.click();late.el("ferma").events.click();permission(late.stream);await opening;assert.equal(late.stops,1);
  const timeout=bench();await tick();timeout.media=new Promise(()=>{});const waiting=timeout.el("avvia").events.click();
  [...timeout.timers.values()].find(t=>t.ms===20000).f();await waiting;assert.equal(timeout.el("avvia").disabled,false);
  console.log("PASS SAM 2 UI: 8 prompt separati, foto ferma, correzioni, risultato obsoleto e consenso tardivo/timeout");
})().catch(e=>{console.error(e);process.exitCode=1;});
