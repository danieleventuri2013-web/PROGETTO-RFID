/* Ritaglio nativo persistente, quattro angoli, rettifica prima del conteggio. */
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync(require("node:path").join(__dirname,"../webui/static/sam2-auto.js"),"utf8");
const tick=()=>new Promise(r=>setImmediate(r));
function bench(storage=new Map(),search="?t=token&port=8772"){
  const elements=new Map(),calls=[],draws=[],timers=new Map();let serial=0,stops=0,media=null,reply=null;
  class El{
    constructor(){this.events={};this.value="";this.width=1280;this.height=720;this.videoWidth=1920;this.videoHeight=1080;this.readyState=2;this.classList={toggle(){}};}
    addEventListener(n,f){this.events[n]=f;}replaceChildren(...a){this.options=a;}
    closest(name){return el("parent-"+name+"-"+this.id);}get parentElement(){return this.closest("parent");}
    getContext(){const self=this;return {drawImage(...a){draws.push({canvas:self,args:a});},beginPath(){},lineTo(){},moveTo(){},closePath(){},fill(){},stroke(){},arc(){},fillText(){},fillRect(){},strokeRect(){},setLineDash(){}};}
    play(){return Promise.resolve();}setPointerCapture(){}
    toDataURL(){return "data:image/jpeg;base64,"+Buffer.from(`${this.width}x${this.height}`).toString("base64");}
    getBoundingClientRect(){return {left:0,top:0,width:1000,height:600};}
  }
  const el=id=>{if(!elements.has(id)){const e=new El();e.id=id;elements.set(id,e);}return elements.get(id);};
  el("modo-area").value="rettangolo";const win=new El();
  const stream={getTracks:()=>[{stop:()=>stops++}],getVideoTracks:()=>[{getSettings:()=>({deviceId:el("camera").value||"cam"})}]};
  const context={document:{getElementById:el,createElement:()=>new El(),querySelector:el},window:win,location:{search},URLSearchParams,AbortController,Date,
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    Image:class{decode(){return Promise.resolve();}},requestAnimationFrame:()=>1,cancelAnimationFrame(){},
    navigator:{mediaDevices:{enumerateDevices:async()=>[{kind:"videoinput",deviceId:"cam",label:"C920"},{kind:"videoinput",deviceId:"cam2",label:"Altra"}],getUserMedia:()=>media||Promise.resolve(stream)}},
    setTimeout:(f,ms)=>{timers.set(++serial,{f,ms});return serial;},clearTimeout:i=>timers.delete(i),setInterval:()=>++serial,clearInterval(){},
    fetch:async(url,options)=>{
      calls.push({url,data:JSON.parse(options.body)});if(reply)return reply;
      if(url.endsWith("/prepara"))return {ok:true,json:async()=>({immagine_base64:"RECTIFIED",dimensioni:[900,600],correzione:{nota:"piano corretto",avvisi:[]}})};
      return {ok:true,json:async()=>({conteggio:6,tempo_secondi:85,tempo_bordo_secondi:12,tempo_conteggio_secondi:73,oggetti:[],borsa:{rilevata:false},avvisi:[]})};
    }};
  vm.createContext(context);vm.runInContext(source,context);
  return {el,calls,draws,stream,timers,storage,get stops(){return stops;},set media(p){media=p;},set reply(p){reply=p;},
    point(type,x,y){el("overlay").events[type]({clientX:x,clientY:y,pointerId:1});},
    rectangle(){this.point("pointerdown",100,120);this.point("pointermove",900,480);this.point("pointerup",900,480);}};
}
(async()=>{
  const b=bench();await tick();await b.el("avvia").events.click();assert.equal(b.el("scatta").disabled,true,"serve una configurazione");
  b.el("configura").events.click();b.rectangle();b.el("salva").events.click();assert.equal(b.el("scatta").disabled,false);
  const profile=JSON.parse(b.storage.get("rfid.sam2.area.v2.cam"));assert.deepEqual(profile.area,[.1,.2,.9,.8]);
  await b.el("scatta").events.click();assert.equal(b.calls.length,1);assert(b.calls[0].url.endsWith("/automatico"));
  assert.equal(Buffer.from(b.calls[0].data.immagine_base64,"base64").toString(),"1536x648","analizza solo i pixel originali della selezione");
  assert(!("oggetti" in b.calls[0].data));assert(!("attesi" in b.calls[0].data));assert.match(b.el("conteggio").textContent,/riconosciuti: 6/);
  assert(b.draws.some(d=>d.args[0]===b.el("video")&&JSON.stringify(d.args.slice(1))==="[192,216,1536,648,0,0,1536,648]"));
  const restored=bench(b.storage);await tick();await restored.el("avvia").events.click();assert.equal(restored.el("scatta").disabled,false,"calibrazione ripristinata");
  restored.el("camera").value="cam2";await restored.el("camera").events.change();assert.equal(restored.el("scatta").disabled,true,"non riusa area di un'altra webcam");
  const changed=bench(b.storage);changed.el("video").videoHeight=1440;await tick();await changed.el("avvia").events.click();assert.equal(changed.el("scatta").disabled,true,"aspect ratio cambiato richiede nuova area");
  b.el("nuova").events.click();let resolve;b.reply=new Promise(r=>{resolve=r;});const pending=b.el("scatta").events.click();b.el("nuova").events.click();
  resolve({ok:true,json:async()=>({conteggio:99,tempo_secondi:1,oggetti:[]})});await pending;assert.match(b.el("conteggio").textContent,/riconosciuti: —/,"scarta risposta della foto precedente");
  const quad=bench();await tick();await quad.el("avvia").events.click();quad.el("modo-area").value="prospettiva";quad.el("modo-area").events.change();quad.el("configura").events.click();
  for(const [x,y] of [[100,100],[900,150],[850,500],[150,480]])quad.point("pointerdown",x,y);
  quad.el("larghezza").value="60";quad.el("lunghezza").value="40";quad.el("altezza-camera").value="80";quad.el("salva").events.click();
  const saved=JSON.parse(quad.storage.get("rfid.sam2.area.v2.cam"));assert.equal(saved.punti.length,4);assert.equal(saved.altezza_camera_cm,80);
  await quad.el("scatta").events.click();assert.equal(quad.calls.length,2);assert(quad.calls[0].url.endsWith("/prepara"));
  assert.equal(quad.calls[0].data.calibrazione.larghezza_cm,60);assert.equal(quad.calls[0].data.calibrazione.lunghezza_cm,40);
  assert.notEqual(Buffer.from(quad.calls[0].data.immagine_base64,"base64").toString(),"1920x1080","anche la rettifica riceve solo la zona selezionata con un piccolo margine");
  assert(quad.calls[0].data.calibrazione.punti.every(p=>p.every(v=>v>=0&&v<=1)),"angoli ricondotti alle coordinate del ritaglio");
  assert.equal(quad.calls[1].data.immagine_base64,"RECTIFIED","SAM analizza la foto rettificata, non l'originale");
  assert.match(quad.el("conteggio").textContent,/riconosciuti: 6/);
  // Confronto sulla stessa foto: Qwen riceve gli stessi byte rettificati,
  // non un nuovo frame e non i pixel con i contorni disegnati sull'overlay.
  quad.el("motore").value="qwen";quad.el("motore").events.change();
  assert.equal(quad.el("riconta").disabled,false);
  const oldDraws=quad.draws.length;
  const recounted=quad.el("riconta").events.click();
  assert.equal(quad.el("motore").disabled,true,"modello bloccato durante l'analisi");
  await recounted;
  assert.equal(quad.calls.length,3);
  assert(quad.calls[2].url.endsWith("/qwen/automatico"));
  assert.equal(quad.calls[2].data.immagine_base64,"RECTIFIED");
  assert.equal(quad.draws.slice(oldDraws).filter(d=>d.args[0]===quad.el("video")).length,0,"nessun nuovo scatto nella rianalisi");
  const remembered=bench(quad.storage);await tick();assert.equal(remembered.el("motore").value,"qwen");
  // YOLO: terzo motore sulla stessa foto rettificata, endpoint dedicato.
  quad.el("motore").value="yolo";quad.el("motore").events.change();
  await quad.el("riconta").events.click();
  assert.equal(quad.calls.length,4);
  assert(quad.calls[3].url.endsWith("/yolo/automatico"),quad.calls[3].url);
  assert.equal(quad.calls[3].data.immagine_base64,"RECTIFIED","YOLO analizza gli stessi byte di SAM e Qwen");
  assert.equal(bench(quad.storage).el("motore").value,"yolo","la scelta YOLO è ricordata");
  assert.equal(bench(new Map()).el("motore").value,"yolo","senza scelta salvata il motore predefinito è YOLO");
  assert.equal(bench(new Map([["rfid.visione.motore.v1","sam2"]])).el("motore").value,"sam2","una scelta salvata resta valida");
  quad.el("nuova").events.click();assert.equal(quad.el("riconta").disabled,true,"la nuova anteprima invalida il vecchio scatto");
  const noDims=bench();await tick();await noDims.el("avvia").events.click();noDims.el("configura").events.click();noDims.rectangle();noDims.el("larghezza").value="60";noDims.el("salva").events.click();assert.equal(noDims.storage.size,0,"rifiuta dimensioni incomplete");
  const late=bench();await tick();let permission;late.media=new Promise(r=>{permission=r;});const opening=late.el("avvia").events.click();late.el("ferma").events.click();permission(late.stream);await opening;assert.equal(late.stops,1);
  const embedded=bench(new Map(),"?t=token&port=8772&impostazioni=1&device=cam2&motore=qwen");await tick();
  assert.equal(embedded.el("camera").value,"cam2","calibra la webcam scelta nel pannello principale");
  assert.equal(embedded.el("camera").closest("label").hidden,true);
  assert.equal(embedded.el("motore").closest(".comandi").hidden,true);
  assert.equal(embedded.el("scatta").closest(".comandi").hidden,true);
  await embedded.el("avvia").events.click();embedded.el("configura").events.click();embedded.rectangle();embedded.el("salva").events.click();
  assert(embedded.storage.has("rfid.sam2.area.v2.cam2"));
  await embedded.el("scatta").events.click();assert.equal(embedded.calls.length,0,"il riquadro configurazione non invia foto ai modelli");
  const missing=bench(new Map(),"?impostazioni=1&device=assente");await tick();
  assert.equal(missing.el("avvia").disabled,true,"non sostituisce in silenzio la webcam salvata assente");
  assert.match(missing.el("stato").textContent,/Videocamera selezionata non disponibile/);
  console.log("PASS SAM automatico UI: ritaglio nativo, persistenza/webcam/aspect ratio, quattro angoli/misure, rettifica prima di SAM e risposte obsolete");
})().catch(e=>{console.error(e);process.exitCode=1;});
