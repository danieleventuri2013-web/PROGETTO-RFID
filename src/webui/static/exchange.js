/* Invii cumulativi e distinte attese: nessun accesso alla posta fino a Invia. */
(() => {
  "use strict";
  let bozza = null;
  const stati = { prepared: "bozza pronta", sending: "invio in corso o interrotto: verificare la posta",
    smtp_accepted: "accettata dal server SMTP", send_unknown: "esito incerto: verificare la posta",
    failed: "invio non riuscito", manual_sent: "invio manuale confermato" };
  const campi = ["sender", "username", "smtp_host", "smtp_port", "smtp_mode", "password_env"];

  function nodo(tipo, testo) {
    const n = document.createElement(tipo);
    n.textContent = testo;
    return n;
  }
  function azione(id, fn) {
    $(id).addEventListener("click", async () => {
      const button = $(id);
      button.disabled = true;
      try { await fn(); }
      catch (e) { avvisa(e.message, "errore", 12000); }
      finally { button.disabled = false; }
    });
  }
  async function caricaConfig() {
    const cfg = await chiama("impostazioni_email");
    for (const campo of campi) $("#email-" + campo).value = cfg[campo];
    $("#email-enabled").checked = cfg.enabled;
    $("#email-config-nota").textContent = cfg.credenziale_presente ? "Credenziale disponibile al programma." : "Credenziale non impostata; bozza e allegati disponibili.";
  }
  async function caricaColli() {
    const data = await chiama("colli_email");
    const list = $("#email-colli");
    list.replaceChildren();
    for (const row of data.colli) {
      const label = document.createElement("label");
      const check = document.createElement("input");
      check.type = "checkbox";
      check.value = row.id;
      label.append(check, nodo("span", `Collo / spedizione ${row.id} — ${row.destinazione} — ${row.data}${row.item_count != null ? ` — ${row.item_count} contenitori` : " — da esportare"}`));
      list.append(label);
    }
    if (!data.colli.length) list.append(nodo("p", "Nessun collo chiuso disponibile."));
    const history = $("#email-storico");
    history.replaceChildren();
    for (const row of data.invii) {
      const button = nodo("button", `${row.subject} — ${stati[row.state] || row.state}`);
      button.type = "button";
      button.className = "bottone bottone--sobrio";
      button.addEventListener("click", async () => {
        try { mostraBozza(await chiama("dettaglio_email", { id: row.id })); }
        catch (e) { avvisa(e.message, "errore"); }
      });
      history.append(button);
    }
  }
  function mostraBozza(data) {
    bozza = data;
    $("#email-anteprima").hidden = false;
    const info = $("#email-intestazione");
    info.replaceChildren();
    for (const [k, v] of [["Da", data.sender], ["A", data.recipient], ["Oggetto", data.subject]]) {
      info.append(nodo("dt", k), nodo("dd", v));
    }
    $("#email-testo").textContent = data.body;
    $("#email-allegati").replaceChildren(...data.allegati.map(a => nodo("li", a.nome)));
    $("#email-esito").textContent = (stati[data.state] || data.state) + (data.last_error ? ": " + data.last_error : "");
    $("#email-invia").hidden = !["prepared", "failed"].includes(data.state);
    $("#email-manuale").hidden = ["smtp_accepted", "manual_sent"].includes(data.state);
  }
  async function scaricaInvio(formato) {
    if (!bozza) return;
    const file = await chiama("file_email", { id: bozza.id, formato });
    const bytes = Uint8Array.from(atob(file.contenuto_base64), c => c.charCodeAt(0));
    const url = URL.createObjectURL(new Blob([bytes], { type: file.tipo }));
    const link = document.createElement("a");
    link.href = url;
    link.download = file.nome;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function caricaDistinte() {
    const data = await chiama("distinte_attese");
    const list = $("#distinte-attese");
    list.replaceChildren();
    for (const row of data.distinte) {
      const box = row.box_epc ? `collo ${row.box_epc}` : "senza tag collo: selezione manuale";
      const status = row.state === "received" ? "ricezione completata" : "da verificare";
      const button = nodo("button", `Sede ${row.origin_lab_id} · spedizione ${row.origin_shipment_id} · ${row.item_count} contenitori · ${box} · ${status}`);
      button.className = "bottone bottone--sobrio";
      button.type = "button";
      button.addEventListener("click", async () => {
        try {
          ripristinaRicezione(await chiama("seleziona_distinta", { inbound_id: row.id }));
          aggiornaWorkflowBar();
        } catch (e) { avvisa(e.message, "errore"); }
      });
      list.append(button);
    }
    if (!data.distinte.length) list.append(nodo("p", "Nessuna distinta importata."));
    if (data.non_indicizzate.length) list.append(nodo("p", "Alcune distinte storiche non sono leggibili con le chiavi configurate."));
  }
  // L'esito già salvato resta valido anche se il rinnovo dell'elenco fallisce.
  window.aggiornaArchivioRicezione = async () => {
    try { await caricaDistinte(); }
    catch (e) { avvisa(`Archivio ricezioni non aggiornato: ${e.message}. Premi Aggiorna per riprovare.`, "errore"); }
  };
  window.importaDistinteMultiple = async (evento) => {
    const selected = [...(evento.target.files || [])];
    if (!selected.length) return;
    $("#errore-distinta").textContent = "";
    try {
      if (selected.length > 50 || selected.reduce((n, f) => n + f.size, 0) > 2 * 1024 * 1024) throw new Error("Importare al massimo 50 file e 2 MiB per volta.");
      const files = [];
      for (const file of selected) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = "";
        for (const byte of bytes) binary += String.fromCharCode(byte);
        files.push({ nome: file.name, contenuto_base64: btoa(binary) });
      }
      const result = await chiama("importa_distinte", { files });
      $("#importazione-esiti").replaceChildren(...result.risultati.map(r => nodo("li", `${r.nome}: ${r.esito}${r.errore ? " — " + r.errore : ""}`)));
      await caricaDistinte();
    } catch (e) { $("#errore-distinta").textContent = e.message; }
    finally { evento.target.value = ""; }
  };

  azione("#email-salva", async () => {
    const cfg = Object.fromEntries(campi.map(c => [c, $("#email-" + c).value]));
    cfg.username = cfg.username || cfg.sender;
    cfg.smtp_port = Number(cfg.smtp_port);
    cfg.enabled = $("#email-enabled").checked;
    await chiama("salva_email", cfg);
    await caricaConfig();
    avvisa("Casella salvata", "ok");
  });
  azione("#email-aggiorna", caricaColli);
  azione("#email-prepara", async () => {
    const ids = [...$("#email-colli").querySelectorAll("input:checked")].map(c => Number(c.value));
    mostraBozza(await chiama("prepara_invio_email", { shipment_ids: ids }));
    await caricaColli();
  });
  azione("#email-copia", async () => {
    if (!bozza) return;
    const text = `A: ${bozza.recipient}\nOggetto: ${bozza.subject}\n\n${bozza.body}`;
    try { await navigator.clipboard.writeText(text); avvisa("Destinatario, oggetto e testo copiati", "ok"); }
    catch { window.prompt("Copia destinatario, oggetto e testo:", text); }
  });
  azione("#email-eml", () => scaricaInvio("eml"));
  azione("#email-zip", () => scaricaInvio("zip"));
  azione("#email-invia", async () => {
    if (!bozza) return;
    const id = bozza.id;
    if (!await domanda("Invia email", `Inviare i ${bozza.allegati.length} allegati cifrati al destinatario mostrato nell'anteprima?`, "Invia")) return;
    try { mostraBozza(await chiama("invia_email", { id })); }
    finally { mostraBozza(await chiama("dettaglio_email", { id })); await caricaColli(); }
  });
  azione("#email-manuale", async () => {
    if (!bozza) return;
    const id = bozza.id;
    if (await domanda("Conferma invio manuale", "Hai inviato dalla webmail il messaggio con tutte le distinte allegate?", "Conferma invio eseguito")) {
      mostraBozza(await chiama("conferma_email_manuale", { id }));
      await caricaColli();
    }
  });
  azione("#aggiorna-distinte", caricaDistinte);
  azione("#riconosci-collo", async () => {
    $("#conferma-ricezione").disabled = true;
    const result = await chiama("riconosci_collo");
    ripristinaRicezione(result.distinta);
    dipingiRicezione(result);
    aggiornaWorkflowBar();
    await caricaDistinte();
  });
  $("#invii-email").addEventListener("toggle", () => {
    if ($("#invii-email").open) Promise.all([caricaConfig(), caricaColli()]).catch(e => avvisa(e.message, "errore"));
  });
  $$(".rail__voce").forEach(button => button.addEventListener("click", () => {
    if (button.dataset.schermata === "ricezione") caricaDistinte().catch(e => avvisa(e.message, "errore"));
  }));
})();
