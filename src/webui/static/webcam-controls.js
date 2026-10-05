/* Regolazioni reali della webcam e correzione locale dei pixel dello scatto. */
(() => {
  "use strict";
  const numeric={brightness:"Luminosità",contrast:"Contrasto",saturation:"Saturazione",sharpness:"Nitidezza",exposureCompensation:"Compensazione esposizione",exposureTime:"Tempo di esposizione",colorTemperature:"Temperatura colore",focusDistance:"Distanza di messa a fuoco",iso:"Sensibilità ISO"};
  const modes={exposureMode:"Esposizione",focusMode:"Messa a fuoco",whiteBalanceMode:"Bilanciamento del bianco"};
  const modeLabels={continuous:"Automatica continua",manual:"Manuale","single-shot":"Automatica singola",none:"Non regolabile"};
  const dependencies={exposureTime:"exposureMode",focusDistance:"focusMode",colorTemperature:"whiteBalanceMode"};
  window.RFIDCameraControls=function({onBusy,onChange}){
    const el=id=>document.getElementById(id),rows=new Map(),sample=document.createElement("canvas");
    let track=null,caps={},initial={},pending=false,disabled=true,epoch=0,lastAuto=0,cameraId=null,autoValues={brightness:100,contrast:100};
    const software={brightness:100,contrast:100,saturation:100};
    const status=text=>{el("stato-regolazioni").textContent=text;};
    const settingText=value=>typeof value==="number"?String(Math.round(value*1000)/1000):modeLabels[value]||value||"Non comunicato";
    function lock(){
      const settings=track?.getSettings()||{},blocked=disabled||pending||!track;
      for(const [name,row] of rows){row.input.disabled=blocked||(dependencies[name]&&caps[dependencies[name]]&&settings[dependencies[name]]!=="manual");}
      el("auto-webcam").disabled=blocked||!Object.keys(modes).some(name=>caps[name]?.some(v=>v==="continuous"||v==="single-shot"));
      el("ripristina-webcam").disabled=blocked||!Object.keys(initial).length;
      el("auto-immagine").disabled=blocked;
      for(const name of Object.keys(software))el(`software-${name}`).disabled=blocked||(el("auto-immagine").checked&&name!=="saturation");
      el("ripristina-immagine").disabled=blocked;
    }
    function sync(){
      const settings=track?.getSettings()||{};
      for(const [name,row] of rows){if(settings[name]!==undefined)row.input.value=settings[name];row.output.textContent=settingText(settings[name]);}
      lock();
    }
    async function apply(values,message){
      if(disabled||pending||!track)return;
      const current=track,turn=epoch;pending=true;lock();onBusy(true);
      try{
        // Conserva risoluzione e tutte le regolazioni già richieste.
        const old=current.getConstraints?.()||{},advanced=(old.advanced||[]).map(v=>({...v}));
        for(const item of advanced)for(const name of Object.keys(values))delete item[name];
        advanced.push(values);
        await current.applyConstraints({...old,advanced});
        if(turn!==epoch||track!==current)return;
        const effective=current.getSettings(),ignored=Object.keys(values).filter(name=>effective[name]===undefined||(typeof values[name]==="number"?Math.abs(effective[name]-values[name])>Math.max(.001,(caps[name]?.step||0)/2):effective[name]!==values[name]));
        status(ignored.length?`La webcam non ha confermato: ${ignored.map(name=>numeric[name]||modes[name]).join(", ")}. Controlla l’immagine e i valori effettivi.`:message);
        onChange();sync();
      }catch(e){if(turn===epoch){status(`Regolazione non applicata: ${e.message}`);sync();}}
      finally{if(turn===epoch){pending=false;lock();onBusy(false);}}
    }
    function row(name,label,cap,value){
      const wrapper=document.createElement("label"),title=document.createElement("span"),output=document.createElement("output");title.textContent=label;
      const input=document.createElement(Array.isArray(cap)?"select":"input");input.id=`webcam-${name}`;
      if(Array.isArray(cap))input.replaceChildren(...cap.map(v=>{const o=document.createElement("option");o.value=v;o.textContent=modeLabels[v]||v;return o;}));
      else{input.type="range";input.min=cap.min;input.max=cap.max;input.step=cap.step>0?cap.step:"any";}
      input.value=value??(Array.isArray(cap)?cap[0]:cap.min);output.textContent=settingText(value);
      input.addEventListener("input",()=>{output.textContent=settingText(Array.isArray(cap)?input.value:Number(input.value));});
      input.addEventListener("change",()=>apply({[name]:Array.isArray(cap)?input.value:Number(input.value)},`${label} applicata. Valore effettivo mostrato accanto al controllo.`));
      wrapper.append(title,input,output);rows.set(name,{input,output});return wrapper;
    }
    function open(newTrack){
      close();track=newTrack;try{caps=track.getCapabilities?.()||{};}catch(_e){caps={};}
      const settings=track.getSettings(),nodes=[];
      if(cameraId!==settings.deviceId){
        cameraId=settings.deviceId;el("auto-immagine").checked=false;
        for(const name of Object.keys(software)){software[name]=100;el(`software-${name}`).value=100;el(`valore-${name}`).textContent="100%";}
      }
      lastAuto=0;
      for(const [name,label] of Object.entries(modes))if(Array.isArray(caps[name])&&caps[name].length>1){nodes.push(row(name,label,caps[name],settings[name]));if(settings[name]!==undefined)initial[name]=settings[name];}
      for(const [name,label] of Object.entries(numeric)){
        const range=caps[name];if(!range||!Number.isFinite(range.min)||!Number.isFinite(range.max)||range.max<=range.min)continue;
        nodes.push(row(name,label,range,settings[name]));if(Number.isFinite(settings[name]))initial[name]=settings[name];
      }
      el("controlli-webcam").replaceChildren(...nodes);status(nodes.length?"Regola i controlli esposti da questa webcam, oppure premi Automatico webcam.":"Questa webcam non espone regolazioni immagine al browser. Puoi usare la correzione software sotto.");
      lock();
    }
    function close(){epoch++;track=null;caps={};initial={};pending=false;rows.clear();el("controlli-webcam").replaceChildren();status("Attiva la webcam per vedere le regolazioni disponibili.");lock();onBusy(false);}
    el("auto-webcam").addEventListener("click",()=>{
      const values={};for(const name of Object.keys(modes)){const supported=caps[name];if(supported?.includes("continuous"))values[name]="continuous";else if(supported?.includes("single-shot"))values[name]="single-shot";}
      if(Object.keys(values).length)return apply(values,"Regolazioni automatiche della webcam attivate. Attendi che l’immagine si stabilizzi prima di scattare.");
    });
    el("ripristina-webcam").addEventListener("click",()=>apply({...initial},"Ripristinati i valori della webcam rilevati all’attivazione."));
    for(const name of Object.keys(software)){
      el(`software-${name}`).value=100;
      el(`software-${name}`).addEventListener("input",()=>{if(disabled||pending||!track)return;software[name]=Number(el(`software-${name}`).value);el(`valore-${name}`).textContent=`${software[name]}%`;onChange();});
    }
    el("auto-immagine").addEventListener("change",()=>{if(disabled||pending||!track)return;lastAuto=0;lock();onChange();});
    el("ripristina-immagine").addEventListener("click",()=>{
      if(disabled||pending||!track)return;el("auto-immagine").checked=false;lastAuto=0;
      for(const name of Object.keys(software)){software[name]=100;el(`software-${name}`).value=100;el(`valore-${name}`).textContent="100%";}
      lock();onChange();
    });
    function filter(source,area,force=false){
      let values=software;
      if(el("auto-immagine").checked){
        if(force||!lastAuto||Date.now()-lastAuto>800){
          const w=source.videoWidth||source.width,h=source.videoHeight||source.height;
          if(w&&h){
            sample.width=96;sample.height=72;const ctx=sample.getContext("2d",{willReadFrequently:true});
            const rect=area||[0,0,w,h];ctx.drawImage(source,...rect,0,0,96,72);
            try{
              const pixels=ctx.getImageData(0,0,96,72).data,hist=new Uint32Array(256);
              for(let i=0;i<pixels.length;i+=4)hist[Math.round(.2126*pixels[i]+.7152*pixels[i+1]+.0722*pixels[i+2])]++;
              let total=0,lo=0,hi=255;for(let i=0;i<256;i++){total+=hist[i];if(total<pixels.length/4*.05)lo=i;if(total>=pixels.length/4*.95){hi=i;break;}}
              // Correzione moderata: non amplifica senza limiti una scena nera o uniforme.
              const midpoint=(lo+hi)/2;autoValues={brightness:Math.round(Math.max(80,Math.min(140,128/Math.max(32,midpoint)*100))),contrast:hi-lo<24?100:Math.round(Math.max(85,Math.min(135,180/(hi-lo)*100)))};
              lastAuto=Date.now();el("esito-auto-immagine").textContent=`Correzione automatica: luminosità ${autoValues.brightness}%, contrasto ${autoValues.contrast}%.`;
            }catch(_e){autoValues={brightness:100,contrast:100};el("esito-auto-immagine").textContent="Correzione automatica non disponibile per questo fotogramma.";}
          }
        }
        values={...autoValues,saturation:software.saturation};
      }else el("esito-auto-immagine").textContent="Correzione automatica spenta; usa i cursori manuali.";
      return `brightness(${values.brightness}%) contrast(${values.contrast}%) saturate(${values.saturation}%)`;
    }
    return {open,close,filter,setDisabled(value){disabled=value;lock();},get pending(){return pending;}};
  };
})();
