#!/usr/bin/env python3
"""Estimates a live hour of the show: voice characters, spins per minute and model calls, with every round
commented (short lines for plain rounds). Runs the real orchestrator, brain, scheduler and persona on a
simulated clock. Nothing is called: lines come from saved Haiku replies (evals/results/haiku_pass4.jsonl)
and the free mock writer, rounds from a slot-like random table.

    .venv/bin/python tools/pacing_estimate.py [--hours 6] [--chat-per-min 1] [--spin 7.5]

Timing model (measured on the preview, 2026-11-07): a slot spin is the press, the reels and tumbles
(--spin seconds plus ~1 s per tumble, free spins ~40 s) and the 2.5 s the adapter waits for the win
animation; Haiku answers in ~1 s, Opus in ~2.5 s, ElevenLabs in ~0.8 s; the Rick voice speaks ~11.6
characters a second (8.48 s for a 98-character line).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hubie.brain.brain import Brain                           # noqa: E402
from hubie.brain.memory import Memory                         # noqa: E402
from hubie.brain.persona import Persona                       # noqa: E402
from hubie.brain.provider import LLMProvider, MockProvider, ReplayProvider  # noqa: E402
from hubie.brain.scheduler import PLAY, Scheduler             # noqa: E402
from hubie.chat.fake import FakeChatSource                    # noqa: E402
from hubie.games.base import GameAdapter, GameInfo, RoundResult  # noqa: E402
from hubie.orchestrator import Orchestrator                   # noqa: E402
from hubie.safety.moderation import ChatModerator             # noqa: E402
from hubie.safety.output_filter import SafeLine               # noqa: E402

CPS = 11.6
PAD = 0.3          # lead-in and tail silence ElevenLabs puts around a clip
SLOTS = [GameInfo("sweet_bonanza_1000", "Sweet Bonanza 1000", "slot", "Candy slot, 8 or more anywhere pay, tumbles."),
         GameInfo("gates_of_olympus_1000", "Gates of Olympus 1000", "slot", "Zeus slot, tumbles, multiplier orbs."),
         GameInfo("sugar_rush_1000", "Sugar Rush 1000", "slot", "Cluster slot, multiplier spots."),
         GameInfo("fruit_party", "Fruit Party", "slot", "Cluster slot, random multipliers on wins.")]


def slot_outcome(r: random.Random) -> tuple[float, int, bool]:
    """Rough Pragmatic-style table: ~72% blank, ~18% small (under 2x), ~8% 2-5x, ~2% 5x+, ~1 in 300 free spins."""
    if r.random() < 1 / 300:
        return round(r.choice([0.3, 0.8, 4, 12, 30, 60]), 2), r.randint(2, 6), True
    x = r.random()
    if x < 0.72:
        return 0.0, 0, False
    if x < 0.90:
        return round(r.uniform(0.1, 1.95), 2), r.randint(1, 2), False
    if x < 0.98:
        return round(r.uniform(2, 5), 2), r.randint(2, 4), False
    return round(r.uniform(5, 40), 2), r.randint(3, 6), False


class SimSlots(GameAdapter):
    name = "sim"

    def __init__(self, clock, spin: float, seed: int):
        self.clock, self.spin, self.rng, self.cur, self.n = clock, spin, random.Random(seed), SLOTS[0].id, 0

    def start(self): pass
    def list_games(self): return list(SLOTS)
    def load_game(self, game_id): self.cur = game_id; self.clock[0] += 8.0
    def is_demo(self): return True
    def recover(self): pass

    def play_round(self, bet, options=None):
        mult, tumbles, fs = slot_outcome(self.rng)
        self.clock[0] += self.spin + tumbles * 1.0 + (40.0 if fs else 0.0) + 2.5
        self.n += 1
        return RoundResult(self.cur, f"sim-{self.n}", bet, mult, round(bet * mult, 2), 1000.0, True,
                           {"slot": self.cur, "tumbles": tumbles, "free_spins_triggered": fs})


class SimProvider(LLMProvider):
    """Saved Haiku replies for full lines, the mock writer for short ones; each call costs simulated time."""
    name = "sim"

    def __init__(self, clock, seed: int):
        self.clock = clock
        self.replay = ReplayProvider(str(ROOT / "evals/results/haiku_pass4.jsonl"), seed=seed)
        self.mock = MockProvider(seed)
        self.calls: dict[str, int] = {}

    def complete(self, req):
        key = "round_short" if req.facts.get("short") else req.kind
        self.calls[key] = self.calls.get(key, 0) + 1
        self.clock[0] += 2.5 if req.kind == "chat" else 1.0
        return (self.mock if req.facts.get("short") else self.replay).complete(req)


class SimSpeaker:
    def __init__(self, clock):
        self.clock, self.busy_until = clock, 0.0
        self.chars: dict[str, int] = {}
        self.lines: dict[str, int] = {}
        self.words_short: list[int] = []

    def say(self, line, kind="line", mood="idle", channel="voice", reply_to=None, droppable=False):
        assert isinstance(line, SafeLine)
        self.clock[0] += 0.8                              # voice synthesis before the clip is queued
        k = "round_short" if (kind == "round" and droppable) else kind
        self.chars[k] = self.chars.get(k, 0) + len(line.text)
        self.lines[k] = self.lines.get(k, 0) + 1
        if k == "round_short":
            self.words_short.append(len(line.text.split()))
        self.busy_until = self.clock[0] + PAD + len(line.text) / CPS
        return True

    def pending(self):
        return 1 if self.clock[0] < self.busy_until else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--chat-per-min", type=float, default=1.0)
    ap.add_argument("--spin", type=float, default=4.0, help="press + reels before tumbles, seconds")
    ap.add_argument("--talk-pace", type=float, default=0.33)
    ap.add_argument("--short-llm-share", type=float, default=0.35)
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    clock = [1_800_000_000.0]
    tick = lambda: clock[0]
    mem = Memory(now=tick)
    persona = Persona.load(ROOT / "persona/hubie.draft.yaml")
    prov = SimProvider(clock, a.seed)
    brain = Brain(persona, prov, mem, seed=a.seed, short_llm_share=a.short_llm_share)
    sp = SimSpeaker(clock)
    orch = Orchestrator(SimSlots(clock, a.spin, a.seed), FakeChatSource(per_minute=a.chat_per_min, seed=a.seed, now=tick),
                        brain, sp, Scheduler(mem, persona.topics, persona.night_topics, seed=a.seed, now=tick,
                                             favorite_games=[], talk_segments=persona.segments, schedule=[]),
                        ChatModerator(now=tick), now=tick, sleep=lambda s: None, seed=a.seed, talk_pace=a.talk_pace)
    start, end = clock[0], clock[0] + a.hours * 3600
    play_time = 0.0
    while clock[0] < end:
        t0 = clock[0]
        in_play = orch.segment is not None and orch.segment.kind == PLAY
        orch.step()
        clock[0] += 0.25
        if in_play:
            play_time += clock[0] - t0
    hours = (clock[0] - start) / 3600
    total = sum(sp.chars.values())
    rounds = orch.counts["rounds"]
    out = {
        "hours": round(hours, 2),
        "chars_per_hour": round(total / hours),
        "chars_per_hour_by_kind": {k: round(v / hours) for k, v in sorted(sp.chars.items())},
        "chars_6h": round(total / hours * 6),
        "rounds_per_hour": round(rounds / hours),
        "share_of_time_playing": round(play_time / (clock[0] - start), 2),
        "spins_per_min_while_playing": round(rounds / max(1.0, play_time / 60), 1),
        "spins_per_min_overall": round(rounds / (hours * 60), 1),
        "plain_rounds": orch.counts.get("short_rounds", 0), "notable_rounds": orch.counts.get("full_rounds", 0),
        "avg_short_words": round(sum(sp.words_short) / max(1, len(sp.words_short)), 1),
        "llm_calls_per_hour": {k: round(v / hours) for k, v in sorted(prov.calls.items())},
        "lines_per_hour": {k: round(v / hours) for k, v in sorted(sp.lines.items())},
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
