"""Spin click clip: the stage plans it on the encoder's clock, the adapter holds the spin until the claw lands,
reactions keep their rules around it, and the encoder graph plays it from memory. Needs the v3 mouth sets
(assets/cam_visemes_v3 and its click/ folder); the planning and adapter parts run without them."""
import asyncio
import subprocess
import sys
import time

import pytest

from hubie.audio import lipsync as L
from hubie.config import ROOT
from hubie.games.base import GameAdapter

sys.path.insert(0, str(ROOT / "tests"))
from test_desk_scene import _cfg  # noqa: E402

V3 = ROOT / "assets/cam_visemes_v3"
needs_v3 = pytest.mark.skipif(not (V3 / f"click/mouth_{len(L.NAMES) - 1}.mp4").exists()
                              or not (V3 / f"mouth_{len(L.NAMES) - 1}.mp4").exists(), reason="v3 mouth sets not built")
needs_v1 = pytest.mark.skipif(not (ROOT / f"assets/cam_visemes/mouth_{len(L.NAMES) - 1}.mp4").exists(),
                              reason="v1 mouth set not built")


def _v3(tmp_path, monkeypatch, **env):
    return _cfg(tmp_path, monkeypatch, HUBIE_MOUTH_DIR=str(V3), HUBIE_CLICK_MOUTH_DIR=str(V3 / "click"),
                HUBIE_CAM_ZOOM="1.0", **env)


class Cue(GameAdapter):
    name = "cue"
    def start(self): pass
    def list_games(self): return []
    def load_game(self, gid): pass
    def play_round(self, bet, options=None): pass
    def is_demo(self): return True
    def recover(self): pass


# ---- adapter side: a spin is never lost or held long ----

def test_click_at_holds_only_for_a_sane_answer():
    a = Cue()
    assert a.click_at() is None                                  # no stage: click at once
    a.click_cue = lambda why: time.time() + 1.0
    assert a.click_at() == pytest.approx(time.time() + 1.0, abs=0.05)
    a.click_cue = lambda why: None
    assert a.click_at() is None
    a.click_cue = lambda why: time.time() + 30                   # nonsense plan: never hold a spin that long
    assert a.click_at() is None
    a.click_cue = lambda why: time.time() - 1                    # already past
    assert a.click_at() is None

    def boom(why):
        raise OSError("stage down")
    a.click_cue = boom
    assert a.click_at() is None


def test_housebets_click_hold_waits_for_the_claw_and_runs_the_guard_meanwhile():
    from hubie.games.housebets import HousebetsSlotsAdapter as HousebetsAdapter
    h = HousebetsAdapter.__new__(HousebetsAdapter)
    seen = []
    h.click_cue = lambda why: (seen.append(why), time.time() + 0.4)[1]

    async def guard():
        seen.append("guard")

    async def run():
        t = time.time()
        await h._click_hold("buy", guard())
        return time.time() - t
    took = asyncio.run(run())
    assert seen == ["buy", "guard"] and 0.35 < took < 0.6
    h.click_cue = None
    assert asyncio.run(run()) < 0.05                              # no click clip: no wait at all

    def slow(why):
        time.sleep(2)
    h.click_cue = slow
    assert asyncio.run(run()) < 0.8                               # a stage that hangs costs half a second at most


# ---- stage side ----

def test_click_expression_frames_and_fades():
    from hubie.stage.desk import click_expr
    x, m = click_expr(30, 2.0, 56, 376, 1, 1)
    assert x == "376*clip(floor((t-2.0000)*30+0.5),0,55)"
    assert "if(lt(" in m and "gte(" in m and "round(16*" in m


@needs_v3
def test_click_assets_paw_and_full(tmp_path, monkeypatch):
    from hubie.stage.desk import click_assets
    paw = click_assets(_v3(tmp_path, monkeypatch))
    assert paw["mode"] == "paw" and paw["mouths"] == 1 and paw["n"] == 56 and paw["idle_frames"] == 150
    assert (paw["w"], paw["h"]) == (376, 250)
    full = click_assets(_v3(tmp_path, monkeypatch, HUBIE_CLICK_MODE="full"))
    assert full["mode"] == "full" and full["mouths"] == len(L.NAMES) and full["grid"] != paw["grid"]


@needs_v3
def test_v1_reactions_are_left_out_on_the_v3_idle(tmp_path, monkeypatch):
    from hubie.stage.desk import reaction_paths
    assert reaction_paths(_v3(tmp_path, monkeypatch)) == {}


@needs_v1
def test_v1_reactions_still_match_the_v1_idle(tmp_path, monkeypatch):
    from hubie.stage.desk import reaction_paths
    got = reaction_paths(_cfg(tmp_path, monkeypatch))
    assert set(got) == {"win", "loss", "surprise", "banana"}


@needs_v3
def test_click_cue_plans_on_the_encoder_clock_and_skips_when_it_must(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    from hubie.util import write_status
    cfg = _v3(tmp_path, monkeypatch)
    sent = []

    async def run():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        assert s.apply({"type": "click"}) == {"skip": "no encoder clock"}
        write_status(cfg.status_dir, "encoder_clock", frame=300, fps=30)
        now = time.time()
        r = s.apply({"type": "click", "why": "spin"})
        # paw mode: the first frame at least CLICK_LEAD ahead, no waiting for a loop phase
        assert r["start"] - now == pytest.approx(s.CLICK_LEAD, abs=0.06)
        assert r["at"] - r["start"] == pytest.approx(cfg.click_land_s, abs=1e-6)
        keys = [k for k, _ in sent]
        assert "clk_x" in keys and "clk_m" in keys and "clk_y" not in keys     # the face is never touched
        assert f"t-{r['frame'] / 30:.4f}" in dict(sent)["clk_x"]
        assert s.apply({"type": "click"}) == {"skip": "click playing"}
        s.click_win = (0, 0)
        s.reacting = True
        assert s.apply({"type": "click"}) == {"skip": "reaction playing"}
    asyncio.run(run())


@needs_v3
def test_full_mode_waits_for_a_close_idle_frame_and_mouths_follow(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    from hubie.util import write_status
    cfg = _v3(tmp_path, monkeypatch, HUBIE_CLICK_MODE="full")
    sent = []

    async def run():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        c = s.click
        write_status(cfg.status_dir, "encoder_clock", frame=150 + 60, fps=30)   # idle mid-loop, far from frame 0
        r = s.click_cue()
        cost = lambda f: c["din"][f % 150] + c["dout"][(f + c["n"]) % 150]
        first = r["frame"] - round((r["start"] - time.time()) * 30)
        assert r["start"] - time.time() <= s.CLICK_LEAD + cfg.click_max_wait + 0.1
        assert cost(r["frame"]) <= cost(first) + 1e-6
        assert ("clk_y", f"crop@clk y {c['h'] * 0}") in sent
        s.set_mouth(3)                                               # he talks through it: the click's mouth row
        assert ("clk_y", f"crop@clk y {c['h'] * 3}") in sent and sent[-1] == ("cam", "streamselect@mouth map 3")
    asyncio.run(run())


@needs_v1
def test_reaction_waits_for_a_click_and_banana_gives_way(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    from hubie.util import write_status
    cfg = _cfg(tmp_path, monkeypatch)
    sent = []

    async def run():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        s.loop_s = 0.6
        write_status(cfg.status_dir, "encoder_clock", frame=0, fps=30)
        now = time.time()
        s.click_win = (now + 0.1, now + 1.0)                         # a click planned across the next boundary
        assert s.react("win")
        await asyncio.sleep(0.75)
        assert not any("map 9" in c for _, c in sent)                # held back while the click plays
        await asyncio.sleep(1.2)
        assert ("cam", f"streamselect@mouth map {s.reactions['win']}") in sent
        await asyncio.sleep(0.7)
        sent.clear()
        s.last_react -= 100
        s.click_win = (time.time() + 0.1, time.time() + 1.0)
        assert s.react("banana")
        await asyncio.sleep(1.5)
        assert not any(f"map {s.reactions['banana']}" in c for _, c in sent)   # the click wins, no banana
    asyncio.run(run())


@needs_v3
def test_encoder_graph_plays_the_click_from_memory(tmp_path, monkeypatch):
    """The desk graph with the click inputs parses and runs in ffmpeg (two seconds, still background)."""
    from hubie.stage.desk import filter_inputs, prepare
    cfg = _v3(tmp_path, monkeypatch)
    prepare(cfg)
    ins, fc = filter_inputs(cfg, [])
    assert "crop@clk=" in fc and "crop@clkm=" in fc and "maskedmerge[camraw]" in fc
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30", *ins,
           "-filter_complex", fc.replace("[scene]", "[out]"), "-map", "[out]", "-t", "2", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
