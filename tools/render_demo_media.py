"""Trasforma screenshot reali della WebUI in PNG 1080p, video e ZIP."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import textwrap
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

WIDTH = 1920
HEIGHT = 1080
INK = "#1F2933"
ACCENT = "#B72D5D"
PAPER = "#F4F1EC"
WATERMARK = "DEMO - DATI FITTIZI - NESSUN INVIO PEC REALE"

SCENES = [
    ("01_anagrafica_paziente", "Inserimento dell'anagrafica del paziente e dei dati del reperto."),
    ("02_accettazione_due_contenitori", "Accettazione registrata: due contenitori da inizializzare."),
    ("03_tag_scritti", "Scrittura e verifica dei due tag RFID, uno per volta sul banco."),
    ("04_laboratorio_destinatario", "Laboratorio destinatario già configurato con codice univoco e PEC."),
    ("05_spedizione_preparata", "Preparazione della spedizione verso il laboratorio selezionato."),
    ("06_sigillo_conforme", "Sigillo conforme: nel volume sono presenti esattamente 2 campioni su 2."),
    ("07_distinta_pec_consegnata", "Distinta v2 cifrata, firmata, archiviata e consegnata via PEC simulata."),
    ("08_distinta_verificata", "Il destinatario apre la distinta: hash e firma del mittente risultano verificati."),
    ("09_ricezione_completata", "Ricezione conforme e registrata: arrivati esattamente 2 campioni su 2."),
]


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = [
        "C:/Windows/Fonts/seguisb.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for name in names:
        if Path(name).is_file():
            return ImageFont.truetype(name, size)
    return ImageFont.load_default(size=size)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> str:
    words = text.split()
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return "\n".join(lines)


def render_scene(source: Path, target: Path, *, index: int, caption: str) -> None:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), PAPER)
    draw = ImageDraw.Draw(canvas)
    title_font = _font(38, bold=True)
    caption_font = _font(31)
    small_font = _font(22, bold=True)

    draw.rectangle((0, 0, WIDTH, 88), fill=INK)
    draw.text((72, 22), "RFID LIMS - Dimostrazione frontend", font=title_font, fill="white")
    badge = f"PASSO {index}/9"
    badge_box = draw.textbbox((0, 0), badge, font=small_font)
    badge_w = badge_box[2] - badge_box[0] + 42
    draw.rounded_rectangle((WIDTH - badge_w - 72, 20, WIDTH - 72, 68), 20, fill=ACCENT)
    draw.text((WIDTH - badge_w - 51, 31), badge, font=small_font, fill="white")

    screenshot = Image.open(source).convert("RGB")
    content_box = (54, 112, WIDTH - 54, 865)
    fitted = ImageOps.contain(
        screenshot,
        (content_box[2] - content_box[0], content_box[3] - content_box[1]),
        Image.Resampling.LANCZOS,
    )
    x = content_box[0] + (content_box[2] - content_box[0] - fitted.width) // 2
    y = content_box[1] + (content_box[3] - content_box[1] - fitted.height) // 2
    draw.rounded_rectangle((x - 4, y - 4, x + fitted.width + 4, y + fitted.height + 4), 8, fill="#D2CBC2")
    canvas.paste(fitted, (x, y))

    draw.rectangle((0, 890, WIDTH, HEIGHT), fill="white")
    wrapped = _fit_text(draw, caption, caption_font, 1500)
    draw.text((72, 922), wrapped, font=caption_font, fill=INK, spacing=8)
    draw.text((72, 1031), WATERMARK, font=small_font, fill=ACCENT)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "PNG", optimize=True)


def render_card(target: Path, title: str, subtitle: str) -> None:
    canvas = Image.new("RGB", (WIDTH, HEIGHT), INK)
    draw = ImageDraw.Draw(canvas)
    title_font = _font(76, bold=True)
    subtitle_font = _font(38)
    small_font = _font(25, bold=True)
    draw.rectangle((0, 0, 22, HEIGHT), fill=ACCENT)
    draw.text((150, 315), title, font=title_font, fill="white")
    wrapped = textwrap.fill(subtitle, width=62)
    draw.multiline_text((155, 445), wrapped, font=subtitle_font, fill="#E8E3DC", spacing=16)
    draw.text((155, 890), "William - RIXLAB", font=small_font, fill="#E8E3DC")
    draw.text((155, 950), WATERMARK, font=small_font, fill="#FF8DB2")
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "PNG", optimize=True)


def _ffmpeg() -> Path:
    candidates = [
        shutil.which("ffmpeg"),
        "C:/Program Files/Softdeluxe/Free Download Manager/ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError("FFmpeg non trovato")


def _quote_concat(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")


def build_video(output: Path, frames: list[tuple[Path, float]]) -> Path:
    timeline = output / "_video_frames" / "timeline.txt"
    timeline.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for path, duration in frames:
        lines.append(f"file '{_quote_concat(path)}'")
        lines.append(f"duration {duration:.2f}")
    lines.append(f"file '{_quote_concat(frames[-1][0])}'")
    timeline.write_text("\n".join(lines) + "\n", encoding="utf-8")
    video = output / "RFID_LIMS_demo_William_RIXLAB.mp4"
    command = [
        str(_ffmpeg()),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(timeline),
        "-vf",
        "fps=20,format=nv12",
        "-c:v",
        "h264_mf",
        "-b:v",
        "1400k",
        "-movflags",
        "+faststart",
        str(video),
    ]
    subprocess.run(command, check=True)
    subprocess.run(
        [str(_ffmpeg()), "-v", "error", "-i", str(video), "-f", "null", "-"],
        check=True,
    )
    if video.stat().st_size > 20 * 1024 * 1024:
        raise RuntimeError(f"video oltre 20 MB: {video.stat().st_size} byte")
    return video


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_readme(output: Path, video: Path, screenshots: list[Path]) -> Path:
    readme = output / "LEGGIMI.txt"
    lines = [
        "DIMOSTRAZIONE FRONTEND RFID LIMS - WILLIAM / RIXLAB",
        "",
        WATERMARK,
        "",
        "Scenario: una Unita Locale Demo registra un paziente fittizio, scrive due tag,",
        "sigilla la spedizione, archivia e invia una distinta v2 tramite PEC simulata.",
        "Il laboratorio ricevente verifica firma e SHA-256, legge 2/2 campioni e registra",
        "la ricezione conforme.",
        "",
        "Nessun lettore RFID, account PEC, database o dato sanitario reale e' stato usato.",
        "Gli indirizzi .invalid non possono essere recapitati su Internet.",
        "",
        f"Video: {video.name} ({video.stat().st_size / 1024 / 1024:.2f} MB)",
        f"SHA-256 video: {_sha256(video)}",
        "",
        "Screenshot:",
    ]
    lines.extend(f"- {path.name}" for path in screenshots)
    readme.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return readme


def build_zip(output: Path, files: list[Path]) -> Path:
    target = output / "RFID_LIMS_demo_William_RIXLAB.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    raw = output / "raw"
    frames_dir = output / "_video_frames"
    screenshots: list[Path] = []
    for index, (name, caption) in enumerate(SCENES, start=1):
        source = raw / f"{name}.png"
        if not source.is_file():
            raise FileNotFoundError(f"screenshot mancante: {source}")
        target = output / f"{name}.png"
        render_scene(source, target, index=index, caption=caption)
        screenshots.append(target)

    title = frames_dir / "00_titolo.png"
    outro = frames_dir / "10_conclusione.png"
    render_card(
        title,
        "Tracciabilita' campioni RFID",
        "Dal paziente alla ricezione: scrittura tag, sigillo, distinta sicura e verifica 2/2.",
    )
    render_card(
        outro,
        "Flusso completato",
        "Catena di custodia registrata, distinta cifrata e firmata, campioni ricevuti 2/2.",
    )
    video_frames = [(title, 4.0)] + [(path, 7.0) for path in screenshots] + [(outro, 5.0)]
    video = build_video(output, video_frames)
    readme = build_readme(output, video, screenshots)
    zip_path = build_zip(output, [video, readme, *screenshots])
    print(
        f"Creati {len(screenshots)} screenshot, {video.name} "
        f"({video.stat().st_size} byte) e {zip_path.name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
