"""Anti-repeat and viewer memory. Survives brain restarts."""
from __future__ import annotations

import re
import time
from collections import deque
from pathlib import Path

from ..util import atomic_write_json, read_json
from .similar import STOP as _SIM_STOP, talk_repeat as _talk_repeat, tokens as _sim_tokens, too_similar

SAID_TTL = 86400.0
CRISIS_WINDOW = 1800.0   # how long after a worrying message he stays out of the bit with that viewer
ROUND_WINDOW = 15        # round lines kept for the prompt and the repeat check
TALK_WINDOW = 15         # talk-segment lines and asides are checked against this many recent lines of any kind
# ...and against every talk line (segments, bits, asides, idle lines) of the last 45 minutes for a plain repeat: the
# same idle line with a tail on it came back after 8 minutes, and the algae fact twice in ten (2026-11-08).
TALK_REPEAT_S = 2700.0
TOPIC_REPEAT_S = 2700.0  # a talk topic or sloth fact isn't picked again inside this
COMMON_FIRST = {"i", "the", "a", "that", "this", "it", "and", "so", "no"}
WORD = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _shingles(tokens: list[str], n: int = 2) -> set[tuple]:
    return {tuple(tokens[i:i + n]) for i in range(max(0, len(tokens) - n + 1))} or {tuple(tokens)}


class Memory:
    def __init__(self, path: Path | None = None, line_window: int = 300, opener_window: int = 8,
                 max_viewers: int = 5000, now=time.time):
        self.path = Path(path) if path else None
        self.tics: dict[str, int] = {}
        self.now = now
        self.lines: deque[str] = deque(maxlen=line_window)
        self.openers: deque[str] = deque(maxlen=opener_window)
        self.canned_used: dict[str, list[str]] = {}
        self.viewers: dict[str, dict] = {}
        self.recent_chat: deque[list] = deque(maxlen=60)
        self.name_opens: deque[bool] = deque(maxlen=3)
        self.max_viewers = max_viewers
        self.scheduler: dict = {}
        self.round_lines: deque[list] = deque(maxlen=ROUND_WINDOW)   # [game, line]
        self.angles: deque[str] = deque(maxlen=4)
        self.best_round: list = []                                    # [game, mult]
        self.talk_log: deque[list] = deque(maxlen=500)                # [ts, line] of talk lines (segments, asides)
        self.topic_log: dict[str, float] = {}                         # talk topic / sloth fact -> last used at
        self.stats = {"rounds": 0, "best_mult": 0.0, "lines": 0, "replies": 0, "filtered": 0}
        self._dirty = 0
        self.load()

    def load(self) -> None:
        if not self.path:
            return
        d = read_json(self.path, {}) or {}
        self.lines.extend(d.get("lines", []))
        self.openers.extend(d.get("openers", []))
        self.canned_used = d.get("canned_used", {})
        self.viewers = d.get("viewers", {})
        self.recent_chat.extend(d.get("recent_chat", []))
        self.name_opens.extend(d.get("name_opens", []))
        self.scheduler = d.get("scheduler", {})
        self.round_lines.extend(d.get("round_lines", []))
        self.angles.extend(d.get("angles", []))
        self.best_round = d.get("best_round", [])
        self.talk_log.extend(x for x in d.get("talk_log", []) if isinstance(x, list) and len(x) == 2)
        self.topic_log = {k: float(v) for k, v in (d.get("topic_log") or {}).items()}
        self.stats.update(d.get("stats", {}))

    def save(self, force: bool = False) -> None:
        self._dirty += 1
        if not self.path or (not force and self._dirty < 5):
            return
        self._dirty = 0
        atomic_write_json(self.path, {
            "lines": list(self.lines), "openers": list(self.openers),
            "canned_used": self.canned_used, "viewers": self.viewers, "recent_chat": list(self.recent_chat), "name_opens": list(self.name_opens),
            "scheduler": self.scheduler, "stats": self.stats,
            "round_lines": list(self.round_lines), "angles": list(self.angles), "best_round": self.best_round,
            "talk_log": list(self.talk_log), "topic_log": self.topic_log})

    @staticmethod
    def opener(text: str) -> str:
        return " ".join(_tokens(text)[:2])

    def is_repeat(self, text: str, threshold: float = 0.55) -> bool:
        toks = _tokens(text)
        if not toks:
            return True
        if self.opener(text) in self.openers:
            return True
        # Same first word as either of the last two lines reads as a pattern, small words aside.
        if toks[0] not in COMMON_FIRST and any(_tokens(x)[:1] == toks[:1] for x in list(self.lines)[-2:]):
            return True
        if self.tic_repeat(text):
            return True
        cur = _shingles(toks)
        for old in self.lines:
            prev = _shingles(_tokens(old))
            if len(cur & prev) / max(1, len(cur | prev)) >= threshold:
                return True
        return False

    def tic_repeat(self, text: str) -> bool:
        """A rationed tic or catchphrase he already used inside its window."""
        low = " ".join(_tokens(text))
        recent = list(self.lines)
        return any(tic in low and any(tic in " ".join(_tokens(x)) for x in recent[-every:])
                   for tic, every in self.tics.items())

    def remember_line(self, text: str) -> None:
        self.lines.append(text)
        self.openers.append(self.opener(text))
        self.stats["lines"] += 1
        self.save()

    def remember_round(self, text: str, game: str, mult: float, angle: str | None = None) -> None:
        self.round_lines.append([game, text])
        if angle:
            self.angles.append(angle)
        if mult >= (self.best_round[1] if self.best_round else 0.0):
            self.best_round = [game, mult]

    def round_repeat(self, text: str, game: str | None = None) -> str | None:
        """The recent round line this one is too close to, or None. The game's own name doesn't count."""
        ignore = set(_sim_tokens(game or ""))
        recent = [x[1] for x in self.round_lines]
        hit = too_similar(text, recent, ignore)
        if hit:
            return hit
        # A short run of real words ("slow loss", "desk's still mine") already used twice in the last ten
        # round lines has become a habit.
        toks = _sim_tokens(text, ignore)
        grams = {" ".join(toks[i:i + 2]) for i in range(len(toks) - 1) if not set(toks[i:i + 2]) & _SIM_STOP}
        grams |= {" ".join(toks[i:i + 3]) for i in range(len(toks) - 2) if len(set(toks[i:i + 3]) - _SIM_STOP) >= 2}
        for g in grams:
            olds = [x for x in recent[-10:] if f" {g} " in f" {' '.join(_sim_tokens(x, ignore))} "]
            if len(olds) >= 2:
                return olds[-1]
        return None

    def talk_repeat(self, text: str, window: int = TALK_WINDOW) -> str | None:
        """For talk-segment lines and asides: the recent line (any kind) this one paraphrases, or None.
        Stricter than is_repeat: a shared key phrase ("slow stuff roll by") or the same few words count. Older talk
        lines, back TALK_REPEAT_S, count when this is the same line again (a tail added on doesn't make it new)."""
        hit = _talk_repeat(text, list(self.lines)[-window:])
        if hit:
            return hit
        return too_similar(text, self.recent_talk(), threshold=0.5)

    def remember_talk(self, text: str) -> None:
        self.talk_log.append([self.now(), text])

    def recent_talk(self, window_s: float = TALK_REPEAT_S) -> list[str]:
        t = self.now()
        return [x[1] for x in self.talk_log if t - x[0] < window_s]

    def note_topic(self, topic: str) -> None:
        t = self.now()
        self.topic_log = {k: v for k, v in self.topic_log.items() if t - v < 86400}
        self.topic_log[topic] = t

    def topic_fresh(self, topic: str, window_s: float = TOPIC_REPEAT_S) -> bool:
        return self.now() - self.topic_log.get(topic, -1e12) >= window_s

    def topic_age(self, topic: str) -> float:
        return self.now() - self.topic_log.get(topic, -1e12)

    def pick_canned(self, event: str, options: list[str], rng) -> list[str]:
        """Options ordered by least recently used, shuffled within equals."""
        used = self.canned_used.get(event, [])
        fresh = [o for o in options if o not in used]
        rng.shuffle(fresh)
        stale = [o for o in used if o in options]
        return fresh + stale

    def mark_canned(self, event: str, template: str) -> None:
        used = self.canned_used.setdefault(event, [])
        if template in used:
            used.remove(template)
        used.append(template)

    def viewer(self, username: str) -> dict:
        key = username.lower()
        v = self.viewers.get(key)
        if not v:
            if len(self.viewers) >= self.max_viewers:
                oldest = min(self.viewers, key=lambda k: self.viewers[k]["last"])
                self.viewers.pop(oldest)
            v = {"first": self.now(), "last": self.now(), "seen": 0, "replied": 0}
            self.viewers[key] = v
        return v

    def note_said(self, username: str, name: str, text: str) -> None:
        """Only moderated public chat is remembered, and only for a day."""
        v = self.viewer(username)
        said = [x for x in v.get("said", []) if self.now() - x[0] < SAID_TTL]
        v["said"] = (said + [[self.now(), text]])[-5:]
        self.recent_chat.append([self.now(), name, text])

    def earlier_said(self, username: str) -> list[str]:
        """What this viewer said earlier, oldest first, excluding their latest message."""
        v = self.viewer(username)
        return [x[1] for x in v.get("said", []) if self.now() - x[0] < SAID_TTL][:-1]

    def recent_public_chat(self, limit: int = 12) -> list[tuple[str, str]]:
        names = self.crisis_names()
        return [(x[1], x[2]) for x in self.recent_chat if self.now() - x[0] < SAID_TTL
                and x[1].lower() not in names and not any(n in x[2].lower() for n in names)][-limit:]

    def note_crisis(self, username: str, name: str) -> None:
        """Starts the serious window and forgets their chat, so no callback segment can joke about it later."""
        v = self.viewer(username)
        v["crisis_at"] = self.now()
        v["crisis_said"] = []
        v["crisis_name"] = name
        v.pop("said", None)
        low = name.lower()
        kept = [x for x in self.recent_chat if x[1] != name and low not in x[2].lower()]
        self.recent_chat.clear()
        self.recent_chat.extend(kept)
        # What he already said to them is in here too, and these lines go into everyone else's prompts.
        lines = [x for x in self.lines if low not in x.lower()]
        self.lines.clear()
        self.lines.extend(lines)
        rounds = [x for x in self.round_lines if low not in x[1].lower()]
        self.round_lines.clear()
        self.round_lines.extend(rounds)

    def note_crisis_said(self, username: str, text: str) -> list[str]:
        """Keeps what they said since the first check-in, so he doesn't ask what they already answered. Returns the earlier part."""
        v = self.viewer(username)
        earlier = v.get("crisis_said", [])
        v["crisis_said"] = (earlier + [text])[-4:]
        return earlier

    def crisis_names(self) -> set[str]:
        """Spoken names of everyone in the serious window, lowercase."""
        t = self.now()
        return {v["crisis_name"].lower() for v in self.viewers.values()
                if v.get("crisis_name") and t - v.get("crisis_at", -1e12) < CRISIS_WINDOW}

    def in_crisis(self, username: str) -> bool:
        return self.now() - self.viewer(username).get("crisis_at", -1e12) < CRISIS_WINDOW

    def route_crisis(self, username: str, name: str, action: str) -> str:
        """After a worrying message nothing that viewer says gets a joke or a stock line back."""
        if action == "crisis":
            self.note_crisis(username, name)
            return action
        if action != "ignore" and self.in_crisis(username):
            return "crisis_followup"
        if not self.in_crisis(username):
            self.viewer(username).pop("crisis_said", None)
        return action

    def route_minor(self, username: str, action: str) -> str:
        """Once someone says they're under 18, he doesn't chat about anything else with them."""
        v = self.viewer(username)
        if action in ("crisis", "crisis_followup"):
            return action
        if action == "age":
            v["minor"] = True
        if not v.get("minor") or action == "ignore":
            return action
        # Said once is a wall. Repeating it would only give them the airtime they came for.
        if self.now() - v.get("minor_told", 0) < 300:
            return "ignore"
        v["minor_told"] = self.now()
        return "age"

    def note_seen(self, username: str) -> dict:
        v = self.viewer(username)
        v["seen"] += 1
        v["prev_last"] = v["last"]
        v["last"] = self.now()
        return v
