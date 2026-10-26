#!/usr/bin/env python3
"""
Urdu YouTube Shorts maker.

Reads scripts.txt and makes one vertical 1080x1920 MP4 per script (in ./out).
  - Voice:    Microsoft neural voice ur-PK-AsadNeural (male Urdu, free, needs internet)
  - Captions: big Urdu text, one line of the script at a time, timed to the voice
  - Look:     slowly moving gradient background, title banner, progress bar

scripts.txt format (see the example file):
    Title line (Urdu)
    narration line 1
    narration line 2
    ...
    tags: #Shorts #اردو        (optional)
    ---                          (separates videos)
"""
import argparse
import asyncio
import glob
import math
import re
import os
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

W, H, FPS = 1080, 1920, 30
VOICE = os.environ.get("TTS_VOICE", "ur-PK-AsadNeural")  # male Urdu (Pakistan)
RATE = os.environ.get("TTS_RATE", "+0%")                  # e.g. "+10%" faster, "-10%" slower
LEAD = 0.35   # seconds of silence before the first line
GAP = 0.30    # pause between lines
TAIL = 0.80   # seconds after the last line
LANG = "ur"
SAMPLE_RATE = 44100

# (top colour, bottom colour, accent colour) - cycles per video
PALETTES = [
    ((14, 36, 84), (88, 28, 135), (255, 196, 61)),
    ((6, 78, 59), (6, 30, 50), (253, 224, 71)),
    ((127, 29, 29), (30, 20, 60), (251, 146, 60)),
    ((8, 47, 73), (19, 78, 74), (125, 211, 252)),
    ((49, 46, 129), (136, 19, 55), (253, 164, 175)),
]

FALLBACK_FONTS = [
    "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
]


# --------------------------------------------------------------------------- #
# Reading scripts
# --------------------------------------------------------------------------- #
def parse_scripts(text):
    videos = []
    for chunk in re.split(r"^\s*---+\s*$", text, flags=re.M):
        lines = [l.strip() for l in chunk.splitlines()]
        lines = [l for l in lines if l and not l.startswith("#")]
        if len(lines) < 2:
            continue
        title, tags, narration = lines[0], "", []
        for l in lines[1:]:
            if l.lower().startswith("tags:"):
                tags = l[5:].strip()
            else:
                narration.append(l)
        if narration:
            videos.append({"title": title, "lines": narration, "tags": tags})
    return videos


# --------------------------------------------------------------------------- #
# Fonts and text drawing (Pillow + libraqm does the Urdu shaping / right-to-left)
# --------------------------------------------------------------------------- #
def find_font():
    env = os.environ.get("FONT_FILE")
    if env and Path(env).exists():
        return env

    here = Path(__file__).resolve().parent
    local = sorted(glob.glob(str(here / "fonts" / "*.ttf")) + glob.glob(str(here / "fonts" / "*.otf")))
    if local:
        return local[0]

    style = os.environ.get("FONT_STYLE", "naskh").lower()
    family = "NotoNastaliqUrdu" if style == "nastaliq" else "NotoNaskhArabic"
    for root in ("/usr/share/fonts", "/usr/local/share/fonts", str(Path.home() / ".fonts")):
        found = [f for f in glob.glob(f"{root}/**/*{family}*", recursive=True)
                 if f.lower().endswith((".ttf", ".otf"))]
        bold = [f for f in found if "bold" in Path(f).name.lower() and "semi" not in Path(f).name.lower()]
        if bold:
            return sorted(bold)[0]
        if found:
            return sorted(found)[0]

    for p in FALLBACK_FONTS:
        if Path(p).exists():
            return p
    sys.exit("No Urdu font found. On GitHub the workflow installs one for you. "
             "Locally, put an Urdu .ttf file in a folder called 'fonts' next to this script.")


def load_font(path, size):
    f = ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
    try:  # variable fonts: ask for bold; ordinary fonts raise, which is fine
        f.set_variation_by_axes([700])
    except Exception:
        pass
    return f


def text_width(draw, s, font):
    return draw.textlength(s, font=font, direction="rtl", language=LANG)


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if not cur or text_width(draw, trial, font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def render_card(text, font_path, start_size, max_w, max_lines, box_rgba,
                stroke=6, pad=(46, 34), radius=44,
                fill=(255, 255, 255), stroke_fill=(0, 0, 0)):
    """Return an RGBA image: rounded box with centred right-to-left text."""
    scratch = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    size = start_size
    while True:
        font = load_font(font_path, size)
        lines = wrap(scratch, text, font, max_w)
        if len(lines) <= max_lines or size <= 44:
            break
        size -= 4

    kw = dict(font=font, anchor="ma", direction="rtl", language=LANG, stroke_width=stroke)
    boxes = [scratch.textbbox((0, 0), l, **kw) for l in lines]
    line_h = max(b[3] - b[1] for b in boxes)
    top = min(b[1] for b in boxes)
    gap = int(size * 0.22)
    text_w = max(b[2] - b[0] for b in boxes)

    bw = int(text_w + 2 * pad[0])
    bh = int(line_h * len(lines) + gap * (len(lines) - 1) + 2 * pad[1])
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, bw - 1, bh - 1), radius=radius, fill=box_rgba)
    y = pad[1] - top
    for l in lines:
        d.text((bw / 2, y), l, fill=fill, stroke_fill=stroke_fill, **kw)
        y += line_h + gap
    return img


def make_background(idx, path):
    top, bottom, accent = PALETTES[idx % len(PALETTES)]
    bw, bh = int(W * 1.15), int(H * 1.15)  # a bit bigger than the video so it can drift
    grad = Image.linear_gradient("L").resize((bw, bh))
    bg = ImageOps.colorize(grad, black=top, white=bottom).convert("RGBA")
    glow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    d.ellipse((-200, 200, 700, 1100), fill=accent + (70,))
    d.ellipse((600, 1300, 1400, 2100), fill=accent + (55,))
    d.ellipse((300, -300, 1100, 400), fill=(255, 255, 255, 35))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    Image.alpha_composite(bg, glow).convert("RGB").save(path)


def make_bar(idx, path):
    accent = PALETTES[idx % len(PALETTES)][2]
    Image.new("RGBA", (W, 14), accent + (235,)).save(path)


# --------------------------------------------------------------------------- #
# Voice
# --------------------------------------------------------------------------- #
async def synth_all(lines, mp3_paths):
    import edge_tts  # imported here so --fake-voice works without it
    for text, path in zip(lines, mp3_paths):
        last_error = None
        for attempt in range(4):
            try:
                await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
                if Path(path).stat().st_size > 1000:
                    break
            except Exception as e:  # network hiccup: wait and retry
                last_error = e
                await asyncio.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"Voice generation failed for: {text[:40]}... ({last_error})")


def to_wav(src, dst):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ar", str(SAMPLE_RATE),
                    "-ac", "1", "-c:a", "pcm_s16le", str(dst)], check=True)


def fake_wav(text, dst):
    """Quiet test tone, only used with --fake-voice (no internet needed)."""
    seconds = max(1.2, 0.075 * len(text))
    n = int(SAMPLE_RATE * seconds)
    frames = b"".join(struct.pack("<h", int(2500 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE)))
                      for i in range(n))
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(frames)


def build_voice_track(lines, workdir, fake):
    """Return (voice.wav path, [(start, end) per line], total duration)."""
    wavs = []
    if fake:
        for i, text in enumerate(lines):
            p = workdir / f"line_{i}.wav"
            fake_wav(text, p)
            wavs.append(p)
    else:
        mp3s = [workdir / f"line_{i}.mp3" for i in range(len(lines))]
        asyncio.run(synth_all(lines, mp3s))
        for i, m in enumerate(mp3s):
            p = workdir / f"line_{i}.wav"
            to_wav(m, p)
            wavs.append(p)

    silence = lambda sec: b"\x00\x00" * int(SAMPLE_RATE * sec)
    out = workdir / "voice.wav"
    times, t = [], LEAD
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(silence(LEAD))
        for i, p in enumerate(wavs):
            with wave.open(str(p), "rb") as r:
                data, n = r.readframes(r.getnframes()), r.getnframes()
            dur = n / SAMPLE_RATE
            times.append((t, t + dur))
            w.writeframes(data)
            t += dur
            if i < len(wavs) - 1:
                w.writeframes(silence(GAP))
                t += GAP
        w.writeframes(silence(TAIL))
        t += TAIL
    return out, times, t


# --------------------------------------------------------------------------- #
# One video
# --------------------------------------------------------------------------- #
def make_video(index, video, out_dir, font_path, fake):
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"short_{index:02d}"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        voice_wav, times, dur = build_voice_track(video["lines"], tmp, fake)

        bg, bar = tmp / "bg.png", tmp / "bar.png"
        make_background(index - 1, bg)
        make_bar(index - 1, bar)

        banner = render_card(video["title"], font_path, 68, 880, 2,
                             PALETTES[(index - 1) % len(PALETTES)][2] + (235,),
                             stroke=0, fill=(20, 20, 30), pad=(44, 26), radius=60)
        banner_path = tmp / "banner.png"
        banner.save(banner_path)

        captions = []
        for i, text in enumerate(video["lines"]):
            card = render_card(text, font_path, 86, 900, 4, (0, 0, 0, 150))
            p = tmp / f"cap_{i}.png"
            card.save(p)
            captions.append((p, card.size))

        cmd = ["ffmpeg", "-y", "-loglevel", "error",
               "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", str(bg),
               "-i", str(bar), "-i", str(banner_path)]
        for p, _ in captions:
            cmd += ["-i", str(p)]
        cmd += ["-i", str(voice_wav)]
        audio_index = 3 + len(captions)

        fc = [f"[0:v]crop={W}:{H}:x='(iw-{W})/2*(1+sin(2*PI*t/22))':"
              f"y='(ih-{H})/2*(1+cos(2*PI*t/28))'[v0]",
              f"[v0][1:v]overlay=x='-w+w*t/{dur:.3f}':y=40[v1]",
              f"[v1][2:v]overlay=x={(W - banner.size[0]) // 2}:y=190[v2]"]
        label = "v2"
        for i, ((p, (cw, ch)), (start, _end)) in enumerate(zip(captions, times)):
            shows_from = max(0.0, start - 0.05)
            shows_to = (times[i + 1][0] - 0.05) if i + 1 < len(times) else dur
            nxt = f"c{i}"
            fc.append(f"[{label}][{3 + i}:v]overlay=x={(W - cw) // 2}:y={int(1010 - ch / 2)}:"
                      f"enable='between(t,{shows_from:.3f},{shows_to:.3f})'[{nxt}]")
            label = nxt
        fc.append(f"[{label}]format=yuv420p[v]")

        out_mp4 = out_dir / f"{name}.mp4"
        cmd += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", f"{audio_index}:a",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS),
                "-c:a", "aac", "-b:a", "160k", "-t", f"{dur:.3f}", "-movflags", "+faststart",
                str(out_mp4)]
        subprocess.run(cmd, check=True)

    tags = video["tags"] or "#Shorts #اردو"
    if "#shorts" not in tags.lower():
        tags += " #Shorts"
    (out_dir / f"{name}.txt").write_text(
        f"TITLE:\n{video['title']} #Shorts\n\nDESCRIPTION:\n{video['title']}\n\n{tags}\n",
        encoding="utf-8")
    return out_mp4, dur


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Make Urdu YouTube Shorts from scripts.txt")
    ap.add_argument("--scripts", default="scripts.txt")
    ap.add_argument("--out", default="out")
    ap.add_argument("--only", type=int, help="only make video number N")
    ap.add_argument("--fake-voice", action="store_true",
                    help="test mode: placeholder beeps instead of the real voice (no internet)")
    args = ap.parse_args()

    if not features.check("raqm"):
        sys.exit("Urdu text shaping is missing (Pillow needs libraqm). "
                 "On Ubuntu run: sudo apt-get install -y libraqm0 libfribidi0")
    if subprocess.run(["ffmpeg", "-version"], capture_output=True).returncode != 0:
        sys.exit("ffmpeg is not installed.")

    text = Path(args.scripts).read_text(encoding="utf-8")
    videos = parse_scripts(text)
    if not videos:
        sys.exit(f"No scripts found in {args.scripts}.")

    font_path = find_font()
    print(f"Font: {font_path}")
    print(f"Voice: {'(test beeps)' if args.fake_voice else VOICE}   Videos: {len(videos)}")

    for i, v in enumerate(videos, start=1):
        if args.only and i != args.only:
            continue
        mp4, dur = make_video(i, v, Path(args.out), font_path, args.fake_voice)
        note = "  <-- over 60s, consider trimming" if dur > 60 else ""
        print(f"[{i}/{len(videos)}] {mp4}  ({dur:.1f}s){note}")


if __name__ == "__main__":
    main()
