"""House originals at a zero bet: the in-flight guard, the answer checks, round outcomes, the 80/20 rotation and
the zero-bet commentary. Offline: fixtures recorded live on 2026-11-08 (tests/fixtures/housebets_originals.json)."""
import asyncio
import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from hubie.games import housebets as hb
from hubie.games.base import GameError, GameGone, GameInfo, RoundResult

FIX = json.loads((Path(__file__).parent / "fixtures" / "housebets_originals.json").read_text())["exchanges"]
USDT = {1}


def ex(path, method="POST", nth=0):
    found = [x for x in FIX if x["path"] == path and x["method"] == method]
    return copy.deepcopy(found[nth])


# ── the in-flight request guard ──

def test_real_zero_bets_pass():
    for x in FIX:
        if x["method"] == "POST" and x["path"].endswith("/bet"):
            ok, why = hb.check_original_request("https://games.housebets.com" + x["path"], json.dumps(x["request"]),
                                                set(), USDT)
            assert ok, why


@pytest.mark.parametrize("change", [{"amount": 0.01}, {"amount": "0.5"}, {"amount": 1e-8}, {"amount": None},
                                    {"amount": True}, {"currency": 2}, {"currency": "usdc"}, {"stake": 1},
                                    {"betAmount": 3}])
def test_any_amount_or_other_currency_is_blocked(change):
    body = dict(ex("/dice/bet")["request"], **change)
    ok, why = hb.check_original_request("https://games.housebets.com/dice/bet", json.dumps(body), set(), USDT)
    assert not ok and why


def test_bet_without_amount_or_currency_or_unreadable_is_blocked():
    url = "https://games.housebets.com/limbo/bet"
    assert not hb.check_original_request(url, json.dumps({"currency": 1}), set(), USDT)[0]
    assert not hb.check_original_request(url, json.dumps({"amount": 0}), set(), USDT)[0]
    assert not hb.check_original_request(url, "amount=0", set(), USDT)[0]
    assert not hb.check_original_request(url, None, set(), USDT)[0]
    assert not hb.check_original_request(url, json.dumps({"amount": 0, "currency": 1}), set(), set())[0]   # USDT unknown


def test_actions_only_on_rounds_seen_starting_at_zero():
    x = ex("/mines2/action")
    url = "https://games.housebets.com/mines2/action"
    assert not hb.check_original_request(url, json.dumps(x["request"]), set(), USDT)[0]
    assert hb.check_original_request(url, json.dumps(x["request"]), {x["request"]["roundId"]}, USDT)[0]


def test_other_posts_only_when_not_money_like():
    assert hb.check_original_request("https://games.housebets.com/user/preferences", "{}", set(), USDT)[0]
    for p in ("/dice/autobet", "/mines2/cashout", "/wallet/tip", "/vault/deposit"):
        assert not hb.check_original_request("https://games.housebets.com" + p, "{}", set(), USDT)[0]


def test_zero_spellings():
    assert all(hb._is_zero(v) for v in (0, 0.0, "0", "0.00000000", ".0"))
    assert not any(hb._is_zero(v) for v in (None, False, "", "0.01", 1e-9, "abc", []))
    assert all(hb.bet_field_zero(v) for v in ("", "0", "0.00000000", " 0.0 ", None))
    assert not any(hb.bet_field_zero(v) for v in ("0.01", "1", "0.00000001", "max"))


# ── the answer checks ──

def test_recorded_answers_are_all_ok():
    for x in FIX:
        if x["method"] == "POST" or x["path"].endswith("/game"):
            assert hb.check_original_response(x["response"], USDT) == ("ok", ""), x["path"]


def test_a_stake_or_payout_in_the_answer_is_real():
    d = ex("/dice/bet")["response"]
    d["game"]["game"]["amount"] = 0.5
    assert hb.check_original_response(d, USDT)[0] == "real"
    d = ex("/mines2/bet")["response"]
    d["game"]["stake"] = 1
    assert hb.check_original_response(d, USDT)[0] == "real"
    d = ex("/road/action", nth=1)["response"]
    d["round"]["payout"] = 2
    assert hb.check_original_response(d, USDT)[0] == "real"


def test_wrong_currency_or_settled_without_round_stops():
    d = ex("/limbo/bet")["response"]
    d["game"]["game"]["currency"] = 2
    assert hb.check_original_response(d, USDT)[0] == "stop"
    d = ex("/plinko2/bet")["response"]
    del d["round"]
    assert hb.check_original_response(d, USDT)[0] == "stop"
    assert hb.check_original_response({"nope": 1}, USDT)[0] == "stop"


# ── round outcomes ──

def test_outcomes_from_recorded_rounds():
    g = hb.ORIG_BY_ID
    dice = hb.original_outcome(g["dice"], ex("/dice/bet")["response"], None)
    assert dice["outcome"] == "rolled 45.10, needed over 50.5" and dice["multiplier"] == 0 and not dice["won"]
    limbo = hb.original_outcome(g["limbo"], ex("/limbo/bet")["response"], None)
    assert limbo["outcome"] == "limbo 3.35x vs 2x target" and limbo["multiplier"] == 2 and limbo["won"]
    plinko = hb.original_outcome(g["plinko"], ex("/plinko2/bet")["response"], None)
    assert plinko["outcome"].startswith("plinko 0.48x") and not plinko["won"]
    mines = hb.original_outcome(g["mines"], ex("/mines2/bet")["response"], ex("/mines2/action", nth=1)["response"], 1)
    assert mines["outcome"] == "1 gem then cashed out 1.12x" and mines["won"] and mines["picks"] == 1
    road = hb.original_outcome(g["hubies_road"], ex("/road/bet")["response"], ex("/road/action", nth=1)["response"])
    assert road["outcome"] == "road: 1 step, cashed out 1.12x" and road["won"]


def test_outcomes_when_it_hits():
    mines = ex("/mines2/action", nth=1)["response"]
    mines["game"].update(hit=4, opened=[6, 4], outcome="hit", multiplier=0)
    out = hb.original_outcome(hb.ORIG_BY_ID["mines"], {}, mines, 2)
    assert out["outcome"] == "1 gem then a mine" and out["multiplier"] == 0 and not out["won"]
    mines["game"].update(hit=4, opened=[4])
    assert hb.original_outcome(hb.ORIG_BY_ID["mines"], {}, mines, 1)["first_click"]
    road = ex("/road/action", nth=1)["response"]
    road["game"].update(hit=True, position=3, trapLane=4, outcome="hit")
    out = hb.original_outcome(hb.ORIG_BY_ID["hubies_road"], {}, road)
    assert out["outcome"] == "road: 3 safe steps, hit on lane 4" and out["multiplier"] == 0 and not out["first_click"]
    road["game"].update(hit=1, position=0, trapLane=1, outcome="trap")      # live 2026-11-08: shark on the first hop
    out = hb.original_outcome(hb.ORIG_BY_ID["hubies_road"], {}, road)
    assert out["outcome"] == "road: hit on lane 1, the first hop" and out["first_click"]


def test_click_allow_list():
    hb.assert_original_clickable("BET", "Bet")
    hb.assert_original_clickable("CASH OUT", "Cash out 0.00000000")
    hb.assert_original_clickable("TILE", "", "Tile 7")
    with pytest.raises(GameGone):
        hb.assert_original_clickable("DEPOSIT", "Deposit")
    with pytest.raises(GameGone):
        hb.assert_original_clickable("LIMBO TARGET", "max", "Max bet")
    with pytest.raises(GameError):
        hb.assert_original_clickable("BET", "Start Autobet")
    with pytest.raises(GameError):
        hb.assert_original_clickable("CASH OUT", "Cash out 5.00")


def test_source_original_clicks_only_go_through_the_allow_list():
    src = Path(hb.__file__).read_text()
    sec = src[src.index("    # ── house originals, zero bet ──"):src.index("    async def _recover(self):")]
    labels = set(re.findall(r"_orig_(?:click|press|type|pick_text|click_el|find)\(\s*\"([^\"]+)\"", sec))
    assert labels and labels <= set(hb.ORIGINAL_CLICKS)
    # both raw clicks come right after an ON_TARGET check and an allow-list check
    for m in re.finditer(r"await self\._page\.mouse\.click\(x, y\)", sec):
        before = sec[max(0, m.start() - 500):m.start()]
        assert "ON_TARGET_JS" in before and "assert_original_clickable" in before


# ── the adapter's route and answer reader, with fakes ──

def adapter():
    a = hb.HousebetsSlotsAdapter.__new__(hb.HousebetsSlotsAdapter)
    hb.HousebetsSlotsAdapter.__init__(a, games=["sweet_bonanza_1000"])
    a._L.loop.call_soon_threadsafe(a._L.loop.stop)
    return a


class FakeRoute:
    def __init__(self, url, body, method="POST"):
        self.request = SimpleNamespace(url=url, post_data=body, method=method)
        self.done = None

    async def continue_(self):
        self.done = "continue"

    async def abort(self):
        self.done = "abort"


class FakeResp:
    def __init__(self, path, data, method="GET", status=200, body=None):
        self.url = "https://games.housebets.com" + path
        self.status = status
        self.request = SimpleNamespace(method=method, post_data=json.dumps(body) if body else None)
        self._t = json.dumps(data)

    async def text(self):
        return self._t


def test_route_lets_zero_bets_through_and_hard_stops_the_rest():
    a = adapter()
    a._usdt_ids = {1}
    ok = FakeRoute("https://games.housebets.com/dice/bet", json.dumps(ex("/dice/bet")["request"]))
    asyncio.run(a._route_original(ok))
    assert ok.done == "continue" and not a._orig_stop
    get = FakeRoute("https://games.housebets.com/limits/dice", None, "GET")
    asyncio.run(a._route_original(get))
    assert get.done == "continue"
    bad = FakeRoute("https://games.housebets.com/dice/bet", json.dumps(dict(ex("/dice/bet")["request"], amount=0.01)))
    asyncio.run(a._route_original(bad))
    assert bad.done == "abort" and a._orig_stop
    assert all(g.kind == "slot" for g in a.list_games())                # originals gone for the session
    with pytest.raises(GameGone):
        a.load_game("dice")
    again = FakeRoute("https://games.housebets.com/dice/bet", json.dumps(ex("/dice/bet")["request"]))
    asyncio.run(a._route_original(again))                                # even a zero bet is refused now
    assert again.done == "abort"
    assert not a._hard_stop                                              # slots carry on


def test_reader_learns_usdt_and_zero_rounds_and_stops_on_a_stake():
    a = adapter()
    asyncio.run(a._read_original(FakeResp("/limits/mines2", ex("/limits/mines2", "GET")["response"])))
    assert a._usdt_ids == {1} and a._orig_limits["mines2"] == (1, "USDT")
    asyncio.run(a._read_original(FakeResp("/user/balance?currency=1", {"balance": 0})))
    bet = ex("/mines2/bet")
    asyncio.run(a._read_original(FakeResp("/mines2/bet", bet["response"], "POST", body=bet["request"])))
    assert bet["response"]["game"]["roundId"] in a._zero_rounds and a._orig_q[-1]["ok"]
    left = ex("/road/game", "GET")["response"]
    asyncio.run(a._read_original(FakeResp("/road/game", left)))
    assert a._orig_active["road"] == left and left["game"]["roundId"] in a._zero_rounds
    staked = copy.deepcopy(bet["response"])
    staked["game"]["stake"] = 0.5
    asyncio.run(a._read_original(FakeResp("/mines2/bet", staked, "POST")))
    assert a._orig_stop and a._hard_stop                                  # money may have moved: all play stops


def test_reader_stops_when_the_game_balance_goes_down():
    a = adapter()
    asyncio.run(a._read_original(FakeResp("/user/balance?currency=3", {"balance": 0.0065})))
    asyncio.run(a._read_original(FakeResp("/user/balance?currency=3", {"balance": 0.0060})))
    assert a._orig_stop and a._hard_stop


def test_live_wallet_feed_going_down_stops_everything():
    a = adapter()
    handlers = {}
    a._on_ws(SimpleNamespace(on=lambda ev, fn: handlers.__setitem__(ev, fn)))
    frame = {"type": "snapshot", "topic": "wallet", "data": {"currency": "usdc", "cash_balance": "0.00649690",
                                                             "bonus_balance": "0.00000000"}}
    handlers["framereceived"](json.dumps(frame))
    frame["data"]["cash_balance"] = "0.00700000"                          # money in from outside: fine
    handlers["framereceived"](json.dumps(frame))
    assert not a._hard_stop
    frame["data"]["cash_balance"] = "0.00600000"
    handlers["framereceived"](json.dumps(frame))
    assert a._hard_stop and a._orig_stop


# ── rotation and commentary ──

def test_scheduler_gives_originals_about_80_percent(monkeypatch):
    from hubie.brain.memory import Memory
    from hubie.brain.scheduler import Scheduler
    games = [GameInfo(f"s{i}", f"S{i}", "slot", "") for i in range(6)] + \
            [GameInfo(g.id, g.name, "original", g.how) for g in hb.ORIGINALS]
    sched = Scheduler(Memory(None), ["x"], seed=3)
    picks = []
    for _ in range(1000):
        gid = sched._pick_game(games)
        sched.state["recent_games"] = (sched.state["recent_games"] + [gid])[-20:]
        picks.append(gid)
    share = sum(p in hb.ORIG_BY_ID for p in picks) / len(picks)
    assert 0.75 < share < 0.85
    assert {g.id for g in hb.ORIGINALS} <= set(picks)
    monkeypatch.setenv("HUBIE_HB_ORIGINALS_SHARE", "0")
    assert all(sched._pick_game(games).startswith("s") for _ in range(50))
    seg = sched.next([g for g in games if g.kind == "original"], force_play=True)
    assert seg.kind == "play" and "spin" not in seg.title


def test_zero_bet_result_reads_as_a_win_or_loss_without_money():
    from hubie.brain import prompts
    r = RoundResult("limbo", "x", 0.0, 2.0, 0.0, None, True,
                    {"zero_bet": True, "won": True, "outcome": "limbo 3.35x vs 2x target"})
    assert r.zero_bet and r.won and r.came_back
    lost = RoundResult("dice", "y", 0.0, 0.0, 0.0, None, True, {"zero_bet": True, "won": False, "outcome": "rolled 1"})
    assert not lost.won and not lost.came_back
    assert RoundResult("s", "z", 1.0, 2.0, 2.0, 5.0, True).won           # slots unchanged
    p = prompts.short_round_prompt(r, "Limbo", [])
    assert "limbo 3.35x vs 2x target" in p and "spin" not in p.split("\n")[0] and "dollar" not in p
    full = prompts.round_prompt(lost, "Dice", "house games", [])
    assert "lost" in full and "broke even" not in full and "zero bet" in full


# ── the orchestrator side ──

def test_game_gone_drops_the_game_and_play_goes_on():
    from test_orchestrator import build
    from hubie.brain.scheduler import PLAY, Segment
    from hubie.games.fake import FakeGameAdapter

    class Gone(FakeGameAdapter):
        def play_round(self, bet, options=None):
            if self.current == "dice":
                raise GameGone("house originals off: test")
            return super().play_round(bet, options)

    orch, speaker, clock = build(adapter=Gone(seed=1, round_seconds=(0, 0)))
    orch.games()
    orch.adapter.load_game("dice")
    seg = Segment(PLAY, "x", target=5, game_id="dice")
    orch.segment = seg
    orch.play_one(seg)
    assert seg.finished and not orch.refuse_play and orch.consecutive_fails == 0
    assert "dice" not in {g.id for g in orch.games()} and orch.benched["dice"] == float("inf")
    assert not any(k == "system" for k, _ in speaker.lines)               # no stall line


def test_zero_bet_rounds_get_a_line_each_without_money_words():
    from test_orchestrator import build
    orch, speaker, clock = build()
    for mult, won, text in ((0.0, False, "rolled 22.54, needed over 33"), (2.0, True, "limbo 3.57x vs 2x target"),
                            (0.49, False, "plinko 0.49x, low risk"), (1.12, True, "1 gem then cashed out 1.12x")):
        r = RoundResult("dice", "r", 0.0, mult, 0.0, None, True, {"zero_bet": True, "won": won, "outcome": text})
        for short in (True, False):
            line = orch.brain.comment_round(r, "Dice", "house games", short=short).text.lower()
            assert line and "dollar" not in line and "cents" not in line and "nothing back" not in line


def test_per_game_zoom(monkeypatch):
    from hubie.games import housebets as hb
    monkeypatch.delenv("HUBIE_HB_ORIG_ZOOM_DICE", raising=False)
    assert hb.orig_zoom("dice") == 0.85
    assert hb.orig_zoom("road") == float(hb.ORIG_ZOOM)
    monkeypatch.setenv("HUBIE_HB_ORIG_ZOOM_DICE", "0.8")
    assert hb.orig_zoom("dice") == 0.8
    monkeypatch.setenv("HUBIE_HB_ORIG_ZOOM_DICE", "junk")
    assert hb.orig_zoom("dice") == 0.9


def test_human_path_lands_exactly_and_varies():
    import random
    from hubie.games.housebets import human_path
    rng = random.Random(7)
    durs, shapes = [], set()
    for k in range(200):
        x0, y0, x1, y1 = (rng.uniform(100, 1200) for _ in range(4))
        p = human_path(x0, y0, x1, y1, rng, overshoot=(k % 3 == 0))
        assert p[-1][:2] == (x1, y1)
        assert all(d >= 0 for *_, d in p)
        dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
        total = sum(d for *_, d in p)
        if dist > 50:
            assert 0.15 < total < 2.0
            # not a straight line: the middle of the path is off the chord
            mx, my = p[len(p) // 2][:2]
            off = abs((x1 - x0) * (y0 - my) - (x0 - mx) * (y1 - y0)) / dist
            shapes.add(round(off))
        durs.append(round(total, 2))
    assert len(set(durs)) > 50 and max(shapes) > 10
