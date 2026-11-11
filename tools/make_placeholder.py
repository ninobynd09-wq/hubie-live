#!/usr/bin/env python3
"""Draws the placeholder scene art. Swap by pointing HUBIE_ART_DIR at a folder holding the same
three files: background.png (1280x720), hubie_closed.png and hubie_open.png (same size, transparent)."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "assets/placeholder"
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def background() -> None:
    im = Image.new("RGB", (1280, 720), (13, 26, 20))
    d = ImageDraw.Draw(im)
    for y in range(720):
        d.line([(0, y), (1280, y)], fill=(13, 26 + y // 24, 20 + y // 40))
    panel = dict(radius=14, fill=(8, 16, 13), outline=(44, 84, 64), width=2)
    d.rounded_rectangle((20, 20, 820, 470), **panel)     # game area
    d.rounded_rectangle((840, 20, 1260, 330), **panel)   # chat
    d.rounded_rectangle((20, 485, 930, 675), **panel)    # caption
    small = ImageFont.truetype(BOLD, 15)
    d.text((36, 30), "GAME AREA  (placeholder, fake demo source)", font=small, fill=(90, 130, 110))
    d.text((856, 30), "CHAT", font=small, fill=(90, 130, 110))
    im.save(OUT / "background.png")


def hubie(mouth_open: bool) -> Image.Image:
    im = Image.new("RGBA", (300, 340), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    fur, face, ink = (122, 98, 72, 255), (206, 184, 150, 255), (28, 22, 18, 255)
    d.rounded_rectangle((70, 235, 230, 340), radius=30, fill=(238, 238, 232, 255))   # white suit
    d.ellipse((60, 70, 240, 245), fill=fur)
    d.ellipse((85, 105, 215, 225), fill=face)
    d.ellipse((102, 128, 142, 168), fill=fur)
    d.ellipse((158, 128, 198, 168), fill=fur)
    d.ellipse((114, 140, 130, 156), fill=ink)
    d.ellipse((170, 140, 186, 156), fill=ink)
    d.ellipse((140, 165, 160, 180), fill=ink)
    if mouth_open:
        d.ellipse((130, 186, 170, 216), fill=(70, 22, 22, 255))
    else:
        d.arc((125, 175, 175, 205), 25, 155, fill=ink, width=4)
    d.rectangle((62, 62, 238, 76), fill=ink)                 # top hat
    d.rectangle((95, 6, 205, 64), fill=ink)
    d.rectangle((95, 46, 205, 56), fill=(170, 30, 30, 255))
    d.polygon([(150, 248), (118, 234), (118, 262)], fill=ink)  # bow tie
    d.polygon([(150, 248), (182, 234), (182, 262)], fill=ink)
    d.text((104, 316), "placeholder", font=ImageFont.truetype(BOLD, 13), fill=(90, 90, 90, 255))
    return im


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    background()
    hubie(False).save(OUT / "hubie_closed.png")
    hubie(True).save(OUT / "hubie_open.png")
