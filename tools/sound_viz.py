"""Animated GIF of an announcement's frequency bars, a quieter build drawn over Sony's original. No sound.

The GIF holds only bar heights, which can't be turned back into the recording, so it can be
published where Sony's audio can't.

    uv run --with pillow --with miniaudio --with numpy tools/sound_viz.py
    uv run --with pillow --with miniaudio --with numpy tools/sound_viz.py --file voice/000001_se_pwr_on.mp3 --label "Power on"
    uv run --with pillow --with miniaudio --with numpy tools/sound_viz.py --theme light

Defaults: the English package and the -12 dB build in work/ui/packages/, output in work/viz/.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import miniaudio
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from xm3.package import VoicePackage  # noqa: E402

RATE = 16000               # Sony's announcements are 16 kHz mono MP3
FPS = 25
HOP = RATE // FPS          # 640 samples = 40 ms per frame
WINDOW = 1024
BANDS = 28
LOW_HZ, HIGH_HZ = 90, 7800
FLOOR_DB = -66.0
FALL_DB = 2.5              # how fast a bar drops per frame, for smoother motion
LEAD_S, TAIL_S = 0.4, 0.8  # quiet frames before and after, so the loop breathes

W, H = 960, 400
THEMES = {
    "dark": {"bg": "#14161a", "panel": "#1c1f25", "grid": "#2c313a", "text": "#e9ebef", "dim": "#8b93a1",
             "before": "#9aa3b2", "after": "#e8a23a"},
    "light": {"bg": "#ffffff", "panel": "#f4f5f7", "grid": "#e1e4e9", "text": "#1d2027", "dim": "#6b7280",
              "before": "#c4cad3", "after": "#e08e1b"},
}


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    for name in (("segoeuib.ttf", "DejaVuSans-Bold.ttf") if bold else ("segoeui.ttf", "DejaVuSans.ttf")):
        for folder in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")):
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), size)
    return ImageFont.load_default(size)


def decode(data: bytes) -> np.ndarray:
    decoded = miniaudio.decode(data, nchannels=1, sample_rate=RATE, output_format=miniaudio.SampleFormat.SIGNED16)
    return np.array(decoded.samples, dtype=np.float64)


def spectra(samples: np.ndarray) -> np.ndarray:
    """Bar levels in dBFS, one row per frame, with a gentle fall so the bars don't flicker."""
    signal = np.concatenate([np.zeros(int(LEAD_S * RATE)), samples / 32768.0, np.zeros(int(TAIL_S * RATE) + WINDOW)])
    window = np.hanning(WINDOW)
    freqs = np.fft.rfftfreq(WINDOW, 1 / RATE)
    edges = np.geomspace(LOW_HZ, HIGH_HZ, BANDS + 1)
    bands = [np.flatnonzero((freqs >= lo) & (freqs < hi)) for lo, hi in zip(edges[:-1], edges[1:])]
    bands = [b if len(b) else np.array([np.argmin(abs(freqs - lo))]) for b, lo in zip(bands, edges[:-1])]
    rows, previous = [], np.full(BANDS, FLOOR_DB)
    for start in range(0, len(signal) - WINDOW, HOP):
        magnitude = np.abs(np.fft.rfft(signal[start:start + WINDOW] * window)) / (window.sum() / 2)
        level = np.array([20 * np.log10(max(magnitude[b].max(), 1e-9)) for b in bands])
        previous = np.maximum(np.clip(level, FLOOR_DB, 0), previous - FALL_DB)
        rows.append(previous.copy())
    return np.array(rows)


def legend(draw: ImageDraw.ImageDraw, x: float, y: float, color: str, text: str, fonts: dict, c: dict) -> float:
    """Draw a colour chip and its text, right-aligned at x. Returns where the next one should end."""
    width = draw.textlength(text, font=fonts["title"])
    draw.text((x, y), text, font=fonts["title"], fill=c["text"], anchor="rm")
    draw.rounded_rectangle((x - width - 26, y - 8, x - width - 10, y + 8), 3, fill=color)
    return x - width - 50


def render(before: np.ndarray, after: np.ndarray, label: str, gain_db: float, c: dict) -> list[Image.Image]:
    """One panel: each original bar stands behind the quieter one, so the grey cap is what was removed."""
    fonts = {"head": font(22, True), "title": font(17, True), "small": font(13)}
    top, bottom, left, right = 118, H - 50, 90, W - 40
    step = (right - left) / BANDS

    def y(db: float) -> float:
        return bottom - (db - FLOOR_DB) / -FLOOR_DB * (bottom - top)

    frames = []
    for b, a in zip(before, after):
        image = Image.new("RGB", (W, H), c["bg"])
        draw = ImageDraw.Draw(image)
        draw.text((24, 18), label, font=fonts["head"], fill=c["text"])
        draw.text((W - 24, 24), "frequency bars · no sound", font=fonts["small"], fill=c["dim"], anchor="ra")
        draw.rounded_rectangle((24, 62, W - 24, H - 16), 14, fill=c["panel"])
        x = legend(draw, right, 88, c["after"], f"With this project ({gain_db:g} dB)", fonts, c)
        legend(draw, x, 88, c["before"], "Sony's original", fonts, c)
        for db in (0, -20, -40, -60):
            draw.line((left, y(db), right, y(db)), fill=c["grid"], width=1)
            draw.text((left - 10, y(db)), f"{db} dB", font=fonts["small"], fill=c["dim"], anchor="rm")
        for i, (original, quiet) in enumerate(zip(b, a)):
            bx = left + i * step + step * 0.14
            for db, color in ((original, c["before"]), (quiet, c["after"])):
                if bottom - y(db) > 0.5:
                    draw.rounded_rectangle((bx, y(db), bx + step * 0.72, bottom), 3, fill=color)
        draw.text((left, bottom + 10), "low", font=fonts["small"], fill=c["dim"])
        draw.text((right, bottom + 10), "high pitch", font=fonts["small"], fill=c["dim"], anchor="ra")
        frames.append(image)
    return frames


def main() -> None:
    packages = ROOT / "work" / "ui" / "packages"
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--original", type=Path, default=packages / "sony-VGIDCSR0001.bin")
    parser.add_argument("--quiet", type=Path, default=packages / "quiet-v8-b8-4de3966e.bin")
    parser.add_argument("--file", default="voice/000004_se_bt_cntd.mp3")
    parser.add_argument("--label", default="“Bluetooth connected”")
    parser.add_argument("--theme", choices=THEMES, default="dark")
    parser.add_argument("--out", type=Path, default=ROOT / "work" / "viz" / "announcement-quieter.gif")
    args = parser.parse_args()

    original, quiet = VoicePackage(args.original.read_bytes()), VoicePackage(args.quiet.read_bytes())
    meta = args.quiet.with_suffix(".json")
    gain = json.loads(meta.read_text())["voice_db" if args.file.startswith("voice/") else "beep_db"] if meta.exists() else None
    a, b = decode(original.read(args.file)), decode(quiet.read(args.file))
    if gain is None:
        gain = round(20 * np.log10(np.abs(b).max() / np.abs(a).max()), 1)

    frames = render(spectra(a), spectra(b), args.label, gain, THEMES[args.theme])
    palette = frames[len(frames) // 3].quantize(48, method=Image.Quantize.MEDIANCUT)
    # Median cut averages colours, so pin the background entry to the exact theme colour:
    # on a page of the same colour, the GIF then has no visible edge.
    bg = Image.new("RGB", (1, 1), THEMES[args.theme]["bg"]).quantize(palette=palette, dither=Image.Dither.NONE).getpixel((0, 0))
    colours = palette.getpalette()
    colours[bg * 3:bg * 3 + 3] = Image.new("RGB", (1, 1), THEMES[args.theme]["bg"]).getpixel((0, 0))
    palette.putpalette(colours)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    gif[0].save(args.out, save_all=True, append_images=gif[1:], duration=1000 // FPS, loop=0, optimize=True)
    print(f"{args.out}  {len(gif)} frames, {len(gif) / FPS:.1f} s, {args.out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
