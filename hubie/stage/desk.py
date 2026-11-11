"""The desk layout for the browser-free scene. Three looks (HUBIE_LOOK):

- mix (default since 2026-11-09, docs/screenshot_live.png): rainy neon alley, cam top left, chat under it, game
  top right, a white notification card with his line and a row of white stat pills under the game (look_mix.py).
- h (approved 2026-11-08, qa/look_1108/preview_H.png): the Penthouse Lounge room behind everything, the game
  in a rounded teal window on the left, Hubie's desk cam top right (Kick-green LIVE · AI badge, navy name pill on its
  bottom edge), a frosted navy chat pane under it (game name + DEMO PLAY, named chat lines, live / rounds / best), and
  his line in a speech bubble under the game that stays up between lines, dimmed. The room, glass and borders are
  one static picture (assets/look_h/frame_top_H.png, holes for the game and cam) laid over the game and cam.
- desk1: the earlier flat navy desk layout (game top left, cam / chat / stats column, subtitle plate), for rollback.

Static parts are drawn once into PNGs. The parts that change (stats, subtitles, chat list) are small
opaque PNGs that ffmpeg re-reads a few times a second; this module redraws them only on events.
The cam is two video loops decoded side by side all the time, so neither restarts; a tiny grey
image (mix.png) is the talk loop's alpha, and fading it swaps idle and talking without a pop.
Cosmetic: nothing here may raise into the stream."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..audio import lipsync
from . import look_mix as MX

log = logging.getLogger("hubie.desk")

POPPINS = "/root/.fonts/Poppins-Bold.ttf"
POPPINS_X = "/root/.fonts/Poppins-ExtraBold.ttf"
UBUNTU = "/usr/share/fonts/truetype/ubuntu/Ubuntu-M.ttf"
FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

BASE = (6, 16, 23)
PANEL = (10, 24, 33)
PANEL_EDGE = (28, 64, 72)
TEAL = (64, 224, 208)
GOLD = (240, 190, 60)
WHITE = (236, 244, 247)
GREY = (132, 160, 172)
RED = (255, 72, 72)
MIX_LEVELS = 6            # mix_0.png (idle) .. mix_6.png (talking)
SUB_FPS, PANEL_FPS, CARD_FPS, CHAT_FPS = 30, 2, 4, 5
CHAT_KEEP = 40            # chat entries kept (only the newest that fit are drawn)
CHAT_MAX_LINES = 3        # a message wraps to at most this many lines
CHAT_HINT = (74, 100, 112)  # the empty chat list's dim hint
CHAT_EMPTY = ("chat's quiet.", "say hi, hubie answers", "everyone by name")
HUBIE_ROW = (44, 36, 12)  # the gold-tinted backing of his own lines in the chat list
# Viewer name colours, picked by a hash of the username so a viewer keeps theirs; all readable on the navy panel,
# none of them gold (his) or the header teal.
NAME_COLOURS = [(110, 190, 255), (255, 130, 190), (140, 230, 120), (255, 165, 80), (185, 150, 255),
                (225, 240, 110), (255, 120, 110)]
HB_NAVY = (11, 25, 32)    # the Housebets site's own header colour
ZERO_RED = (232, 98, 98)
BACK_GREY = (176, 194, 204)
HUBIE_ART = Path(__file__).resolve().parents[2] / "assets/official/hubie-240.png"
# ---- look H ----
H_FRAME = Path(__file__).resolve().parents[2] / "assets/look_h/frame_top_H.png"   # room + glass + borders, 1280x720
KICK_GREEN = (83, 252, 24)    # #53FC18
H_PILL_Y = 270               # the name pill's top: 8 px over the cam window's bottom edge (278)
H_INK = (8, 20, 28)           # dark text on the green badge
TEAL_D = (24, 120, 116)
H_LABEL = (132, 160, 172)     # footer labels
H_PILL_SUB = (150, 176, 186)
H_HUB_TEXT = (200, 240, 236)  # his own chat lines, a touch of teal
H_DIVIDER = (30, 72, 82)
H_DIM_ALPHA = 115             # the veil over his last line between lines: text shows at ~55%
H_SUB_SIZE = 25
H_STRIP_FILL = "0x03080c"
H_STRIP_LINE = "0x185458"
# the house originals (the adapter zooms their page to 0.9); everything else is a slot in the provider iframe
ORIGINAL_NAMES = ("Hubie's Road", "Mines", "Dice", "Limbo", "Plinko")
# What part of the 1280x720 game display goes into the game window (x, y, w, h), each about the window's aspect:
# originals = the game panel at its zoom (0.9, dice 0.85) down to its "Total profit" row (site top bar, icon rail and footer bar cut);
# slots = the 16:9 provider iframe under the site's top bar (HUBIE_HB_TOPBAR=1: y 80..720). Both start below the top
# bar (rows 0..79, where the account balance sits), so it can never be in the picture.
VIEW_ORIGINAL = (108, 86, 1116, 588)
VIEW_SLOT = (34, 80, 1212, 640)
# mix: the originals' panel at 0.9 zoom from just left of its controls (x 148) to past the plinko history column (x 1194),
# top bar cut, down to y 668: measured 2026-11-09 on all seven originals at 0.9, their play areas end at y 662-673 and
# the footer bar's buttons start lower (dice is the shortest: area to 662, its Fair button from ~670)
VIEW_ORIGINAL_MIX = (140, 80, 1066, 588)
# that region is ~1.81:1, so the picture is cut MIX_GAME_PAD px short of the window and padded at the bottom in the
# panel's own colour (what sits right under its play area anyway): nothing of the game is cut, no footer bar shows
MIX_GAME_PAD = 12
PANEL_BG = "0x0c1a22"     # the panel's own colour under its play area
MIX_ORIGINALS = ORIGINAL_NAMES + ("Keno", "Blackjack")
# two originals have shorter panels: their footer bar starts higher (limbo y 594, blackjack y 628), so their view stops
# above it (limbo's play area ends y 587, blackjack's 621) at the same aspect, a little narrower on the right where
# both are empty table (blackjack: the edge of the card shoe image top right)
VIEW_MIX_BY_GAME = {"limbo": (140, 80, 924, 510), "blackjack": (144, 80, 986, 544)}
RECENT_N = 6
# What the game area shows while it isn't a running game: (small label, big line when there's no game name, sub line)
CARD_TEXT = {
    "loading": ("SWITCHING GAME", "picking the next game", "loading, at sloth speed"),
    "recovering": ("ONE SEC", "resetting the table", "fixing the table, back in a moment"),
    "offline": ("TAKING A BREAK", "reels are resting", "still here, chat's open"),
}


LOOKS = ("mix", "h", "desk1")


def look_name() -> str:
    """HUBIE_LOOK: mix (default), h or desk1 (the earlier looks, for rollback)."""
    v = os.environ.get("HUBIE_LOOK", "mix").strip().lower()
    return v if v in LOOKS else "mix"


@dataclass(frozen=True)
class Geo:
    """Every box in pixels. Laid out in 1280x720 units and scaled, so 1080p is the same picture."""
    w: int
    h: int
    look: str = ""      # "mix", "h" or "desk1"; empty = HUBIE_LOOK

    def __post_init__(self):
        if self.look not in LOOKS:
            object.__setattr__(self, "look", look_name())

    @property
    def is_h(self) -> bool:
        return self.look == "h"

    @property
    def is_mix(self) -> bool:
        return self.look == "mix"

    @property
    def framed(self) -> bool:
        """H and mix: one static frame layer over the game and cam, his line stays up dimmed, game views."""
        return self.look in ("h", "mix")

    def box(self, b) -> tuple:
        """A look_mix (x0, y0, x1, y1) box as (x, y, w, h) at this size."""
        return self.s(b[0]), self.s(b[1]), self.s(b[2] - b[0]), self.s(b[3] - b[1])

    def s(self, v: float) -> int:
        return int(round(v * self.w / 1280))

    def even(self, v: float) -> int:
        return self.s(v) // 2 * 2

    @property
    def strip_h(self) -> int:
        return 34 if self.h <= 720 else self.s(34)

    @property
    def game(self):      # x, y, w, h
        if self.is_mix:
            x0, y0, x1, y1 = MX.GAME
            return self.even(x0), self.even(y0), self.even(x1 - x0), self.even(y1 - y0)
        if self.is_h:    # the rounded game window (frame hole 54,75 - 796,466); a row under the frame's top edge
            return self.even(54), self.even(74), self.even(742), self.even(392)
        return 0, 0, self.even(880), self.even(496)   # desk1: 16:9, about 69% of the width; nothing covers it

    @property
    def game_pad(self) -> int:
        """Rows at the bottom of the game window filled with the panel colour instead of the picture (mix only)."""
        return self.even(MIX_GAME_PAD) if self.is_mix else 0

    @property
    def cam(self):
        if self.is_mix:
            x0, y0, x1, y1 = MX.CAM
            return self.even(x0), self.even(y0), self.even(x1 - x0), self.even(y1 - y0)
        if self.is_h:    # the cam window (frame hole 846,75 - 1226,278)
            return self.even(846), self.even(74), self.even(380), self.even(204)
        cw, ch = self.even(376), self.even(250)       # desk1: 3:2 at the top of the right column
        return self.w - self.s(12) - cw, self.s(8), cw, ch

    @property
    def cam_cy(self) -> float:
        """Where the cam crop sits in the source's spare height (0 top, 1 bottom). The H window is wider than the
        clip, so it is cut lower: from the hat's H badge down to the mouse; the name pill overlaps the window's bottom
        edge by only 8 px so the paw and the mouse stay in view."""
        return 0.5 if self.framed else 0.3

    @property
    def pane(self):      # H: the frosted chat pane (header, chat, footer)
        return self.s(846), self.s(318), self.s(386), self.s(314)

    @property
    def frame_pad(self) -> int:
        return self.s(3)

    @property
    def panel(self):     # the stats block at the bottom of the right column, as wide as the cam frame
        if self.is_mix:  # mix: the transparent pill row under the card
            return self.box(MX.PILL_LAYER)
        if self.is_h:    # H: the whole pane (game name header + session footer); the chat is laid over its middle
            return self.pane
        cx, cy, cw, ch = self.cam
        p = self.frame_pad
        h = self.s(150)
        return cx - p, self.h - self.strip_h - self.s(6) - h, cw + 2 * p, h

    @property
    def chat(self):      # the live chat list between the cam and the stats
        if self.is_mix:
            return self.box(MX.CHAT_BODY)
        if self.is_h:    # H: the pane's body, between the header divider (y 364) and the footer divider (y 595)
            return self.s(848), self.s(366), self.s(382), self.s(226)
        cx, cy, cw, ch = self.cam
        p = self.frame_pad
        y = cy + ch + p + self.s(6)
        return cx - p, y, cw + 2 * p, self.panel[1] - self.s(6) - y

    @property
    def subs(self):
        gx, gy, gw, gh = self.game
        return 0, gh, gw, self.h - self.strip_h - gh

    @property
    def plate(self):     # the subtitle backing: two lines, at the top of the band under the game
        if self.is_mix:  # mix: the white notification card
            return self.box(MX.CARD)
        if self.is_h:    # H: the speech bubble under the game
            return self.s(54), self.s(490), self.s(742), self.s(116)
        sx, sy, sw, sh = self.subs
        w, h = self.even(840), self.even(100)
        return sx + (sw - w) // 2, sy + self.s(6), w, h

    @property
    def veil(self):      # H: the bubble's inside, where his line is written (the dimming veil covers it)
        if self.is_mix:
            return self.box(MX.VEIL)
        return self.s(62), self.s(498), self.s(726), self.s(100)

    @property
    def spins(self):     # the recent-spins strip, under the subtitles (desk1 only; H has no round-history row)
        if self.framed:
            return None
        sx, sy, sw, sh = self.subs
        px, py, pw, ph = self.plate
        y = py + ph + self.s(6)
        return px, y, pw, self.even(70)


def _font(path: str, size: int):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.truetype(FALLBACK, size)


def _save(img: Image.Image, path: Path) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.png")
    img.save(tmp, compress_level=1)
    os.replace(tmp, path)


def _write_text(path: Path, text: str) -> None:
    # drawtext re-reads this every frame: replace it atomically, never leave it empty
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(text.strip() or " ", encoding="utf-8")
    os.replace(tmp, path)


def _link(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.lnk")
    tmp.unlink(missing_ok=True)
    os.symlink(src, tmp)
    os.replace(tmp, dst)


# ---------- static art ----------

def draw_chrome(g: Geo) -> Image.Image:
    """Full-frame opaque base: everything that never changes. The game and cam cover their boxes."""
    img = Image.new("RGB", (g.w, g.h), BASE)
    d = ImageDraw.Draw(img)
    px, py, pw, ph = g.panel
    # faint teal edge where the game meets the side column and the subtitle band
    gx, gy, gw, gh = g.game
    d.line((gw, 0, gw, gh), fill=PANEL_EDGE, width=max(1, g.s(2)))
    d.line((0, gh, gw, gh), fill=PANEL_EDGE, width=max(1, g.s(2)))
    return img


def draw_camframe(g: Geo) -> Image.Image:
    """Teal border plus the LIVE · AI tag, transparent inside; drawn over the cam."""
    cx, cy, cw, ch = g.cam
    p = g.frame_pad
    img = Image.new("RGBA", (cw + 2 * p, ch + 2 * p), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = g.s(16)
    d.rounded_rectangle((p // 2, p // 2, cw + 2 * p - 1 - p // 2, ch + 2 * p - 1 - p // 2), r + p // 2,
                        outline=TEAL + (255,), width=max(2, g.s(2)))
    f = _font(POPPINS, g.s(13))
    label = "LIVE · AI"
    tw = d.textlength(label, font=f)
    x0, y0 = p + g.s(10), p + g.s(10)
    d.rounded_rectangle((x0, y0, x0 + tw + g.s(30), y0 + g.s(24)), g.s(7), fill=(4, 12, 18, 225))
    d.ellipse((x0 + g.s(9), y0 + g.s(8), x0 + g.s(17), y0 + g.s(16)), fill=RED + (255,))
    d.text((x0 + g.s(22), y0 + g.s(12)), label, font=f, fill=WHITE + (255,), anchor="lm")
    return img


def draw_cammask(g: Geo) -> Image.Image:
    cx, cy, cw, ch = g.cam
    m = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, cw - 1, ch - 1), g.s(16), fill=255)
    return m


# ---------- look H static art ----------

def _aa_rrect(dst: Image.Image, box, r: float, fill=None, outline=None, width: int = 1) -> None:
    """Anti-aliased rounded box alpha-composited onto an RGBA image (drawn 3x and scaled down)."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    w, h = x1 - x0, y1 - y0
    k = 3
    lay = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
    ImageDraw.Draw(lay).rounded_rectangle((0, 0, w * k - 1, h * k - 1), r * k,
                                          fill=(tuple(fill) + (255,)) if fill else None,
                                          outline=(tuple(outline) + (255,)) if outline else None,
                                          width=width * k if outline else 0)
    dst.alpha_composite(lay.resize((w, h), Image.LANCZOS), (x0, y0))


def _soft_shadow(dst: Image.Image, box, r: float, blur: int = 6, strength: int = 200) -> None:
    from PIL import ImageFilter
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    pad = blur * 3
    m = Image.new("L", (x1 - x0 + 2 * pad, y1 - y0 + 2 * pad), 0)
    ImageDraw.Draw(m).rounded_rectangle((pad, pad, pad + x1 - x0, pad + y1 - y0), r, fill=strength)
    m = m.filter(ImageFilter.GaussianBlur(blur))
    sh = Image.new("RGBA", m.size, (0, 0, 0, 0))
    sh.putalpha(m)
    dst.alpha_composite(sh, (x0 - pad, y0 - pad))


def live_badge_h(dst: Image.Image, xy, size: int = 13) -> tuple:
    """Kick-green pill with a dark dot and dark 'LIVE · AI'. Returns its box."""
    d = ImageDraw.Draw(dst)
    f = _font(POPPINS, size)
    t = "LIVE · AI"
    h = size + 12
    w = int(d.textlength(t, font=f) + 2 * 10 + size * 0.7 + 4)
    x, y = xy
    _aa_rrect(dst, (x, y, x + w, y + h), h // 2, fill=KICK_GREEN)
    d = ImageDraw.Draw(dst)
    cx, cy, rr = x + 10 + size * 0.35, y + h / 2, size * 0.28
    d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=H_INK + (255,))
    d.text((x + 10 + size * 0.7 + 4, y + h / 2 + 1), t, font=f, fill=H_INK + (255,), anchor="lm")
    return x, y, x + w, y + h


def draw_frame_h(g: Geo) -> Image.Image:
    """Look H's full-frame top layer (RGBA): the room, the glass pane and bubble, the teal borders, with rounded
    holes for the game and the cam; plus the static marks: LIVE · AI on the cam, the name pill on its bottom edge,
    the DEMO PLAY tag in the pane header. Drawn at 1280x720 and scaled for other sizes."""
    img = Image.open(H_FRAME).convert("RGBA")
    if img.size != (1280, 720):
        img = img.resize((1280, 720), Image.LANCZOS)
    cam = (846, 75, 1226, 278)
    pane = (846, 318, 1232, 632)
    live_badge_h(img, (cam[0] + 10, cam[1] + 10), 13)
    d = ImageDraw.Draw(img)
    f, fs = _font(POPPINS_X, 19), _font(UBUNTU, 13)
    t1, t2 = "hubie", "ai host, awake (mostly)"
    pw = int(d.textlength(t1, font=f) + d.textlength(t2, font=fs) + 42)
    cx = (cam[0] + cam[2]) // 2
    px0, py0 = cx - pw // 2, H_PILL_Y
    _soft_shadow(img, (px0, py0, px0 + pw, py0 + 30), 15, blur=6, strength=200)
    _aa_rrect(img, (px0, py0, px0 + pw, py0 + 30), 15, fill=(8, 22, 31), outline=(52, 170, 162))
    d = ImageDraw.Draw(img)
    d.text((px0 + 16, py0 + 15), t1, font=f, fill=WHITE + (255,), anchor="lm")
    d.text((px0 + 26 + d.textlength(t1, font=f), py0 + 16), t2, font=fs, fill=H_PILL_SUB + (255,), anchor="lm")
    dp, fd = "DEMO PLAY", _font(POPPINS, 12)
    dw = d.textlength(dp, font=fd) + 18
    _aa_rrect(img, (pane[2] - 12 - dw, pane[1] + 13, pane[2] - 12, pane[1] + 33), 10, fill=(12, 48, 54), outline=TEAL_D)
    d = ImageDraw.Draw(img)
    d.text((pane[2] - 12 - dw / 2, pane[1] + 23.5), dp, font=fd, fill=TEAL + (255,), anchor="mm")
    if (g.w, g.h) != (1280, 720):
        img = img.resize((g.w, g.h), Image.LANCZOS)
    return img


def h_tag_width(g: Geo) -> int:
    """The DEMO PLAY tag's width plus margins: the header's game name stops before it."""
    f = _font(POPPINS, 12)
    return g.s(f.getlength("DEMO PLAY") + 18 + 12 + 14)


def draw_chrome_h(g: Geo) -> Image.Image:
    """The H top layer flattened onto the base colour: what every opaque text layer is cut from, so it matches."""
    base = Image.new("RGBA", (g.w, g.h), BASE + (255,))
    base.alpha_composite(draw_frame_h(g))
    return base.convert("RGB")


def draw_veil(g: Geo, chrome: Image.Image) -> Image.Image:
    """The bubble's own glass at ~45% over his last line between lines, so it reads dimmed (~55%)."""
    if g.is_mix:
        return MX.scaled(MX.draw_veil(H_DIM_ALPHA), *g.veil[2:])
    x, y, w, h = g.veil
    img = chrome.crop((x, y, x + w, y + h)).convert("RGBA")
    img.putalpha(H_DIM_ALPHA)
    return img


def draw_frame_mix(g: Geo) -> Image.Image:
    return MX.scaled(MX.frame_1280(), g.w, g.h)


def draw_chrome_mix(g: Geo) -> Image.Image:
    return MX.scaled(MX._flat(), g.w, g.h)


def chrome_for(g: Geo) -> Image.Image:
    if g.is_mix:
        return draw_chrome_mix(g)
    return draw_chrome_h(g) if g.is_h else draw_chrome(g)


def frame_for(g: Geo) -> Image.Image:
    return draw_frame_mix(g) if g.is_mix else draw_frame_h(g)


def is_original(game: str, names=ORIGINAL_NAMES) -> bool:
    return " ".join(str(game or "").split()).lower() in {n.lower() for n in names}


def view_kind(g: Geo, state: str, game: str) -> str:
    """Which game view a screen state gets: "slot" (the iframe / site page under the top bar) or "original"."""
    names = MIX_ORIGINALS if g.is_mix else ORIGINAL_NAMES
    if state == "browsing" or not is_original(game, names):
        return "slot"
    key = " ".join(str(game or "").split()).lower()
    return f"original:{key}" if (g.is_mix and key in VIEW_MIX_BY_GAME) else "original"


def game_view(g: Geo, kind: str, src_w: int = 1280, src_h: int = 720) -> tuple[int, int, int, int]:
    """How the game display goes into the H window: (scaled width, scaled height, crop x, crop y). The whole source
    is scaled so the view region is the window's width (an even size), then a window-sized crop is cut at the region's
    corner. Changing the view at run time only changes the scale size and the crop corner, never the crop size."""
    _, _, gw, gh = g.game
    gh -= g.game_pad
    if kind.startswith("original:") and g.is_mix:
        rx, ry, rw, rh = VIEW_MIX_BY_GAME.get(kind.split(":", 1)[1], VIEW_ORIGINAL_MIX)
    elif kind.startswith("original"):
        rx, ry, rw, rh = VIEW_ORIGINAL_MIX if g.is_mix else VIEW_ORIGINAL
    else:
        rx, ry, rw, rh = VIEW_SLOT
    f = src_w / 1280
    rx, ry, rw, rh = rx * f, ry * f, rw * f, rh * f
    k = max(gw / rw, gh / rh)
    sw, sh = int(math.ceil(src_w * k / 2)) * 2, int(math.ceil(src_h * k / 2)) * 2
    x = min(max(0, int(round(rx * k + (rw * k - gw) / 2))), sw - gw)
    y = min(max(0, int(math.ceil(ry * k))), sh - gh)
    return sw, sh, x, y


def view_cmds(g: Geo, kind: str, src_w: int = 1280, src_h: int = 720) -> dict[str, str]:
    sw, sh, x, y = game_view(g, kind, src_w, src_h)
    return {"gz_w": f"scale@gz w {sw}", "gz_h": f"scale@gz h {sh}", "gc_x": f"crop@gc x {x}", "gc_y": f"crop@gc y {y}"}


# ---------- dynamic layers (opaque, cut from the chrome so they blend in) ----------

def _cut(chrome: Image.Image, box) -> Image.Image:
    x, y, w, h = box
    return chrome.crop((x, y, x + w, y + h))


def _wrap_px(text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        t = f"{cur} {wd}".strip()
        if font.getlength(t) <= width or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    out = []
    for ln in lines:   # a single word wider than the box gets cut, never overflows
        while font.getlength(ln) > width and len(ln) > 1:
            ln = ln[:-1]
        out.append(ln)
    return out


def sub_font(g: Geo):
    if g.is_mix:
        return _font(str(MX.FONTS["is"]), g.s(MX.LINE_SIZE))
    return _font(UBUNTU, g.s(H_SUB_SIZE if g.is_h else 34))


def sub_width(g: Geo) -> int:
    if g.is_mix:
        return g.s(MX.LINE_W)
    return g.s(700) if g.is_h else g.plate[2] - g.s(48)


def pages(g: Geo, text: str, lines: int = 2) -> list[str]:
    """Subtitle pages of at most two lines each."""
    wrapped = _wrap_px(" ".join((text or "").split()), sub_font(g), sub_width(g))
    return ["\n".join(wrapped[i:i + lines]) for i in range(0, len(wrapped), lines)] or [""]


def draw_plate(g: Geo) -> Image.Image:
    """Dark rounded backing for the subtitles; the encoder shows it only while he speaks."""
    x, y, w, h = g.plate
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w - 1, h - 1), g.s(14), fill=(13, 31, 41, 255),
                                          outline=(30, 70, 78, 255), width=1)
    return img


def sub_lines(page: str) -> dict:
    """What the three subtitle text slots hold for a page: two lines top/bottom, one line in the middle."""
    lines = [ln for ln in (page or "").split("\n") if ln.strip()][:2]
    if len(lines) == 2:
        return {"sub_top": lines[0], "sub_bot": lines[1], "sub_mid": ""}
    return {"sub_top": "", "sub_bot": "", "sub_mid": lines[0] if lines else ""}


def fmt_hms(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def fmt_money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def draw_panel(g: Geo, chrome: Image.Image, st: dict, now: float) -> Image.Image:
    """Stats block: TIME LIVE, ROUNDS / BEST MULTI, DEMO BALANCE in a 2x2 grid, then the game and the segment label
    on their own lines under a divider, so nothing collides with the values."""
    if g.is_mix:
        img = MX.draw_pills(h_footer_mix(st, now), h_game_title(st))
        return MX.scaled(img, *g.panel[2:])
    if g.is_h:
        return draw_panel_h(g, chrome, st, now)
    img = _cut(chrome, g.panel)
    d = ImageDraw.Draw(img)
    W, H = img.size
    d.rounded_rectangle((0, 0, W - 1, H - 1), g.s(12), fill=PANEL)
    m = g.s(14)
    fl, fv = _font(POPPINS, g.s(12)), _font(POPPINS, g.s(22))
    best = st.get("best")
    cells = [("TIME LIVE", fmt_hms(now - st.get("live_since", now)), WHITE),
             ("ROUNDS", str(st.get("spins", 0)), WHITE),
             ("BEST MULTI", f"{best:g}x" if best else "-", TEAL),
             ("DEMO BALANCE", fmt_money(st.get("balance")), WHITE)]
    colw = (W - 2 * m) / 2
    for i, (k, v, c) in enumerate(cells):
        x = m + (i % 2) * colw
        y = g.s(9) + (i // 2) * g.s(46)
        d.text((x, y), k, font=fl, fill=GREY)
        vv = _wrap_px(v, fv, int(colw - g.s(8)))[:1]
        d.text((x, y + g.s(14)), vv[0] if vv else v, font=fv, fill=c)
    y = g.s(100)
    d.line((m, y, W - m, y), fill=PANEL_EDGE, width=1)
    fk, ft = _font(POPPINS, g.s(12)), _font(UBUNTU, g.s(15))
    kw = g.s(50)
    for row, (k, v, c) in enumerate((("GAME", st.get("game") or "-", WHITE),
                                     ("NOW", seg_label(st.get("segment") or "") or "-", TEAL))):
        yy = y + g.s(14) + row * g.s(21)
        d.text((m, yy), k, font=fk, fill=GREY, anchor="lm")
        txt = _wrap_px(v, ft, W - 2 * m - kw)[:1]
        d.text((m + kw, yy), txt[0] if txt else "-", font=ft, fill=c, anchor="lm")
    return img


def h_game_title(st: dict) -> str:
    scr = st.get("screen") if isinstance(st.get("screen"), dict) else {}
    return " ".join(str(st.get("game") or scr.get("game") or "").split()) or "picking a game"


def h_footer(st: dict, now: float) -> list[tuple[str, str, tuple]]:
    """The pane footer: live time, ROUNDS played this session, best multiplier (never spins)."""
    try:
        best = float(st.get("best") or 0)
    except (TypeError, ValueError):
        best = 0.0
    return [("live", fmt_hms(now - st.get("live_since", now)), WHITE),
            ("rounds", str(st.get("spins", 0)), WHITE),
            ("best", mult_text(best) if best > 0 else "-", TEAL)]


def h_footer_mix(st: dict, now: float) -> list[tuple[str, str, tuple]]:
    """Mix pills: Live / Rounds / Best, dark ink on the white glass, best in teal."""
    labs = ("Live", "Rounds", "Best")
    cols = (MX.INK, MX.INK, MX.BEST)
    return [(lab, v, c) for lab, (_, v, _), c in zip(labs, h_footer(st, now), cols)]


def draw_panel_h(g: Geo, chrome: Image.Image, st: dict, now: float) -> Image.Image:
    """H pane, header and footer: the current game's name left of the DEMO PLAY tag; the divider and
    live / rounds / best at the bottom. The chat list is laid over the middle."""
    img = _cut(chrome, g.panel)
    d = ImageDraw.Draw(img)
    W, H = img.size
    ft = _font(POPPINS, g.s(15))
    title = _wrap_px(h_game_title(st), ft, W - g.s(14) - h_tag_width(g))[:1]
    d.text((g.s(14), g.s(23)), title[0] if title else "", font=ft, fill=WHITE, anchor="lm")
    d.line((g.s(14), g.s(277), W - g.s(14), g.s(277)), fill=H_DIVIDER, width=1)
    fl, fv = _font(UBUNTU, g.s(14)), _font(POPPINS, g.s(15))
    x, y = g.s(16), g.s(295)
    for lab, val, col in h_footer(st, now):
        d.text((x, y), lab, font=fl, fill=H_LABEL, anchor="lm")
        x += fl.getlength(lab) + g.s(6)
        d.text((x, y), val, font=fv, fill=col, anchor="lm")
        x += fv.getlength(val) + g.s(20)
    return img


def name_colour(user: str) -> tuple:
    """A viewer's name colour: stable across restarts (md5, not Python's salted hash)."""
    h = int(hashlib.md5(str(user or "").lower().encode("utf-8")).hexdigest()[:8], 16)
    return NAME_COLOURS[h % len(NAME_COLOURS)]


def chat_fonts(g: Geo):
    return _font(POPPINS, g.s(15)), _font(UBUNTU, g.s(16)), g.s(22 if g.is_h else 21)


def chat_rows(g: Geo, name: str, text: str, width: int) -> list[str]:
    """A message as wrapped lines: the first line starts after "name: ", at most CHAT_MAX_LINES, "..." if cut."""
    fn, ft, _ = chat_fonts(g)
    lead = fn.getlength(f"{name}: ")
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
        while ft.getlength(ln) > room(i) and len(ln) > 1:   # one word wider than the box gets cut
            ln = ln[:-1]
        out.append(ln)
    if len(out) > CHAT_MAX_LINES:
        out = out[:CHAT_MAX_LINES]
        last = out[-1]
        while ft.getlength(last + "...") > room(CHAT_MAX_LINES - 1) and len(last) > 1:
            last = last[:-1]
        out[-1] = last.rstrip(" ,.") + "..."
    return out


def draw_chat(g: Geo, chrome: Image.Image, entries: list[dict]) -> Image.Image:
    """The live Kick chat list: newest at the bottom, older ones scroll up and off. His replies are gold rows."""
    if g.is_mix:
        return MX.scaled(MX.draw_chat(entries, CHAT_EMPTY), *g.chat[2:])
    if g.is_h:
        return draw_chat_h(g, chrome, entries)
    img = _cut(chrome, g.chat)
    d = ImageDraw.Draw(img)
    W, H = img.size
    d.rounded_rectangle((0, 0, W - 1, H - 1), g.s(12), fill=PANEL)
    m = g.s(12)
    d.text((m, g.s(10)), "KICK CHAT", font=_font(POPPINS, g.s(13)), fill=TEAL)
    fn, ft, lh = chat_fonts(g)
    top = g.s(34)
    width = W - 2 * m - g.s(6)
    # rows are drawn bottom-up; only whole messages are shown, an older one that doesn't fit is left off,
    # so nothing ever runs into the header
    body = Image.new("RGB", (W, H - g.s(12) - top), PANEL)
    bd = ImageDraw.Draw(body)
    y = body.height - g.s(2)
    if not entries:
        # an empty box reads as broken: a dim hint until the first message lands
        f1, f2 = _font(POPPINS, g.s(14)), _font(UBUNTU, g.s(14))
        rows = [(CHAT_EMPTY[0], f1)] + [(t, f2) for t in CHAT_EMPTY[1:]]
        yy = (body.height - len(rows) * g.s(21)) // 2
        for t, f in rows:
            bd.text(((W - f.getlength(t)) / 2, yy), t, font=f, fill=CHAT_HINT)
            yy += g.s(21)
    for e in reversed(entries or []):
        hub = bool(e.get("hubie"))
        name = "Hubie" if hub else (str(e.get("name") or "friend")[:24] or "friend")
        rows = chat_rows(g, name, str(e.get("text") or ""), width)
        h = len(rows) * lh + (g.s(9) if hub else g.s(4))
        if y - h < 0:
            break
        y -= h
        if hub:
            bd.rounded_rectangle((m - g.s(6), y, W - m + g.s(4), y + h - g.s(4)), g.s(6), fill=HUBIE_ROW)
        yy = y + (g.s(2) if hub else 0)
        nc = GOLD if hub else name_colour(e.get("user") or name)
        for i, ln in enumerate(rows):
            if i == 0:
                bd.text((m, yy), f"{name}:", font=fn, fill=nc)
                bd.text((m + fn.getlength(f"{name}: "), yy + g.s(1)), ln, font=ft, fill=WHITE)
            else:
                bd.text((m, yy + g.s(1)), ln, font=ft, fill=WHITE)
            yy += lh
    img.paste(body, (0, top))
    return img


def draw_chat_h(g: Geo, chrome: Image.Image, entries: list[dict]) -> Image.Image:
    """H chat: plain lines on the glass, newest at the bottom; viewer names in their colour, his name teal."""
    img = _cut(chrome, g.chat)
    d = ImageDraw.Draw(img)
    W, H = img.size
    m = g.s(12)
    fn, ft, lh = chat_fonts(g)
    width = W - 2 * m - g.s(2)
    if not entries:
        f1, f2 = _font(POPPINS, g.s(14)), _font(UBUNTU, g.s(14))
        rows = [(CHAT_EMPTY[0], f1)] + [(t, f2) for t in CHAT_EMPTY[1:]]
        yy = (H - len(rows) * g.s(21)) // 2
        for t, f in rows:
            d.text(((W - f.getlength(t)) / 2, yy), t, font=f, fill=CHAT_HINT)
            yy += g.s(21)
        return img
    y = H - g.s(6)
    for e in reversed(entries):
        hub = bool(e.get("hubie"))
        name = "hubie" if hub else (str(e.get("name") or "friend")[:24] or "friend")
        rows = chat_rows(g, name, str(e.get("text") or ""), width)
        h = len(rows) * lh
        if y - h < g.s(4):
            break
        y -= h
        nc = TEAL if hub else name_colour(e.get("user") or name)
        tc = H_HUB_TEXT if hub else WHITE
        for i, ln in enumerate(rows):
            cy = y + i * lh + lh / 2
            if i == 0:
                d.text((m, cy), f"{name}:", font=fn, fill=nc, anchor="lm")
                d.text((m + fn.getlength(f"{name}: "), cy + g.s(1)), ln, font=ft, fill=tc, anchor="lm")
            else:
                d.text((m, cy + g.s(1)), ln, font=ft, fill=tc, anchor="lm")
        y -= g.s(3)
    return img


def seg_label(seg: str) -> str:
    """The panel prints "NOW <label>": never "NOW Now playing", never "NOW now ..."."""
    t = " ".join(str(seg or "").split())
    low = t.lower()
    if low in ("", "now playing", "play", "playing", "slots"):
        return "spinning slowly" if low else ""
    if low.startswith("now "):
        t = t[4:]
    return t


SHORT_NAMES = (("Super Scatter", "SS"), ("Megaways", "MW"), ("Sweet Bonanza", "Bonanza"),
               ("Gates of Olympus", "Olympus"), ("Big Bass Bonanza", "Big Bass"), ("Fortune of Olympus", "Fortune"),
               ("Madame Destiny", "Destiny"))


def short_game(name: str) -> str:
    n = str(name or "").strip()
    for a, b in SHORT_NAMES:
        n = n.replace(a, b)
    return n


def mult_text(m: float) -> str:
    if m <= 0:
        return "0x"
    if m >= 100:
        return f"{m:,.0f}x"
    return f"{m:.2f}x"


def mult_colour(m: float, won: bool) -> tuple:
    if m >= 10:
        return GOLD
    if won:
        return TEAL
    return BACK_GREY if m > 0 else ZERO_RED


def recent_item(ev: dict) -> dict | None:
    try:
        m = float(ev.get("multiplier") or 0)
    except (TypeError, ValueError):
        return None
    d = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    won = bool(ev.get("won")) if ev.get("won") is not None else m > 1
    return {"g": short_game(ev.get("game") or d.get("slot") or ""), "m": round(m, 2), "w": won,
            "buy": bool(d.get("bonus_buy")), "fs": bool(d.get("free_spins_triggered"))}


def draw_spins(g: Geo, chrome: Image.Image, recent: list[dict]) -> Image.Image:
    """Last few rounds, newest first: game short name, multiplier in its colour, BUY / FS tags. desk1 only."""
    if g.framed:
        return None     # looks H and mix have no round-history row (the owner removed it twice)
    img = _cut(chrome, g.spins)
    if os.environ.get("HUBIE_SPINS_STRIP", "0") != "1":
        return img      # off by default: not asked for by the contest rules (owner 2026-11-07: "useless")
    d = ImageDraw.Draw(img)
    W, H = img.size
    fl = _font(POPPINS, g.s(13))
    lw = g.s(64)
    d.text((g.s(4), H // 2 - g.s(9)), "LAST", font=fl, fill=GREY, anchor="lm")
    d.text((g.s(4), H // 2 + g.s(9)), "SPINS", font=fl, fill=GREY, anchor="lm")
    gap = g.s(6)
    cw = (W - lw - gap * (RECENT_N - 1)) / RECENT_N
    fn, fm, ft = _font(UBUNTU, g.s(16)), _font(POPPINS, g.s(27)), _font(POPPINS, g.s(11))
    for i in range(RECENT_N):
        x0 = lw + i * (cw + gap)
        box = (round(x0), 0, round(x0 + cw) - 1, H - 1)
        it = recent[i] if i < len(recent) else None
        if not it:
            d.rounded_rectangle(box, g.s(10), outline=(20, 44, 52), width=1)
            continue
        edge = GOLD if it.get("buy") else (TEAL if it.get("fs") else PANEL_EDGE)
        d.rounded_rectangle(box, g.s(10), fill=PANEL, outline=edge, width=max(1, g.s(2) if edge != PANEL_EDGE else 1))
        tag = "BUY" if it.get("buy") else ("FS" if it.get("fs") else "")
        tw = (ft.getlength(tag) + g.s(10)) if tag else 0
        name = _wrap_px(it.get("g") or "-", fn, int(cw - g.s(16) - tw))[:1]
        d.text((x0 + g.s(10), g.s(17)), name[0] if name else "-", font=fn, fill=GREY, anchor="lm")
        if tag:
            tx1 = box[2] - g.s(7)
            d.rounded_rectangle((tx1 - tw, g.s(8), tx1, g.s(25)), g.s(4), fill=GOLD if it.get("buy") else TEAL)
            d.text((tx1 - tw / 2, g.s(17)), tag, font=ft, fill=BASE, anchor="mm")
        m = float(it.get("m") or 0)
        d.text((x0 + g.s(10), H - g.s(20)), mult_text(m), font=fm, fill=mult_colour(m, bool(it.get("w"))), anchor="lm")
    return img


_ART: dict = {}


def _hubie_art(height: int):
    if height not in _ART:
        try:
            im = Image.open(HUBIE_ART).convert("RGBA")
            _ART[height] = im.resize((round(im.width * height / im.height), height), Image.LANCZOS)
        except OSError:
            _ART[height] = None
    return _ART[height]


def _card_scale(g: Geo):
    """The card is laid out for desk1's 880x496 game box; in a smaller window everything shrinks by the height
    ratio and the layout is centred. Returns (u, ox): u(v) scales a 1280-unit length, ox is the x offset."""
    gx, gy, gw, gh = g.game
    k = min(1.0, gh / g.s(496))
    ox = int(round((gw - g.s(880) * k) / 2)) if k < 1 else 0
    return (lambda v: int(round(g.s(v) * k))), ox


def draw_card_base(g: Geo, state: str, game: str) -> Image.Image:
    """The switching-game card that covers the game box (site page, fun-mode box, top bar) until a game runs."""
    gx, gy, gw, gh = g.game
    u, ox = _card_scale(g)
    img = Image.new("RGB", (gw, gh), HB_NAVY)
    d = ImageDraw.Draw(img)
    # a soft teal glow behind him, Housebets colours, nothing that looks like game art
    glow = Image.new("L", (gw, gh), 0)
    ImageDraw.Draw(glow).ellipse((ox + u(40), u(40), ox + u(420), gh - u(20)), fill=60)
    from PIL import ImageFilter
    glow = glow.filter(ImageFilter.GaussianBlur(u(60)))
    img.paste(Image.new("RGB", (gw, gh), (20, 90, 96)), (0, 0), glow)
    if not g.framed:    # H / mix: the frame's own rounded border sits on top
        d.rectangle((0, 0, gw - 1, gh - 1), outline=PANEL_EDGE, width=max(1, g.s(2)))
    art = _hubie_art(u(330))
    if art is not None:
        img.paste(art, (ox + u(110), (gh - art.height) // 2 + u(10)), art)
    label, idle_title, sub = CARD_TEXT.get(state, CARD_TEXT["loading"])
    x = ox + u(380)
    d.text((x, u(172)), label, font=_font(POPPINS, u(16)), fill=TEAL)
    title = game if (game and state != "offline") else idle_title
    ft = _font(POPPINS, u(36))
    lines = _wrap_px(title, ft, gw - x - u(40))[:2]
    y = u(198)
    for ln in lines:
        d.text((x, y), ln, font=ft, fill=WHITE)
        y += u(46)
    d.text((x, y + u(58)), sub, font=_font(UBUNTU, u(19)), fill=GREY)
    return img


def card_bar_y(g: Geo, game: str, state: str) -> int:
    u, ox = _card_scale(g)
    x = ox + u(380)
    title = game if (game and state != "offline") else CARD_TEXT.get(state, CARD_TEXT["loading"])[1]
    n = len(_wrap_px(title, _font(POPPINS, u(36)), g.game[2] - x - u(40))[:2]) or 1
    return u(198) + n * u(46) + u(22)


def draw_card(g: Geo, base: Image.Image, state: str, game: str, phase: float) -> Image.Image:
    """The card with its loading bar at `phase` (seconds): a teal block sliding along a dark track."""
    img = base.copy()
    d = ImageDraw.Draw(img)
    u, ox = _card_scale(g)
    x0, w, h = ox + u(380), u(340), max(4, u(10))
    y = card_bar_y(g, game, state)
    d.rounded_rectangle((x0, y, x0 + w, y + h), h // 2, fill=(20, 44, 52))
    if state != "offline":
        seg = u(90)
        t = (phase % 2.4) / 2.4
        a = x0 - seg + t * (w + seg)
        lo, hi = max(x0, a), min(x0 + w, a + seg)
        if hi - lo > h:
            d.rounded_rectangle((lo, y, hi, y + h), h // 2, fill=TEAL)
    return img


# ---------- cam loops ----------

def cam_paths(cfg) -> tuple[Path, Path]:
    talk = Path(cfg.cam_talk)
    if not talk.exists() and Path(cfg.cam_talk_fallback).exists():
        talk = Path(cfg.cam_talk_fallback)
    return Path(cfg.cam_idle), talk


def mouth_paths(cfg) -> list[Path]:
    """mouth_0.mp4 (rest) .. mouth_N.mp4: the idle loop with each mouth shape painted in
    (tools/lipsync/build_visemes.py). Empty unless the set is complete and lip-sync is on."""
    if cfg.cam_mode != "lipsync":
        return []
    out = []
    for k in range(len(lipsync.NAMES)):
        p = Path(cfg.mouth_dir) / f"mouth_{k}.mp4"
        if not p.exists():
            return []
        out.append(p)
    return out


REACTIONS = ("win", "loss", "surprise", "banana")


def reaction_paths(cfg) -> dict[str, Path]:
    """Reaction clips that exist and decode (each starts and ends on the desk still, so they cut in and out
    of the idle loop at a loop boundary). Lip-sync mode only; a missing or broken clip is left out."""
    if not mouth_paths(cfg):
        return {}
    out, off = {}, []
    base = _first_gray(mouth_paths(cfg)[0])
    for name in REACTIONS:
        p = Path(getattr(cfg, f"react_{name}"))
        if not (p.exists() and clip_ok(p)):
            continue
        # a reaction cuts in and out on the idle's frame 0: one shot on another desk (v1 reactions on the v3
        # mirrored idle) would be a jump to a different Hubie, so it is left out until a matching clip exists
        first = _first_gray(p)
        d = float(abs(first.astype("float32") - base.astype("float32")).mean()) if (first is not None and base is not None) else 0.0
        if d > cfg.react_match:
            off.append(f"{name} ({d:.0f})")
            continue
        out[name] = p
    key = (str(cfg.mouth_dir), tuple(off))
    if off and key not in _REACT_LOGGED:
        _REACT_LOGGED.add(key)
        log.warning("reactions left out, first frame doesn't match the idle's frame 0 (mean diff): %s", ", ".join(off))
    return out


_REACT_LOGGED: set = set()
_GRAY: dict = {}


def _first_gray(p: Path):
    """The clip's first frame, small and grey (numpy), cached per file version; None if it can't be read."""
    try:
        import numpy as np
        st = p.stat()
        key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
        if key not in _GRAY:
            r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-frames:v", "1", "-vf", "scale=96:64",
                                "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=20)
            _GRAY[key] = np.frombuffer(r.stdout, np.uint8).reshape(64, 96) if len(r.stdout) == 96 * 64 else None
        return _GRAY[key]
    except (OSError, ValueError, subprocess.SubprocessError, ImportError):
        return None


def click_paths(cfg) -> list[Path]:
    """The click clip's mouth set (HUBIE_CLICK_MOUTH_DIR/mouth_0..8.mp4). Empty unless lip-sync is on and the set
    is complete; then a spin gets the click."""
    if not cfg.click_mouth_dir or not mouth_paths(cfg):
        return []
    out = []
    for k in range(len(lipsync.NAMES)):
        p = Path(cfg.click_mouth_dir) / f"mouth_{k}.mp4"
        if not p.exists():
            return []
        out.append(p)
    return out


CLICK_LEVELS = 16   # cross-fade steps in ramp.yuv


def _yuv_frames(p: Path, w: int, h: int):
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(p), "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                         capture_output=True, check=True, timeout=120).stdout
    fs = w * h * 3 // 2
    a = np.frombuffer(raw, np.uint8)[: len(raw) // fs * fs].reshape(-1, fs)
    return (a[:, : w * h].reshape(-1, h, w), a[:, w * h: w * h * 5 // 4].reshape(-1, h // 2, w // 2),
            a[:, w * h * 5 // 4:].reshape(-1, h // 2, w // 2))


def click_assets(cfg) -> dict | None:
    """The click as one raw picture the encoder keeps in memory: every frame (columns) of every mouth (rows) at
    cam size, so the clip can start on any frame; ramp.yuv holds the cross-fade levels. meta has how far each
    idle frame is from the click's first and last frame (mean luma difference), to pick a start and fade lengths.
    Made once and cached on disk. None if there is no click set."""
    paths = click_paths(cfg)
    if not paths:
        return None
    paw = cfg.click_mode != "full"
    if paw:
        paths = paths[:1]   # the face is never touched: one row (the rest mouth) is all that is laid over
    import numpy as np
    g = Geo(cfg.width, cfg.height)
    _, _, cw, ch = g.cam
    clips = [cam_clip(cfg, p) for p in paths]
    idle = cam_clip(cfg, mouth_paths(cfg)[0])
    key = hashlib.sha1(("|".join(f"{c.name}" for c in [*clips, idle]) +
                        (f"|paw {cfg.click_paw}" if paw else "|full") +
                        ("" if g.cam_cy == 0.3 else f"|cy{g.cam_cy}")).encode()).hexdigest()[:12]
    d = cfg.data_dir / "cam_cache" / f"click_{cw}x{ch}_{key}"
    meta_p = d / "meta.json"
    if meta_p.exists():
        try:
            meta = json.loads(meta_p.read_text())
            meta.update(grid=str(d / "grid.yuv"), ramp=str(d / "ramp.yuv"))
            return meta
        except (OSError, ValueError):
            pass
    d.mkdir(parents=True, exist_ok=True)
    frames = [_yuv_frames(c, cw, ch) for c in clips]
    n = min(len(f[0]) for f in frames)
    planes = []
    for k, (pw, ph) in enumerate(((cw, ch), (cw // 2, ch // 2), (cw // 2, ch // 2))):
        grid = np.zeros((len(frames) * ph, n * pw), np.uint8)
        for m, f in enumerate(frames):
            grid[m * ph:(m + 1) * ph] = np.concatenate(list(f[k][:n]), axis=1)
        planes.append(grid)
    tmp = d / f".grid.{os.getpid()}"
    tmp.write_bytes(b"".join(p.tobytes() for p in planes))
    os.replace(tmp, d / "grid.yuv")
    shape = paw_mask(cfg, cw, ch) if paw else np.ones((ch, cw), np.float32)
    shape_c = shape[::2, ::2]
    ramp_y = np.concatenate([np.round(255 * l / CLICK_LEVELS * shape).astype(np.uint8)
                             for l in range(CLICK_LEVELS + 1)], axis=1)
    ramp_c = np.concatenate([np.round(255 * l / CLICK_LEVELS * shape_c).astype(np.uint8)
                             for l in range(CLICK_LEVELS + 1)], axis=1)
    (d / "ramp.yuv").write_bytes(ramp_y.tobytes() + ramp_c.tobytes() + ramp_c.tobytes())
    iy = _yuv_frames(idle, cw, ch)[0].astype(np.float32)
    c0, c1 = frames[0][0][0].astype(np.float32), frames[0][0][n - 1].astype(np.float32)
    if paw:   # inside the paw area the idle must be the click's first frame on every loop frame, or it would pop
        inside = shape > 0.05
        worst = max(float(abs(f - c0)[inside].mean()) for f in iy)
        if worst > 4:
            log.warning("click paw area moves in the idle (mean diff up to %.1f): the click may pop in and out", worst)
    meta = {"n": n, "mouths": len(frames), "w": cw, "h": ch, "idle_frames": len(iy), "mode": "paw" if paw else "full",
            "din": [round(float(abs(f - c0).mean()), 2) for f in iy],
            "dout": [round(float(abs(f - c1).mean()), 2) for f in iy]}
    meta_p.write_text(json.dumps(meta))
    meta.update(grid=str(d / "grid.yuv"), ramp=str(d / "ramp.yuv"))
    return meta


def paw_mask(cfg, cw: int, ch: int):
    """The held paw + mouse ellipse (HUBIE_CLICK_PAW, source pixels) mapped into the cam box the way cam_clip crops
    and scales, feathered: 1 inside, 0 outside (numpy float, ch x cw)."""
    import cv2
    import numpy as np
    x0, y0, x1, y1, fe = (float(v) for v in cfg.click_paw.split(","))
    w, h = (int(v) for v in subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0",
         str(click_paths(cfg)[0])], capture_output=True, text=True, timeout=10).stdout.strip().split(",")[:2])
    z = max(1.0, cfg.cam_zoom)
    cwid, chei = min(w, h * cw / ch) / z, min(h, w * ch / cw) / z
    ox, oy = (w - cwid) / 2, (h - chei) * Geo(cfg.width, cfg.height).cam_cy
    k = cw / cwid
    m = np.zeros((ch, cw), np.float32)
    cv2.ellipse(m, (round(((x0 + x1) / 2 - ox) * k), round(((y0 + y1) / 2 - oy) * k)),
                (round((x1 - x0) / 2 * k), round((y1 - y0) / 2 * k)), 0, 0, 360, 1, -1)
    return cv2.GaussianBlur(m, (0, 0), max(0.5, fe * k))


def fade_frames(dist: float) -> int:
    """Cross-fade length for a cut between two frames this far apart (mean luma difference)."""
    return 2 if dist < 1.0 else 4 if dist < 2.5 else 6 if dist < 4.5 else 8


def click_expr(fps: int, t0: float, n: int, w: int, fin: int, fout: int) -> tuple[str, str]:
    """crop x expressions: which click frame (column) and which fade level (ramp tile) to show at stream time t."""
    f = f"floor((t-{t0:.4f})*{fps}+0.5)"
    x = f"{w}*clip({f},0,{n - 1})"
    a = f"min(1,({f}+1)/{fin + 1})*min(1,({n}-{f})/{fout + 1})"
    m = f"{w}*if(lt({f},0)+gte({f},{n}),0,round({CLICK_LEVELS}*{a}))"
    return x, m


def clip_ok(p: Path) -> bool:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(p)],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and int(r.stdout.strip() or 0) > 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def loop_seconds(p: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                           capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 5.0


def reaction_for(ev: dict, best_before: float) -> str | None:
    """Which reaction a finished round earns, if any."""
    try:
        mult = float(ev.get("multiplier") or 0)
    except (TypeError, ValueError):
        return None
    detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    bonus = bool(detail.get("free_spins_triggered") or detail.get("free_spins") or detail.get("bonus"))
    if mult >= 10 or (mult >= 3 and mult > best_before > 0):
        return "win"
    if bonus:
        if mult < 1:
            return "loss"
        # the free spins got their surprise clip live when they started (orchestrator "react" event)
        return None if ev.get("feature_seen") else "surprise"
    return None


def desk_ready(cfg) -> bool:
    if cfg.layout != "desk":
        return False
    idle, talk = cam_paths(cfg)
    return bool(mouth_paths(cfg)) or (idle.exists() and talk.exists())


class CmdLink:
    """Sends filter commands to the encoder over its zmq filter on loopback (mouth shape, subtitle plate).
    Commands are applied to the frame being drawn, so they line up with the voice. Only the latest command
    per key matters; a thread sends them and nothing waits on the encoder. Everything is re-sent every 2 s,
    so a restarted encoder picks the current state back up."""

    def __init__(self, port: int):
        self.addr = f"tcp://127.0.0.1:{port}"
        self.want: dict[str, str] = {}
        self.cond = threading.Condition()
        threading.Thread(target=self._run, daemon=True, name="cmdlink").start()

    def send(self, key: str, cmd: str) -> None:
        with self.cond:
            self.want[key] = cmd
            self.cond.notify()

    def _run(self) -> None:
        try:
            import zmq
        except ImportError:
            log.warning("pyzmq missing: mouth and subtitle plate stay as they are")
            return
        ctx = zmq.Context.instance()
        sock, sent, last = None, {}, 0.0
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.want != sent, timeout=2.0)
                todo = dict(self.want)
            if time.monotonic() - last >= 2.0:
                sent = {}
            for key, cmd in todo.items():
                if sent.get(key) == cmd:
                    continue
                try:
                    if sock is None:
                        sock = ctx.socket(zmq.REQ)
                        sock.setsockopt(zmq.LINGER, 0)
                        sock.connect(self.addr)
                    sock.send_string(cmd)
                    if sock.poll(400):
                        sock.recv()
                        sent[key] = cmd
                        last = time.monotonic()
                    else:   # encoder down or restarting: a REQ socket can't send again until it's rebuilt
                        sock.close()
                        sock, sent = None, {}
                        time.sleep(0.3)
                        break
                except Exception as e:
                    log.warning("cmd link: %r", e)
                    try:
                        sock.close()
                    except Exception:
                        pass
                    sock, sent = None, {}
                    time.sleep(0.5)
                    break


def cam_clip(cfg, src: Path) -> Path:
    """The loop scaled to the cam box at the stream's frame rate, made once and cached on disk.
    Decoding two small loops costs a fraction of two full-size ones."""
    g = Geo(cfg.width, cfg.height)
    _, _, cw, ch = g.cam
    st = src.stat()
    cy = g.cam_cy
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}|{cw}x{ch}|{cfg.fps}|{cfg.cam_zoom}|v2"
                       f"{'' if cy == 0.3 else f'|cy{cy}'}".encode()).hexdigest()[:12]
    out = cfg.data_dir / "cam_cache" / f"{src.stem}_{cw}x{ch}_{key}.mp4"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.mp4")
    z = max(1.0, cfg.cam_zoom)
    # crop to the box's aspect, zoomed in a little on his face (desk1: kept toward the top so the hat stays in;
    # H: the wider window is cut lower so the paw and mouse stay in)
    vf = (f"crop='min(iw,ih*{cw}/{ch})/{z}':'min(ih,iw*{ch}/{cw})/{z}':'(iw-ow)/2':'(ih-oh)*{cy}',"
          f"scale={cw}:{ch}:flags=lanczos,"
          f"fps={cfg.fps},format=yuv420p")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src), "-an",
                    "-vf", vf, "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-g", str(cfg.fps),
                    str(tmp)], check=True, timeout=120, capture_output=True)
    os.replace(tmp, out)
    return out


def prepare(cfg) -> None:
    """Static art and first copies of every layer, so the encoder can start whatever else is down."""
    g = Geo(cfg.width, cfg.height)
    d = Path(cfg.scene_dir)
    d.mkdir(parents=True, exist_ok=True)
    if g.framed:
        chrome = chrome_for(g)
        _save(frame_for(g), d / "frame.png")
        _save(draw_veil(g, chrome), d / "veil.png")
    else:
        chrome = draw_chrome(g)
        _save(chrome, d / "chrome.png")
        _save(draw_camframe(g), d / "camframe.png")
        _save(draw_cammask(g), d / "cammask.png")
    _, _, cw, ch = g.cam
    for k in range(MIX_LEVELS + 1):
        p = d / f"mix_{k}.png"
        if not p.exists():
            _save(Image.new("L", (cw, ch), round(255 * k / MIX_LEVELS)), p)
    if not (d / "mix.png").exists():
        _link(d / "mix_0.png", d / "mix.png")
    st = load_state(d)
    if not _size_ok(d / "panel.png", g.panel[2:], "RGBA" if g.is_mix else "RGB"):   # missing, or another look's
        _save(draw_panel(g, chrome, st, time.time()), d / "panel.png")
    _save(draw_chat(g, chrome, st.get("chat") or []), d / "chat.png")
    if not g.framed:
        _save(draw_plate(g), d / "plate.png")
        _save(draw_spins(g, chrome, st.get("recent") or []), d / "spins.png")
    scr = st.get("screen") or {}
    _save(draw_card(g, draw_card_base(g, scr.get("state") or "loading", scr.get("game") or ""),
                    scr.get("state") or "loading", scr.get("game") or "", 0.0), d / "card.png")
    for k in sub_lines(""):
        if not (d / f"{k}.txt").exists():
            _write_text(d / f"{k}.txt", "")
    for src in [*(mouth_paths(cfg) or cam_paths(cfg)), *reaction_paths(cfg).values()]:
        cam_clip(cfg, src)
    try:
        click_assets(cfg)
    except Exception as e:
        log.warning("click clip not prepared: %r", e)


def _size_ok(p: Path, size, mode: str | None = None) -> bool:
    try:
        with Image.open(p) as im:
            return im.size == tuple(size) and (mode is None or im.mode == mode)
    except OSError:
        return False


def load_state(d: Path) -> dict:
    try:
        st = json.loads((Path(d) / "session.json").read_text())
        if isinstance(st, dict) and "live_since" in st:
            return st
    except (OSError, ValueError):
        pass
    st = {"live_since": time.time(), "spins": 0, "best": 0.0, "balance": None, "game": "", "segment": "",
          "recent": [], "screen": {"state": "loading", "game": ""}, "chat": []}
    try:
        (Path(d) / "session.json").write_text(json.dumps(st))
    except OSError:
        pass
    return st


# ---------- the live part (runs inside the stage server) ----------

class DeskScene:
    SUB_HOLD = 0.8
    REACT_COOLDOWN = float(os.environ.get("HUBIE_REACT_COOLDOWN", "60"))   # tools shorten it to show every clip
    REACT_HOLD = 12.0     # a clip asked for during speech still plays if he stops talking within this
    BANANA_EVERY = 600.0  # at most one banana bit per this many seconds
    DEAD_AIR = 20.0       # silence this long counts as dead air

    def __init__(self, cfg):
        self.cfg = cfg
        self.g = Geo(cfg.width, cfg.height)
        self.dir = Path(cfg.scene_dir)
        try:
            prepare(cfg)
        except Exception as e:
            log.warning("desk prepare failed: %r", e)
        self.chrome = chrome_for(self.g)
        self.state = load_state(self.dir)
        self.view = ""      # H: which game view the encoder is on ("original" / "slot")
        self.fade = max(0.0, cfg.cam_fade_ms / 1000)
        self.mix = 0
        self.mix_target = 0
        self.speech: asyncio.Task | None = None
        self.fader: asyncio.Task | None = None
        self.clock: asyncio.Task | None = None
        self.lipsync = bool(mouth_paths(cfg))
        self.link = CmdLink(cfg.mouth_port)
        self.mouth = self.lipsync
        self.mouth_task: asyncio.Task | None = None
        self.cur_mouth = 0
        n_mouths = len(lipsync.NAMES)
        self.reactions = {k: n_mouths + i for i, k in enumerate(reaction_paths(cfg))}
        mp = mouth_paths(cfg)
        self.loop_s = loop_seconds(mp[0]) if mp else 5.0
        self.reacting = False
        self.react_name = ""
        self.react_task: asyncio.Task | None = None
        self.react_pending: tuple[str, float] | None = None   # a clip asked for while he talked: (name, play by)
        self.last_react = -1e9
        self.last_banana = time.monotonic()
        self.click = None
        if self.lipsync:
            try:
                self.click = click_assets(cfg)
            except Exception as e:
                log.warning("click clip left out: %r", e)
        self.click_win = (0.0, 0.0)   # wall-clock start and end of the click planned or playing
        self.clicks = {"played": 0, "skipped": 0}
        self.speaking = False
        self.last_speech_end = time.monotonic()
        self.rng = random.Random()
        scr = self.state.get("screen") or {}
        self.screen = (str(scr.get("state") or "loading"), str(scr.get("game") or ""))
        self.card_base = None
        self.card_task: asyncio.Task | None = None
        self.card_t0 = time.monotonic()

    # ---- plumbing ----
    def _safe(self, fn, *a) -> None:
        try:
            fn(*a)
        except Exception as e:   # cosmetic
            log.warning("desk draw failed: %r", e)

    def _save_state(self) -> None:
        try:
            tmp = self.dir / f".session.{os.getpid()}"
            tmp.write_text(json.dumps(self.state))
            os.replace(tmp, self.dir / "session.json")
        except OSError:
            pass

    def start(self) -> None:
        if not self.clock:
            self.clock = asyncio.ensure_future(self._clock())
        self._safe(self.set_screen, *self.screen, True)
        if self.g.framed and not self.speaking:
            self.link.send("veil", "overlay@veil enable 1")   # his last line, dimmed, until he speaks again

    # ---- the game area: the real game only once it runs, the switching card otherwise ----
    @property
    def card_up(self) -> bool:
        return self.screen[0] not in ("ready", "browsing")

    def set_screen(self, state: str, game: str = "", force: bool = False) -> None:
        state = state if state in (*CARD_TEXT, "ready", "browsing") else "loading"
        if (state, game) == self.screen and not force:
            return
        self.screen = (state, game)
        self.state["screen"] = {"state": state, "game": game}
        self._save_state()
        if self.g.framed and game and state != "offline" and game != self.state.get("game"):
            self.state["game"] = game       # the pane header names the game as soon as the card does
            self._safe(self.draw_panel)
        if self.g.framed and state != "offline":
            self.set_view(view_kind(self.g, state, game), force)
        if state in ("ready", "browsing"):   # browsing: the site page itself, balance patched by the adapter
            self.link.send("card", "overlay@card enable 0")
            # the encoder picks up a new card image up to a second late: leave a generic one ready for the next
            # switch, so that second never shows the last game's name
            self.card_base = None
            _save(draw_card(self.g, draw_card_base(self.g, "loading", ""), "loading", "", 0.0), self.dir / "card.png")
            return
        self.card_base = draw_card_base(self.g, state, game)
        self.card_t0 = time.monotonic()
        self.draw_card()
        self.link.send("card", "overlay@card enable 1")
        try:
            if self.card_task is None or self.card_task.done():
                self.card_task = asyncio.ensure_future(self._card_loop())
        except RuntimeError:   # no loop (tools drawing a still): the card is drawn, just not animated
            pass

    def set_view(self, kind: str, force: bool = False) -> None:
        """H: points the game window at the originals' game panel or the slot iframe (the encoder's scale@gz and
        crop@gc). Sent while the switching card is up, so the change itself is never seen."""
        if kind == self.view and not force:
            return
        self.view = kind
        src = self.cfg.game_grab[2:] if (self.cfg.scene_bg == "x11" and self.cfg.game_grab) else (self.cfg.width, self.cfg.height)
        for key, cmd in view_cmds(self.g, kind, *src).items():
            self.link.send(key, cmd)

    def draw_card(self) -> None:
        if self.card_base is None:
            self.card_base = draw_card_base(self.g, *self.screen)
        _save(draw_card(self.g, self.card_base, self.screen[0], self.screen[1], time.monotonic() - self.card_t0),
              self.dir / "card.png")

    async def _card_loop(self) -> None:
        while self.card_up:
            self._safe(self.draw_card)
            await asyncio.sleep(1 / CARD_FPS)

    def draw_spins(self) -> None:
        if not self.g.framed:
            _save(draw_spins(self.g, self.chrome, self.state.get("recent") or []), self.dir / "spins.png")

    async def _clock(self) -> None:
        while True:
            self._safe(self.draw_panel)
            self._safe(self._maybe_banana)
            self._safe(self._react_pending)
            await asyncio.sleep(1.0 - (time.time() % 1.0) + 0.02)

    # ---- mouth and reactions (one streamselect: mouths first, then reaction clips) ----
    def set_mouth(self, k: int) -> None:
        self.cur_mouth = int(k)
        if self.lipsync and not self.reacting:
            if self.click and self.click.get("mode") == "full" and time.time() < self.click_win[1] + 0.2:
                self.link.send("clk_y", f"crop@clk y {self.click['h'] * self.cur_mouth}")
            self.link.send("cam", f"streamselect@mouth map {self.cur_mouth}")

    def enc_frame(self, now: float | None = None) -> float | None:
        """The encoder's frame number right now (fractional), from status/encoder_clock.json."""
        try:
            c = json.loads((Path(self.cfg.status_dir) / "encoder_clock.json").read_text())
            now = time.time() if now is None else now
            if now - float(c["ts"]) > 5:
                return None
            fps = float(c.get("fps") or self.cfg.fps)
            return int(c["frame"]) + (now - float(c["ts"])) * fps
        except (OSError, ValueError, KeyError, TypeError):
            return None

    CLICK_LEAD = 0.25   # commands go out this long before the click's first frame

    def click_cue(self, why: str = "spin") -> dict:
        """A spin (or bonus-buy confirm) is about to be sent. Plans the click clip to start on a frame where the
        idle is close to the click's first frame (waiting up to HUBIE_CLICK_MAX_WAIT for a better one), with
        cross-fades sized to the cut, and says when the claw lands ("at", wall clock): the adapter clicks then.
        Skipped (adapter clicks right away) while a reaction plays or another click runs."""
        c = self.click
        now = time.time()
        if not c:
            return {"skip": "no click clip"}
        if self.reacting:
            self.clicks["skipped"] += 1
            return {"skip": "reaction playing"}
        if now < self.click_win[1]:
            self.clicks["skipped"] += 1
            return {"skip": "click playing"}
        ef = self.enc_frame(now)
        if ef is None:
            return {"skip": "no encoder clock"}
        fps = self.cfg.fps
        n, L = c["n"], c["idle_frames"]
        first = int(ef + self.CLICK_LEAD * fps) + 1
        paw = c.get("mode") == "paw"
        best = None
        for j in range(1 if paw else int(self.cfg.click_max_wait * fps) + 1):
            f = first + j
            cost = c["din"][f % L] + c["dout"][(f + n) % L] + 0.03 * j
            if best is None or cost < best[0]:
                best = (cost, f)
        f = best[1]
        # paw: the idle holds that area on the click's first frame, so it needs no fade (one frame softens the edge)
        fin, fout = (1, 1) if paw else (fade_frames(c["din"][f % L]), fade_frames(c["dout"][(f + n) % L]))
        x, m = click_expr(fps, f / fps, n, c["w"], fin, fout)
        start = now + (f - ef) / fps
        self.click_win = (start, start + n / fps)
        if self.react_task and not self.react_task.done() and not self.reacting and self.react_name == "banana":
            self.react_task.cancel()   # the click has priority over the banana bit
        if not paw:
            self.link.send("clk_y", f"crop@clk y {c['h'] * self.cur_mouth}")
        self.link.send("clk_x", f"crop@clk x {x}")
        self.link.send("clk_m", f"crop@clkm x {m}")
        self.clicks["played"] += 1
        at = start + self.cfg.click_land_s + self.cfg.click_offset_ms / 1000
        log.info("click (%s) at frame %d, idle frame %d, fades %d/%d, wait %.2f s", why, f, f % L, fin, fout,
                 (f - first) / fps)
        return {"at": at, "start": start, "frame": f, "fade": [fin, fout]}

    def loop_phase(self, now: float | None = None) -> float | None:
        """Seconds into the cam loops right now, from the encoder's frame counter (status/encoder_clock.json).
        Every cam input loops in step from frame 0, so this is the same for all of them."""
        try:
            c = json.loads((Path(self.cfg.status_dir) / "encoder_clock.json").read_text())
            now = time.time() if now is None else now
            if now - float(c["ts"]) > 5:
                return None
            fps = float(c.get("fps") or self.cfg.fps)
            return ((int(c["frame"]) + (now - float(c["ts"])) * fps) / fps) % self.loop_s
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def react(self, name: str) -> bool:
        """Plays a reaction clip from its first frame at the next loop boundary. Never starts one during
        speech, one at a time, with a cooldown. Returns whether it was scheduled."""
        if name not in self.reactions or self.reacting or (self.react_task and not self.react_task.done()):
            return False
        if self.speaking or time.monotonic() - self.last_react < self.REACT_COOLDOWN:
            return False
        phase = self.loop_phase()
        if phase is None:
            return False
        self.react_name = name
        self.react_task = asyncio.ensure_future(self._react(name, self.loop_s - phase))
        return True

    def react_soon(self, name: str, hold: float | None = None) -> bool:
        """react(), or if he is talking right now, the same clip once he stops (within hold seconds).
        A cooldown or a clip already playing still says no."""
        if self.react(name):
            return True
        if self.speaking and name in self.reactions and time.monotonic() - self.last_react >= self.REACT_COOLDOWN:
            self.react_pending = (name, time.monotonic() + (self.REACT_HOLD if hold is None else hold))
        return False

    def _react_pending(self) -> None:
        if not self.react_pending:
            return
        name, until = self.react_pending
        now = time.monotonic()
        if now > until:
            self.react_pending = None
        elif not self.speaking and now - self.last_speech_end >= 0.3:
            self.react_pending = None
            self.react(name)

    async def _react(self, name: str, wait: float) -> None:
        # the command lands on the next frame drawn, so send it half a frame before the boundary
        await asyncio.sleep(max(0.0, wait - 0.5 / self.cfg.fps))
        while True:
            # a click planned or playing inside this clip's 5 s: the click goes first, the reaction waits for the
            # loop boundary after it (a banana bit is just dropped)
            s, e = self.click_win
            now = time.time()
            if not (now < e and now + self.loop_s > s):
                break
            if name == "banana":
                return
            await asyncio.sleep(math.ceil((e - now) / self.loop_s) * self.loop_s)
        if self.speaking:
            # he started talking while it waited: play it once he stops, if that's soon
            self.react_pending = (name, time.monotonic() + self.REACT_HOLD)
            return
        self.reacting = True
        self.last_react = time.monotonic()
        if name == "banana":
            self.last_banana = self.last_react
        self.link.send("cam", f"streamselect@mouth map {self.reactions[name]}")
        log.info("reaction %s playing", name)
        try:
            await asyncio.sleep(self.loop_s)
        finally:
            # if he started talking meanwhile, the reaction played out with no mouth; lip-sync resumes here
            self.reacting = False
            self.link.send("cam", f"streamselect@mouth map {self.cur_mouth}")

    def _maybe_banana(self) -> None:
        now = time.monotonic()
        if (not self.speaking and now - self.last_speech_end >= self.DEAD_AIR and time.time() > self.click_win[1] + 1.0
                and now - self.last_banana >= self.BANANA_EVERY and self.rng.random() < 0.05):
            self.react("banana")

    def draw_panel(self) -> None:
        _save(draw_panel(self.g, self.chrome, self.state, time.time()), self.dir / "panel.png")

    def show_sub(self, page: str) -> None:
        if self.g.framed and not page.strip():
            self.dim_sub()      # H: the bubble keeps his last line, dimmed
            return
        for k, v in sub_lines(page).items():
            _write_text(self.dir / f"{k}.txt", v)
        if self.g.framed:
            self.link.send("veil", "overlay@veil enable 0")
        else:
            self.link.send("plate", f"overlay@plate enable {1 if page.strip() else 0}")

    def dim_sub(self) -> None:
        """H: between lines the bubble keeps his last line under a veil of its own glass (text at ~55%)."""
        self.link.send("veil", "overlay@veil enable 1")

    def draw_chat(self) -> None:
        _save(draw_chat(self.g, self.chrome, self.state.get("chat") or []), self.dir / "chat.png")

    def add_chat(self, entry: dict) -> None:
        """One more row in the chat list (redrawn only on a change); kept in session.json across restarts."""
        self.state["chat"] = (list(self.state.get("chat") or []) + [entry])[-CHAT_KEEP:]
        self._save_state()
        self.draw_chat()

    def hide_chat(self, cid: str) -> None:
        chat = list(self.state.get("chat") or [])
        keep = [e for e in chat if e.get("id") != cid]
        if cid and len(keep) != len(chat):
            self.state["chat"] = keep
            self._save_state()
            self.draw_chat()

    def set_mix(self, k: int) -> None:
        self.mix = k
        _link(self.dir / f"mix_{k}.png", self.dir / "mix.png")

    def talk(self, on: bool) -> None:
        if self.lipsync:
            return
        self.mix_target = MIX_LEVELS if on else 0
        if self.fader is None or self.fader.done():
            self.fader = asyncio.ensure_future(self._fade())

    async def _fade(self) -> None:
        step = self.fade / MIX_LEVELS
        while self.mix != self.mix_target:
            k = self.mix + (1 if self.mix_target > self.mix else -1)
            if step <= 0:
                k = self.mix_target
            self._safe(self.set_mix, k)
            self.mix = k
            if step > 0:
                await asyncio.sleep(step)

    # ---- events ----
    def apply(self, ev: dict) -> dict | None:
        t = ev.get("type")
        if t == "game_state":
            self._safe(self.set_screen, str(ev.get("state") or ""), str(ev.get("game") or ""))
        elif t == "segment":
            self.state["segment"] = str(ev.get("title") or ev.get("kind") or "")
            if ev.get("game"):
                self.state["game"] = str(ev["game"])
            self._save_state()
            self._safe(self.draw_panel)
        elif t == "round":
            best_before = float(self.state.get("best") or 0)
            self.state["spins"] = int(self.state.get("spins", 0)) + 1
            try:
                mult = float(ev.get("multiplier") or 0)
                self.state["best"] = max(float(self.state.get("best") or 0), round(mult, 2))
            except (TypeError, ValueError):
                pass
            if ev.get("balance") is not None:
                self.state["balance"] = ev.get("balance")
            if ev.get("game"):
                self.state["game"] = str(ev["game"])
            item = recent_item(ev)
            if item:
                self.state["recent"] = ([item] + list(self.state.get("recent") or []))[:RECENT_N]
            self._save_state()
            self._safe(self.draw_panel)
            self._safe(self.draw_spins)
            name = reaction_for(ev, best_before)
            if name:
                self._safe(self.react_soon, name)
        elif t == "live_balance":
            # a feature paying out: the demo balance moves with it; SPINS still counts finished base rounds
            if ev.get("balance") is not None and ev.get("balance") != self.state.get("balance"):
                self.state["balance"] = ev.get("balance")
                self._save_state()
                self._safe(self.draw_panel)
        elif t == "react":
            name = str(ev.get("name") or "")
            if name in REACTIONS and name != "banana":
                self._safe(self.react_soon, name)
        elif t == "chat":
            text = " ".join(str(ev.get("text") or "").split())
            if text:
                self._safe(self.add_chat, {"id": str(ev.get("id") or ""), "name": str(ev.get("name") or "friend")[:24],
                                           "user": str(ev.get("user") or ev.get("name") or ""), "text": text[:300]})
        elif t == "chat_hide":
            self._safe(self.hide_chat, str(ev.get("id") or ""))
        elif t == "click":
            return self.click_cue(str(ev.get("why") or "spin"))
        elif t == "speak":
            if self.speech:
                self.speech.cancel()
            self.speech = asyncio.ensure_future(self._speak(ev))

    async def _speak(self, ev: dict) -> None:
        text = str(ev.get("text", ""))
        env = ev.get("env") or []
        length = len(env) * 0.05 or float(ev.get("duration") or 0) or 2.0
        reply = ev.get("reply_to") if isinstance(ev.get("reply_to"), dict) else None
        self.speaking = True
        if reply and text.strip():
            # a chat reply: his line goes into the chat list the moment he starts saying it
            self._safe(self.add_chat, {"hubie": True, "text": " ".join(text.split())[:300]})
        try:
            self.talk(True)
            if self.mouth:
                cues = ev.get("visemes") if isinstance(ev.get("visemes"), list) else None
                if not cues:
                    cues = lipsync.track_from_env(text, env) if env else [[0.0, lipsync.REST]]
                if self.mouth_task:
                    self.mouth_task.cancel()
                self.mouth_task = asyncio.ensure_future(self._mouth(cues, audio_start(ev)))
            pg = pages(self.g, text)
            total = sum(len(p) for p in pg) or 1
            for p in pg:
                self._safe(self.show_sub, p)
                await asyncio.sleep(length * len(p) / total)
            self.talk(False)
            self.speaking = False
            self.last_speech_end = time.monotonic()
            if self.react_pending:      # a clip held back during this line goes right after it
                asyncio.ensure_future(self._later(0.35, self._react_pending))
            await asyncio.sleep(self.SUB_HOLD)
            self._safe(self.show_sub, "")
        except asyncio.CancelledError:
            pass   # the next line took over and sets talk and subtitles itself

    async def _mouth(self, cues: list, start: float | None = None) -> None:
        """Plays the shapes on the speech's clock: from `start` (monotonic, the moment the mixer sent the line's first
        audio out) when the speak event carries it, else from now. HUBIE_LIPSYNC_OFFSET_MS shifts them against the
        audio. Cues already due are skipped to the current one, never flashed through; a line whose mouth starts
        late opens through an in-between for two frames instead of jumping from closed to a big shape."""
        t0 = (time.monotonic() if start is None else start) + self.cfg.lipsync_offset_ms / 1000
        frame = 1 / lipsync.FPS
        try:
            i, opened = 0, False
            first_open = next((float(c[0]) for c in cues if int(c[1]) != lipsync.REST), None)
            while i < len(cues):
                now = time.monotonic()
                while i + 1 < len(cues) and t0 + float(cues[i + 1][0]) <= now:
                    i += 1   # running behind: go straight to the shape that is due
                delay = t0 + float(cues[i][0]) - now
                if delay > 0:
                    await asyncio.sleep(delay)
                shape = int(cues[i][1])
                if not opened and shape != lipsync.REST:
                    opened = True
                    mids = lipsync.between(self.cur_mouth, shape)
                    # late on the first opening (measured against the line's first open cue, not the cue skipped
                    # to, which has usually only just come due): ease in
                    late = time.monotonic() - (t0 + first_open) if first_open is not None else 0.0
                    if mids and late > frame / 2:
                        self.set_mouth(mids[0] if lipsync.RANK[shape] > lipsync.RANK[self.cur_mouth] else mids[-1])
                        await asyncio.sleep(lipsync.HOLD * frame)
                        continue   # then whatever is due by now
                self.set_mouth(shape)
                i += 1
        except asyncio.CancelledError:
            return
        except Exception as e:   # cosmetic
            log.warning("mouth cues: %r", e)
        self.set_mouth(lipsync.REST)

    async def _later(self, delay: float, fn, *a) -> None:
        try:
            await asyncio.sleep(delay)
            self._safe(fn, *a)
        except asyncio.CancelledError:
            pass


def audio_start(ev: dict, now_wall=time.time, now_mono=time.monotonic) -> float | None:
    """The speak event's audio start (mixer wall clock, `audio_t0`) on this process's monotonic clock. None when it
    is missing or implausible (more than 3 s off), and the mouth then runs from the event's arrival."""
    try:
        t = float(ev.get("audio_t0"))
    except (TypeError, ValueError):
        return None
    d = t - now_wall()
    if not -3.0 < d < 3.0:
        return None
    return now_mono() + d


def filter_inputs(cfg, base: list[str], first: int = 1) -> tuple[list[str], str]:
    """ffmpeg inputs numbered from `first`, and the filter graph for the desk picture.
    `base` is input 0, the clock. Returns (inputs, graph ending in [scene])."""
    g = Geo(cfg.width, cfg.height)
    d = Path(cfg.scene_dir)
    mouths = [cam_clip(cfg, p) for p in mouth_paths(cfg)]
    if mouths:
        mouths += [cam_clip(cfg, p) for p in reaction_paths(cfg).values()]
    if not mouths:
        idle, talk = (cam_clip(cfg, p) for p in cam_paths(cfg))
    gx, gy, gw, gh = g.game
    cx, cy, cw, ch = g.cam
    pad = g.frame_pad
    ins: list[str] = []
    n = first

    def add(*a) -> int:
        nonlocal n
        ins.extend(a)
        n += 1
        return n - 1

    # Only input 0 (the clock) runs at -re. Everything else is pulled when the picture needs it: with -re
    # on every input the graph started ~3 s late and stayed behind the wall-clock audio, so the mouth and
    # subtitles ran ahead of the voice. A short read-ahead queue keeps a changed layer to a frame or two.
    loop_img = lambda p, fps: ("-thread_queue_size", "1", "-f", "image2", "-loop", "1", "-framerate", str(fps),
                               "-i", str(p))
    if not g.framed:
        i_chrome = add("-i", str(d / "chrome.png"))
    if cfg.scene_bg == "x11":
        # the game browser fills the display (1280x720); it is scaled into the game box
        x, y, w, h = cfg.game_grab or (0, 0, cfg.width, cfg.height)
        i_game = add("-f", "x11grab", "-framerate", str(cfg.fps), "-video_size", f"{w}x{h}", "-draw_mouse", "0",
                     "-thread_queue_size", "128", "-i", f"{cfg.display}+{x},{y}")
        game_scale = (w, h) != (gw, gh)
    elif g.framed:
        # a still game picture (tests, demos): looped, so a view change (scale@gz / crop@gc) still applies
        i_game = add("-loop", "1", "-framerate", str(cfg.fps), "-f", "image2", "-i", str(cfg.scene_bg_image))
        game_scale = True
    else:
        i_game = add("-i", str(cfg.scene_bg_image))
        game_scale = True
    if mouths:
        # every mouth and reaction loop decodes all the time in step; streamselect picks one per frame
        # and the stage says which over zmq
        i_m = [add("-stream_loop", "-1", "-i", str(p)) for p in mouths]
        click = None
        try:
            click = click_assets(cfg)
        except Exception as e:
            log.warning("click clip left out: %r", e)
        cam = ("".join(f"[{i}:v]" for i in i_m) +
               f"streamselect@mouth=inputs={len(i_m)}:map=0[{'camsel' if click else 'camraw'}];")
        if click:
            # The click is one raw picture held in memory (decoded once, no extra decoder running): crop@clk picks
            # the frame (column, from stream time) and mouth (row); crop@clkm picks the cross-fade level from
            # ramp.yuv; maskedmerge lays it over the idle. The stage sets both crops' x as expressions of t, so
            # the click starts on an exact frame whenever a command arrives ahead of it.
            cw_, ch_ = click["w"], click["h"]
            raw = lambda path, w, h: ("-f", "rawvideo", "-pix_fmt", "yuv420p", "-video_size", f"{w}x{h}",
                                      "-framerate", "1", "-i", path)
            i_cg = add(*raw(click["grid"], cw_ * click["n"], ch_ * click["mouths"]))
            i_cr = add(*raw(click["ramp"], cw_ * (CLICK_LEVELS + 1), ch_))
            hold = f"loop=loop=-1:size=1:start=0,settb=1/{cfg.fps},setpts=N"
            cam += (f"[{i_cg}:v]{hold},crop@clk={cw_}:{ch_}:0:0[clk];"
                    f"[{i_cr}:v]{hold},crop@clkm={cw_}:{ch_}:0:0[clkm];"
                    f"[camsel][clk][clkm]maskedmerge[camraw];")
    else:
        i_idle = add("-stream_loop", "-1", "-i", str(idle))
        i_talk = add("-stream_loop", "-1", "-i", str(talk))
        i_mix = add(*loop_img(d / "mix.png", cfg.fps))
        cam = (f"[{i_mix}:v]format=gray[mix];"
               f"[{i_talk}:v][mix]alphamerge[talk];"
               f"[{i_idle}:v][talk]overlay=0:0[camraw];")
    if g.framed:
        return ins, _graph_h(cfg, g, d, add, loop_img, i_game, cam, n)
    i_mask = add(*loop_img(d / "cammask.png", cfg.fps))
    i_frame = add("-i", str(d / "camframe.png"))
    i_panel = add(*loop_img(d / "panel.png", PANEL_FPS))
    i_plate = add("-i", str(d / "plate.png"))
    i_chat = add(*loop_img(d / "chat.png", CHAT_FPS))
    i_spins = add(*loop_img(d / "spins.png", PANEL_FPS))
    i_card = add(*loop_img(d / "card.png", CARD_FPS))
    sx, sy, sw, sh = g.plate
    px, py, _, _ = g.panel
    bx, by, _, _ = g.chat
    rx, ry, _, _ = g.spins
    scale = f"scale={gw}:{gh}:flags=bilinear," if game_scale else ""
    fc = (f"[0:v][{i_chrome}:v]overlay=0:0:eof_action=repeat[c0];"
          f"[{i_game}:v]{scale}format=yuv420p[game];"
          f"[c0][game]overlay={gx}:{gy}:eof_action=repeat[c1];"
          + cam +
          f"[{i_mask}:v]format=gray[mask];"
          f"[camraw][mask]alphamerge[cam];"
          # the switching card covers the whole game box (top bar included) until the stage says a game runs;
          # it starts shown, so a fresh encoder never shows a site page before the stage has spoken
          f"[c1][{i_card}:v]overlay@card={gx}:{gy}:enable=1[c1b];"
          f"[c1b][cam]overlay={cx}:{cy}[c2];"
          f"[c2][{i_frame}:v]overlay={cx - pad}:{cy - pad}:eof_action=repeat[c3];"
          f"[c3][{i_panel}:v]overlay={px}:{py}[c4];"
          f"[c4][{i_chat}:v]overlay={bx}:{by}[c4b];"
          f"[c4b][{i_spins}:v]overlay={rx}:{ry}[c5];"
          # subtitles: drawtext re-reads its file for every frame drawn and the plate is switched by a
          # command, so both land on the frame the voice is on (an image layer showed up to a second late)
          f"[c5][{i_plate}:v]overlay@plate={sx}:{sy}:eof_action=repeat:enable=0,"
          + ",".join(_sub_text(cfg, g, k) for k in ("sub_top", "sub_bot", "sub_mid")) +
          f",zmq=bind_address='tcp\\://127.0.0.1\\:{cfg.mouth_port}'[scene]")
    return ins, fc


def _game_src(cfg) -> tuple[int, int]:
    if cfg.scene_bg == "x11":
        return tuple(cfg.game_grab[2:]) if cfg.game_grab else (cfg.width, cfg.height)
    try:
        with Image.open(cfg.scene_bg_image) as im:
            return im.size
    except OSError:
        return cfg.width, cfg.height


def _graph_h(cfg, g: Geo, d: Path, add, loop_img, i_game: int, cam: str, n_next: int) -> str:
    """Look H: clock -> game window (scaled + cropped to the originals' panel or the slot iframe) -> switching card
    -> cam -> the static room/frame layer (rounded holes) -> pane (header + footer) -> chat -> his line in the bubble
    -> the dimming veil (on between lines) -> [scene]. The room and frame are one still image read once."""
    gx, gy, gw, gh = g.game
    cx, cy, cw, ch = g.cam
    i_frame = add("-i", str(d / "frame.png"))
    # the pane is read at the card's rate: each looped image runs ~9 frames behind (the encoder's read-ahead), so at
    # 2 fps the header named the next game ~2.5 s after the card did; at the same rate they change together
    i_panel = add(*loop_img(d / "panel.png", CARD_FPS))
    i_chat = add(*loop_img(d / "chat.png", CHAT_FPS))
    i_card = add(*loop_img(d / "card.png", CARD_FPS))
    i_veil = add("-i", str(d / "veil.png"))
    st = load_state(d)
    scr = st.get("screen") or {}
    kind = view_kind(g, str(scr.get("state") or ""), scr.get("game") or "")
    sw, sh, vx, vy = game_view(g, kind, *_game_src(cfg))
    px, py, _, _ = g.panel
    bx, by, _, _ = g.chat
    vx0, vy0, _, _ = g.veil
    pad = g.game_pad
    padf = f"pad={gw}:{gh}:0:0:color={PANEL_BG}," if pad else ""
    return (f"[{i_game}:v]scale@gz={sw}:{sh}:flags=bicubic,crop@gc={gw}:{gh - pad}:{vx}:{vy},{padf}format=yuv420p[game];"
            f"[0:v][game]overlay={gx}:{gy}:eof_action=repeat[c1];"
            + cam +
            # the switching card covers the game window until the stage says a game runs; it starts shown
            f"[c1][{i_card}:v]overlay@card={gx}:{gy}:enable=1[c1b];"
            f"[c1b][camraw]overlay={cx}:{cy}[c2];"
            f"[c2][{i_frame}:v]overlay=0:0:eof_action=repeat[c3];"
            f"[c3][{i_panel}:v]overlay={px}:{py}[c4];"
            f"[c4][{i_chat}:v]overlay={bx}:{by},"
            + ",".join(_sub_text(cfg, g, k) for k in ("sub_top", "sub_bot", "sub_mid")) +
            f"[c5];[c5][{i_veil}:v]overlay@veil={vx0}:{vy0}:eof_action=repeat:enable=1,"
            f"zmq=bind_address='tcp\\://127.0.0.1\\:{cfg.mouth_port}'[scene]")


def _sub_text(cfg, g: Geo, key: str) -> str:
    x, y, w, h = g.plate
    font = UBUNTU if Path(UBUNTU).exists() else FALLBACK
    if g.is_mix:
        # his line in the white card, dark ink, left-aligned after the avatar; fixed baselines like H
        f = MX.FONTS["is"] if MX.FONTS["is"].exists() else font
        base = MX.LINE_BASE[key]
        return (f"drawtext@{key}=fontfile={f}:textfile={Path(cfg.scene_dir) / (key + '.txt')}:reload=1:"
                f"expansion=none:fontsize={g.s(MX.LINE_SIZE)}:fontcolor=0x101e30:x={g.s(MX.LINE_X)}:"
                f"y={g.s(base)}-max_glyph_a")
    if g.is_h:
        # left-aligned in the bubble; baselines fixed (y = baseline - this line's ascent) so the two lines and the
        # one-line case sit where the design has them whatever letters they hold
        base = {"sub_top": 542, "sub_bot": 573, "sub_mid": 557}[key]
        return (f"drawtext@{key}=fontfile={font}:textfile={Path(cfg.scene_dir) / (key + '.txt')}:reload=1:"
                f"expansion=none:fontsize={g.s(H_SUB_SIZE)}:fontcolor=0xf0f6f8:x={g.s(80)}:y={g.s(base)}-max_glyph_a")
    lh = g.s(44)
    top = y + (h - 2 * lh) // 2 + g.s(2)
    ys = {"sub_top": top, "sub_bot": top + lh, "sub_mid": top + lh // 2}[key]
    font = UBUNTU if Path(UBUNTU).exists() else FALLBACK
    # expansion=none: the text comes from chat and the model, a % in it must stay a %
    return (f"drawtext@{key}=fontfile={font}:textfile={Path(cfg.scene_dir) / (key + '.txt')}:reload=1:"
            f"expansion=none:fontsize={g.s(34)}:fontcolor=0xecf4f7:x={x + w // 2}-text_w/2:y={ys}")
