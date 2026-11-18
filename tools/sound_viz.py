"""Animated GIF of an announcement's frequency bars, Sony's original next to a quieter build. No sound.

The GIF holds only bar heights, which can't be turned back into the recording, so it can be
published where Sony's audio can't.

    uv run --with pillow --with miniaudio --with numpy tools/sound_viz.py
    uv run --with pillow --with miniaudio --with numpy tools/sound_viz.py --file voice/000001_se_pwr_on.mp3 --label "Power on"

Defaults: the English package and the -9 dB build in work/ui/packages/, output in work/viz/.
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
BG, PANEL, GRID, TEXT, DIM = "#14161a", "#1c1f25", "#2c313a", "#e9ebef", "#8b93a1"
BEFORE, AFTER = "#9aa3b2", "#e8a23a"


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


def draw_panel(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], levels: np.ndarray,
               title: str, color: str, fonts: dict) -> None:
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, 14, fill=PANEL)
    draw.text((x0 + 20, y0 + 16), title, font=fonts["title"], fill=TEXT)
    top, bottom, left, right = y0 + 62, y1 - 30, x0 + 66, x1 - 18
    for db in (0, -20, -40, -60):
        y = bottom - (db - FLOOR_DB) / -FLOOR_DB * (bottom - top)
        draw.line((left, y, right, y), fill=GRID, width=1)
        draw.text((left - 8, y), f"{db} dB", font=fonts["small"], fill=DIM, anchor="rm")
    step = (right - left) / BANDS
    for i, db in enumerate(levels):
        height = (db - FLOOR_DB) / -FLOOR_DB * (bottom - top)
        if height > 0.5:
            bx = left + i * step + step * 0.16
            draw.rounded_rectangle((bx, bottom - height, bx + step * 0.68, bottom), 3, fill=color)
    draw.text((left, bottom + 8), "low", font=fonts["small"], fill=DIM)
    draw.text((right, bottom + 8), "high pitch", font=fonts["small"], fill=DIM, anchor="ra")


def render(before: np.ndarray, after: np.ndarray, label: str, gain_db: float) -> list[Image.Image]:
    fonts = {"head": font(22, True), "title": font(19, True), "small": font(13)}
    frames = []
    for b, a in zip(before, after):
        image = Image.new("RGB", (W, H), BG)
        draw = ImageDraw.Draw(image)
        draw.text((24, 18), label, font=fonts["head"], fill=TEXT)
        draw.text((W - 24, 24), "frequency bars · no sound", font=fonts["small"], fill=DIM, anchor="ra")
        draw_panel(draw, (24, 62, W // 2 - 8, H - 20), b, "Sony's original", BEFORE, fonts)
        draw_panel(draw, (W // 2 + 8, 62, W - 24, H - 20), a, f"With this project ({gain_db:g} dB)", AFTER, fonts)
        frames.append(image)
    return frames


def main() -> None:
    packages = ROOT / "work" / "ui" / "packages"
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--original", type=Path, default=packages / "sony-VGIDCSR0001.bin")
    parser.add_argument("--quiet", type=Path, default=packages / "quiet-v6-b6-3415f735.bin")
    parser.add_argument("--file", default="voice/000004_se_bt_cntd.mp3")
    parser.add_argument("--label", default="“Bluetooth connected”")
    parser.add_argument("--out", type=Path, default=ROOT / "work" / "viz" / "announcement-quieter.gif")
    args = parser.parse_args()

    original, quiet = VoicePackage(args.original.read_bytes()), VoicePackage(args.quiet.read_bytes())
    meta = args.quiet.with_suffix(".json")
    gain = json.loads(meta.read_text())["voice_db" if args.file.startswith("voice/") else "beep_db"] if meta.exists() else None
    a, b = decode(original.read(args.file)), decode(quiet.read(args.file))
    if gain is None:
        gain = round(20 * np.log10(np.abs(b).max() / np.abs(a).max()), 1)

    frames = render(spectra(a), spectra(b), args.label, gain)
    palette = frames[len(frames) // 3].quantize(48, method=Image.Quantize.MEDIANCUT)
    gif = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    gif[0].save(args.out, save_all=True, append_images=gif[1:], duration=1000 // FPS, loop=0, optimize=True)
    print(f"{args.out}  {len(gif)} frames, {len(gif) / FPS:.1f} s, {args.out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
