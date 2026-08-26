"""Base45 (RFC 9285): byte arbitrari dentro un QR alfanumerico.

Un QR ha una modalita' **alfanumerica** che usa 5,5 bit per carattere invece
degli 8 della modalita' byte, ma accetta soltanto 45 simboli: cifre, lettere
maiuscole e nove segni di interpunzione. Base45 esiste esattamente per quello —
e' la codifica dei certificati COVID europei, nata dallo stesso problema:
infilare dati binari in un QR senza sprecare un terzo della capienza.

Il conto, per la distinta che deve stare su un foglio:

* 30 righe da dieci colonne fanno circa 3.600 caratteri di testo;
* compresso con `zlib` scende attorno ai 1.500 byte, perche' il testo e'
  ripetitivo — stesse date, stessi codici, EPC con lo stesso prefisso;
* in Base45 diventano ~2.250 caratteri, che stanno in un QR a correzione M con
  margine, mentre i 3.600 di partenza entrerebbero solo al livello di
  correzione piu' debole.

Due byte per volta diventano tre caratteri (45³ = 91.125 > 65.536); un byte
spaiato in fondo ne diventa due. Il byte meno significativo va per primo:
e' l'ordine dello standard, ed e' l'unico dettaglio in cui si sbaglia.
"""

from __future__ import annotations

__all__ = ["ALFABETO", "Base45Error", "codifica", "decodifica"]

#: I 45 simboli, nell'ordine che ne fissa il valore. E' lo stesso insieme che
#: la modalita' alfanumerica del QR sa rappresentare, e non e' una
#: coincidenza: RFC 9285 e' scritta per questo.
ALFABETO = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ $%*+-./:"

_VALORI = {carattere: indice for indice, carattere in enumerate(ALFABETO)}


class Base45Error(ValueError):
    """Testo che non e' Base45 valido."""


def codifica(dati: bytes) -> str:
    """Byte → testo Base45."""
    uscita: list[str] = []
    for posizione in range(0, len(dati) - 1, 2):
        valore = dati[posizione] * 256 + dati[posizione + 1]
        valore, primo = divmod(valore, 45)
        terzo, secondo = divmod(valore, 45)
        uscita.append(ALFABETO[primo] + ALFABETO[secondo] + ALFABETO[terzo])
    if len(dati) % 2:
        secondo, primo = divmod(dati[-1], 45)
        uscita.append(ALFABETO[primo] + ALFABETO[secondo])
    return "".join(uscita)


def decodifica(testo: str) -> bytes:
    """Testo Base45 → byte.

    **Lo spazio non si tocca.** E' il carattere 36 dell'alfabeto, quindi in
    Base45 e' un dato a tutti gli effetti: uno spazio in fondo puo' essere
    l'ultimo byte del contenuto, e toglierlo lo corromperebbe in silenzio. Si
    tolgono solo il ritorno a capo e la tabulazione, che sono cio' che un
    lettore di codici a barre aggiunge come terminatore.
    """
    pulito = testo.strip("\r\n\t").upper()
    if not pulito:
        return b""
    resto = len(pulito) % 3
    if resto == 1:
        raise Base45Error(
            f"lunghezza {len(pulito)} non valida: Base45 usa gruppi da 3 caratteri, "
            "o 2 per l'ultimo byte"
        )

    try:
        valori = [_VALORI[carattere] for carattere in pulito]
    except KeyError as exc:
        raise Base45Error(f"carattere non ammesso in Base45: {exc.args[0]!r}") from exc

    uscita = bytearray()
    for posizione in range(0, len(valori) - resto, 3):
        numero = valori[posizione] + valori[posizione + 1] * 45 + valori[posizione + 2] * 45 * 45
        if numero > 0xFFFF:
            raise Base45Error("gruppo fuori intervallo: il testo non e' Base45 valido")
        uscita += numero.to_bytes(2, "big")
    if resto == 2:
        numero = valori[-2] + valori[-1] * 45
        if numero > 0xFF:
            raise Base45Error("ultimo gruppo fuori intervallo: il testo e' corrotto")
        uscita.append(numero)
    return bytes(uscita)
