"""Report locale dei conteggi API già eseguiti: non invia altre immagini."""
from __future__ import annotations

import argparse
import html
import json
import statistics
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("api_output",type=Path)
    parser.add_argument("--sam",type=Path,required=True)
    parser.add_argument("--attesi",type=int,nargs="+",required=True)
    args=parser.parse_args()
    sam=json.loads(args.sam.read_text(encoding="utf-8"))
    names=sorted({row["foto"] for row in sam["prove"]})
    if len(names)!=len(args.attesi):
        parser.error("un conteggio atteso per ciascuna foto SAM")
    expected=dict(zip(names,args.attesi,strict=True))
    groups=[]
    for path in sorted(args.api_output.glob("*/report.json")):
        report=json.loads(path.read_text(encoding="utf-8"))
        rows=report["prove"]
        groups.append({"cartella":path.parent.name,"modello":report["modello"],
                       "prove":len(rows),"corrette":sum(row.get("conteggio")==expected[row["foto"]] for row in rows),
                       "foto_distinte":len({row["foto"] for row in rows}),
                       "media_secondi":statistics.mean(row["tempo_secondi"] for row in rows),
                       "min_secondi":min(row["tempo_secondi"] for row in rows),
                       "max_secondi":max(row["tempo_secondi"] for row in rows),
                       "costo_usd":sum(row.get("uso",{}).get("cost",0) or 0 for row in rows),
                       "provider":sorted({row.get("provider","") for row in rows}),"risultati":rows})
    best=next(group for group in groups if group["cartella"]=="qwen38")
    summary={"attesi":expected,"gruppi":groups,"costo_totale_usd":sum(group["costo_usd"] for group in groups),
             "sam_media_secondi":statistics.mean(row["tempo_secondi"] for row in sam["prove"]),
             "sam_corrette":sum(row["conteggio"]==expected[row["foto"]] for row in sam["prove"])}
    (args.api_output/"confronto.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    table=[]
    for group in groups:
        table.append(f'<tr><td>{html.escape(group["modello"])}<br><small>{html.escape(", ".join(group["provider"]))}</small></td><td>{group["corrette"]}/{group["prove"]}</td><td>{group["media_secondi"]:.2f} s</td><td>{group["min_secondi"]:.2f}–{group["max_secondi"]:.2f} s</td><td>${group["costo_usd"]:.6f}</td></tr>')
    table.append(f'<tr><td>SAM 2 OpenVINO GPU, banco automatico</td><td>{summary["sam_corrette"]}/{len(sam["prove"])}</td><td>{summary["sam_media_secondi"]:.2f} s</td><td>38,96–40,89 s</td><td>locale</td></tr>')
    cards=[]
    montage=Image.new("RGB",(1200,1000),"#eef3f0")
    for i,row in enumerate(best["risultati"]):
        stem=Path(row["foto"]).stem
        dirname=f'{stem}-prova{row["ripetizione"]}'
        href=f'{best["cartella"]}/{dirname}/centri.png'
        cards.append(f'<article><h3>{html.escape(row["foto"])} · prova {row["ripetizione"]}</h3><p><strong>{row["conteggio"]}/{expected[row["foto"]]} campioni</strong> · {row["tempo_secondi"]:.2f} s</p><a href="{href}"><img src="{href}"></a><p><a href="{best["cartella"]}/{dirname}/risultato.json">Risultato, centri e costo</a></p></article>')
        image=Image.open(args.api_output/href).convert("RGB")
        image.thumbnail((390,215))
        x,y=(i%3)*400,(i//3)*250
        montage.paste(image,(x+(400-image.width)//2,y+30))
        ImageDraw.Draw(montage).text((x+8,y+7),f'{stem} p{row["ripetizione"]}: {row["conteggio"]}/{expected[row["foto"]]} - {row["tempo_secondi"]:.2f} s',fill="#17472f")
    montage.save(args.api_output/"panoramica-qwen38.png")
    report=f'''<!doctype html><html lang="it"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Conteggio campioni: OpenRouter e SAM 2</title><style>body{{font:16px system-ui;background:#eef3f0;color:#23392d;max-width:1250px;margin:auto;padding:24px}}p{{line-height:1.5}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #d7e2dc}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}}article{{background:white;padding:16px;border-radius:12px}}img{{width:100%;height:auto}}h3{{font-size:18px}}a{{color:#176547}}</style><h1>Conteggio campioni con modelli visuali aperti</h1><p>Prova del 2 ottobre 2026. Sei foto distinte, quantità verificate: 8,8,8,8,6,5. Due richieste indipendenti per foto e modello, temperatura 0. Il modello riceve soltanto la foto originale e lo stesso prompt, senza nome del file, numero atteso o contorni SAM.</p><table><thead><tr><th>Modello e provider</th><th>Conteggi corretti / prove</th><th>Tempo medio</th><th>Intervallo</th><th>Costo effettivo delle prove</th></tr></thead><tbody>{''.join(table)}</tbody></table><p><strong>Qwen3.8 27B: {best["corrette"]}/{best["prove"]} conteggi corretti, media {best["media_secondi"]:.2f} s.</strong> I tempi API comprendono preparazione dell'immagine, invio, elaborazione remota e ricezione del JSON con conteggio e centri. Totale OpenRouter, incluse le prove iniziali del secondo modello: ${summary["costo_totale_usd"]:.6f}.</p><p>Qwen3 VL 235B con instradamento automatico ha restituito tre conteggi zero con provider Alibaba, senza indicare incertezza; il resto delle richieste è stato instradato su Parasail. È stato poi riprovato fissando Parasail e disabilitando il fallback: i due gruppi sono riportati separatamente. L'esito dipende anche dal provider e dal formato della risposta, non soltanto dal nome del modello.</p><p>Questo è un confronto operativo sul piccolo insieme di foto disponibili. SAM riconosce il bordo, seleziona una finestra interna ed esegue segmentazione; il modello visuale esamina tutta la foto già ritagliata e fornisce centri approssimativi. I punti blu sotto non sono contorni delle maschere. Le ripetizioni non aggiungono nuove immagini e non provano l'accuratezza su nuovi allestimenti o sulla foto webcam con 17 campioni. I tempi SAM escludono caricamento iniziale; il servizio locale GPU rimane attivo.</p><p>La pagina webcam ora permette di scegliere SAM o Qwen e rianalizzare lo stesso scatto ritagliato e rettificato. Questa tabella riguarda le sei foto da file, non un nuovo benchmark webcam. <a href="confronto.json">Dati del confronto</a> · <a href="../openvino-20261002/report.html">Prove SAM e contorni</a> · <a href="https://huggingface.co/Qwen/Qwen3.8-27B">Pesi aperti Qwen3.8 27B</a></p><h2>Campioni individuati da Qwen3.8 27B</h2><div class="grid">{''.join(cards)}</div></html>'''
    (args.api_output/"report.html").write_text(report,encoding="utf-8")
    print(json.dumps({k:v for k,v in summary.items() if k!="gruppi"},ensure_ascii=False))
    for group in groups:
        print(json.dumps({k:v for k,v in group.items() if k!="risultati"},ensure_ascii=False))


if __name__=="__main__":
    main()
