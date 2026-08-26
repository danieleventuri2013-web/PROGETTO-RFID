"""Il codificatore QR e la Base45, provati sul serio.

Un codificatore QR sbagliato non si vede: produce quadratini che sembrano
giusti e che nessun lettore accetta. Qui ci sono due prove indipendenti.

**Dall'esterno** — le capienze. Sedici numeri pubblicati nella norma (versione 1
e versione 40, quattro livelli di correzione, tre modalita'): se una tabella
fosse sbagliata di un valore, almeno una capienza non tornerebbe. E' cio' che
ha scoperto l'unico errore che c'era davvero.

**Dall'interno** — si rilegge il simbolo. Il test smonta la griglia come
farebbe un lettore: trova il formato, toglie la maschera, ripercorre lo zigzag,
disintreccia i blocchi e ricostruisce il testo. Se disposizione, maschera o
intreccio fossero sbagliati, il testo non tornerebbe.

E poi Base45 sui vettori di RFC 9285, perche' e' la codifica che porta la
tabella della distinta dentro la modalita' alfanumerica.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lims import qr
from lims.base45 import Base45Error, codifica as b45_codifica, decodifica as b45_decodifica
from lims.qr import Correzione, capacita, codifica, svg

#: Le capienze pubblicate nella norma, per (versione, correzione):
#: (byte, alfanumerici, numerici).
CAPIENZE_PUBBLICATE = {
    (1, Correzione.L): (17, 25, 41),
    (1, Correzione.M): (14, 20, 34),
    (1, Correzione.Q): (11, 16, 27),
    (1, Correzione.H): (7, 10, 17),
    (40, Correzione.L): (2953, 4296, 7089),
    (40, Correzione.M): (2331, 3391, 5596),
    (40, Correzione.Q): (1663, 2420, 3993),
    (40, Correzione.H): (1273, 1852, 3057),
}


# ---------------------------------------------------------------------------
# Un lettore, per rileggere quello che si e' scritto
# ---------------------------------------------------------------------------
def _rileggi(codice) -> str:
    """Smonta un simbolo e ne ricostruisce il testo.

    Fa gli stessi passi di un lettore vero, meno la correzione degli errori:
    formato, maschera, zigzag, disintreccio, segmenti. Non e' una funzione di
    produzione — vive qui perche' e' un modo di verificare, non di leggere.
    """
    lato = codice.lato
    versione = (lato - 17) // 4
    assert versione == codice.versione, "il lato non corrisponde alla versione"

    # 1. Il formato: quindici bit attorno allo schema in alto a sinistra.
    grezzo = 0
    for indice in range(6):
        grezzo |= codice.moduli[indice][8] << indice
    grezzo |= codice.moduli[7][8] << 6
    grezzo |= codice.moduli[8][8] << 7
    grezzo |= codice.moduli[8][7] << 8
    for indice in range(9, 15):
        grezzo |= codice.moduli[8][14 - indice] << indice
    bit_formato = (grezzo ^ 0x5412) >> 10
    correzione = next(c for c in Correzione if c.bit_formato == bit_formato >> 3)
    maschera = bit_formato & 7
    assert correzione is codice.correzione
    assert maschera == codice.maschera

    # 2. La mappa degli schemi di servizio, ricostruita come in scrittura.
    tela = qr._Tela(versione)
    tela.schemi()

    # 3. Lo zigzag, togliendo la maschera.
    bit: list[int] = []
    destra = lato - 1
    while destra >= 1:
        if destra == 6:
            destra = 5
        for scorrimento in range(lato):
            for colonna in (destra, destra - 1):
                verso_alto = ((destra + 1) & 2) == 0
                riga = (lato - 1 - scorrimento) if verso_alto else scorrimento
                if tela.servizio[riga][colonna]:
                    continue
                valore = codice.moduli[riga][colonna]
                if qr._maschera(colonna, riga, maschera):
                    valore = not valore
                bit.append(1 if valore else 0)
        destra -= 2

    codeword = [
        int("".join(str(b) for b in bit[i : i + 8]), 2) for i in range(0, len(bit) // 8 * 8, 8)
    ]

    # 4. Il disintreccio: si rimettono insieme i blocchi.
    indice = correzione.indice
    quanti_blocchi = qr._BLOCCHI[indice][versione]
    ecc_per_blocco = qr._ECC_PER_BLOCCO[indice][versione]
    totale = qr._moduli_dati_grezzi(versione) // 8
    blocchi_corti = quanti_blocchi - totale % quanti_blocchi
    lunghezza_corta = totale // quanti_blocchi
    lunghezze = [
        lunghezza_corta - ecc_per_blocco + (0 if n < blocchi_corti else 1)
        for n in range(quanti_blocchi)
    ]

    blocchi: list[list[int]] = [[] for _ in range(quanti_blocchi)]
    posizione = 0
    for colonna in range(max(lunghezze)):
        for numero in range(quanti_blocchi):
            if colonna < lunghezze[numero]:
                blocchi[numero].append(codeword[posizione])
                posizione += 1
    dati = [byte for blocco in blocchi for byte in blocco]

    # 5. I segmenti.
    flusso = "".join(f"{byte:08b}" for byte in dati)
    modo = int(flusso[:4], 2)
    gruppo = qr._gruppo_versione(versione)
    bit_lunghezza = qr._BIT_LUNGHEZZA[modo][gruppo]
    quanti = int(flusso[4 : 4 + bit_lunghezza], 2)
    corpo = flusso[4 + bit_lunghezza :]

    if modo == qr._MODO_BYTE:
        return bytes(int(corpo[i : i + 8], 2) for i in range(0, quanti * 8, 8)).decode("utf-8")
    if modo == qr._MODO_ALFANUMERICO:
        testo = []
        posizione = 0
        for _ in range(quanti // 2):
            valore = int(corpo[posizione : posizione + 11], 2)
            testo.append(qr.ALFANUMERICI[valore // 45])
            testo.append(qr.ALFANUMERICI[valore % 45])
            posizione += 11
        if quanti % 2:
            testo.append(qr.ALFANUMERICI[int(corpo[posizione : posizione + 6], 2)])
        return "".join(testo)
    # Numerica
    testo = []
    posizione = 0
    rimasti = quanti
    while rimasti >= 3:
        testo.append(f"{int(corpo[posizione:posizione + 10], 2):03d}")
        posizione += 10
        rimasti -= 3
    if rimasti == 2:
        testo.append(f"{int(corpo[posizione:posizione + 7], 2):02d}")
    elif rimasti == 1:
        testo.append(f"{int(corpo[posizione:posizione + 4], 2):01d}")
    return "".join(testo)


# ---------------------------------------------------------------------------
# Le capienze
# ---------------------------------------------------------------------------
def test_le_capienze_sono_quelle_della_norma():
    """Sedici numeri pubblicati: se una tabella e' sbagliata, si vede qui."""
    for (versione, correzione), (byte, alfanum, num) in CAPIENZE_PUBBLICATE.items():
        assert capacita(versione, correzione, qr._MODO_BYTE) == byte, (versione, correzione)
        assert capacita(versione, correzione, qr._MODO_ALFANUMERICO) == alfanum, (
            versione,
            correzione,
        )
        assert capacita(versione, correzione, qr._MODO_NUMERICO) == num, (versione, correzione)


def test_la_capienza_cresce_sempre_con_la_versione():
    """Un simbolo piu' grande non puo' contenere meno: e' il controllo che
    scopre una riga di tabella spostata di un posto."""
    for correzione in Correzione:
        serie = [capacita(v, correzione, qr._MODO_BYTE) for v in range(1, 41)]
        assert all(b > a for a, b in zip(serie, serie[1:])), correzione.name


def test_piu_correzione_vuol_dire_meno_spazio():
    for versione in (1, 7, 20, 32, 40):
        capienze = [
            capacita(versione, c, qr._MODO_BYTE)
            for c in (Correzione.L, Correzione.M, Correzione.Q, Correzione.H)
        ]
        assert all(b < a for a, b in zip(capienze, capienze[1:])), versione


# ---------------------------------------------------------------------------
# Rileggere quello che si scrive
# ---------------------------------------------------------------------------
def test_un_testo_alfanumerico_si_rilegge():
    codice = codifica("HELLO WORLD", Correzione.Q)
    assert codice.versione == 1
    assert _rileggi(codice) == "HELLO WORLD"


def test_si_rilegge_in_tutte_le_modalita():
    casi = [
        ("0123456789012345", qr._MODO_NUMERICO),
        ("RFQ1 OSP-S/OF 30", qr._MODO_ALFANUMERICO),
        ("Ospedale di Spoleto — reparto", qr._MODO_BYTE),
    ]
    for testo, modo in casi:
        assert qr._modo_adatto(testo) == modo, testo
        codice = codifica(testo)
        assert _rileggi(codice) == testo, testo


def test_si_rilegge_a_ogni_livello_di_correzione():
    testo = "DISTINTA OSP-S OF 30 CAMPIONI"
    for correzione in Correzione:
        codice = codifica(testo, correzione)
        assert _rileggi(codice) == testo, correzione.name


def test_si_rilegge_anche_quando_serve_un_simbolo_grande():
    """Le versioni oltre la 6 portano anche l'informazione di versione, e oltre
    la 9 cambiano i bit dell'indicatore di lunghezza: due punti in cui si
    sbaglia."""
    for lunghezza in (200, 800, 1500, 2200):
        testo = b45_codifica(os.urandom(lunghezza))
        codice = codifica(testo, Correzione.M)
        assert codice.versione >= 7
        assert _rileggi(codice) == testo, (lunghezza, codice.versione)


def test_una_tabella_da_trenta_righe_sta_in_un_solo_qr():
    """Il caso vero: la distinta di una scatola piena.

    Trenta righe da dieci colonne, compresse e in Base45, devono entrare a
    correzione M — che e' la promessa fatta sul foglio stampato.
    """
    import zlib

    righe = [
        f"MRTMTT25D09F205{chr(65 + n % 26)}:DELLA VALLE:GIANFRANCO:M:19250409:"
        f"20260826:1430:PEZZO OPERATORIO COLON:01:01:03:1/1:"
        f"0100010000{n:04d}0101ABCDEF"
        for n in range(30)
    ]
    tabella = "\n".join(righe)
    compresso = zlib.compress(tabella.encode("utf-8"), 9)
    testo = b45_codifica(compresso)

    assert qr._modo_adatto(testo) == qr._MODO_ALFANUMERICO
    codice = codifica(testo, Correzione.M)
    assert _rileggi(codice) == testo
    # E il conto che è stato promesso: la compressione deve guadagnare parecchio.
    assert len(compresso) < len(tabella) * 0.6, (len(tabella), len(compresso))
    assert codice.versione <= 40


# ---------------------------------------------------------------------------
# La forma del simbolo
# ---------------------------------------------------------------------------
def test_gli_schemi_di_ricerca_sono_ai_tre_angoli():
    codice = codifica("PROVA", Correzione.M)
    lato = codice.lato
    for ox, oy in ((0, 0), (lato - 7, 0), (0, lato - 7)):
        # L'anello esterno scuro, quello interno chiaro, il nucleo scuro.
        assert codice.moduli[oy][ox] is True
        assert codice.moduli[oy + 1][ox + 1] is False
        assert codice.moduli[oy + 3][ox + 3] is True


def test_il_modulo_sempre_scuro_c_e():
    """Un modulo che non ha nessuna funzione se non essere scuro. Se manca,
    nessun lettore trova il formato."""
    for testo in ("A", "PROVA PIU LUNGA", "0" * 100):
        codice = codifica(testo)
        assert codice.moduli[codice.lato - 8][8] is True


def test_il_lato_corrisponde_alla_versione():
    for testo, atteso in (("A", 1), ("A" * 30, 2)):
        codice = codifica(testo, Correzione.M)
        assert codice.lato == codice.versione * 4 + 17
        assert codice.versione == atteso, testo


def test_si_puo_pretendere_una_versione_minima():
    piccolo = codifica("PROVA", Correzione.M)
    grande = codifica("PROVA", Correzione.M, versione_minima=10)
    assert piccolo.versione == 1
    assert grande.versione == 10
    assert _rileggi(grande) == "PROVA"


def test_un_contenuto_smisurato_lo_dice():
    try:
        codifica("A" * 5000, Correzione.H)
        raise AssertionError("QRTroppoGrande attesa")
    except qr.QRTroppoGrande as exc:
        assert "non entrano" in str(exc)


def test_un_contenuto_vuoto_viene_rifiutato():
    try:
        codifica("")
        raise AssertionError("ValueError attesa")
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# Il disegno
# ---------------------------------------------------------------------------
def test_l_svg_ha_la_zona_di_quiete():
    """Senza margine chiaro molti lettori non trovano il codice: la norma ne
    chiede quattro moduli."""
    codice = codifica("PROVA", Correzione.M)
    disegno = svg(codice, margine_moduli=4)
    atteso = codice.lato + 8
    assert f'viewBox="0 0 {atteso} {atteso}"' in disegno
    assert disegno.startswith("<svg") and disegno.endswith("</svg>")
    assert "mm" in disegno, "va stampato, quindi va misurato in millimetri"


def test_l_svg_unisce_i_moduli_contigui():
    """Quattromila rettangoli separati sono lenti da stampare."""
    codice = codifica("0" * 200, Correzione.M)
    disegno = svg(codice)
    scuri = sum(sum(1 for m in riga if m) for riga in codice.moduli)
    rettangoli = disegno.count("<rect") - 1  # meno lo sfondo
    assert rettangoli < scuri, (rettangoli, scuri)


def test_il_disegno_a_caratteri_serve_a_guardarlo():
    codice = codifica("A", Correzione.H)
    testo = str(codice)
    assert testo.count("\n") == codice.lato - 1
    assert "##" in testo


# ---------------------------------------------------------------------------
# Base45
# ---------------------------------------------------------------------------
def test_i_vettori_di_rfc_9285():
    for grezzo, atteso in (
        (b"AB", "BB8"),
        (b"Hello!!", "%69 VD92EX0"),
        (b"base-45", "UJCLQE7W581"),
        (b"ietf!", "QED8WEX0"),
    ):
        assert b45_codifica(grezzo) == atteso, grezzo
        assert b45_decodifica(atteso) == grezzo


def test_base45_va_e_torna_a_ogni_lunghezza():
    for lunghezza in range(0, 200):
        grezzo = os.urandom(lunghezza)
        assert b45_decodifica(b45_codifica(grezzo)) == grezzo, lunghezza


def test_lo_spazio_e_un_dato_non_una_sbavatura():
    """Lo spazio e' il carattere 36 dell'alfabeto, non spaziatura.

    Un testo Base45 puo' cominciare con uno spazio e puo' contenerne dentro —
    il vettore `%69 VD92EX0` della RFC ne ha uno in mezzo. Chi lo ripulisse
    coi metodi soliti corromperebbe il contenuto in silenzio, che e' il modo
    peggiore di sbagliare.
    """
    iniziali = [
        b45_codifica(grezzo)
        for grezzo in (bytes([n, 0]) for n in range(256))
        if b45_codifica(bytes([0, 0]))[0] == " " or True
    ]
    con_spazio_davanti = [t for t in iniziali if t.startswith(" ")]
    assert con_spazio_davanti, "nessun testo Base45 comincia con uno spazio"
    for testo in con_spazio_davanti[:5]:
        assert len(b45_decodifica(testo)) == 2
        # La controprova: toglierlo cambia il contenuto.
        try:
            diverso = b45_decodifica(testo.lstrip(" "))
        except Base45Error:
            continue
        assert diverso != b45_decodifica(testo)

    # E uno spazio in mezzo attraversa la codifica intatto.
    assert b45_decodifica("%69 VD92EX0") == b"Hello!!"


def test_base45_rifiuta_quello_che_non_e_base45():
    for guasto in ("ABCD", "ab!c", "ABè"):
        try:
            b45_decodifica(guasto)
            raise AssertionError(f"Base45Error attesa per {guasto!r}")
        except Base45Error:
            pass


def test_il_terminatore_del_lettore_non_da_fastidio():
    """Un lettore di codici a barre chiude con Invio o una tabulazione."""
    testo = b45_codifica(b"distinta di prova")
    for terminatore in ("\r\n", "\n", "\t"):
        assert b45_decodifica(testo + terminatore) == b"distinta di prova"


def _run_all() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    passed = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
            passed += 1
        except AssertionError as exc:
            print(f"FAIL {test.__name__}: {exc}")
    print()
    print(f"{passed}/{len(tests)} test superati")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(_run_all())
