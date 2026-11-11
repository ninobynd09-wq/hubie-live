"""Look H (HUBIE_LOOK=h, the default on the stream): geometry against the frame's holes, the static frame (green
LIVE badge, no gold), the pane, the persistent dimmed bubble, no round-history row, the exact safety strip, the
game views (the site's top bar with the balance never in the window) and the encoder graph."""
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
def look_h(monkeypatch):
    monkeypatch.setenv("HUBIE_LOOK", "h")


def _h(tmp_path, monkeypatch, **env):
    return _cfg(tmp_path, monkeypatch, HUBIE_LOOK="h", HUBIE_MOUTH_DIR=str(V3), HUBIE_CLICK_MOUTH_DIR=str(V3 / "click"),
                HUBIE_CAM_ZOOM="1.0", HUBIE_CAM_IDLE=str(ROOT / "shots/kling_idle_v3.mp4"),
                **{f"HUBIE_REACT_{k.upper()}": str(ROOT / f"shots/kling_react_{k}_v3.mp4")
                   for k in ("win", "loss", "surprise", "banana")}, **env)


def test_look_switch():
    from hubie.stage.desk import Geo, look_name
    assert look_name() == "h" and Geo(1280, 720).is_h
    assert Geo(1280, 720, "desk1").game == (0, 0, 880, 496)


def test_windows_sit_in_the_frame_holes_and_nothing_overlaps():
    from hubie.stage.desk import Geo, draw_frame_h
    g = Geo(1280, 720)
    assert g.game == (54, 74, 742, 392) and g.cam == (846, 74, 380, 204)
    a = np.asarray(draw_frame_h(g))[..., 3]
    for x, y, w, h in (g.game, g.cam):
        assert w % 2 == 0 and h % 2 == 0 and x % 2 == 0 and y % 2 == 0
        inner = a[y + 14:y + h - 14, x + 14:x + w - 14]
        assert inner.max() == 0 or (x, y) == g.cam[:2]          # the cam hole holds the badge and the pill
        assert a[y + h // 2, x - 6] == 255 and a[y + h // 2, x + w + 6] == 255   # frame all round
        assert a[y, x + w // 2] > 0 and a[y + h, x + w // 2] > 0                 # the video tucks under the border
    gx, gy, gw, gh = g.game
    cx, cy, cw, ch = g.cam
    px, py, pw, ph = g.pane
    bx, by, bw, bh = g.plate
    assert gx + gw < cx and cy + ch < py and gy + gh < by and by + bh <= 720 - g.strip_h
    chx, chy, chw, chh = g.chat
    assert px <= chx and chx + chw <= px + pw and py + 46 < chy and chy + chh < py + 277   # between the dividers
    assert g.spins is None                                       # no round-history row in H
    big = Geo(1920, 1080)
    assert big.game[2] / 1920 == pytest.approx(742 / 1280, abs=0.002) and draw_frame_h(big).size == (1920, 1080)


def test_frame_has_kick_green_live_badge_pill_and_no_gold():
    import cv2
    from hubie.stage.desk import Geo, KICK_GREEN, draw_chrome_h
    g = Geo(1280, 720)
    img = np.asarray(draw_chrome_h(g)).astype(int)
    badge = img[85:110, 856:940].reshape(-1, 3)
    green = (np.abs(badge - np.array(KICK_GREEN)).sum(1) < 30).sum()
    red = ((badge[:, 0] > 180) & (badge[:, 1] < 90)).sum()
    assert green > 600 and red == 0
    assert (badge.sum(1) < 120).sum() > 40                       # dark text and dot on it
    pill = img[275:296, 925:1145]
    assert pill.mean() < 80                                      # the navy name pill over the cam's bottom edge
    hsv = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_RGB2HSV)
    gold = (hsv[..., 0] >= 17) & (hsv[..., 0] <= 30) & (hsv[..., 1] > 110) & (hsv[..., 2] > 140)
    for x, y, w, h in (g.game, g.cam):
        gold[y:y + h, x:x + w] = False
    assert gold.sum() == 0


def test_pane_footer_says_rounds_and_header_names_the_game():
    from hubie.stage.desk import Geo, draw_chrome_h, draw_panel, h_footer, h_game_title
    st = {"live_since": 1000.0, "spins": 412, "best": 8.04, "game": "Hubie's Road"}
    assert h_footer(st, 1000.0 + 2 * 3600 + 14 * 60 + 7) == [
        ("live", "2:14:07", (236, 244, 247)), ("rounds", "412", (236, 244, 247)), ("best", "8.04x", (64, 224, 208))]
    assert h_footer({"live_since": 0, "spins": 0, "best": 0}, 5)[2][1] == "-"
    assert h_game_title({"screen": {"game": "Dice"}}) == "Dice" and h_game_title({}) == "picking a game"
    g = Geo(1280, 720)
    img = draw_panel(g, draw_chrome_h(g), st, 1000.0)
    assert img.size == g.pane[2:]
    a = np.asarray(img).astype(int)
    assert (a[10:36, 10:120].max(axis=2) > 200).sum() > 60       # the game name in the header
    assert (a[285:305, 10:300].max(axis=2) > 200).sum() > 60     # the footer values


def test_chat_names_viewers_coloured_his_name_teal():
    from hubie.stage.desk import TEAL, Geo, draw_chat, draw_chrome_h
    g = Geo(1280, 720)
    img = draw_chat(g, draw_chrome_h(g), [{"name": "Kokax", "user": "Kokax", "text": "yo hubie"},
                                           {"hubie": True, "text": "slow and steady, bruh."}])
    a = np.asarray(img).astype(int)
    last = a[-30:, :60].reshape(-1, 3)                          # his line, newest at the bottom
    assert (np.abs(last - np.array(TEAL)).sum(1) < 60).sum() > 20
    assert img.size == g.chat[2:]


def test_card_fits_the_window():
    from hubie.stage.desk import Geo, card_bar_y, draw_card, draw_card_base
    g = Geo(1280, 720)
    for state, game in (("loading", "Sweet Bonanza 1000"), ("recovering", "Hubie's Road"), ("offline", "")):
        img = draw_card(g, draw_card_base(g, state, game), state, game, 0.7)
        assert img.size == g.game[2:] and card_bar_y(g, game, state) + 10 < g.game[3] - 20


def test_game_views_never_show_the_top_bar_and_fit_the_game():
    from hubie.games.housebets import TOPBAR_H, GameView
    from hubie.stage.desk import Geo, game_view, is_original
    g = Geo(1280, 720)
    _, _, gw, gh = g.game
    assert is_original("Hubie's Road") and is_original("plinko") and not is_original("Sweet Bonanza 1000")
    for kind in ("original", "slot"):
        sw, sh, x, y = game_view(g, kind)
        k = sw / 1280
        assert sw % 2 == 0 and sh % 2 == 0 and x + gw <= sw and y + gh <= sh
        assert y / k >= TOPBAR_H - 0.5                         # the balance row (y 0..79) is never in the window
        src = (x / k, y / k, (x + gw) / k, (y + gh) / k)
        if kind == "slot":                                     # the whole provider iframe
            v = GameView.make(1280, 720, topbar=True)
            assert src[0] <= v.x0 and src[2] >= v.x0 + v.w and src[3] >= 718
        else:                                                  # the 0.9-zoom game panel down to "Total profit"
            assert src[0] <= 120 and src[2] >= 1214 and src[3] >= 660 and src[1] >= 72


def test_strip_text_exact_with_teal_pipes(tmp_path, monkeypatch):
    from hubie.pipeline.commands import _strip_and_out, strip_pipes
    from hubie.safety.overlay import STRIP_TEXT
    from hubie.stage.desk import POPPINS
    # the words exactly; the strip has always spaced its pipes wider ("18+   |   Gamble ...")
    assert [w.strip() for w in STRIP_TEXT.split("|")] == ["18+", "Gamble responsibly", "AI-generated character",
                                                           "Demo play, no real money"]
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_LOOK="h")
    fc = _strip_and_out(cfg, "scene", 5)
    assert (cfg.run_dir / "strip.txt").read_text() == STRIP_TEXT
    assert f"textfile={cfg.run_dir / 'strip.txt'}" in fc and "[5:a]" in fc and "[probe]" in fc
    assert fc.count("text='|'") == 3 == len(strip_pipes(POPPINS, 19))


@needs_v3
def test_bubble_keeps_his_last_line_dimmed_between_lines(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    cfg = _h(tmp_path, monkeypatch)
    sc = DeskScene(cfg)
    sent = []
    sc.link.send = lambda k, c: sent.append((k, c))
    sc.SUB_HOLD = 0.05

    async def run():
        await sc._speak({"type": "speak", "text": "damn. first hop, straight into the shark.", "duration": 0.2})
    asyncio.run(run())
    d = cfg.scene_dir
    assert (d / "sub_mid.txt").read_text() == "damn. first hop, straight into the shark."   # still there after
    veil = [c for k, c in sent if k == "veil"]
    assert veil[0] == "overlay@veil enable 0" and veil[-1] == "overlay@veil enable 1"
    assert not any(k == "plate" for k, _ in sent)
    # a new line lights it back up
    sc.show_sub("next line")
    assert sent[-1] == ("veil", "overlay@veil enable 0") and (d / "sub_mid.txt").read_text() == "next line"
    # a game switch points the window at the right view
    sent.clear()
    sc.set_screen("loading", "Sweet Bonanza 1000")
    assert ("gz_w", "scale@gz w 784") in sent and ("gc_y", "crop@gc y 49") in sent
    sc.set_screen("ready", "Mines")
    assert ("gz_w", "scale@gz w 854") in sent
    assert not (d / "spins.png").exists()


@needs_v3
def test_encoder_graph_h(tmp_path, monkeypatch):
    from hubie.pipeline.commands import scene_cmd
    cfg = _h(tmp_path, monkeypatch)
    cmd = scene_cmd(cfg)
    fc = cmd[cmd.index("-filter_complex") + 1]
    j = " ".join(cmd)
    for want in ("scale@gz=", "crop@gc=742:392:", "overlay@card=54:74", "overlay=846:74", "overlay@veil=62:498",
                 "frame.png", "streamselect@mouth", "maskedmerge", "drawtext@sub_mid", "strip.txt"):
        assert want in fc or want in j, want
    for gone in ("spins.png", "cammask.png", "camframe.png", "chrome.png", "overlay@plate"):
        assert gone not in j, gone
    assert "_380x204_" in j                                    # cam clips cut for the new window
    assert Image.open(cfg.scene_dir / "frame.png").size == (1280, 720)
