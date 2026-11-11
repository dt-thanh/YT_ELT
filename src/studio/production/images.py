"""
Shot images. Square, so ONE image serves both the 16:9 long cut and the 9:16 Short:
the subject stays inside the centre square that both crops keep (spec §5, D10).

`placeholder_image` draws a labelled card instead of an AI picture (pilot, owner's
choice 2026-11-11): it tests the whole pipeline - timing, motion, Short cut,
subtitles - before an image tool is chosen (M-07). The card shows the prompt an image
model would get, so the pilot doubles as a storyboard.
"""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

from studio.schemas import Shot, StoryBible

SIDE = 2160                      # square source; the 16:9 and 9:16 crops share the centre
SAFE = SIDE * 9 // 16            # 1215: the centre square both crops keep

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu",
              "/usr/local/share/fonts")


def image_prompt(shot: Shot, bible: StoryBible) -> str:
    """The prompt an image model gets (M-07): fixed style, the scene, the characters'
    fixed looks, fixed negatives."""
    looks = "; ".join(f"{c.name}: {c.look}" for c in bible.characters if c.id in shot.characters)
    parts = [bible.style_prefix, shot.image_prompt, looks]
    prompt = ". ".join(p for p in parts if p)
    return f"{prompt}. Tránh: {bible.negative}" if bible.negative else prompt


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size)


def _colour(key: str) -> tuple[int, int, int]:
    h = hashlib.sha256(key.encode()).digest()
    return (30 + h[0] % 70, 30 + h[1] % 70, 45 + h[2] % 80)


def placeholder_image(shot: Shot, bible: StoryBible, index: int, out: Path) -> Path:
    from PIL import Image, ImageDraw

    top, bottom = _colour(shot.place), _colour(shot.place + shot.shot_id)
    img = Image.new("RGB", (SIDE, SIDE))
    draw = ImageDraw.Draw(img)
    for y in range(SIDE):                                    # vertical gradient
        f = y / SIDE
        draw.line([(0, y), (SIDE, y)], fill=tuple(int(a + (b - a) * f) for a, b in zip(top, bottom)))

    lo = (SIDE - SAFE) // 2
    guide = (120, 124, 140)
    draw.rectangle([lo, 0, lo + SAFE, SIDE - 1], outline=guide, width=2)    # 9:16 crop
    draw.rectangle([0, lo, SIDE - 1, lo + SAFE], outline=guide, width=2)    # 16:9 crop

    # Text stays in the middle of the shared square: the slow zoom (up to 12 %) eats the
    # edges, and the top band is where the subtitles put the place and year.
    x, y = lo + SAFE // 10, lo + SAFE * 22 // 100
    draw.text((x, y), f"CẢNH {index} · {shot.kind.upper()}", font=_font(44, True), fill=(255, 214, 120))
    y += 70
    for line in textwrap.wrap(shot.place, 26):
        draw.text((x, y), line, font=_font(56, True), fill=(255, 255, 255))
        y += 70
    y += 20
    for line in textwrap.wrap(shot.image_prompt, 40)[:8]:
        draw.text((x, y), line, font=_font(36), fill=(230, 230, 230))
        y += 48
    names = [bible.character(c).name for c in shot.characters]
    if names:
        y += 20
        draw.text((x, y), "Nhân vật: " + ", ".join(names), font=_font(34, True), fill=(180, 220, 255))
    draw.text((x, lo + SAFE * 82 // 100), "ẢNH GIẢ LẬP · thay bằng ảnh AI (M-07)",
              font=_font(26), fill=(200, 200, 200))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out
