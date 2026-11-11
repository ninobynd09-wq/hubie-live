"""Segment scheduler with anti-repeat memory. Decides what happens next so the
stream never settles into a visible loop, across restarts too."""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

from ..games.base import GameInfo

PLAY, CHAT, BIT, RECAP, SCHOOL, VOTE, RG = "play", "chat_floor", "bit", "recap", "game_school", "vote", "rg_reminder"
WEIGHTS = {PLAY: 7, CHAT: 2, BIT: 2, RECAP: 1, SCHOOL: 1, VOTE: 1}
NIGHT_WEIGHTS = {PLAY: 6, CHAT: 3, BIT: 2, RECAP: 1, SCHOOL: 1, VOTE: 1}
# The play segment's label, rotated so it never reads the same twice in a row (the panel prints "NOW <title>").
PLAY_TITLES = ["spinning slowly", "slots, at sloth speed", "a few slow spins", "reels, no rush",
               "one spin at a time", "taking the slow lane"]
NIGHT_PLAY_TITLES = ["late night spins", "slots after dark", "quiet night spins"]
# The same for a house original (dice, mines, Hubie's Road ...): no "spins" there.
ORIGINAL_TITLES = ["house games, slowly", "one slow bet at a time", "house originals, no rush",
                   "the house games", "slow bets on the house"]
NIGHT_ORIGINAL_TITLES = ["late night house games", "quiet night bets"]


def originals_share() -> float:
    """Share of play segments that go to a house original when both kinds are on offer (HUBIE_HB_ORIGINALS_SHARE,
    default 0.8); the rest are slots."""
    try:
        return min(1.0, max(0.0, float(os.environ.get("HUBIE_HB_ORIGINALS_SHARE", "0.8"))))
    except ValueError:
        return 0.8
TITLES = {PLAY: "spinning slowly", CHAT: "Open floor", BIT: "Slow thoughts", RECAP: "The accounts",
          SCHOOL: "Sloth school", VOTE: "Chat votes", RG: "A word from the desk"}


@dataclass
class Segment:
    kind: str
    title: str
    target: int = 1
    done: int = 0
    game_id: str | None = None
    topic: str | None = None
    gap: float = 0.0
    options: dict = field(default_factory=dict)
    started: float = field(default_factory=time.time)

    @property
    def finished(self) -> bool:
        return self.done >= self.target


class Scheduler:
    def __init__(self, memory, topics: list[str], night_topics: list[str] | None = None, seed: int | None = None,
                 rg_every: float = 1500.0, night_offset_hours: float = 0.0, now=time.time,
                 favorite_games: list[str] | None = None, talk_segments: dict | None = None,
                 schedule: list[dict] | None = None):
        self.mem = memory
        self.favorites = list(favorite_games or [])
        self.talk = dict(talk_segments or {})
        self.schedule = list(schedule or [])
        self.topics = topics
        self.night_topics = night_topics or []
        self.rng = random.Random(seed)
        self.title_rng = random.Random(seed)   # its own stream: titles don't shift which segments come up
        self.rg_every = rg_every
        self.night_offset = night_offset_hours
        self.now = now
        s = memory.scheduler
        s.setdefault("recent_kinds", [])
        s.setdefault("recent_games", [])
        s.setdefault("recent_topics", [])
        s.setdefault("last_rg", now())
        s.setdefault("forced_game", None)
        s.setdefault("anchors_done", {})
        s.setdefault("play_titles", [])
        self.state = s

    def is_night(self) -> bool:
        return self.local_hour() < 6

    def local_hour(self) -> int:
        return int(((self.now() + self.night_offset * 3600) % 86400) // 3600)

    def _due_anchor(self) -> dict | None:
        """A daily anchor fires once, in its hour. Survives restarts."""
        day = int((self.now() + self.night_offset * 3600) // 86400)
        hour = self.local_hour()
        for e in self.schedule:
            key = f"{e['hour']}:{e['kind']}"
            if e["hour"] == hour and self.state["anchors_done"].get(key) != day:
                return e
        return None

    def _anchor_used(self, e: dict) -> None:
        # marked only when it actually runs: an RG reminder that wins the same slot doesn't eat the day's anchor
        day = int((self.now() + self.night_offset * 3600) // 86400)
        self.state["anchors_done"][f"{e['hour']}:{e['kind']}"] = day

    def force_next_game(self, game_id: str) -> None:
        self.state["forced_game"] = game_id

    def _pick_game(self, games: list[GameInfo]) -> str:
        forced = self.state.get("forced_game")
        ids = [g.id for g in games]
        if forced in ids:
            self.state["forced_game"] = None
            return forced
        kinds = {g.kind for g in games}
        if {"original", "slot"} <= kinds:
            # originals are the main games, slots the occasional extra
            kind = "original" if self.rng.random() < originals_share() else "slot"
            ids = [g.id for g in games if g.kind == kind]
        recent = self.state["recent_games"]
        window = min(len(ids) - 1, 3)
        block = set(recent[-window:]) if window > 0 else set()
        pool = [i for i in ids if i not in block] or ids
        pool += [f for f in self.favorites if f in pool] * 2   # more often, anti-repeat still applies
        return self.rng.choice(pool)

    def _pick_topic(self, topics: list[str] | None = None) -> str:
        pool = list(topics if topics is not None else self.topics)
        if topics is None and self.is_night():
            pool += self.night_topics * 2
        recent = self.state["recent_topics"]
        window = min(len(set(pool)) - 1, 12)
        block = set(recent[-window:]) if window > 0 else set()
        choices = [t for t in pool if t not in block] or pool
        fresh = getattr(self.mem, "topic_fresh", None)
        if fresh is not None:
            # nothing said in the last 45 minutes comes back (the topic, or the same sloth fact as an aside)
            ok = [t for t in choices if fresh(t)] or [t for t in pool if fresh(t)]
            choices = ok or [max(set(pool), key=self.mem.topic_age)]
        t = self.rng.choice(choices)
        if hasattr(self.mem, "note_topic"):
            self.mem.note_topic(t)
        return t

    def play_title(self, original: bool = False) -> str:
        if original:
            pool = ORIGINAL_TITLES + (NIGHT_ORIGINAL_TITLES * 2 if self.is_night() else [])
        else:
            pool = PLAY_TITLES + (NIGHT_PLAY_TITLES * 2 if self.is_night() else [])
        recent = self.state.setdefault("play_titles", [])
        choices = [t for t in pool if t not in recent[-3:]] or pool
        t = self.title_rng.choice(choices)
        self.state["play_titles"] = (recent + [t])[-6:]
        return t

    def _talk_segment(self, kind: str, now: float) -> Segment:
        meta = self.talk.get(kind) or {}
        lo, hi = (meta.get("target") or [2, 3])[:2]
        seg = Segment(kind, meta.get("title") or TITLES.get(kind, kind), target=self.rng.randint(lo, hi),
                      gap=float(meta.get("gap") or 0), started=now)
        topics = meta.get("topics")
        if kind == BIT or topics:
            seg.topic = self._pick_topic(topics if kind != BIT else None)
            self.state["recent_topics"] = (self.state["recent_topics"] + [seg.topic])[-40:]
        return seg

    def next(self, games: list[GameInfo], force_play: bool = False) -> Segment:
        """force_play: talk has run long with no game, so the weighted pick is play (a due reminder or anchor still
        goes first)."""
        s = self.state
        now = self.now()
        anchor = self._due_anchor()
        options, title, forced = {}, None, None
        if now - s["last_rg"] >= self.rg_every:
            kind = RG
        elif anchor and (anchor["kind"] != PLAY or any(g.id == anchor.get("game") for g in games)):
            self._anchor_used(anchor)
            kind, options, title, forced = anchor["kind"], anchor.get("options") or {}, anchor.get("title"), anchor.get("game")
        else:
            night = self.is_night()
            weights = dict(NIGHT_WEIGHTS if night else WEIGHTS)
            for k, m in self.talk.items():
                w = m.get("night_weight" if night else "weight", 0)
                if w:
                    weights[k] = w
                else:
                    weights.pop(k, None)
            if not games:
                for k in (PLAY, SCHOOL, VOTE):
                    weights.pop(k, None)
            recent = s["recent_kinds"]
            if recent:
                weights.pop(recent[-1], None)
            # Hubie is here to play: two talk segments in a row is the limit.
            if games and (force_play or (len(recent) >= 2 and PLAY not in recent[-2:])):
                weights = {PLAY: 1}
            kinds = list(weights)
            kind = self.rng.choices(kinds, [weights[k] for k in kinds])[0]

        if kind in self.talk:
            seg = self._talk_segment(kind, now)
        elif kind == PLAY and not title:
            seg = Segment(kind, "", started=now)
        else:
            seg = Segment(kind, title or TITLES[kind], started=now)
        if kind == PLAY:
            seg.game_id = forced or self._pick_game(games)
            if not seg.title:
                seg.title = self.play_title(any(g.id == seg.game_id and g.kind == "original" for g in games))
            seg.target = self.rng.randint(5, 12)
            seg.options = dict(options)
            s["recent_games"] = (s["recent_games"] + [seg.game_id])[-20:]
        elif kind == SCHOOL:
            seg.game_id = self.rng.choice(games).id
        elif kind == BIT and BIT not in self.talk:
            seg.topic = self._pick_topic()
            seg.target = self.rng.randint(2, 4)
            s["recent_topics"] = (s["recent_topics"] + [seg.topic])[-40:]
        elif kind == CHAT:
            seg.target = self.rng.randint(3, 6)
        elif kind == RG:
            s["last_rg"] = now
        s["recent_kinds"] = (s["recent_kinds"] + [kind])[-20:]
        self.mem.save()
        return seg
