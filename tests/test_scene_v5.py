"""Game switch card, switch line, dead-air asides, recent-spins strip, play segment labels, Housebets top bar."""
import asyncio
import json
import sys

from hubie.config import ROOT

sys.path.insert(0, str(ROOT / "tests"))
from test_orchestrator import TimedSpeaker, build, run  # noqa: E402

from hubie.brain.scheduler import PLAY, PLAY_TITLES, Scheduler, Segment  # noqa: E402
from hubie.brain.memory import Memory  # noqa: E402


def test_game_switch_puts_the_card_up_says_where_it_goes_then_shows_the_game():
    events = []
    orch, sp, clock = build(seed=3)
    orch.notify = events.append
    orch.games()
    orch.sched.next = lambda games: Segment(PLAY, "spinning slowly", target=3, game_id="max_win")
    orch.start_segment()
    states = [e for e in events if e["type"] == "game_state"]
    assert [s["state"] for s in states] == ["loading", "ready"]
    name = orch.game_names["max_win"]
    assert states[0]["game"] == name
    kind, text = sp.lines[0]
    assert kind == "segment" and name.lower() in text and len(text.split()) <= 9


def test_card_stays_up_while_the_adapter_says_the_screen_is_not_safe():
    events = []
    orch, sp, clock = build(seed=3)
    orch.notify = events.append
    orch.adapter.screen_ready = lambda: False
    orch.games()
    orch.sched.next = lambda games: Segment(PLAY, "spinning slowly", target=3, game_id="max_win")
    orch.start_segment()
    orch.step()
    assert [e["state"] for e in events if e["type"] == "game_state"] == ["loading", "recovering"]
    orch.adapter.screen_ready = lambda: True
    clock[0] += 5
    orch.step()
    assert events[-1]["type"] == "game_state" and events[-1]["state"] == "ready" or \
        [e["state"] for e in events if e["type"] == "game_state"][-1] == "ready"


def test_a_failed_round_shows_the_recovering_card():
    events = []
    orch, sp, clock = build(seed=3)
    orch.notify = events.append

    def boom(bet, opts=None):
        raise RuntimeError("frame gone")
    orch.adapter.play_round = boom
    orch.games()
    orch.segment = Segment(PLAY, "spinning slowly", target=5, game_id="max_win")
    orch.step()
    assert [e["state"] for e in events if e["type"] == "game_state"][-1] == "recovering"


def test_long_silence_gets_a_pool_aside_but_planned_quiet_does_not():
    clock = [1_000_000.0]
    # talk_pace under 1 so the rest is honoured (it used to pass only because the quiet clock ran on through lines)
    orch, _, clock = build(now=clock, fill_after=10.0, talk_pace=0.33)
    sp = TimedSpeaker(clock)
    orch.speaker = sp
    orch.segment = Segment("chat_floor", "Open floor", target=99)
    orch.rest_until = clock[0] + 60          # a long rest between talk lines
    calls = dict(orch.brain.llm_calls)
    out = run(orch, clock, 60, dt=0.25)      # 15 s
    assert "aside" in out and orch.counts["asides"] == 1
    assert orch.brain.llm_calls == calls     # pool only
    orch2, _, clock2 = build(fill_after=10.0)
    orch2.speaker = TimedSpeaker(clock2)
    orch2.quiet_until = clock2[0] + 30       # after a crisis line, or a talk segment's planned gap
    orch2.segment = Segment("chat_floor", "Open floor", target=99)
    assert "aside" not in run(orch2, clock2, 80, dt=0.25)


def test_a_slow_game_load_gets_waiting_asides():
    clock = [1_000_000.0]
    orch, _, clock = build(now=clock, fill_after=10.0)
    sp = TimedSpeaker(clock)
    orch.speaker = sp

    def slow_load(gid):
        tick = orch._waiting("wait_loading")
        for _ in range(12):                  # 24 s on the test clock, ticking like _call_with_timeout does
            clock[0] += 2
            tick()
    orch.adapter.load_game = slow_load
    orch.games()
    orch.sched.next = lambda games: Segment(PLAY, "spinning slowly", target=3, game_id="max_win")
    orch.start_segment()
    kinds = [k for k, _ in sp.lines]
    assert kinds[0] == "segment" and kinds.count("aside") >= 1
    texts = [t for k, t in sp.lines if k == "aside"]
    pool = {x.lower() for x in orch.brain.persona.canned["wait_loading"]}
    assert all(t in pool for t in texts)


def test_play_segments_get_hubie_titles_never_now_playing():
    mem = Memory()
    sch = Scheduler(mem, ["a"], seed=1)
    titles = [sch.play_title() for _ in range(20)]
    assert all(t != "Now playing" for t in titles)
    assert all(a != b for a, b in zip(titles, titles[1:]))
    assert set(titles) <= set(PLAY_TITLES) | {"late night spins", "slots after dark", "quiet night spins"}
    from hubie.stage.desk import seg_label
    assert seg_label("Now playing") == "spinning slowly" and seg_label("now late night spins") == "late night spins"
    assert seg_label("tall tale") == "tall tale"


def test_recent_spins_strip_and_card_draw(tmp_path):
    from hubie.stage.desk import Geo, draw_card, draw_card_base, draw_chrome, draw_spins, recent_item, short_game
    g = Geo(1280, 720)
    gx, gy, gw, gh = g.game
    sx, sy, sw, sh = g.spins
    px, py, pw, ph = g.plate
    assert sy >= py + ph and sy + sh <= 720 - g.strip_h and sy >= gh
    it = recent_item({"game": "Sweet Bonanza Super Scatter", "multiplier": 0.77, "won": False,
                      "detail": {"bonus_buy": True, "free_spins_triggered": True}})
    assert it == {"g": "Bonanza SS", "m": 0.77, "w": False, "buy": True, "fs": True}
    assert short_game("Gates of Olympus 1000") == "Olympus 1000"
    img = draw_spins(g, draw_chrome(g), [it] * 6)
    assert img.size == (sw, sh)
    for st in ("loading", "recovering", "offline"):
        c = draw_card(g, draw_card_base(g, st, "Gates of Olympus 1000"), st, "Gates of Olympus 1000", 0.7)
        assert c.size == (gw, gh)


def test_desk_card_follows_game_state_and_rounds_fill_the_strip(tmp_path, monkeypatch):
    from test_desk_scene import _cfg
    from hubie.stage.desk import DeskScene
    monkeypatch.setenv("HUBIE_SPINS_STRIP", "1")      # the strip is off by default since 033dd8d; this checks it fills
    cfg = _cfg(tmp_path, monkeypatch)
    sent = []

    async def go():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        s.apply({"type": "game_state", "state": "loading", "game": "Fruit Party"})
        assert sent[-1] == ("card", "overlay@card enable 1")
        card0 = (cfg.scene_dir / "card.png").read_bytes()
        await asyncio.sleep(0.6)
        assert (cfg.scene_dir / "card.png").read_bytes() != card0     # the bar moves
        s.apply({"type": "game_state", "state": "ready", "game": "Fruit Party"})
        assert sent[-1] == ("card", "overlay@card enable 0")
        spins0 = (cfg.scene_dir / "spins.png").read_bytes()
        s.apply({"type": "round", "game": "Fruit Party", "multiplier": 1.4, "won": True, "balance": 99.0,
                 "detail": {}})
        assert (cfg.scene_dir / "spins.png").read_bytes() != spins0
        st = json.loads((cfg.scene_dir / "session.json").read_text())
        assert st["recent"][0]["g"] == "Fruit Party" and st["screen"]["state"] == "ready"

    asyncio.run(go())


def test_card_and_strip_are_in_the_encoder_graph_card_on_by_default(tmp_path, monkeypatch):
    from test_desk_scene import _cfg
    from hubie.stage.desk import filter_inputs
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_CAM_MODE="switch")
    from hubie.stage import desk
    monkeypatch.setattr(desk, "cam_clip", lambda cfg, p: p)
    ins, fc = filter_inputs(cfg, [], 1)
    assert "overlay@card=0:0:enable=1" in fc and "spins.png" in " ".join(ins) and "card.png" in " ".join(ins)


def test_housebets_game_view_maps_game_points_under_the_top_bar():
    from hubie.games.housebets import TOPBAR_H, GameView
    v = GameView.make(1280, 720, topbar=False)
    assert v.pt((990, 650)) == (990, 650) and v.box((1, 2, 3, 4)) == (1, 2, 3, 4)
    v = GameView.make(1280, 720, topbar=True)
    assert v.y0 == TOPBAR_H and v.h == 720 - TOPBAR_H and abs(v.w / v.h - 16 / 9) < 0.01
    assert v.x0 + v.w <= 1280 and v.x0 >= 0
    x, y = v.pt((1280, 720))
    assert abs(x - (v.x0 + v.w)) < 1 and abs(y - 720) < 1
    assert "top:80px" in v.css() and "body::after" in v.css()


def test_top_bar_must_read_in_game_with_no_balance_left_of_wallet():
    from hubie.games.housebets import topbar_safe
    good = [["Housebets", 50], ["In Game", 470], ["Wallet", 600], ["Monthly Prize", 790], ["$100,000.00", 790],
            ["Position", 905], ["8000th", 905]]
    assert topbar_safe(good)[0]
    assert not topbar_safe([["Housebets", 50], ["$12.34", 470], ["Wallet", 600]])[0]
    assert not topbar_safe([["In Game", 470], ["0.00031", 440], ["Wallet", 600]])[0]
    assert not topbar_safe([["In Game", 470]])[0]
    assert not topbar_safe("garbage")[0]


def test_browsing_switch_shows_the_page_says_the_pick_and_never_names_it_first():
    events = []
    clock = [1_000_000.0]
    orch, _, clock = build(now=clock, fill_after=10.0)
    sp = TimedSpeaker(clock)
    orch.speaker = sp
    orch.notify = events.append
    orch.adapter.browse = True
    phases = iter(["loading", "browsing", "browsing", "picked", "picked", "loading", "ready"])
    cur = ["loading"]
    orch.adapter.screen_state = lambda: cur[0]

    def browse_load(gid):
        tick = orch._loading_tick(gid)
        for p in phases:
            cur[0] = p
            clock[0] += 2.5
            tick()
    orch.adapter.load_game = browse_load
    orch.games()
    orch.sched.next = lambda games: Segment(PLAY, "spinning slowly", target=3, game_id="max_win")
    orch.start_segment()
    name = orch.game_names["max_win"]
    p = orch.brain.persona.canned
    first = sp.lines[0][1]
    assert first in {x.lower() for x in p["browse_start"]} and name.lower() not in first
    pool = {x.format(game=name).lower() for x in p["browse_pick"]}
    assert len([t for k, t in sp.lines if t in pool]) == 1
    states = [e["state"] for e in events if e["type"] == "game_state"]
    assert states[0] == "loading" and "browsing" in states and states[-1] == "ready"


def test_desk_hides_the_card_while_browsing(tmp_path, monkeypatch):
    from test_desk_scene import _cfg
    from hubie.stage.desk import DeskScene
    cfg = _cfg(tmp_path, monkeypatch)
    sent = []

    async def go():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        s.apply({"type": "game_state", "state": "loading", "game": "Fruit Party"})
        s.apply({"type": "game_state", "state": "browsing", "game": "Fruit Party"})
        assert sent[-1] == ("card", "overlay@card enable 0") and not s.card_up

    asyncio.run(go())


def test_tile_click_allow_list_is_exact():
    import pytest
    from hubie.games.base import NotDemoError
    from hubie.games.housebets import GAMES, assert_tile_clickable
    assert_tile_clickable("/games/pragmaticplay-sweet-bonanza-1000", GAMES)
    for bad in ("/games/pragmaticplay-sweet-bonanza-1000?mode=real", "/games/slots", "/wallet", "/games/other-game",
                "https://evil/games/pragmaticplay-sweet-bonanza-1000", "", "/games/pragmaticplay-sweet-bonanza-1000/real"):
        with pytest.raises(NotDemoError):
            assert_tile_clickable(bad, GAMES)


def test_browsing_never_shows_unless_the_balance_patch_is_proven():
    from hubie.games.housebets import HousebetsSlotsAdapter
    a = HousebetsSlotsAdapter.__new__(HousebetsSlotsAdapter)
    a._hard_stop = []

    async def no(*_):
        return False

    async def yes(*_):
        return True
    a._patched = no
    asyncio.run(a._show_if_patched("browsing"))
    assert a.screen_state() == "loading"
    a._patched = yes
    asyncio.run(a._show_if_patched("browsing"))
    assert a.screen_state() == "browsing"
    a._screen_ok = True
    assert a.screen_state() == "ready"
