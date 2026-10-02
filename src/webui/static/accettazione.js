/* Accettazione giornaliera: la coda vive nel database, la selezione nel browser. */
const Giornata = {
  avviata: false, vista: "elenco", selezionato: null, dettaglio: null,
  bozza: null, persona: null, sporco: false, sequenza: 0, selezioneRichiesta: 0,
  stati: {da_preparare: "Da preparare", da_scrivere: "Da scrivere", parziale: "Parziale", completato: "Completato", annullato: "Annullato"},
  avvertenze: {URGENT: [1, "Urgente"], FROZEN: [2, "Congelato"], BIOBANK: [4, "Biobanca"], INFECTIOUS: [8, "Rischio biologico"], DECALCIFIED: [16, "Decalcificato"], UNDER_VACUUM: [32, "Sotto vuoto"]},

  testo(tag, testo, classe = "") {
    const n = document.createElement(tag); n.textContent = testo; n.className = classe; return n;
  },
  bottone(testo, azione, primario = false) {
    const b = this.testo("button", testo, `bottone ${primario ? "bottone--primario" : "bottone--sobrio"}`);
    b.type = "button";
    b.addEventListener("click", async () => {
      b.disabled = true;
      try { await azione(); } catch (e) { avvisa(e.message, "errore", 9000); }
      finally { b.disabled = false; }
    });
    return b;
  },
  etichetta(stato_, errore = false) {
    return this.testo("span", errore ? "! Da verificare" : this.stati[stato_] || stato_, `giornata__stato giornata__stato--${errore ? "errore" : stato_}`);
  },
  async avvia() {
    this.avviata = true;
    $("#giorno-corpo").append($("#pannello-anagrafica"));
    this.organizzaModulo();
    $("#giorno-nuovo").addEventListener("click", () => this.apriModulo());
    for (const id of ["giorno-annulla-modulo", "giorno-torna"]) $("#" + id).addEventListener("click", () => this.torna().catch(e => avvisa(e.message, "errore")));
    $("#giorno-indietro").addEventListener("click", async () => {
      if (await this.puoLasciare()) { this.cambiaVista("elenco"); $("#giornata").classList.remove("giornata--dettaglio"); }
    });
    $("#giorno-aggiorna").addEventListener("click", () => this.aggiorna());
    $("#giorno-oggi").addEventListener("click", async () => {
      if (!await this.puoLasciare()) return;
      $("#giorno-data").value = ""; await this.aggiorna();
    });
    for (const id of ["giorno-data", "giorno-filtro"]) $("#" + id).addEventListener("change", () => this.aggiorna());
    $("#giorno-cerca").addEventListener("input", () => {
      clearTimeout(this.ricercaTimer); this.ricercaTimer = setTimeout(() => this.aggiorna(), 200);
    });
    $("#modulo-accettazione").addEventListener("input", () => { this.sporco = true; });
    $("#modulo-accettazione").addEventListener("change", () => { this.sporco = true; });
    for (const id of ["cognome", "nome", "cf"]) $("#" + id).addEventListener("input", () => {
      clearTimeout(this.omonimiTimer); this.omonimiTimer = setTimeout(() => this.cercaEsistenti(), 250);
    });
    window.addEventListener("beforeunload", e => { if (this.sporco) { e.preventDefault(); e.returnValue = ""; } });
    try {
      const memoria = JSON.parse(sessionStorage.getItem("accettazione-giorno") || "{}");
      $("#giorno-data").value = memoria.salvatoIl === new Date().toLocaleDateString("it-IT", {timeZone: "Europe/Rome"}) ? memoria.data || "" : "";
      $("#giorno-filtro").value = memoria.filtro || "tutti";
      this.selezionato = memoria.patient_id || null;
    } catch (_) { /* Una preferenza illeggibile non impedisce di aprire la coda. */ }
    this.cambiaVista("elenco");
    await this.aggiorna();
    if (this.selezionato) await this.apriPaziente(this.selezionato);
  },
  ricorda() {
    try { sessionStorage.setItem("accettazione-giorno", JSON.stringify({data: $("#giorno-data").value, filtro: $("#giorno-filtro").value, patient_id: this.selezionato, salvatoIl: new Date().toLocaleDateString("it-IT", {timeZone: "Europe/Rome"})})); }
    catch (_) { /* Lo stato persistente del lavoro resta comunque sul server. */ }
  },
  async puoLasciare() {
    if (this.salvataggio) { avvisa("Attendi il salvataggio della bozza.", "attesa"); return false; }
    if (stato.occupato) { avvisa("Attendi il risultato della scrittura in corso.", "attesa"); return false; }
    if (!this.sporco) return true;
    const ok = await domanda("Modifiche non salvate", "Chiudendo il modulo perderai le modifiche non salvate. I dati già registrati restano nell’elenco.", "Scarta modifiche");
    if (ok) this.sporco = false;
    return ok;
  },
  cambiaVista(vista) {
    this.vista = vista;
    $("#giornata").hidden = vista === "scrittura";
    $("#pannello-postazione").classList.toggle("pannello--nascosto", vista !== "scrittura");
    $("#pannello-anagrafica").classList.toggle("pannello--nascosto", vista !== "modulo");
    $("#giorno-dettaglio").hidden = vista === "modulo";
    $("#giornata").classList.toggle("giornata--dettaglio", vista === "modulo" || Boolean(this.selezionato));
    if (vista === "scrittura") this.aggiornaPostazione();
    else aggiornaComandoScrittura();
  },
  aggiornaPostazione() {
    if (this.vista !== "scrittura") return;
    dipingiSerie(); dipingiCartella(); aggiornaComandoScrittura();
    $("#nuova-accettazione").disabled = stato.occupato;
    $("#nuova-accettazione").textContent = "Torna alla giornata";
  },
  async aggiorna() {
    const seq = ++this.sequenza;
    $("#giorno-esito").textContent = "Aggiornamento…";
    try {
      const r = await chiama("accettazioni_giorno", {data: $("#giorno-data").value || null, query: $("#giorno-cerca").value, filtro: $("#giorno-filtro").value});
      if (seq !== this.sequenza) return;
      this.dati = r; $("#giorno-data").value = r.data; this.ricorda();
      const numeri = $("#giorno-contatori"); numeri.replaceChildren();
      for (const [k, label] of [["pazienti", "Pazienti"], ["campioni", "Campioni"], ["da_scrivere", "Tag da scrivere"], ["verificati", "Tag verificati"]]) {
        const card = this.testo("div", "", `giornata__numero giornata__numero--${k}`);
        card.append(this.testo("b", r.contatori[k]), this.testo("span", label)); numeri.append(card);
      }
      $("#giorno-quanti").textContent = r.pazienti.length;
      this.elenco($("#giorno-pazienti"), r.pazienti);
      this.elenco($("#giorno-arretrati"), r.arretrati);
      $("#giorno-arretrati-sezione").hidden = !r.arretrati.length;
      this.avvisaArretrati(r.arretrati, r.contatori);
      $("#giorno-esito").textContent = "Aggiornato · i contatori si riferiscono alla giornata, prima dei filtri.";
      if (this.selezionato && this.vista === "elenco") {
        const pid = this.selezionato;
        const d = await chiama("dettaglio_accettazione", {patient_id: pid});
        if (seq === this.sequenza && pid === this.selezionato && this.vista === "elenco") {
          this.dettaglio = d; this.dipingiDettaglio();
        }
      }
      if (!this.selezionato && this.vista !== "modulo") {
        const vuoto = $("#giorno-dettaglio"); vuoto.replaceChildren();
        vuoto.append(this.testo("div", "◎", "giornata__segnaposto"), this.testo("h2", "Ogni campione, al suo posto"), this.testo("p", "Seleziona un paziente per vedere campioni e tag, oppure aggiungi il primo paziente della giornata.", "tenue"));
      }
    } catch (e) { if (seq === this.sequenza) $("#giorno-esito").textContent = e.message; }
  },
  /* I tag rimasti indietro dai giorni scorsi vanno detti in testa alla
     giornata, non scoperti in fondo all'elenco: il banner porta dritto al
     primo paziente da completare. Il badge sulla voce di navigazione somma
     giornata e arretrati, cosi' il lavoro residuo si vede da ovunque. */
  avvisaArretrati(arretrati, contatori) {
    const residui = arretrati.reduce((n, p) => n + Math.max(0, (p.totale || 0) - (p.verificati || 0)), 0);
    const avviso = $("#giorno-arretrati-avviso");
    avviso.replaceChildren();
    if (!arretrati.length) avviso.hidden = true;
    else {
      avviso.hidden = false;
      avviso.append(
        this.testo("p", `Dai giorni precedenti: ${arretrati.length} ${plurale(arretrati.length, "paziente", "pazienti")} con ${residui} ${plurale(residui, "tag ancora da scrivere", "tag ancora da scrivere")}. I dati sono gia' registrati: basta appoggiare i tag.`),
        this.bottone("Apri il primo da completare", () => this.apriPaziente(arretrati[0].id), true)
      );
    }
    const badge = $("#rail-badge-accettazione");
    const daScrivere = (contatori?.da_scrivere || 0) + residui;
    badge.hidden = daScrivere === 0;
    badge.textContent = daScrivere;
    badge.title = `${daScrivere} ${plurale(daScrivere, "tag da scrivere", "tag da scrivere")} fra giornata e arretrati`;
  },
  elenco(nodo, persone) {
    nodo.replaceChildren();
    if (!persone.length) { nodo.append(this.testo("p", "Nessun paziente con questi criteri.", "giornata__vuoto tenue")); return; }
    for (const p of persone) {
      const b = this.testo("button", "", "giornata__paziente"); b.type = "button";
      b.dataset.patientId = p.id; b.setAttribute("aria-pressed", String(p.id === this.selezionato));
      const alto = this.testo("span", "", "giornata__paziente-testa");
      alto.append(this.testo("b", `${p.cognome} ${p.nome}`), this.etichetta(p.stato, p.errori > 0));
      b.append(alto, this.testo("small", `${p.codice} · ${p.campioni} campioni`, "tenue"));
      const avanzamento = this.testo("span", `${p.verificati}/${p.totale} tag verificati`, "giornata__avanzamento");
      const barra = document.createElement("progress"); barra.max = Math.max(1, p.totale); barra.value = p.verificati;
      barra.setAttribute("aria-label", `Tag verificati per ${p.cognome} ${p.nome}`);
      b.append(avanzamento, barra); b.addEventListener("click", () => this.apriPaziente(p.id)); nodo.append(b);
    }
  },
  async apriPaziente(id) {
    if (!await this.puoLasciare()) return;
    const req = ++this.selezioneRichiesta;
    try {
      const r = await chiama("dettaglio_accettazione", {patient_id: id});
      if (req !== this.selezioneRichiesta) return;
      this.selezionato = Number(id); this.dettaglio = r; this.cambiaVista("elenco"); this.ricorda();
      this.dipingiDettaglio();
      $$(".giornata__paziente").forEach(b => b.setAttribute("aria-pressed", String(Number(b.dataset.patientId) === this.selezionato)));
    } catch (e) { avvisa(e.message, "errore"); }
  },
  dipingiDettaglio() {
    const {paziente: p, accettazioni: casi} = this.dettaglio;
    const area = $("#giorno-dettaglio"); area.replaceChildren();
    const testa = this.testo("header", "", "giornata__dettaglio-testa");
    testa.append(this.testo("span", p.codice, "etichetta"), this.testo("h2", `${p.cognome} ${p.nome}`),
      this.testo("p", [p.codice_fiscale || "Codice fiscale non indicato", p.data_nascita ? `Nascita ${data(p.data_nascita)}` : ""].filter(Boolean).join(" · "), "tenue"));
    testa.append(this.bottone("+ Aggiungi campioni", () => {
      const aperta = casi.find(c => c.modificabile && c.giorno === this.dati.data);
      return this.apriModulo(p, aperta || null, true);
    }, true));
    area.append(testa);
    for (const c of casi) {
      const card = this.testo("section", "", "giornata__lotto");
      const top = this.testo("header", "", "giornata__lotto-testa");
      top.append(this.testo("h3", `Accettazione ${c.accession_id}`), this.etichetta(c.stato, c.errori > 0));
      card.append(top, this.testo("p", `${data(c.giorno)} · ${c.campioni} campioni · ${c.verificati}/${c.totale} tag verificati`, "tenue"));
      if (!c.reperti.length) card.append(this.testo("p", "Paziente registrato. Aggiungi i campioni quando sono disponibili.", "giornata__vuoto"));
      for (const [i, s] of c.reperti.entries()) {
        const gruppo = this.testo("div", "", "giornata__campione");
        gruppo.append(this.testo("h4", `${i + 1}. ${s.descrizione || "Campione senza descrizione"}`));
        const nomi = [["materiali", s.material_code], ["fissativi", s.fixative_code], ["sedi", s.site_code]].map(([k, v]) => stato.descrizione.codebook[k].find(x => x.codice === v)?.nome || "");
        gruppo.append(this.testo("small", nomi.join(" · "), "tenue"));
        const avvisi = Object.values(this.avvertenze).filter(([bit]) => s.flags & bit).map(([, nome]) => nome);
        if (avvisi.length) gruppo.append(this.testo("p", avvisi.join(" · "), "giornata__avvertenze"));
        for (const t of s.contenitori) {
          const riga = this.testo("div", "", "giornata__contenitore");
          const verificato = Boolean(t.provisioned_at) && t.state !== "planned" && t.state !== "voided";
          const label = t.state === "voided" ? "Annullato" : verificato ? "✓ Tag verificato" : t.last_write_error ? "! Scrittura da riprendere" : "○ Tag da scrivere";
          riga.append(this.testo("span", `Contenitore ${t.idx}/${t.total}`), this.testo("strong", label, verificato ? "giornata__ok" : ""));
          if (t.epc) riga.append(this.testo("small", `${verificato ? "EPC" : "EPC riservato"} ${t.epc}`, "hex"));
          if (t.last_write_error && !verificato) riga.append(this.testo("small", t.last_write_error, "giornata__errore"));
          if (verificato) riga.append(this.bottone("Etichetta", () => mostraEtichetta(t.id)));
          gruppo.append(riga);
        }
        card.append(gruppo);
      }
      const azioni = this.testo("div", "", "giornata__azioni");
      if (c.modificabile) {
        azioni.append(this.bottone(c.campioni ? "Modifica campioni" : "Prepara campioni", () => this.apriModulo(p, c, true)));
        azioni.append(this.bottone("Annulla bozza", async () => {
          if (await domanda("Annullare la bozza?", "La bozza resterà nello storico come annullata.", "Annulla bozza")) {
            await chiama("annulla_bozza", {accession_id: c.accession_id}); await this.aggiorna(); await this.apriPaziente(p.id);
          }
        }));
      }
      if (c.verificati < c.totale && c.stato !== "annullato") azioni.append(this.bottone(c.verificati || c.frozen_at ? "Riprendi scrittura" : "Scrivi tag", () => this.scrivi(c.accession_id), true));
      if (c.frozen_at && c.stato !== "annullato") card.append(this.testo("p", "Lotto avviato. I nuovi campioni avranno una nuova accettazione dello stesso paziente.", "tenue"));
      card.append(azioni); area.append(card);
    }
  },
  async scrivi(accession) {
    if (!await this.puoLasciare()) return;
    stato.accettazione = annotaResiduo(await chiama("seleziona_accettazione", {accession_id: accession}));
    this.azzeraScrittura(); this.cambiaVista("scrittura");
    Scena.stato("attesa", "Appoggia un solo tag e premi Scrivi");
    aggiornaWorkflowBar(); $("#giorno-torna").focus();
  },
  azzeraScrittura() {
    stato.ultimoEpc = ""; stato.ultimoContenitore = null;
    for (const id of ["riepilogo", "etichetta", "riscrivi", "salta"]) $("#" + id).hidden = true;
    $("#errore-scrittura").textContent = ""; $("#passi-scrittura").replaceChildren();
  },
  async torna() {
    if (!await this.puoLasciare()) return;
    if (this.vista === "scrittura") {
      try { await chiama("sospendi_accettazione"); }
      catch (e) {
        // Un'altra finestra può aver selezionato un lotto diverso. Si torna
        // comunque alla coda, senza modificare il lavoro di quella finestra.
        if (e.stato !== 400 && e.stato !== 409) throw e;
        avvisa(e.message, "attesa");
      }
      stato.accettazione = null;
      this.azzeraScrittura(); aggiornaWorkflowBar();
    }
    this.sporco = false; this.cambiaVista("elenco"); await this.aggiorna();
    if (this.selezionato) await this.apriPaziente(this.selezionato);
  },
  organizzaModulo() {
    const f = $("#modulo-accettazione"), grid = f.querySelector(".modulo__griglia");
    const titolo = $("#pannello-anagrafica h2"); titolo.id = "giorno-modulo-titolo";
    $("#pannello-anagrafica .pannello__testa p").textContent = "Salva il paziente e prepara i campioni. Potrai scrivere i tag quando vuoi.";
    const anag = document.createElement("div"); anag.className = "modulo__griglia";
    f.prepend(anag);
    for (const id of ["cognome", "nome", "cf"]) anag.append($("#" + id).closest("label"));
    $("#cf-nota").textContent = "Facoltativo. L’ID interno distingue anche i pazienti omonimi.";
    const suggerimenti = this.testo("div", "", "giornata__suggerimenti"); suggerimenti.id = "giorno-suggerimenti";
    f.prepend(anag, suggerimenti);
    const extra = document.createElement("details"); extra.className = "giornata__sezione";
    extra.append(this.testo("summary", "Dati aggiuntivi e prelievo"));
    const altri = this.testo("div", "", "modulo__griglia");
    for (const id of ["sesso", "data-nascita", "reparto", "medico", "data-prelievo", "ora-prelievo"]) altri.append($("#" + id).closest("label"));
    extra.append(altri); anag.after(suggerimenti, extra);
    const camp = document.createElement("details"); camp.className = "giornata__sezione"; camp.id = "giorno-campioni"; camp.open = true;
    camp.append(this.testo("summary", "Campioni di questa accettazione"));
    grid.before(camp); camp.append(grid, f.querySelector(".avvertenze"), $("#reperti-extra"), $("#aggiungi-reperto"));
    // Citologia è un materiale, non un bit previsto dallo schema delle avvertenze.
    camp.querySelector('input[value="CYTOLOGY"]')?.closest("label").remove();
    $("#modello-reperto").content.querySelector('[data-avvertenza][value="CYTOLOGY"]')?.closest("label").remove();
    for (const [flag, [, nome]] of Object.entries(this.avvertenze)) {
      for (const area of [camp.querySelector(".avvertenze"), $("#modello-reperto").content.querySelector(".avvertenze")]) {
        if (area.querySelector(`input[value="${flag}"]`)) continue;
        const label = this.testo("label", "", "interruttore"), box = document.createElement("input");
        box.type = "checkbox"; box.value = flag; box.dataset.avvertenza = "";
        label.append(box, this.testo("span", nome)); area.append(label);
      }
    }
    const numero = document.createElement("label"); numero.className = "campo";
    numero.innerHTML = '<span class="etichetta">Contenitori per questo campione</span><input id="giorno-contenitori" type="number" min="1" max="255" value="1" required>';
    grid.append(numero);
    const modello = numero.cloneNode(true); modello.querySelector("input").removeAttribute("id"); modello.querySelector("input").dataset.campo = "contenitori";
    $("#modello-reperto").content.querySelector(".modulo__griglia").append(modello);
    const toggle = document.createElement("label"); toggle.className = "interruttore giornata__includi";
    toggle.innerHTML = '<input type="checkbox" id="giorno-includi" checked><span>Prepara anche i campioni</span>';
    camp.before(toggle); $("#giorno-includi").addEventListener("change", () => this.mostraCampioni());
    $("#aggiungi-reperto").addEventListener("click", () => { this.sporco = true; });
    camp.addEventListener("click", e => { if (e.target.closest(".reperto__togli")) this.sporco = true; });
  },
  mostraCampioni() {
    const incluso = $("#giorno-includi").checked; $("#giorno-campioni").hidden = !incluso;
    $$("#giorno-campioni input, #giorno-campioni select, #giorno-campioni button").forEach(n => n.disabled = !incluso);
  },
  async apriModulo(persona = null, bozza = null, conCampioni = true) {
    if (!await this.puoLasciare()) return;
    ++this.selezioneRichiesta; // una vecchia richiesta di dettaglio non chiude il nuovo modulo
    this.persona = persona; this.bozza = bozza; this.sporco = false;
    const f = $("#modulo-accettazione"); f.reset(); $("#reperti-extra").replaceChildren();
    $("#giorno-suggerimenti").replaceChildren(); $("#errore-accettazione").textContent = "";
    $("#giorno-modulo-titolo").textContent = bozza ? `Modifica accettazione ${bozza.accession_id}` : persona ? "Nuovi campioni" : "Nuovo paziente";
    const valori = {...persona, ...bozza};
    for (const [id, key] of [["nome", "nome"], ["cognome", "cognome"], ["cf", "codice_fiscale"], ["sesso", "sesso"], ["data-nascita", "data_nascita"], ["reparto", "reparto"], ["medico", "medico"], ["data-prelievo", "data_prelievo"], ["ora-prelievo", "ora_prelievo"]]) {
      let v = valori[key] || (id === "sesso" ? "X" : "");
      if (id === "ora-prelievo" && v.length === 4) v = v.slice(0,2) + ":" + v.slice(2);
      $("#" + id).value = v;
    }
    if (!bozza) preimpostaDate();
    const reps = bozza?.reperti || [];
    for (const [i, r] of reps.entries()) {
      if (i > 0) aggiungiReperto();
      const container = i === 0 ? $("#giorno-campioni") : $$("#reperti-extra .reperto")[i - 1];
      const fields = i === 0 ? {descrizione: "#descrizione", material_code: "#materiale", fixative_code: "#fissativo", site_code: "#sede", contenitori: "#giorno-contenitori"} : Object.fromEntries(["descrizione", "material_code", "fixative_code", "site_code", "contenitori"].map(k => [k, `[data-campo="${k}"]`]));
      for (const [k, sel] of Object.entries(fields)) container.querySelector(sel).value = k === "contenitori" ? r.contenitori.filter(t => t.state !== "voided").length : r[k];
      for (const [flag, [bit]] of Object.entries(this.avvertenze)) {
        const box = container.querySelector(i === 0 ? `.avvertenze input[value="${flag}"]` : `[data-avvertenza][value="${flag}"]`);
        if (box) box.checked = Boolean(r.flags & bit);
      }
    }
    $("#giorno-includi").checked = conCampioni; this.mostraCampioni(); numeraReperti();
    if (persona) this.selezionato = persona.id;
    this.cambiaVista("modulo"); this.sporco = false;
    (persona ? $("#descrizione") : $("#cognome")).focus();
  },
  async cercaEsistenti() {
    if (this.persona || this.vista !== "modulo") return;
    const query = $("#cf").value.trim() || $("#cognome").value.trim() || $("#nome").value.trim();
    if (query.length < 2) { $("#giorno-suggerimenti").replaceChildren(); return; }
    const seq = this.ultimaRicerca = query;
    try {
      const r = await chiama("cerca_paziente", {query});
      if (this.ultimaRicerca !== seq || this.vista !== "modulo" || this.persona) return;
      const area = $("#giorno-suggerimenti"); area.replaceChildren();
      if (!r.risultati.length) return;
      area.append(this.testo("p", "Anagrafiche già presenti. Scegli solo se è la stessa persona; altrimenti continua con il nuovo paziente.", "tenue"));
      for (const p of r.risultati.slice(0, 6)) area.append(this.bottone(`P${String(p.id).padStart(6, "0")} · ${p.cognome} ${p.nome} · ${p.data_nascita || p.codice_fiscale || "dati aggiuntivi non indicati"}`, async () => {
        const d = await chiama("dettaglio_accettazione", {patient_id: p.id}); await this.apriModulo(d.paziente);
      }));
    } catch (_) { /* La ricerca resta un aiuto: la validazione definitiva è sul server. */ }
  },
  async salva() {
    if (this.salvataggio) return;
    const f = $("#modulo-accettazione"); if (!f.reportValidity()) return;
    this.salvataggio = true;
    $("#registra").disabled = true;
    try {
      const r0 = {...primoReperto(), contenitori: Number($("#giorno-contenitori").value), avvertenze: $$("#giorno-campioni > .avvertenze input:checked").map(n => n.value)};
      const altri = repertiAggiuntivi().map((r, i) => ({...r, contenitori: Number($$("#reperti-extra .reperto")[i].querySelector('[data-campo="contenitori"]').value)}));
      const dati = {patient_id: this.persona?.id, accession_id: this.bozza?.accession_id, edit_version: this.bozza?.edit_version,
        nome: $("#nome").value, cognome: $("#cognome").value, codice_fiscale: $("#cf").value, sesso: $("#sesso").value,
        data_nascita: $("#data-nascita").value, reparto: $("#reparto").value, medico: $("#medico").value,
        data_prelievo: $("#data-prelievo").value, ora_prelievo: $("#ora-prelievo").value,
        external_ref: this.bozza?.external_ref || "",
        reperti: $("#giorno-includi").checked ? [r0, ...altri] : []};
      const risposta = await chiama("salva_bozza", dati);
      this.salvataggio = false;
      this.sporco = false; this.selezionato = risposta.patient_id;
      if (!this.bozza) $("#giorno-data").value = "";
      await this.aggiorna(); await this.apriPaziente(risposta.patient_id);
      avvisa("Paziente e campioni salvati. I tag possono essere scritti quando vuoi.");
    } catch (e) { $("#errore-accettazione").textContent = e.message; }
    finally { this.salvataggio = false; $("#registra").disabled = false; }
  },
};
