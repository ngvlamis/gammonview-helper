# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Nicholas Vlamis

"""Draw the menu-bar icons in `gvhelper/resources/`.

A doubling cube's face showing 2, as macOS template images: black and alpha
only, so the menu bar can tint them for light and dark. Drawn at 10x and scaled
down to 40x40 (20pt at 2x). Paused is the same shape at lower alpha.

A 2 rather than the 64 a cube rests on: at menu-bar size "64" in a rounded box
reads as the battery's percentage.

    uv run --with pillow python scripts/menubar-icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SCALE, SIZE = 10, 40
FACE = (2, 2, 38, 38)
RADIUS = 6.5
DIGIT = 28
PAUSED_ALPHA = 114
FONT = "/System/Library/Fonts/Helvetica.ttc"  # index 1 is Bold

OUT = Path(__file__).resolve().parent.parent / "gvhelper" / "resources"


def draw(alpha: int) -> Image.Image:
    big = Image.new("L", (SIZE * SCALE, SIZE * SCALE), 0)
    d = ImageDraw.Draw(big)
    x0, y0, x1, y1 = [v * SCALE for v in FACE]
    d.rounded_rectangle((x0, y0, x1, y1), radius=RADIUS * SCALE, fill=255)
    font = ImageFont.truetype(FONT, DIGIT * SCALE, index=1)
    left, top, right, bottom = d.textbbox((0, 0), "2", font=font)
    d.text(
        ((x0 + x1 - (right - left)) / 2 - left, (y0 + y1 - (bottom - top)) / 2 - top),
        "2",
        font=font,
        fill=0,
    )
    mask = big.resize((SIZE, SIZE), Image.LANCZOS).point(lambda v: v * alpha // 255)
    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.putalpha(mask)
    return icon


if __name__ == "__main__":
    draw(255).save(OUT / "menubar.png")
    draw(PAUSED_ALPHA).save(OUT / "menubar-paused.png")
