"""Un codice QR disegnato invece che scaricato.

La distinta stampata porta un QR perche' i due laboratori **non condividono
nessun database**: quello che non viaggia sul foglio, a destinazione non c'e'.
Il foglio resta la copia autorevole e leggibile a occhio; il QR e' la stessa
tabella in forma che si rilegge con un lettore invece che a mano.

Nessuna dipendenza, per la stessa ragione di `webui/icone.py`: il laboratorio e'
isolato dalla rete e questo programma non installa niente per disegnare dei
quadratini. Il disegno e' il codice, e resta rileggibile fra due anni.

Cosa c'e' dentro, in ordine:

1. **segmentazione** — il testo diventa bit, in modalita' numerica,
   alfanumerica o byte a seconda di cosa contiene. La modalita' alfanumerica
   usa 5,5 bit per carattere invece di 8: e' la ragione per cui la distinta
   viene codificata in Base45 (`lims.base45`) prima di arrivare qui;
2. **correzione d'errore** — Reed-Solomon su GF(256), che e' cio' che permette
   di leggere un codice con sopra un'impronta o una piega;
3. **disposizione** — schemi di ricerca, allineamento, sincronismo, e poi i
   dati a zigzag da destra in basso;
4. **maschera** — otto disegni possibili, si sceglie quello che rende il
   simbolo piu' facile da leggere secondo le penalita' della norma.

Le tabelle sono quelle di ISO/IEC 18004. Il test verifica le capienze
pubblicate (2.953 byte a versione 40-L, 4.296 caratteri alfanumerici) e rilegge
i simboli prodotti smontandoli: se la disposizione o la maschera fossero
sbagliate, non tornerebbe il testo di partenza.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CodiceQR",
    "Correzione",
    "QRTroppoGrande",
    "capacita",
    "codifica",
    "svg",
]


class QRTroppoGrande(ValueError):
    """Il contenuto non entra nemmeno nella versione 40."""


class Correzione(Enum):
    """Quanta ridondanza mettere: piu' alta, meno capienza, piu' robustezza.

    Il valore e' `(bit di formato, indice nelle tabelle)`. I bit di formato non
    seguono l'ordine di robustezza — e' cosi' nella norma, e va copiato com'e'.
    """

    L = (1, 0)   # ~7% recuperabile
    M = (0, 1)   # ~15%
    Q = (3, 2)   # ~25%
    H = (2, 3)   # ~30%

    @property
    def bit_formato(self) -> int:
        return self.value[0]

    @property
    def indice(self) -> int:
        return self.value[1]


# --------------------------------------------------------------------------
# Tabelle della norma
# --------------------------------------------------------------------------
#: Codeword di correzione per blocco, per versione 1..40. Indice 0 non usato.
_ECC_PER_BLOCCO = (
    # L
    (0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28,
     28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    # M
    (0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26,
     26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    # Q
    (0, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26,
     30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    # H
    (0, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26,
     28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)

#: Numero di blocchi di correzione, per versione 1..40.
_BLOCCHI = (
    # L
    (0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7,
     8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    # M
    (0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14,
     16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    # Q
    (0, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21,
     20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    # H
    (0, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25,
     25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)

#: I 45 caratteri della modalita' alfanumerica, nell'ordine che ne da' il valore.
ALFANUMERICI = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"
_VALORE_ALFANUMERICO = {c: i for i, c in enumerate(ALFANUMERICI)}

_MODO_NUMERICO = 1
_MODO_ALFANUMERICO = 2
_MODO_BYTE = 4

#: Bit dell'indicatore di lunghezza, per modalita' e gruppo di versioni
#: (1-9, 10-26, 27-40).
_BIT_LUNGHEZZA = {
    _MODO_NUMERICO: (10, 12, 14),
    _MODO_ALFANUMERICO: (9, 11, 13),
    _MODO_BYTE: (8, 16, 16),
}

#: Byte di riempimento alternati, come prescrive la norma.
_RIEMPIMENTO = (0xEC, 0x11)


def _gruppo_versione(versione: int) -> int:
    if versione <= 9:
        return 0
    return 1 if versione <= 26 else 2


def _moduli_dati_grezzi(versione: int) -> int:
    """Moduli disponibili ai dati, tolti gli schemi di servizio."""
    risultato = (16 * versione + 128) * versione + 64
    if versione >= 2:
        allineamenti = versione // 7 + 2
        risultato -= (25 * allineamenti - 10) * allineamenti - 55
        if versione >= 7:
            risultato -= 36
    return risultato


def _codeword_dati(versione: int, correzione: Correzione) -> int:
    """Quanti byte di contenuto stanno in questa versione a questa correzione."""
    indice = correzione.indice
    return (
        _moduli_dati_grezzi(versione) // 8
        - _ECC_PER_BLOCCO[indice][versione] * _BLOCCHI[indice][versione]
    )


def capacita(versione: int, correzione: Correzione, modo: int = _MODO_BYTE) -> int:
    """Quanti caratteri (o byte) entrano, indicatori compresi."""
    bit = _codeword_dati(versione, correzione) * 8
    bit -= 4 + _BIT_LUNGHEZZA[modo][_gruppo_versione(versione)]
    if modo == _MODO_BYTE:
        return max(0, bit // 8)
    if modo == _MODO_ALFANUMERICO:
        # Due caratteri ogni 11 bit; uno spaiato ne costa 6.
        return max(0, bit // 11 * 2 + (1 if bit % 11 >= 6 else 0))
    # Numerica: tre cifre ogni 10 bit; i resti costano 7 e 4.
    return max(0, bit // 10 * 3 + (2 if bit % 10 >= 7 else (1 if bit % 10 >= 4 else 0)))


# --------------------------------------------------------------------------
# Il testo diventa bit
# --------------------------------------------------------------------------
def _modo_adatto(testo: str) -> int:
    if all("0" <= c <= "9" for c in testo):
        return _MODO_NUMERICO
    if all(c in _VALORE_ALFANUMERICO for c in testo):
        return _MODO_ALFANUMERICO
    return _MODO_BYTE


class _Bit:
    """Un flusso di bit che si scrive in avanti e si rilegge in byte."""

    def __init__(self) -> None:
        self.bit: list[int] = []

    def aggiungi(self, valore: int, quanti: int) -> None:
        for spostamento in range(quanti - 1, -1, -1):
            self.bit.append((valore >> spostamento) & 1)

    def __len__(self) -> int:
        return len(self.bit)


def _segmenta(testo: str, modo: int, versione: int) -> _Bit:
    flusso = _Bit()
    flusso.aggiungi(modo, 4)
    if modo == _MODO_BYTE:
        grezzo = testo.encode("utf-8")
        flusso.aggiungi(len(grezzo), _BIT_LUNGHEZZA[modo][_gruppo_versione(versione)])
        for byte in grezzo:
            flusso.aggiungi(byte, 8)
        return flusso

    flusso.aggiungi(len(testo), _BIT_LUNGHEZZA[modo][_gruppo_versione(versione)])
    if modo == _MODO_ALFANUMERICO:
        for posizione in range(0, len(testo) - 1, 2):
            valore = (
                _VALORE_ALFANUMERICO[testo[posizione]] * 45
                + _VALORE_ALFANUMERICO[testo[posizione + 1]]
            )
            flusso.aggiungi(valore, 11)
        if len(testo) % 2:
            flusso.aggiungi(_VALORE_ALFANUMERICO[testo[-1]], 6)
        return flusso

    for posizione in range(0, len(testo), 3):
        pezzo = testo[posizione : posizione + 3]
        flusso.aggiungi(int(pezzo), len(pezzo) * 3 + 1)
    return flusso


def _codeword(testo: str, modo: int, versione: int, correzione: Correzione) -> list[int]:
    """Bit del contenuto, chiusi e riempiti fino alla capienza della versione."""
    flusso = _segmenta(testo, modo, versione)
    capienza_bit = _codeword_dati(versione, correzione) * 8
    if len(flusso) > capienza_bit:
        raise QRTroppoGrande("il contenuto non entra nella versione scelta")

    # Terminatore: fino a quattro zeri, meno se non ci stanno.
    flusso.aggiungi(0, min(4, capienza_bit - len(flusso)))
    # Poi si arriva al byte pieno.
    flusso.aggiungi(0, (8 - len(flusso) % 8) % 8)
    byte = [
        int("".join(str(b) for b in flusso.bit[i : i + 8]), 2)
        for i in range(0, len(flusso), 8)
    ]
    # E si riempie fino alla capienza alternando i due byte di norma, contando
    # dal primo aggiunto: sono 11101100 e 00010001, scelti perche' il loro
    # alternarsi non somiglia a nessuno schema di servizio.
    mancanti = capienza_bit // 8 - len(byte)
    byte.extend(_RIEMPIMENTO[indice % 2] for indice in range(mancanti))
    return byte


# --------------------------------------------------------------------------
# Reed-Solomon su GF(256)
# --------------------------------------------------------------------------
def _moltiplica(a: int, b: int) -> int:
    """Prodotto in GF(256) con polinomio primitivo 0x11D."""
    risultato = 0
    while b:
        if b & 1:
            risultato ^= a
        b >>= 1
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
    return risultato


def _generatore(grado: int) -> list[int]:
    """Polinomio generatore di grado dato, coefficienti dal piu' alto."""
    risultato = [1]
    radice = 1
    for _ in range(grado):
        nuovo = [0] * (len(risultato) + 1)
        for indice, coefficiente in enumerate(risultato):
            nuovo[indice] ^= coefficiente
            nuovo[indice + 1] ^= _moltiplica(coefficiente, radice)
        risultato = nuovo
        radice = _moltiplica(radice, 2)
    return risultato


def _correzione(dati: list[int], quanti: int) -> list[int]:
    """I codeword di correzione: il resto della divisione per il generatore."""
    generatore = _generatore(quanti)
    resto = [0] * quanti
    for byte in dati:
        fattore = byte ^ resto[0]
        resto = resto[1:] + [0]
        for indice in range(quanti):
            resto[indice] ^= _moltiplica(generatore[indice + 1], fattore)
    return resto


def _intreccia(dati: list[int], versione: int, correzione: Correzione) -> list[int]:
    """Divide in blocchi, calcola la correzione e li intreccia.

    L'intreccio non e' un vezzo: sparpaglia i byte di ogni blocco su tutto il
    simbolo, cosi' un'unica macchia grande danneggia un po' di ogni blocco
    invece di distruggerne uno per intero — ed e' proprio quello che la
    correzione sa riparare.
    """
    indice = correzione.indice
    quanti_blocchi = _BLOCCHI[indice][versione]
    ecc_per_blocco = _ECC_PER_BLOCCO[indice][versione]
    totale = _moduli_dati_grezzi(versione) // 8
    blocchi_corti = quanti_blocchi - totale % quanti_blocchi
    lunghezza_corta = totale // quanti_blocchi

    blocchi: list[list[int]] = []
    codici: list[list[int]] = []
    posizione = 0
    for numero in range(quanti_blocchi):
        lunghezza_dati = lunghezza_corta - ecc_per_blocco + (0 if numero < blocchi_corti else 1)
        blocco = dati[posizione : posizione + lunghezza_dati]
        posizione += lunghezza_dati
        blocchi.append(blocco)
        codici.append(_correzione(blocco, ecc_per_blocco))

    uscita: list[int] = []
    for colonna in range(max(len(b) for b in blocchi)):
        for blocco in blocchi:
            if colonna < len(blocco):
                uscita.append(blocco[colonna])
    for colonna in range(ecc_per_blocco):
        for codice in codici:
            uscita.append(codice[colonna])
    return uscita


# --------------------------------------------------------------------------
# Il simbolo
# --------------------------------------------------------------------------
@dataclass
class CodiceQR:
    """Un simbolo pronto: `moduli[y][x]` vero dove e' scuro."""

    moduli: list[list[bool]]
    versione: int
    correzione: Correzione
    maschera: int

    @property
    def lato(self) -> int:
        return len(self.moduli)

    def __str__(self) -> str:
        """Disegno a caratteri, comodo per guardarlo da un terminale."""
        return "\n".join(
            "".join("##" if modulo else "  " for modulo in riga) for riga in self.moduli
        )


def _posizioni_allineamento(versione: int) -> list[int]:
    if versione == 1:
        return []
    quanti = versione // 7 + 2
    passo = 26 if versione == 32 else (versione * 4 + quanti * 2 + 1) // (quanti * 2 - 2) * 2
    return [6] + [(versione * 4 + 10) - indice * passo for indice in range(quanti - 2, -1, -1)]


class _Tela:
    """La griglia mentre si costruisce, con la memoria di cosa e' di servizio."""

    def __init__(self, versione: int):
        self.versione = versione
        self.lato = versione * 4 + 17
        self.moduli = [[False] * self.lato for _ in range(self.lato)]
        #: Vero dove c'e' uno schema di servizio: quei moduli non portano dati
        #: e la maschera non li tocca.
        self.servizio = [[False] * self.lato for _ in range(self.lato)]

    def imposta(self, x: int, y: int, scuro: bool, servizio: bool = True) -> None:
        self.moduli[y][x] = scuro
        if servizio:
            self.servizio[y][x] = True

    def _riquadro(self, x: int, y: int) -> None:
        """Uno schema di ricerca 7x7 con il suo bordo di separazione."""
        for dy in range(-1, 8):
            for dx in range(-1, 8):
                cx, cy = x + dx, y + dy
                if not (0 <= cx < self.lato and 0 <= cy < self.lato):
                    continue
                distanza = max(abs(dx - 3), abs(dy - 3))
                self.imposta(cx, cy, distanza != 2 and distanza <= 3)

    def schemi(self) -> None:
        # Sincronismo: la riga e la colonna 6, a moduli alternati. Servono al
        # lettore per contare i moduli quando il simbolo e' deformato.
        for posizione in range(self.lato):
            self.imposta(6, posizione, posizione % 2 == 0)
            self.imposta(posizione, 6, posizione % 2 == 0)

        self._riquadro(0, 0)
        self._riquadro(self.lato - 7, 0)
        self._riquadro(0, self.lato - 7)

        posizioni = _posizioni_allineamento(self.versione)
        ultimo = len(posizioni) - 1
        for i, cx in enumerate(posizioni):
            for j, cy in enumerate(posizioni):
                # I tre angoli sono gia' occupati dagli schemi di ricerca.
                if (i, j) in ((0, 0), (0, ultimo), (ultimo, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.imposta(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)

        # Riserva le aree del formato: si riempiono dopo, scelta la maschera.
        for posizione in range(9):
            self.imposta(posizione, 8, False)
            self.imposta(8, posizione, False)
        for posizione in range(8):
            self.imposta(self.lato - 1 - posizione, 8, False)
            self.imposta(8, self.lato - 1 - posizione, False)
        # Il modulo sempre scuro, che non ha nessuna funzione se non esserci.
        self.imposta(8, self.lato - 8, True)

        if self.versione >= 7:
            self._versione()

    def _versione(self) -> None:
        resto = self.versione
        for _ in range(12):
            resto = (resto << 1) ^ ((resto >> 11) * 0x1F25)
        bit = self.versione << 12 | resto
        for indice in range(18):
            scuro = (bit >> indice) & 1 != 0
            x, y = indice // 3, self.lato - 11 + indice % 3
            self.imposta(x, y, scuro)
            self.imposta(y, x, scuro)

    def formato(self, correzione: Correzione, maschera: int) -> None:
        dati = correzione.bit_formato << 3 | maschera
        resto = dati
        for _ in range(10):
            resto = (resto << 1) ^ ((resto >> 9) * 0x537)
        bit = (dati << 10 | resto) ^ 0x5412

        for indice in range(6):
            self.imposta(8, indice, (bit >> indice) & 1 != 0)
        self.imposta(8, 7, (bit >> 6) & 1 != 0)
        self.imposta(8, 8, (bit >> 7) & 1 != 0)
        self.imposta(7, 8, (bit >> 8) & 1 != 0)
        for indice in range(9, 15):
            self.imposta(14 - indice, 8, (bit >> indice) & 1 != 0)

        for indice in range(8):
            self.imposta(self.lato - 1 - indice, 8, (bit >> indice) & 1 != 0)
        for indice in range(8, 15):
            self.imposta(8, self.lato - 15 + indice, (bit >> indice) & 1 != 0)
        self.imposta(8, self.lato - 8, True)

    def disponi(self, codeword: list[int]) -> None:
        """I dati a zigzag, dal basso a destra verso l'alto."""
        indice = 0
        totale = len(codeword) * 8
        destra = self.lato - 1
        while destra >= 1:
            if destra == 6:
                # La colonna 6 e' quella del sincronismo: si salta.
                destra = 5
            for scorrimento in range(self.lato):
                for colonna in (destra, destra - 1):
                    verso_alto = ((destra + 1) & 2) == 0
                    riga = (self.lato - 1 - scorrimento) if verso_alto else scorrimento
                    if self.servizio[riga][colonna]:
                        continue
                    scuro = False
                    if indice < totale:
                        scuro = (codeword[indice >> 3] >> (7 - (indice & 7))) & 1 != 0
                        indice += 1
                    # I bit oltre la fine restano chiari: sono i «resti» che la
                    # norma prevede quando i moduli non sono un multiplo di 8.
                    self.moduli[riga][colonna] = scuro
            destra -= 2


def _maschera(x: int, y: int, disegno: int) -> bool:
    if disegno == 0:
        return (x + y) % 2 == 0
    if disegno == 1:
        return y % 2 == 0
    if disegno == 2:
        return x % 3 == 0
    if disegno == 3:
        return (x + y) % 3 == 0
    if disegno == 4:
        return (y // 2 + x // 3) % 2 == 0
    if disegno == 5:
        return x * y % 2 + x * y % 3 == 0
    if disegno == 6:
        return (x * y % 2 + x * y % 3) % 2 == 0
    return ((x + y) % 2 + x * y % 3) % 2 == 0


def _applica_maschera(tela: _Tela, disegno: int) -> None:
    for y in range(tela.lato):
        for x in range(tela.lato):
            if not tela.servizio[y][x] and _maschera(x, y, disegno):
                tela.moduli[y][x] = not tela.moduli[y][x]


def _penalita(moduli: list[list[bool]]) -> int:
    """Quanto e' scomodo da leggere questo disegno (regole della norma).

    Le quattro regole puniscono cio' che confonde un lettore: file lunghe dello
    stesso colore, blocchi 2x2 uniformi, disegni che somigliano allo schema di
    ricerca, e uno sbilanciamento fra chiaro e scuro.
    """
    lato = len(moduli)
    totale = 0

    # Regola 1: file di cinque o piu' moduli uguali.
    for indice in range(lato):
        for linea in (moduli[indice], [riga[indice] for riga in moduli]):
            corrente, quanti = linea[0], 1
            for modulo in linea[1:]:
                if modulo == corrente:
                    quanti += 1
                else:
                    if quanti >= 5:
                        totale += 3 + (quanti - 5)
                    corrente, quanti = modulo, 1
            if quanti >= 5:
                totale += 3 + (quanti - 5)

    # Regola 2: blocchi 2x2 dello stesso colore.
    for y in range(lato - 1):
        for x in range(lato - 1):
            if moduli[y][x] == moduli[y][x + 1] == moduli[y + 1][x] == moduli[y + 1][x + 1]:
                totale += 3

    # Regola 3: la sequenza dello schema di ricerca con quattro moduli chiari
    # a fianco, che un lettore scambierebbe per un angolo.
    schema = [True, False, True, True, True, False, True]
    chiaro = [False] * 4
    for indice in range(lato):
        for linea in (moduli[indice], [riga[indice] for riga in moduli]):
            for posizione in range(lato - 6):
                if linea[posizione : posizione + 7] != schema:
                    continue
                prima = linea[max(0, posizione - 4) : posizione]
                dopo = linea[posizione + 7 : posizione + 11]
                if prima == chiaro or dopo == chiaro:
                    totale += 40

    # Regola 4: quanto ci si allontana da meta' scuro e meta' chiaro.
    scuri = sum(sum(1 for m in riga if m) for riga in moduli)
    percentuale = scuri * 100 // (lato * lato)
    totale += 10 * max(abs(percentuale - 50) // 5, 0)
    return totale


def codifica(
    testo: str,
    correzione: Correzione = Correzione.M,
    *,
    versione_minima: int = 1,
    versione_massima: int = 40,
) -> CodiceQR:
    """Il simbolo piu' piccolo che contiene `testo` a quella correzione."""
    if not testo:
        raise ValueError("non si codifica un contenuto vuoto")
    modo = _modo_adatto(testo)

    for versione in range(max(1, versione_minima), versione_massima + 1):
        try:
            codeword = _codeword(testo, modo, versione, correzione)
        except QRTroppoGrande:
            continue
        break
    else:
        raise QRTroppoGrande(
            f"{len(testo)} caratteri non entrano in un QR a correzione "
            f"{correzione.name}: al massimo {capacita(versione_massima, correzione, modo)}"
        )

    intrecciati = _intreccia(codeword, versione, correzione)

    migliore: CodiceQR | None = None
    punteggio_migliore = -1
    for disegno in range(8):
        tela = _Tela(versione)
        tela.schemi()
        tela.disponi(intrecciati)
        _applica_maschera(tela, disegno)
        tela.formato(correzione, disegno)
        punteggio = _penalita(tela.moduli)
        if migliore is None or punteggio < punteggio_migliore:
            migliore = CodiceQR(
                moduli=[riga[:] for riga in tela.moduli],
                versione=versione,
                correzione=correzione,
                maschera=disegno,
            )
            punteggio_migliore = punteggio
    assert migliore is not None
    return migliore


def svg(
    codice: CodiceQR,
    *,
    lato_mm: float = 45.0,
    margine_moduli: int = 4,
    colore: str = "currentColor",
    titolo: str = "Codice QR della distinta",
) -> str:
    """Il simbolo come SVG, pronto per la stampa.

    Il margine chiaro (la «zona di quiete») non e' decorazione: senza, molti
    lettori non trovano il codice. La norma ne chiede quattro moduli.
    """
    lato = codice.lato + margine_moduli * 2
    quadrati = []
    for y, riga in enumerate(codice.moduli):
        # Moduli contigui in un solo rettangolo: un SVG con quattromila
        # rettangoli separati e' lento da stampare e pesante da salvare.
        x = 0
        while x < len(riga):
            if not riga[x]:
                x += 1
                continue
            inizio = x
            while x < len(riga) and riga[x]:
                x += 1
            quadrati.append(
                f'<rect x="{inizio + margine_moduli}" y="{y + margine_moduli}" '
                f'width="{x - inizio}" height="1"/>'
            )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {lato} {lato}" '
        f'width="{lato_mm}mm" height="{lato_mm}mm" role="img" '
        f'aria-label="{titolo}" shape-rendering="crispEdges">'
        f"<title>{titolo}</title>"
        f'<rect width="{lato}" height="{lato}" fill="#fff"/>'
        f'<g fill="{colore}">{"".join(quadrati)}</g>'
        "</svg>"
    )
