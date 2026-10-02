/* Regressioni della webcam WebUI con sorgente e decoder simulati, senza browser. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const sorgente = fs.readFileSync(path.join(__dirname, "../webui/static/qr-camera.js"), "utf8");

class Elemento {
  constructor() {
    this.eventi = {};
    this.value = "";
    this.videoWidth = 640;
    this.videoHeight = 480;
    this.readyState = 2;
    this.hidden = true;
  }
  addEventListener(nome, fn) { this.eventi[nome] = fn; }
  replaceChildren() {}
  append() {}
  play() { return Promise.resolve(); }
  createTHead() { return this; }
  createTBody() { return this; }
  insertRow() { return this; }
  insertCell() { return new Elemento(); }
  toDataURL() { return "data:image/jpeg;base64,AAA="; }
  getContext() {
    return { drawImage() {}, clearRect() {}, beginPath() {}, moveTo() {}, lineTo() {},
      closePath() {}, stroke() {}, getImageData: () => ({ data: [200, 200, 200, 255] }) };
  }
}

function banco() {
  const elementi = new Map();
  const $ = id => { if (!elementi.has(id)) elementi.set(id, new Elemento()); return elementi.get(id); };
  const document = new Elemento();
  document.hidden = false;
  document.createElement = () => new Elemento();
  document.querySelector = () => null;
  const window = new Elemento();
  const stato = { scansioni: [], distintaQr: { righe: [] } };
  let orologio = 0, stop = 0, acquisizioni = 0, richieste = 0, completa = false, camera;
  const track = { stop: () => stop++, getSettings: () => ({ deviceId: "cam1" }), addEventListener() {} };
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] };
  const mediaDevices = { getUserMedia: () => camera || Promise.resolve(stream),
    enumerateDevices: async () => [{ kind: "videoinput", deviceId: "cam1", label: "Webcam prova" }] };
  const timers = new Map();
  let timerId = 0;
  vm.runInNewContext(sorgente, { $, document, window, stato, navigator: { mediaDevices }, TOKEN: "test",
    performance: { now: () => orologio }, AbortController,
    setTimeout: (fn, ms) => { timers.set(++timerId, { fn, ms }); return timerId; },
    clearTimeout: id => timers.delete(id), dipingiParti() {}, schermataAttiva: () => "ricezione",
    leggiScansione: async testi => { acquisizioni++; assert.equal(testi[0], "RFQ1:prova"); return completa; },
    fetch: async () => { richieste++; return { ok: true, json: async () => ({ larghezza: 640, altezza: 480,
      codici: [{ testo: "RFQ1:prova", vertici: [[0, 0], [100, 0], [100, 100], [0, 100]] }] }) }; },
  });
  return { $, document, window, stream,
    get acquisizioni() { return acquisizioni; }, get stop() { return stop; }, get richieste() { return richieste; },
    set completa(value) { completa = value; }, set tempo(value) { orologio = value; }, set camera(value) { camera = value; },
    click: id => $(id).eventi.click(),
    spazio: (opts = {}) => document.eventi.keydown({ code: "Space", target: { closest: () => null }, preventDefault() {}, ...opts }),
    ciclo() { for (const [id, t] of timers) { if (t.ms === 350) { timers.delete(id); t.fn(); break; } } },
  };
}

const aggiorna = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  const b = banco();
  await b.click("#qr-camera-avvia"); await aggiorna();
  assert.equal(b.acquisizioni, 0, "il riconoscimento non deve acquisire automaticamente");
  assert.equal(b.$("#qr-camera-acquisisci").disabled, false);
  b.spazio({ repeat: true });
  b.spazio({ target: { closest: () => ({}) } });
  assert.equal(b.acquisizioni, 0, "Spazio ripetuto o in un campo non deve acquisire");
  b.spazio(); b.spazio(); await aggiorna();
  assert.equal(b.acquisizioni, 1, "una pressione durante la richiesta non deve duplicarla");
  assert.equal(b.stop, 0, "le parti mancanti lasciano attiva la webcam");
  b.tempo = 2000;
  await b.click("#qr-camera-acquisisci");
  assert.equal(b.acquisizioni, 1, "non acquisire un QR non più visibile");
  b.ciclo(); await aggiorna();
  b.completa = true;
  await b.click("#qr-camera-acquisisci");
  assert.equal(b.acquisizioni, 2);
  assert.equal(b.stop, 1, "la distinta completa rilascia la fotocamera");
  assert.equal(b.$("#qr-camera-dati").hidden, false);
  const richieste = b.richieste;
  b.ciclo(); await aggiorna();
  assert.equal(b.richieste, richieste, "nessuna decodifica dopo lo spegnimento");
  b.window.azzeraWebcamRicezione();
  assert.equal(b.$("#qr-camera-dati").hidden, true, "la selezione di un file toglie i dati QR precedenti");

  const tardiva = banco();
  let apri;
  tardiva.camera = new Promise(resolve => { apri = resolve; });
  const apertura = tardiva.click("#qr-camera-avvia");
  assert.equal(tardiva.$("#qr-camera-ferma").disabled, false);
  tardiva.window.fermaWebcamRicezione();
  apri(tardiva.stream); await apertura; await aggiorna();
  assert.equal(tardiva.stop, 1, "un permesso tardivo non riattiva la webcam dopo l'uscita");
  assert.equal(tardiva.richieste, 0);
  console.log("PASS webcam UI: acquisizione manuale, Spazio, duplicati, scadenza QR, spegnimento e permesso tardivo");
})().catch(error => { console.error(error); process.exitCode = 1; });
