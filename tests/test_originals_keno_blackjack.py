"""Keno and Blackjack as house originals (2026-11-08), checked against answers recorded live on the zero wallet:
the request and answer guards (side bets, boxes, every stake at 0), the allow-listed controls, basic strategy with
chat pushes, the plans, the outcome words, and the decision lines said before each press."""
import asyncio
import copy
import json
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

from hubie.games import housebets as hb
from hubie.games import plays
from hubie.games.base import GameError, GameGone, RoundResult

sys.path.insert(0, str(Path(__file__).parent))
FIX = json.loads((Path(__file__).parent / "fixtures" / "originals_keno_blackjack.json").read_text())
USDT = {1}
KENO = hb.ORIG_BY_ID["keno"]
BJ = hb.ORIG_BY_ID["blackjack"]


def body(d):
    return json.dumps(d)


# ── registered like the other originals ──

def test_keno_and_blackjack_are_originals_in_the_rotation():
    assert (KENO.api, KENO.tag, KENO.slug) == ("keno2", "keno2-game", "housegames-keno")
    assert (BJ.api, BJ.tag, BJ.slug) == ("blackjack7", "blackjack7-game", "housegames-blackjack")
    assert {"keno", "blackjack"} <= set(hb.originals_from_env())
    assert plays.GAME_OF_API["keno2"] == "keno" and plays.GAME_OF_API["blackjack7"] == "blackjack"


# ── request guard ──

def test_recorded_bets_and_actions_pass_the_request_guard():
    k = FIX["keno_bet"]
    assert hb.check_original_request(k["url"], body(k["request"]), set(), USDT) == (True, "")
    rounds = set()
    for step in FIX["blackjack"] + FIX["blackjack_hits"]:
        req = step["request"]
        if step["url"].endswith("/bet"):
            assert hb.check_original_request(step["url"], body(req), rounds, USDT) == (True, "")
            g = step["response"]["game"]
            assert hb.check_original_response(step["response"], USDT) == ("ok", "")
            rounds.add(g["roundId"])
        else:
            rounds.add(req["roundId"])          # the open hand was seen starting at 0 on load (/game)
            assert hb.check_original_request(step["url"], body(req), rounds, USDT) == (True, ""), req


@pytest.mark.parametrize("change,why", [
    ({"perfectPairs": 1}, "perfectPairs"),
    ({"twentyOnePlusThree": "0.5"}, "twentyOnePlusThree"),
    ({"seats": 2}, "boxes"),
    ({"amount": 0.01}, "amount"),
    ({"currency": 2}, "currency"),
    ({"sideBetValue": 3}, "sideBetValue"),
])
def test_a_blackjack_bet_with_any_money_side_bet_or_extra_box_is_aborted(change, why):
    req = {**FIX["blackjack"][1]["request"], **change}
    ok, msg = hb.check_original_request("https://games.housebets.com/blackjack7/bet", body(req), set(), USDT)
    assert not ok and why in msg, msg


def test_every_blackjack_step_must_name_a_round_seen_at_zero_whatever_its_path():
    act = FIX["blackjack"][0]["request"]
    for path in ("action", "double", "split", "insurance", "hit"):
        ok, msg = hb.check_original_request(f"https://games.housebets.com/blackjack7/{path}", body(act), set(), USDT)
        assert not ok and "not one seen starting at 0" in msg, (path, msg)
    ok, _ = hb.check_original_request("https://games.housebets.com/blackjack7/action", body({**act, "amount": 1}),
                                      {act["roundId"]}, USDT)
    assert not ok


def test_keno_bet_with_an_amount_is_aborted():
    req = {**FIX["keno_bet"]["request"], "amount": "0.1"}
    assert not hb.check_original_request(FIX["keno_bet"]["url"], body(req), set(), USDT)[0]


# ── answer guard ──

def test_recorded_answers_pass_and_any_stake_anywhere_is_real():
    for step in FIX["blackjack"] + FIX["blackjack_hits"] + [FIX["keno_bet"]] + FIX["keno_more"]:
        assert hb.check_original_response(step["response"], USDT) == ("ok", ""), step["response"]["game"].get("id")
    base = FIX["blackjack"][0]["response"]                # a settled hand
    for path, v in ((("stakes", "main"), 1), (("stakes", "perfectPairs"), 0.2), (("staked",), 1), (("payout",), 3)):
        d = copy.deepcopy(base)
        tgt = d["game"]
        for k in path[:-1]:
            tgt = tgt[k]
        tgt[path[-1]] = v
        assert hb.check_original_response(d, USDT)[0] == "real", path
    d = copy.deepcopy(base)
    d["game"]["hands"][0]["stake"] = 0.5
    assert hb.check_original_response(d, USDT)[0] == "real"
    d = copy.deepcopy(base)
    d["round"]["payout"] = 1
    assert hb.check_original_response(d, USDT)[0] == "real"


def test_blackjack_answers_missing_their_stakes_or_round_or_wallet_stop():
    base = FIX["blackjack"][0]["response"]
    d = copy.deepcopy(base)
    del d["game"]["stakes"]
    assert hb.check_original_response(d, USDT)[0] == "stop"
    d = copy.deepcopy(base)
    del d["round"]
    assert hb.check_original_response(d, USDT)[0] == "stop"
    d = copy.deepcopy(base)
    d["game"]["currency"] = 5
    assert hb.check_original_response(d, USDT)[0] == "stop"
    d = copy.deepcopy(base)
    del d["game"]["staked"]
    assert hb.check_original_response(d, USDT)[0] == "stop"


# ── controls ──

def test_controls_allow_hit_stand_free_double_and_refuse_the_rest():
    hb.assert_original_clickable("BJ HIT", "Hit")
    hb.assert_original_clickable("BJ STAND", "Stand")
    hb.assert_original_clickable("BJ DOUBLE", "2x Double")
    hb.assert_original_clickable("KENO NUMBER", "7", "Number 7")
    hb.assert_original_clickable("KENO RISK", "High")
    hb.assert_original_clickable("KENO CLEAR", "Clear table")
    for label, text in (("BJ HIT", "Split"), ("BJ STAND", "Insurance"), ("BJ DOUBLE", "Split"),
                        ("KENO RISK", "Classic Pro")):
        with pytest.raises(GameError):
            hb.assert_original_clickable(label, text)
    # "double" stays forbidden everywhere else, and on the double button any other wording of it
    for label, text, aria in (("BET", "Double", ""), ("BJ DOUBLE", "Double max", ""), ("BJ DOUBLE", "Double", "Double bet"),
                              ("KENO CLEAR", "Auto pick", ""), ("BJ HIT", "Hit", "Switch wallet")):
        with pytest.raises((GameGone, GameError)):
            hb.assert_original_clickable(label, text, aria)
    for label in ("BJ SPLIT", "BJ INSURANCE", "SIDE BETS", "BOXES"):
        with pytest.raises(GameGone):
            hb.assert_original_clickable(label, "x")


def test_double_only_when_the_server_prices_it_at_zero():
    deal = next(s["response"]["game"] for s in FIX["blackjack"] if s["url"].endswith("/bet"))
    assert deal["options"]["double"] and hb.bj_double_free(deal)
    for change in ({"options": {**deal["options"], "doubleCost": 0.5}}, {"options": {**deal["options"], "quote": 1}},
                   {"staked": 0.1}, {"stakes": {**deal["stakes"], "main": 1}}, {"options": {**deal["options"], "double": False}}):
        assert not hb.bj_double_free({**deal, **change}), change


def test_keno_card_is_checked_before_any_click():
    class P:
        pass
    for nums in ([0, 5], [41], list(range(1, 12)), [3, 3], []):
        with pytest.raises(GameError):
            asyncio.new_event_loop().run_until_complete(hb.HousebetsSlotsAdapter._keno_pick(P(), {"numbers": nums}))


def test_source_blackjack_never_presses_split_insurance_or_side_bets():
    src = Path(hb.__file__).read_text()
    sec = src[src.index("    async def _bj_steps("):src.index("    async def _orig_burst(")]
    labels = set(__import__("re").findall(r'"(BJ [A-Z ]+)"', sec))
    assert labels == {"BJ HIT", "BJ STAND", "BJ DOUBLE", "BJ NO INSURANCE"}
    assert "bj_double_free(game)" in sec


# ── outcomes ──

def test_keno_outcome_and_settings_from_the_recorded_round():
    bet = FIX["keno_bet"]["response"]
    o = hb.original_outcome(KENO, bet, bet)
    assert o["outcome"] == "keno 1 hit out of 3, 1.1x, low risk" and o["multiplier"] == 1.1 and o["won"]
    assert hb.played_settings(KENO, bet) == {"risk": "Low", "picks": 3}
    assert hb.plan_matches({"risk": "Low", "picks": 3, "numbers": [7, 19, 33]}, {"risk": "Low", "picks": 3})
    assert not hb.plan_matches({"risk": "Low", "picks": 4}, {"risk": "Low", "picks": 3})


def test_blackjack_outcomes_from_recorded_hands():
    lose = FIX["blackjack"][0]["response"]
    o = hb.blackjack_outcome(lose)
    assert o["multiplier"] == 0 and o["outcome"] == "blackjack 10 vs dealer 18: loss"
    win = FIX["blackjack"][2]["response"]
    o = hb.blackjack_outcome(win)
    assert o["multiplier"] == 2 and o["won"] and o["outcome"] == "blackjack 20 and the dealer bust with 25: win"
    last = FIX["blackjack_hits"][-1]["response"]
    o = hb.blackjack_outcome(last)
    assert o["moves"] == ["hit", "hit", "hit", "stand"] and not o["doubled"]
    d = copy.deepcopy(win)
    d["game"]["hands"][0].update(natural=True, value=21)
    assert hb.blackjack_outcome(d)["multiplier"] == 2.5
    d["game"]["hands"][0].update(natural=False, value=24, outcome="lose")
    assert hb.blackjack_outcome(d)["outcome"] == "blackjack busted on 24"
    d["game"]["hands"][0].update(value=20, outcome="push")
    d["game"]["dealer"]["value"] = 20
    d["game"]["actions"] = ["double"]
    o = hb.blackjack_outcome(d)
    assert o["multiplier"] == 1 and o["outcome"].endswith("push, doubled")
    d["game"]["hands"][0]["outcome"] = "surrender"
    with pytest.raises(GameError):
        hb.blackjack_outcome(d)


# ── strategy and chat pushes ──

OPTS = {"hit": True, "stand": True, "double": True, "doubleCost": 0, "quote": 0}


@pytest.mark.parametrize("cards,value,soft,up,want", [
    (["TC", "6H"], 16, False, "TS", "hit"), (["TC", "6H"], 16, False, "6S", "stand"),
    (["TC", "2H"], 12, False, "3S", "hit"), (["TC", "2H"], 12, False, "4S", "stand"),
    (["6C", "5H"], 11, False, "6S", "double"), (["6C", "5H"], 11, False, "AS", "hit"),
    (["6C", "4H"], 10, False, "JC", "hit"), (["5C", "4H"], 9, False, "3D", "double"),
    (["AC", "7H"], 18, True, "6S", "double"), (["AC", "7H"], 18, True, "8S", "stand"),
    (["AC", "7H"], 18, True, "TS", "hit"), (["AC", "6H"], 17, True, "TS", "hit"),
    (["AC", "8H"], 19, True, "6S", "stand"), (["TC", "8H"], 18, False, "AS", "stand"),
    (["AC", "AH"], 12, True, "6S", "hit"), (["5C", "3H"], 8, False, "6S", "hit"),
])
def test_basic_strategy(cards, value, soft, up, want):
    assert plays.bj_decide({"cards": cards, "value": value, "soft": soft}, up, OPTS) == (want, False)


def test_no_double_after_two_cards_or_when_not_free_and_pushes_only_when_legal():
    three = {"cards": ["2C", "4H", "5D"], "value": 11, "soft": False}
    assert plays.bj_decide(three, "6S", OPTS)[0] == "hit"
    soft18 = {"cards": ["AC", "7H"], "value": 18, "soft": True}
    assert plays.bj_decide(soft18, "6S", {**OPTS, "doubleCost": 1})[0] == "stand"
    hand = {"cards": ["TC", "9H"], "value": 19, "soft": False}
    assert plays.bj_decide(hand, "6S", OPTS, "hit") == ("hit", True)
    assert plays.bj_decide(hand, "6S", {**OPTS, "hit": False}, "hit") == ("stand", False)
    assert plays.card_value("TS") == 10 and plays.card_value("AS") == 11 and plays.card_value("10H") == 10


@pytest.mark.parametrize("text,current,want", [
    ("hit", "blackjack", ("blackjack", {"push": "hit"})),
    ("stand pls", "blackjack", ("blackjack", {"push": "stand"})),
    ("hubie hit!", "blackjack", ("blackjack", {"push": "hit"})),
    ("blackjack stand", "mines", ("blackjack", {"push": "stand"})),
    ("keno 8 numbers", "limbo", ("keno", {"picks": 8})),
    ("pick 5", "keno", ("keno", {"picks": 5})),
    ("high risk keno", "dice", ("keno", {"risk": "High"})),
    ("keno ten numbers classic", None, ("keno", {"picks": 10, "risk": "Classic"})),
])
def test_chat_asks_for_the_new_games(text, current, want):
    assert plays.parse_request(text, current) == want


@pytest.mark.parametrize("text,current", [("great hit", "blackjack"), ("hit", "mines"), ("hit the gym later", "blackjack"),
                                          ("bet 5 on hit", "blackjack"), ("keno 10 dollars", "keno")])
def test_chatter_is_not_a_push(text, current):
    assert plays.parse_request(text, current) is None


def test_keno_plans_vary_counts_and_risks_with_distinct_numbers():
    rng = random.Random(4)
    ps = [plays.plan_round("keno", rng) for _ in range(2000)]
    assert {p["risk"] for p in ps} == {"Classic", "Low", "Medium", "High"}
    c = Counter(p["picks"] for p in ps)
    assert set(c) == set(range(1, 11)) and sum(c[n] for n in range(3, 8)) / len(ps) > 0.6
    assert all(len(set(p["numbers"])) == p["picks"] and all(1 <= n <= 40 for n in p["numbers"]) for p in ps)
    p = plays.plan_round("keno", rng, {"picks": 8, "risk": "High"})
    assert p["picks"] == 8 and p["risky"] and p["short"] == "8 numbers" and p["text"] == "high risk, 8 numbers"
    assert plays.plan_round("blackjack", rng, {"push": "hit", "name": "Lena"})["push"] == "hit"
    assert plays.request_text("blackjack", {"push": "stand"}) == "standing"


# ── what he says ──

def _brain():
    from test_brain import make
    return make(seed=3)


def test_decisions_are_said_with_hand_and_dealer_and_pushes_by_name():
    b, _ = _brain()
    t = b.decision_line({"move": "hit", "hand": 16, "soft": False, "dealer": 10}).text
    assert "16" in t and "a 10" in t and "hit" in t
    t = b.decision_line({"move": "stand", "hand": 18, "soft": True, "dealer": 11}).text
    assert "soft 18" in t and "an ace" in t
    t = b.decision_line({"move": "double", "hand": 11, "dealer": 6}).text
    assert "doubl" in t and "11" in t
    t = b.decision_line({"move": "hit", "hand": 12, "dealer": 3, "pushed_by": "Binlala"}).text
    assert "Binlala" in t and "hit" in t


def test_orchestrator_speaks_decision_events_while_the_hand_plays():
    from test_orchestrator import build
    orch, speaker, clock = build()
    evs = [{"type": "decision", "move": "hit", "hand": 16, "soft": False, "dealer": 10, "pushed_by": None}]
    orch.adapter.poll_events = lambda: evs.pop() if False else [evs.pop()] if evs else []
    n0 = len(speaker.lines)
    orch.feature_tick("Blackjack")
    said = [t for _, t in speaker.lines[n0:]]
    assert len(said) == 1 and "16" in said[0] and "hit" in said[0], said


def test_round_lines_for_keno_and_blackjack_are_games_not_slots():
    b, _ = _brain()
    b.short_llm_share = 0.0
    for game, name, m, out in (("keno", "Keno", 0.0, "keno 1 hit out of 6, 0x"),
                               ("blackjack", "Blackjack", 2.0, "blackjack 20 vs dealer 17: win")):
        for _ in range(10):
            r = RoundResult(game, "r", 0.0, m, 0.0, None, True, {"zero_bet": True, "won": m > 1, "outcome": out})
            t = b.comment_round(r, name, short=True).text
            assert not __import__("re").search(r"\b(spin|reel|tumbl)", t), t


def test_insurance_hand_recorded_live_declined_then_played_and_guarded():
    steps = FIX["blackjack_insurance"]
    deal = steps[0]["response"]
    assert deal["game"]["phase"] == "insurance" and deal["game"]["insurance"]["offered"]
    assert "insurance" in hb.BJ_OPEN_PHASES
    rounds = {deal["game"]["roundId"]}
    for s in steps:
        assert hb.check_original_response(s["response"], USDT) == ("ok", "")
        if s["url"].endswith("/action"):
            assert hb.check_original_request(s["url"], body(s["request"]), rounds, USDT) == (True, "")
    assert steps[1]["request"]["action"] == "noInsurance"
    assert not steps[-1]["response"]["game"]["insurance"]["taken"]
    hb.assert_original_clickable("BJ NO INSURANCE", "No thanks")
    with pytest.raises((GameError, GameGone)):
        hb.assert_original_clickable("BJ NO INSURANCE", "Insure 0.00")
    d = copy.deepcopy(steps[-1]["response"])
    d["game"]["insurance"].update(taken=True, cost=0.5)
    assert hb.check_original_response(d, USDT)[0] == "real"
