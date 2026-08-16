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
  collegato: false,
  occupato: false,
  sorveglianza: null,
  intervalloSorveglianza: 900,
  /** EPC del contenitore appena scritto: finche' e' sul piatto non si avanza. */
  ultimoEpc: "",
  ultimoContenitore: null,
  etichettaCorrente: null,
  impostazioni: null,
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

/** «Manca 1 contenitore» invece di «Mancano 1 contenitori»: un messaggio che
 *  sgrammatica fa dubitare anche del numero che sta accanto. */
const plurale = (n, uno, molti) => (n === 1 ? uno : molti);

/* ==========================================================================
   Rete
   ========================================================================== */
async function chiama(operazione, dati = {}) {
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
  $("#sbarramento").hidden = false;
}

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
   Navigazione
   ========================================================================== */
function schermataAttiva() {
  return $(".schermata--attiva")?.id.replace("schermata-", "") || "";
}

function mostra(nome) {
  $$(".schermata").forEach((sezione) => {
    sezione.classList.toggle("schermata--attiva", sezione.id === `schermata-${nome}`);
  });
  $$(".rail__voce").forEach((voce) => {
    if (voce.dataset.schermata === nome) voce.setAttribute("aria-current", "page");
    else voce.removeAttribute("aria-current");
  });
  // La sorveglianza del piatto costa una lettura radio: si tiene accesa solo
  // dove serve davvero.
  if (nome === "accettazione") avviaSorveglianza();
  else fermaSorveglianza();
  // Le impostazioni si rileggono ogni volta: possono essere cambiate altrove,
  // e mostrare valori vecchi qui significherebbe farli riscrivere per sbaglio.
  if (nome === "impostazioni") caricaImpostazioni();
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
    stato.collegato = true;
    dipingiStatoLettore("pronto", "pronto");
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

async function registra(evento) {
  evento.preventDefault();
  $("#errore-accettazione").textContent = "";
  const avvertenze = $$(".avvertenze input:checked").map((casella) => casella.value);
  const dati = {
    codice_fiscale: $("#cf").value.trim().toUpperCase(),
    cognome: $("#cognome").value.trim(),
    nome: $("#nome").value.trim(),
    sesso: $("#sesso").value,
    reparto: $("#reparto").value.trim(),
    medico: $("#medico").value.trim(),
    descrizione: $("#descrizione").value.trim(),
    material_code: Number($("#materiale").value || 0),
    fixative_code: Number($("#fissativo").value || 0),
    site_code: Number($("#sede").value || 0),
    contenitori: Number($("#contenitori").value || 1),
    avvertenze,
  };
  try {
    stato.accettazione = await chiama("registra", dati);
    $("#pannello-anagrafica").classList.add("pannello--nascosto");
    $("#pannello-postazione").classList.remove("pannello--nascosto");
    dipingiAccettazione();
    avviaSorveglianza();
    $("#scrivi").focus();
  } catch (errore) {
    $("#errore-accettazione").textContent = errore.message;
    $("#cf").dataset.invalido = /fiscale/i.test(errore.message) ? "1" : "";
  }
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
    dipingiCampione(prossimo.campione);
  } else {
    Scena.contatore(dati.scritti, dati.totale);
    $("#cartella-etichetta").textContent = `${dati.scritti} / ${dati.totale}`;
    $("#scrivi").disabled = true;
    Scena.stato("verificato", "Accettazione completata — tutti i tag sono scritti");
    $("#vai-al-sigillo").focus();
  }
}

function dipingiAccettazione() {
  dipingiSerie();
  dipingiCartella();
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
  $("#passi-scrittura").innerHTML = "";
  dipingiCartella();
}

function dipingiCampione(campione) {
  if (!campione) return;
  $("#cartella-paziente").textContent = campione.paziente || "—";
  $("#cartella-cf").textContent = campione.codice_fiscale || "";

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
  if (stato.sorveglianza || !stato.collegato) return;
  if ($("#pannello-postazione").classList.contains("pannello--nascosto")) return;
  stato.sorveglianza = setInterval(guarda, stato.intervalloSorveglianza);
  guarda();
}

function fermaSorveglianza() {
  if (!stato.sorveglianza) return;
  clearInterval(stato.sorveglianza);
  stato.sorveglianza = null;
}

async function guarda() {
  if (stato.occupato) return;
  try {
    const esito = await chiama("sorveglia");
    if (stato.occupato) return;

    // Finche' sul piatto c'e' ancora il contenitore appena scritto, si chiede
    // di toglierlo. E' la lettura a dire che e' andato via, non un cronometro:
    // avanzare da soli significherebbe dare per fatto un gesto dell'operatore.
    const ancoraLui = stato.ultimoEpc && esito.epcs.includes(stato.ultimoEpc);
    if (ancoraLui) {
      Scena.stato("rimuovi");
      $("#scrivi").disabled = true;
      return;
    }
    if (stato.ultimoEpc) avanza();

    if (esito.stato === "vuoto") {
      Scena.stato("attesa");
      $("#scrivi").disabled = true;
    } else if (esito.stato === "pronto") {
      Scena.stato("rilevato");
      $("#scrivi").disabled = !stato.accettazione?.prossimo;
    } else if (esito.stato === "troppi") {
      Scena.stato("troppi", `Sull'antenna ci sono ${esito.epcs.length} contenitori: lasciane uno solo`);
      $("#scrivi").disabled = true;
    } else {
      Scena.stato("attesa", esito.messaggio || "Lettura non riuscita");
      $("#scrivi").disabled = true;
    }
  } catch (errore) {
    if (!errore.occupato) {
      fermaSorveglianza();
      dipingiStatoLettore("errore", "errore");
    }
  }
}

/* -- Scrittura ------------------------------------------------------------ */
async function scrivi() {
  if (!stato.accettazione) return;

  // La conferma del numero si chiede alla PRIMA scrittura, non alla
  // registrazione: e' qui che il totale diventa irreversibile, ed e' qui che
  // l'operatore ha i campioni in mano invece della tastiera.
  if (!stato.accettazione.conteggio_confermato) {
    const totale = stato.accettazione.totale;
    const conferma = await domanda(
      "Conferma il numero di contenitori",
      `Stai per scrivere <strong>${totale}</strong> ${totale === 1 ? "contenitore" : "contenitori"} per questo paziente.
       Il numero finisce dentro ogni tag e dopo la prima scrittura non si corregge:
       si possono solo annullare i tag già scritti.
       <br><br>Hai contato i contenitori fisici?`,
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
  $("#passi-scrittura").innerHTML = "";
  Scena.stato("scrittura");
  dipingiStatoLettore("scrittura", "attivo");

  try {
    stato.accettazione = await chiama("scrivi");
    const esito = stato.accettazione.scrittura;
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
    }
    dipingiSerie();
    avviaSorveglianza();
  } catch (errore) {
    Scena.stato("errore", errore.message);
    $("#errore-scrittura").textContent = errore.message;
    avviaSorveglianza();
  } finally {
    stato.occupato = false;
    dipingiStatoLettore(stato.collegato ? "pronto" : "non collegato", stato.collegato ? "pronto" : "fermo");
  }
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
async function cambiaConteggio() {
  const attuale = stato.accettazione?.totale ?? 1;
  const nuovo = prompt(
    "Quanti contenitori ci sono davvero?\n\n" +
      "I tag già scritti conservano il vecchio totale: verranno elencati qui sotto.",
    String(attuale)
  );
  if (nuovo === null) return;
  try {
    stato.accettazione = await chiama("correggi_conteggio", { totale: Number(nuovo) });
    dipingiAccettazione();
    const superati = stato.accettazione.tag_con_totale_superato || [];
    if (superati.length) {
      avvisa(
        `Attenzione: ${superati.length} ${plurale(superati.length, "tag già scritto riporta", "tag già scritti riportano")} il vecchio totale (${superati.join(", ")})`,
        "attesa",
        12000
      );
    } else {
      avvisa(
        `Ora ${plurale(stato.accettazione.totale, "il contenitore è", "i contenitori sono")} ${stato.accettazione.totale}`,
        "ok"
      );
    }
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function annullaContenitore() {
  const prossimo = stato.accettazione?.prossimo;
  if (!prossimo) return;
  const motivo = prompt(
    `Annulli il contenitore ${prossimo.index}/${prossimo.total}?\n\n` +
      "Ne verrà creato uno sostitutivo con un tag nuovo. Scrivi il motivo:",
    "tag non funzionante"
  );
  if (motivo === null) return;
  try {
    stato.accettazione = await chiama("annulla_contenitore", {
      container_id: prossimo.container_id,
      motivo,
      tag_guasto: /tag/i.test(motivo),
    });
    $("#salta").hidden = true;
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
    $("#passi-scrittura").innerHTML = "";
    $("#modulo-accettazione").reset();
    $("#cf").focus();

    const rimasti = esitoAnnullo.gia_scritti.length;
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
async function preparaSpedizione() {
  $("#errore-spedizione").textContent = "";
  try {
    stato.spedizione = await chiama("prepara_spedizione", {
      destinazione: $("#destinazione").value.trim(),
    });
    $("#pannello-sigillo").classList.remove("pannello--nascosto");
    disponiPuntiIn("#volume-punti", stato.spedizione.attesi);
    aggiornaVerdetto(0, stato.spedizione.attesi, "attesa");
    $("#verdetto-esito").textContent =
      `${stato.spedizione.attesi} contenitori pronti a partire. Chiudi la scatola e certifica.`;
    $("#sigilla").focus();
  } catch (errore) {
    $("#errore-spedizione").textContent = errore.message;
  }
}

/** Accende i punti trovati.
 *
 *  I punti sono anonimi di proposito: non si sa dove stia fisicamente ogni
 *  contenitore dentro la scatola, e disegnarlo in un posto preciso
 *  suggerirebbe una precisione che la radio non dà. Contano quanti sono. */
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
  if (!conferma) return;

  stato.occupato = true;
  fermaSorveglianza();
  $("#sigilla").disabled = true;
  $("#interrompi").hidden = false;
  $("#mancanti").hidden = true;
  $("#esporta").disabled = true;
  dipingiStatoLettore("lettura del volume", "attivo");
  aggiornaVerdetto(0, stato.spedizione.attesi, "lettura");
  $("#verdetto-esito").textContent = "Lettura in corso…";

  try {
    const esito = await chiama("sigilla");
    stato.spedizione = esito;
    dipingiSigillo(esito);
  } catch (errore) {
    $("#verdetto-esito").textContent = errore.message;
    $("#verdetto").dataset.esito = "incompleto";
    $("#volume").dataset.stato = "incompleto";
  } finally {
    stato.occupato = false;
    $("#sigilla").disabled = false;
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

  // La distinta si esporta solo se il sigillo e' completo: mandarne una che
  // dichiara contenitori non verificati sposterebbe il problema a destinazione.
  $("#esporta").disabled = !completo;
  if (completo) avvisa("Contenuto certificato: puoi esportare la distinta", "ok", 8000);
  else
    avvisa(
      `${quanti} ${plurale(quanti, "contenitore non trovato", "contenitori non trovati")}: ` +
        "riapri e controlla",
      "errore",
      12000
    );
}

async function esportaDistinta() {
  try {
    await scarica("/api/distinta", "distinta.rfidman");
    $("#prepara-email").disabled = false;
    avvisa("Distinta cifrata scaricata: inviala al laboratorio destinatario", "ok", 9000);
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
  $("#leggi-volume").disabled = true;
  $("#volume-arrivo").dataset.stato = "lettura";
  $("#verdetto-arrivo").dataset.esito = "lettura";
  dipingiStatoLettore("lettura del volume", "attivo");
  try {
    const esito = await chiama("leggi_volume");
    dipingiRicezione(esito);
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

async function misuraAntenna() {
  const antenna = Number($("#antenna-diagnosi").value || 1);
  $("#misura-antenna").disabled = true;
  $("#rl-nota").textContent = "Misura in corso: il modulo spazza la banda, può volerci mezzo minuto.";
  try {
    const esito = await chiama("diagnostica_antenna", { antenna });
    disegnaReturnLoss(esito.data);
  } catch (errore) {
    $("#rl-nota").textContent = errore.message;
    $("#verdetto-vswr").hidden = true;
  } finally {
    $("#misura-antenna").disabled = false;
  }
}

function disegnaReturnLoss(dati) {
  const misure = (dati.measurements || []).slice().sort((a, b) => a.frequency_khz - b.frequency_khz);
  const soglia = dati.threshold ?? 7;
  if (!misure.length) {
    $("#rl-nota").textContent = "Il modulo non ha restituito misure.";
    return;
  }

  // Le etichette degli assi stanno FUORI dall'area di disegno: dentro
  // finirebbero sopra la curva proprio dove la curva conta di piu', cioe' al
  // bordo basso della banda.
  const L = 60, R = 620, T = 44, B = 250;
  const fMin = Math.min(...misure.map((m) => m.frequency_khz));
  const fMax = Math.max(...misure.map((m) => m.frequency_khz));
  const vMax = Math.max(soglia * 1.3, ...misure.map((m) => m.vswr));
  const x = (khz) => L + ((khz - fMin) / Math.max(1, fMax - fMin)) * (R - L);
  const y = (vswr) => B - ((vswr - 1) / Math.max(0.001, vMax - 1)) * (B - T);

  // La banda ETSI, se rientra nell'intervallo misurato: è dove il sistema deve
  // funzionare per legge, e il resto della curva è contesto.
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

  const griglia = $("#rl-griglia");
  griglia.innerHTML = "";
  const assi = $("#rl-assi");
  assi.innerHTML = "";
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
    // Centrata sulla banda, ma trattenuta dentro il grafico: quando la banda
    // sta sul bordo la scritta uscirebbe dal disegno.
    const centro = Math.min(R - 34, Math.max(L + 34, (x(b0) + x(b1)) / 2));
    assi.append(testoSvg(centro, T - 12, "banda ETSI", "middle"));
  }

  $("#rl-soglia").setAttribute("d", `M${L} ${y(soglia)} H${R}`);
  $("#rl-curva").setAttribute(
    "d",
    misure.map((m, i) => `${i ? "L" : "M"}${x(m.frequency_khz)} ${y(m.vswr)}`).join(" ")
  );

  const punti = $("#rl-punti");
  punti.innerHTML = "";
  for (const misura of misure) {
    const punto = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    punto.setAttribute("cx", x(misura.frequency_khz));
    punto.setAttribute("cy", y(misura.vswr));
    punto.setAttribute("r", 3);
    if (!misura.ok) punto.dataset.oltre = "1";
    punti.append(punto);
  }

  // Il verdetto che conta è quello **dentro la banda EU**: un'antenna ottima a
  // 915 MHz e disadattata a 866 non serve a niente qui.
  const inBanda = misure.filter(
    (m) => m.frequency_khz >= BANDA_EU_KHZ[0] && m.frequency_khz <= BANDA_EU_KHZ[1]
  );
  const riferimento = inBanda.length ? inBanda : misure;
  const peggiore = Math.max(...riferimento.map((m) => m.vswr));
  const pastiglia = $("#verdetto-vswr");
  pastiglia.hidden = false;
  pastiglia.className = `pastiglia ${peggiore < soglia ? "pastiglia--ok" : "pastiglia--allarme"}`;
  pastiglia.textContent = `VSWR peggiore ${peggiore.toFixed(1)}`;
  $("#rl-nota").textContent = inBanda.length
    ? `Nella banda ETSI 865–868 MHz il VSWR peggiore è ${peggiore.toFixed(2)} (soglia ${soglia}). ` +
      `Return loss minimo ${Math.min(...riferimento.map((m) => m.return_loss_db)).toFixed(1)} dB.`
    : `La misura non copre la banda ETSI: verdetto sull'intero intervallo, VSWR peggiore ${peggiore.toFixed(2)}.`;
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
async function profila() {
  $("#profila").disabled = true;
  $("#esito-profilo").textContent = "Misura in corso…";
  try {
    const esito = await chiama("profila_tag");
    const profilo = esito.profilo;
    $("#esito-profilo").textContent = profilo.suitable ? "Tag adatto." : "Tag non adatto.";

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
    avvisa(esito.riassunto, profilo.suitable ? "ok" : "attesa", 12000);
  } catch (errore) {
    $("#esito-profilo").textContent = errore.message;
  } finally {
    $("#profila").disabled = false;
  }
}

/* -- Campagna ------------------------------------------------------------- */
async function rilevaControllo(posizione) {
  try {
    const esito = await chiama("rileva_controllo", { posizione });
    $("#conta-dentro").textContent = esito.dentro;
    $("#conta-fuori").textContent = esito.fuori;
    // Zero tag di controllo fuori non è «niente da segnalare»: è una campagna
    // che sceglierà la potenza massima, cioè quella che legge il tavolo accanto.
    $("#conta-fuori").className = esito.fuori ? "pastiglia pastiglia--ok" : "pastiglia pastiglia--attesa";
    $("#conta-dentro").className = esito.dentro ? "pastiglia pastiglia--ok" : "pastiglia";
    $("#avvia-campagna").disabled = esito.dentro === 0;
    avvisa(
      `${esito.epcs.length} ${plurale(esito.epcs.length, "tag rilevato", "tag rilevati")} ${posizione}`,
      "ok"
    );
    if (posizione === "dentro" && esito.fuori === 0) {
      avvisa(
        "Senza tag di controllo fuori dal contenitore la campagna sceglierà sempre la potenza massima",
        "attesa",
        12000
      );
    }
  } catch (errore) {
    avvisa(errore.message, "errore", 9000);
  }
}

async function avviaCampagna() {
  $("#avvia-campagna").disabled = true;
  $("#avanzamento-campagna").hidden = false;
  $("#consigliata").hidden = true;
  try {
    const report = await chiama("campagna", { cicli: 10, potenze_dbm: [15, 20, 25, 30] });
    dipingiCampagna(report);
  } catch (errore) {
    avvisa(errore.message, "errore", 12000);
  } finally {
    $("#avvia-campagna").disabled = false;
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
async function cercaPaziente() {
  const query = $("#cerca-paziente").value.trim();
  if (!query) {
    esito("#esito-ricerca", "Scrivi cosa cercare.", "");
    return;
  }
  try {
    const risposta = await chiama("cerca_paziente", { query });
    dipingiRisultati(risposta.risultati);
    esito(
      "#esito-ricerca",
      risposta.risultati.length
        ? `${risposta.risultati.length} ${plurale(risposta.risultati.length, "paziente", "pazienti")}`
        : "Nessun paziente trovato.",
      risposta.risultati.length ? "ok" : ""
    );
  } catch (errore) {
    esito("#esito-ricerca", errore.message, "errore");
  }
}

function dipingiRisultati(righe) {
  const corpo = $("#tabella-pazienti").querySelector("tbody");
  corpo.innerHTML = "";
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
  if (!righe.length) $("#pannello-storico").classList.add("pannello--nascosto");
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
  const tdAttivo = document.createElement("td");
  const attivo = document.createElement("input");
  attivo.type = "checkbox";
  attivo.dataset.campo = "attivo";
  attivo.checked = valori.attivo !== false;
  tdAttivo.append(attivo);
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
  await aggiornaPorte();
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
    const versione = risposta.avvio?.data?.firmware_info ?? risposta.avvio?.data?.version ?? "";
    stato.collegato = true;
    $("#collega").textContent = "Scollega";
    dipingiStatoLettore("pronto", "pronto");
    esito(
      "#esito-collegamento",
      versione
        ? `Il lettore risponde: ${JSON.stringify(versione)}`
        : "Il lettore risponde.",
      "ok"
    );
    avvisa("Lettore collegato con i nuovi parametri", "ok");
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
  flusso.addEventListener("sigillo", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase !== "passata") return;
    aggiornaVerdetto(dati.trovati, dati.attesi, "lettura");
    $("#verdetto-esito").textContent = `Passata ${dati.passata} — lettura in corso…`;
  });

  flusso.addEventListener("ricezione", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
    if (dati.fase === "lettura") $("#ricezione-esito").textContent = "Lettura della scatola in corso…";
  });

  // La campagna dura minuti: senza avanzamento sembrerebbe bloccata.
  flusso.addEventListener("campagna", (messaggio) => {
    const dati = JSON.parse(messaggio.data).data;
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
  applicaTema(localStorage.getItem("tema") || "sistema");
  Scena.collega($("#scena"));

  // Indirizzo senza token: inutile provare, si dice subito.
  if (!TOKEN) {
    sbarra();
    return;
  }

  $("#tema").addEventListener("click", alternaTema);
  $("#collega").addEventListener("click", () => (stato.collegato ? scollega() : collega()));
  $("#modulo-accettazione").addEventListener("submit", registra);
  $("#scrivi").addEventListener("click", scrivi);
  $("#salta").addEventListener("click", annullaContenitore);
  $("#etichetta").addEventListener("click", () => mostraEtichetta(stato.ultimoContenitore));
  $("#dialogo-etichetta").addEventListener("close", stampaEtichetta);
  $("#cambia-conteggio").addEventListener("click", cambiaConteggio);
  $("#annulla-accettazione").addEventListener("click", annullaAccettazione);
  $("#cerca").addEventListener("click", cercaPaziente);
  $("#cerca-paziente").addEventListener("keydown", (evento) => {
    if (evento.key === "Enter") {
      evento.preventDefault();
      cercaPaziente();
    }
  });
  $("#vai-al-sigillo").addEventListener("click", () => mostra("sigillo"));
  $("#prepara").addEventListener("click", preparaSpedizione);
  $("#file-distinta").addEventListener("change", apriDistinta);
  $("#leggi-volume").addEventListener("click", leggiVolume);
  $("#applica-potenze").addEventListener("click", applicaPotenze);
  $("#applica-gen2").addEventListener("click", applicaGen2);
  $("#misura-antenna").addEventListener("click", misuraAntenna);
  $("#profila").addEventListener("click", profila);
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
  $$(".ricetta [data-posizione]").forEach((bottone) =>
    bottone.addEventListener("click", () => rilevaControllo(bottone.dataset.posizione))
  );
  $("#sigilla").addEventListener("click", sigilla);
  $("#esporta").addEventListener("click", esportaDistinta);
  $("#interrompi").addEventListener("click", () =>
    fetch("/api/interrompi", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-RFID-Token": TOKEN },
      body: "{}",
    })
  );
  $("#operatore").addEventListener("change", async (evento) => {
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
    voce.addEventListener("click", () => mostra(voce.dataset.schermata))
  );

  $$(".conteggio__passo").forEach((bottone) =>
    bottone.addEventListener("click", () => {
      const campo = $("#contenitori");
      const nuovo = Number(campo.value || 1) + Number(bottone.dataset.delta);
      campo.value = Math.min(255, Math.max(1, nuovo));
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
    if (evento.target.matches("input, select, textarea") || evento.ctrlKey || evento.altKey) return;
    if (evento.key === "Enter" && !$("#scrivi").disabled && schermataAttiva() === "accettazione") {
      evento.preventDefault();
      scrivi();
    }
    if (evento.key === "Escape") $("#interrompi").click();
    const scorciatoie = {
      1: "accettazione",
      2: "sigillo",
      3: "ricezione",
      4: "strumenti",
      5: "archivio",
      6: "registro",
      7: "impostazioni",
    };
    if (scorciatoie[evento.key]) mostra(scorciatoie[evento.key]);
  });

  try {
    stato.descrizione = await chiama("descrivi");
    stato.intervalloSorveglianza = stato.descrizione.watch_interval_ms || 900;
    dipingiTestata();
    dipingiDestinatari();
    Scena.antennaAttiva(stato.descrizione.antenne.scrittura[0]);
    const lettura = stato.descrizione.antenne.lettura;
    if (lettura[0]) $("#volume-a1").textContent = lettura[0];
    if (lettura[1]) $("#volume-a2").textContent = lettura[1];
    riempiCodebook();
    costruisciAntenne();
    if (stato.descrizione.tema && stato.descrizione.tema !== "sistema" && !localStorage.getItem("tema")) {
      applicaTema(stato.descrizione.tema === "chiaro" ? "chiaro" : "scuro");
    }
  } catch (errore) {
    avvisa(`Impossibile leggere la configurazione: ${errore.message}`, "errore", 15000);
  }

  Scena.stato("fermo");
  ascoltaEventi();
  $("#cf").focus();
}

document.addEventListener("DOMContentLoaded", avvia);
