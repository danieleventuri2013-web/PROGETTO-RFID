"""L'icona dell'applicazione, disegnata invece che salvata.

Android chiede un PNG per mettere l'interfaccia sulla schermata Home, e ne
chiede due misure. Tenerli come file binari in un repository di codice
significa non poterli piu' rileggere: nessuno sa piu' con che colori sono
stati fatti ne' come rifarli quando la tavolozza cambia. Qui il disegno e' il
codice, e i colori sono gli stessi di `tema.css`.

Il segno e' quello dell'intestazione: un vetrino visto di taglio, viola sopra
e rosa sotto — ematossilina ed eosina, nell'ordine in cui si usano al banco.

Nessuna dipendenza: il PNG si scrive con `zlib` e `struct`, che sono nella
libreria standard. Il laboratorio e' isolato dalla rete e questo programma non
installa niente per disegnare due quadrati.
"""

from __future__ import annotations

import math
import struct
import zlib
from functools import lru_cache

__all__ = ["LATI", "icona", "lato_dal_percorso"]

#: Le misure che chiede Android: 192 per l'elenco, 512 per il ritaglio.
LATI = (192, 512)

# Gli stessi valori di `tema.css`, campo chiaro.
_FONDO = (0xF3, 0xF1, 0xF7)
_EMATOSSILINA = (0x3B, 0x2A, 0x63)
_EOSINA = (0xD9, 0x53, 0x6B)

#: Proporzioni del vetrino sul lato dell'icona. Con questi valori il segno sta
#: dentro il cerchio di sicurezza dell'80% che Android usa per il ritaglio
#: («maskable»): mezza diagonale = sqrt(0.30² + 0.62²) / 2 ≈ 0.34 del lato,
#: contro lo 0.40 disponibile.
#:
#: Nell'intestazione il segno e' largo 6px e stondato a 3: a quella misura una
#: capsula e un rettangolo sono la stessa cosa. Qui c'e' spazio per leggere la
#: forma, e la forma giusta e' quella dell'oggetto — un vetrino, angoli appena
#: smussati. Con gli estremi tondi diventerebbe una pastiglia, che in mezzo
#: alle altre icone del tavoletta vorrebbe dire un'altra cosa.
_LARGHEZZA = 0.30
_ALTEZZA = 0.62
_RAGGIO = 0.045
#: Dove l'ematossilina lascia il posto all'eosina, come nel gradiente del segno.
_TAGLIO = 0.55


def lato_dal_percorso(percorso: str) -> int | None:
    """`/icona-192.png` → `192`; qualunque altra cosa → `None`."""
    for lato in LATI:
        if percorso == f"/icona-{lato}.png":
            return lato
    return None


@lru_cache(maxsize=len(LATI))
def icona(lato: int) -> bytes:
    """Il PNG dell'icona, disegnato una volta sola per misura."""
    if lato not in LATI:
        raise ValueError(f"misura non prevista: {lato}")

    centro = lato / 2
    larghezza = lato * _LARGHEZZA
    altezza = lato * _ALTEZZA
    raggio = lato * _RAGGIO
    # Meta' dei lati diritti: oltre questi, l'angolo stondato.
    mezza_larghezza_retta = larghezza / 2 - raggio
    mezza_altezza_retta = altezza / 2 - raggio
    taglio = centro - altezza / 2 + altezza * _TAGLIO

    # Solo la striscia che contiene il segno va calcolata pixel per pixel: il
    # resto e' fondo pieno, e su 512x512 sono nove decimi dell'immagine.
    primo_x = max(int(centro - larghezza / 2) - 2, 0)
    ultimo_x = min(int(centro + larghezza / 2) + 2, lato)
    primo_y = max(int(centro - altezza / 2) - 2, 0)
    ultimo_y = min(int(centro + altezza / 2) + 2, lato)

    riga_fondo = bytes(_FONDO) * lato
    righe = bytearray()
    for y in range(lato):
        if not (primo_y <= y < ultimo_y):
            righe += b"\x00" + riga_fondo
            continue
        py = y + 0.5
        colore = _EMATOSSILINA if py < taglio else _EOSINA
        riga = bytearray(riga_fondo)
        for x in range(primo_x, ultimo_x):
            px = x + 0.5
            # Distanza dal bordo del vetrino: negativa dentro, positiva fuori.
            # Mezzo pixel di sfumatura basta a togliere la scala agli angoli.
            dx = max(abs(px - centro) - mezza_larghezza_retta, 0.0)
            dy = max(abs(py - centro) - mezza_altezza_retta, 0.0)
            distanza = math.hypot(dx, dy) - raggio
            copertura = min(max(0.5 - distanza, 0.0), 1.0)
            if copertura <= 0.0:
                continue
            for canale in range(3):
                fondo = _FONDO[canale]
                riga[x * 3 + canale] = round(fondo + (colore[canale] - fondo) * copertura)
        righe += b"\x00" + bytes(riga)

    return _png(lato, lato, bytes(righe))


def _pezzo(tipo: bytes, dati: bytes) -> bytes:
    corpo = tipo + dati
    return struct.pack(">I", len(dati)) + corpo + struct.pack(">I", zlib.crc32(corpo))


def _png(larghezza: int, altezza: int, righe: bytes) -> bytes:
    """PNG a 8 bit per canale, RGB, senza filtri (`righe` gia' prefissate)."""
    intestazione = struct.pack(">IIBBBBB", larghezza, altezza, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _pezzo(b"IHDR", intestazione)
        + _pezzo(b"IDAT", zlib.compress(righe, 9))
        + _pezzo(b"IEND", b"")
    )
