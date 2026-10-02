/* Archivio ricezioni: aggiornamento dopo conferma e rilettura, senza browser. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Elemento {
  constructor() {
    this.eventi = {};
    this.children = [];
    this.dataset = {};
    this.value = "";
    this.textContent = "";
  }
  addEventListener(nome, fn) { this.eventi[nome] = fn; }
  replaceChildren(...nodi) { this.children = nodi; }
  append(...nodi) { this.children.push(...nodi); }
}

function banco() {
  const elementi = new Map();
  const $ = id => {
    if (!elementi.has(id)) elementi.set(id, new Elemento());
    return elementi.get(id);
  };
  const chiamate = [], avvisi = [];
  let ricevuta = false, erroreArchivio = false, erroreConferma = false;
  const documento = new Elemento();
  documento.querySelector = $;
  documento.querySelectorAll = () => [];
  documento.createElement = () => new Elemento();
  const contesto = vm.createContext({
    document: documento, window: {}, URLSearchParams, location: { search: "?t=prova" },
    fetch: async (url, richiesta) => {
      const azione = url.replace("/api/", "");
      chiamate.push(azione);
      let risposta;
      if (azione === "distinte_attese") {
        if (erroreArchivio) throw new Error("rete non disponibile");
        risposta = { distinte: [{ id: 1, origin_lab_id: 1, origin_shipment_id: 7,
          item_count: 2, state: ricevuta ? "received" : "open" }], non_indicizzate: [] };
      } else if (azione === "conferma_ricezione") {
        assert.equal(JSON.parse(richiesta.body).inbound_id, 1);
        if (erroreConferma) throw new Error("conferma rifiutata");
        ricevuta = true;
        risposta = { inbound_id: 1, stato: "received" };
      } else if (azione === "leggi_volume") {
        assert.equal(JSON.parse(richiesta.body).inbound_id, 1);
        ricevuta = false;
        risposta = { riconciliazione: { ok: true } };
      } else throw new Error(`Chiamata inattesa: ${azione}`);
      return { ok: true, json: async () => risposta };
    },
    registraAvviso: testo => avvisi.push(testo),
  });
  for (const nome of ["app.js", "exchange.js"]) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, "../webui/static", nome), "utf8"), contesto);
  }
  // Il disegno del volume è indipendente dall'archivio sottoposto a prova.
  vm.runInContext(`
    avvisa = registraAvviso;
    dipingiStatoLettore = () => {};
    dipingiRicezione = () => { stato.distinta.stato = "open"; };
    stato.distinta = { inbound_id: 1, stato: "open" };
  `, contesto);
  return { $, chiamate, avvisi,
    aggiorna: () => contesto.window.aggiornaArchivioRicezione(),
    conferma: () => contesto.confermaRicezione(),
    rileggi: () => contesto.leggiVolume(),
    get etichetta() { return $("#distinte-attese").children[0]?.textContent; },
    set erroreArchivio(v) { erroreArchivio = v; },
    set erroreConferma(v) { erroreConferma = v; },
  };
}

(async () => {
  const b = banco();
  await b.aggiorna();
  assert.match(b.etichetta, /da verificare/);
  await b.conferma();
  assert.match(b.etichetta, /ricezione completata/, "la conferma aggiorna subito l'elenco");
  assert.equal(b.$("#esporta-riscontro").disabled, false);
  await b.rileggi();
  assert.match(b.etichetta, /da verificare/, "la rilettura riapre la voce nell'elenco");
  assert.match(b.$("#workflow-stato").textContent, /Ricezione 1: open/);
  console.log("PASS archivio UI: conferma e rilettura aggiornano elenco e stato");

  b.erroreArchivio = true;
  await b.conferma();
  assert.equal(b.$("#esporta-riscontro").disabled, false);
  assert.ok(b.avvisi.some(t => t.includes("Archivio ricezioni non aggiornato")));
  assert.ok(b.avvisi.includes("Ricezione registrata nella catena di custodia"));
  b.erroreArchivio = false;
  await b.aggiorna();
  assert.match(b.etichetta, /ricezione completata/);
  console.log("PASS archivio UI: errore di aggiornamento conserva l'esito e consente di riprovare");

  const rifiutata = banco();
  await rifiutata.aggiorna();
  rifiutata.erroreConferma = true;
  await rifiutata.conferma();
  assert.match(rifiutata.etichetta, /da verificare/);
  assert.deepEqual(rifiutata.chiamate, ["distinte_attese", "conferma_ricezione"]);
  assert.ok(rifiutata.avvisi.includes("conferma rifiutata"));
  console.log("PASS archivio UI: una conferma rifiutata non anticipa lo stato ricevuto");
})().catch(errore => { console.error(errore); process.exitCode = 1; });
