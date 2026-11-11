"""More drama in the house originals (2026-11-08): plinko bursts of 3-5 balls in one round, mines "all in" (deep on
few mines, always said first), and the chat asks for both. Every ball is its own zero-bet request through the guard."""
import asyncio
import random
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from hubie.brain.scheduler import PLAY, Segment
from hubie.games import housebets as hb
from hubie.games import plays
from hubie.games.base import GameGone, RoundResult

sys.path.insert(0, str(Path(__file__).parent))


# ── plans ──

def test_plinko_bursts_about_one_round_in_four_three_to_five_balls():
    rng = random.Random(7)
    ps = [plays.plan_round("plinko", rng) for _ in range(4000)]
    c = Counter(p["balls"] for p in ps)
    assert set(c) == {1, 3, 4, 5}
    assert 0.2 < (len(ps) - c[1]) / len(ps) < 0.3
    burst = next(p for p in ps if p["balls"] == 5)
    assert burst["short"] == "5 balls" and burst["text"].endswith(", 5 balls") and burst["risky"]
    one = next(p for p in ps if p["balls"] == 1)
    assert "balls" not in one["text"] and "balls" not in one["short"]


def test_mines_all_in_about_one_round_in_six_deep_on_few_mines_and_announced():
    rng = random.Random(8)
    ps = [plays.plan_round("mines", rng) for _ in range(6000)]
    allin = [p for p in ps if p.get("allin")]
    assert 0.13 < len(allin) / len(ps) < 0.2
    assert {p["mines"] for p in allin} == {1, 2, 3} and {p["depth"] for p in allin} == {8, 9, 10}
    assert all(p["announce"] and p["risky"] for p in allin)
    assert allin[0]["short"] == f"{allin[0]['depth']} deep on {allin[0]['mines']} mine" + \
        ("s" if allin[0]["mines"] != 1 else "")
    normal = [p for p in ps if not p.get("allin")]
    assert all(1 <= p["depth"] <= 6 and not p.get("announce") for p in normal)


def test_chat_asks_override_the_dice_no_surprise_burst_or_all_in():
    rng = random.Random(9)
    for _ in range(300):
        assert plays.plan_round("plinko", rng, {"risk": "High"})["balls"] == 1
        assert not plays.plan_round("mines", rng, {"mines": 5}).get("allin")
    p = plays.plan_round("plinko", rng, {"balls": 5, "name": "Binlala"})
    assert p["balls"] == 5 and p["asked_by"] == "Binlala"
    p = plays.plan_round("mines", rng, {"allin": True, "depth": 9, "mines": 3})
    assert (p["mines"], p["depth"], p["allin"]) == (3, 9, True)
    p = plays.plan_round("mines", rng, {"allin": True, "depth": 24, "mines": 20})
    assert p["depth"] == 4                              # never more tiles than are safe to pick


@pytest.mark.parametrize("text,current,want", [
    ("drop 5 balls", "limbo", ("plinko", {"balls": 5})),
    ("5 balls", "plinko", ("plinko", {"balls": 5})),
    ("drop five balls high risk", None, ("plinko", {"balls": 5, "risk": "High"})),
    ("20 balls", "plinko", ("plinko", {"balls": 5})),     # capped at five
    ("go all in", "mines", ("mines", {"allin": True})),
    ("all in", "plinko", ("mines", {"allin": True})),
    ("9 deep", "mines", ("mines", {"allin": True, "depth": 9})),
    ("go nine deep", "mines", ("mines", {"allin": True, "depth": 9})),
    ("3 mines 9 deep", None, ("mines", {"allin": True, "depth": 9, "mines": 3})),
    ("go all in 1 mine", None, ("mines", {"allin": True, "mines": 1})),
])
def test_drama_chat_asks_are_read(text, current, want):
    assert plays.parse_request(text, current) == want


@pytest.mark.parametrize("text", ["go all in 100x", "all in $5", "all in real money", "max bet 9 deep",
                                  "go all in with my balance", "deposit and drop 5 balls"])
def test_all_in_with_money_or_a_target_is_still_not_a_play_ask(text):
    assert plays.parse_request(text, "mines") is None


def test_request_text_for_the_new_asks():
    assert plays.request_text("plinko", {"balls": 5}) == "5 balls"
    assert plays.request_text("mines", {"allin": True}) == "going way deep"
    assert plays.request_text("mines", {"allin": True, "depth": 9, "mines": 3}) == "3 mines, 9 deep"


# ── the burst result ──

def test_burst_outcome_lists_every_ball_and_pays_the_average():
    outs = [{"multiplier": m, "risk": "high"} for m in (0.5, 1.1, 3, 0.3, 0.7)]
    o = hb.burst_outcome(outs)
    assert o["outcome"].startswith("5 balls: 0.5x, 1.1x, 3x, 0.3x, 0.7x, best 3x")
    assert o["multiplier"] == pytest.approx(1.12) and o["won"] and o["best"] == 3 and o["burst"] == 5
    o = hb.burst_outcome([{"multiplier": 0.2}, {"multiplier": 0.5}, {"multiplier": 1.1}])
    assert not o["won"] and hb.outcome_class(o["multiplier"]) == "loss"


class FakeAdapter:
    """Just enough of HousebetsAdapter for _orig_burst: every press is checked by the real request guard."""

    def __init__(self, answers, missing_after=None):
        self._usdt_ids = {7}
        self._orig_stop, self._hard_stop = [], []
        self.answers = list(answers)
        self.presses, self.guards = [], 0
        self.missing_after = missing_after
        self._page = self

    async def wait_for_timeout(self, ms):
        pass

    async def _orig_guard(self):
        self.guards += 1

    async def _orig_press(self, label, kind, pick=None, paw=False, wait=0.0):
        assert label == "BET" and kind == "bet" and not paw
        if self.missing_after is not None and len(self.presses) >= self.missing_after:
            raise hb.GameError("Plinko: BET not on screen")
        ok, why = hb.check_original_request("https://games.housebets.com/plinko2/bet",
                                            '{"amount": 0, "currency": 7}', set(), self._usdt_ids)
        assert ok, why
        self.presses.append(label)
        return self.answers.pop(0)

    _orig_breach = hb.HousebetsSlotsAdapter._orig_breach
    _orig_burst = hb.HousebetsSlotsAdapter._orig_burst


def ans(m, amount=0, payout=0):
    return {"game": {"multiplier": m, "risk": "high", "rows": 12,
                     "game": {"amount": amount, "currency": 7, "payoutMultiplier": m}},
            "round": {"stake": 0, "payout": payout}}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_burst_balls_go_one_at_a_time_each_guarded_and_checked():
    a = FakeAdapter([ans(1.1), ans(3), ans(0.3), ans(0.7)])
    g = hb.ORIG_BY_ID["plinko"]
    out = run(a._orig_burst(g, ans(0.5), 5))
    assert [o["game"]["multiplier"] for o in out] == [1.1, 3, 0.3, 0.7]
    assert a.guards == 4 and len(a.presses) == 4


def test_burst_stops_everything_on_a_ball_with_money_in_it():
    a = FakeAdapter([ans(1.1), ans(3, amount=1)])
    with pytest.raises(GameGone):
        run(a._orig_burst(hb.ORIG_BY_ID["plinko"], ans(0.5), 5))
    assert a._hard_stop and len(a.presses) == 2          # the bad answer ends it before a third ball


def test_burst_cut_short_when_the_bet_button_does_not_come_back():
    a = FakeAdapter([ans(1.1), ans(3)], missing_after=1)
    out = run(a._orig_burst(hb.ORIG_BY_ID["plinko"], ans(0.5), 4))
    assert len(out) == 1 and not a._orig_stop


def test_burst_code_in_play_original_uses_the_guard_and_never_the_amount():
    src = Path(hb.__file__).read_text()
    body = src[src.index("    async def _orig_burst("):src.index("    async def _play_original(")]
    assert "await self._orig_guard()" in body and 'self._orig_press("BET", "bet"' in body
    assert "amount" not in body.replace("check_original_response", "")
    play = src[src.index("    async def _play_original("):src.index("    async def _recover(self):")]
    assert "plays.PLINKO_MAX_BALLS" in play and "burst_outcome(" in play


# ── lines ──

def _brain():
    from test_brain import make
    return make(seed=2)


def zr(mult, game="plinko", **detail):
    d = {"zero_bet": True, "won": mult > 1, "outcome": f"{game} {mult}x", **detail}
    return RoundResult(game, "r", 0.0, mult, 0.0, None, True, d)


def test_burst_round_line_names_the_burst_and_is_honest_about_the_total():
    from hubie.brain.brain import Brain
    b, _ = _brain()
    assert not Brain.is_plain(zr(0.6, balls=[0.5, 1.1, 0.2]))
    for _ in range(12):
        t = b.comment_round(zr(1.12, balls=[0.5, 1.1, 3, 0.3, 0.7]), "Plinko", short=True).text
        assert re.search(r"\b5\b", t) and "3x" in t and not re.search(r"\bdown\b|sank|no\.", t), t
    for _ in range(12):
        t = b.comment_round(zr(0.6, balls=[0.5, 1.1, 0.2]), "Plinko", short=True).text
        assert re.search(r"\b3\b", t) and "1.1x" in t and not re.search(r"\bpaid\b|\bup overall\b|ahead", t), t


def test_all_in_is_announced_with_mines_and_depth_and_the_round_ties_back():
    b, _ = _brain()
    p = plays.plan_round("mines", random.Random(1), {"allin": True, "mines": 3, "depth": 9})
    for _ in range(8):
        t = b.plan_line(p, "Mines").text
        assert "3 mines" in t and "9" in t, t
    p["said"] = True
    t = b.comment_round(zr(0.0, "mines", plan=p, plan_kept=True, picks=4), "Mines", short=True).text
    assert "9 deep" in t or "mine" in t or "depth" in t or "said no" in t, t


def test_orchestrator_always_says_all_in_first_and_a_burst_ask_reaches_plinko():
    from test_orchestrator import build
    from test_originals_play import OrigFake
    orch, speaker, clock = build(adapter=OrigFake())
    orch.games()
    orch.adapter.load_game("mines")
    seg = Segment(PLAY, "x", target=1000, game_id="mines")
    orch.segment = seg
    for _ in range(120):
        n0 = len(speaker.lines)
        orch.play_one(seg)
        plan = orch.adapter.plans[-1]
        if plan.get("allin"):
            said = [t for _, t in speaker.lines[n0:]]
            assert plan["said"] and any("deep" in t or "tiles on" in t for t in said), said
    assert any(p.get("allin") for p in orch.adapter.plans)
    assert orch._play_ask("drop 5 balls") is None or orch._play_ask("drop 5 balls")[0] == "plinko"


def test_nothing_he_says_for_the_drama_says_all_in():
    """The safety filter refuses "all in" in speech: the chat ask's reply and every drama pool line avoid it."""
    b, _ = _brain()
    for req in ({"allin": True}, {"allin": True, "depth": 9}, {"allin": True, "mines": 3}, {"balls": 5}):
        gid = "plinko" if "balls" in req else "mines"
        t = b.plan_line({"short": plays.request_text(gid, req)}, "Mines", name="Binlala").text
        assert "Binlala" in t and "all in" not in t, t
    for ev in ("plan_say_allin", "allin_paid", "allin_missed", "burst_up", "burst_even", "burst_down"):
        assert b.persona.canned[ev]
        assert not any(re.search(r"\ball[- ]?in\b", x) for x in b.persona.canned[ev]), ev
