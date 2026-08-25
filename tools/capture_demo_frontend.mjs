/** Acquisisce nove schermate reali della WebUI tramite Chrome DevTools Protocol.
 *
 * Non richiede pacchetti npm: usa soltanto Node.js, Chrome installato e le due
 * postazioni locali avviate da `demo_frontend.py serve`.
 */

import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import net from "node:net";
import path from "node:path";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function freePort() {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function waitJson(url, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch (error) {
      lastError = error;
    }
    await sleep(150);
  }
  throw new Error(`Chrome non ha aperto la porta di debug: ${lastError || url}`);
}

class CDP {
  constructor(url) {
    this.ws = new WebSocket(url);
    this.nextId = 0;
    this.pending = new Map();
    this.ready = new Promise((resolve, reject) => {
      this.ws.addEventListener("open", resolve, { once: true });
      this.ws.addEventListener("error", reject, { once: true });
    });
    this.ws.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (!message.id) return;
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(JSON.stringify(message.error)));
      else waiter.resolve(message.result || {});
    });
  }

  async send(method, params = {}) {
    await this.ready;
    const id = ++this.nextId;
    return await new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    this.ws.close();
  }
}

function js(value) {
  return JSON.stringify(value);
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || expression);
  }
  return result.result?.value;
}

async function waitFor(cdp, expression, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(cdp, `Boolean(${expression})`)) return;
    await sleep(150);
  }
  throw new Error(`Timeout in attesa di: ${expression}`);
}

async function post(station, operation, data = {}) {
  const response = await fetch(`${station.base}/api/${operation}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-RFID-Token": station.token,
    },
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(`${operation}: ${body.errore || response.status}`);
  return body;
}

async function navigate(cdp, url) {
  await cdp.send("Page.navigate", { url });
  await waitFor(cdp, "document.readyState === 'complete'");
  await waitFor(cdp, "document.querySelector('#materiale')?.options.length > 0");
  await evaluate(cdp, "document.documentElement.dataset.tema='chiaro'; document.body.style.zoom='0.82'; true");
  await sleep(250);
}

async function click(cdp, selector) {
  const ok = await evaluate(
    cdp,
    `(() => { const node = document.querySelector(${js(selector)}); if (!node) return false; node.click(); return true; })()`,
  );
  if (!ok) throw new Error(`Elemento non trovato: ${selector}`);
}

async function fill(cdp, selector, value) {
  const ok = await evaluate(
    cdp,
    `(() => {
      const node = document.querySelector(${js(selector)});
      if (!node) return false;
      node.value = ${js(String(value))};
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    })()`,
  );
  if (!ok) throw new Error(`Campo non trovato: ${selector}`);
}

async function capture(cdp, rawDir, filename, focus = null) {
  if (focus) {
    await evaluate(
      cdp,
      `document.querySelector(${js(focus)})?.scrollIntoView({block:'center'}); true`,
    );
  } else {
    await evaluate(cdp, "scrollTo(0, 0); true");
  }
  await sleep(300);
  const result = await cdp.send("Page.captureScreenshot", {
    format: "png",
    captureBeyondViewport: false,
    fromSurface: true,
  });
  await fs.writeFile(path.join(rawDir, filename), Buffer.from(result.data, "base64"));
  process.stdout.write(`acquisito ${filename}\n`);
}

async function main() {
  const sessionPath = path.resolve(process.argv[2] || "demo-output/rixlab_frontend_demo/demo_session.json");
  const session = JSON.parse(await fs.readFile(sessionPath, "utf8"));
  const output = path.dirname(sessionPath);
  const rawDir = path.join(output, "raw");
  await fs.mkdir(rawDir, { recursive: true });

  const chrome = "C:/Program Files/Google/Chrome/Application/chrome.exe";
  const port = await freePort();
  const profile = path.join(output, "_runtime", `chrome-profile-${Date.now()}`);
  const browserProcess = spawn(
    chrome,
    [
      "--headless=new",
      `--remote-debugging-port=${port}`,
      `--user-data-dir=${profile}`,
      "--window-size=1800,900",
      "--force-device-scale-factor=1",
      "--hide-scrollbars",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "about:blank",
    ],
    { stdio: "ignore" },
  );

  let cdp;
  try {
    await waitJson(`http://127.0.0.1:${port}/json/version`);
    const target = await fetch(
      `http://127.0.0.1:${port}/json/new?${encodeURIComponent("about:blank")}`,
      { method: "PUT" },
    ).then((response) => response.json());
    cdp = new CDP(target.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 1800,
      height: 900,
      deviceScaleFactor: 1,
      mobile: false,
    });

    // 1. Mittente: compilazione anagrafica.
    await navigate(cdp, session.mittente.url);
    await click(cdp, "#collega");
    await waitFor(cdp, "document.querySelector('#stato-lettore-testo')?.textContent === 'pronto'");
    for (const [selector, value] of [
      ["#cf", session.scenario.codice_fiscale],
      ["#cognome", "PAZIENTE"],
      ["#nome", "DEMO"],
      ["#sesso", "M"],
      ["#reparto", "Chirurgia Demo"],
      ["#medico", "Dott. Demo"],
      ["#descrizione", "Biopsia cutanea - materiale dimostrativo"],
      ["#materiale", "1"],
      ["#fissativo", "1"],
      ["#sede", "1"],
      ["#contenitori", "2"],
    ]) await fill(cdp, selector, value);
    await evaluate(cdp, "document.querySelector('.avvertenze input[value=URGENT]').checked=true; true");
    await capture(cdp, rawDir, "01_anagrafica_paziente.png", "#modulo-accettazione");

    // 2. Registrazione e piano dei due contenitori.
    await evaluate(cdp, "document.querySelector('#modulo-accettazione').requestSubmit(); true");
    await waitFor(cdp, "!document.querySelector('#pannello-postazione').classList.contains('pannello--nascosto')");
    await capture(cdp, rawDir, "02_accettazione_due_contenitori.png", "#pannello-postazione");

    // 3. Scrittura reale sul simulatore, un tag per volta.
    await post(session.mittente, "demo_campo", { tag: [1] });
    await waitFor(cdp, "document.querySelector('#scrivi') && !document.querySelector('#scrivi').disabled", 5000);
    await click(cdp, "#scrivi");
    await waitFor(cdp, "document.querySelector('#dialogo')?.open");
    await click(cdp, "#dialogo-ok");
    await waitFor(cdp, "document.querySelectorAll('#serie-elenco [data-stato=scritto]').length === 1");
    await post(session.mittente, "demo_campo", { tag: [] });
    await waitFor(cdp, "document.querySelector('#scrivi')?.disabled", 5000);
    await post(session.mittente, "demo_campo", { tag: [2] });
    await waitFor(cdp, "document.querySelector('#scrivi') && !document.querySelector('#scrivi').disabled", 5000);
    await click(cdp, "#scrivi");
    await waitFor(cdp, "document.querySelectorAll('#serie-elenco [data-stato=scritto]').length === 2");
    await capture(cdp, rawDir, "03_tag_scritti.png", "#pannello-postazione");

    // 4. Destinatario configurato.
    await click(cdp, "[data-schermata=impostazioni]");
    await waitFor(cdp, "document.querySelectorAll('#tabella-destinatari tbody tr').length === 1");
    await capture(cdp, rawDir, "04_laboratorio_destinatario.png", "#tabella-destinatari");

    // 5. Preparazione della spedizione.
    const queue = await post(session.mittente, "coda_spedizione");
    const containerIds = queue.gruppi.flatMap((group) =>
      group.contenitori.map((container) => container.container_id),
    );
    await post(session.mittente, "prepara_spedizione", {
      destinazione: session.scenario.destinatario,
      container_ids: containerIds,
    });
    await navigate(cdp, session.mittente.url);
    await click(cdp, "#collega");
    await waitFor(cdp, "document.querySelector('#stato-lettore-testo')?.textContent === 'pronto'");
    await click(cdp, "[data-schermata=sigillo]");
    await waitFor(cdp, "!document.querySelector('#pannello-sigillo').classList.contains('pannello--nascosto')");
    await capture(cdp, rawDir, "05_spedizione_preparata.png", "#pannello-sigillo");

    // 6. Sigillo: il campo contiene esattamente i due campioni attesi.
    await post(session.mittente, "demo_campo", { tag: [1, 2] });
    await click(cdp, "#sigilla");
    await waitFor(cdp, "document.querySelector('#dialogo')?.open");
    await click(cdp, "#dialogo-ok");
    await waitFor(cdp, "document.querySelector('#verdetto-conteggio')?.textContent.trim() === '2 / 2'", 30000);
    await capture(cdp, rawDir, "06_sigillo_conforme.png", "#pannello-sigillo");

    // 7. PEC simulata, ricevute tecniche e partenza.
    await click(cdp, "#invia-pec");
    await waitFor(cdp, "document.querySelector('#stato-pec')?.textContent.includes('affidata al gestore PEC')");
    await click(cdp, "#aggiorna-pec");
    await waitFor(cdp, "document.querySelector('#stato-pec')?.textContent.includes('consegna PEC certificata')");
    await capture(cdp, rawDir, "07_distinta_pec_consegnata.png", "#stato-pec");
    await click(cdp, "#conferma-invio");
    await waitFor(cdp, "document.querySelector('#dialogo')?.open");
    await click(cdp, "#dialogo-ok");
    await waitFor(cdp, "document.querySelector('#stato-spedizione')?.textContent.includes('partenza confermata')");

    // Salva gli stessi byte archiviati e li importa nella postazione ricevente.
    const manifestResponse = await fetch(`${session.mittente.base}/api/distinta`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-RFID-Token": session.mittente.token },
      body: "{}",
    });
    if (!manifestResponse.ok) throw new Error(`download distinta: ${manifestResponse.status}`);
    const manifest = Buffer.from(await manifestResponse.arrayBuffer());
    await fs.writeFile(session.distinta, manifest);
    await post(session.destinatario, "importa_distinta", {
      contenuto_base64: manifest.toString("base64"),
    });

    // 8. Destinatario: hash e firma verificati.
    await navigate(cdp, session.destinatario.url);
    await click(cdp, "#collega");
    await waitFor(cdp, "document.querySelector('#stato-lettore-testo')?.textContent === 'pronto'");
    await click(cdp, "[data-schermata=ricezione]");
    await waitFor(cdp, "document.querySelector('#distinta') && !document.querySelector('#distinta').hidden");
    await capture(cdp, rawDir, "08_distinta_verificata.png", "#distinta");

    // 9. Lettura 2/2 e registrazione nella catena di custodia.
    await post(session.destinatario, "demo_campo", { tag: [1, 2] });
    await click(cdp, "#leggi-volume");
    await waitFor(cdp, "document.querySelector('#arrivo-conteggio')?.textContent.trim() === '2 / 2'", 30000);
    await click(cdp, "#conferma-ricezione");
    await waitFor(cdp, "document.querySelector('#conferma-ricezione')?.disabled");
    await capture(cdp, rawDir, "09_ricezione_completata.png", "#pannello-ricezione");
  } finally {
    if (cdp) cdp.close();
    browserProcess.kill();
  }
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
