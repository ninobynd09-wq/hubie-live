"""How Hubie plays the house originals (2026-11-08 live check): varied plans, chat asks, honest win/loss lines,
games-not-slots wording, no talk repeats inside 45 minutes, and the zero-bet guards on the new settings."""
import random
import re
from collections import Counter
from pathlib import Path

import pytest

from hubie.brain.memory import Memory
from hubie.brain.scheduler import PLAY, Scheduler, Segment
from hubie.games import housebets as hb
from hubie.games import plays
from hubie.games.base import GameError, GameGone, GameInfo, RoundResult


# ── plans ──

def test_mines_plans_mostly_three_to_five_sometimes_ten_or_twenty_deeper_with_fewer():
    rng = random.Random(3)
    ps = [p for p in (plays.plan_round("mines", rng) for _ in range(3600)) if not p.get("allin")]   # all in: drama
    c = Counter(p["mines"] for p in ps)
    assert set(c) == {1, 3, 5, 10, 20}
    assert (c[3] + c[5]) / len(ps) > 0.55 and c[10] + c[20] > 0.15 * len(ps)
    assert all(1 <= p["depth"] <= 6 for p in ps)
    deep = lambda n: sum(p["depth"] for p in ps if p["mines"] == n) / c[n]
    assert deep(1) > deep(3) > deep(10) > deep(20)


def test_road_levels_mostly_easy_medium_rarely_daredevil_runs_of_one_to_six():
    rng = random.Random(4)
    ps = [plays.plan_round("hubies_road", rng) for _ in range(3000)]
    c = Counter(p["level"] for p in ps)
    assert c["Easy"] + c["Medium"] > 0.65 * len(ps) and c["Hard"] > c["Daredevil"] > 0.04 * len(ps)
    assert {p["steps"] for p in ps} == {1, 2, 3, 4, 5, 6}


def test_plinko_limbo_dice_vary_with_occasional_long_shots():
    rng = random.Random(5)
    pl = [plays.plan_round("plinko", rng) for _ in range(1000)]
    assert {p["risk"] for p in pl} == {"Low", "Medium", "High"} and {p["rows"] for p in pl} == set(range(8, 17))
    li = Counter(plays.plan_round("limbo", rng)["target"] for _ in range(3000))
    assert set(li) == {1.5, 2, 5, 10, 25, 50, 100} and 0.03 < (li[25] + li[50] + li[100]) / 3000 < 0.2
    di = [plays.plan_round("dice", rng)["roll_over"] for _ in range(2000)]
    assert 0.08 < sum(x >= 90 for x in di) / 2000 < 0.3          # 5-10% win chance now and then


@pytest.mark.parametrize("text,current,want", [
    ("go 10 mines", "limbo", ("mines", {"mines": 10})),
    ("ten mines pls", None, ("mines", {"mines": 10})),
    ("DAREDEVIL", "mines", ("hubies_road", {"level": "Daredevil"})),
    ("hard", "hubies_road", ("hubies_road", {"level": "Hard"})),
    ("go deeper", "mines", ("mines", {"deeper": True})),
    ("100x", "limbo", ("limbo", {"target": 100.0})),
    ("limbo 50x", "dice", ("limbo", {"target": 50.0})),
    ("high risk 16 rows", "plinko", ("plinko", {"risk": "High", "rows": 16})),
    ("roll over 95", "dice", ("dice", {"roll_over": 95.0})),
])
def test_chat_asks_are_read(text, current, want):
    assert plays.parse_request(text, current) == want


@pytest.mark.parametrize("text", ["bet 10 dollars on 10 mines", "max bet daredevil", "go all in 100x", "hi hubie",
                                  "hard day today", "deposit and go 10 mines", "$5 on dice", "go deeper",
                                  "real money 100x", "25 mines"])
def test_money_asks_and_chatter_are_not_play_asks(text):
    assert plays.parse_request(text, "limbo") is None


def test_ask_beats_the_dice_and_says_what_it_is():
    p = plays.plan_round("mines", random.Random(1), {"mines": 10, "name": "Binlala"})
    assert p["mines"] == 10 and p["short"] == "10 mines" and p["asked_by"] == "Binlala"
    p = plays.plan_round("hubies_road", random.Random(1), {"level": "Daredevil"})
    assert p["level"] == "Daredevil" and p["risky"] and plays.plan_short(p) == "daredevil"
    assert plays.request_text("limbo", {"target": 100.0}) == "100x"


# ── zero-bet guards on the new settings ──

def test_settings_only_take_setting_values():
    hb.assert_setting_ok("MINES COUNT", "10")
    hb.assert_setting_ok("PLINKO ROWS", "16")
    hb.assert_setting_ok("LIMBO TARGET INPUT", "25")
    hb.assert_setting_ok("ROLL OVER", "95.00")
    for label, v in (("MINES COUNT", "0"), ("MINES COUNT", "25"), ("PLINKO ROWS", "17"), ("LIMBO TARGET INPUT", "1"),
                     ("LIMBO TARGET INPUT", "5000"), ("ROLL OVER", "99.5"), ("BET AMOUNT", "0"),
                     ("LIMBO TARGET INPUT", "1e9"), ("MINES COUNT", "3; 1")):
        with pytest.raises(GameGone):
            hb.assert_setting_ok(label, v)


def test_new_controls_are_allow_listed_and_still_refuse_money_buttons():
    hb.assert_original_clickable("ROAD LEVEL", "Daredevil", "Daredevil")
    hb.assert_original_clickable("ROAD LEVEL", "Hard", "Hard")
    hb.assert_original_clickable("LIMBO TARGET", "100x")
    hb.assert_original_clickable("MINES COUNT", "", "Number of mines")
    with pytest.raises(GameGone):
        hb.assert_original_clickable("LIMBO TARGET", "max")
    with pytest.raises(GameError):
        hb.assert_original_clickable("LIMBO TARGET", "5×5", "5 by 5 grid, 25 tiles")   # the mines grid buttons
    with pytest.raises(GameGone):
        hb.assert_original_clickable("PLINKO ROWS", "", "Switch wallet")


def test_source_settings_go_through_the_allow_list_and_never_touch_the_amount():
    src = Path(hb.__file__).read_text()
    sec = src[src.index("    # ── house originals, zero bet ──"):src.index("    async def _recover(self):")]
    labels = set(re.findall(r"_orig_(?:click|press|type|pick_text|click_el|find|wait_find|set)\(\s*\"([^\"]+)\"", sec))
    assert {"MINES COUNT", "PLINKO ROWS", "LIMBO TARGET INPUT", "ROLL OVER"} <= labels <= set(hb.ORIGINAL_CLICKS)
    # typing and setting check the value first; the bet field is only ever cleared by the guard
    for fn in ("_orig_type", "_orig_set"):
        body = sec[sec.index(f"    async def {fn}("):]
        body = body[:body.index("\n    async def ", 10)]
        assert "assert_setting_ok(label, value)" in body
    assert not re.search(r"_orig_(?:type|set)\(\s*\"BET AMOUNT\"", sec)


def test_outcome_class_and_plan_check():
    assert [hb.outcome_class(m) for m in (1.85, 1.0, 0.99, 0.0, 1.0000001)] == ["win", "even", "loss", "loss", "even"]
    plan = {"mines": 10, "depth": 2}
    assert hb.plan_matches(plan, {"mines": 10}) and not hb.plan_matches(plan, {"mines": 3})
    assert hb.plan_matches({"level": "Hard"}, {"level": "Hard"}) and not hb.plan_matches({"level": "Hard"}, {"level": None})
    assert hb.plan_matches({"target": 100.0}, {"target": 100.0})
    road = hb.ORIG_BY_ID["hubies_road"]
    assert hb.played_settings(road, {"game": {"difficulty": "daredevil"}}) == {"level": "Daredevil"}


# ── honest round lines ──

def _brain():
    from test_brain import make
    return make(seed=2)


def zr(mult, game="plinko", **detail):
    d = {"zero_bet": True, "won": mult > 1, "outcome": f"{game} {mult}x", **detail}
    return RoundResult(game, "r", 0.0, mult, 0.0, None, True, d)


def test_free_round_buckets_go_by_multiplier():
    from hubie.brain.brain import Brain
    assert [Brain.bucket(zr(m)) for m in (1.85, 1.0, 0.4, 0.0)] == ["win", "even", "back", "loss"]


def test_plinko_win_never_gets_a_loss_line():
    b, _ = _brain()
    b.short_llm_share = 0.0
    p = b.persona.canned
    for _ in range(25):
        t = b.comment_round(zr(1.85), "Plinko", short=True).text
        assert t in {x.format(won="a little one", back="some of it") for x in p["round_short_win"]}, t
    for _ in range(10):
        t = b.comment_round(zr(1.0), "Plinko", short=True).text
        assert t in p["round_short_even"], t


def test_a_model_line_that_contradicts_the_result_is_dropped():
    from hubie.brain.brain import Brain
    assert not Brain.verdict_ok("that one went quiet.", "win")
    assert not Brain.verdict_ok("nice, that paid.", "loss")
    assert Brain.verdict_ok("ok, that paid a bit.", "win") and Brain.verdict_ok("nope, not that one.", "loss")

    class Quiet:
        name = "q"

        def complete(self, req):
            return "that one went quiet."
    b, _ = _brain()
    b.provider = Quiet()
    b.short_llm_share = 1.0
    t = b.comment_round(zr(1.85), "Plinko", short=True).text
    assert "quiet" not in t


def test_original_rounds_skip_slot_only_pool_lines():
    b, _ = _brain()
    b.short_llm_share = 0.0
    for _ in range(40):
        t = b.comment_round(zr(0.0, "dice"), "Dice", short=True).text
        assert not re.search(r"spin|reel|tumbl", t), t


def test_announced_plan_is_referenced_after_the_round():
    b, _ = _brain()
    plan = {"mines": 10, "depth": 3, "said": True, "short": "10 mines", "text": "10 mines, 3 tiles"}
    t = b.comment_round(zr(0.0, "mines", plan=plan, plan_kept=True, picks=2), "Mines", short=True).text
    assert "10 mines" in t or "mines said no" in t.lower() or "said no" in t, t
    t = b.comment_round(zr(1.9, "mines", plan=plan, plan_kept=True), "Mines", short=True).text
    assert "10 mines" in t, t
    # not as planned (a setting didn't take): no reference to a plan that wasn't played
    t = b.comment_round(zr(1.2, "mines", plan=plan, plan_kept=False), "Mines", short=True).text
    assert "10 mines" not in t


def test_plan_lines():
    b, _ = _brain()
    plan = plays.plan_round("mines", random.Random(1), {"mines": 20})
    assert plan["risky"]
    t = b.plan_line(plan, "Mines").text
    assert plan["text"] in t
    t = b.plan_line({"short": "10 mines"}, "Mines", name="Binlala").text
    assert "Binlala" in t and "10 mines" in t


# ── the orchestrator: asks by name, plans about one round in three ──

class OrigFake:
    """A fake adapter with house originals: records the plan each round was given."""

    def __new__(cls):
        from hubie.games.fake import FakeGameAdapter

        class A(FakeGameAdapter):
            plans = []

            def list_games(self):
                return [GameInfo("mines", "Mines", "original", "grid"), GameInfo("limbo", "Limbo", "original", "x"),
                        GameInfo("hubies_road", "Hubie's Road", "original", "road")]

            def load_game(self, game_id):
                self.current = game_id

            def play_round(self, bet, options=None):
                plan = (options or {}).get("plan") or {}
                self.plans.append(plan)
                m = random.Random(len(self.plans)).choice([0.0, 1.0, 1.4, 3.0])
                return RoundResult(self.current, f"r{len(self.plans)}", 0.0, m, 0.0, None, True,
                                   {"zero_bet": True, "won": m > 1, "outcome": "x", "plan": plan, "plan_kept": True})
        return A(seed=1, round_seconds=(0, 0))


def test_chat_ask_is_answered_by_name_and_the_next_round_follows_it():
    from test_orchestrator import build
    from hubie.chat.base import ChatMessage
    orch, speaker, clock = build(adapter=OrigFake())
    orch.games()
    orch.adapter.load_game("limbo")
    seg = Segment(PLAY, "x", target=20, game_id="limbo")
    orch.segment = seg
    orch.chat.poll = lambda: [ChatMessage("Binlala", "go 10 mines", clock[0])]
    orch.ingest_chat()
    orch.chat.poll = lambda: []
    assert orch.answer_next()
    assert any("Binlala" in t and "10 mines" in t for _, t in speaker.lines), speaker.lines
    orch.play_one(seg)                                  # still limbo: the ask waits for mines
    assert orch.adapter.plans[-1]["game"] == "limbo"
    orch.adapter.load_game("mines")
    seg2 = Segment(PLAY, "x", target=20, game_id="mines")
    orch.segment = seg2
    orch.play_one(seg2)
    assert orch.adapter.plans[-1]["mines"] == 10 and orch.adapter.plans[-1]["asked_by"] == "Binlala"
    orch.play_one(seg2)
    assert "asked_by" not in orch.adapter.plans[-1]      # one round only


def test_plans_are_said_about_one_round_in_three_and_every_round_gets_a_line():
    from test_orchestrator import build
    orch, speaker, clock = build(adapter=OrigFake())
    orch.games()
    orch.adapter.load_game("mines")
    seg = Segment(PLAY, "x", target=1000, game_id="mines")
    orch.segment = seg
    for _ in range(300):
        orch.play_one(seg)
    said = sum(1 for p in orch.adapter.plans if p.get("said") and not p.get("allin"))
    assert 0.2 < said / 300 < 0.45
    assert all(p.get("said") for p in orch.adapter.plans if p.get("allin"))     # all in is always said first
    assert sum(1 for k, _ in speaker.lines if k == "round") == 300


def test_switch_line_says_games_not_slots_for_an_original():
    from test_orchestrator import build
    orch, speaker, clock = build(adapter=OrigFake())
    orch.adapter.browse = True
    for _ in range(30):
        orch.segment = None
        orch.games()
        orch.sched.force_next_game("mines")
        orch.start_segment()
    assert not any("slot" in t for _, t in speaker.lines), [t for _, t in speaker.lines if "slot" in t]


# ── no talk repeats inside 45 minutes ──

def test_same_talk_line_with_a_tail_is_a_repeat_for_45_minutes():
    clock = [1_000_000.0]
    mem = Memory(now=lambda: clock[0])
    mem.remember_line("unreasonable hour and i'm dressed for a wedding. still all demo.")
    mem.remember_talk("unreasonable hour and i'm dressed for a wedding. still all demo.")
    for i in range(80):
        mem.remember_line(f"filler round line number {i} about tiles")
        clock[0] += 6
    assert mem.talk_repeat("unreasonable hour and i'm dressed for a wedding. carry on.")
    clock[0] += 2700
    assert not mem.talk_repeat("unreasonable hour and i'm dressed for a wedding. carry on.")


def test_topics_and_sloth_facts_wait_45_minutes():
    clock = [1_000_000.0]
    mem = Memory(now=lambda: clock[0])
    topics = ["algae", "metabolism", "swimming", "once a week", "digest"]
    s = Scheduler(mem, ["a", "b"], seed=1, now=lambda: clock[0],
                  talk_segments={"canopy_math": {"weight": 1, "topics": topics}})
    seen = []
    for _ in range(40):
        seen.append((clock[0], s._talk_segment("canopy_math", clock[0]).topic))
        clock[0] += 600
    for i, (t, topic) in enumerate(seen):
        for t2, topic2 in seen[i + 1:]:
            if t2 - t < 2700:
                assert topic2 != topic
    b, _ = _brain()
    b.mem.note_topic(b.persona.segments["canopy_math"]["topics"][3])
    for _ in range(30):
        b.seg_angles.clear()
        _, fact = b._segment_angle("bit")
        assert fact != b.persona.segments["canopy_math"]["topics"][3]
