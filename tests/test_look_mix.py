"""Look MIX (HUBIE_LOOK=mix, the default since 2026-11-09): geometry against the frame's holes, the static frame (green
LIVE badge, pink and teal rims, white card, no gold), the pill row, the chat (newest at the bottom), the persistent dimmed
line in the card, no round-history row, the exact safety strip with ice pipes on drawtext's own pipes, the game views
(top bar never in the window, the originals' panel whole) and the encoder graph."""
import asyncio
import sys

import numpy as np
import pytest
from PIL import Image

from hubie.audio import lipsync as L
from hubie.config import ROOT

sys.path.insert(0, str(ROOT / "tests"))
from test_desk_scene import _cfg  # noqa: E402

V3 = ROOT / "assets/cam_visemes_v3"
needs_v3 = pytest.mark.skipif(not (V3 / f"click/mouth_{len(L.NAMES) - 1}.mp4").exists(), reason="v3 mouth sets not built")


@pytest.fixture(autouse=True)
def look_mix(monkeypatch):
    monkeypatch.setenv("HUBIE_LOOK", "mix")


def _m(tmp_path, monkeypatch, **env):
    return _cfg(tmp_path, monkeypatch, HUBIE_LOOK="mix", HUBIE_MOUTH_DIR=str(V3), HUBIE_CLICK_MOUTH_DIR=str(V3 / "click"),
                HUBIE_CAM_ZOOM="1.0", HUBIE_CAM_IDLE=str(ROOT / "shots/kling_idle_v3.mp4"),
                **{f"HUBIE_REACT_{k.upper()}": str(ROOT / f"shots/kling_react_{k}_v3.mp4")
                   for k in ("win", "loss", "surprise", "banana")}, **env)


def test_mix_is_the_default_and_the_others_stay(monkeypatch):
    from hubie.stage.desk import Geo, look_name
    monkeypatch.delenv("HUBIE_LOOK")
    assert look_name() == "mix" and Geo(1280, 720).is_mix and Geo(1280, 720).framed
    monkeypatch.setenv("HUBIE_LOOK", "h")
    assert look_name() == "h" and Geo(1280, 720).game == (54, 74, 742, 392)
    assert Geo(1280, 720, "desk1").game == (0, 0, 880, 496)
    monkeypatch.setenv("HUBIE_LOOK", "nonsense")
    assert look_name() == "mix"


def test_preview_runs_mix_with_music_rotation():
    sys.path.insert(0, str(ROOT / "tools"))
    import preview
    assert preview.SHOW_ENV["HUBIE_LOOK"] == "mix" and "HUBIE_LOOK" in preview.PASS_THROUGH
    assert preview.SHOW_ENV["HUBIE_MUSIC_PACKS"] == "house,lofi,lounge,night"
    assert preview.SHOW_ENV["HUBIE_MUSIC_PACK_TRACKS"] == "5" and preview.SHOW_ENV["HUBIE_MUSIC_GAIN_DB"] == "-8"


def test_windows_sit_in_the_frame_holes_and_nothing_overlaps():
    from hubie.stage.desk import Geo, draw_frame_mix
    g = Geo(1280, 720)
    assert g.game == (408, 24, 848, 480) and g.cam == (24, 24, 360, 240)
    a = np.asarray(draw_frame_mix(g))[..., 3]
    assert a.shape == (720, 1280)
    for x, y, w, h in (g.game, g.cam):
        assert w % 2 == 0 and h % 2 == 0 and x % 2 == 0 and y % 2 == 0
        inner = a[y + 20:y + h - 40, x + 130:x + w - 20]            # clear of the badge and the plate
        assert inner.max() == 0
        assert a[y + h // 2, x - 8] == 255 and a[y + h // 2, x + w + 8] == 255   # frame all round
        assert a[y, x + w // 2] > 0 and a[y + h - 1, x + w // 2] > 0             # the rim glows over the edge
    gx, gy, gw, gh = g.game
    cx, cy, cw, ch = g.cam
    bx, by, bw, bh = g.chat
    kx, ky, kw, kh = g.plate          # the card
    px, py, pw, ph = g.panel          # the pill layer
    assert cx + cw < gx and cy + ch < by and by + bh <= 720 - g.strip_h
    assert gy + gh < ky and ky + kh <= py + 20 and py + ph <= 720 - g.strip_h
    vx, vy, vw, vh = g.veil
    assert kx < vx and vx + vw <= kx + kw and ky < vy and vy + vh <= ky + kh
    assert g.spins is None and g.game_pad == 12
    big = Geo(1920, 1080)
    assert big.game[2] / 1920 == pytest.approx(848 / 1280, abs=0.002) and draw_frame_mix(big).size == (1920, 1080)


def test_frame_has_kick_live_badge_rims_white_card_and_no_gold():
    import cv2
    from hubie.stage.desk import Geo, KICK_GREEN, draw_chrome_mix
    from hubie.stage import look_mix as MX
    g = Geo(1280, 720)
    img = np.asarray(draw_chrome_mix(g)).astype(int)
    badge = img[36:60, 36:110].reshape(-1, 3)
    assert (np.abs(badge - np.array(KICK_GREEN)).sum(1) < 30).sum() > 600
    assert (badge.sum(1) < 120).sum() > 40                       # dark text and dot on it
    teal = img[300, 405]                                         # the game's left rim
    pink = img[150, 21]                                          # the cam's left rim
    assert teal[1] > 150 and teal[2] > 150 and pink[0] > 180 and pink[1] < 160
    card = img[600:612, 1100:1200]
    assert card.mean() > 215                                     # the white card
    plate = img[256:274, 50:180]
    assert plate.mean() < 90                                     # the name plate on the cam's bottom edge
    hsv = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_RGB2HSV)
    gold = (hsv[..., 0] >= 17) & (hsv[..., 0] <= 30) & (hsv[..., 1] > 110) & (hsv[..., 2] > 140)
    for x, y, w, h in (g.game, g.cam):
        gold[y:y + h, x:x + w] = False
    assert gold.sum() < 50
    assert MX.PLATE_Y + 28 > g.cam[1] + g.cam[3] > MX.PLATE_Y   # straddles the edge: the paw and mouse stay clear


def test_pills_name_the_stats_and_the_game():
    from hubie.stage.desk import Geo, draw_chrome_mix, draw_panel, h_footer_mix
    from hubie.stage import look_mix as MX
    st = {"live_since": 1000.0, "spins": 412, "best": 8.04, "game": "Hubie's Road"}
    assert h_footer_mix(st, 1000.0 + 2 * 3600 + 14 * 60 + 7) == [
        ("Live", "2:14:07", MX.INK), ("Rounds", "412", MX.INK), ("Best", "8.04x", MX.BEST)]
    g = Geo(1280, 720)
    img = draw_panel(g, draw_chrome_mix(g), st, 1000.0)
    assert img.size == g.panel[2:] and img.mode == "RGBA"
    a = np.asarray(img).astype(int)
    py = MX.PILLS_Y - MX.PILL_LAYER[1]
    row = a[py + 4:py + 34]
    assert (row[..., 3] == 255).sum() > 5000                     # opaque white pills
    assert ((row[..., :3].sum(-1) < 200) & (row[..., 3] == 255)).sum() > 300   # dark ink on them
    assert a[2, 2, 3] < 40                                       # transparent around them
    # a very long game name is cut, never runs into the stats
    long = draw_panel(g, draw_chrome_mix(g), dict(st, game="An Extremely Long Provider Slot Name " * 4), 1000.0)
    assert long.size == img.size


def test_chat_newest_at_the_bottom_names_coloured_his_teal_and_long_lines_wrap():
    from hubie.stage.desk import TEAL, Geo, draw_chat, draw_chrome_mix
    from hubie.stage import look_mix as MX
    g = Geo(1280, 720)
    img = draw_chat(g, draw_chrome_mix(g), [{"name": "Kokax", "user": "Kokax", "text": "yo hubie"},
                                            {"hubie": True, "text": "slow and steady, bruh."}])
    a = np.asarray(img).astype(int)
    assert img.size == g.chat[2:]
    last = a[-32:, :60].reshape(-1, 3)
    assert (np.abs(last - np.array(TEAL)).sum(1) < 60).sum() > 20
    assert a[:150].max(axis=2).max() < 120                       # empty space on top, filled from the bottom
    rows = MX.chat_rows("mintyfresh", "road next pls and can you also try plinko on high risk with sixteen rows later", 328)
    assert 2 <= len(rows) <= 3
    rows = MX.chat_rows("x", "word " * 200, 328)
    assert len(rows) == 3 and rows[-1].endswith("...")
    empty = np.asarray(draw_chat(g, draw_chrome_mix(g), [])).astype(int)
    assert empty[-90:].max() > 80                                # the dim hint, at the bottom


def test_card_fits_the_window():
    from hubie.stage.desk import Geo, card_bar_y, draw_card, draw_card_base
    g = Geo(1280, 720)
    for state, game in (("loading", "Sweet Bonanza 1000"), ("recovering", "Hubie's Road"), ("offline", "")):
        img = draw_card(g, draw_card_base(g, state, game), state, game, 0.7)
        assert img.size == g.game[2:] and card_bar_y(g, game, state) + 10 < g.game[3] - 20


def test_lines_fit_the_card():
    from hubie.stage.desk import Geo, pages, sub_font, sub_width
    from hubie.stage import look_mix as MX
    g = Geo(1280, 720)
    long = "damn. first hop, straight into the shark. the shark had other plans and honestly, respect, " * 3
    for p in pages(g, long):
        assert len(p.split("\n")) <= 2
        for ln in p.split("\n"):
            assert sub_font(g).getlength(ln) <= sub_width(g)
    assert MX.LINE_X + MX.LINE_W <= MX.CARD[2] - 16
    assert MX.LINE_BASE["sub_bot"] + 6 < MX.CARD[3]


def test_game_views_never_show_the_top_bar_and_keep_the_whole_panel():
    from hubie.games.housebets import TOPBAR_H
    from hubie.stage.desk import Geo, game_view, view_kind
    g = Geo(1280, 720)
    _, _, gw, gh = g.game
    ch = gh - g.game_pad
    assert view_kind(g, "ready", "Keno") == "original" and view_kind(g, "ready", "Blackjack") == "original:blackjack"
    assert view_kind(g, "browsing", "Mines") == "slot" and view_kind(g, "ready", "Sweet Bonanza 1000") == "slot"
    for kind in ("original", "slot"):
        sw, sh, x, y = game_view(g, kind)
        k = sw / 1280
        assert sw % 2 == 0 and sh % 2 == 0 and x + gw <= sw and y + ch <= sh
        assert y / k >= TOPBAR_H - 0.5                         # the balance row (y 0..79) is never in the window
        src = (x / k, y / k, (x + gw) / k, (y + ch) / k)
        if kind == "original":   # 0.9 zoom: controls from x 148, plinko's history column to x 1194, play areas to y 662+
            assert src[0] <= 146 and src[2] >= 1200 and 664 <= src[3] <= 670
    # limbo and blackjack: shorter panels, the view stops above their footer bars (y 594 / 628)
    assert view_kind(g, "ready", "Limbo") == "original:limbo" and view_kind(g, "ready", "Dice") == "original"
    for game, bottom in (("Limbo", (588, 594)), ("Blackjack", (622, 628))):
        sw, sh, x, y = game_view(g, view_kind(g, "ready", game))
        k = sw / 1280
        assert y / k >= TOPBAR_H - 0.5 and x / k <= 146 and bottom[0] <= (y + ch) / k <= bottom[1]
        assert x + gw <= sw and y + ch <= sh


def test_dice_zoom_follows_the_look(monkeypatch):
    from hubie.games import housebets as hb
    assert hb.orig_zoom("dice") == 0.9 and hb.orig_zoom("road") == 0.9
    monkeypatch.setenv("HUBIE_LOOK", "h")
    assert hb.orig_zoom("dice") == 0.85


def test_strip_text_exact_with_ice_pipes_on_drawtexts_own(tmp_path, monkeypatch):
    from hubie.pipeline.commands import _strip_and_out, strip_pipes_drawn
    from hubie.safety.overlay import STRIP_TEXT
    from hubie.stage import look_mix as MX
    assert [w.strip() for w in STRIP_TEXT.split("|")] == ["18+", "Gamble responsibly", "AI-generated character",
                                                           "Demo play, no real money"]
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_LOOK="mix")
    fc = _strip_and_out(cfg, "scene", 5)
    assert (cfg.run_dir / "strip.txt").read_text() == STRIP_TEXT
    assert f"textfile={cfg.run_dir / 'strip.txt'}" in fc and "[5:a]" in fc and "[probe]" in fc
    assert fc.count("text='|'") == 3 and "0x96dcff" in fc and "Inter_28pt-SemiBold" in fc
    pipes = strip_pipes_drawn(str(MX.FONTS["is"]), 15)
    assert len(pipes) == 3 and pipes[0][0] < pipes[1][0] < pipes[2][0] < pipes[0][1]


@needs_v3
def test_card_keeps_his_last_line_dimmed_between_lines(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    cfg = _m(tmp_path, monkeypatch)
    sc = DeskScene(cfg)
    sent = []
    sc.link.send = lambda k, c: sent.append((k, c))
    sc.SUB_HOLD = 0.05

    async def run():
        await sc._speak({"type": "speak", "text": "damn. first hop, straight into the shark.", "duration": 0.2})
    asyncio.run(run())
    d = cfg.scene_dir
    assert (d / "sub_mid.txt").read_text() == "damn. first hop, straight into the shark."
    veil = [c for k, c in sent if k == "veil"]
    assert veil[0] == "overlay@veil enable 0" and veil[-1] == "overlay@veil enable 1"
    assert not any(k == "plate" for k, _ in sent)
    sent.clear()
    sc.set_screen("loading", "Keno")
    assert ("gc_y", "crop@gc y 64") in sent and ("gz_w", "scale@gz w 1020") in sent
    sc.set_screen("ready", "Sweet Bonanza 1000")
    assert any(k == "gz_w" and c != "scale@gz w 1020" for k, c in sent)
    assert not (d / "spins.png").exists()
    assert Image.open(d / "panel.png").mode == "RGBA"


@needs_v3
def test_encoder_graph_mix(tmp_path, monkeypatch):
    from hubie.pipeline.commands import scene_cmd
    cfg = _m(tmp_path, monkeypatch)
    cmd = scene_cmd(cfg)
    fc = cmd[cmd.index("-filter_complex") + 1]
    j = " ".join(cmd)
    for want in ("scale@gz=", "crop@gc=848:468:", "pad=848:480:0:0:color=0x0c1a22", "overlay@card=408:24",
                 "overlay=24:24", "overlay=392:598", "overlay=28:330", "overlay@veil=488:556", "frame.png",
                 "streamselect@mouth", "drawtext@sub_mid", "fontcolor=0x101e30", "strip.txt"):
        assert want in fc or want in j, want
    for gone in ("spins.png", "cammask.png", "camframe.png", "chrome.png", "overlay@plate"):
        assert gone not in j, gone
    assert "_360x240_" in j
    assert Image.open(cfg.scene_dir / "frame.png").size == (1280, 720)
