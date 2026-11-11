"""Look MIX (HUBIE_LOOK=mix, owner pick 2026-11-09, docs/screenshot_live.png): setup 10's rainy neon alley behind
everything, Hubie's desk cam top left in a pink neon rim (Kick-green LIVE · AI badge, "hubie / night shift" plate on
its bottom edge), a dark glass chat panel under it, the game top right in a teal neon rim, under the game a white
notification card with his current line ("hubie  now", avatar) and a row of white pills (live time, rounds, best, the
game's name, Demo play), and setup 06's dark safety strip at the bottom.

Everything is drawn at 1280x720 and scaled for other sizes. The static part is one RGBA picture (frame.png) with
rounded holes for the game and the cam; the pills, the chat list and his line are drawn over it (the pills as a
transparent layer, the chat cut from the flattened frame like look H). Cosmetic: nothing here may raise into the stream.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ASSETS = Path(__file__).resolve().parents[2] / "assets/look_mix"
PLATE = ASSETS / "plate_mix.png"
AVATAR = ASSETS / "avatar.png"
FD = ASSETS / "fonts"
FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONTS = {"ib": FD / "Inter_28pt-Bold.ttf", "is": FD / "Inter_28pt-SemiBold.ttf", "im": FD / "Inter_28pt-Medium.ttf",
         "sb": FD / "SpaceGrotesk-Bold.ttf", "sr": FD / "SpaceGrotesk-Regular.ttf", "sm": FD / "SpaceGrotesk-Medium.ttf",
         "px": Path("/root/.fonts/Poppins-ExtraBold.ttf")}

W, H = 1280, 720
TEAL = (64, 224, 208)
PINK = (255, 92, 170)
NIGHT = (8, 12, 26)
MUTE = (150, 160, 190)
WHITE = (240, 245, 248)
KICK = (83, 252, 24)
ICE = (150, 220, 255)
# the white glass (setup 09's card and pills)
INK = (16, 30, 48)
GREY = (110, 122, 136)
BEST = (0, 150, 150)
GLASS = (250, 252, 253)
GLASS_A = 0.93
DP_FILL, DP_EDGE, DP_TXT = (220, 244, 242), (120, 210, 204), (0, 150, 150)
# chat (setup 10)
CHAT_TEXT = (226, 230, 240)
HUB_TEXT = (214, 250, 246)
CHAT_HINT = (96, 108, 140)
NAME_COLOURS = [(120, 200, 255), PINK, (150, 240, 140), (255, 180, 90), (190, 160, 255), (240, 240, 120),
                (255, 130, 120)]

# ---- boxes (x0, y0, x1, y1) at 1280x720 ----
GAME = (408, 24, 1256, 504)           # 848x480, ~1.77:1
CAM = (24, 24, 384, 264)              # 360x240 = the clip's own 3:2, nothing cut
CHATP = (24, 284, 384, 672)           # chat glass panel
CHAT_BODY = (28, 330, 380, 666)       # where the chat list is drawn (under the header divider)
CARD = (408, 522, 1256, 618)          # the notification card
PILLS_Y = 634                         # the pill row, 38 tall
PILL_LAYER = (392, 598, 1272, 686)    # the transparent pill layer (pills + their shadows)
VEIL = (488, 556, 1248, 616)          # his line inside the card (dimmed between lines)
LINE_X = 494                          # his line starts here (after the avatar)
LINE_W = 738
LINE_SIZE = 23
LINE_BASE = {"sub_top": 582, "sub_bot": 609, "sub_mid": 591}   # baselines: two lines / one line
STRIP_Y, STRIP_H = 686, 34
PLATE_Y = 250                          # the "hubie / night shift" plate: on the cam's bottom edge, clear of the paw


@lru_cache(maxsize=64)
def font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS[name]), size)
    except OSError:
        return ImageFont.truetype(FALLBACK, size)


def name_colour(user: str) -> tuple:
    h = int(hashlib.md5(str(user or "").lower().encode("utf-8")).hexdigest()[:8], 16)
    return NAME_COLOURS[h % len(NAME_COLOURS)]


# ---------- drawing helpers ----------

def _rmask(w: int, h: int, r: float) -> Image.Image:
    m = Image.new("L", (w * 3, h * 3), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * 3 - 1, h * 3 - 1), r * 3, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _shadow(dst: Image.Image, box, r: float, blur: int, strength: int, colour=(0, 0, 0)) -> None:
    """Soft drop shadow composited onto an RGBA image."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    pad = blur * 3
    m = Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0), r, fill=strength)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    sh = Image.new("RGBA", m.size, colour + (0,))
    sh.putalpha(m)
    dst.alpha_composite(sh, (x0 - pad, y0 - pad))


def _glass(dst: Image.Image, box, r: float, tint, a: float, blur: int = 16, src: Image.Image | None = None) -> None:
    """Frosted glass: what is behind (src, default dst), blurred and tinted, pasted in a rounded box."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    src = dst if src is None else src
    m = blur * 2
    big = src.crop((x0 - m, y0 - m, x1 + m, y1 + m)).convert("RGB").filter(ImageFilter.GaussianBlur(blur))
    crop = big.crop((m, m, m + x1 - x0, m + y1 - y0))
    crop = Image.blend(crop, Image.new("RGB", crop.size, tuple(tint)), a).convert("RGBA")
    crop.putalpha(_rmask(x1 - x0, y1 - y0, r))
    dst.alpha_composite(crop, (x0, y0))


def _rrect(dst: Image.Image, box, r: float, fill=None, a: int = 255, outline=None, width: int = 1) -> None:
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    k = 3
    lay = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    ImageDraw.Draw(lay).rounded_rectangle((0, 0, w * k - 1, h * k - 1), r * k,
                                          fill=(tuple(fill) + (a,)) if fill else None,
                                          outline=(tuple(outline) + (255,)) if outline else None,
                                          width=width * k if outline else 0)
    dst.alpha_composite(lay.resize((w, h), Image.LANCZOS), (x0, y0))


def _neon(dst: Image.Image, box, r: float, col, a: int = 230, glow_a: int = 120) -> None:
    """A thin neon line with a soft glow both ways (over the video inside the hole as well)."""
    g = Image.new("L", (W, H), 0)
    ImageDraw.Draw(g).rounded_rectangle(box, r, outline=255, width=4)
    g = g.filter(ImageFilter.GaussianBlur(7)).point(lambda v: v * glow_a // 255)
    lay = Image.new("RGBA", (W, H), tuple(col) + (0,))
    lay.putalpha(g)
    dst.alpha_composite(lay)
    s = 3
    hi = Image.new("L", (W * s, H * s), 0)
    ImageDraw.Draw(hi).rounded_rectangle([v * s for v in box], r * s, outline=a, width=2 * s)
    lay = Image.new("RGBA", (W, H), tuple(col) + (0,))
    lay.putalpha(hi.resize((W, H), Image.LANCZOS))
    dst.alpha_composite(lay)


def _vignette(im: Image.Image, k: float = 0.3, cy: float = 0.45) -> Image.Image:
    import numpy as np
    yy, xx = np.mgrid[0:im.height, 0:im.width].astype(np.float32)
    v = 1 - k * np.clip(((xx - im.width / 2) / (im.width / 2)) ** 2 + ((yy - im.height * cy) / (im.height * 0.6)) ** 2
                        - 0.2, 0, 1)
    return Image.fromarray((np.asarray(im) * v[..., None]).astype(np.uint8))


def avatar(size: int) -> Image.Image | None:
    try:
        im = Image.open(AVATAR).convert("RGBA").resize((size, size), Image.LANCZOS)
    except OSError:
        return None
    m = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    im.putalpha(ImageChops.multiply(im.getchannel("A"), m.resize((size, size), Image.LANCZOS)))
    return im


def live_badge(dst: Image.Image, xy, f, h: int = 24, padx: int = 10) -> tuple:
    d = ImageDraw.Draw(dst)
    t = "LIVE · AI"
    dw = int(f.size * 0.45)
    w = int(d.textlength(t, font=f) + 2 * padx + dw + 6)
    x, y = xy
    _rrect(dst, (x, y, x + w, y + h), h // 2, fill=KICK)
    d = ImageDraw.Draw(dst)
    ink = (10, 14, 10, 255)
    d.ellipse((x + padx, y + h / 2 - dw / 2, x + padx + dw, y + h / 2 + dw / 2), fill=ink)
    d.text((x + padx + dw + 6, y + h / 2 + 1), t, font=f, fill=ink, anchor="lm")
    return x, y, x + w, y + h


# ---------- the static layer ----------

def _background() -> Image.Image:
    try:
        im = Image.open(PLATE).convert("RGB").resize((W, H), Image.LANCZOS)
    except OSError:
        im = Image.new("RGB", (W, H), NIGHT)
    im = im.filter(ImageFilter.GaussianBlur(1.5))
    im = Image.eval(im, lambda v: int(v * 0.85))
    return _vignette(im, 0.3)


@lru_cache(maxsize=2)
def frame_1280() -> Image.Image:
    """The full static layer at 1280x720 (RGBA): background, shadows, neon rims, chat glass + header, the white card
    with avatar and "hubie  now", LIVE · AI and the name plate; transparent rounded holes for the game and the cam."""
    img = _background().convert("RGBA")
    _shadow(img, GAME, 12, blur=22, strength=220)
    _shadow(img, CAM, 12, blur=18, strength=220)
    _shadow(img, CHATP, 12, blur=16, strength=200)
    # chat glass
    _glass(img, CHATP, 12, NIGHT, 0.72, blur=14)
    _rrect(img, CHATP, 12, outline=(60, 70, 110))
    d = ImageDraw.Draw(img)
    d.text((CHATP[0] + 16, CHATP[1] + 22), "CHAT", font=font("sb", 13), fill=TEAL + (255,), anchor="lm")
    d.text((CHATP[2] - 16, CHATP[1] + 22), "answered by name", font=font("sr", 12), fill=MUTE + (255,), anchor="rm")
    d.line((CHATP[0] + 16, CHATP[1] + 40, CHATP[2] - 16, CHATP[1] + 40), fill=(50, 60, 96, 255))
    # the white notification card
    _shadow(img, CARD, 22, blur=20, strength=120)
    _glass(img, CARD, 22, GLASS, GLASS_A, blur=22)
    _rrect(img, CARD, 22, outline=(255, 255, 255))
    av = avatar(52)
    if av is not None:
        img.alpha_composite(av, (CARD[0] + 18, CARD[1] + 22))
    d = ImageDraw.Draw(img)
    fb, fn = font("ib", 16), font("im", 14)
    d.text((CARD[0] + 86, CARD[1] + 26), "hubie", font=fb, fill=INK + (255,), anchor="lm")
    d.text((CARD[0] + 86 + d.textlength("hubie", font=fb) + 8, CARD[1] + 27), "now", font=fn, fill=GREY + (255,),
           anchor="lm")
    # holes for the game and the cam (the video tucks under the rims)
    a = img.getchannel("A")
    for b in (GAME, CAM):
        hole = Image.new("L", (W, H), 0)
        hole.paste(_rmask(b[2] - b[0], b[3] - b[1], 12), b[:2])
        a = ImageChops.subtract(a, hole)
    img.putalpha(a)
    _neon(img, (GAME[0] - 3, GAME[1] - 3, GAME[2] + 3, GAME[3] + 3), 14, TEAL)
    _neon(img, (CAM[0] - 3, CAM[1] - 3, CAM[2] + 3, CAM[3] + 3), 14, PINK, glow_a=100)
    live_badge(img, (CAM[0] + 12, CAM[1] + 12), font("sb", 13))
    # the name plate on the cam's bottom edge (inside the cam it would cover the paw on the mouse)
    pb = (CAM[0] + 12, PLATE_Y, CAM[0] + 168, PLATE_Y + 28)
    _shadow(img, pb, 6, blur=6, strength=160)
    _rrect(img, pb, 6, fill=NIGHT, a=235, outline=(120, 50, 95))
    d = ImageDraw.Draw(img)
    d.text((pb[0] + 12, pb[1] + 14), "hubie", font=font("px", 17), fill=WHITE + (255,), anchor="lm")
    d.text((pb[0] + 72, pb[1] + 15), "night shift", font=font("sm", 13), fill=PINK + (255,), anchor="lm")
    return img


def scaled(img: Image.Image, w: int, h: int) -> Image.Image:
    return img if (w, h) == img.size else img.resize((w, h), Image.LANCZOS)


# ---------- dynamic layers (drawn at 1280 units) ----------

def pill_values(lab_vals) -> list:
    return [(str(a), str(b), c) for a, b, c in lab_vals]


@lru_cache(maxsize=2)
def _pill_glass(chrome_key: int) -> Image.Image:
    """The background under the pill row, blurred and tinted white (what every pill's glass is cut from)."""
    base = _flat()
    m = 44
    x0, y0, x1, y1 = PILL_LAYER
    big = base.crop((x0 - m, y0 - m, x1 + m, y1 + m)).filter(ImageFilter.GaussianBlur(22))
    big = big.crop((m, m, m + x1 - x0, m + y1 - y0))
    return Image.blend(big, Image.new("RGB", big.size, GLASS), GLASS_A)


@lru_cache(maxsize=1)
def _flat() -> Image.Image:
    base = Image.new("RGBA", (W, H), (6, 16, 23, 255))
    base.alpha_composite(frame_1280())
    return base.convert("RGB")


def draw_pills(stats: list, game: str) -> Image.Image:
    """The pill row as a transparent layer (PILL_LAYER box): Live / Rounds / Best on the left, the game's name and
    Demo play on the right."""
    x0, y0, x1, y1 = PILL_LAYER
    img = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    glass = _pill_glass(0)
    py = PILLS_Y - y0
    fl, fv, fg, fd = font("im", 14), font("ib", 16), font("is", 15), font("ib", 14)
    d = ImageDraw.Draw(img)

    def pill(x, w):
        b = (x, py, x + w, py + 38)
        _shadow(img, b, 19, blur=10, strength=110)
        g = glass.crop((int(x), py, int(x + w), py + 38)).convert("RGBA")
        g.putalpha(_rmask(int(w), 38, 19))
        img.alpha_composite(g, (int(x), py))
        _rrect(img, b, 19, outline=(255, 255, 255))

    x = GAME[0] - x0
    for lab, val, col in stats:
        w = int(d.textlength(lab, font=fl) + d.textlength(val, font=fv) + 36)
        pill(x, w)
        d = ImageDraw.Draw(img)
        d.text((x + 16, py + 19), lab, font=fl, fill=GREY + (255,), anchor="lm")
        d.text((x + 22 + d.textlength(lab, font=fl), py + 19), val, font=fv, fill=tuple(col) + (255,), anchor="lm")
        x += w + 8
    left_end = x
    right = GAME[2] - x0
    dp = "Demo play"
    dw = int(d.textlength(dp, font=fd) + 32)
    _rrect(img, (right - dw, py + 2, right, py + 36), 17, fill=DP_FILL, outline=DP_EDGE)
    d = ImageDraw.Draw(img)
    d.text((right - dw / 2, py + 19), dp, font=fd, fill=DP_TXT + (255,), anchor="mm")
    gx1 = right - dw - 8
    room = gx1 - left_end - 8 - 32
    name = " ".join(str(game or "").split())
    while name and d.textlength(name, font=fg) > room:
        name = name[:-2].rstrip() + "…" if len(name) > 2 else ""
    if name:
        gw = int(d.textlength(name, font=fg) + 32)
        pill(gx1 - gw, gw)
        d = ImageDraw.Draw(img)
        d.text((gx1 - gw / 2, py + 19), name, font=fg, fill=INK + (255,), anchor="mm")
    return img


def chat_rows(name: str, text: str, width: int, max_lines: int = 3) -> list[str]:
    """A message as wrapped lines: the first line starts after "name  ", at most max_lines, "..." if cut."""
    fn, ft = font("sb", 15), font("sr", 15)
    lead = fn.getlength(name + "  ")
    room = lambda i: width - (lead if i == 0 else 0)
    lines, cur = [], ""
    for wd in " ".join(str(text or "").split()).split():
        t = f"{cur} {wd}".strip()
        if ft.getlength(t) <= room(len(lines)) or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    out = []
    for i, ln in enumerate(lines or [""]):
        while ft.getlength(ln) > room(i) and len(ln) > 1:
            ln = ln[:-1]
        out.append(ln)
    if len(out) > max_lines:
        out = out[:max_lines]
        last = out[-1]
        while ft.getlength(last + "...") > room(max_lines - 1) and len(last) > 1:
            last = last[:-1]
        out[-1] = last.rstrip(" ,.") + "..."
    return out


def draw_chat(entries: list[dict], empty_lines: tuple) -> Image.Image:
    """Chat list on the glass (cut from the flat frame): newest at the bottom, names coloured, his name teal."""
    x0, y0, x1, y1 = CHAT_BODY
    img = _flat().crop(CHAT_BODY)
    d = ImageDraw.Draw(img)
    Wd, Hd = img.size
    m = 12
    fn, ft, lh = font("sb", 15), font("sr", 15), 20
    width = Wd - 2 * m
    if not entries:
        f1, f2 = font("sb", 14), font("sr", 14)
        rows = [(empty_lines[0], f1)] + [(t, f2) for t in empty_lines[1:]]
        yy = Hd - 16 - len(rows) * 21
        for t, f in rows:
            d.text(((Wd - f.getlength(t)) / 2, yy), t, font=f, fill=CHAT_HINT)
            yy += 21
        return img
    y = Hd - 8
    for e in reversed(entries):
        hub = bool(e.get("hubie"))
        name = "hubie" if hub else (str(e.get("name") or "friend")[:24] or "friend")
        rows = chat_rows(name, str(e.get("text") or ""), width)
        h = len(rows) * lh
        if y - h < 2:
            break
        y -= h
        nc = TEAL if hub else name_colour(e.get("user") or name)
        tc = HUB_TEXT if hub else CHAT_TEXT
        lead = fn.getlength(name + "  ")
        for i, ln in enumerate(rows):
            cy = y + i * lh + lh / 2
            if i == 0:
                d.text((m, cy), name, font=fn, fill=nc, anchor="lm")
                d.text((m + lead, cy), ln, font=ft, fill=tc, anchor="lm")
            else:
                d.text((m, cy), ln, font=ft, fill=tc, anchor="lm")
        y -= 6
    return img


def draw_veil(alpha: int) -> Image.Image:
    img = _flat().crop(VEIL).convert("RGBA")
    img.putalpha(alpha)
    return img
