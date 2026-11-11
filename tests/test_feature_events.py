"""Live feature events: parsed from the game's own traffic, said over the feature at a calm pace."""
import json
import re
import threading
import time
from pathlib import Path

from hubie.brain.brain import HYPE, NO_TAILS, NON_SEQUITUR, Brain
from hubie.brain.memory import Memory
from hubie.brain.persona import Persona
from hubie.brain.provider import LLMProvider, MockProvider
from hubie.brain.scheduler import PLAY, Segment
from hubie.games import housebets as hb
from hubie.games.base import GameAdapter, GameInfo, RoundResult
from hubie.safety.output_filter import OutputFilter
from hubie.stage.desk import reaction_for

from test_orchestrator import PERSONA, build

BUY_FIX = Path(__file__).parent / "fixtures" / "pragmatic_sweet_bonanza_1000_buy.json"


def replay(exchanges):
    p = hb.PragmaticRound()
    events, rounds = [], []
    for q, a in exchanges:
        r = p.feed(q, a)
        events += p.drain_events()
        if r:
            rounds.append(r)
    return events, rounds


def loud(events):
    return [e for e in events if e["type"] != "progress"]


# ── parsing ──

def test_saved_free_spins_round_gives_live_events():
    """The real Sweet Bonanza 1000 feature (bought, 10 spins + 5 retriggered, $153.90 on $2 base bet)."""
    events, rounds = replay(json.loads(BUY_FIX.read_text())["exchanges"])
    ev = loud(events)
    assert [e["type"] for e in ev] == ["feature_triggered", "retrigger", "big_hit", "milestone", "milestone",
                                       "feature_end"]
    assert ev[0]["spins"] == 10 and ev[0]["bought"] and ev[0]["bet"] == 2.0
    assert ev[1]["added"] == 5 and ev[1]["spins"] == 15
    assert ev[2]["x"] == 33.2                        # free spin 1: 1.00 + 5.80 + 1.50 + 58.10 on a $2 bet
    assert [e["x"] for e in ev[3:5]] == [25, 50]     # 72.40 skips 10x and says 25x once; 113.40 -> 50x
    end = ev[-1]
    assert end["total"] == 153.9 and end["x"] == 77.0 and end["multiplier"] == 0.77 and end["spins"] == 15
    prog = [e for e in events if e["type"] == "progress"]
    assert prog[-1]["left"] == 0 and prog[0]["left"] == 10
    # the end result is unchanged by the live events
    assert len(rounds) == 1 and rounds[0]["payout"] == 153.9 and rounds[0]["free_spins_count"] == 15


def test_plain_rounds_say_nothing_live():
    fix = Path(__file__).parent / "fixtures" / "pragmatic_sweet_bonanza_1000.json"
    events, rounds = replay(json.loads(fix.read_text())["exchanges"])
    assert len(rounds) == 3 and events == []


# Gates of Olympus style natural trigger, written for this test (multiplier orbs as rmul=sym~pos~value).
NATURAL = [
    ("action=doInit", "balance=100,000.00&na=s&c=0.10&l=20"),
    ("action=doSpin&c=0.1&l=20", "tw=0.40&balance=99,998.00&na=s&rs_p=0&rs_c=1&c=0.10&l=20&w=0.40"),
    ("action=doSpin&c=0.1&l=20", "tw=0.40&balance=99,998.00&na=s&rs_t=1&fs=1&fsmax=15&fswin=0.00&c=0.10&l=20&w=0.00"),
    ("action=doSpin", "tw=0.40&balance=99,998.00&na=s&fs=2&fsmax=15&fswin=0.00&w=0.00"),
    ("action=doSpin", "tw=3.40&balance=99,998.00&na=s&fs=3&fsmax=15&fswin=3.00&rs_p=0&rmul=12~4~25;12~17~2&w=3.00"),
    ("action=doSpin", "tw=45.40&balance=99,998.00&na=s&fs=3&fsmax=15&fswin=45.00&rs_t=1&rmul=12~4~25;12~17~2&w=42.00"),
    ("action=doSpin", "tw=45.40&balance=99,998.00&na=s&fs=4&fsmax=20&fsmore=5&fswin=45.00&w=0.00"),
    ("action=doSpin", "tw=52.70&balance=99,998.00&na=c&fs_total=20&fswin_total=52.30&w=7.30"),
    ("action=doCollect", "balance=100,050.70&na=s"),
]


def test_natural_trigger_multiplier_retrigger_and_end():
    events, rounds = replay(NATURAL)
    ev = loud(events)
    kinds = [e["type"] for e in ev]
    assert kinds[0] == "feature_triggered" and ev[0]["spins"] == 15 and not ev[0]["bought"]
    multis = [e for e in ev if e["type"] == "multiplier"]
    assert [m["value"] for m in multis] == [25]      # the 2x is too small; the 25x orb is said once, not per tumble
    assert any(e["type"] == "big_hit" and e["x"] == 22.5 for e in ev)        # 3.00 + 42.00 on one free spin
    assert [e["x"] for e in ev if e["type"] == "milestone"] == [10, 25]      # 45.40 -> 22.7x ; 52.70 -> 26.4x
    assert any(e["type"] == "retrigger" and e["added"] == 5 for e in ev)
    assert kinds[-1] == "feature_end" and ev[-1]["x"] == 26.4 and ev[-1]["multiplier"] == 26.35
    assert rounds[0]["free_spins"] and rounds[0]["payout"] == 52.7


def test_parse_multipliers_ignores_junk():
    assert hb.parse_multipliers({"rmul": "12~4~25;12~17~2"}) == [("12~4~25", 25), ("12~17~2", 2)]
    assert hb.parse_multipliers({"rmul": "garbage"}) == []
    assert hb.parse_multipliers({}) == []


def test_broken_feature_fields_never_cost_the_round():
    p = hb.PragmaticRound()
    p.feed("action=doInit", "balance=100,000.00&na=s")
    p.feed("action=doSpin&c=0.1&l=20", "tw=0&balance=99,998.00&na=s&fs=x&fsmax=10&rmul=~~~")
    r = p.feed("action=doSpin", "tw=4.00&balance=99,998.00&na=c&fs_total=10&fswin_total=4.00")
    r = r or p.feed("action=doCollect", "balance=100,002.00&na=s")
    assert r and r["payout"] == 4.0


# ── orchestrator: pace, priority, no fillers on top ──

class EventAdapter(GameAdapter):
    """A slot whose round runs a scripted feature: events show up while play_round blocks."""
    name = "events"

    def __init__(self, events, hold=0.6):
        self.events, self.hold, self.out = list(events), hold, []
        self.lock = threading.Lock()

    def start(self): pass
    def list_games(self): return [GameInfo("gates", "Gates of Olympus 1000", "slot", "orbs.")]
    def load_game(self, game_id): pass
    def is_demo(self): return True
    def recover(self): pass

    def play_round(self, bet, options=None):
        with self.lock:
            self.out.extend(self.events)
        time.sleep(self.hold)
        return RoundResult("gates", "r1", 2.0, 26.35, 52.7, 100050.7, True,
                           {"free_spins_triggered": True, "free_spins_count": 20})

    def poll_events(self):
        with self.lock:
            out, self.out = self.out, []
        return out


def feature_orch(events):
    ad = EventAdapter(events)
    orch, sp, clock = build(adapter=ad, fill_after=10.0)
    orch.feature_llm_share = 0.0
    sent = []
    orch.notify = sent.append
    return orch, sp, clock, ad, sent


def test_trigger_line_and_surprise_clip_right_away():
    orch, sp, clock, ad, sent = feature_orch([])
    ad.out = [{"type": "feature_triggered", "spins": 15, "bought": False}]
    assert orch.feature_tick("Gates of Olympus 1000")
    kind, text = sp.lines[-1]
    assert kind == "feature" and "fifteen" in text and not re.search(r"\d", text)
    assert {"type": "react", "name": "surprise", "why": "feature_triggered"} in sent


def test_rate_limit_and_priority():
    orch, sp, clock, ad, sent = feature_orch([])
    ad.out = [{"type": "feature_triggered", "spins": 15}]
    orch.feature_tick("Gates")
    said = len(sp.lines)
    # a burst of events inside the gap: nothing more is said until the gap is over
    for dt, ev in [(1, {"type": "multiplier", "value": 25}), (2, {"type": "milestone", "x": 10, "left": 12}),
                   (3, {"type": "retrigger", "added": 5, "spins": 20}), (4, {"type": "big_hit", "x": 22})]:
        clock[0] += dt
        ad.out = [ev]
        assert not orch.feature_tick("Gates")
    assert len(sp.lines) == said
    clock[0] = orch.feature["next_ok"] - 3.0          # retrigger came last-but-one: still waiting, now ~10 s old
    assert not orch.feature_tick("Gates")
    # past the gap: one line, the retrigger (highest priority still fresh), then a fresh gap of 6-8 s
    clock[0] = orch.feature["next_ok"] + 0.1
    ad.out = [{"type": "retrigger", "added": 5, "spins": 20}]
    assert orch.feature_tick("Gates")
    assert "five" in sp.lines[-1][1]
    t_line = clock[0]
    ad.out = [{"type": "milestone", "x": 25, "left": 9}]
    clock[0] += 5.0
    assert not orch.feature_tick("Gates")
    clock[0] = t_line + 8.01
    assert orch.feature_tick("Gates")
    assert "twenty five" in sp.lines[-1][1]
    starts = [orch.feature["last_line"]]
    assert orch.counts["feature_lines"] == 3


def test_stale_events_are_dropped_and_quiet_feature_gets_a_word():
    orch, sp, clock, ad, sent = feature_orch([])
    ad.out = [{"type": "feature_triggered", "spins": 15}, {"type": "progress", "left": 15, "spins": 15}]
    orch.feature_tick("Gates")
    ad.out = [{"type": "multiplier", "value": 10}]
    orch.feature_tick("Gates")
    clock[0] += 14.5                                  # past the trigger hold, but the multiplier is 14.5 s old
    ad.out = [{"type": "progress", "left": 8, "spins": 15}]
    assert orch.feature_tick("Gates")                 # quiet for 14.5 s: a word about the bonus, not the old orb
    assert sp.lines[-1][0] == "feature" and "eight" in sp.lines[-1][1]


def test_feature_end_stops_live_lines():
    orch, sp, clock, ad, sent = feature_orch([])
    ad.out = [{"type": "feature_triggered", "spins": 10}]
    orch.feature_tick("Gates")
    ad.out = [{"type": "milestone", "x": 50, "left": 3}, {"type": "feature_end", "total": 120, "x": 60, "multiplier": 60}]
    clock[0] += 30
    assert not orch.feature_tick("Gates")
    assert not orch.feature_live()


def test_no_fillers_while_a_feature_talks_and_the_round_line_still_comes():
    events = [{"type": "feature_triggered", "spins": 15, "bought": False},
              {"type": "progress", "left": 15, "spins": 15}]
    orch, sp, clock, ad, sent = feature_orch(events)
    ad.hold = 1.2
    orch.silent_since = clock[0] - 60                 # long silence: a filler would be due at once
    orch.game_names["gates"] = "Gates of Olympus 1000"
    orch.play_one(Segment(PLAY, "spinning slowly", target=5, game_id="gates"))
    kinds = [k for k, _ in sp.lines]
    assert kinds.count("feature") == 1 and "aside" not in kinds
    assert kinds[-1] == "round"
    rnd = [e for e in sent if e.get("type") == "round"][0]
    assert rnd["feature_seen"] is True
    assert orch.feature is None


def test_end_clip_win_or_loss_never_a_second_surprise():
    fs = {"detail": {"free_spins_triggered": True}}
    assert reaction_for({**fs, "multiplier": 26.35, "feature_seen": True}, 5) == "win"
    assert reaction_for({**fs, "multiplier": 0.26, "feature_seen": True}, 5) == "loss"
    assert reaction_for({**fs, "multiplier": 2.0, "feature_seen": True}, 5) is None
    assert reaction_for({**fs, "multiplier": 2.0}, 5) == "surprise"


# ── what he says ──

def brain(provider=None):
    return Brain(Persona.load(PERSONA), provider or MockProvider(1), Memory(None), OutputFilter(), seed=3)


class Hype(LLMProvider):
    name = "hype"

    def __init__(self, line):
        self.line, self.calls = line, 0

    def complete(self, req):
        self.calls += 1
        return self.line


def test_model_feature_lines_with_hype_or_inside_jokes_go_to_the_pool():
    p = Persona.load(PERSONA)
    pool = [x.format(spins="fifteen") for x in p.canned["feature_start"]]
    for bad in ["LETS GO fifteen free spins!", "here we go, fifteen spins", "fifteen spins, the moss saw it",
                "buy it again chat, fifteen spins", "fifteen spins. you should try this"]:
        b = brain(Hype(bad))
        line = b.feature_line("feature_start", "Gates", {"event": "feature_start", "spins": "fifteen"},
                              llm_share=1.0, spins="fifteen")
        assert line.text in pool, bad
    b = brain(Hype("ok, fifteen free spins. i'm watching."))
    assert b.feature_line("feature_start", "Gates", {}, llm_share=1.0, spins="fifteen").text == \
        "ok, fifteen free spins. i'm watching."


def test_feature_pools_are_calm_and_never_about_buying_more():
    p = Persona.load(PERSONA)
    for ev in ["feature_start", "feature_retrigger", "feature_multi", "feature_hit", "feature_milestone",
               "feature_milestone_done", "feature_quiet"]:
        assert len(p.canned[ev]) >= 3, ev
        for t in p.canned[ev]:
            assert not HYPE.search(t), t
            assert not NON_SEQUITUR.search(t), t


def test_fillers_never_get_tails():
    b = brain()
    for _ in range(40):
        t = b.aside("wait_round").text
        assert t in Persona.load(PERSONA).canned["wait_round"], t
    assert "wait_round" in NO_TAILS


def test_no_inside_references_in_round_or_filler_pools():
    p = Persona.load(PERSONA)
    for ev in ["round_win", "round_loss", "round_partial", "round_big", "round_short_loss", "round_short_back",
               "round_short_win", "wait_round", "wait_loading", "aside_short", "browse_aside", "idle", "back",
               "game_switch", "chat_return"]:
        for t in p.canned[ev]:
            assert not NON_SEQUITUR.search(t), (ev, t)
    for t in p.tails:
        assert not NON_SEQUITUR.search(t), t


# ── live demo balance + quiet after the end line (2026-11-07) ──

GLITCHY = re.compile(r"lost my (thought|train)|where was i|forgot what|^anyway\.?$|that's where we are", re.IGNORECASE)


def test_filler_pools_have_no_glitchy_lines():
    p = Persona.load(PERSONA)
    for ev in ["aside_short", "wait_round", "wait_loading", "browse_aside", "idle", "stall"]:
        for t in p.canned[ev]:
            assert not GLITCHY.search(t), (ev, t)


def test_progress_carries_the_live_demo_balance_and_the_end_matches():
    events, rounds = replay(NATURAL)
    bal = [e["balance"] for e in events if e["type"] == "progress"]
    assert bal[0] == 99998.4 and bal == sorted(bal)            # 100,000 - 2.00 bet + 0.40 so far, then up
    assert bal[-1] == rounds[0]["balance"] == 100050.7         # the last live value is the paid-out balance
    events, rounds = replay(json.loads(BUY_FIX.read_text())["exchanges"])
    bal = [e["balance"] for e in events if e["type"] == "progress"]
    assert bal == sorted(bal) and bal[-1] == rounds[0]["balance"]   # a bought feature starts from balance - cost


def test_panel_balance_moves_during_the_feature_and_spins_count_once():
    events = [{"type": "feature_triggered", "spins": 15, "bought": False},
              {"type": "progress", "left": 15, "spins": 15, "balance": 99998.0},
              {"type": "progress", "left": 14, "spins": 15, "balance": 99998.0},
              {"type": "progress", "left": 12, "spins": 15, "balance": 100020.5}]
    orch, sp, clock, ad, sent = feature_orch(events)
    orch.game_names["gates"] = "Gates of Olympus 1000"
    orch.play_one(Segment(PLAY, "spinning slowly", target=5, game_id="gates"))
    live = [e["balance"] for e in sent if e.get("type") == "live_balance"]
    assert live == [99998.0, 100020.5]                           # only on a change
    kinds = [e["type"] for e in sent if e.get("type") in ("live_balance", "round")]
    assert kinds[-1] == "round" and kinds.count("round") == 1


def test_desk_panel_takes_live_balance_and_round_overwrites_it(tmp_path, monkeypatch):
    import asyncio
    from test_desk_scene import _cfg
    from hubie.stage.desk import DeskScene
    cfg = _cfg(tmp_path, monkeypatch)

    async def go():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: None
        s.apply({"type": "round", "game": "Gates", "multiplier": 0, "won": False, "balance": 1000.0, "detail": {}})
        panel0 = (cfg.scene_dir / "panel.png").read_bytes() if (cfg.scene_dir / "panel.png").exists() else None
        s.apply({"type": "live_balance", "balance": 1040.0})
        st = json.loads((cfg.scene_dir / "session.json").read_text())
        assert st["balance"] == 1040.0 and st["spins"] == 1
        if panel0 is not None:
            assert (cfg.scene_dir / "panel.png").read_bytes() != panel0
        s.apply({"type": "round", "game": "Gates", "multiplier": 26.35, "won": True, "balance": 1050.7,
                 "detail": {"free_spins_triggered": True}, "feature_seen": True})
        st = json.loads((cfg.scene_dir / "session.json").read_text())
        assert st["balance"] == 1050.7 and st["spins"] == 2      # absolute: nothing added twice

    asyncio.run(go())


def test_no_filler_right_after_the_feature_end_line():
    """2026-11-07: the end line ("yeah, the bonus buy paid off...") was followed at once by "hm. lost my thought."
    The quiet clock had started before the end line and wasn't polled while he spoke."""
    events = [{"type": "feature_triggered", "spins": 15, "bought": True},
              {"type": "progress", "left": 15, "spins": 15}]
    orch, sp, clock, ad, sent = feature_orch(events)
    orch.fill_after = 5.0
    orch.silent_since = clock[0] - 60                  # quiet clock running long before the round line
    orch.game_names["gates"] = "Gates of Olympus 1000"
    orch.play_one(Segment(PLAY, "spinning slowly", target=5, game_id="gates"))
    assert sp.lines[-1][0] == "round"
    clock[0] += 0.1
    assert not orch.fill_air()
    clock[0] += 6.0                                    # past fill_after, still inside the 8 s window
    assert not orch.fill_air()
    clock[0] += 2.5
    assert orch.fill_air()
