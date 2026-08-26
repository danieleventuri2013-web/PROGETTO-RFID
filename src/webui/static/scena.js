/* ==========================================================================
   La scena del banco.

   Disegna la postazione di scrittura vista di lato e la fa cambiare stato.
   Il punto fermo: **ogni stato arriva da un evento vero**. Non c'e' nessun
   timer che finge un'animazione di scrittura, perche' un'animazione che dura
   tre secondi «perche' e' bella» racconta all'operatore qualcosa che magari non
   e' successo — e qui la parola dell'interfaccia e' l'unica prova che il
   campione e' stato scritto.
   ========================================================================== */

const Scena = (() => {
  const STATI = {
    attesa: {
      istruzione: "Appoggia il contenitore sull'antenna",
    },
    rilevato: {
      istruzione: "Contenitore rilevato — premi «Scrivi il tag»",
    },
    troppi: {
      istruzione: "Più di un contenitore sull'antenna: lasciane uno solo",
    },
    scrittura: {
      istruzione: "Scrittura in corso — non spostare il contenitore",
    },
    verificato: {
      istruzione: "Scritto e riletto dal chip",
    },
    rimuovi: {
      istruzione: "Togli il contenitore e appoggia il prossimo",
    },
    errore: {
      istruzione: "Scrittura non riuscita",
    },
    fermo: {
      istruzione: "Collega il lettore per iniziare",
    },
  };

  let radice = null;
  let passo = null;
  let istruzione = null;
  let antenna = null;
  let statoCorrente = "attesa";

  function collega(elemento) {
    radice = elemento;
    passo = elemento.querySelector("#scena-passo");
    istruzione = elemento.querySelector("#scena-istruzione");
    antenna = elemento.querySelector("#scena-antenna");
  }

  /** Chi vuole sapere quando la scena cambia (il diario). Uno solo, e
   *  facoltativo: la scena non deve dipendere da chi la guarda. */
  let osservatore = null;

  function osserva(funzione) {
    osservatore = funzione;
  }

  /** Cambia stato. `testo` sovrascrive l'istruzione quando serve dire di più
   *  (per esempio il motivo preciso di un errore). */
  function stato(nome, testo) {
    if (!radice) return;
    const voce = STATI[nome] || STATI.attesa;
    statoCorrente = nome;
    radice.dataset.stato = nome === "troppi" ? "errore" : nome;
    istruzione.textContent = testo || voce.istruzione;
    // Dopo aver dipinto, non prima: quello che si annota è quello che
    // l'operatore ha davvero davanti agli occhi.
    try {
      osservatore?.(nome, istruzione.textContent);
    } catch (errore) {
      /* chi guarda non può rompere la scena */
    }
  }

  function contatore(indice, totale) {
    if (!passo) return;
    passo.textContent = indice && totale ? `${indice} di ${totale}` : "— di —";
  }

  function antennaAttiva(id) {
    if (antenna) antenna.textContent = id ?? "—";
  }

  return {
    collega,
    stato,
    contatore,
    antennaAttiva,
    osserva,
    get corrente() {
      return statoCorrente;
    },
  };
})();
