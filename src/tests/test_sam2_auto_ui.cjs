/* Ritaglio nativo persistente, quattro angoli, rettifica prima del conteggio. */
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const source=fs.readFileSync(require("node:path").join(__dirname,"../webui/static/sam2-auto.js"),"utf8");
const controlsSource=fs.readFileSync(require("node:path").join(__dirname,"../webui/static/webcam-controls.js"),"utf8");
const tick=()=>new Promise(r=>setImmediate(r));
function bench(storage=new Map(),search="?t=token&port=8772"){
  const elements=new Map(),calls=[],draws=[],timers=new Map(),mediaCalls=[],constraintCalls=[],deviceEvents={};let serial=0,stops=0,media=null,reply=null,constraints=null;
  let devices=[{kind:"videoinput",deviceId:"cam",label:"C920"},{kind:"videoinput",deviceId:"cam2",label:"Altra"}];
  let capabilities={width:{max:2592},height:{max:1944}},settings={},trackConstraints={width:{ideal:1920},height:{ideal:1080}},ignoreControls=false;
  class El{
    constructor(){this.events={};this.value="";this.width=1280;this.height=720;this.videoWidth=1920;this.videoHeight=1080;this.readyState=2;this.classList={toggle(){}};}
    addEventListener(n,f){this.events[n]=f;}replaceChildren(...a){this.options=a;}
    append(...a){this.children=a;}set id(v){this._id=v;elements.set(v,this);}get id(){return this._id;}
    closest(name){return el("parent-"+name+"-"+this.id);}get parentElement(){return this.closest("parent");}
    getContext(){const self=this;if(!this.ctx)this.ctx={drawImage(...a){draws.push({canvas:self,args:a,filter:this.filter||"none"});},getImageData(){return {data:new Uint8ClampedArray(96*72*4).fill(64)};},beginPath(){},lineTo(){},moveTo(){},closePath(){},fill(){},stroke(){},arc(){},fillText(){},fillRect(){},strokeRect(){},setLineDash(){}};return this.ctx;}
    play(){return Promise.resolve();}setPointerCapture(){}
    toDataURL(){return "data:image/jpeg;base64,"+Buffer.from(`${this.width}x${this.height}`).toString("base64");}
    getBoundingClientRect(){return {left:0,top:0,width:1000,height:600};}
  }
  const el=id=>{if(!elements.has(id)){const e=new El();e.id=id;elements.set(id,e);}return elements.get(id);};
  el("modo-area").value="rettangolo";const win=new El();
  const track={stop:()=>stops++,getSettings:()=>({deviceId:el("camera").value||"cam",frameRate:15,...settings}),getCapabilities:()=>capabilities,getConstraints:()=>trackConstraints,applyConstraints:async c=>{constraintCalls.push(c);if(constraints)return constraints;trackConstraints=c;if(c.width){el("video").videoWidth=c.width.ideal;el("video").videoHeight=c.height.ideal;}if(!ignoreControls)for(const item of c.advanced||[])Object.assign(settings,item);}};
  const stream={getTracks:()=>[track],getVideoTracks:()=>[track]};
  const context={document:{getElementById:el,createElement:()=>new El(),querySelector:el},window:win,location:{search},URLSearchParams,AbortController,Date,
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    Image:class{decode(){return Promise.resolve();}},requestAnimationFrame:()=>1,cancelAnimationFrame(){},
    navigator:{mediaDevices:{enumerateDevices:async()=>devices,addEventListener:(n,f)=>{deviceEvents[n]=f;},getUserMedia:c=>{mediaCalls.push(c);return media||Promise.resolve(stream);}}},
    setTimeout:(f,ms)=>{timers.set(++serial,{f,ms});return serial;},clearTimeout:i=>timers.delete(i),setInterval:()=>++serial,clearInterval(){},
    fetch:async(url,options)=>{
      calls.push({url,data:JSON.parse(options.body)});if(reply)return reply;
      if(url.endsWith("/prepara"))return {ok:true,json:async()=>({immagine_base64:"RECTIFIED",dimensioni:[900,600],correzione:{nota:"piano corretto",avvisi:[]}})};
      return {ok:true,json:async()=>({conteggio:6,tempo_secondi:85,tempo_bordo_secondi:12,tempo_conteggio_secondi:73,oggetti:[],borsa:{rilevata:false},avvisi:[]})};
    }};
  vm.createContext(context);vm.runInContext(controlsSource,context);vm.runInContext(source,context);
  return {el,calls,draws,stream,timers,storage,mediaCalls,constraintCalls,deviceEvents,get stops(){return stops;},set media(p){media=p;},set reply(p){reply=p;},set constraints(p){constraints=p;},set devices(d){devices=d;},set capabilities(c){capabilities=c;},set settings(s){settings=s;},set ignoreControls(v){ignoreControls=v;},
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
  await missing.el("aggiorna-camera").events.click();assert.equal(missing.el("avvia").disabled,true,"aggiornare non sostituisce il dispositivo richiesto nelle Impostazioni");
  // Risoluzioni effettive inferiori alla richiesta e scelta persistente per camera.
  const low=bench();await tick();low.el("video").videoWidth=640;low.el("video").videoHeight=480;
  await low.el("avvia").events.click();assert.equal(low.mediaCalls[0].video.width.ideal,1920);
  assert.match(low.el("qualita").textContent,/Webcam effettiva: 640×480/);
  assert.match(low.el("qualita").textContent,/meno pixel/);
  low.el("risoluzione").value="1944";await low.el("risoluzione").events.change();
  assert.equal(low.stops,1,"rilascia lo stream prima di cambiare risoluzione");
  assert.equal(low.mediaCalls[1].video.width.ideal,2592);assert.equal(low.mediaCalls[1].video.height.ideal,1944);
  assert.equal(low.storage.get("rfid.sam2.risoluzione.v1.cam"),"1944");
  const res=bench(low.storage);await tick();assert.equal(res.el("risoluzione").value,"1944");
  res.el("camera").value="cam2";await res.el("camera").events.change();assert.equal(res.el("risoluzione").value,"1080","risoluzione distinta per camera");
  const max=bench();await tick();max.el("risoluzione").value="massima";await max.el("avvia").events.click();
  assert.equal(max.constraintCalls[0].width.ideal,2592);assert.equal(max.constraintCalls[0].height.ideal,1944);
  assert.match(max.el("qualita").textContent,/Webcam effettiva: 2592×1944/);
  const oldProfile=bench(b.storage);await tick();await oldProfile.el("avvia").events.click();await oldProfile.el("scatta").events.click();
  oldProfile.el("risoluzione").value="massima";await oldProfile.el("risoluzione").events.change();
  assert.equal(oldProfile.el("scatta").disabled,true,"nuovo formato 4:3 richiede calibrazione");
  assert.equal(oldProfile.el("riconta").disabled,true,"non riusa lo scatto della risoluzione precedente");
  assert.equal(oldProfile.calls.length,1,"il cambio risoluzione non invia foto ai modelli");
  const lateMax=bench();await tick();lateMax.el("risoluzione").value="massima";let applied;lateMax.constraints=new Promise(r=>{applied=r;});
  const maxOpening=lateMax.el("avvia").events.click();await tick();lateMax.el("ferma").events.click();applied();await maxOpening;
  assert.equal(lateMax.stops,1);assert.equal(lateMax.el("video").srcObject,null,"non riattiva la camera dopo Stop durante la scelta massima");
  const fourK=bench();await tick();fourK.el("video").videoWidth=3840;fourK.el("video").videoHeight=2160;
  fourK.el("risoluzione").value="2160";await fourK.el("avvia").events.click();fourK.el("configura").events.click();fourK.rectangle();fourK.el("salva").events.click();
  await fourK.el("scatta").events.click();assert.equal(Buffer.from(fourK.calls[0].data.immagine_base64,"base64").toString(),"1920x810","ritaglia i pixel 4K prima del limite Full HD");
  assert.match(fourK.el("qualita").textContent,/Scatto: 1920×810/);
  const plugged=bench();await tick();plugged.devices=[{kind:"videoinput",deviceId:"cam",label:"C920"},{kind:"videoinput",deviceId:"cam2",label:"Netum"},{kind:"videoinput",deviceId:"cam3",label:"Terza camera"}];
  await plugged.el("aggiorna-camera").events.click();assert.equal(plugged.el("camera").options.length,3,"ricarica anche la terza camera collegata dopo l'avvio");
  assert.equal(plugged.el("camera").value,"cam","aggiornare non cambia camera");assert.equal(plugged.mediaCalls.length,0,"non apre una webcam né chiede nuovi consensi");
  await plugged.el("avvia").events.click();plugged.devices=[{kind:"videoinput",deviceId:"cam3",label:"Terza camera"}];plugged.deviceEvents.devicechange();await tick();
  assert.equal(plugged.stops,1,"rilascia la webcam scollegata");assert.equal(plugged.el("camera").value,"cam","non sostituisce la camera in silenzio");assert.equal(plugged.el("avvia").disabled,true);
  plugged.devices=[{kind:"videoinput",deviceId:"cam",label:"C920"},{kind:"videoinput",deviceId:"cam3",label:"Terza camera"}];plugged.deviceEvents.devicechange();await tick();
  assert.equal(plugged.el("avvia").disabled,false,"camera ricollegata disponibile");assert.equal(plugged.el("camera").options.length,2);
  const image=bench();await tick();await image.el("avvia").events.click();
  assert.match(image.el("stato-regolazioni").textContent,/non espone regolazioni/);assert.equal(image.el("auto-webcam").disabled,true);
  image.el("software-brightness").value="120";image.el("software-brightness").events.input();image.el("software-contrast").value="130";image.el("software-contrast").events.input();
  image.el("configura").events.click();image.rectangle();image.el("salva").events.click();await image.el("scatta").events.click();
  assert(image.draws.some(d=>d.args[0]===image.el("video")&&d.args.length===9&&d.filter==="brightness(120%) contrast(130%) saturate(100%)"),"i pixel del ritaglio vengono corretti prima dell'invio");
  assert(image.draws.filter(d=>d.args[0]!==image.el("video")).every(d=>d.filter==="none"),"la correzione non viene applicata di nuovo all'overlay o alla foto rettificata");
  assert.equal(image.el("software-brightness").disabled,true,"foto congelata: non può modificare i pixel della rianalisi");
  image.el("nuova").events.click();assert.equal(image.el("software-brightness").disabled,false);
  image.el("auto-immagine").checked=true;image.el("auto-immagine").events.change();assert.equal(image.el("software-brightness").disabled,true);
  assert.match(image.el("esito-auto-immagine").textContent,/luminosità 140%, contrasto 100%/);await image.el("scatta").events.click();
  assert(image.draws.some(d=>d.filter==="brightness(140%) contrast(100%) saturate(100%)"),"automatico software limitato su un'immagine scura uniforme");
  image.el("nuova").events.click();image.el("ripristina-immagine").events.click();assert.equal(image.el("auto-immagine").checked,false);assert.equal(image.el("software-brightness").value,100);
  const hardware=bench();await tick();hardware.capabilities={brightness:{min:0,max:255,step:1},exposureMode:["continuous","manual"],exposureTime:{min:1,max:100,step:1},focusMode:["continuous","manual"],whiteBalanceMode:["continuous","manual"]};
  hardware.settings={brightness:100,exposureMode:"continuous",exposureTime:25,focusMode:"manual",whiteBalanceMode:"manual"};await hardware.el("avvia").events.click();
  assert.equal(hardware.el("webcam-exposureTime").disabled,true,"esposizione manuale bloccata in modalità automatica");
  hardware.el("webcam-brightness").value="130";await hardware.el("webcam-brightness").events.change();
  assert.equal(hardware.constraintCalls[0].width.ideal,1920,"mantiene i vincoli di risoluzione");assert.equal(hardware.el("webcam-brightness").value,130);
  hardware.el("webcam-exposureMode").value="manual";await hardware.el("webcam-exposureMode").events.change();assert.equal(hardware.el("webcam-exposureTime").disabled,false);
  await hardware.el("auto-webcam").events.click();assert.equal(hardware.el("webcam-exposureMode").value,"continuous");assert.equal(hardware.el("webcam-focusMode").value,"continuous");
  hardware.ignoreControls=true;hardware.el("webcam-brightness").value="150";await hardware.el("webcam-brightness").events.change();
  assert.match(hardware.el("stato-regolazioni").textContent,/non ha confermato/);assert.equal(hardware.el("webcam-brightness").value,130,"ripresenta il valore effettivo se il driver ignora la richiesta");
  hardware.ignoreControls=false;await hardware.el("ripristina-webcam").events.click();assert.equal(hardware.el("webcam-brightness").value,100);assert.equal(hardware.el("webcam-focusMode").value,"manual");
  let finishControl;hardware.constraints=new Promise(r=>{finishControl=r;});hardware.el("webcam-brightness").value="110";const adjustment=hardware.el("webcam-brightness").events.change();
  assert.equal(hardware.el("camera").disabled,true);hardware.el("ferma").events.click();finishControl();await adjustment;
  assert.equal(hardware.el("webcam-brightness").disabled,true,"una risposta tardiva non riattiva i controlli della camera spenta");
  console.log("PASS SAM automatico UI: ritaglio nativo, persistenza/webcam/aspect ratio, quattro angoli/misure, rettifica prima di SAM e risposte obsolete");
})().catch(e=>{console.error(e);process.exitCode=1;});
