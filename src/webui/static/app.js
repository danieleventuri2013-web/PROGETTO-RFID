/* ==========================================================================
   Stato dell'interfaccia, chiamate al server, tastiera.

   Regole che valgono in tutto il file:
   * niente stato inventato — se il server non ha ancora risposto, non si mostra
     un risultato «probabile»;
   * un solo posto costruisce le richieste (`chiama`), cosi' il token e il
     trattamento degli errori non si sdoppiano;
   * gli eventi del server muovono la scena; l'interfaccia non anticipa mai.
   ========================================================================== */

const TOKEN = new URLSearchParams(location.search).get("t") || "";

const stato = {
  descrizione: null,
  accettazione: null,
  spedizione: null,
  distinta: null,
  coda: null,
  ultimaRicezione: null,
  collegato: false,
  occupato: false,
  sorveglianza: null,
  intervalloSorveglianza: 900,
  /** EPC del contenitore appena scritto: finche' e' sul piatto non si avanza. */
  ultimoEpc: "",
  ultimoContenitore: null,
  etichettaCorrente: null,
  impostazioni: null,
  campagnaTag: [],
  /** Contenitori scritti e non ancora in una spedizione. */
  residuo: 0,
  /** Riempimento in corso: l'ultimo riepilogo ricevuto dal server. */
  riempimento: null,
  /** Le parti del QR lette finora, in attesa che siano tutte. */
  scansioni: [],
  distintaQr: null,
  transito: null,
  /** Le curve di adattamento accumulate nel grafico. */
  curve: [],
  /** L'ultima prova di lettura, per il confronto prima/dopo. */
  prova: null,
  provaContinua: null,
  proposta: null,
  /** Cosa e' collegato: rilevato al collegamento, guida cosa si puo' offrire. */
  hardware: null,
  /** L'archivio: quanti se ne vedono, quanti ce ne sono, con che ordine. */
  archivio: { mostrati: 0, totale: 0, ordine: "recenti", attesa: null },
  /** Timer della sorveglianza della scatola. */
  vigilanza: null,
  /** Il giro della scatola in volo: chi deve usare il lettore lo aspetta. */
  giroScatola: null,
  presenza: null,
  presenzaRichiesta: null,
  presenzaUltimoGiro: 0,
  presenzaPausa: false,
  presenzaErrore: "",
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/** «Manca 1 contenitore» invece di «Mancano 1 contenitori»: un messaggio che
 *  sgrammatica fa dubitare anche del numero che sta accanto. */
const plurale = (n, uno, molti) => (n === 1 ? uno : molti);

function aggiornaWorkflowBar() {
  const parti = [];
  // Il residuo viene per primo ed e' sempre presente, anche a zero: e' il
  // numero che impedisce di finire la giornata con un campione scritto e mai
  // partito, e serve proprio quando nessun altro flusso e' aperto.
  const residuo = stato.residuo ?? 0;
  parti.push(
    residuo === 0
      ? "Nulla in attesa di spedizione"
      : `${residuo} ${plurale(residuo, "campione scritto", "campioni scritti")} da spedire`
  );
  if (stato.accettazione?.accession_id) {
    parti.push(
      "Accettazione " + stato.accettazione.accession_id + ": " +
      stato.accettazione.scritti + "/" + stato.accettazione.totale + " tag"
    );
  }
  if (stato.spedizione?.shipment_id) {
    parti.push(
      "Spedizione " + stato.spedizione.shipment_id + ": " +
      (stato.spedizione.stato || "bozza")
    );
  }
  if (stato.distinta?.inbound_id) {
    parti.push(
      "Ricezione " + stato.distinta.inbound_id + ": " +
      (stato.distinta.stato || "aperta")
    );
  }
  $("#workflow-stato").textContent = parti.join(" · ");
  $("#workflow-bar").dataset.residuo = residuo > 0 ? "1" : "";
}

/** Tiene aggiornato il residuo con quello che la risposta porta con se'.
 *  Cosi' il numero cambia subito dopo una scrittura o una chiusura di scatola,
 *  senza una seconda chiamata solo per chiederlo. */
function annotaResiduo(risposta) {
  if (risposta && typeof risposta.residuo_da_spedire === "number") {
    stato.residuo = risposta.residuo_da_spedire;
  }
  return risposta;
}

/* ==========================================================================
   Rete
   ========================================================================== */
async function chiama(operazione, dati = {}) {
  if (["scrivi", "conferma_conteggio", "correggi_conteggio", "annulla_accettazione", "annulla_contenitore", "sospendi_accettazione"].includes(operazione) && stato.accettazione?.accession_id) {
    dati = {accession_id: stato.accettazione.accession_id, ...dati};
  }
  const risposta = await fetch(`/api/${operazione}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
    body: JSON.stringify(dati),
  });
  const corpo = await risposta.json().catch(() => ({}));
  if (!risposta.ok) {
    // Un token rifiutato non è un errore fra tanti: senza, nessuna operazione
    // funzionerà mai. Si dice una volta e si smette di provare.
    if (risposta.status === 401) sbarra();
    const errore = new Error(corpo.errore || `errore ${risposta.status}`);
    errore.stato = risposta.status;
    errore.occupato = risposta.status === 409;
    throw errore;
  }
  return corpo;
}

function sbarra() {
  fermaSorveglianza();
  fermaVigilanza();
  fermaProvaContinua();
  $("#sbarramento").hidden = false;
}

/* ==========================================================================
   Diario di prototipazione (lato pagina)

   Finche' il flusso di lavoro cambia ancora, serve poter rileggere una
   sessione intera: cosa e' stato premuto, cosa e' stato digitato, in che
   ordine, e cosa rispondeva la radio nel frattempo. Il server registra gia'
   le proprie chiamate; qui si aggiunge quello che il server non puo' vedere.

   Tre regole:
   * si accoda e si spedisce a lotti — un giro di rete per clic renderebbe
     l'interfaccia piu' lenta proprio al banco;
   * i campi password non si guardano mai, nemmeno per sapere che sono stati
     compilati;
   * se il diario non funziona, non se ne accorge nessuno: la registrazione
     non deve mai fermare il lavoro.
   ========================================================================== */
const Traccia = {
  coda: [],
  timer: null,
  attiva: true,
  /** Oltre questo numero si spedisce subito invece di aspettare il timer. */
  soglia: 40,

  nota(nome, dati = {}) {
    if (!this.attiva || !TOKEN) return;
    this.coda.push({
      nome,
      quando: new Date().toISOString(),
      schermata: schermataAttiva(),
      ...dati,
    });
    if (this.coda.length >= this.soglia) this.invia();
    else if (!this.timer) this.timer = setTimeout(() => this.invia(), 2000);
  },

  invia(conBeacon = false) {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (!this.coda.length) return;
    const lotto = this.coda.splice(0, 200);
    const corpo = JSON.stringify({ eventi: lotto });
    // Alla chiusura della pagina `fetch` viene interrotta: `sendBeacon` e'
    // l'unico modo di non perdere l'ultimo pezzo di sessione, ed e' anche
    // l'unico che non puo' portare intestazioni — per questo il token va
    // nell'indirizzo, come gia' fa il flusso eventi.
    if (conBeacon && navigator.sendBeacon) {
      navigator.sendBeacon(
        `/api/traccia?t=${encodeURIComponent(TOKEN)}`,
        new Blob([corpo], { type: "application/json" })
      );
      return;
    }
    fetch("/api/traccia", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
      body: corpo,
      keepalive: true,
    }).catch(() => {
      // Il diario non e' il lavoro: se il server non risponde si smette di
      // insistere invece di riempire la console di errori.
      this.attiva = false;
    });
  },

  /** Valore di un campo, o perche' non lo si registra. */
  valore(campo) {
    if (campo.type === "password") return "(non registrata)";
    if (campo.type === "checkbox" || campo.type === "radio") return campo.checked;
    const testo = String(campo.value ?? "");
    return testo.length > 200 ? `${testo.slice(0, 200)}…` : testo;
  },

  collega() {
    if (!TOKEN) return;

    // Un solo ascoltatore delegato per tipo: gli elementi dell'interfaccia
    // nascono e muoiono di continuo (righe della coda, schede dei campioni) e
    // agganciarne uno per nodo li perderebbe tutti.
    document.addEventListener(
      "click",
      (evento) => {
        const comando = evento.target.closest("button, a, .rail__voce, label.bottone");
        if (!comando) return;
        this.nota("clic", {
          id: comando.id || "",
          testo: (comando.textContent || "").trim().slice(0, 60),
          disabilitato: Boolean(comando.disabled),
        });
      },
      true
    );

    document.addEventListener("change", (evento) => {
      const campo = evento.target;
      if (!campo.matches?.("input, select, textarea")) return;
      this.nota("campo", {
        id: campo.id || campo.name || "",
        valore: this.valore(campo),
      });
    });

    document.addEventListener("submit", (evento) => {
      this.nota("modulo", { id: evento.target.id || "" });
    });

    // La chiusura della scheda e il passaggio in secondo piano sono i due
    // momenti in cui si perderebbe la coda.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.invia(true);
    });
    window.addEventListener("pagehide", () => this.invia(true));
  },
};

/** Scarica un file prodotto dal server (la distinta cifrata). */
async function scarica(percorso, nomeSuggerito) {
  const risposta = await fetch(percorso, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
    body: "{}",
  });
  if (!risposta.ok) {
    const corpo = await risposta.json().catch(() => ({}));
    throw new Error(corpo.errore || `errore ${risposta.status}`);
  }
  const intestazione = risposta.headers.get("Content-Disposition") || "";
  const trovato = /filename="([^"]+)"/.exec(intestazione);
  const blob = await risposta.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = trovato ? trovato[1] : nomeSuggerito;
  link.click();
  URL.revokeObjectURL(url);
}

/* ==========================================================================
   Avvisi
   ========================================================================== */
function avvisa(testo, tipo = "ok", durata = 5000) {
  const nodo = document.createElement("div");
  nodo.className = "avviso";
  nodo.dataset.tipo = tipo;
  nodo.textContent = testo;
  $("#avvisi").append(nodo);
  setTimeout(() => nodo.remove(), durata);
  Traccia.nota("avviso", { tipo, testo });
}

/* ==========================================================================
   Tema
   ========================================================================== */
function applicaTema(scelta) {
  document.documentElement.dataset.tema = scelta;
  localStorage.setItem("tema", scelta);
}

function alternaTema() {
  const attuale = document.documentElement.dataset.tema;
  const scuroDiSistema = matchMedia("(prefers-color-scheme: dark)").matches;
  const stiamoAlScuro = attuale === "scuro" || (attuale === "sistema" && scuroDiSistema);
  applicaTema(stiamoAlScuro ? "chiaro" : "scuro");
}

/* ==========================================================================
   Postazione — banco (mouse) o tavoletta (dita)

   Una sola interfaccia, due misure. La scelta sta in un attributo sulla
   radice, e `tocco.css` non applica una riga finche' non vale "tavoletta":
   il banco non puo' cambiare per colpa di questo file.

   L'ordine con cui si decide non e' arbitrario:
   1. `?modo=` nell'indirizzo, perche' e' quello che finisce nel collegamento
      salvato sulla schermata Home della tavoletta — l'apparecchio si porta
      dietro la sua modalita' e non dipende da cosa ha scelto qualcun altro;
   2. la scelta salvata su questo apparecchio;
   3. il tipo di puntatore. `pointer: coarse` vuol dire «il puntatore piu'
      preciso di questo apparecchio e' un dito».
   ========================================================================== */
const MODI = ["banco", "tavoletta"];

function postazioneRichiesta() {
  const daIndirizzo = new URLSearchParams(location.search).get("modo");
  if (MODI.includes(daIndirizzo)) return daIndirizzo;
  const salvata = localStorage.getItem("postazione");
  if (MODI.includes(salvata)) return salvata;
  return matchMedia("(pointer: coarse)").matches ? "tavoletta" : "banco";
}

function applicaPostazione(scelta, { ricorda = true } = {}) {
  const modo = MODI.includes(scelta) ? scelta : "banco";
  document.documentElement.dataset.postazione = modo;
  if (ricorda) localStorage.setItem("postazione", modo);
  const radio = $$('input[name="postazione"]').find((r) => r.value === modo);
  if (radio) radio.checked = true;
  return modo;
}

function tavoletta() {
  return document.documentElement.dataset.postazione === "tavoletta";
}

/* Il manifesto dice ad Android come installare la pagina sulla schermata Home.
   Non e' un file statico perche' contiene il token: servirlo a chiunque passi
   sulla rete regalerebbe il comando del lettore. Il collegamento si costruisce
   qui, con il token che questa pagina ha gia'. */
function collegaManifesto() {
  if (!TOKEN) return;
  const link = document.createElement("link");
  link.rel = "manifest";
  link.href = `/manifest.webmanifest?t=${encodeURIComponent(TOKEN)}`;
  document.head.append(link);
}

/* ==========================================================================
   Navigazione
   ========================================================================== */
function schermataAttiva() {
  return $(".schermata--attiva")?.id.replace("schermata-", "") || "";
}

function mostra(nome, scheda) {
  const ruolo = stato.descrizione?.operativita?.station_mode || "entrambe";
  if ((ruolo === "ricezione" && ["accettazione", "sigillo"].includes(nome)) ||
      (ruolo === "spedizione" && nome === "ricezione")) nome = "impostazioni";
  if (nome !== "ricezione") window.fermaWebcamRicezione?.();
  if (nome !== "sigillo") window.fermaWebcamSigillo?.();
  if (nome !== "impostazioni") window.fermaConfigurazioneVisiva?.();
  Traccia.nota("schermata", { da: schermataAttiva(), a: nome, scheda: scheda || "" });
  $$(".schermata").forEach((sezione) => {
    sezione.classList.toggle("schermata--attiva", sezione.id === `schermata-${nome}`);
  });
  $$(".rail__voce").forEach((voce) => {
    if (voce.dataset.schermata === nome) voce.setAttribute("aria-current", "page");
    else voce.removeAttribute("aria-current");
  });
  // Le due sorveglianze costano una lettura radio ciascuna: si tengono accese
  // solo dove servono, e mai insieme — userebbero lo stesso lettore.
  if (nome === "accettazione") avviaSorveglianza();
  else fermaSorveglianza();
  if (nome === "sigillo" && stato.riempimento) avviaVigilanza();
  else fermaVigilanza();
  if(nome==="sigillo")window.caricaImpostazioniVisive?.();
  // Le impostazioni si rileggono ogni volta: possono essere cambiate altrove,
  // e mostrare valori vecchi qui significherebbe farli riscrivere per sbaglio.
  if (nome === "impostazioni") {
    caricaImpostazioni();
    window.caricaImpostazioniVisive?.();
    if (scheda) mostraScheda(scheda);
  }
  // L'archivio si riempie da solo: e' un elenco da sfogliare, non un modulo da
  // compilare prima di vedere qualcosa. Si ricarica ogni volta perche' nel
  // frattempo si sono accettati altri pazienti.
  if (nome === "archivio") elencaPazienti({ azzera: true });
  if (nome === "accettazione" && typeof Giornata !== "undefined" && Giornata.avviata) Giornata.aggiorna();
}

/** Le due metà delle impostazioni: la configurazione si fa una volta e si
 *  dimentica, le misure si fanno col lettore in mano. Tenerle su una schermata
 *  sola significherebbe undici riquadri in fila e quello che serve in fondo. */
function mostraScheda(nome) {
  if(nome!=="controllo-visivo")window.fermaConfigurazioneVisiva?.();
  $$(".scheda").forEach((riquadro) => {
    riquadro.classList.toggle("scheda--attiva", riquadro.id === `scheda-${nome}`);
  });
  $$(".schede__voce").forEach((voce) => {
    voce.setAttribute("aria-selected", voce.dataset.scheda === nome ? "true" : "false");
  });
  Traccia.nota("scheda", { nome });
  // La prova di lettura dal vivo costa una lettura radio ogni giro: si spegne
  // appena si guarda altro.
  if (nome !== "misure") fermaProvaContinua();
}

function schedaAttiva() {
  return $(".scheda--attiva")?.id.replace("scheda-", "") || "configurazione";
}

/* ==========================================================================
   Lettore
   ========================================================================== */
function dipingiStatoLettore(testo, tipo) {
  const pastiglia = $("#stato-lettore");
  pastiglia.dataset.stato = tipo;
  $("#stato-lettore-testo").textContent = testo;
}

async function collega() {
  dipingiStatoLettore("collegamento…", "attivo");
  try {
    const esito = await chiama("connetti");
    // Il censimento arriva insieme al collegamento: da questo momento
    // l'interfaccia sa cosa questo modulo puo' fare e si adegua.
    if (esito.hardware) {
      stato.hardware = esito.hardware;
      if (stato.descrizione) stato.descrizione.hardware = esito.hardware;
      dipingiHardware(esito.hardware);
      adattaAllHardware(esito.hardware);
    }
    stato.collegato = true;
    stato.radioConfigurata = Boolean(esito.configurazione_ok);
    dipingiStatoLettore(stato.radioConfigurata ? "pronto" : "da configurare", stato.radioConfigurata ? "pronto" : "attesa");
    $("#collega").textContent = "Scollega";
    if (!esito.configurazione_ok && esito.configurazione_errore) {
      avvisa(`Lettore avviato ma non configurato: ${esito.configurazione_errore}`, "attesa", 9000);
    }
    avviaSorveglianza();
  } catch (errore) {
    stato.collegato = false;
    dipingiStatoLettore("non collegato", "errore");
    avvisa(errore.message, "errore", 9000);
  }
}

async function scollega() {
  fermaSorveglianza();
  try {
    await chiama("disconnetti");
  } catch (errore) {
    avvisa(errore.message, "errore");
  }
  stato.collegato = false;
  dipingiStatoLettore("non collegato", "fermo");
  $("#collega").textContent = "Collega";
  Scena.stato("fermo");
}

/* ==========================================================================
   Accettazione
   ========================================================================== */
function riempiCodebook() {
  const mappa = {
    "#materiale": stato.descrizione.codebook.materiali,
    "#fissativo": stato.descrizione.codebook.fissativi,
    "#sede": stato.descrizione.codebook.sedi,
  };
  for (const [sel, voci] of Object.entries(mappa)) {
    const campo = $(sel);
    campo.innerHTML = "";
    for (const voce of voci) {
      const opzione = document.createElement("option");
      opzione.value = voce.codice;
      opzione.textContent = voce.nome;
      campo.append(opzione);
    }
  }
}

/* -- I reperti -------------------------------------------------------------
   Quattro campioni presi allo stesso paziente nella stessa seduta hanno lo
   stesso nome sopra, ma descrizione, materiale, fissativo, sede e avvertenze
   possono essere tutti diversi. Il primo reperto è il modulo che si vede
   aprendo la schermata; gli altri si aggiungono solo quando servono, così il
   caso normale — un campione, uno o più vasetti uguali — non paga niente.
   -------------------------------------------------------------------------- */
function primoReperto() {
  return {
    descrizione: $("#descrizione").value.trim(),
    material_code: Number($("#materiale").value || 0),
    fixative_code: Number($("#fissativo").value || 0),
    site_code: Number($("#sede").value || 0),
    avvertenze: $$("#modulo-accettazione > .avvertenze input:checked").map((c) => c.value),
  };
}

function repertiAggiuntivi() {
  return $$("#reperti-extra .reperto").map((riquadro) => ({
    descrizione: riquadro.querySelector('[data-campo="descrizione"]').value.trim(),
    material_code: Number(riquadro.querySelector('[data-campo="material_code"]').value || 0),
    fixative_code: Number(riquadro.querySelector('[data-campo="fixative_code"]').value || 0),
    site_code: Number(riquadro.querySelector('[data-campo="site_code"]').value || 0),
    avvertenze: Array.from(
      riquadro.querySelectorAll("[data-avvertenza]:checked")
    ).map((c) => c.value),
  }));
}

function aggiungiReperto() {
  const modello = $("#modello-reperto").content.cloneNode(true);
  const riquadro = modello.querySelector(".reperto");
  // I codebook si riempiono adesso: sono gli stessi del primo reperto e
  // arrivano dal server, non si duplicano nel markup.
  for (const [selettore, codici] of [
    ['[data-campo="material_code"]', stato.descrizione?.codebook?.materiali],
    ['[data-campo="fixative_code"]', stato.descrizione?.codebook?.fissativi],
    ['[data-campo="site_code"]', stato.descrizione?.codebook?.sedi],
  ]) {
    const scelta = riquadro.querySelector(selettore);
    for (const voce of codici || []) {
      const opzione = document.createElement("option");
      opzione.value = voce.codice;
      opzione.textContent = voce.nome;
      scelta.append(opzione);
    }
  }
  riquadro.querySelector(".reperto__togli").addEventListener("click", () => {
    riquadro.remove();
    numeraReperti();
  });
  $("#reperti-extra").append(riquadro);
  numeraReperti();
  riquadro.querySelector('[data-campo="descrizione"]').focus();
}

/** «Campione 2», «Campione 3»: con quattro vasetti dello stesso paziente serve
 *  sapere quale si sta compilando. */
function numeraReperti() {
  const extra = $$("#reperti-extra .reperto");
  extra.forEach((riquadro, indice) => {
    riquadro.querySelector(".reperto__titolo").textContent = `Campione ${indice + 2}`;
  });
  // Finché il campione è uno solo l'etichetta resta quella di sempre: numerare
  // un elenco di uno è rumore.
  $("#etichetta-descrizione").textContent = extra.length
    ? "Campione 1 — descrizione del reperto"
    : "Descrizione del reperto";
}

async function registra(evento) {
  evento.preventDefault();
  if (typeof Giornata !== "undefined" && Giornata.avviata) return Giornata.salva();
  $("#errore-accettazione").textContent = "";
  const reperti = [primoReperto(), ...repertiAggiuntivi()];
  const dati = {
    codice_fiscale: $("#cf").value.trim().toUpperCase(),
    cognome: $("#cognome").value.trim(),
    nome: $("#nome").value.trim(),
    sesso: $("#sesso").value,
    data_nascita: $("#data-nascita").value,
    data_prelievo: $("#data-prelievo").value,
    ora_prelievo: $("#ora-prelievo").value,
    reparto: $("#reparto").value.trim(),
    medico: $("#medico").value.trim(),
    reperti,
  };
  try {
    stato.accettazione = annotaResiduo(await chiama("registra", dati));
    $("#pannello-anagrafica").classList.add("pannello--nascosto");
    $("#pannello-postazione").classList.remove("pannello--nascosto");
    dipingiAccettazione();
    dipingiNotaPaziente(stato.accettazione.paziente_gia_noto);
    aggiornaWorkflowBar();
    avviaSorveglianza();
    $("#scrivi").focus();
  } catch (errore) {
    $("#errore-accettazione").textContent = errore.message;
    $("#cf").dataset.invalido = /fiscale/i.test(errore.message) ? "1" : "";
  }
}

/** La data del prelievo parte da oggi: è così nella quasi totalità dei casi,
 *  e chi ha in mano un campione di ieri la corregge. L'ora invece resta vuota
 *  finché non la si legge dall'etichetta: una mezzanotte inventata sarebbe
 *  peggio di un campo in bianco. */
function preimpostaDate() {
  const oggi = new Date();
  $("#data-prelievo").value = new Date(oggi.getTime() - oggi.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 10);
}

/** «Di questo paziente ce ne sono già altri»: informazione, non allarme.
 *  Compare solo quando c'è davvero qualcosa da dire. */
function dipingiNotaPaziente(nota) {
  const riquadro = $("#nota-paziente");
  const quanti = nota?.contenitori || 0;
  riquadro.hidden = quanti === 0;
  if (!quanti) return;
  const accettazioni = (nota.accettazioni || []).join(", ");
  riquadro.textContent =
    `Di questo paziente ${plurale(quanti, "risulta già", "risultano già")} ` +
    `${quanti} ${plurale(quanti, "contenitore registrato", "contenitori registrati")} oggi` +
    (accettazioni ? ` (${plurale(nota.accettazioni.length, "accettazione", "accettazioni")} ${accettazioni})` : "") +
    ". Se è lo stesso campione, controlla prima di scrivere.";
}

/** I pallini dell'avanzamento. Si aggiornano subito dopo ogni scrittura. */
function dipingiSerie() {
  const dati = stato.accettazione;
  if (!dati) return;

  const elenco = $("#serie-elenco");
  elenco.innerHTML = "";
  for (const voce of dati.contenitori) {
    const riga = document.createElement("li");
    riga.className = "serie__voce";
    riga.dataset.stato =
      voce.stato === "scritto" ? "scritto" : voce.stato === "errore" ? "errore" : "";
    const numero = document.createElement("b");
    numero.textContent = voce.etichetta;
    const nota = document.createElement("small");
    nota.className = "hex";
    nota.textContent = voce.epc ? voce.epc.slice(0, 12) + "…" : "da scrivere";
    riga.append(numero, nota);
    if (voce.descrizione) {
      const quale = document.createElement("small");
      quale.className = "serie__reperto";
      quale.textContent = voce.descrizione;
      riga.append(quale);
      riga.title = voce.descrizione;
    }
    elenco.append(riga);
  }

  const prossimo = dati.prossimo;
  if (prossimo) {
    const voce = elenco.children[prossimo.index - 1];
    if (voce && !voce.dataset.stato) voce.dataset.stato = "corso";
  }
}

/** La cartella del campione da scrivere adesso. Si aggiorna solo quando il
 *  contenitore precedente e' stato tolto davvero dal piatto. */
function dipingiCartella() {
  const dati = stato.accettazione;
  if (!dati) return;
  const prossimo = dati.prossimo;
  if (prossimo) {
    Scena.contatore(prossimo.index, prossimo.total);
    $("#cartella-etichetta").textContent = `${prossimo.index} / ${prossimo.total}`;
    dipingiCampione(prossimo.campione, prossimo.descrizione);
  } else {
    Scena.contatore(dati.scritti, dati.totale);
    $("#cartella-etichetta").textContent = `${dati.scritti} / ${dati.totale}`;
    $("#scrivi").disabled = true;
    Scena.stato("verificato", "Accettazione completata — tutti i tag sono scritti");
    $("#vai-al-sigillo").focus();
  }
}

function dipingiAccettazione() {
  if (typeof Giornata !== "undefined" && Giornata.avviata) return Giornata.aggiornaPostazione();
  const attiva = Boolean(stato.accettazione?.accession_id);
  $("#pannello-anagrafica").classList.toggle("pannello--nascosto", attiva);
  $("#pannello-postazione").classList.toggle("pannello--nascosto", !attiva);
  if (!attiva) return;
  dipingiSerie();
  dipingiCartella();
  $("#nuova-accettazione").disabled = Boolean(stato.accettazione.prossimo);
}

async function nuovaAccettazione({ automatica = false } = {}) {
  if (typeof Giornata !== "undefined" && Giornata.avviata) return Giornata.torna();
  const conclusa = stato.accettazione?.accession_id;
  try {
    await chiama("nuova_accettazione");
    fermaSorveglianza();
    stato.accettazione = null;
    $("#modulo-accettazione").reset();
    $("#reperti-extra").innerHTML = "";
    numeraReperti();
    preimpostaDate();
    $("#nota-paziente").hidden = true;
    dipingiAccettazione();
    aggiornaWorkflowBar();
    await caricaCoda();
    // Sulla tavoletta il fuoco farebbe salire la tastiera di sistema su un
    // campo che nessuno ha ancora chiesto di compilare.
    if (!tavoletta()) $("#cf").focus();
    if (automatica && conclusa) {
      avvisa(
        `Accettazione ${conclusa} completata. Pronto per il campione successivo.`,
        "ok",
        6000
      );
    }
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

/** Cosa e' finito nel chip: EPC, TID, byte e antenna che ha scritto. */
function dipingiRiepilogo(esito, campione) {
  const contenitore = $("#riepilogo");
  const dati = $("#riepilogo-dati");
  dati.innerHTML = "";
  const righe = [
    ["contenitore", campione?.contenitore || "", false],
    ["epc", esito.epc, true],
    ["chip (tid)", esito.tid, true],
    [
      "payload",
      `${esito.payload_bytes} byte in ${esito.blocks_written} ${
        esito.blocks_written === 1 ? "blocco" : "blocchi"
      }`,
      false,
    ],
    ["antenna", esito.antenna ?? "—", false],
    ["revisione", esito.revision, false],
  ];
  for (const [chiave, valore, mono] of righe) {
    if (valore === "" || valore === null || valore === undefined) continue;
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    if (mono) dd.className = "hex";
    dd.textContent = valore;
    dati.append(dt, dd);
  }
  contenitore.hidden = false;
}

/** Il contenitore e' stato tolto: si passa al prossimo. */
function avanza() {
  stato.ultimoEpc = "";
  $("#riepilogo").hidden = true;
  $("#etichetta").hidden = true;
  // Il contenitore ha lasciato il piatto: l'offerta di riscrivere *quel* tag
  // non ha piu' oggetto, e lasciarla accesa la farebbe cadere sul prossimo.
  $("#riscrivi").hidden = true;
  $("#passi-scrittura").innerHTML = "";
  // Ultimo contenitore andato via dal piatto: l'accettazione è chiusa e la
  // postazione torna da sola al modulo, pronta per il campione successivo. I
  // campioni arrivano uno alla volta e in ordine sparso: chiedere «Nuova
  // accettazione» dopo ognuno sarebbe una cerimonia per ogni provetta.
  // A deciderlo non è un cronometro, è la lettura che ha visto il contenitore
  // lasciare l'antenna.
  if (stato.accettazione && !stato.accettazione.prossimo) {
    nuovaAccettazione({ automatica: true });
    return;
  }
  dipingiCartella();
}

function dipingiCampione(campione, descrizione = "") {
  if (!campione) return;
  $("#cartella-paziente").textContent = campione.paziente || "—";
  $("#cartella-cf").textContent = campione.codice_fiscale || "";
  // Con quattro reperti dello stesso paziente il nome non basta a dire quale
  // vasetto si ha in mano: la descrizione sì.
  const nota = $("#cartella-reperto");
  nota.textContent = descrizione || "";
  nota.hidden = !descrizione;

  const dati = $("#cartella-dati");
  dati.innerHTML = "";
  const righe = [
    ["accettazione", campione.accettazione],
    ["materiale", campione.materiale],
    ["fissativo", campione.fissativo],
    ["sede", campione.sede],
    ["prelievo", campione.data_prelievo ? data(campione.data_prelievo) : "non indicata"],
  ];
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore ?? "—";
    dati.append(dt, dd);
  }

  const avvertenze = $("#cartella-avvertenze");
  avvertenze.innerHTML = "";
  const nomi = {
    INFECTIOUS: "Rischio biologico",
    URGENT: "Urgente",
    FROZEN: "Congelato",
    CYTOLOGY: "Citologia",
  };
  for (const flag of campione.avvertenze || []) {
    const li = document.createElement("li");
    li.textContent = nomi[flag] || flag;
    avvertenze.append(li);
  }
}

/* -- Sorveglianza del piatto ---------------------------------------------
   Nessun pulsante «l'ho appoggiato»: la postazione se ne accorge da sola.
   La lettura serve comunque, perche' la guardia di scrittura pretende un
   inventory con un tag solo. -------------------------------------------- */
function avviaSorveglianza() {
  // Compatibilita' con gli aggiornamenti della postazione: nessun timer RF.
  aggiornaComandoScrittura();
}

function fermaSorveglianza() {
  if (stato.sorveglianza) clearInterval(stato.sorveglianza);
  stato.sorveglianza = null;
}

function aggiornaComandoScrittura() {
  $("#scrivi").disabled = !stato.collegato || !stato.radioConfigurata ||
    (typeof Giornata !== "undefined" && Giornata.avviata && Giornata.vista !== "scrittura") ||
    stato.occupato || Boolean(stato.ultimoEpc) || !stato.accettazione?.prossimo;
  $("#prossimo-contenitore").hidden = !stato.ultimoEpc;
  $("#prossimo-contenitore").textContent = stato.accettazione?.prossimo
    ? "Prossimo contenitore" : "Completa accettazione";
}

/* -- Scrittura ------------------------------------------------------------ */
async function scrivi(deroga = false) {
  if (!stato.accettazione) return;
  if (typeof Giornata !== "undefined" && Giornata.avviata && Giornata.vista !== "scrittura") return;

  // La conferma del numero si chiede alla PRIMA scrittura, non alla
  // registrazione: e' qui che il totale diventa irreversibile, ed e' qui che
  // l'operatore ha i campioni in mano invece della tastiera.
  if (!stato.accettazione.conteggio_confermato) {
    const totale = stato.accettazione.totale;
    const conferma = await domanda(
      "Conferma i contenitori di questa accettazione",
      `Stai per scrivere <strong>${totale}</strong> ${plurale(totale, "contenitore", "contenitori")} per questo paziente.
       Il totale finisce dentro ogni tag come <span class="hex">n/${totale}</span> e dopo la
       prima scrittura non si corregge: si possono solo annullare i tag già scritti.
       <br><br>Sono tutti qui davanti a te?`,
      "Sì, sono " + totale
    );
    if (!conferma) return;
    await chiama("conferma_conteggio");
    stato.accettazione.conteggio_confermato = true;
  }

  // Il contenitore che sta per essere scritto: dopo la scrittura `prossimo`
  // punta gia' a quello dopo, e l'etichetta finirebbe sul campione sbagliato.
  const scritto = stato.accettazione.prossimo?.container_id;

  fermaSorveglianza();
  stato.occupato = true;
  $("#scrivi").disabled = true;
  $("#etichetta").hidden = true;
  $("#errore-scrittura").textContent = "";
  $("#riscrivi").hidden = true;
  $("#passi-scrittura").innerHTML = "";
  Scena.stato("scrittura");
  dipingiStatoLettore("scrittura", "attivo");

  try {
    stato.accettazione = annotaResiduo(
      await chiama("scrivi", {authorized_rewrite: deroga, accession_id: stato.accettazione.accession_id})
    );
    const esito = stato.accettazione.scrittura;
    stato.radioConfigurata = stato.accettazione.radio_configurata !== false;
    if (esito.ok) {
      Scena.stato("verificato");
      chiudiPassi("ok");
      stato.ultimoEpc = esito.epc;
      stato.ultimoContenitore = scritto;
      dipingiRiepilogo(esito, stato.accettazione.campione);
      // L'etichetta si stampa adesso, con il contenitore ancora in mano.
      $("#etichetta").hidden = false;
      // Il riepilogo resta finche' l'operatore non toglie il contenitore: e'
      // la sorveglianza a dire quando e' andato via, non un cronometro.
      Scena.stato("rimuovi");
      avvisa(`Contenitore ${esito.epc ? esito.epc.slice(0, 8) + "…" : ""} scritto e verificato`, "ok");
    } else {
      Scena.stato("errore", esito.error || "Scrittura non riuscita");
      chiudiPassi("errore");
      $("#errore-scrittura").textContent = esito.error || "";
      $("#salta").hidden = false;
      // Il chip non e' rotto: e' gia' scritto. E' l'unico errore a cui
      // l'operatore possa rispondere qualcosa, e in prototipazione capita a
      // ogni giro, perche' lo stesso tag si riusa. Si riconosce dal codice,
      // non dal testo del messaggio.
      $("#riscrivi").hidden = esito.error_code !== "tag_gia_scritto";
    }
    dipingiSerie();
    aggiornaWorkflowBar();
    if (!stato.accettazione.prossimo) caricaCoda();
    avviaSorveglianza();
  } catch (errore) {
    Scena.stato("errore", errore.message);
    $("#errore-scrittura").textContent = errore.message;
    avviaSorveglianza();
  } finally {
    stato.occupato = false;
    aggiornaComandoScrittura();
    dipingiStatoLettore(stato.collegato ? (stato.radioConfigurata ? "pronto" : "da configurare") : "non collegato", stato.collegato && stato.radioConfigurata ? "pronto" : "attesa");
  }
}

/** Riscrive un tag che risulta gia' scritto. Deroga, non scorciatoia. */
async function riscriviTag() {
  const ok = await domanda(
    "Riscrivere questo tag?",
    `Questo chip risulta <strong>già scritto</strong>. Riscriverlo sovrascrive
     i dati che porta adesso: se appartiene a un campione ancora in giro, quel
     campione resta senza identificazione.
     <br><br>La strada normale è annullare il contenitore e usarne uno nuovo.
     La riscrittura esiste per l'assistenza e per il banco di prova, e resta
     <strong>scritta nel registro</strong> con il nome di chi è in servizio.`,
    "Sì, riscrivi"
  );
  if (!ok) return;
  await scrivi(true);
}

function aggiungiPasso(testo) {
  const elenco = $("#passi-scrittura");
  elenco.querySelectorAll('[data-esito="corso"]').forEach((nodo) => {
    nodo.dataset.esito = "fatto";
  });
  const riga = document.createElement("li");
  riga.dataset.esito = "corso";
  riga.textContent = testo;
  elenco.append(riga);
}

function chiudiPassi(esito) {
  $("#passi-scrittura")
    .querySelectorAll('[data-esito="corso"]')
    .forEach((nodo) => {
      nodo.dataset.esito = esito === "ok" ? "fatto" : "errore";
    });
}

/* -- Correzioni ----------------------------------------------------------- */
async function annullaContenitore() {
  const prossimo = stato.accettazione?.prossimo;
  if (!prossimo) return;
  const motivo = await chiediTesto(
    `Annullare il contenitore ${prossimo.index}/${prossimo.total}?`,
    "<p>Ne verrà creato uno sostitutivo con un tag nuovo. Scrivi il motivo:</p>",
    "tag non funzionante",
    "Annulla il contenitore"
  );
  if (motivo === null) return;
  try {
    stato.accettazione = await chiama("annulla_contenitore", {
      container_id: prossimo.container_id,
      motivo,
      tag_guasto: /tag/i.test(motivo),
    });
    $("#salta").hidden = true;
    $("#riscrivi").hidden = true;
    $("#errore-scrittura").textContent = "";
    dipingiAccettazione();
    avvisa("Contenitore annullato: prepara il sostituto con un tag nuovo", "attesa", 8000);
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

/** Abbandona l'accettazione in corso e torna al modulo. */
async function annullaAccettazione() {
  if (!stato.accettazione) return;
  const scritti = stato.accettazione.scritti || 0;
  const totale = stato.accettazione.totale || 0;

  const ok = await domanda(
    "Annullare questa accettazione?",
    scritti
      ? `Di ${totale} contenitori ne sono già stati <strong>scritti ${scritti}</strong>.
         Quei tag non si cancellano — sono scritti una volta sola e i contenitori
         esistono già, con l'etichetta addosso: restano in archivio e si possono
         ancora spedire.
         <br><br>Verranno annullati solo i ${totale - scritti} non ancora scritti,
         e la postazione tornerà al modulo di accettazione.`
      : `Nessun tag è ancora stato scritto: i ${totale} contenitori previsti
         verranno annullati e la postazione tornerà al modulo.
         <br><br>Il numero di accettazione non verrà riutilizzato.`,
    "Sì, annulla"
  );
  if (!ok) return;

  try {
    const esitoAnnullo = await chiama("annulla_accettazione", {});
    stato.accettazione = null;
    stato.ultimoEpc = "";
    fermaSorveglianza();
    $("#pannello-postazione").classList.add("pannello--nascosto");
    $("#pannello-anagrafica").classList.remove("pannello--nascosto");
    $("#riepilogo").hidden = true;
    $("#etichetta").hidden = true;
    $("#salta").hidden = true;
    $("#riscrivi").hidden = true;
    $("#passi-scrittura").innerHTML = "";
    $("#modulo-accettazione").reset();
    $("#cf").focus();
    aggiornaWorkflowBar();
    await caricaCoda();

    const rimasti = esitoAnnullo.gia_scritti.length;
    if (typeof Giornata !== "undefined" && Giornata.avviata) {
      Giornata.cambiaVista("elenco");
      await Giornata.torna();
    }
    avvisa(
      rimasti
        ? `Accettazione ${esitoAnnullo.accettazione} chiusa: ${rimasti} ${plurale(
            rimasti,
            "contenitore già scritto resta",
            "contenitori già scritti restano"
          )} in archivio`
        : `Accettazione ${esitoAnnullo.accettazione} annullata`,
      rimasti ? "attesa" : "ok",
      10000
    );
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

/* ==========================================================================
   Etichette
   ========================================================================== */
async function mostraEtichetta(containerId) {
  let etichetta;
  try {
    etichetta = await chiama("etichetta", { container_id: containerId });
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
    return;
  }
  stato.etichettaCorrente = containerId;
  const contenuto = etichetta.contenuto;

  $("#etichetta-numero").textContent = etichetta.etichetta;
  const anteprima = $("#anteprima-etichetta");
  anteprima.innerHTML = "";

  const paziente = document.createElement("div");
  paziente.className = "anteprima__paziente";
  paziente.textContent = contenuto.paziente;

  const riga = document.createElement("div");
  riga.className = "anteprima__riga";
  const numero = document.createElement("span");
  numero.className = "anteprima__contenitore";
  numero.textContent = contenuto.contenitore;
  riga.append(numero);
  for (const testo of [
    contenuto.codice_fiscale,
    `acc. ${contenuto.accettazione}`,
    contenuto.data_prelievo ? data(contenuto.data_prelievo) : "",
    contenuto.riferimento,
  ]) {
    if (!testo) continue;
    const nodo = document.createElement("span");
    nodo.textContent = testo;
    riga.append(nodo);
  }

  anteprima.append(paziente, riga);

  if (contenuto.avvertenze.length) {
    const banda = document.createElement("div");
    banda.className = "anteprima__avvertenze";
    for (const testo of contenuto.avvertenze) {
      const nodo = document.createElement("span");
      nodo.textContent = testo;
      banda.append(nodo);
    }
    anteprima.append(banda);
  }

  const codice = document.createElement("div");
  codice.className = "anteprima__codice";
  codice.textContent = contenuto.epc;
  anteprima.append(codice);

  $("#zpl").textContent = etichetta.zpl;

  // Senza stampante configurata il bottone dice perche', invece di fallire dopo.
  const stampa = $("#stampa-etichetta");
  stampa.disabled = !etichetta.stampante;
  stampa.textContent = etichetta.stampante
    ? `Stampa su ${etichetta.stampante}`
    : "Nessuna stampante configurata";

  $("#dialogo-etichetta").showModal();
}

async function stampaEtichetta(evento) {
  if (evento.target.returnValue !== "stampa") return;
  try {
    const esito = await chiama("stampa_etichetta", { container_id: stato.etichettaCorrente });
    avvisa(`Etichetta ${esito.stampata} inviata a ${esito.stampante}`, "ok");
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

/* ==========================================================================
   Sigillo e spedizione
   ========================================================================== */
async function caricaCoda() {
  try {
    stato.coda = await chiama("coda_spedizione");
    // La coda e' il residuo visto dall'altro lato: lo stesso numero, gia'
    // calcolato. Aggiornarlo qui evita una chiamata in piu' a ogni giro.
    stato.residuo = stato.coda.totale ?? stato.residuo;
    dipingiCoda();
    aggiornaWorkflowBar();
  } catch (errore) {
    $("#errore-spedizione").textContent = errore.message;
  }
}

function dipingiCoda() {
  const elenco = $("#coda-elenco");
  elenco.innerHTML = "";
  const gruppi = stato.coda?.gruppi || [];
  for (const gruppo of gruppi) {
    const sezione = document.createElement("section");
    sezione.className = "coda__gruppo";
    const titolo = document.createElement("h3");
    titolo.textContent =
      "Accettazione " + gruppo.accession_id + " · " + gruppo.paziente;
    sezione.append(titolo);
    for (const voce of gruppo.contenitori) {
      const riga = document.createElement("label");
      riga.className = "coda__voce";
      const casella = document.createElement("input");
      casella.type = "checkbox";
      casella.name = "contenitore-spedizione";
      casella.value = voce.container_id;
      casella.addEventListener("change", aggiornaRiepilogoCoda);
      const etichetta = document.createElement("b");
      etichetta.textContent = voce.etichetta;
      const epc = document.createElement("span");
      epc.className = "hex tenue";
      epc.title = voce.epc;
      epc.textContent = voce.epc;
      riga.append(casella, etichetta, epc);
      sezione.append(riga);
    }
    elenco.append(sezione);
  }
  aggiornaRiepilogoCoda();
}

function aggiornaRiepilogoCoda() {
  const disponibili = $$('input[name="contenitore-spedizione"]');
  const selezionati = disponibili.filter((voce) => voce.checked).length;
  $("#coda-riepilogo").textContent = disponibili.length
    ? selezionati + " di " + disponibili.length + " selezionati"
    : "Nessun contenitore pronto";
  $("#prepara").disabled = selezionati === 0;
}

function selezionaTuttaCoda() {
  const caselle = $$('input[name="contenitore-spedizione"]');
  const tutte = caselle.length > 0 && caselle.every((voce) => voce.checked);
  caselle.forEach((voce) => { voce.checked = !tutte; });
  aggiornaRiepilogoCoda();
}

async function preparaSpedizione() {
  $("#errore-spedizione").textContent = "";
  try {
    const containerIds = $$('input[name="contenitore-spedizione"]:checked').map(
      (voce) => Number(voce.value)
    );
    stato.spedizione = await chiama("prepara_spedizione", {
      destinazione: $("#destinazione").value.trim(),
      container_ids: containerIds,
    });
    dipingiSpedizione();
    aggiornaWorkflowBar();
    $("#pannello-sigillo").classList.remove("pannello--nascosto");
    disponiPuntiIn("#volume-punti", stato.spedizione.attesi);
    aggiornaPresenzaContenuto();
  } catch (errore) {
    $("#errore-spedizione").textContent = errore.message;
  }
}

function dipingiSpedizione() {
  window.aggiornaVisivoSpedizione?.();
  const spedizione = stato.spedizione;
  const attiva = Boolean(spedizione?.shipment_id);
  const pecAbilitata = Boolean(stato.descrizione?.pec?.abilitata);
  $("#pannello-sigillo").classList.toggle("pannello--nascosto", !attiva);
  $("#coda-spedizione").hidden =
    attiva && !["sent", "cancelled"].includes(spedizione.stato);
  if (!attiva) {
    stato.presenza = null;
    return;
  }

  disponiPuntiIn("#volume-punti", spedizione.attesi);
  const nomi = {
    open: "bozza preparata",
    sealed: "contenuto certificato",
    exported: "distinta esportata",
    sent: "partenza confermata",
    cancelled: "annullata",
  };
  $("#stato-spedizione").textContent = nomi[spedizione.stato] || spedizione.stato;
  $("#sigilla").disabled = true;
  $("#esporta").disabled = !["sealed", "exported"].includes(spedizione.stato);
  $("#prepara-email").disabled = !["exported", "sent"].includes(spedizione.stato);
  $("#conferma-invio").disabled = spedizione.stato !== "exported";
  $("#annulla-spedizione").disabled = ["sent", "cancelled"].includes(spedizione.stato);

  const consegna = spedizione.consegna_pec;
  $("#invia-pec").hidden = !pecAbilitata;
  $("#aggiorna-pec").hidden = !pecAbilitata;
  $("#prepara-email").hidden = pecAbilitata;
  $("#invia-pec").disabled =
    !["sealed", "exported"].includes(spedizione.stato) ||
    ["smtp_accepted", "pec_accepted", "delivered", "delivery_unknown"].includes(
      consegna?.stato
    );
  $("#aggiorna-pec").disabled = !consegna?.message_id || consegna?.stato === "delivered";

  const statoPec = $("#stato-pec");
  statoPec.hidden = !pecAbilitata || !consegna;
  statoPec.innerHTML = "";
  if (pecAbilitata && consegna) {
    const nomiPec = {
      archived: "archiviata localmente",
      failed: "invio rifiutato",
      delivery_unknown: "esito dell'invio incerto",
      smtp_accepted: "affidata al gestore PEC",
      pec_accepted: "ricevuta di accettazione acquisita",
      delivered: "consegna PEC certificata",
      superseded: "sostituita da una nuova spedizione",
    };
    for (const [chiave, valore] of [
      ["PEC", nomiPec[consegna.stato] || consegna.stato],
      ["identificativo", consegna.manifest_uuid],
      ["SHA-256", consegna.sha256],
      ["consegnata", consegna.consegnata ? dataOra(consegna.consegnata) : "—"],
      ["ultimo errore", consegna.errore || "—"],
    ]) {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = chiave;
      dd.textContent = valore;
      statoPec.append(dt, dd);
    }
  }
  $("#deroga-pec").hidden =
    !pecAbilitata || spedizione.stato !== "exported" || consegna?.stato === "delivered";

  if (spedizione.sigillo?.ripristinato) {
    aggiornaVerdetto(
      spedizione.sigillo.ok ? spedizione.attesi : 0,
      spedizione.attesi,
      spedizione.sigillo.ok ? "completo" : "incompleto"
    );
    $("#verdetto-esito").textContent = spedizione.sigillo.dettaglio;
  } else if (spedizione.sigillo?.expected) {
    dipingiSigillo(spedizione);
  } else {
    dipingiPresenzaContenuto();
  }
  $("#presenza-contenuto").hidden = spedizione.stato !== "open";
  $("#verifica-contenuto").hidden = spedizione.stato !== "open";
  aggiornaPresenzaContenuto();
}

function chiaveContenuto() {
  return JSON.stringify([stato.spedizione?.shipment_id,
    (stato.spedizione?.contenitori || []).map((c) => c.epc).sort()]);
}

function presenzaAttiva() {
  return schermataAttiva() === "sigillo" && !document.hidden &&
    stato.collegato && stato.radioConfigurata !== false && !stato.riempimento &&
    stato.spedizione?.stato === "open";
}

function presenzaRecente() {
  return stato.presenza?.chiave === chiaveContenuto() &&
    performance.now() - stato.presenza.ricevuta < 10000;
}

function dipingiPresenzaContenuto() {
  if (stato.spedizione?.stato !== "open" || stato.occupato) return;
  const recente = presenzaRecente() && presenzaAttiva();
  const p = recente ? stato.presenza : null;
  $("#sigilla").disabled = !p?.completo || Boolean(stato.presenzaRichiesta);
  $("#verifica-contenuto").disabled = !presenzaAttiva() || Boolean(stato.presenzaRichiesta);
  aggiornaVerdetto(p?.trovati ?? 0, stato.spedizione.attesi,
    p ? (p.completo ? "completo" : "incompleto") : "attesa");
  if (!p) $("#verdetto-conteggio").textContent = `— / ${stato.spedizione.attesi}`;
  $("#verdetto-esito").textContent = p
    ? (p.completo ? "Tutti i tag attesi sono stati rilevati. Puoi chiudere la scatola e certificare."
      : `${p.attesi - p.trovati} tag attesi non rilevati` +
        (p.estranei.length ? `; ${p.estranei.length} tag fuori elenco nell'area di lettura.` : ". Controlla posizione e contenuto."))
    : (stato.presenzaErrore || (stato.collegato
      ? "Verifica attuale del contenuto in attesa…" : "Collega il lettore per verificare il contenuto."));
  $("#presenza-orario").textContent = p
    ? `Ultima verifica: ${dataOra(p.verificato_il)} · antenne ${p.antenne.join(", ")} · aggiornamento automatico`
    : "Nessuna verifica recente. Le letture durante l'inserimento non valgono come presenza attuale.";
  const elenco = $("#presenza-elenco");
  elenco.replaceChildren();
  for (const voce of p?.contenitori || []) {
    const li = document.createElement("li");
    li.textContent = `${voce.rilevato ? "✓ Rilevato" : "Non rilevato"} — ${voce.etichetta} · ${voce.epc}`;
    elenco.append(li);
  }
  for (const epc of p?.estranei || []) {
    const li = document.createElement("li");
    li.textContent = `Fuori elenco — ${epc}`;
    elenco.append(li);
  }
}

async function verificaContenuto() {
  if (!presenzaAttiva() || stato.occupato || stato.presenzaRichiesta || stato.giroScatola) return;
  const chiave = chiaveContenuto();
  const richiesta = chiama("verifica_contenuto", {shipment_id: stato.spedizione.shipment_id});
  stato.presenzaRichiesta = richiesta;
  stato.presenzaUltimoGiro = performance.now();
  dipingiPresenzaContenuto();
  try {
    const p = await richiesta;
    if (chiave !== chiaveContenuto() || !presenzaAttiva()) return;
    const ricevuta = JSON.stringify([p.shipment_id, p.contenitori.map((c) => c.epc).sort()]);
    if (ricevuta !== chiave) throw new Error("L'elenco della scatola è cambiato: riapri la spedizione.");
    stato.presenza = {...p, chiave, ricevuta: performance.now()};
    stato.presenzaErrore = "";
  } catch (errore) {
    if (chiave !== chiaveContenuto()) return;
    stato.presenza = null;
    stato.presenzaErrore = `Verifica non disponibile: ${errore.message}`;
  } finally {
    stato.presenzaRichiesta = null;
    dipingiPresenzaContenuto();
  }
}

function aggiornaPresenzaContenuto() {
  if (stato.presenzaPausa || stato.occupato) return;
  if (!presenzaAttiva()) {
    stato.presenza = null;
    $("#sigilla").disabled = true;
    return;
  }
  dipingiPresenzaContenuto();
  if (!stato.presenzaRichiesta && performance.now() - stato.presenzaUltimoGiro >= 4000) {
    verificaContenuto();
  }
}

async function annullaSpedizione() {
  const ok = await domanda(
    "Annullare la bozza di spedizione?",
    "I contenitori torneranno nella coda dei pronti e potranno essere selezionati di nuovo.",
    "Annulla la bozza"
  );
  if (!ok) return;
  try {
    await chiama("annulla_spedizione");
    stato.spedizione = null;
    dipingiSpedizione();
    await caricaCoda();
    aggiornaWorkflowBar();
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function confermaInvio() {
  const ok = await domanda(
    "Confermare la partenza?",
    "Usa questa conferma solo quando la scatola è stata consegnata al trasportatore. Da questo momento contenitori e tag risultano spediti.",
    "Conferma la partenza"
  );
  if (!ok) return;
  try {
    stato.spedizione = await chiama("conferma_invio", {
      motivo_deroga: $("#motivo-deroga").value.trim(),
      pin: $("#pin-deroga").value,
    });
    $("#pin-deroga").value = "";
    dipingiSpedizione();
    aggiornaWorkflowBar();
    avvisa("Partenza registrata nel registro di custodia", "ok", 9000);
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function inviaDistintaPec() {
  try {
    stato.spedizione = await chiama("invia_distinta_pec");
    dipingiSpedizione();
    aggiornaWorkflowBar();
    avvisa("Distinta archiviata e affidata al gestore PEC", "ok", 10000);
  } catch (errore) {
    stato.spedizione = await chiama("stato_spedizione").catch(() => stato.spedizione);
    dipingiSpedizione();
    avvisa(errore.message, "errore", 12000);
  }
}

async function aggiornaRicevutePec() {
  try {
    stato.spedizione = await chiama("aggiorna_ricevute_pec");
    dipingiSpedizione();
    avvisa(
      stato.spedizione.consegna_pec?.stato === "delivered"
        ? "Ricevuta di avvenuta consegna acquisita"
        : "Ricevute PEC aggiornate",
      "ok",
      9000
    );
  } catch (errore) {
    avvisa(errore.message, "errore", 10000);
  }
}

/** Accende i punti trovati.
 *
 *  I punti sono anonimi di proposito: non si sa dove stia fisicamente ogni
 *  contenitore dentro la scatola, e disegnarlo in un posto preciso
 *  suggerirebbe una precisione che la radio non dà. Contano quanti sono. */
/* ==========================================================================
   Il segnale acustico

   Durante il riempimento l'operatore guarda le mani e la scatola, non lo
   schermo: la conferma che il campione è entrato deve arrivare all'orecchio.
   Due toni distinti e brevissimi, generati sul momento — nessun file da
   scaricare, che in un laboratorio senza rete non arriverebbe comunque.

   I browser non lasciano suonare niente finché l'utente non ha toccato la
   pagina: il contesto audio si sblocca sul clic di «Apri la scatola», che è un
   gesto vero e non un espediente.
   ========================================================================== */
const Suono = {
  contesto: null,
  attivo: true,

  sblocca() {
    if (this.contesto) return;
    try {
      this.contesto = new (window.AudioContext || window.webkitAudioContext)();
    } catch (errore) {
      // Nessun audio disponibile: la conferma visiva resta, e basta a lavorare.
      this.attivo = false;
    }
  },

  nota(frequenza, durata, ritardo = 0, volume = 0.12) {
    if (!this.attivo || !this.contesto) return;
    const oscillatore = this.contesto.createOscillator();
    const guadagno = this.contesto.createGain();
    const inizio = this.contesto.currentTime + ritardo;
    oscillatore.type = "sine";
    oscillatore.frequency.value = frequenza;
    // Attacco e rilascio dolci: un'onda tagliata di netto fa un clic che a
    // trenta campioni per scatola diventa fastidioso.
    guadagno.gain.setValueAtTime(0, inizio);
    guadagno.gain.linearRampToValueAtTime(volume, inizio + 0.01);
    guadagno.gain.linearRampToValueAtTime(0, inizio + durata);
    oscillatore.connect(guadagno).connect(this.contesto.destination);
    oscillatore.start(inizio);
    oscillatore.stop(inizio + durata + 0.02);
  },

  /** Campione riconosciuto: un colpo breve e alto. */
  ok() {
    this.nota(1320, 0.09);
  },

  /** Anomalia: due colpi bassi, che non si confondono col precedente. */
  errore() {
    this.nota(320, 0.16, 0);
    this.nota(240, 0.22, 0.2);
  },

  /** Campione uscito dalla scatola: un colpo che scende. */
  uscito() {
    this.nota(880, 0.1, 0, 0.08);
    this.nota(660, 0.12, 0.1, 0.08);
  },
};

/* ==========================================================================
   Riempimento della scatola

   Il gesto dell'operatore è mettere il campione dentro. Non c'è nessun pulsante
   che dica «l'ho messo»: la postazione se ne accorge leggendo, e ogni riga di
   questo elenco corrisponde a una lettura vera. Le righe arrivano dagli eventi
   del server, mai da un timer.
   ========================================================================== */
async function apriScatola() {
  const destinazione = $("#destinazione").value;
  $("#errore-spedizione").textContent = "";
  // Il clic che serve ai browser per lasciar suonare qualcosa.
  Suono.sblocca();
  try {
    const esito = annotaResiduo(await chiama("avvia_riempimento", { destinazione }));
    stato.spedizione = esito;
    stato.riempimento = esito.riempimento;
    $("#pannello-riempimento").classList.remove("pannello--nascosto");
    $("#pannello-sigillo").classList.add("pannello--nascosto");
    $("#riempimento-destinazione").textContent = esito.destinazione || destinazione;
    $("#riempimento-scatola").textContent = esito.shipment_id ?? "—";
    dipingiRiempimento(stato.riempimento);
    aggiornaWorkflowBar();
    avviaVigilanza();
  } catch (errore) {
    $("#errore-spedizione").textContent = errore.message;
  }
}

function avviaVigilanza() {
  if (stato.vigilanza || !stato.collegato) return;
  stato.vigilanza = setInterval(guardaScatola, stato.intervalloSorveglianza);
  guardaScatola();
}

function fermaVigilanza() {
  if (!stato.vigilanza) return;
  clearInterval(stato.vigilanza);
  stato.vigilanza = null;
}

async function guardaScatola() {
  // Un giro alla volta. Con la scatola piena una lettura dura piu'
  // dell'intervallo (1,25 s contro 0,9 s al banco, l'11/09/2026): il giro
  // successivo trovava la radio occupata dal precedente, un giro su due
  // tornava 409, e i pulsanti premuti in quel momento venivano rifiutati.
  if (stato.occupato || stato.giroScatola) return;
  const richiesta = chiama("sorveglia_scatola");
  // Chi deve usare il lettore subito dopo aspetta questa promessa, che non
  // fallisce mai: l'errore del giro resta affare di questa funzione.
  stato.giroScatola = richiesta.catch(() => {});
  try {
    const giro = annotaResiduo(await richiesta);
    // Sorveglianza fermata mentre il giro era in volo (scatola chiusa,
    // annullata o ripresa): la risposta tardiva non deve riportare in vita
    // un riempimento che l'operatore ha gia' chiuso.
    if (!stato.vigilanza) return;
    stato.riempimento = giro;
    for (const evento of giro.eventi || []) annunciaTag(evento);
    dipingiRiempimento(giro);
    aggiornaWorkflowBar();
    if (giro.errore) {
      $("#riempimento-stato").textContent = `Lettura non riuscita: ${giro.errore}`;
    }
  } catch (errore) {
    if (errore.occupato || !stato.vigilanza) return;
    fermaVigilanza();
    $("#riempimento-stato").textContent = errore.message;
    // Il motivo va dove si legge: il pannello del riempimento puo' essere
    // nascosto, e la sola pastiglia «errore» del lettore faceva cercare un
    // guasto di collegamento davanti a un rifiuto del flusso («aprire prima
    // una scatola»): il lettore veniva scollegato e ricollegato per niente.
    avvisa(errore.message, "errore", 9000);
    if (errore.stato !== 400) dipingiStatoLettore("errore", "errore");
  } finally {
    stato.giroScatola = null;
  }
}

/** Il momento in cui un campione entra: suono, riga, e nient'altro. */
function annunciaTag(evento) {
  Traccia.nota("riempimento", {
    esito: evento.esito,
    epc: evento.epc,
    etichetta: evento.etichetta,
  });
  if (evento.esito === "entrato") {
    Suono.ok();
    $("#riempimento-stato").textContent =
      `${evento.paziente || "campione"} — ${evento.etichetta} riconosciuto.`;
  } else if (evento.esito === "uscito") {
    Suono.uscito();
    $("#riempimento-stato").textContent =
      `${evento.paziente || "un campione"} non è più nella scatola.`;
  } else if (evento.esito === "scatola") {
    $("#riempimento-scatola").textContent = evento.epc.slice(0, 12) + "…";
  } else if (evento.anomalia) {
    Suono.errore();
    $("#riempimento-stato").textContent = motivoAnomalia(evento);
    avvisa(motivoAnomalia(evento), "errore", 9000);
  }
}

function motivoAnomalia(evento) {
  if (evento.esito === "non_inizializzato") {
    return evento.paziente
      ? `${evento.paziente}: il tag risulta assegnato ma non ancora scritto.`
      : "Campione non inizializzato: questo contenitore non è stato preparato.";
  }
  if (evento.esito === "altra_spedizione") {
    return `${evento.paziente || "Questo contenitore"}: ${evento.dettaglio}`;
  }
  if (evento.esito === "annullato") {
    return `${evento.paziente || "Questo contenitore"} risulta annullato.`;
  }
  return evento.dettaglio || "Tag non atteso nella scatola.";
}

function dipingiRiempimento(dati) {
  if (!dati) return;
  const dentro = dati.dentro || [];
  $("#riempimento-quanti").textContent = dentro.length;
  $("#riempimento-vuoto").hidden = dentro.length > 0;
  $("#chiudi-scatola").disabled = dentro.length === 0;
  $("#riempimento-residuo").textContent = stato.residuo ?? "—";

  const elenco = $("#riempimento-dentro");
  const gia = new Set(
    Array.from(elenco.children).map((nodo) => nodo.dataset.epc)
  );
  elenco.innerHTML = "";
  dentro.forEach((voce, indice) => {
    const riga = document.createElement("li");
    riga.className = "dentro__voce";
    riga.dataset.epc = voce.epc;
    // Solo le righe che prima non c'erano si accendono: ridipingere l'elenco
    // non è un evento, e far lampeggiare tutto a ogni giro renderebbe il segno
    // insignificante.
    if (!gia.has(voce.epc)) riga.dataset.nuovo = "1";

    const numero = document.createElement("span");
    numero.className = "dentro__numero";
    numero.textContent = indice + 1;

    const chi = document.createElement("div");
    chi.className = "dentro__chi";
    const paziente = document.createElement("span");
    paziente.className = "dentro__paziente";
    paziente.textContent = voce.paziente || "paziente non noto";
    const campione = document.createElement("span");
    campione.className = "dentro__campione";
    campione.textContent = [voce.etichetta, voce.descrizione].filter(Boolean).join(" · ");
    chi.append(paziente, campione);

    const togli = document.createElement("button");
    togli.className = "bottone bottone--sobrio";
    togli.type = "button";
    togli.textContent = "Non è nella scatola";
    togli.addEventListener("click", () => togliDallaScatola(voce));

    riga.append(numero, chi, togli);
    elenco.append(riga);
  });

  dipingiAnomalie(dati.anomalie || [], dati.esclusi || []);
}

function dipingiAnomalie(anomalie, esclusi) {
  const voci = [...anomalie, ...esclusi];
  $("#riempimento-anomalie").hidden = voci.length === 0;
  const elenco = $("#anomalie-elenco");
  elenco.innerHTML = "";
  for (const voce of voci) {
    const riga = document.createElement("li");
    riga.className = "anomalie__voce";
    const testo = document.createElement("div");
    const titolo = document.createElement("strong");
    titolo.textContent = voce.paziente || voce.epc.slice(0, 12) + "…";
    const motivo = document.createElement("span");
    motivo.className = "anomalie__motivo";
    motivo.textContent =
      voce.esito === "escluso" ? voce.dettaglio : motivoAnomalia(voce);
    testo.append(titolo, motivo);
    riga.append(testo);
    elenco.append(riga);
  }
}

async function togliDallaScatola(voce) {
  const conferma = await domanda(
    "Toglilo dalla scatola",
    `<strong>${voce.paziente || voce.epc}</strong> ${voce.etichetta ? "(" + voce.etichetta + ")" : ""}
     risulta letto ma tu dici che non è dentro la scatola.
     <br><br>Torna disponibile per un'altra spedizione. Se è sul tavolo accanto
     alle antenne, spostalo: finché resta lì il lettore continuerà a vederlo.`,
    "Toglilo"
  );
  if (!conferma) return;
  try {
    stato.riempimento = annotaResiduo(
      await chiama("togli_dalla_scatola", { epc: voce.epc })
    );
    dipingiRiempimento(stato.riempimento);
    aggiornaWorkflowBar();
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function chiudiScatola() {
  fermaVigilanza();
  // Il giro gia' partito tiene la radio finche' non risponde: chiudere nel
  // frattempo dava «il lettore sta gia' eseguendo: sorveglia_scatola».
  await stato.giroScatola;
  try {
    stato.spedizione = annotaResiduo(await chiama("chiudi_riempimento"));
    stato.riempimento = null;
    $("#pannello-riempimento").classList.add("pannello--nascosto");
    dipingiSpedizione();
    aggiornaWorkflowBar();
    avvisa(
      `Scatola composta: ${stato.spedizione.attesi} ` +
        plurale(stato.spedizione.attesi, "campione", "campioni") +
        ". Verifico adesso la presenza prima della chiusura.",
      "ok",
      9000
    );
    $("#sigilla").focus();
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
    avviaVigilanza();
  }
}

async function annullaScatola() {
  const conferma = await domanda(
    "Annullare la scatola?",
    `I campioni già riconosciuti tornano disponibili per un'altra spedizione.
     Nessun tag viene toccato: restano scritti e restano validi.`,
    "Annulla la scatola"
  );
  if (!conferma) return;
  fermaVigilanza();
  await stato.giroScatola;
  try {
    await chiama("annulla_spedizione");
    stato.riempimento = null;
    stato.spedizione = annotaResiduo(await chiama("stato_spedizione"));
    $("#pannello-riempimento").classList.add("pannello--nascosto");
    dipingiSpedizione();
    await caricaCoda();
    aggiornaWorkflowBar();
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

/** Le scatole rimaste indietro: sigillate ma con la distinta ancora da mandare. */
async function caricaInSospeso() {
  try {
    const elenco = await chiama("spedizioni_aperte");
    const rimaste = (elenco.spedizioni || []).filter(
      (voce) => voce.shipment_id !== elenco.attiva && voce.da_fare
    );
    $("#in-sospeso").hidden = rimaste.length === 0;
    const lista = $("#in-sospeso-elenco");
    lista.innerHTML = "";
    for (const voce of rimaste) {
      const riga = document.createElement("li");
      riga.className = "in-sospeso__voce";
      const testo = document.createElement("span");
      testo.textContent =
        `Scatola ${voce.shipment_id} → ${voce.destinazione} · ` +
        `${voce.pezzi} ${plurale(voce.pezzi, "campione", "campioni")} · ${voce.da_fare}`;
      const apri = document.createElement("button");
      apri.className = "bottone bottone--sobrio";
      apri.type = "button";
      apri.textContent = "Riprendila";
      apri.addEventListener("click", () => riprendiSpedizione(voce.shipment_id));
      riga.append(testo, apri);
      lista.append(riga);
    }
  } catch (errore) {
    $("#in-sospeso").hidden = true;
  }
}

async function riprendiSpedizione(shipment_id) {
  try {
    fermaVigilanza();
    await stato.giroScatola;
    stato.riempimento = null;
    $("#pannello-riempimento").classList.add("pannello--nascosto");
    stato.spedizione = annotaResiduo(await chiama("riapri_spedizione", { shipment_id }));
    dipingiSpedizione();
    aggiornaWorkflowBar();
    await caricaInSospeso();
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

function accendiPunti(trovati, definitivo = false) {
  const punti = $("#volume-punti").children;
  for (let indice = 0; indice < punti.length; indice += 1) {
    if (indice < trovati) punti[indice].dataset.trovato = "1";
    // Durante la lettura un punto spento vuol dire «non ancora»: colorarlo di
    // rosso mentre le passate sono in corso sarebbe un allarme prematuro.
    else if (definitivo) punti[indice].dataset.trovato = "0";
    else punti[indice].removeAttribute("data-trovato");
  }
}

function aggiornaVerdetto(trovati, attesi, esito) {
  $("#verdetto-conteggio").textContent = `${trovati} / ${attesi}`;
  $("#verdetto").dataset.esito = esito;
  $("#volume").dataset.stato = esito === "attesa" ? "attesa" : esito;
  accendiPunti(trovati, esito === "completo" || esito === "incompleto");
}

async function sigilla() {
  if (!stato.spedizione) return;
  const contenutoConfermato = chiaveContenuto();
  if (!presenzaAttiva() || !presenzaRecente() || !stato.presenza?.completo || stato.presenzaRichiesta) {
    avvisa("Attendi una verifica recente e completa del contenuto prima di certificare.", "attesa");
    return;
  }
  stato.presenzaPausa = true;

  // La prova di chiusura oggi e' la parola dell'operatore: si chiede in modo
  // esplicito e si registra come tale nel documento del sigillo. Il giorno in
  // cui ci sara' un sensore sugli agganci, questa domanda sparisce.
  const conferma = await domanda(
    "La scatola è chiusa?",
    `Il sigillo certifica cosa c'è <strong>dentro la scatola chiusa</strong>.
     Finché il coperchio è aperto, il contenuto può ancora cambiare.
     <br><br>Confermi che il coperchio è montato e agganciato su tutti e quattro i lati?`,
    "Sì, è chiusa"
  );
  stato.presenzaPausa = false;
  if (!conferma) return;
  if (contenutoConfermato !== chiaveContenuto()) return;
  // Durante la conferma il contenuto puo' cambiare: si rilegge prima del sigillo.
  await verificaContenuto();
  if (!presenzaAttiva() || !presenzaRecente() || !stato.presenza?.completo ||
      contenutoConfermato !== chiaveContenuto()) {
    avvisa("Il contenuto non risulta completo alla nuova lettura. Controlla i tag prima di certificare.", "attesa");
    return;
  }

  stato.occupato = true;
  fermaSorveglianza();
  $("#sigilla").disabled = true;
  $("#interrompi").hidden = false;
  $("#mancanti").hidden = true;
  $("#esporta").disabled = true;
  dipingiStatoLettore("lettura del volume", "attivo");
  aggiornaVerdetto(0, stato.spedizione.attesi, "lettura");
  $("#verdetto-esito").textContent = "Lettura in corso…";

  // Nuovi giri della scatola non partono piu' (stato.occupato), ma quello
  // eventualmente in volo tiene la radio finche' non risponde.
  await stato.giroScatola;

  try {
    const esito = annotaResiduo(await chiama("sigilla"));
    stato.spedizione = esito;
    dipingiSpedizione();
    aggiornaWorkflowBar();
  } catch (errore) {
    $("#verdetto-esito").textContent = errore.message;
    $("#verdetto").dataset.esito = "incompleto";
    $("#volume").dataset.stato = "incompleto";
  } finally {
    stato.occupato = false;
    stato.presenza = null;
    $("#sigilla").disabled = true;
    $("#interrompi").hidden = true;
    dipingiStatoLettore(stato.collegato ? "pronto" : "non collegato", stato.collegato ? "pronto" : "fermo");
  }
}

function dipingiSigillo(esito) {
  const sigillo = esito.sigillo;
  if (!sigillo) return;

  // Regola non negoziabile: nessun verde se i trovati non sono gli attesi.
  const completo = sigillo.ok && sigillo.trovati === sigillo.attesi && !sigillo.missing.length;
  aggiornaVerdetto(sigillo.trovati, sigillo.attesi, completo ? "completo" : "incompleto");

  const quanti = sigillo.missing.length;
  $("#verdetto-esito").textContent = completo
    ? "Contenuto certificato: nella scatola ci sono esattamente i contenitori attesi."
    : `${plurale(quanti, "Manca", "Mancano")} ${quanti} ${plurale(
        quanti,
        "contenitore",
        "contenitori"
      )}. La spedizione non è certificata.`;
  $("#mancanti-titolo").textContent = plurale(
    quanti,
    "Contenitore non trovato",
    "Contenitori non trovati"
  );

  const dettagli = $("#verdetto-dettagli");
  dettagli.innerHTML = "";
  const righe = [
    ["passate", sigillo.passes_run],
    ["arresto", sigillo.stop_reason || "—"],
    ["prova di chiusura", sigillo.closure_proof === "operator" ? "parola dell'operatore" : sigillo.closure_proof],
    ["tag coperchio", sigillo.box_epc ? sigillo.box_epc.slice(0, 12) + "…" : "nessuno"],
    ["estranei", sigillo.unexpected.length],
    ["scartati", sigillo.rejected.length],
  ];
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    dettagli.append(dt, dd);
  }

  const mancanti = esito.mancanti_descritti || [];
  $("#mancanti").hidden = mancanti.length === 0;
  const elenco = $("#mancanti-elenco");
  elenco.innerHTML = "";
  for (const voce of mancanti) {
    const riga = document.createElement("li");
    const etichetta = document.createElement("b");
    etichetta.textContent = voce.etichetta;
    const paziente = document.createElement("span");
    paziente.textContent = voce.paziente || "paziente non noto";
    const epc = document.createElement("span");
    epc.className = "hex tenue";
    epc.textContent = voce.epc;
    riga.append(etichetta, paziente, epc);
    elenco.append(riga);
  }

  const corpo = $("#tabella-passate").querySelector("tbody");
  corpo.innerHTML = "";
  for (const passata of sigillo.passes || []) {
    const riga = document.createElement("tr");
    for (const cella of [
      passata.pass ?? "",
      (passata.antennas || []).join(" "),
      passata.read_power_cdbm ? `${(passata.read_power_cdbm / 100).toFixed(1)} dBm` : "invariata",
      passata.session ?? "—",
      passata.tags ?? "",
    ]) {
      const td = document.createElement("td");
      td.textContent = cella;
      riga.append(td);
    }
    corpo.append(riga);
  }
  $("#prova-nota").textContent = sigillo.error
    ? `Errore durante il sigillo: ${sigillo.error}`
    : "Le passate variano una condizione per volta, così i fallimenti non si assomigliano.";

  // La distinta si esporta — e si stampa — solo se il sigillo e' completo:
  // mandarne una che dichiara contenitori non verificati sposterebbe il
  // problema a destinazione, dove nessuno puo' piu' controllare.
  $("#esporta").disabled = !completo;
  $("#stampa-distinta").disabled = !completo;
  if (completo) avvisa("Contenuto certificato: puoi esportare la distinta", "ok", 8000);
  else
    avvisa(
      `${quanti} ${plurale(quanti, "contenitore non trovato", "contenitori non trovati")}: ` +
        "riapri e controlla",
      "errore",
      12000
    );
}

/* ==========================================================================
   Il foglio della distinta

   È il documento che accompagna la scatola, e i due laboratori non condividono
   nessun archivio: quello che non è scritto lì, a destinazione non esiste. La
   tabella è la copia autorevole; il QR serve solo a non ridigitare trenta righe
   all'arrivo.
   ========================================================================== */
async function stampaDistinta() {
  try {
    const foglio = await chiama("distinta_stampabile");
    dipingiFoglio(foglio);
    $("#foglio").hidden = false;
    // Il fuoco sul pulsante di stampa: da tastiera si finisce con un Invio.
    $("#foglio-stampa").focus();
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

function dipingiFoglio(foglio) {
  $("#foglio-visivo-riepilogo").textContent = foglio.visual_check
    ? `Controllo visivo: ${foglio.visual_check.stato}; visibili ${foglio.visual_check.confermati?.length ?? "—"}; RFID ${foglio.visual_check.rfid?.trovati ?? "—"}; coperchio ${foglio.visual_check.coperchio?.stato || "non richiesto"}.`
    : "Controllo visivo aggiuntivo non eseguito.";
  window.mostraProvaVisiva?.(foglio.visual_check, $("#foglio-visivo-foto"));
  $("#foglio-mittente").textContent =
    foglio.mittente.nome || foglio.mittente.insegna || "—";
  $("#foglio-destinatario").textContent = foglio.destinatario.nome || "—";

  const dati = $("#foglio-dati");
  dati.innerHTML = "";
  const sigillo = foglio.sigillo || {};
  const righe = [
    ["spedizione", `n. ${foglio.shipment_id}`],
    ["identificativo", foglio.identificativo],
    ["campioni", foglio.totale],
    ["stampata il", dataOra(foglio.stampata_il)],
    ["operatore", foglio.operatore || "—"],
    [
      "sigillo",
      sigillo.ok
        ? `verificato: ${sigillo.trovati}/${sigillo.attesi} in ${sigillo.passate} passate`
        : "NON verificato",
    ],
    ["prova di chiusura", sigillo.prova_chiusura || "—"],
  ];
  if (foglio.destinatario.citta) righe.push(["destinazione", foglio.destinatario.citta]);
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    dati.append(dt, dd);
  }

  // Tredici colonne piene non stanno su un A4 in verticale: un EPC da 24
  // caratteri finirebbe incolonnato uno per riga. Ogni campione occupa quindi
  // **due righe**: sopra chi è, sotto cosa è. Nessun dato viene lasciato
  // fuori — è il documento che il destinatario riceve, e quello che non c'è
  // scritto lì per lui non esiste.
  const intestazioni = $("#foglio-intestazioni");
  intestazioni.innerHTML = "";
  for (const titolo of ["#", "Codice fiscale", "Paziente", "S", "Nato il", "Prelievo", "Cont."]) {
    const th = document.createElement("th");
    th.textContent = titolo;
    intestazioni.append(th);
  }

  const corpo = $("#foglio-righe");
  corpo.innerHTML = "";
  foglio.righe.forEach((riga, indice) => {
    const prima = document.createElement("tr");
    prima.className = "foglio__riga";
    const celle = [
      [indice + 1, "foglio__numero"],
      [riga.codice_fiscale, "hex"],
      [riga.paziente, "foglio__paziente"],
      [riga.sesso, ""],
      [riga.data_nascita, ""],
      [[riga.data_prelievo, riga.ora_prelievo].filter(Boolean).join(" "), ""],
      [riga.etichetta, ""],
    ];
    for (const [valore, classe] of celle) {
      const td = document.createElement("td");
      if (classe) td.className = classe;
      td.textContent = valore ?? "";
      prima.append(td);
    }

    const seconda = document.createElement("tr");
    seconda.className = "foglio__dettaglio";
    const vuota = document.createElement("td");
    seconda.append(vuota);
    const dettaglio = document.createElement("td");
    dettaglio.colSpan = 6;
    const campione = document.createElement("span");
    campione.className = "foglio__campione";
    campione.textContent = riga.descrizione || "campione non descritto";
    const codici = document.createElement("span");
    codici.className = "foglio__codifiche";
    codici.textContent = [riga.materiale, riga.fissativo, riga.sede]
      .filter(Boolean)
      .join(" · ");
    const epc = document.createElement("span");
    epc.className = "hex foglio__epc";
    epc.textContent = riga.epc;
    dettaglio.append(campione, codici, epc);
    seconda.append(dettaglio);

    corpo.append(prima, seconda);
  });

  const codici = $("#foglio-codici");
  codici.innerHTML = "";
  for (const codice of foglio.codici) {
    const riquadro = document.createElement("figure");
    riquadro.className = "foglio__codice";
    // Il server produce l'SVG: qui si inserisce come markup perché è un
    // disegno, non testo dell'utente, e viene dal nostro stesso processo.
    riquadro.innerHTML = codice.svg;
    const didascalia = document.createElement("figcaption");
    didascalia.textContent =
      codice.parti > 1 ? `codice ${codice.parte} di ${codice.parti}` : "leggi qui";
    riquadro.append(didascalia);
    codici.append(riquadro);
  }

  $("#foglio-nota").textContent =
    foglio.codici.length > 1
      ? `Il codice è diviso in ${foglio.codici.length} parti: all'arrivo vanno lette tutte.`
      : "Il codice contiene le stesse righe della tabella: all'arrivo si legge invece di ridigitare.";
}

/* -- Ricezione: leggere il QR invece del file ----------------------------- */
async function leggiScansione(testi) {
  const nuove = (Array.isArray(testi) ? testi : [testi]).map(t => String(t || "").replace(/[\r\n\t]+$/, "")).filter(Boolean);
  const candidate = [...new Set([...(stato.scansioni || []), ...nuove])];
  $("#errore-scansione").textContent = "";
  try {
    const letta = await chiama("leggi_qr_distinta", { scansioni: candidate });
    if (letta.completa === false) {
      stato.scansioni = candidate;
      dipingiParti(letta.parti_acquisite.map(n => `parte ${n} letta`));
      $("#scansione-nota").textContent = letta.messaggio;
      return false;
    }
    stato.scansioni = [];
    dipingiParti([]);
    $("#scansione-nota").textContent =
      `Distinta letta: ${letta.attesi} ${plurale(letta.attesi, "campione", "campioni")}` +
      (letta.firma_verificata ? ", firma verificata." : ", firma non verificata.");
    stato.distintaQr = letta;
    stato.distinta = letta.ricezione || null;
    $("#visivo-ricevuta-foto").replaceChildren();
    stato.ultimaRicezione = null;
    if (letta.ricezione) ripristinaRicezione(letta.ricezione, true);
    $("#conferma-ricezione").disabled = true;
    $("#esporta-riscontro").disabled = true;
    $("#pannello-ricezione").classList.remove("pannello--nascosto");
    $("#leggi-volume").disabled = false;
    $("#arrivo-conteggio").textContent = `— / ${letta.attesi}`;
    $("#arrivo-mancanti").hidden = true;
    $("#arrivo-inattesi").hidden = true;
    $("#tabella-letti tbody").replaceChildren();
    $("#ricezione-esito").textContent = "Dati QR caricati. Leggere il contenuto del collo.";
    disponiPuntiIn("#arrivo-punti", letta.attesi);
    dipingiDistintaLetta(letta);
    aggiornaWorkflowBar();
    avvisa(`Distinta letta dal QR: ${letta.attesi} campioni attesi`, "ok", 8000);
    return true;
  } catch (errore) {
    $("#errore-scansione").textContent = errore.message;
    return false;
  }
}

function dipingiParti(scansioni) {
  const elenco = $("#scansione-parti");
  elenco.innerHTML = "";
  scansioni.forEach((testo) => {
    const voce = document.createElement("li");
    voce.className = "scansione__parte";
    voce.textContent = testo;
    elenco.append(voce);
  });
}

/** L'elenco atteso ricostruito dal QR, con nomi e campioni. */
function dipingiDistintaLetta(letta) {
  const riquadro = $("#distinta");
  riquadro.hidden = false;
  riquadro.innerHTML = "";
  const righe = [
    ["origine", "codice QR sul foglio"],
    ["identificativo", letta.identificativo],
    ["campioni attesi", letta.attesi],
    [
      "firma",
      letta.firma_verificata === true
        ? "verificata"
        : letta.firma_verificata === false
        ? "NON verificata"
        : "assente",
    ],
  ];
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    riquadro.append(dt, dd);
  }
  dipingiChecklistAtteso(letta.righe || [], null);
}

async function esportaDistinta() {
  try {
    await scarica("/api/distinta", "distinta.rfidman");
    stato.spedizione = annotaResiduo(await chiama("stato_spedizione"));
    dipingiSpedizione();
    aggiornaWorkflowBar();
    avvisa(
      "Distinta cifrata scaricata. Dopo la consegna al trasportatore conferma la partenza.",
      "ok",
      10000
    );
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

/** Apre il programma di posta con indirizzo, oggetto e testo già scritti.
 *
 *  L'allegato NO: una pagina web non può allegare un file a un messaggio del
 *  programma di posta. Va detto, non lasciato scoprire con una mail vuota già
 *  partita. */
async function preparaEmail() {
  let bozza;
  try {
    bozza = await chiama("bozza_email");
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
    return;
  }
  if (!bozza.a) {
    avvisa(
      `Nessuna email configurata per «${bozza.destinatario}»: aggiungila in Impostazioni`,
      "attesa",
      12000
    );
    return;
  }

  const ok = await domanda(
    "L'allegato va messo a mano",
    `Si apre il programma di posta con destinatario, oggetto e testo già scritti per
     <strong>${bozza.destinatario}</strong> (${bozza.a}).
     <br><br><strong>Il file della distinta non può essere allegato in automatico</strong>:
     una pagina web non ha il permesso di farlo. Allega tu
     <span class="hex">${bozza.nome_file}</span>, che hai appena scaricato,
     prima di inviare.`,
    "Ho capito, apri l'email"
  );
  if (!ok) return;

  const parametri = new URLSearchParams({ subject: bozza.oggetto, body: bozza.corpo });
  if (bozza.cc) parametri.set("cc", bozza.cc);
  window.location.href = `mailto:${encodeURIComponent(bozza.a)}?${parametri}`;
}

/* ==========================================================================
   Ricezione
   ========================================================================== */
async function apriDistinta(evento) {
  const file = evento.target.files?.[0];
  if (!file) return;
  $("#errore-distinta").textContent = "";
  const buffer = await file.arrayBuffer();
  let binario = "";
  const byte = new Uint8Array(buffer);
  for (let indice = 0; indice < byte.length; indice += 1) binario += String.fromCharCode(byte[indice]);

  try {
    stato.distinta = await chiama("importa_distinta", { contenuto_base64: btoa(binario) });
    window.azzeraWebcamRicezione?.();
    stato.ultimaRicezione = null;
    $("#conferma-ricezione").disabled = true;
    $("#esporta-riscontro").disabled = true;
    aggiornaWorkflowBar();
  } catch (errore) {
    // I tre errori restano tre: servono tre azioni diverse — chiedere la
    // chiave, sospettare una manomissione, accorgersi del file sbagliato.
    $("#errore-distinta").textContent = errore.message;
    $("#pannello-ricezione").classList.add("pannello--nascosto");
    return;
  }

  const distinta = stato.distinta;
  const elenco = $("#distinta");
  elenco.hidden = false;
  elenco.innerHTML = "";
  const sigillo = distinta.sigillo_partenza || {};
  for (const [chiave, valore] of [
    ["destinazione", distinta.destinazione],
    ["creata", dataOra(distinta.creata)],
    ["operatore", distinta.operatore || "—"],
    ["contenitori", distinta.attesi],
    ["SHA-256", distinta.verifica_documento?.sha256 || "—"],
    [
      "firma mittente",
      distinta.verifica_documento?.ok === true
        ? `verificata (${distinta.verifica_documento.mittente})`
        : distinta.verifica_documento?.dettaglio || "distinta legacy",
    ],
    ["sigillo alla partenza", sigillo.ok ? `completo, ${sigillo.trovati}/${sigillo.attesi}` : "non certificato"],
    ["tag coperchio", distinta.box_epc || "nessuno"],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore ?? "—";
    elenco.append(dt, dd);
  }

  $("#pannello-ricezione").classList.remove("pannello--nascosto");
  disponiPuntiIn("#arrivo-punti", distinta.attesi);
  // La checklist dei campioni attesi, per paziente: con l'import da file non
  // era mai mostrata, e invece e' il riferimento per l'apertura della scatola.
  dipingiChecklistAtteso(distinta.contenitori || [], distinta.riconciliazione?.missing || null);
  $("#arrivo-conteggio").textContent = `— / ${distinta.attesi}`;
  $("#ricezione-esito").textContent = "Appoggia la scatola chiusa sulle antenne e leggi.";
  $("#leggi-volume").disabled = false;
  $("#leggi-volume").focus();
}

function disponiPuntiIn(selettore, quanti) {
  const gruppo = $(selettore);
  gruppo.innerHTML = "";
  if (!quanti) return;
  const colonne = Math.ceil(Math.sqrt(quanti * 1.4));
  const righe = Math.ceil(quanti / colonne);
  const larghezza = 300 / colonne;
  const altezza = 210 / righe;
  const raggio = Math.max(4, Math.min(11, Math.min(larghezza, altezza) / 2.6));
  for (let indice = 0; indice < quanti; indice += 1) {
    const punto = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    punto.setAttribute("cx", 80 + larghezza * ((indice % colonne) + 0.5));
    punto.setAttribute("cy", 50 + altezza * (Math.floor(indice / colonne) + 0.5));
    punto.setAttribute("r", raggio);
    gruppo.append(punto);
  }
}

async function leggiVolume() {
  stato.occupato = true;
  stato.ultimaRicezione = null;
  $("#conferma-ricezione").disabled = true;
  $("#leggi-volume").disabled = true;
  $("#volume-arrivo").dataset.stato = "lettura";
  $("#verdetto-arrivo").dataset.esito = "lettura";
  dipingiStatoLettore("lettura del volume", "attivo");
  try {
    const esito = await chiama("leggi_volume", { inbound_id: stato.distinta?.inbound_id ?? null });
    dipingiRicezione(esito);
    aggiornaWorkflowBar();
    await window.aggiornaArchivioRicezione?.();
  } catch (errore) {
    $("#ricezione-esito").textContent = errore.message;
    $("#verdetto-arrivo").dataset.esito = "incompleto";
    $("#volume-arrivo").dataset.stato = "incompleto";
  } finally {
    stato.occupato = false;
    $("#leggi-volume").disabled = false;
    dipingiStatoLettore(stato.collegato ? "pronto" : "non collegato", stato.collegato ? "pronto" : "fermo");
  }
}

function dipingiRicezione(esito) {
  const conciliazione = esito.riconciliazione;
  const rilievo = esito.rilievo || {};
  const osservazioni = rilievo.osservazioni || [];

  avvisaBiorischio(osservazioni);

  if (!conciliazione) {
    $("#ricezione-esito").textContent =
      `Letti ${rilievo.tag_totali || 0} tag. Senza distinta non c'è niente con cui confrontarli.`;
    return;
  }

  const completo = conciliazione.ok;
  if (stato.distinta) {
    stato.distinta.stato = "open";
    stato.distinta.confermata = null;
  }
  $("#esporta-riscontro").disabled = true;
  stato.ultimaRicezione = conciliazione;
  $("#campo-motivo-ricezione").hidden = completo;
  $("#conferma-ricezione").disabled = Boolean(esito.solo_qr);
  $("#arrivo-conteggio").textContent = `${conciliazione.arrivati} / ${conciliazione.attesi}`;
  $("#verdetto-arrivo").dataset.esito = completo ? "completo" : "incompleto";
  $("#volume-arrivo").dataset.stato = completo ? "completo" : "incompleto";

  const punti = $("#arrivo-punti").children;
  for (let indice = 0; indice < punti.length; indice += 1) {
    if (indice < conciliazione.arrivati) punti[indice].dataset.trovato = "1";
    else punti[indice].dataset.trovato = "0";
  }

  const persi = conciliazione.mancanti.length;
  const intrusi = conciliazione.inattesi.length;
  $("#ricezione-esito").textContent = completo
    ? "Tutto quello che era in distinta è arrivato, e non c'è nient'altro."
    : [
        persi
          ? `${persi} ${plurale(persi, "contenitore non arrivato", "contenitori non arrivati")}`
          : "",
        intrusi
          ? `${intrusi} ${plurale(intrusi, "non in distinta", "non in distinta")}`
          : "",
      ]
        .filter(Boolean)
        .join(", ") + ".";
  if (esito.solo_qr) $("#ricezione-esito").textContent +=
    " Confronto con il foglio QR. Per registrare la ricezione ed esportare il verbale, importa la distinta cifrata della spedizione.";

  const dettagli = $("#arrivo-dettagli");
  dettagli.innerHTML = "";
  for (const [chiave, valore] of [
    ["tag letti", rilievo.tag_totali ?? 0],
    ["estranei", rilievo.tag_estranei ?? 0],
    ["non leggibili", osservazioni.filter((o) => o.stato !== "completo" && !o.campione).length],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    dettagli.append(dt, dd);
  }

  dipingiElencoArrivo("#arrivo-mancanti", "#arrivo-mancanti-elenco", esito.mancanti_descritti || []);
  dipingiElencoArrivo(
    "#arrivo-inattesi",
    "#arrivo-inattesi-elenco",
    (conciliazione.inattesi || []).map((epc) => ({ epc, etichetta: "?", paziente: "sconosciuto" }))
  );

  // Ogni riga della checklist prende il suo esito: arrivato o mancante.
  if (stato.distinta?.contenitori?.length) {
    dipingiChecklistAtteso(stato.distinta.contenitori, conciliazione.mancanti || []);
  } else if (stato.distintaQr?.righe?.length) {
    dipingiChecklistAtteso(stato.distintaQr.righe, conciliazione.mancanti || []);
  }

  const corpo = $("#tabella-letti").querySelector("tbody");
  corpo.innerHTML = "";
  for (const osservazione of osservazioni) {
    const campione = osservazione.campione || {};
    const riga = document.createElement("tr");
    for (const [testo, mono] of [
      [osservazione.contenitore || "—", false],
      [campione.paziente || "—", false],
      [campione.materiale || "—", false],
      [osservazione.stato, false],
      [osservazione.epc, true],
    ]) {
      const td = document.createElement("td");
      if (mono) td.className = "hex";
      td.textContent = testo;
      riga.append(td);
    }
    corpo.append(riga);
  }

  if (completo) avvisa("Ricezione conforme alla distinta", "ok", 8000);
  else avvisa("La scatola non corrisponde alla distinta", "errore", 12000);
}

/* ==========================================================================
   Il verbale di riscontro: come è andata, detto da chi ha aperto la scatola

   Finché questo non torna indietro, il mittente sa una cosa sola: di aver
   spedito. Le ricevute PEC provano che il documento è arrivato, non che le
   provette ci siano.
   ========================================================================== */
async function esportaRiscontro() {
  try {
    await scarica("/api/riscontro", "riscontro.rfidric");
    avvisa(
      "Verbale esportato. Va rimandato al laboratorio mittente: senza, per loro " +
        "questa scatola resta «partita e mai confermata».",
      "ok",
      12000
    );
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function importaRiscontro(evento) {
  const file = evento.target.files?.[0];
  if (!file) return;
  $("#esito-riscontro").textContent = "";
  try {
    const contenuto = await file.arrayBuffer();
    const esito = await chiama("importa_riscontro", {
      contenuto_base64: base64Da(contenuto),
    });
    const testo = esito.ok
      ? `Spedizione ${esito.shipment_id}: arrivata intera (${esito.arrivati}/${esito.attesi}).`
      : `Spedizione ${esito.shipment_id}: ${esito.mancanti} ` +
        `${plurale(esito.mancanti, "campione non arrivato", "campioni non arrivati")}.`;
    $("#esito-riscontro").textContent = testo;
    avvisa(testo, esito.ok ? "ok" : "errore", 12000);
    if (!esito.ok && esito.mancanti_descritti?.length) {
      for (const voce of esito.mancanti_descritti) {
        avvisa(`Non arrivato: ${voce.paziente || voce.epc} (${voce.etichetta})`, "errore", 15000);
      }
    }
    await generaTransito();
  } catch (errore) {
    $("#esito-riscontro").textContent = errore.message;
  } finally {
    evento.target.value = "";
  }
}

/** Un ArrayBuffer in base64, senza librerie. */
function base64Da(buffer) {
  const byte = new Uint8Array(buffer);
  let testo = "";
  // A pezzi: `String.fromCharCode(...)` su un file intero supera il limite di
  // argomenti e solleva un errore che sembra un problema del file.
  for (let indice = 0; indice < byte.length; indice += 8192) {
    testo += String.fromCharCode.apply(null, byte.subarray(indice, indice + 8192));
  }
  return btoa(testo);
}

/* -- Il riepilogo del materiale transitato -------------------------------- */
async function generaTransito() {
  const dati = {
    destinazione: $("#transito-controparte").value,
    dal: $("#transito-dal").value,
    al: $("#transito-al").value,
  };
  try {
    const riepilogo = await chiama("riepilogo_transito", dati);
    stato.transito = riepilogo;
    dipingiTransito(riepilogo);
    $("#scarica-transito").disabled = riepilogo.spedizioni.length === 0;
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

function dipingiTransito(riepilogo) {
  $("#transito").hidden = false;
  const totali = riepilogo.totali;

  const verdetto = $("#transito-verdetto");
  if (!riepilogo.spedizioni.length) {
    verdetto.dataset.esito = "attesa";
    verdetto.textContent = "Nessuna spedizione nel periodo scelto.";
  } else if (riepilogo.tutto_a_buon_fine) {
    verdetto.dataset.esito = "completo";
    verdetto.textContent =
      `Tutto a buon fine: ${totali.spedizioni} ` +
      `${plurale(totali.spedizioni, "spedizione", "spedizioni")}, ${totali.pezzi} ` +
      `${plurale(totali.pezzi, "campione", "campioni")}, tutti arrivati e confermati.`;
  } else if (totali.mancanti) {
    verdetto.dataset.esito = "incompleto";
    verdetto.textContent =
      `${totali.mancanti} ${plurale(totali.mancanti, "campione non è arrivato", "campioni non sono arrivati")}. ` +
      "Il riepilogo non può dirsi chiuso.";
  } else {
    // «Non partita» e «partita ma senza verbale» sono due cose diverse, e
    // l'operatore deve sapere quale delle due lo riguarda: la prima si
    // risolve consegnando al corriere, la seconda telefonando all'altro
    // laboratorio.
    const ferme = riepilogo.spedizioni.filter((v) => v.esito === "non partita").length;
    const inViaggio = totali.non_confermate - ferme;
    const parti = [];
    if (inViaggio) {
      parti.push(
        `${inViaggio} ${plurale(inViaggio, "spedizione partita", "spedizioni partite")} ` +
          `senza verbale di riscontro: nessuno ha ancora detto che ${plurale(inViaggio, "è arrivata", "sono arrivate")}`
      );
    }
    if (ferme) {
      parti.push(
        `${ferme} ${plurale(ferme, "spedizione", "spedizioni")} ` +
          `${plurale(ferme, "sigillata ma non ancora partita", "sigillate ma non ancora partite")}`
      );
    }
    verdetto.dataset.esito = "attesa";
    verdetto.textContent = parti.join(" · ") + ".";
  }

  const elenco = $("#transito-totali");
  elenco.innerHTML = "";
  for (const [chiave, valore] of [
    ["spedizioni", totali.spedizioni],
    ["campioni", totali.pezzi],
    ["pazienti", totali.pazienti],
    ["confermate", totali.confermate],
    ["senza verbale", totali.non_confermate],
    ["non arrivati", totali.mancanti],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    elenco.append(dt, dd);
  }

  const corpo = $("#transito-tabella").querySelector("tbody");
  corpo.innerHTML = "";
  for (const voce of riepilogo.spedizioni) {
    const riga = document.createElement("tr");
    const arrivo = voce.arrivo;
    const celle = [
      [`n. ${voce.shipment_id} → ${voce.destinazione}`, ""],
      [data(voce.partita || voce.sigillata || voce.data), ""],
      [voce.pezzi, ""],
      [voce.pazienti, ""],
      [voce.sigillo_ok === null ? "—" : voce.sigillo_ok ? "ok" : "no", ""],
      [arrivo ? `${arrivo.arrivati}/${arrivo.attesi} il ${data(arrivo.quando)}` : "—", ""],
      [voce.esito, voce.esito],
    ];
    for (const [valore, esito] of celle) {
      const td = document.createElement("td");
      if (esito) td.dataset.esito = esito;
      td.textContent = valore;
      riga.append(td);
    }
    corpo.append(riga);
  }
}

async function scaricaTransito() {
  try {
    const risposta = await fetch("/api/riepilogo", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
      body: JSON.stringify({
        destinazione: $("#transito-controparte").value,
        dal: $("#transito-dal").value,
        al: $("#transito-al").value,
      }),
    });
    if (!risposta.ok) {
      const corpo = await risposta.json().catch(() => ({}));
      throw new Error(corpo.errore || `errore ${risposta.status}`);
    }
    const intestazione = risposta.headers.get("Content-Disposition") || "";
    const trovato = /filename="([^"]+)"/.exec(intestazione);
    const blob = await risposta.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = trovato ? trovato[1] : "transito.csv";
    link.click();
    URL.revokeObjectURL(url);
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function confermaRicezione() {
  try {
    const risposta = await chiama("conferma_ricezione", {
      inbound_id: stato.distinta?.inbound_id ?? null,
      motivo_non_conformita: $("#motivo-ricezione").value.trim(),
    });
    stato.distinta = risposta;
    $("#conferma-ricezione").disabled = true;
    $("#leggi-volume").disabled = true;
    // Adesso c'è qualcosa da certificare: prima no, e un verbale su una
    // scatola mai controllata sarebbe peggio di nessun verbale.
    $("#esporta-riscontro").disabled = false;
    aggiornaWorkflowBar();
    await window.aggiornaArchivioRicezione?.();
    avvisa("Ricezione registrata nella catena di custodia", "ok", 9000);
    avvisa(
      "Esporta il verbale e mandalo al mittente: senza, per loro questa scatola " +
        "resta «partita e mai confermata».",
      "ok",
      15000
    );
  } catch (errore) {
    avvisa(errore.message, "errore", 10000);
    if (/motivazione/i.test(errore.message)) $("#motivo-ricezione").focus();
  }
}

function ripristinaRicezione(distinta, daQr = false) {
  if (!distinta?.inbound_id) return;
  $("#visivo-ricevuta-foto").replaceChildren();
  if (!daQr) window.azzeraWebcamRicezione?.();
  stato.distinta = distinta;
  stato.ultimaRicezione = null;
  $("#conferma-ricezione").disabled = true;
  $("#motivo-ricezione").value = "";
  $("#ricezione-esito").textContent = "Distinta selezionata. Leggere il contenuto del collo.";
  $("#arrivo-conteggio").textContent = `— / ${distinta.attesi}`;
  $("#arrivo-mancanti").hidden = true;
  $("#arrivo-inattesi").hidden = true;
  $("#tabella-letti tbody").replaceChildren();
  // Una ricezione già confermata prima di un riavvio ha ancora il suo verbale
  // da mandare: il pulsante deve tornare disponibile da solo.
  $("#esporta-riscontro").disabled = distinta.stato !== "received";
  const elenco = $("#distinta");
  elenco.hidden = false;
  elenco.innerHTML = "";
  for (const [chiave, valore] of [
    ["spedizione origine", distinta.inbound_id],
    ["destinazione", distinta.destinazione],
    ["creata", dataOra(distinta.creata)],
    ["contenitori", distinta.attesi],
    ["SHA-256", distinta.verifica_documento?.sha256 || "—"],
    [
      "firma mittente",
      distinta.verifica_documento?.ok === true
        ? `verificata (${distinta.verifica_documento.mittente})`
        : distinta.verifica_documento?.dettaglio || "distinta legacy",
    ],
    ["stato", distinta.stato],
  ]) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore ?? "—";
    elenco.append(dt, dd);
  }
  $("#pannello-ricezione").classList.remove("pannello--nascosto");
  disponiPuntiIn("#arrivo-punti", distinta.attesi);
  dipingiChecklistAtteso(distinta.contenitori || [], distinta.riconciliazione?.missing || null);
  $("#leggi-volume").disabled = false;
  const conciliazione = distinta.riconciliazione;
  if (conciliazione) {
    const arrivati = conciliazione.arrived?.length || 0;
    const attesi = conciliazione.expected?.length || distinta.attesi;
    $("#arrivo-conteggio").textContent = arrivati + " / " + attesi;
    $("#ricezione-esito").textContent = conciliazione.ok
      ? "Ultimo confronto conforme, salvato nell'archivio."
      : "Ultimo confronto non conforme, salvato nell'archivio.";
    $("#campo-motivo-ricezione").hidden = conciliazione.ok;
    $("#conferma-ricezione").disabled = distinta.stato === "received" || !distinta.lettura_valida;
  }
}

function dipingiElencoArrivo(contenitoreSel, elencoSel, voci) {
  $(contenitoreSel).hidden = voci.length === 0;
  const elenco = $(elencoSel);
  elenco.innerHTML = "";
  for (const voce of voci) {
    const riga = document.createElement("li");
    const etichetta = document.createElement("b");
    etichetta.textContent = voce.etichetta || "?";
    const paziente = document.createElement("span");
    paziente.textContent = voce.paziente || "";
    const epc = document.createElement("span");
    epc.className = "hex tenue";
    epc.textContent = voce.epc;
    riga.append(etichetta, paziente, epc);
    elenco.append(riga);
  }
}

/** Il materiale viaggia come codice nella distinta cifrata e come nome gia'
 *  stampato nel QR: si risolve nel codebook quando si puo', altrimenti si
 *  mostra com'e' — un nome leggibile vale piu' di un codice esatto. */
function nomeMateriale(valore) {
  if (!valore) return "";
  const voce = stato.descrizione?.codebook?.materiali?.find((x) => x.codice === valore);
  return voce?.nome || valore;
}

/* -- La checklist degli attesi ----------------------------------------------
   Un gruppo per paziente, una riga per contenitore. Lo stato parte da
   «in attesa» e si decide solo dopo una lettura vera della scatola: arrivato
   o mancante. Vale sia per la distinta da file (contenitori con codici) sia
   per quella ricostruita dal QR (righe con nomi): i due formati vengono
   normalizzati qui, invece di avere due elenchi quasi uguali. */
function dipingiChecklistAtteso(contenitori, mancanti) {
  const pannello = $("#contenitore-atteso");
  pannello.hidden = contenitori.length === 0;
  const elenco = $("#elenco-atteso");
  elenco.innerHTML = "";
  if (!contenitori.length) return;

  // Prima di qualunque lettura `mancanti` e' null: nessuna riga ha un esito.
  const persi = mancanti ? new Set(mancanti.map((epc) => String(epc).toUpperCase())) : null;

  // Raggruppa per paziente mantenendo l'ordine della distinta: chi apre la
  // scatola confronta i vasetti persona per persona, non per EPC.
  const gruppi = new Map();
  for (const voce of contenitori) {
    const paziente =
      voce.paziente || [voce.cognome, voce.nome].filter(Boolean).join(" ") || "Paziente senza nome";
    const chiave = `${paziente}|${voce.codice_fiscale || ""}|${voce.accettazione || ""}`;
    if (!gruppi.has(chiave)) {
      gruppi.set(chiave, {
        paziente,
        codice_fiscale: voce.codice_fiscale || "",
        accettazione: voce.accettazione || "",
        righe: [],
      });
    }
    gruppi.get(chiave).righe.push(voce);
  }

  let arrivati = 0;
  for (const gruppo of gruppi.values()) {
    const sezione = document.createElement("section");
    sezione.className = "checklist__paziente";
    const testa = document.createElement("header");
    testa.className = "checklist__testa";
    const nome = document.createElement("b");
    nome.textContent = gruppo.paziente;
    const dettagli = document.createElement("span");
    dettagli.className = "tenue";
    dettagli.textContent = [
      gruppo.codice_fiscale || "codice fiscale non indicato",
      gruppo.accettazione ? `accettazione ${gruppo.accettazione}` : "",
    ]
      .filter(Boolean)
      .join(" · ");
    testa.append(nome, dettagli);

    const lista = document.createElement("ul");
    lista.className = "checklist__righe";
    for (const voce of gruppo.righe) {
      const epc = String(voce.epc || "").toUpperCase();
      const esito = persi === null ? "attesa" : persi.has(epc) ? "mancante" : "presente";
      if (esito === "presente") arrivati += 1;
      const riga = document.createElement("li");
      riga.className = `checklist__riga checklist__riga--${esito}`;
      const etichetta = document.createElement("b");
      etichetta.textContent = voce.etichetta || "?";
      const campione = document.createElement("span");
      campione.textContent = nomeMateriale(voce.materiale) || voce.descrizione || "";
      const epcNodo = document.createElement("span");
      epcNodo.className = "hex tenue";
      epcNodo.textContent = voce.epc || "";
      const pastiglia = document.createElement("span");
      pastiglia.className = `checklist__stato checklist__stato--${esito}`;
      pastiglia.textContent =
        esito === "attesa" ? "In attesa" : esito === "presente" ? "✓ Arrivato" : "✗ Mancante";
      riga.append(etichetta, campione, epcNodo, pastiglia);
      lista.append(riga);
    }
    sezione.append(testa, lista);
    elenco.append(sezione);
  }

  $("#atteso-titolo").textContent =
    persi === null
      ? "Campioni attesi"
      : `Campioni attesi — ${arrivati} ${plurale(arrivati, "arrivato", "arrivati")}, ` +
        `${persi.size} ${plurale(persi.size, "mancante", "mancanti")}`;
}

/** Le avvertenze arrivano dai tag, letti attraverso la scatola chiusa: e'
 *  l'unico momento in cui l'avviso serve, cioe' prima di aprirla. */
function avvisaBiorischio(osservazioni) {
  const nomi = {
    INFECTIOUS: "rischio biologico",
    FROZEN: "campioni congelati",
    URGENT: "urgente",
    CYTOLOGY: "citologia",
  };
  const trovate = new Set();
  for (const osservazione of osservazioni) {
    for (const flag of osservazione.campione?.avvertenze || []) trovate.add(flag);
  }
  const banda = $("#biorischio");
  if (!trovate.size) {
    banda.hidden = true;
    return;
  }
  const infettivo = trovate.has("INFECTIOUS");
  $("#biorischio-titolo").textContent = infettivo ? "Rischio biologico" : "Avvertenze sui campioni";
  $("#biorischio-testo").textContent =
    (infettivo
      ? "Nella scatola ci sono campioni a rischio biologico. Dispositivi di protezione prima di aprirla. "
      : "") + `Avvertenze rilevate: ${[...trovate].map((f) => nomi[f] || f).join(", ")}.`;
  banda.hidden = false;
}

/* ==========================================================================
   Strumenti e calibrazione
   ========================================================================== */
function costruisciAntenne() {
  const contenitore = $("#antenne");
  const scelta = $("#antenna-diagnosi");
  contenitore.innerHTML = "";
  scelta.innerHTML = "";
  const ruoli = {};
  for (const antenna of stato.descrizione?.antenne?.lettura || []) ruoli[antenna] = "lettura";
  for (const antenna of stato.descrizione?.antenne?.scrittura || []) {
    ruoli[antenna] = ruoli[antenna] ? "lettura e scrittura" : "scrittura";
  }

  for (const voce of stato.descrizione?.antenne?.potenze || []) {
    const scheda = document.createElement("div");
    scheda.className = "antenna";
    scheda.dataset.id = voce.id;
    scheda.innerHTML = `
      <div class="antenna__nome"><b>Antenna ${voce.id}</b><span>${ruoli[voce.id] || "non usata"}</span></div>
      ${cursore("lettura", "Lettura", voce.lettura)}
      ${cursore("scrittura", "Scrittura", voce.scrittura)}`;
    contenitore.append(scheda);

    const opzione = document.createElement("option");
    opzione.value = voce.id;
    opzione.textContent = `Antenna ${voce.id}`;
    scelta.append(opzione);
  }

  contenitore.addEventListener("input", (evento) => {
    const campo = evento.target;
    if (campo.type !== "range") return;
    campo.closest(".cursore").querySelector(".cursore__valore").textContent = `${campo.value} dBm`;
  });
}

function cursore(chiave, etichetta, valore) {
  return `<label class="cursore">
    <span class="cursore__testa">
      <span class="etichetta">${etichetta}</span>
      <span class="cursore__valore">${valore} dBm</span>
    </span>
    <input type="range" min="5" max="30" step="1" value="${valore}" data-chiave="${chiave}">
  </label>`;
}

async function applicaPotenze() {
  const antenne = $$(".antenna").map((scheda) => ({
    id: Number(scheda.dataset.id),
    lettura: Number(scheda.querySelector('[data-chiave="lettura"]').value),
    scrittura: Number(scheda.querySelector('[data-chiave="scrittura"]').value),
  }));
  try {
    await chiama("potenze", { antenne });
    $("#esito-potenze").textContent = "Applicate.";
    avvisa("Potenze applicate al lettore", "ok");
  } catch (errore) {
    $("#esito-potenze").textContent = errore.message;
  }
}

async function applicaGen2() {
  const parametri = {
    session: $("#gen2-session").value,
    target: $("#gen2-target").value,
    target_dynamic: $("#gen2-target-dyn").checked,
    q: $("#gen2-q").value,
    q_dynamic: $("#gen2-q-dyn").checked,
    rf_mode: $("#gen2-rf").value,
  };
  try {
    const esito = await chiama("gen2", parametri);
    const applicati = esito.data?.applied || esito.data || {};
    $("#esito-gen2").textContent = `Applicati: ${JSON.stringify(applicati)}`;
    avvisa("Parametri Gen2 applicati", "ok");
  } catch (errore) {
    $("#esito-gen2").textContent = errore.message;
  }
}

/* -- Il grafico del return loss ------------------------------------------- */
const BANDA_EU_KHZ = [865000, 868000];

/* ==========================================================================
   Adattamento delle antenne

   Il VSWR misurato a 866 MHz dice se l'antenna è adattata *lì*. Non dice se
   quell'antenna è cattiva o è buona ma accordata altrove — e sono due problemi
   diversi: il primo si risolve cambiando antenna, il secondo chiedendo al
   fornitore la stessa antenna tarata per l'Europa. Per distinguerli bisogna
   vedere dove sta il minimo, e quindi guardare più in largo.

   Le curve si accumulano invece di sostituirsi: è il confronto a dire qualcosa.
   ========================================================================== */
const COLORI_CURVA = [
  "var(--ematossilina)",
  "var(--conferma)",
  "var(--attesa)",
  "var(--eosina)",
  "var(--ematossilina-debole)",
];

async function misuraAntenna() {
  const antenna = Number($("#antenna-diagnosi").value || 1);
  const scelta = $("#intervallo-diagnosi").value;
  const nota = $("#nota-diagnosi").value.trim();
  const dati = { antenna, nota };
  let etichetta = "banda configurata";
  if (scelta !== "banda") {
    const [da, a] = scelta.split(",").map(Number);
    dati.da_khz = da * 1000;
    dati.a_khz = a * 1000;
    // Un passo che dia una sessantina di punti: abbastanza per vedere la forma
    // della curva senza far durare la misura più di quanto serve.
    dati.passo_khz = Math.max(200, Math.round(((a - da) * 1000) / 60 / 100) * 100);
    etichetta = `${da}–${a} MHz`;
  }

  $("#misura-antenna").disabled = true;
  $("#rl-nota").textContent =
    "Misura in corso: il modulo spazza le frequenze, può volerci mezzo minuto.";
  try {
    let esito = await chiama("diagnostica_antenna", dati);

    // Il firmware certificato per una regione rifiuta le altre. Non è un
    // guasto: è una domanda da fare all'operatore.
    if (esito.rifiutata) {
      const conferma = await domanda(
        "Il modulo rifiuta questa banda",
        `Il lettore è impostato sulla regione
         <span class="hex">0x${esito.regione_attuale.toString(16).toUpperCase()}</span>
         e non accetta di misurare sulla
         <span class="hex">0x${esito.banda_chiesta.toString(16).toUpperCase()}</span>.
         <br><br>Posso <strong>commutarlo per il tempo della misura</strong> e
         rimetterlo a posto subito dopo. In quei secondi il modulo trasmette
         fuori dalla banda ETSI: ha senso su un banco di prototipazione e in
         nessun altro posto.`,
        "Commuta e misura"
      );
      if (!conferma) {
        $("#rl-nota").textContent = esito.messaggio;
        return;
      }
      esito = await chiama("diagnostica_antenna", {
        ...dati,
        consenti_cambio_regione: true,
      });
    }

    // La condizione entra nell'etichetta: una legenda con tre curve e nessuna
    // indicazione di cosa c'era sull'antenna e' una legenda ambigua.
    aggiungiCurva(esito, `antenna ${antenna} · ${etichetta}${nota ? ` · ${nota}` : ""}`);
    if (esito.regione_commutata) {
      avvisa("Misura fatta a regione commutata. La regione è stata rimessa a posto.", "attesa", 9000);
    }
  } catch (errore) {
    $("#rl-nota").textContent = errore.message;
    $("#verdetto-vswr").hidden = true;
  } finally {
    $("#misura-antenna").disabled = false;
  }
}

function aggiungiCurva(esito, etichetta) {
  const misure = (esito.data?.measurements || [])
    .slice()
    .sort((a, b) => a.frequency_khz - b.frequency_khz);
  if (!misure.length) {
    $("#rl-nota").textContent = "Il modulo non ha restituito misure.";
    return;
  }
  stato.curve = stato.curve || [];
  stato.curve.push({
    etichetta,
    misure,
    soglia: esito.data?.threshold ?? 7,
    risonanza: esito.data?.risonanza,
    larghezza: esito.data?.larghezza_banda,
    inBanda: esito.data?.in_banda_eu,
  });
  disegnaCurve();
}

function svuotaCurve() {
  stato.curve = [];
  disegnaCurve();
}

function disegnaCurve() {
  const curve = stato.curve || [];
  const legenda = $("#rl-legenda");
  legenda.innerHTML = "";
  const gruppo = $("#rl-curve");
  gruppo.innerHTML = "";
  const griglia = $("#rl-griglia");
  griglia.innerHTML = "";
  const assi = $("#rl-assi");
  assi.innerHTML = "";

  if (!curve.length) {
    $("#rl-banda").setAttribute("width", 0);
    $("#rl-soglia").setAttribute("d", "");
    $("#verdetto-vswr").hidden = true;
    $("#risonanza-nota").textContent = "";
    $("#rl-nota").textContent =
      "Nessuna misura ancora fatta. La zona evidenziata è la banda ETSI 865–868 MHz.";
    return;
  }

  // Le etichette degli assi stanno FUORI dall'area di disegno: dentro
  // finirebbero sopra la curva proprio dove conta di più.
  const L = 60, R = 620, T = 44, B = 250;
  const tutte = curve.flatMap((c) => c.misure);
  const soglia = curve[0].soglia;
  const fMin = Math.min(...tutte.map((m) => m.frequency_khz));
  const fMax = Math.max(...tutte.map((m) => m.frequency_khz));
  const vMax = Math.max(soglia * 1.3, ...tutte.map((m) => m.vswr));
  const x = (khz) => L + ((khz - fMin) / Math.max(1, fMax - fMin)) * (R - L);
  const y = (vswr) => B - ((vswr - 1) / Math.max(0.001, vMax - 1)) * (B - T);

  // La banda ETSI, se rientra: è dove il sistema deve funzionare per legge, e
  // il resto della curva è contesto che serve a capire, non a scegliere.
  const banda = $("#rl-banda");
  const b0 = Math.max(fMin, BANDA_EU_KHZ[0]);
  const b1 = Math.min(fMax, BANDA_EU_KHZ[1]);
  if (b1 > b0) {
    banda.setAttribute("x", x(b0));
    banda.setAttribute("width", x(b1) - x(b0));
    banda.setAttribute("y", T);
    banda.setAttribute("height", B - T);
  } else {
    banda.setAttribute("width", 0);
  }

  for (let vswr = 1; vswr <= vMax; vswr += Math.max(1, Math.round((vMax - 1) / 5))) {
    const linea = document.createElementNS("http://www.w3.org/2000/svg", "line");
    linea.setAttribute("x1", L); linea.setAttribute("x2", R);
    linea.setAttribute("y1", y(vswr)); linea.setAttribute("y2", y(vswr));
    griglia.append(linea);
    assi.append(testoSvg(L - 10, y(vswr) + 4, `${vswr}`, "end"));
  }
  assi.append(testoSvg(L, B + 22, `${(fMin / 1000).toFixed(0)} MHz`, "start"));
  assi.append(testoSvg(R, B + 22, `${(fMax / 1000).toFixed(0)} MHz`, "end"));
  assi.append(testoSvg(L - 10, T - 12, "VSWR", "end"));
  if (b1 > b0) {
    const centro = Math.min(R - 34, Math.max(L + 34, (x(b0) + x(b1)) / 2));
    assi.append(testoSvg(centro, T - 12, "banda ETSI", "middle"));
  }
  $("#rl-soglia").setAttribute("d", `M${L} ${y(soglia)} H${R}`);

  curve.forEach((curva, indice) => {
    const colore = COLORI_CURVA[indice % COLORI_CURVA.length];
    const tracciato = document.createElementNS("http://www.w3.org/2000/svg", "path");
    tracciato.setAttribute("class", "grafico__curva");
    // Stile inline e non attributo: una regola CSS batterebbe
    // l'attributo di presentazione e le curve uscirebbero tutte uguali.
    tracciato.style.stroke = colore;
    tracciato.setAttribute(
      "d",
      curva.misure.map((m, i) => `${i ? "L" : "M"}${x(m.frequency_khz)} ${y(m.vswr)}`).join(" ")
    );
    gruppo.append(tracciato);

    for (const misura of curva.misure) {
      const punto = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      punto.setAttribute("cx", x(misura.frequency_khz));
      punto.setAttribute("cy", y(misura.vswr));
      punto.setAttribute("r", curva.misure.length > 30 ? 1.5 : 3);
      punto.style.fill = colore;
      if (!misura.ok) punto.dataset.oltre = "1";
      gruppo.append(punto);
    }

    const voce = document.createElement("li");
    voce.className = "legenda__voce";
    const segno = document.createElement("span");
    segno.className = "legenda__segno";
    segno.style.background = colore;
    const testo = document.createElement("span");
    const dentro = curva.inBanda;
    testo.textContent =
      curva.etichetta +
      (dentro ? ` — in banda EU VSWR ${dentro.vswr_peggiore.toFixed(1)}` : "");
    voce.append(segno, testo);
    legenda.append(voce);
  });

  raccontaCurve(curve, soglia);
}

/** La frase che si porta al fornitore. Il VSWR peggiore da solo non dice se
 *  l'antenna è sbagliata o accordata altrove: quello lo dice dove sta il minimo. */
function raccontaCurve(curve, soglia) {
  const ultima = curve[curve.length - 1];
  const dentro = ultima.inBanda;
  const pastiglia = $("#verdetto-vswr");
  if (dentro) {
    pastiglia.hidden = false;
    pastiglia.className = `pastiglia ${dentro.ok ? "pastiglia--ok" : "pastiglia--allarme"}`;
    pastiglia.textContent = `in banda EU: VSWR ${dentro.vswr_peggiore.toFixed(1)}`;
    $("#rl-nota").textContent =
      `Nella banda ETSI 865–868 MHz il VSWR peggiore è ${dentro.vswr_peggiore.toFixed(2)} ` +
      `(soglia ${soglia}).`;
  } else {
    pastiglia.hidden = true;
    $("#rl-nota").textContent =
      "Questa misura non copre la banda ETSI: serve a vedere dove l'antenna è accordata.";
  }

  // La banda utile e' il numero che un fornitore riconosce: il datasheet
  // SLP1027 dichiara «VSWR <= 1,3 su 902-928 MHz», ed e' con quella riga che
  // la nostra misura si confronta. Il minimo, su un pannello a banda larga,
  // e' piatto e la sua posizione esatta la sposta il rumore.
  const larghezza = ultima.larghezza;
  const pezzi = [];
  if (larghezza?.trovata) {
    const da = (larghezza.da_khz / 1000).toFixed(0);
    const a = (larghezza.a_khz / 1000).toFixed(0);
    pezzi.push(
      `Sotto VSWR ${larghezza.soglia} da ${da} a ${a} MHz` +
        (larghezza.al_bordo ? " (e continua oltre la spazzata)" : "") +
        (larghezza.copre_eu
          ? " — copre tutta la banda ETSI."
          : " — la banda ETSI resta fuori.")
    );
  } else if (larghezza) {
    pezzi.push(`Mai sotto VSWR ${larghezza.soglia} in tutto l'intervallo misurato.`);
  }

  const r = ultima.risonanza;
  if (!r) {
    $("#risonanza-nota").textContent = pezzi.join(" ");
    return;
  }
  const mhz = (r.frequency_khz / 1000).toFixed(1);
  const scarto = (r.scarto_da_eu_khz / 1000).toFixed(1);
  if (r.al_bordo) {
    // Il minimo sul bordo della spazzata non è una risonanza: la risonanza
    // vera sta fuori. Dirlo lo stesso significherebbe portare al fornitore un
    // numero che non esiste.
    pezzi.push(
      `Il punto migliore misurato è ${mhz} MHz, ma sta al bordo dell'intervallo: ` +
        "il minimo vero è più in là. Allarga la spazzata."
    );
    $("#risonanza-nota").textContent = pezzi.join(" ");
    return;
  }
  pezzi.push(
    `Minimo a ${mhz} MHz (VSWR ${r.vswr.toFixed(2)}), ` +
      (Math.abs(r.scarto_da_eu_khz) < 2000
        ? "cioè dentro la banda europea."
        : `${Math.abs(scarto)} MHz ${r.scarto_da_eu_khz > 0 ? "sopra" : "sotto"} il centro ` +
          "della banda europea.")
  );
  $("#risonanza-nota").textContent = pezzi.join(" ");
}

function testoSvg(x, y, testo, ancora) {
  const nodo = document.createElementNS("http://www.w3.org/2000/svg", "text");
  nodo.setAttribute("x", x);
  nodo.setAttribute("y", y);
  nodo.setAttribute("text-anchor", ancora);
  nodo.textContent = testo;
  return nodo;
}

/* -- Profilazione --------------------------------------------------------- */
/* ==========================================================================
   Parametri Gen2: il ritorno che è sempre mancato

   I flussi operativi impostano già il Gen2 da soli — il sigillo cambia sessione
   e modalità a ogni passata, la campagna ha la sua griglia. Questi comandi
   servono a sperimentare, e sperimentare senza misurare non è sperimentare.
   ========================================================================== */
async function gen2Consigliato() {
  $("#esito-gen2").textContent = "";
  try {
    const esito = await chiama("gen2_consigliato");
    // I campi seguono quello che è stato applicato: lasciarli su «non toccare»
    // mentre il modulo è cambiato racconterebbe una cosa falsa.
    const applicati = esito.data?.applied || {};
    $("#gen2-session").value = applicati.session ?? "";
    $("#gen2-target").value = applicati.target ?? "";
    $("#gen2-q").value = "";
    $("#gen2-rf").value = applicati.rf_mode ?? "";
    $("#gen2-target-dyn").checked = Boolean(applicati.target_dynamic);
    $("#gen2-q-dyn").checked = Boolean(applicati.q_dynamic);

    dipingiGen2Riletti(esito.riletti, esito.motivi);
    if (esito.rf_sostituita) {
      avvisa(esito.avviso, "errore", 15000);
      $("#esito-gen2").textContent = esito.avviso;
    } else {
      $("#esito-gen2").textContent = "Valori consigliati applicati e riletti dal modulo.";
    }
  } catch (errore) {
    $("#esito-gen2").textContent = errore.message;
  }
}

/** I valori che il modulo dice di avere davvero, in italiano.
 *
 *  Le chiavi del contratto sono sei ma le cose da sapere sono quattro: se il
 *  target alterna, il target fisso non vuol dire niente, e lo stesso vale per Q
 *  quando è automatico. Mostrarle comunque farebbe leggere «target 0» accanto a
 *  «target alternato sì», che è una contraddizione solo apparente e costa un
 *  minuto a ogni lettura. */
function dipingiGen2Riletti(riletti, motivi) {
  const elenco = $("#gen2-riletti");
  elenco.innerHTML = "";
  const blocco = $("#gen2-riletti-blocco");
  blocco.hidden = true;
  if (!riletti || !Object.keys(riletti).length) return;

  const righe = [];
  if (riletti.session != null) {
    righe.push(["sessione", `S${riletti.session}`, motivi?.session]);
  }
  if (riletti.target_dynamic) {
    righe.push(["target", "A↔B alternato", motivi?.target]);
  } else if (riletti.target != null) {
    righe.push(["target", riletti.target === 0 ? "A" : "B", motivi?.target]);
  }
  if (riletti.q_dynamic) {
    righe.push(["Q", "automatico", motivi?.q]);
  } else if (riletti.q != null) {
    righe.push(["Q", String(riletti.q), motivi?.q]);
  }
  if (riletti.rf_mode != null) {
    const esadecimale = `0x${riletti.rf_mode.toString(16).toUpperCase()}`;
    const nome =
      riletti.rf_mode === 0x71
        ? " — massima sensibilità"
        : riletti.rf_mode === 0x6b
        ? " — predefinita"
        : "";
    righe.push(["modalità RF", esadecimale + nome, motivi?.rf_mode]);
  }

  for (const [chiave, valore, motivo] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    if (motivo) dd.title = motivo;
    elenco.append(dt, dd);
  }
  blocco.hidden = righe.length === 0;
}

/** Un tasso di lettura con una cifra decimale su cinque cifre intere promette
 *  una precisione che la misura non ha — e sul banco simulato, dove non c'è
 *  latenza radio, il numero arriva a cinque cifre. */
function tasso(valore) {
  const n = Number(valore);
  if (!Number.isFinite(n)) return "—";
  return n >= 100 ? Math.round(n).toLocaleString("it-IT") : n.toFixed(1);
}

/* -- La prova di lettura --------------------------------------------------- */
async function provaLettura() {
  try {
    const esito = await chiama("prova_lettura", { cicli: 5 });
    dipingiProva(esito);
    return esito;
  } catch (errore) {
    if (!errore.occupato) $("#esito-prova").textContent = errore.message;
    return null;
  }
}

function dipingiProva(esito) {
  // La misura precedente scende a sinistra: da solo «14 letture al secondo»
  // non vuol dire niente, vale il confronto.
  if (stato.prova) {
    $("#prova-prima").hidden = false;
    $("#prova-rate-prima").textContent = tasso(stato.prova.letture_al_secondo);
    $("#prova-dettaglio-prima").textContent = riassuntoProva(stato.prova);
  }
  stato.prova = esito;
  $("#prova-rate").textContent = tasso(esito.letture_al_secondo);
  $("#prova-dettaglio").textContent = riassuntoProva(esito);
  $("#esito-prova").textContent = esito.errori
    ? `${esito.errori} ${plurale(esito.errori, "ciclo fallito", "cicli falliti")}`
    : "";

  const tabella = $("#tabella-prova");
  tabella.hidden = esito.dettaglio.length === 0;
  const corpo = tabella.querySelector("tbody");
  corpo.innerHTML = "";
  for (const voce of esito.dettaglio) {
    const riga = document.createElement("tr");
    for (const [valore, classe] of [
      [voce.epc, "hex"],
      [voce.letture, ""],
      [`${Math.round(voce.tasso * 100)}%`, ""],
      [voce.antenne.join(" ") || "—", ""],
      [voce.rssi ?? "—", ""],
    ]) {
      const td = document.createElement("td");
      if (classe) td.className = classe;
      td.textContent = valore;
      riga.append(td);
    }
    corpo.append(riga);
  }
}

function riassuntoProva(esito) {
  return (
    `${esito.tag} ${plurale(esito.tag, "tag", "tag")} · ` +
    `${esito.letture} letture in ${esito.cicli} cicli` +
    (esito.rssi ? ` · RSSI ${esito.rssi.mediano} dBm` : "")
  );
}

function alternaProvaContinua() {
  if (stato.provaContinua) {
    fermaProvaContinua();
    return;
  }
  stato.provaContinua = setInterval(provaLettura, 1200);
  $("#prova-continua").textContent = "Ferma la misura";
  $("#prova-continua").dataset.attivo = "1";
  provaLettura();
}

function fermaProvaContinua() {
  if (!stato.provaContinua) return;
  clearInterval(stato.provaContinua);
  stato.provaContinua = null;
  $("#prova-continua").textContent = "Misura in continuo";
  $("#prova-continua").dataset.attivo = "";
}

/* ==========================================================================
   Hardware installato

   Il censimento si fa da solo al collegamento. Sta qui perché è quello che
   decide cosa gli altri pannelli possono offrire: proporre una spazzata larga
   a un modulo monoregione significherebbe far cercare all'operatore un guasto
   che non c'è, quando la risposta è «questo firmware non lo fa».
   ========================================================================== */
async function rilevaHardware() {
  $("#esito-hardware").textContent = "Interrogo il modulo…";
  $("#rileva-hardware").disabled = true;
  try {
    const hardware = await chiama("rileva_hardware");
    stato.hardware = hardware;
    if (stato.descrizione) stato.descrizione.hardware = hardware;
    dipingiHardware(hardware);
    adattaAllHardware(hardware);
    await caricaAntenne();
  } catch (errore) {
    $("#esito-hardware").textContent = errore.message;
  } finally {
    $("#rileva-hardware").disabled = false;
  }
}

function dipingiHardware(hardware) {
  const elenco = $("#hardware-dati");
  const avvisi = $("#hardware-avvisi");
  elenco.innerHTML = "";
  avvisi.innerHTML = "";
  if (!hardware || !hardware.rilevato) {
    $("#esito-hardware").textContent =
      `Hardware da rilevare: ${hardware?.motivo || "nessuna misura"}. Usa Rileva hardware quando il lettore è collegato.`;
    return;
  }
  $("#esito-hardware").textContent = "";

  const mancanti = (hardware.antenne_configurate || []).filter(
    (a) => !(hardware.antenne_collegate || []).includes(a)
  );
  const righe = [
    ["modulo", hardware.modulo || "—"],
    ["firmware", versioneFirmware(hardware.firmware)],
    ["trasporto", hardware.trasporto || "—"],
    ["numero di serie", hardware.seriale || "—"],
    ["temperatura", hardware.temperatura_c == null ? "non disponibile" : `${hardware.temperatura_c} °C`],
    ["banda in uso", hardware.banda_configurata_nome || "—"],
    ["bande accettate", (hardware.bande_nomi || []).join(" · ") || "—"],
    ["porte antenna", porteAntenna(hardware)],
    ["antenne collegate", (hardware.antenne_collegate || []).join(", ") || "nessuna"],
    ["in lettura", (hardware.antenne_lettura || []).join(", ") || "—"],
    ["in scrittura", (hardware.antenne_scrittura || []).join(", ") || "—"],
    ["antenne in configurazione", (hardware.antenne_configurate || []).join(", ") || "—"],
  ];
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    elenco.append(dt, dd);
  }

  const dire = [];
  if (hardware.multibanda) {
    dire.push(
      "Il modulo accetta più bande: la spazzata d'antenna fuori dalla banda EU " +
        "è possibile su questo esemplare."
    );
  } else {
    dire.push(hardware.motivo_limite);
  }
  if (mancanti.length) {
    dire.push(
      `${plurale(mancanti.length, "L'antenna", "Le antenne")} ${mancanti.join(", ")} ` +
        `${plurale(mancanti.length, "è in configurazione ma non risulta collegata", "sono in configurazione ma non risultano collegate")}.`
    );
  }
  const orfane = hardware.antenne_ruoli_scollegati || [];
  if (orfane.length) {
    dire.push(
      `${plurale(orfane.length, "L'antenna", "Le antenne")} ${orfane.join(", ")} ` +
        `${plurale(orfane.length, "ha un ruolo assegnato", "hanno un ruolo assegnato")} ` +
        "ma il modulo non vede niente attaccato: leggere o scrivere lì non " +
        "funzionerà. Si corregge in Impostazioni, «Antenne — chi legge e chi scrive»."
    );
  }
  for (const [chiave, motivo] of Object.entries(hardware.non_disponibili || {})) {
    dire.push(`Il modulo non ha risposto a «${chiave}»: ${motivo}`);
  }
  for (const testo of dire.filter(Boolean)) {
    const voce = document.createElement("li");
    voce.textContent = testo;
    avvisi.append(voce);
  }
}

/** Gli altri pannelli si adeguano a quello che il modulo ha detto di essere. */
function adattaAllHardware(hardware) {
  const selettore = $("#intervallo-diagnosi");
  // Un modulo monoregione non accetta elenchi di frequenze: offrire le
  // spazzate larghe prometterebbe una misura che il firmware rifiuta.
  const larga = !hardware?.rilevato || hardware.spazzata_larga;
  for (const opzione of selettore.options) {
    if (opzione.value === "banda") continue;
    opzione.disabled = !larga;
  }
  if (!larga && selettore.value !== "banda") selettore.value = "banda";
  $("#nota-modulo").textContent = larga
    ? ""
    : "Questo modulo accetta solo la sua banda: le spazzate larghe sono " +
      "disattivate, e la risonanza fuori banda va misurata con un analizzatore " +
      "d'antenna.";
}

/* ==========================================================================
   Salute del lettore

   «È il cavo, è il rumore o è il tag?» — i contatori separano le tre cose, e
   distinguerle cambia dove si va a cercare.
   ========================================================================== */
async function controllaSalute() {
  $("#salute-dati").innerHTML = "";
  $("#salute-avvisi").innerHTML = "";
  try {
    const esito = await chiama("salute");
    dipingiSalute(esito);
  } catch (errore) {
    $("#verdetto-salute").hidden = false;
    $("#verdetto-salute").className = "pastiglia pastiglia--allarme";
    $("#verdetto-salute").textContent = "non raggiungibile";
    const voce = document.createElement("li");
    voce.textContent = errore.message;
    $("#salute-avvisi").append(voce);
  }
}

function dipingiSalute(esito) {
  const dati = esito.data || {};
  const contatori = dati.counters || {};
  const collegate = dati.antennas_connected || [];
  const configurate = [
    ...(stato.descrizione?.antenne?.lettura || []),
    ...(stato.descrizione?.antenne?.scrittura || []),
  ];
  const mancanti = [...new Set(configurate)].filter((a) => !collegate.includes(a));

  const pastiglia = $("#verdetto-salute");
  pastiglia.hidden = false;
  const bene = dati.ok && !mancanti.length;
  pastiglia.className = `pastiglia ${bene ? "pastiglia--ok" : "pastiglia--allarme"}`;
  pastiglia.textContent = bene ? "tutto a posto" : "da guardare";

  const elenco = $("#salute-dati");
  elenco.innerHTML = "";
  const firmware = dati.firmware_info || {};
  const righe = [
    ["trasporto", descrizioneTrasporto(dati.transport)],
    ["avviato", dati.booted ? "sì" : "no — manca il boot del firmware"],
    ["firmware", firmware.firmware_version || firmware.version || firmware.model || "—"],
    ["antenne collegate", collegate.join(", ") || "nessuna"],
    ["antenne in configurazione", [...new Set(configurate)].join(", ") || "—"],
    ["comandi inviati", contatori.commands_sent ?? "—"],
    ["timeout (collegamento)", contatori.timeouts ?? "—"],
    ["errori di frame (rumore, cavo)", contatori.frame_errors ?? "—"],
    ["status rifiutati (comando)", contatori.status_errors ?? "—"],
    ["nessun tag in campo (non è un guasto)", contatori.no_tag_events ?? "—"],
  ];
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.textContent = valore;
    elenco.append(dt, dd);
  }

  const avvisi = $("#salute-avvisi");
  avvisi.innerHTML = "";
  const dire = [];
  if (mancanti.length) {
    dire.push(
      `${plurale(mancanti.length, "L'antenna", "Le antenne")} ${mancanti.join(", ")} ` +
        `${plurale(mancanti.length, "è configurata ma non risulta collegata", "sono configurate ma non risultano collegate")}: ` +
        "è la causa più comune di «non legge»."
    );
  }
  if (contatori.timeouts) {
    dire.push("Ci sono stati timeout: il lettore non ha risposto. Guarda cavo, porta e alimentazione.");
  }
  if (contatori.frame_errors) {
    dire.push("Ci sono errori di frame: il lettore ha risposto male. Rumore elettrico, o un cavo troppo lungo o schermato male.");
  }
  if (contatori.status_errors) {
    dire.push("Ci sono status rifiutati: il lettore ha capito e ha detto di no. Comando non supportato, o parametro fuori intervallo.");
  }
  if (contatori.last_error) {
    dire.push(`Ultimo errore (${contatori.last_error_at || "—"}): ${contatori.last_error}`);
  }
  for (const testo of dire) {
    const voce = document.createElement("li");
    voce.textContent = testo;
    avvisi.append(voce);
  }
}

/* ==========================================================================
   Misure già fatte

   Nessun archivio nuovo: adattamento, profilazione e campagna finiscono già nel
   diario. Mancava soltanto rileggerle in forma di scheda.
   ========================================================================== */
async function aggiornaMisure() {
  try {
    const esito = await chiama("registro_misure", { limite: 40 });
    const elenco = $("#elenco-misure");
    elenco.innerHTML = "";
    if (!esito.misure.length) {
      $("#esito-misure").textContent =
        "Nessuna misura nel diario. Si registrano da sole appena se ne fa una.";
      return;
    }
    $("#esito-misure").textContent = `${esito.misure.length} misure, dalla più recente.`;
    for (const misura of esito.misure) {
      const voce = document.createElement("li");
      voce.className = "misure__voce";
      voce.dataset.genere = misura.genere;
      const testa = document.createElement("div");
      testa.className = "misure__testa";
      const genere = document.createElement("b");
      genere.textContent = misura.genere;
      const quando = document.createElement("span");
      quando.className = "tenue";
      quando.textContent = dataOra(misura.quando);
      testa.append(genere, quando);
      const corpo = document.createElement("span");
      corpo.className = "misure__corpo";
      corpo.textContent = descriviMisura(misura);
      voce.append(testa, corpo);
      elenco.append(voce);
    }
  } catch (errore) {
    $("#esito-misure").textContent = errore.message;
  }
}

function descriviMisura(m) {
  if (m.genere === "adattamento") {
    const r = m.risonanza;
    return (
      `antenna ${m.antenna ?? "?"} · ${(m.da_khz / 1000).toFixed(0)}–${(m.a_khz / 1000).toFixed(0)} MHz · ` +
      `${m.punti} punti · VSWR peggiore ${Number(m.vswr_peggiore).toFixed(2)}` +
      (r ? ` · minimo a ${(r.frequency_khz / 1000).toFixed(1)} MHz` : "") +
      // La condizione della misura vale quanto i numeri: senza, non si sa se
      // due curve diverse sono due antenne o due situazioni.
      (m.nota ? ` — ${m.nota}` : "")
    );
  }
  if (m.genere === "profilazione") {
    return `${m.user_bytes} byte di USER memory · tier ${m.tier} · ${m.adatto ? "utilizzabile" : "NON utilizzabile"}`;
  }
  if (m.genere === "campagna") {
    return `${m.configurazioni} configurazioni provate` + (m.consigliata ? ` · consigliata: ${m.consigliata}` : "");
  }
  if (m.genere === "prova di lettura") {
    return `${m.tag} tag · ${tasso(m.letture_al_secondo)} letture/s`;
  }
  if (m.genere === "salute") {
    return `antenne collegate: ${(m.antenne || []).join(", ") || "nessuna"}`;
  }
  return "";
}

async function profila() {
  $("#profila").disabled = true;
  $("#esito-profilo").textContent = "Misura in corso…";
  try {
    const esito = await chiama("profila_tag");
    const profilo = esito.profilo;
    $("#esito-profilo").textContent = profilo.ok
      ? (profilo.suitable ? "Misura completata: USER compatibile con il payload cifrato." : "Misura completata: utilizzabile in solo EPC se la banca EPC è scrivibile.")
      : `Misura non completata: ${profilo.error || "riprovare su un solo tag"}`;

    const dati = $("#profilo-dati");
    dati.innerHTML = "";
    for (const [chiave, valore] of [
      ["epc", profilo.epc],
      ["tid", profilo.tid?.hex || "non letto"],
      ["costruttore", profilo.tid?.manufacturer || "—"],
      ["tid serializzato", profilo.tid ? (profilo.tid.serialized ? "sì" : "no") : "—"],
      ["user memory", `${profilo.user_bytes} byte (${profilo.user_words} word)`],
      ["payload utile", `${profilo.usable_payload_bytes} byte`],
      ["classe", profilo.tier],
      ["giudizio", profilo.recommendation],
    ]) {
      const dt = document.createElement("dt");
      dt.textContent = chiave;
      const dd = document.createElement("dd");
      dd.textContent = valore ?? "—";
      dati.append(dt, dd);
    }

    const avvisi = $("#profilo-avvisi");
    avvisi.innerHTML = "";
    for (const testo of profilo.warnings || []) {
      const li = document.createElement("li");
      li.textContent = testo;
      avvisi.append(li);
    }
    dipingiProposta(esito.proposta, profilo);
    avvisa(esito.riassunto, profilo.suitable ? "ok" : "attesa", 12000);
  } catch (errore) {
    $("#esito-profilo").textContent = errore.message;
  } finally {
    $("#profila").disabled = false;
  }
}

/** Cosa cambierebbe questa misura in configurazione.
 *
 *  Non cambia niente da sola: una modalità ridotta che si attiva in silenzio
 *  farebbe decadere una difesa senza che nessuno l'abbia decisa, e mesi dopo
 *  nessuno saprebbe più perché. */
function dipingiProposta(proposta, profilo) {
  const riquadro = $("#profilo-proposta");
  riquadro.hidden = !profilo?.ok;
  if (!profilo?.ok) { stato.proposta = null; return; }
  stato.proposta = { ...proposta, profilo };
  $("#proposta-titolo").textContent = "Caratteristiche del modello esaminato";
  $("#proposta-dati").textContent = `USER misurata: ${profilo.user_bytes} byte.`;
  $("#proposta-motivi").textContent = "Puoi usare questa misura nel profilo oppure impostare manualmente i dati del modello che intendi acquistare.";
  $("#applica-profilo").textContent = "Approva questa misura";
  $("#esito-proposta").textContent = "";
}

function nomeModalita(modalita) {
  return modalita === "solo_epc" ? "solo EPC" : "pseudonimo + campione cifrato";
}

async function applicaProfilo() {
  if (!stato.proposta?.profilo?.ok) return;
  try {
    const esito = await chiama("applica_profilo", {});
    $("#esito-proposta").textContent = esito.motivo;
    await caricaImpostazioni();
    stato.descrizione = await chiama("descrivi");
    dipingiModalita(stato.descrizione.modalita_scrittura);
  } catch (errore) { $("#esito-proposta").textContent = errore.message; }
}

/** La fascia che ricorda che si sta lavorando in modalità ridotta. Non deve
 *  essere una cosa che si scopre leggendo il codice. */
function dipingiModalita(modalita) {
  const fascia = $("#modalita-ridotta");
  const ridotta = modalita === "solo_epc";
  fascia.hidden = !ridotta;
  if (ridotta) {
    fascia.textContent =
      "Solo EPC: il codice identifica il contenitore nel database. Non è richiesta USER memory; i dati del paziente restano nel sistema e nella distinta.";
  }
}

/* -- Campagna ------------------------------------------------------------- */
async function inventariaCampagna() {
  const bottone = $("#inventaria-campagna");
  bottone.disabled = true;
  $("#avvia-campagna").disabled = true;
  $("#selezione-campagna").hidden = true;
  $("#avanzamento-inventario").textContent = "0 / 20";
  try {
    const esito = await chiama("inventario_campagna", { cicli: 20 });
    stato.campagnaTag = esito.tag || [];
    disegnaClassificazioneCampagna(stato.campagnaTag);
    $("#avanzamento-inventario").textContent = `${esito.cicli} / ${esito.cicli}`;
    avvisa(
      `${stato.campagnaTag.length} ${plurale(stato.campagnaTag.length, "tag rilevato", "tag rilevati")} in ${esito.cicli} letture`,
      stato.campagnaTag.length ? "ok" : "attesa",
      10000
    );
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  } finally {
    bottone.disabled = false;
  }
}

function disegnaClassificazioneCampagna(tag) {
  const corpo = $("#tabella-tag-campagna tbody");
  corpo.innerHTML = "";
  for (const voce of tag) {
    const tr = document.createElement("tr");
    tr.dataset.epc = voce.epc;

    const epc = document.createElement("td");
    epc.className = "epc-campagna";
    epc.textContent = voce.epc;
    const letture = document.createElement("td");
    letture.textContent = `${voce.letture}/${voce.cicli} (${Math.round(voce.tasso * 100)}%)`;
    const antenne = document.createElement("td");
    antenne.textContent = (voce.antenne || []).join(", ") || "—";
    const posizione = document.createElement("td");
    const select = document.createElement("select");
    select.className = "classifica-tag";
    select.setAttribute("aria-label", `Posizione del tag ${voce.epc}`);
    for (const [valore, testo] of [
      ["", "Da classificare"],
      ["dentro", "Dentro"],
      ["fuori", "Fuori"],
    ]) {
      const opzione = document.createElement("option");
      opzione.value = valore;
      opzione.textContent = testo;
      select.append(opzione);
    }
    select.addEventListener("change", aggiornaClassificazioneCampagna);
    posizione.append(select);
    tr.append(epc, letture, antenne, posizione);
    corpo.append(tr);
  }
  $("#selezione-campagna").hidden = tag.length === 0;
  aggiornaClassificazioneCampagna();
}

function classificazioneCampagna() {
  const gruppi = { dentro: [], fuori: [], mancanti: 0 };
  for (const riga of $$("#tabella-tag-campagna tbody tr")) {
    const posizione = riga.querySelector("select").value;
    if (posizione === "dentro" || posizione === "fuori") {
      gruppi[posizione].push(riga.dataset.epc);
    } else {
      gruppi.mancanti += 1;
    }
  }
  return gruppi;
}

function aggiornaClassificazioneCampagna() {
  const gruppi = classificazioneCampagna();
  $("#conta-dentro").textContent = `${gruppi.dentro.length} dentro`;
  $("#conta-fuori").textContent = `${gruppi.fuori.length} fuori`;
  $("#conta-dentro").className = `pastiglia ${gruppi.dentro.length ? "pastiglia--ok" : "pastiglia--attesa"}`;
  $("#conta-fuori").className = `pastiglia ${gruppi.fuori.length ? "pastiglia--ok" : "pastiglia--attesa"}`;
  $("#avvia-campagna").disabled = !(
    gruppi.dentro.length && gruppi.fuori.length && gruppi.mancanti === 0
  );
}

async function avviaCampagna() {
  const gruppi = classificazioneCampagna();
  $("#avvia-campagna").disabled = true;
  $("#avanzamento-campagna").hidden = false;
  $("#consigliata").hidden = true;
  try {
    const report = await chiama("campagna", {
      cicli: 10,
      potenze_dbm: [15, 20, 25, 30],
      dentro: gruppi.dentro,
      fuori: gruppi.fuori,
    });
    dipingiCampagna(report);
  } catch (errore) {
    avvisa(errore.message, "errore", 12000);
  } finally {
    aggiornaClassificazioneCampagna();
    $("#avanzamento-campagna").hidden = true;
  }
}

function dipingiCampagna(report) {
  const migliore = report.consigliata;
  const scheda = $("#consigliata");
  scheda.hidden = false;
  if (migliore) {
    scheda.innerHTML = `<h3>${migliore.configurazione}</h3>
      <p>Legge <strong>${migliore.dentro_trovati}</strong> tag dentro il contenitore
      (tasso ${migliore.dentro_tasso}) e nessuno di quelli fuori.
      È la più bassa fra quelle che ci riescono: più margine di rumore,
      volume di lettura più netto, meno riscaldamento.</p>`;
  } else {
    scheda.innerHTML = `<h3>Nessuna configurazione utilizzabile</h3>
      <p>Su ${report.configurazioni_provate} configurazioni provate, nessuna legge tutto
      quello che è dentro senza leggere niente di quello che è fuori.
      Con antenne complanari e tag difficili è un esito possibile: va cambiata la
      geometria, non la potenza.</p>`;
  }

  const corpo = $("#tabella-campagna").querySelector("tbody");
  corpo.innerHTML = "";
  for (const riga of report.risultati || []) {
    const tr = document.createElement("tr");
    for (const cella of [
      riga.configurazione,
      riga.dentro_trovati,
      riga.fuori_letti,
      riga.dentro_tasso,
      riga.utilizzabile ? "utilizzabile" : riga.fughe.length ? "legge fuori" : "incompleta",
    ]) {
      const td = document.createElement("td");
      td.textContent = cella;
      tr.append(td);
    }
    corpo.append(tr);
  }
}

/* -- Registro ------------------------------------------------------------- */
async function aggiornaRegistro() {
  try {
    const esito = await chiama("registro", { limite: 200 });
    const corpo = $("#tabella-registro").querySelector("tbody");
    corpo.innerHTML = "";
    for (const evento of esito.eventi) {
      const tr = document.createElement("tr");
      for (const cella of [
        dataOra(evento.at || evento.timestamp),
        evento.operation,
        evento.ok ? "ok" : "fallita",
        evento.operator || "—",
        evento.detail || "",
      ]) {
        const td = document.createElement("td");
        td.textContent = cella;
        tr.append(td);
      }
      corpo.append(tr);
    }
    const parco = await chiama("parco_tag");
    const contenitore = $("#parco");
    contenitore.innerHTML = "";
    const nomi = {
      free: "mai scritti",
      assigned: "scritti, in attesa",
      shipped: "partiti",
      voided: "annullati",
      quarantine: "in quarantena",
      retired: "fuori servizio",
    };
    for (const [chiave, quanti] of Object.entries(parco.per_stato)) {
      const voce = document.createElement("div");
      voce.className = "parco__voce";
      voce.innerHTML = `<b>${quanti}</b><span>${nomi[chiave] || chiave}</span>`;
      contenitore.append(voce);
    }
  } catch (errore) {
    avvisa(errore.message, "errore");
  }
}

/* ==========================================================================
   Date e ore in formato italiano
   ==========================================================================
   L'archivio conserva ISO 8601, che è giusto per un file. A schermo no: in un
   laboratorio italiano «2026-08-16T14:32» si legge male e si sbaglia. */
// Il trasporto arriva come stringa dal lettore vero («serial COM5@115200») e
// puo' arrivare come oggetto da altri backend. Mostrarne una forma sola
// significa stampare virgolette e graffe all'operatore, o niente.
// Il lettore vero si presenta con `firmware_version` e `firmware_date`; il
// banco con `version`. Mostrare la versione **e** la data serve a dire quale
// esemplare ha prodotto una misura, mesi dopo averla presa.
function versioneFirmware(firmware) {
  if (!firmware) return "—";
  const versione = firmware.firmware_version || firmware.version || firmware.model || "";
  const quando = firmware.firmware_date || "";
  if (versione && quando) return `${versione} (${quando})`;
  return versione || quando || "—";
}

// «Quante se ne possono installare» e «quante ce ne sono» sono due domande
// diverse, e il modulo risponde a tutt'e due nello stesso frame.
function porteAntenna(hardware) {
  const porte = hardware.antenne_porte || [];
  if (!porte.length) return hardware.antenne_massime ? String(hardware.antenne_massime) : "—";
  const libere = porte.filter((p) => !p.collegata).map((p) => p.id);
  if (!libere.length) return `${porte.length}, tutte popolate`;
  return `${porte.length}, libere: ${libere.join(", ")}`;
}

function descrizioneTrasporto(valore) {
  if (!valore) return "—";
  if (typeof valore === "string") return valore;
  return valore.description || JSON.stringify(valore);
}

function dataOra(iso) {
  if (!iso) return "—";
  const quando = new Date(iso);
  if (Number.isNaN(quando.getTime())) return String(iso);
  return quando.toLocaleString("it-IT", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function data(iso) {
  if (!iso) return "—";
  const quando = new Date(iso);
  if (Number.isNaN(quando.getTime())) return String(iso);
  return quando.toLocaleDateString("it-IT", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
}

/* ==========================================================================
   Archivio pazienti
   ========================================================================== */
/** L'elenco dei pazienti, filtrato o no.
 *
 *  Ricerca vuota vuol dire **tutti**: l'archivio si sfoglia. Chi cerca un caso
 *  di tre mesi fa spesso non ricorda il cognome, ricorda che c'era — e una
 *  tabella vuota davanti a un archivio pieno lo manda a indovinare.
 *
 *  `azzera` riparte dalla prima pagina; senza, accoda la successiva. */
async function elencaPazienti({ azzera = true } = {}) {
  const query = $("#cerca-paziente").value.trim();
  const offset = azzera ? 0 : stato.archivio.mostrati;
  $("#altri-pazienti").disabled = true;
  try {
    const risposta = await chiama("cerca_paziente", {
      query,
      offset,
      ordine: stato.archivio.ordine,
    });
    if (azzera) svuotaPazienti();
    dipingiRisultati(risposta.risultati);
    stato.archivio.mostrati = offset + risposta.risultati.length;
    stato.archivio.totale = risposta.totale;
    raccontaArchivio(risposta);
  } catch (errore) {
    esito("#esito-ricerca", errore.message, "errore");
  } finally {
    $("#altri-pazienti").disabled = false;
  }
}

/** Quanti se ne vedono e quanti ce ne sono. Sono due numeri diversi: «50
 *  pazienti» e «50 dei 1284 in archivio» si leggono uguale e non lo sono. */
function raccontaArchivio(risposta) {
  const mostrati = stato.archivio.mostrati;
  const totale = risposta.totale;
  let testo;
  if (!totale) {
    testo = risposta.filtrato
      ? "Nessun paziente trovato."
      : "L'archivio è vuoto: qui compariranno i pazienti man mano che si accettano.";
  } else if (mostrati >= totale) {
    testo = risposta.filtrato
      ? `${totale} ${plurale(totale, "paziente trovato", "pazienti trovati")}.`
      : `${totale} ${plurale(totale, "paziente", "pazienti")} in archivio, tutti in elenco.`;
  } else {
    testo =
      `${mostrati} di ${totale} ${plurale(totale, "paziente", "pazienti")}` +
      (risposta.filtrato ? " trovati." : " in archivio.");
  }
  esito("#esito-ricerca", testo, totale ? "ok" : "");

  const altri = Math.max(0, totale - mostrati);
  const bottone = $("#altri-pazienti");
  bottone.hidden = altri === 0;
  bottone.textContent = `Mostra altri ${Math.min(altri, 50)}`;
}

function svuotaPazienti() {
  $("#tabella-pazienti").querySelector("tbody").innerHTML = "";
  stato.archivio.mostrati = 0;
  $("#pannello-storico").classList.add("pannello--nascosto");
}

/** Due domande diverse: «cosa è passato di qui ultimamente» e «trovami questo
 *  cognome che non so scrivere». La prima vuole le date, la seconda l'alfabeto. */
function cambiaOrdineArchivio(ordine) {
  stato.archivio.ordine = ordine;
  $$(".ordine__voce").forEach((voce) =>
    voce.setAttribute("aria-selected", voce.dataset.ordine === ordine ? "true" : "false")
  );
  elencaPazienti({ azzera: true });
}

function dipingiRisultati(righe) {
  const corpo = $("#tabella-pazienti").querySelector("tbody");
  for (const riga of righe) {
    const tr = document.createElement("tr");
    for (const [testo, classe] of [
      [`${riga.cognome} ${riga.nome}`, ""],
      [riga.codice_fiscale, "hex"],
      [String(riga.accettazioni), ""],
      [data(riga.ultima), ""],
    ]) {
      const td = document.createElement("td");
      if (classe) td.className = classe;
      td.textContent = testo;
      tr.append(td);
    }
    const azione = document.createElement("td");
    const apri = document.createElement("button");
    apri.type = "button";
    apri.className = "riga__apri";
    apri.textContent = "Storico →";
    apri.addEventListener("click", () => apriStorico(riga.id));
    azione.append(apri);
    tr.append(azione);
    corpo.append(tr);
  }
}

async function apriStorico(patientId) {
  let storico;
  try {
    storico = await chiama("storico_paziente", { patient_id: patientId });
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
    return;
  }
  const paziente = storico.paziente;
  $("#pannello-storico").classList.remove("pannello--nascosto");
  $("#storico-paziente").textContent = `${paziente.cognome} ${paziente.nome}`;
  $("#storico-cf").textContent = paziente.codice_fiscale;

  const contenitore = $("#storico-accettazioni");
  contenitore.innerHTML = "";
  for (const accettazione of storico.accettazioni) {
    contenitore.append(costruisciAccettazione(accettazione));
  }
  $("#pannello-storico").scrollIntoView({ behavior: "smooth", block: "start" });
}

function costruisciAccettazione(accettazione) {
  const r = accettazione.riassunto;
  const blocco = document.createElement("div");
  blocco.className = "accettazione";
  blocco.dataset.esito = r.completo ? "completo" : "incompleto";

  const testa = document.createElement("div");
  testa.className = "accettazione__testa";
  const numero = document.createElement("span");
  numero.className = "accettazione__numero";
  numero.textContent = `Accettazione ${accettazione.accession_id}`;
  const quando = document.createElement("span");
  quando.className = "tenue";
  quando.textContent = `registrata il ${dataOra(accettazione.creata)}`;
  testa.append(numero, quando);
  if (accettazione.reparto) {
    const reparto = document.createElement("span");
    reparto.className = "tenue";
    reparto.textContent = `· ${accettazione.reparto}`;
    testa.append(reparto);
  }
  blocco.append(testa);

  const riassunto = document.createElement("div");
  riassunto.className = "accettazione__riassunto";
  for (const [valore, etichettaTesto] of [
    [r.pezzi, "pezzi"],
    [r.scritti, "tag scritti"],
    [r.spediti, "spediti"],
    [r.annullati, "annullati"],
  ]) {
    const voce = document.createElement("div");
    voce.innerHTML = `<b>${valore}</b><span>${etichettaTesto}</span>`;
    riassunto.append(voce);
  }
  blocco.append(riassunto);

  if (!r.spedizioni.length) {
    const nulla = document.createElement("p");
    nulla.className = "tenue";
    nulla.textContent = "Non ancora spedita.";
    nulla.style.margin = "0";
    blocco.append(nulla);
    return blocco;
  }

  for (const spedizione of r.spedizioni) {
    const riga = document.createElement("div");
    riga.className = "spedizione";

    const stato = document.createElement("span");
    stato.className =
      "pastiglia " + (spedizione.sigillo_ok === 1 ? "pastiglia--ok" : "pastiglia--allarme");
    stato.textContent = spedizione.sigillo_ok === 1 ? "sigillo completo" : "sigillo incompleto";

    const dove = document.createElement("b");
    dove.textContent = spedizione.destinazione || "destinazione non indicata";

    const pezzi = document.createElement("span");
    pezzi.textContent = `${spedizione.pezzi} ${plurale(spedizione.pezzi, "pezzo", "pezzi")}`;

    const inviata = document.createElement("span");
    inviata.className = "spedizione__quando";
    inviata.textContent = spedizione.inviata
      ? `inviata ${dataOra(spedizione.inviata)}`
      : `sigillata ${dataOra(spedizione.sigillata)} — non ancora inviata`;

    const chi = document.createElement("span");
    chi.className = "tenue";
    chi.textContent = spedizione.supervisore
      ? `supervisione: ${spedizione.supervisore}`
      : "supervisione non registrata";

    riga.append(stato, dove, pezzi, inviata, chi);
    const foto = document.createElement("button");
    foto.type = "button"; foto.className = "bottone"; foto.textContent = "Prova visiva";
    const fotoArea = document.createElement("div");
    foto.addEventListener("click", async () => {
      try {
        const esito = await chiama("controllo_visivo", {azione:"archivio", shipment_id:spedizione.shipment_id});
        window.mostraProvaVisiva?.(esito.prova, fotoArea);
      } catch(e) { avvisa(e.message, "errore"); }
    });
    riga.append(foto, fotoArea);
    if (spedizione.sigillo_dettaglio) {
      const dettaglio = document.createElement("span");
      dettaglio.className = "tenue";
      dettaglio.textContent = `· ${spedizione.sigillo_dettaglio}`;
      riga.append(dettaglio);
    }
    blocco.append(riga);
  }
  return blocco;
}

/* ==========================================================================
   Anagrafiche: laboratorio, operatori, destinatari
   ========================================================================== */
function dipingiTestata() {
  const lab = stato.descrizione?.laboratorio || {};
  $("#lab-insegna").textContent = lab.insegna || "—";
  $("#lab-dettaglio").textContent =
    [lab.codice ? lab.nome : "", lab.citta].filter(Boolean).join(" · ") ||
    (lab.nome ? "" : "laboratorio non configurato");

  const scelta = $("#operatore");
  const precedente = stato.descrizione?.operatore || scelta.value;
  scelta.innerHTML = "";
  const vuoto = document.createElement("option");
  vuoto.value = "";
  vuoto.textContent = "— nessuno —";
  scelta.append(vuoto);
  for (const voce of stato.descrizione?.operatori || []) {
    const opzione = document.createElement("option");
    opzione.value = voce.etichetta;
    opzione.textContent = voce.cognome && voce.codice
      ? `${voce.etichetta} — ${voce.cognome}`
      : voce.etichetta;
    scelta.append(opzione);
  }
  if (!(stato.descrizione?.operatori || []).length) {
    const manuale = document.createElement("option");
    manuale.value = "__manuale__";
    manuale.textContent = "— inserisci nome…";
    scelta.append(manuale);
  }
  scelta.value = [...scelta.options].some((o) => o.value === precedente) ? precedente : "";
  scelta.dataset.vuoto = scelta.value ? "" : "1";
}

function dipingiDestinatari() {
  const scelta = $("#destinazione");
  const precedente = scelta.value;
  const elenco = stato.descrizione?.destinatari || [];
  scelta.innerHTML = "";
  const vuoto = document.createElement("option");
  vuoto.value = "";
  vuoto.textContent = elenco.length ? "— scegli —" : "— nessun destinatario configurato —";
  scelta.append(vuoto);
  for (const voce of elenco) {
    const opzione = document.createElement("option");
    opzione.value = voce.nome;
    opzione.textContent = voce.codice ? `${voce.nome} (${voce.codice})` : voce.nome;
    scelta.append(opzione);
  }
  if ([...scelta.options].some((o) => o.value === precedente)) scelta.value = precedente;

  // Lo stesso elenco serve al riepilogo del transito, con in più «tutti»:
  // là la domanda può essere anche «cosa è passato in tutto, quest'anno».
  const controparte = $("#transito-controparte");
  if (controparte) {
    const scelto = controparte.value;
    controparte.innerHTML = "";
    const tutti = document.createElement("option");
    tutti.value = "";
    tutti.textContent = "— tutte le destinazioni —";
    controparte.append(tutti);
    for (const voce of elenco) {
      const opzione = document.createElement("option");
      opzione.value = voce.nome;
      opzione.textContent = voce.codice ? `${voce.nome} (${voce.codice})` : voce.nome;
      controparte.append(opzione);
    }
    if ([...controparte.options].some((o) => o.value === scelto)) controparte.value = scelto;
  }

  $("#prepara").disabled = elenco.length === 0;
  $("#destinazione-nota").textContent = elenco.length
    ? "Si aggiungono in Impostazioni → Laboratori destinatari."
    : "Nessun destinatario configurato: aggiungilo in Impostazioni prima di spedire.";
}

/** Righe modificabili: una definizione sola per operatori e destinatari. */
const COLONNE = {
  operatori: [
    { campo: "codice", larghezza: "90px" },
    { campo: "cognome" },
    { campo: "nome" },
    { campo: "email", tipo: "email" },
  ],
  destinatari: [
    { campo: "nome" },
    { campo: "codice", larghezza: "90px" },
    { campo: "email", tipo: "email" },
    { campo: "referente" },
    { campo: "citta" },
  ],
};

// Questi campi sono configurati dall'amministratore nel file YAML. Restano
// attaccati alla riga anche se la tabella compatta non li mostra: applicare una
// modifica anagrafica non deve cancellare PEC, certificati o ruolo responsabile.
const CAMPI_NASCOSTI = {
  operatori: ["ruolo", "pin_service", "pin_username"],
  destinatari: ["pec", "indirizzo", "encryption_certificate"],
};

function dipingiTabella(genere, righe) {
  const corpo = $(`#tabella-${genere}`).querySelector("tbody");
  corpo.innerHTML = "";
  if (!righe.length) {
    const vuota = document.createElement("tr");
    vuota.className = "tabella__vuota";
    const cella = document.createElement("td");
    cella.colSpan = COLONNE[genere].length + 2;
    cella.textContent =
      genere === "operatori"
        ? "Nessun operatore: finché l'elenco è vuoto il nome si può digitare liberamente."
        : "Nessun destinatario: il sigillo non potrà preparare una spedizione.";
    vuota.append(cella);
    corpo.append(vuota);
    return;
  }
  for (const riga of righe) corpo.append(costruisciRiga(genere, riga));
}

function costruisciRiga(genere, valori = {}) {
  const tr = document.createElement("tr");
  for (const colonna of COLONNE[genere]) {
    const td = document.createElement("td");
    const campo = document.createElement("input");
    campo.type = colonna.tipo || "text";
    campo.dataset.campo = colonna.campo;
    campo.value = valori[colonna.campo] || "";
    campo.spellcheck = false;
    if (colonna.larghezza) td.style.width = colonna.larghezza;
    td.append(campo);
    tr.append(td);
  }
  for (const nome of CAMPI_NASCOSTI[genere] || []) {
    const campo = document.createElement("input");
    campo.type = "hidden";
    campo.dataset.campo = nome;
    campo.value = valori[nome] || "";
    tr.firstElementChild.append(campo);
  }
  const tdAttivo = document.createElement("td");
  // La casella resta della sua misura anche sulla tavoletta: ingrandirla la
  // farebbe somigliare a un bottone. Ad allargarsi e' il bersaglio — la
  // etichetta che la avvolge prende il tocco su tutta la cella, non solo sul
  // quadratino.
  const spunta = document.createElement("label");
  spunta.className = "spunta";
  const attivo = document.createElement("input");
  attivo.type = "checkbox";
  attivo.dataset.campo = "attivo";
  attivo.checked = valori.attivo !== false;
  spunta.append(attivo);
  tdAttivo.append(spunta);
  tr.append(tdAttivo);

  const tdTogli = document.createElement("td");
  const togli = document.createElement("button");
  togli.type = "button";
  togli.className = "riga__togli";
  togli.textContent = "×";
  togli.title = "Togli questa riga";
  togli.addEventListener("click", () => tr.remove());
  tdTogli.append(togli);
  tr.append(tdTogli);
  return tr;
}

function leggiTabella(genere) {
  return $$(`#tabella-${genere} tbody tr`)
    .filter((tr) => !tr.classList.contains("tabella__vuota"))
    .map((tr) => {
      const voce = {};
      for (const campo of tr.querySelectorAll("input")) {
        voce[campo.dataset.campo] = campo.type === "checkbox" ? campo.checked : campo.value.trim();
      }
      return voce;
    });
}

function aggiungiRiga(genere) {
  const corpo = $(`#tabella-${genere}`).querySelector("tbody");
  corpo.querySelector(".tabella__vuota")?.remove();
  const riga = costruisciRiga(genere);
  corpo.append(riga);
  riga.querySelector("input")?.focus();
}

async function applicaAnagrafiche(chiave, selettoreEsito) {
  const dati =
    chiave === "laboratorio"
      ? {
          laboratorio: {
            nome: $("#lab-nome").value.trim(),
            codice: $("#lab-codice").value.trim(),
            indirizzo: $("#lab-indirizzo").value.trim(),
            cap: $("#lab-cap").value.trim(),
            citta: $("#lab-citta").value.trim(),
            provincia: $("#lab-provincia").value.trim().toUpperCase(),
            telefono: $("#lab-telefono").value.trim(),
            email: $("#lab-email").value.trim(),
            referente: $("#lab-referente").value.trim(),
            email_referente: $("#lab-email-referente").value.trim(),
          },
        }
      : { [chiave]: leggiTabella(chiave) };
  try {
    stato.descrizione = await chiama("anagrafiche", dati);
    dipingiTestata();
    dipingiDestinatari();
    caricaAnagrafiche();
    esito(selettoreEsito, "Applicato.", "ok");
    avvisa("Anagrafica aggiornata", "ok");
  } catch (errore) {
    esito(selettoreEsito, errore.message, "errore");
  }
}

function caricaAnagrafiche() {
  const lab = stato.descrizione?.laboratorio || {};
  $("#lab-nome").value = lab.nome || "";
  $("#lab-codice").value = lab.codice || "";
  $("#lab-id").value = stato.descrizione?.lab_id ?? "";
  $("#lab-indirizzo").value = lab.indirizzo || "";
  $("#lab-cap").value = lab.cap || "";
  $("#lab-citta").value = lab.citta || "";
  $("#lab-provincia").value = lab.provincia || "";
  $("#lab-telefono").value = lab.telefono || "";
  $("#lab-email").value = lab.email || "";
  $("#lab-referente").value = lab.referente || "";
  $("#lab-email-referente").value = lab.email_referente || "";
  dipingiTabella("operatori", stato.descrizione?.operatori || []);
  dipingiTabella("destinatari", stato.descrizione?.destinatari || []);
}

/* ==========================================================================
   Impostazioni
   ========================================================================== */
async function caricaImpostazioni() {
  let dati;
  try {
    dati = await chiama("impostazioni");
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
    return;
  }
  stato.impostazioni = dati;
  dipingiOperativita(dati.operativita);

  const radio = $$('input[name="trasporto"]').find((r) => r.value === dati.trasporto_attivo);
  if (radio) radio.checked = true;
  mostraCampiTrasporto();

  $("#ser-baud").value = dati.seriale.baudrate;
  $("#ser-timeout").value = dati.seriale.timeout_s;
  $("#ser-interbyte").value = dati.seriale.inter_byte_timeout_s;
  $("#tcp-host").value = dati.tcp.host;
  $("#tcp-port").value = dati.tcp.port;
  $("#tcp-timeout").value = dati.tcp.timeout_s;

  const regione = String(dati.lettore.region);
  const nota = $("#regione");
  if ([...nota.options].some((o) => o.value === regione)) nota.value = regione;
  else {
    nota.value = "";
    $("#regione-altra").value = regione;
  }
  mostraRegioneAltra();

  $("#boot-timeout").value = dati.lettore.boot_timeout_ms;
  $("#resp-timeout").value = dati.lettore.response_timeout_ms;
  $("#max-power").value = dati.lettore.max_power_dbm;
  $("#inv-timeout").value = dati.inventario.timeout_ms;
  $("#inv-flags").value = dati.inventario.metadata_flags;
  $("#inv-max").value = dati.inventario.max_unique_epcs;

  const t = dati.taratura;
  if (t.power_mode !== null && t.power_mode !== undefined) $("#power-mode").value = t.power_mode;
  if (t.antenna_dwell_ms) $("#dwell").value = t.antenna_dwell_ms;
  if (t.rssi_filter_dbm) $("#rssi-filtro").value = t.rssi_filter_dbm;
  if (t.duty_cycle_full_ms) $("#duty-full").value = t.duty_cycle_full_ms;
  if (t.duty_cycle_period_ms) $("#duty-period").value = t.duty_cycle_period_ms;
  if (t.max_rssi_reporting !== null && t.max_rssi_reporting !== undefined) {
    $("#rssi-modo").value = t.max_rssi_reporting ? "1" : "0";
  }

  $("#dove-si-salva").textContent = dati.salvabile
    ? `«Salva come predefinito» riscrive ${dati.percorso_config}.`
    : "Questa sessione non ha un file di configurazione: le modifiche valgono solo fino alla chiusura.";
  for (const bottone of [
    "#salva-collegamento",
    "#salva-avanzate",
    "#salva-laboratorio",
    "#salva-operatori",
    "#salva-destinatari",
  ]) {
    $(bottone).disabled = !dati.salvabile;
  }

  caricaAnagrafiche();
  await caricaAntenne();
  await caricaPostazione();
  await aggiornaPorte();
}

/* Quali porte leggono e quali scrivono.

   Le porte le dichiara il modulo, non l'interfaccia: su un lettore a 4 vie con
   tre antenne attaccate la quarta riga c'è ed è vuota, e si vede che è vuota.
   Assegnarle un ruolo è il guasto che si scoprirebbe col tag in mano. */
async function caricaAntenne() {
  let dati;
  try {
    dati = await chiama("antenne_ruoli");
  } catch (errore) {
    $("#antenne-nota").textContent = errore.message;
    return;
  }
  stato.antenne = dati;
  const corpo = $("#tabella-antenne tbody");
  corpo.innerHTML = "";

  for (const porta of dati.porte) {
    const riga = document.createElement("tr");

    const numero = document.createElement("td");
    numero.textContent = porta.id;
    riga.append(numero);

    const stato_ = document.createElement("td");
    if (porta.collegata === null || porta.collegata === undefined) {
      stato_.textContent = "non ancora rilevata";
      stato_.className = "tenue";
    } else if (porta.collegata) {
      stato_.textContent = "antenna collegata";
    } else {
      stato_.textContent = "porta libera";
      stato_.className = "tenue";
    }
    riga.append(stato_);

    for (const ruolo of ["lettura", "scrittura"]) {
      const cella = document.createElement("td");
      const spunta = document.createElement("input");
      spunta.type = "checkbox";
      spunta.checked = Boolean(porta[ruolo]);
      spunta.dataset.ruolo = ruolo;
      spunta.dataset.antenna = String(porta.id);
      // Una porta che il modulo dichiara vuota non si assegna: è il modulo
      // ad averlo detto, non una regola nostra.
      spunta.disabled = false;
      spunta.setAttribute(
        "aria-label",
        `antenna ${porta.id} in ${ruolo}`
      );
      cella.append(spunta);
      riga.append(cella);
    }

    const potenze = document.createElement("td");
    potenze.className = "tenue";
    potenze.textContent = `${porta.potenza_lettura} / ${porta.potenza_scrittura} dBm`;
    riga.append(potenze);

    corpo.append(riga);
  }

  const libere = dati.porte.filter((p) => p.collegata === false).length;
  if (!dati.rilevato) {
    $("#antenne-nota").textContent =
      "Rilevamento non eseguito: usa Rileva hardware per conoscere le porte. " +
      "I ruoli si possono comunque impostare manualmente.";
  } else {
    $("#antenne-nota").textContent =
      `Il modulo dichiara ${dati.massime} porte` +
      (libere
        ? `, di cui ${libere} libere: si possono installare altre ${libere} antenne.`
        : ": tutte popolate.");
  }
}

function dipingiOperativita(dati) {
  if (!dati) return;
  stato.descrizione.operativita = dati;
  stato.radioConfigurata = dati.radio_configurata;
  $("#station-mode").value = dati.station_mode;
  $("#prototype-mode").checked = dati.prototype_mode;
  $("#modo-prototipo").hidden = !dati.prototype_mode;
  $("#tag-user-bytes").value = dati.user_memory_bytes;
  $("#tag-profile-name").value = dati.active_tag_profile || "";
  for (const [chiave, valore] of Object.entries(dati.memorie)) {
    const box = document.getElementById(`memory-${chiave.replace("_", "-")}`);
    if (box) box.checked = valore;
  }
  const profili = $("#tag-profiles");
  profili.replaceChildren(new Option("Configurazione corrente", ""));
  for (const nome of Object.keys(dati.tag_profiles || {})) profili.add(new Option(nome, nome));
  profili.value = dati.active_tag_profile || "";
  for (const nome of ["accettazione", "sigillo", "ricezione"]) {
    const vietata = dati.station_mode === "ricezione" ? nome !== "ricezione" :
      dati.station_mode === "spedizione" && nome === "ricezione";
    const voce = $(`.rail__voce[data-schermata="${nome}"]`);
    voce.hidden = vietata;
    if (vietata && schermataAttiva() === nome) mostra(dati.station_mode === "ricezione" ? "ricezione" : "accettazione");
  }
  aggiornaComandoScrittura();
}

async function salvaOperativita() {
  const memorie = {};
  for (const banca of ["epc", "tid", "user"]) for (const azione of ["read", "write"]) {
    memorie[`${azione}_${banca}`] = document.getElementById(`memory-${azione}-${banca}`).checked;
  }
  try {
    const risposta = await chiama("salva_operativita", {
      station_mode: $("#station-mode").value, prototype_mode: $("#prototype-mode").checked,
      memorie, user_memory_bytes: Number($("#tag-user-bytes").value),
      profile_name: $("#tag-profile-name").value.trim(),
    });
    dipingiOperativita(risposta);
    $("#esito-operativita").textContent = "Salvato e operativo; disponibile anche dopo il riavvio.";
    stato.descrizione = await chiama("descrivi");
    dipingiModalita(stato.descrizione.modalita_scrittura);
  } catch (errore) { $("#esito-operativita").textContent = errore.message; }
}

async function salvaAntenne() {
  const scelte = (ruolo) =>
    $$(`#tabella-antenne input[data-ruolo="${ruolo}"]`)
      .filter((s) => s.checked)
      .map((s) => Number(s.dataset.antenna));

  const esito = $("#esito-antenne");
  esito.textContent = "…";
  try {
    const risposta = await chiama("salva_antenne", {
      lettura: scelte("lettura"),
      scrittura: scelte("scrittura"),
    });
    stato.radioConfigurata = Boolean(risposta.configurazione_ok);
    aggiornaComandoScrittura();
    esito.textContent =
      `lettura ${risposta.lettura.join(", ")} · scrittura ${risposta.scrittura.join(", ")}` +
      (risposta.salvato ? " — salvato" : " — valido fino alla chiusura");
    await caricaAntenne();
  } catch (errore) {
    esito.textContent = "";
    avvisa(errore.message, "errore", 9000);
  }
}

/* Da dove si comanda, e da dove ci si può collegare. */
async function caricaPostazione() {
  applicaPostazione(document.documentElement.dataset.postazione, { ricorda: false });

  const daIndirizzo = new URLSearchParams(location.search).get("modo");
  $("#postazione-nota").textContent = MODI.includes(daIndirizzo)
    ? `Questa finestra è stata aperta con «modo=${daIndirizzo}» nell'indirizzo: la scelta vale qui e non tocca gli altri apparecchi.`
    : localStorage.getItem("postazione")
      ? "Scelta salvata su questo apparecchio. Resta anche dopo un riavvio del programma."
      : `Nessuna scelta salvata: riconosciuta dal tipo di puntatore (${
          matchMedia("(pointer: coarse)").matches ? "dita" : "mouse"
        }).`;

  const elenco = $("#indirizzi-tavoletta");
  elenco.replaceChildren();
  let dati;
  try {
    dati = await chiama("indirizzi");
  } catch (errore) {
    $("#esito-postazione").textContent = errore.message;
    return;
  }

  const righe = [["Da questo computer", dati.locale]];
  for (const indirizzo of dati.rete) righe.push(["Dalla tavoletta", indirizzo]);
  for (const [chiave, valore] of righe) {
    const dt = document.createElement("dt");
    dt.textContent = chiave;
    const dd = document.createElement("dd");
    dd.className = "hex";
    dd.textContent = valore;
    elenco.append(dt, dd);
  }

  const nota = document.createElement("dt");
  nota.textContent = "Come sta";
  const spiega = document.createElement("dd");
  spiega.textContent = dati.rete.length
    ? dati.token_fisso
      ? "Il collegamento salvato sulla tavoletta continua a funzionare dopo un riavvio."
      : "Il token cambia a ogni avvio: il collegamento salvato sulla tavoletta smetterà di funzionare al prossimo riavvio. Fissa «webui.token» in config.yaml."
    : "Il programma ascolta solo su questo computer: dalla tavoletta non è raggiungibile. Imposta «webui.host: 0.0.0.0» in config.yaml e riavvia.";
  elenco.append(nota, spiega);

  const copia = $("#copia-indirizzo");
  const indirizzo = dati.rete[0] || dati.locale;
  copia.hidden = !navigator.clipboard;
  copia.onclick = async () => {
    try {
      await navigator.clipboard.writeText(indirizzo);
      $("#esito-postazione").textContent = "Indirizzo copiato.";
    } catch {
      $("#esito-postazione").textContent = "Il browser non ha concesso la copia: seleziona l'indirizzo a mano.";
    }
  };
}

function mostraCampiTrasporto() {
  const scelta = $$('input[name="trasporto"]').find((r) => r.checked)?.value || "seriale";
  $("#campi-seriale").hidden = scelta !== "seriale";
  $("#campi-tcp").hidden = scelta !== "tcp";
}

function mostraRegioneAltra() {
  $("#campo-regione-altra").hidden = $("#regione").value !== "";
}

async function aggiornaPorte() {
  const scelta = $("#ser-port");
  const attuale = stato.impostazioni?.seriale.port || "";
  let dati;
  try {
    dati = await chiama("porte_seriali");
  } catch (errore) {
    $("#porta-nota").textContent = errore.message;
    return;
  }
  scelta.innerHTML = "";
  for (const porta of dati.porte) {
    const opzione = document.createElement("option");
    opzione.value = porta.device;
    opzione.textContent = porta.descrizione
      ? `${porta.device} — ${porta.descrizione}`
      : porta.device;
    scelta.append(opzione);
  }
  // La porta configurata resta selezionabile anche se al momento non c'è:
  // toglierla farebbe credere che la configurazione sia cambiata.
  if (attuale && !dati.porte.some((p) => p.device === attuale)) {
    const opzione = document.createElement("option");
    opzione.value = attuale;
    // Corto apposta: nel menu a tendina il testo lungo viene troncato, e
    // «ora non present» sembra un difetto invece di un'informazione.
    opzione.textContent = `${attuale} (non presente)`;
    scelta.append(opzione);
  }
  if (attuale) scelta.value = attuale;

  const probabili = dati.porte.filter((p) => p.probabile).map((p) => p.device);
  if (dati.errore) $("#porta-nota").textContent = dati.errore;
  else if (!dati.porte.length)
    $("#porta-nota").textContent =
      "Nessuna porta seriale rilevata: controlla il cavo e i driver USB.";
  else if (probabili.length)
    $("#porta-nota").textContent = `Probabile lettore su ${probabili.join(", ")}.`;
  else
    $("#porta-nota").textContent =
      `${dati.porte.length} porte trovate, nessuna riconosciuta come lettore: scegliere a mano.`;
}

function datiCollegamento() {
  const scelta = $$('input[name="trasporto"]').find((r) => r.checked)?.value || "seriale";
  return scelta === "seriale"
    ? {
        trasporto: "seriale",
        port: $("#ser-port").value,
        baudrate: Number($("#ser-baud").value || 115200),
        timeout_s: Number($("#ser-timeout").value || 2),
        inter_byte_timeout_s: Number($("#ser-interbyte").value || 0.1),
      }
    : {
        trasporto: "tcp",
        host: $("#tcp-host").value.trim(),
        port_tcp: Number($("#tcp-port").value || 8080),
        timeout_s: Number($("#tcp-timeout").value || 2),
      };
}

function esito(selettore, testo, tipo) {
  const nodo = $(selettore);
  nodo.textContent = testo;
  nodo.dataset.tipo = tipo;
}

async function applicaCollegamento() {
  esito("#esito-collegamento", "Collegamento in corso…", "");
  dipingiStatoLettore("collegamento…", "attivo");
  try {
    const risposta = await chiama("applica_collegamento", datiCollegamento());
    stato.radioConfigurata = Boolean(risposta.configurazione_ok);
    const versione = risposta.avvio?.data?.firmware_info ?? risposta.avvio?.data?.version ?? "";
    stato.collegato = true;
    $("#collega").textContent = "Scollega";
    dipingiStatoLettore(stato.radioConfigurata ? "pronto" : "da configurare", stato.radioConfigurata ? "pronto" : "attesa");
    esito(
      "#esito-collegamento",
      versione
        ? `Il lettore risponde: ${JSON.stringify(versione)}`
        : "Il lettore risponde.",
      "ok"
    );
    avvisa("Lettore collegato con i nuovi parametri", "ok");
    if (!risposta.configurazione_ok) {
      esito("#esito-collegamento", risposta.configurazione_errore || "Radio da configurare", "errore");
    }
    await caricaImpostazioni();
  } catch (errore) {
    stato.collegato = false;
    dipingiStatoLettore("non collegato", "errore");
    esito("#esito-collegamento", errore.message, "errore");
  }
}

function datiAvanzate() {
  const regione = $("#regione").value === "" ? $("#regione-altra").value : $("#regione").value;
  const numero = (sel) => {
    const grezzo = $(sel).value;
    return grezzo === "" ? null : Number(grezzo);
  };
  return {
    region: regione === "" ? null : Number(regione),
    boot_timeout_ms: numero("#boot-timeout"),
    response_timeout_ms: numero("#resp-timeout"),
    max_power_dbm: numero("#max-power"),
    power_mode: $("#power-mode").value === "" ? null : Number($("#power-mode").value),
    antenna_dwell_ms: numero("#dwell"),
    duty_cycle_full_ms: numero("#duty-full"),
    duty_cycle_period_ms: numero("#duty-period"),
    rssi_filter_dbm: numero("#rssi-filtro"),
    disable_rssi_filter: $("#rssi-disattiva").checked,
    max_rssi_reporting: $("#rssi-modo").value === "" ? null : $("#rssi-modo").value === "1",
    timeout_ms: numero("#inv-timeout"),
    metadata_flags: numero("#inv-flags"),
    max_unique_epcs: numero("#inv-max"),
  };
}

async function applicaAvanzate() {
  const dati = datiAvanzate();

  // Una banda diversa da quella europea si sceglie una volta sola e a ragion
  // veduta: la conferma serve a rendere la scelta deliberata, non a ostacolarla.
  if (dati.region !== null && dati.region !== 8) {
    const ok = await domanda(
      "Banda diversa da quella europea",
      `Stai per impostare la regione <span class="hex">0x${dati.region
        .toString(16)
        .toUpperCase()
        .padStart(2, "0")}</span>.
       In Italia e nell'Unione Europea l'unica banda autorizzata è <strong>865–868 MHz</strong>
       (regione <span class="hex">0x08</span>, ETSI EN 302 208).
       <br><br>Trasmettere fuori da quella banda è responsabilità di chi imposta il lettore.
       Confermi?`,
      "Confermo, la imposto"
    );
    if (!ok) return;
  }

  esito("#esito-avanzate", "Applico…", "");
  try {
    const risposta = await chiama("avanzate", dati);
    const voci = Object.entries(risposta)
      .filter(([chiave]) => chiave !== "nota_timeout")
      .map(([chiave, valore]) => `${chiave}: ${JSON.stringify(valore)}`);
    esito("#esito-avanzate", `Applicato — ${voci.join(", ")}`, "ok");
    if (risposta.nota_timeout) avvisa(risposta.nota_timeout, "attesa", 9000);
    else avvisa("Parametri applicati al lettore", "ok");
  } catch (errore) {
    esito("#esito-avanzate", errore.message, "errore");
  }
}

async function salvaImpostazioni(selettoreEsito, conTrasporto) {
  try {
    const risposta = await chiama(
      "salva_impostazioni",
      conTrasporto ? datiCollegamento() : {}
    );
    esito(selettoreEsito, `Salvato in ${risposta.salvato}`, "ok");
    avvisa("Configurazione salvata: vale anche al prossimo avvio", "ok");
  } catch (errore) {
    esito(selettoreEsito, errore.message, "errore");
  }
}

/* ==========================================================================
   Dialogo
   ========================================================================== */
function domanda(titolo, corpoHtml, testoOk = "Conferma") {
  return new Promise((risolvi) => {
    const dialogo = $("#dialogo");
    $("#dialogo-titolo").textContent = titolo;
    $("#dialogo-corpo").innerHTML = corpoHtml;
    $("#dialogo-ok").textContent = testoOk;
    dialogo.addEventListener(
      "close",
      () => risolvi(dialogo.returnValue === "conferma"),
      { once: true }
    );
    dialogo.showModal();
    $("#dialogo-ok").focus();
  });
}

/** Come domanda(), ma chiede anche un testo: risolve il testo scritto, o null
 *  se si annulla. Il motivo di un annullamento non e' un si/no, e un prompt()
 *  nativo stonerebbe con il resto dei dialoghi. */
function chiediTesto(titolo, corpoHtml, valoreIniziale = "", testoOk = "Conferma") {
  return new Promise((risolvi) => {
    const dialogo = $("#dialogo");
    $("#dialogo-titolo").textContent = titolo;
    const corpo = $("#dialogo-corpo");
    corpo.innerHTML = corpoHtml;
    const involucro = document.createElement("label");
    involucro.className = "campo dialogo__campo";
    const campo = document.createElement("textarea");
    campo.rows = 3;
    campo.value = valoreIniziale;
    involucro.append(campo);
    corpo.append(involucro);
    $("#dialogo-ok").textContent = testoOk;
    dialogo.addEventListener(
      "close",
      () => risolvi(dialogo.returnValue === "conferma" ? campo.value.trim() : null),
      { once: true }
    );
    dialogo.showModal();
    campo.focus();
  });
}

/* ==========================================================================
   Flusso eventi
   ========================================================================== */
function ascoltaEventi() {
  const flusso = new EventSource(`/api/eventi?t=${encodeURIComponent(TOKEN)}`);

  flusso.addEventListener("scrittura", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase === "passo") aggiungiPasso(dati.testo);
  });

  // Il conteggio sale passata per passata, mentre la lettura e' ancora in
  // corso: e' il momento in cui l'operatore confronta con quello che ha visto
  // mettere nella scatola.
  flusso.addEventListener("visivo", messaggio => window.aggiornaRecuperoVisivo?.(JSON.parse(messaggio.data)));
  flusso.addEventListener("sigillo", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase !== "passata") return;
    aggiornaVerdetto(dati.trovati, dati.attesi, "lettura");
    $("#verdetto-esito").textContent = `Passata ${dati.passata} — lettura in corso…`;
  });

  // Il riempimento: ogni ingresso arriva di qui nel momento in cui accade. La
  // pagina interroga comunque il server (le serve il riepilogo), ma è questo
  // che permette a una seconda schermata aperta di restare allineata.
  flusso.addEventListener("riempimento", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase === "conteggio") {
      $("#riempimento-quanti").textContent = dati.quanti;
    }
  });

  flusso.addEventListener("ricezione", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase === "lettura") $("#ricezione-esito").textContent = "Lettura della scatola in corso…";
  });

  // La campagna dura minuti: senza avanzamento sembrerebbe bloccata.
  flusso.addEventListener("campagna", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase === "inventario_ciclo") {
      const pastiglia = $("#avanzamento-inventario");
      pastiglia.textContent = `${dati.indice} / ${dati.totale} · ${dati.trovati} tag`;
      pastiglia.className = "pastiglia pastiglia--radio";
      return;
    }
    if (dati.fase !== "configurazione") return;
    const pastiglia = $("#avanzamento-campagna");
    pastiglia.hidden = false;
    pastiglia.textContent = `${dati.indice} / ${dati.totale} — ${dati.risultato.configurazione}`;
    pastiglia.className = `pastiglia ${dati.risultato.utilizzabile ? "pastiglia--ok" : "pastiglia--radio"}`;
  });

  flusso.addEventListener("servizio", (messaggio) => {
    const evento = JSON.parse(messaggio.data).data;
    if (evento.kind === "error") avvisa(evento.data?.message || "errore del lettore", "errore", 9000);
  });

  flusso.onerror = () => {
    // EventSource riprova da solo all'infinito. Con un token rifiutato non
    // riuscira' mai: si chiude, invece di martellare il server.
    if (!$("#sbarramento").hidden) {
      flusso.close();
      return;
    }
    dipingiStatoLettore(stato.collegato ? "flusso interrotto" : "non collegato", "errore");
  };
}

/* ==========================================================================
   Avvio
   ========================================================================== */
async function avvia() {
  $("#salva-operativita").addEventListener("click", salvaOperativita);
  $("#prossimo-contenitore").addEventListener("click", () => {
    avanza(); Scena.stato("attesa", "Appoggia un solo tag e premi Scrivi"); aggiornaComandoScrittura();
  });
  $("#applica-radio").addEventListener("click", async () => {
    try {
      const r = await chiama("applica_radio");
      stato.radioConfigurata = r.configurazione_ok;
      dipingiStatoLettore(r.configurazione_ok ? "pronto" : "da configurare", r.configurazione_ok ? "pronto" : "attesa");
      $("#esito-collegamento").textContent = r.configurazione_ok ? "Configurazione applicata" : r.configurazione_errore;
      aggiornaComandoScrittura();
    } catch (e) { $("#esito-collegamento").textContent = e.message; }
  });
  $("#tag-profiles").addEventListener("change", () => {
    const nome = $("#tag-profiles").value;
    const p = stato.descrizione?.operativita?.tag_profiles?.[nome];
    if (!p) return;
    $("#tag-profile-name").value = nome;
    $("#tag-user-bytes").value = p.user_memory_bytes;
    for (const [k, v] of Object.entries(p.memorie)) {
      const box = document.getElementById(`memory-${k.replace("_", "-")}`);
      if (box) box.checked = v;
    }
    $("#esito-operativita").textContent = "Profilo caricato: salvare per applicarlo.";
  });
  for (const id of ["memory-read-user", "memory-write-user"]) {
    document.getElementById(id).addEventListener("change", () => {
      if ($("#memory-read-user").checked || $("#memory-write-user").checked) $("#memory-read-tid").checked = true;
    });
  }
  // Gli strumenti di riconoscimento sono accanto alla configurazione che informano.
  $("#config-tag-rilevamento").append($("#profila").closest(".pannello"));
  $("#tabella-antenne").closest(".pannello").before($("#rileva-hardware").closest(".pannello"));
  applicaTema(localStorage.getItem("tema") || "sistema");
  // Prima di ogni altra cosa: decide le misure di tutta l'interfaccia, e
  // cambiarle dopo che la pagina si e' disegnata si vedrebbe.
  applicaPostazione(postazioneRichiesta(), { ricorda: false });
  collegaManifesto();
  Scena.collega($("#scena"));
  Traccia.collega();
  Scena.osserva((nome, testo) => Traccia.nota("scena", { stato: nome, testo }));

  // Indirizzo senza token: inutile provare, si dice subito.
  if (!TOKEN) {
    sbarra();
    return;
  }

  $("#tema").addEventListener("click", alternaTema);
  $("#collega").addEventListener("click", () => (stato.collegato ? scollega() : collega()));
  $("#modulo-accettazione").addEventListener("submit", registra);
  // Senza la lambda l'ascoltatore passerebbe l'evento del click come primo
  // argomento, cioe' una deroga di riscrittura a ogni pressione del bottone.
  $("#scrivi").addEventListener("click", () => scrivi());
  $("#salta").addEventListener("click", annullaContenitore);
  $("#riscrivi").addEventListener("click", riscriviTag);
  $("#etichetta").addEventListener("click", () => mostraEtichetta(stato.ultimoContenitore));
  $("#dialogo-etichetta").addEventListener("close", stampaEtichetta);
  $("#aggiungi-reperto").addEventListener("click", aggiungiReperto);
  $("#annulla-accettazione").addEventListener("click", annullaAccettazione);
  $("#nuova-accettazione").addEventListener("click", () => nuovaAccettazione());
  $("#cerca").addEventListener("click", () => elencaPazienti({ azzera: true }));
  $("#cerca-paziente").addEventListener("keydown", (evento) => {
    if (evento.key === "Enter") {
      evento.preventDefault();
      elencaPazienti({ azzera: true });
    }
  });
  // Il campo di ricerca filtra mentre si scrive, ma non a ogni tasto: sono
  // interrogazioni all'archivio, e con una tastiera veloce ne partirebbero
  // dieci per un cognome. Svuotarlo rimette tutti, senza dover premere niente.
  $("#cerca-paziente").addEventListener("input", () => {
    clearTimeout(stato.archivio.attesa);
    stato.archivio.attesa = setTimeout(() => elencaPazienti({ azzera: true }), 250);
  });
  $("#altri-pazienti").addEventListener("click", () => elencaPazienti({ azzera: false }));
  $$(".ordine__voce").forEach((voce) =>
    voce.addEventListener("click", () => cambiaOrdineArchivio(voce.dataset.ordine))
  );
  // Registrati una volta sola: erano dentro `mostra()`, e a ogni cambio di
  // schermata se ne aggiungeva un altro.
  $$(".schede__voce").forEach((voce) =>
    voce.addEventListener("click", () => mostraScheda(voce.dataset.scheda))
  );
  $("#vai-al-sigillo").addEventListener("click", () => mostra("sigillo"));
  $("#apri-scatola").addEventListener("click", apriScatola);
  $("#chiudi-scatola").addEventListener("click", chiudiScatola);
  $("#annulla-scatola").addEventListener("click", annullaScatola);
  $("#suono-attivo").addEventListener("change", (evento) => {
    Suono.attivo = evento.target.checked;
    if (Suono.attivo) {
      Suono.sblocca();
      Suono.ok();
    }
  });
  $("#prepara").addEventListener("click", preparaSpedizione);
  $("#aggiorna-coda").addEventListener("click", caricaCoda);
  $("#seleziona-coda").addEventListener("click", selezionaTuttaCoda);
  $("#annulla-spedizione").addEventListener("click", annullaSpedizione);
  $("#conferma-invio").addEventListener("click", confermaInvio);
  $("#invia-pec").addEventListener("click", inviaDistintaPec);
  $("#aggiorna-pec").addEventListener("click", aggiornaRicevutePec);
  $("#file-distinta").addEventListener("change", (evento) => importaDistinteMultiple(evento));
  $("#leggi-volume").addEventListener("click", leggiVolume);
  $("#conferma-ricezione").addEventListener("click", confermaRicezione);
  $("#esporta-riscontro").addEventListener("click", esportaRiscontro);
  $("#file-riscontro").addEventListener("change", importaRiscontro);
  $("#genera-transito").addEventListener("click", generaTransito);
  $("#scarica-transito").addEventListener("click", scaricaTransito);
  $("#applica-potenze").addEventListener("click", applicaPotenze);
  $("#applica-gen2").addEventListener("click", applicaGen2);
  $("#misura-antenna").addEventListener("click", misuraAntenna);
  $("#svuota-curve").addEventListener("click", svuotaCurve);
  $("#gen2-consigliato").addEventListener("click", gen2Consigliato);
  $("#prova-lettura").addEventListener("click", provaLettura);
  $("#prova-continua").addEventListener("click", alternaProvaContinua);
  $("#controlla-salute").addEventListener("click", controllaSalute);
  $("#rileva-hardware").addEventListener("click", rilevaHardware);
  $("#aggiorna-misure").addEventListener("click", aggiornaMisure);
  $("#applica-profilo").addEventListener("click", applicaProfilo);
  $("#profila").addEventListener("click", profila);
  $("#inventaria-campagna").addEventListener("click", inventariaCampagna);
  $("#avvia-campagna").addEventListener("click", avviaCampagna);
  $("#aggiorna-registro").addEventListener("click", aggiornaRegistro);
  $("#aggiorna-porte").addEventListener("click", aggiornaPorte);
  $("#applica-collegamento").addEventListener("click", applicaCollegamento);
  $("#salva-collegamento").addEventListener("click", () =>
    salvaImpostazioni("#esito-collegamento", true)
  );
  $("#applica-avanzate").addEventListener("click", applicaAvanzate);
  $("#salva-avanzate").addEventListener("click", () =>
    salvaImpostazioni("#esito-avanzate", false)
  );
  $("#regione").addEventListener("change", mostraRegioneAltra);
  $$('input[name="trasporto"]').forEach((radio) =>
    radio.addEventListener("change", mostraCampiTrasporto)
  );
  $$('input[name="postazione"]').forEach((radio) =>
    radio.addEventListener("change", () => {
      applicaPostazione(radio.value);
      $("#esito-postazione").textContent =
        radio.value === "tavoletta" ? "Misure da tavoletta." : "Misure da banco.";
    })
  );
  $("#sigilla").addEventListener("click", sigilla);
  $("#verifica-contenuto").addEventListener("click", verificaContenuto);
  setInterval(aggiornaPresenzaContenuto, 1000);
  $("#stampa-distinta").addEventListener("click", stampaDistinta);
  $("#foglio-stampa").addEventListener("click", () => window.print());
  $("#foglio-chiudi").addEventListener("click", () => ($("#foglio").hidden = true));
  $("#esporta").addEventListener("click", esportaDistinta);
  $("#interrompi").addEventListener("click", () =>
    fetch("/api/interrompi", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
      body: "{}",
    })
  );
  $("#operatore").addEventListener("change", async (evento) => {
    if (evento.target.value === "__manuale__") {
      const nome = prompt("Nome o sigla dell'operatore in servizio:");
      if (!nome?.trim()) {
        evento.target.value = "";
        return;
      }
      const opzione = document.createElement("option");
      opzione.value = nome.trim();
      opzione.textContent = nome.trim();
      evento.target.append(opzione);
      evento.target.value = nome.trim();
    }
    evento.target.dataset.vuoto = evento.target.value ? "" : "1";
    try {
      await chiama("operatore", { nome: evento.target.value });
    } catch (errore) {
      avvisa(errore.message, "errore", 9000);
    }
  });
  $("#prepara-email").addEventListener("click", preparaEmail);
  $("#applica-laboratorio").addEventListener("click", () =>
    applicaAnagrafiche("laboratorio", "#esito-laboratorio")
  );
  $("#applica-operatori").addEventListener("click", () =>
    applicaAnagrafiche("operatori", "#esito-operatori")
  );
  $("#applica-destinatari").addEventListener("click", () =>
    applicaAnagrafiche("destinatari", "#esito-destinatari")
  );
  $("#salva-antenne").addEventListener("click", salvaAntenne);
  $("#aggiungi-operatore").addEventListener("click", () => aggiungiRiga("operatori"));
  $("#aggiungi-destinatario").addEventListener("click", () => aggiungiRiga("destinatari"));
  for (const [bottone, esitoSel] of [
    ["#salva-laboratorio", "#esito-laboratorio"],
    ["#salva-operatori", "#esito-operatori"],
    ["#salva-destinatari", "#esito-destinatari"],
  ]) {
    $(bottone).addEventListener("click", () => salvaImpostazioni(esitoSel, false));
  }

  $$(".rail__voce").forEach((voce) =>
    voce.addEventListener("click", () => {
      mostra(voce.dataset.schermata);
      if (voce.dataset.schermata === "sigillo") {
        caricaCoda();
        caricaInSospeso();
      }
    })
  );

  // Il lettore di codici a barre digita in fretta e chiude con Invio: qui
  // significa «passa al campo successivo», non «invia il modulo».
  $("#cf").addEventListener("keydown", (evento) => {
    if (evento.key === "Enter") {
      evento.preventDefault();
      $("#cognome").focus();
    }
  });

  // Scorciatoie da banco: le mani dell'operatore sono spesso occupate, e i
  // guanti rendono il mouse scomodo.
  document.addEventListener("keydown", (evento) => {
    // Invio su un pulsante deve attivare quel pulsante, non la scrittura RF.
    if (evento.target.closest("input, select, textarea, button, a, summary, [contenteditable='true']") || evento.ctrlKey || evento.altKey) return;
    if (evento.key === "Enter" && !$("#scrivi").disabled && schermataAttiva() === "accettazione") {
      evento.preventDefault();
      scrivi();
    }
    if (evento.key === "Escape") $("#interrompi").click();
    const scorciatoie = {
      1: "accettazione",
      2: "sigillo",
      3: "ricezione",
      4: "archivio",
      5: "registro",
      6: "impostazioni",
    };
    if (scorciatoie[evento.key]) mostra(scorciatoie[evento.key]);
  });

  try {
    stato.descrizione = await chiama("descrivi");
    stato.intervalloSorveglianza = stato.descrizione.watch_interval_ms || 900;
    // Se il diario dell'interfaccia è spento si smette di accodare: quello
    // della radio resta acceso comunque, sono due metà separate.
    if (stato.descrizione.diario?.interfaccia === false) Traccia.attiva = false;
    dipingiModalita(stato.descrizione.modalita_scrittura);
    stato.hardware = stato.descrizione.hardware || null;
    dipingiOperativita(stato.descrizione.operativita);
    dipingiHardware(stato.hardware);
    adattaAllHardware(stato.hardware);
    dipingiTestata();
    dipingiDestinatari();
    Scena.antennaAttiva(stato.descrizione.antenne.scrittura[0]);
    const lettura = stato.descrizione.antenne.lettura;
    if (lettura[0]) $("#volume-a1").textContent = lettura[0];
    if (lettura[1]) $("#volume-a2").textContent = lettura[1];
    riempiCodebook();
    preimpostaDate();
    costruisciAntenne();
    if (stato.descrizione.tema && stato.descrizione.tema !== "sistema" && !localStorage.getItem("tema")) {
      applicaTema(stato.descrizione.tema === "chiaro" ? "chiaro" : "scuro");
    }
    const ripresa = await chiama("riprendi_workflow");
    stato.accettazione = ripresa.accettazione;
    stato.spedizione = ripresa.spedizione;
    stato.distinta = ripresa.ricezione;
    await Giornata.avvia();
    dipingiAccettazione();
    dipingiSpedizione();
    ripristinaRicezione(stato.distinta);
    await caricaCoda();
    await caricaInSospeso();
    // Una scatola lasciata aperta da un riavvio si riprende da dove stava: il
    // contenuto è nell'archivio, e la sorveglianza riparte da quello.
    if (stato.spedizione?.shipment_id && stato.spedizione.stato === "open") {
      $("#riempimento-destinazione").textContent = stato.spedizione.destinazione || "—";
      $("#riempimento-scatola").textContent = stato.spedizione.shipment_id;
      $("#pannello-riempimento").classList.remove("pannello--nascosto");
      stato.riempimento = { dentro: [], anomalie: [], esclusi: [] };
      dipingiRiempimento(stato.riempimento);
    }
    aggiornaWorkflowBar();
  } catch (errore) {
    avvisa(`Impossibile leggere la configurazione: ${errore.message}`, "errore", 15000);
  }

  Scena.stato("fermo");
  ascoltaEventi();
  // Il fuoco sul codice fiscale fa partire il lettore di codici a barre senza
  // toccare niente. Sulla tavoletta farebbe salire la tastiera di sistema, che
  // copre meta' schermo per un campo che nessuno ha ancora chiesto di
  // compilare: li' si aspetta il tocco.
  if (!stato.accettazione?.accession_id && !tavoletta()) $("#cf").focus();
}

document.addEventListener("DOMContentLoaded", avvia);
