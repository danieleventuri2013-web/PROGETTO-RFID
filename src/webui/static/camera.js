/* Un solo proprietario della webcam, anche se il permesso arriva in ritardo. */
window.WebcamLocale = (() => {
  let corrente = null, generazione = 0;
  function ferma(nome) {
    if (corrente?.nome !== nome) return;
    generazione++;
    corrente.stream?.getTracks().forEach(t => t.stop());
    corrente = null;
  }
  async function apri(nome, constraints, onStop) {
    if (corrente) {
      const prima = corrente;
      ferma(prima.nome); prima.onStop();
    }
    const turno = ++generazione;
    corrente = { nome, onStop, stream: null };
    if (!navigator.mediaDevices?.getUserMedia) throw new Error("Apri la postazione con localhost oppure HTTPS per usare la webcam.");
    const stream = await navigator.mediaDevices.getUserMedia(constraints);
    if (!corrente || turno !== generazione) {
      stream.getTracks().forEach(t => t.stop());
      throw new DOMException("Apertura annullata", "AbortError");
    }
    corrente.stream = stream;
    return stream;
  }
  return { apri, ferma };
})();
