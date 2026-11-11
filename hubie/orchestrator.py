"""The show runner: plays rounds, reads chat, runs segments, keeps talking.

One loop, one step at a time. Nothing in a step can block past its timeout, and
heartbeat() runs every step, so the watchdog can tell a hung loop from a busy one.
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .audio.queue import Speaker
from .brain.brain import Brain
from .brain.memory import Memory
from .brain.persona import Persona
from .brain.prompts import number_words
from .brain.provider import build_provider
from .brain.scheduler import BIT, CHAT, PLAY, RECAP, RG, SCHOOL, VOTE, Scheduler, Segment
from .chat.base import ChatMessage, ChatSource
from .config import Config, load_dotenv
from .credits import from_env as end_watch_from_env, mark_show_over, show_over
from .games import plays
from .games.base import GameAdapter, GameError, GameGone, NotDemoError
from .safety.moderation import FIXED_ACTIONS, URGENT_ACTIONS, ChatModerator, Decision, clean_chat_text
from .safety.output_filter import OutputFilter, SafeLine
from .tts.http import build_tts
from .util import ask_stage, post_stage, write_status

log = logging.getLogger("hubie.orchestrator")

HANGS_BEFORE_EXIT = 8


def _call_with_timeout(fn, timeout: float, tick=None, every: float = 2.0):
    """Run fn on a thread with a hard limit. tick() runs while waiting, so the heartbeat
    stays honest: it stops only if the main loop itself is stuck, and this wait is bounded."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    th = threading.Thread(target=run, daemon=True)
    th.start()
    end = time.monotonic() + timeout
    while th.is_alive() and time.monotonic() < end:
        th.join(min(every, max(0.01, end - time.monotonic())))
        if tick and th.is_alive():
            tick()
    if th.is_alive():
        raise TimeoutError(f"gave up after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")

SPOKEN_CPS = 14.0     # characters per second of speech, for pacing estimates only
CRISIS_QUIET = 20    # seconds of silence after a crisis line, so no round joke lands right on top of it
AFTER_LINE_GAP = (0.8, 1.5)   # seconds between the end of one line and the next spin or line, so nothing stacks
CHAT_STREAK = 3      # queued chat goes before the next spin, but at most this many reply lines in a row
# Every chat message gets answered by name (contest: "reads chat and answers by name"). Nothing is dropped:
# a busy chat is answered in batches instead (one line naming several viewers).
FAST_AT = 2          # this many still waiting after the one being answered: the reply goes to the cheap chat model
BATCH_AT = 4         # this many viewers waiting: one line answers up to BATCH_MAX of them (cheap model)
BATCH_MAX = 4
ROLLCALL_AT = 10     # this many waiting: a pool line names up to ROLLCALL_MAX of them, no model call
ROLLCALL_MAX = 8
BUSY_CHAT = 2        # this many waiting: a round line is the short one unless the round is big (it is never skipped)
# Messages shown in the on-screen chat list. Blocked ones never are, and nothing serious (crisis, support, age, chasing)
# or steering (injection, fake authority, deposit/advice, cruelty, rage) goes on screen. His replies to the serious
# ones stay off it too.
SHOWN_ACTIONS = {"pass", "pick", "beg", "ai", "sendoff"}
TALK_CAP = 60.0      # seconds of talk segments with no game activity before play resumes ("never loops")
FILL_AFTER = 10.0    # silence this long (outside a deliberate quiet bit) gets a few pool words, no model call
# Live feature words (free spins running inside one round): at most one line per FEATURE_GAP seconds, start to start
# (and never closer than the last line's length plus a breath), the most important event since the last line wins.
FEATURE_GAP = (6.0, 8.0)
FEATURE_QUIET = 14.0          # a running feature with nothing worth a line for this long gets a few words about it
FEATURE_STALE = 12.0          # an event that waited this long behind other speech is dropped, never said late
FEATURE_AFTER_TRIGGER = 12.0  # room after the start line for the surprise clip (it waits for him to stop talking)
FEATURE_END_QUIET = 8.0      # no dead-air filler this soon after a feature's end line: it reads as a glitch
FEATURE_PRIORITY = {"feature_triggered": 5, "retrigger": 4, "milestone": 3, "multiplier": 2, "big_hit": 2}
SIGNOFF_WAIT = 90.0   # at most this long for queued lines (and then the goodbye) to finish playing
SIGNOFF_HOLD = 6.0    # the picture stays up this long after the goodbye, then the show ends


@dataclass
class QueuedChat:
    name: str
    username: str
    text: str
    ts: float
    action: str
    ids: list = field(default_factory=list)   # on-screen chat ids of the message(s) this answers
    count: int = 1                            # messages merged into this one (same viewer, same kind)
    solo: bool = False                        # never batched (the model flagged someone in a batch with it)
    play: tuple | None = None                 # a play ask for a house original: (game id, request), see plays.py


class ChatQueue:
    """Arrival order, nothing dropped. Urgent ones (crisis, support, age, chasing) go first. When a viewer has
    several messages waiting, they come out together so one reply answers them all; a loud viewer still only gets
    one turn per message burst, in the order they first spoke."""

    def __init__(self, now=time.time):
        self.urgent: deque[QueuedChat] = deque()
        self.normal: list[QueuedChat] = []
        self.now = now

    def add(self, q: QueuedChat) -> None:
        (self.urgent if q.action in URGENT_ACTIONS else self.normal).append(q)

    def _take(self, i: int) -> QueuedChat:
        """Removes normal[i] and every later message from the same viewer with the same action, merged into one."""
        q = self.normal.pop(i)
        key = q.username.lower()
        same = [x for x in self.normal[i:] if x.username.lower() == key and x.action == q.action]
        if not same:
            return q
        for x in same:
            self.normal.remove(x)
        texts = [q.text] + [x.text for x in same if x.text]
        return QueuedChat(q.name, q.username, " / ".join(t for t in texts if t)[:400], q.ts, q.action,
                          q.ids + [i for x in same for i in x.ids], q.count + sum(x.count for x in same),
                          q.solo or any(x.solo for x in same),
                          next((x.play for x in reversed([q] + same) if x.play), None))

    def pop(self) -> QueuedChat | None:
        if self.urgent:
            return self.urgent.popleft()
        return self._take(0) if self.normal else None

    def pop_batch(self, n: int) -> list[QueuedChat]:
        """Up to n ordinary messages from different viewers, oldest first (each viewer's waiting ones merged).
        Empty if the oldest message isn't an ordinary one: fixed lines and solo ones are answered on their own."""
        out: list[QueuedChat] = []
        if self.urgent or not self.normal or self.normal[0].action != "pass" or self.normal[0].solo:
            return out
        i = 0
        while i < len(self.normal) and len(out) < n:
            x = self.normal[i]
            if x.action == "pass" and not x.solo:
                out.append(self._take(i))
            else:
                i += 1
        return out

    def push_front(self, items: list[QueuedChat]) -> None:
        self.normal[:0] = items

    def viewers(self) -> int:
        """Viewers with something waiting (urgent ones count too)."""
        return len({q.username.lower() for q in (*self.urgent, *self.normal)})

    def __len__(self) -> int:
        return len(self.urgent) + len(self.normal)


class Orchestrator:
    def __init__(self, adapter: GameAdapter, chat: ChatSource, brain: Brain, speaker, scheduler: Scheduler,
                 moderator: ChatModerator, notify=None, heartbeat=None, now=time.time, sleep=time.sleep,
                 round_timeout: float = 45.0, bet_options=(1, 2, 5, 10), seed: int | None = None,
                 talk_pace: float = 1.0, after_line_gap: tuple[float, float] = AFTER_LINE_GAP,
                 fill_after: float = 0.0, talk_cap: float = TALK_CAP, ender=None, on_show_over=None,
                 signoff_wait: float = SIGNOFF_WAIT, signoff_hold: float = SIGNOFF_HOLD):
        self.adapter = adapter
        self.chat = chat
        self.brain = brain
        self.speaker = speaker
        self.sched = scheduler
        self.mod = moderator
        self.notify = notify or (lambda ev: None)
        self.heartbeat_fn = heartbeat or (lambda **kw: None)
        self.now = now
        self.sleep = sleep
        self.round_timeout = round_timeout
        self.bets = bet_options
        self.rng = random.Random(seed)
        self.chatq = ChatQueue(now=now)
        self.segment: Segment | None = None
        self.rounds_since_chat = 0
        self.benched: dict[str, float] = {}    # game id -> usable again at
        self.consecutive_fails = 0
        self.hangs = 0
        self.refuse_play = False
        self.votes: dict[str, str] = {}
        self.vote_open_until = 0.0
        self.last_speech = now()
        self.counts = {"rounds": 0, "chat_replies": 0, "failures": 0, "ignored": 0, "fixed": 0}
        self.game_names = {}
        self.game_how = {}
        self.game_kinds = {}
        # chat asks for the next round of a house original ("go 10 mines"): game id -> request (+ who asked)
        self.play_requests: dict[str, dict] = {}
        self.plan_share = (0.25, 0.5)      # chance he says his plan first: ordinary plan, risky plan (~1 in 3)
        self.quiet_until = 0.0
        self.last_packed = 0.0
        self.greeted = False
        # Share of the time he talks in talk segments. Below 1, each non-round line buys a rest before the next
        # bit or idle line. Rounds are never skipped: every round gets a line (short for plain rounds).
        self.talk_pace = min(1.0, max(0.05, talk_pace))
        self.rest_until = 0.0
        # Last game activity (a spin, a game switch). Talk with none for talk_cap seconds ends and play resumes.
        self.talk_cap = talk_cap
        self.last_game_activity = now()
        self.talk_gap_until = -1.0
        self.after_line_gap = after_line_gap
        self.was_speaking = False
        self.gap_until = 0.0
        self.chat_streak = 0
        # Dead air: fill_after seconds with nothing queued or playing gets a short pool aside (0 = off).
        self.fill_after = max(0.0, fill_after)
        self.silent_since: float | None = None
        self.counts["asides"] = 0
        self.screen: tuple[str, str] | None = None
        self.doing, self.doing_since = "idle", now()
        # A feature running inside the current round (None outside one): see feature_tick().
        self.feature: dict | None = None
        self.feature_gap = FEATURE_GAP
        self.feature_llm_share = 0.2
        self.counts["feature_lines"] = 0
        self.fill_hold = 0.0          # seconds of silence fillers wait out after the last line (feature end line)
        self.live_balance: float | None = None
        self.counts["answered"] = 0      # chat messages answered (a merged or batched reply answers several)
        self.counts["batches"] = 0
        self.followups: deque = deque()   # the second half of a long batched reply, said next
        self.chat_seq = 0
        # The automatic end (hubie/credits.py): ender.should_end() is asked between steps, so a round, bonus or
        # reply in progress always finishes first. on_show_over(**info) writes the flag that stops the restarts.
        self.ender = ender
        self.on_show_over = on_show_over or (lambda **info: None)
        self.signoff_wait = signoff_wait
        self.signoff_hold = signoff_hold
        self.ending: str | None = None

    # ---- helpers ----

    def games(self):
        if self.refuse_play:
            return []
        t = self.now()
        try:
            games = _call_with_timeout(self.adapter.list_games, 10, self.beat)
        except Exception as e:
            log.warning("list_games failed: %r", e)
            return []
        self.game_names.update({g.id: g.name for g in games})
        self.game_how.update({g.id: g.how_it_works for g in games})
        self.game_kinds.update({g.id: g.kind for g in games})
        return [g for g in games if self.benched.get(g.id, 0) <= t]

    def say(self, line: SafeLine, kind: str, mood: str = "idle", reply_to: dict | None = None,
            droppable: bool = False) -> bool:
        extra = {"reply_to": reply_to} if reply_to else {}
        if droppable:
            extra["droppable"] = True    # the mixer may skip it if a newer round line is already queued
        ok = bool(self.speaker.say(line, kind=kind, mood=mood, **extra))
        if ok:
            self.last_speech = self.now()
            # a new line restarts the quiet clock; silence() isn't polled while he talks, so a clock started
            # before this line would let a filler fire the moment it ends
            self.silent_since = None
            self.fill_hold = 0.0
            if self.talk_pace < 1.0 and kind not in ("round", "aside", "feature"):
                est = max(1.0, len(line.text) / SPOKEN_CPS)
                self.rest_until = max(self.rest_until, self.last_speech + est / self.talk_pace)
        return ok

    def is_original(self, game_id: str | None) -> bool:
        return bool(game_id) and self.game_kinds.get(game_id) == "original"

    def _ev(self, event: str, game_id: str | None) -> str:
        """The house-original version of a pool (games, not slots), when the game is one and the pool exists."""
        alt = f"{event}_original"
        return alt if self.is_original(game_id) and self.brain.persona.canned.get(alt) else event

    # ---- what the game area shows ----

    def show_screen(self, state: str, game: str | None = None) -> None:
        """Tells the stage what the game area is doing: loading / recovering / offline put the switching card
        over it, ready shows the game. Sent only on a change."""
        game = game if game is not None else (self.screen[1] if self.screen else "")
        if self.screen == (state, game):
            return
        self.screen = (state, game)
        self.notify({"type": "game_state", "state": state, "game": game})

    def _screen_after_play(self, game: str) -> None:
        try:
            ok = bool(self.adapter.screen_ready())
        except Exception:
            ok = False
        self.show_screen("ready" if ok else "recovering", game)

    # ---- dead air ----

    def silence(self) -> float:
        """Seconds with nothing queued or playing."""
        if self.speaker.pending() > 0:
            self.silent_since = None
            return 0.0
        if self.silent_since is None:
            self.silent_since = self.now()
        return self.now() - self.silent_since

    def fill_air(self, event: str = "aside_short", after: float | None = None) -> bool:
        """A few words from the pool when it has been quiet for fill_after seconds, unless the quiet is on purpose
        (after a crisis line, a talk segment's planned gap)."""
        if not self.fill_after or self.now() < self.quiet_until or self.chatq.urgent:
            return False
        if self.silence() < max(self.fill_after, after or 0, self.fill_hold):
            return False
        self.say(self.brain.aside(event), "aside", "idle")
        self.silent_since = None
        self.counts["asides"] += 1
        return True

    def _waiting(self, event: str, game: str = ""):
        # a long round (tumbles, free spins) is watched, so it gets fewer words than a loading screen
        after = 18.0 if event.startswith("wait_round") else None

        def tick():
            self.beat()
            if event.startswith("wait_round"):
                try:
                    self.feature_tick(game)
                except Exception as e:
                    log.warning("feature line failed: %r", e)
                if self.feature_live():
                    return       # the feature has its own words: no fillers on top of it
            try:
                self.fill_air(event, after)
            except Exception as e:
                log.warning("aside failed: %r", e)
        return tick

    # ---- live feature words ----

    def feature_live(self) -> bool:
        return bool(self.feature) and not self.feature.get("ended")

    def _poll_events(self) -> list[dict]:
        try:
            return list(self.adapter.poll_events() or [])
        except Exception as e:
            log.warning("poll_events failed: %r", e)
            return []

    def feature_tick(self, game: str) -> bool:
        """Reads the adapter's live events and, when one is due, says a short line over the running feature.
        Returns whether a line was said."""
        now = self.now()
        for ev in self._poll_events():
            t = ev.get("type")
            if t == "decision":
                self._say_decision(ev, game)
                continue
            if t == "feature_triggered":
                self.feature = {"game": game, "bought": bool(ev.get("bought")), "spins": int(ev.get("spins") or 0),
                                "left": int(ev.get("spins") or 0), "next_ok": now, "last_line": now,
                                "pending": dict(ev, _at=now), "ended": False, "lines": 0}
                # the surprise clip: the stage holds it until he isn't talking, and keeps its own cooldown
                self.notify({"type": "react", "name": "surprise", "why": "feature_triggered"})
                continue
            f = self.feature
            if f is None or f.get("ended"):
                continue
            if t == "progress":
                f["left"] = int(ev.get("left") or 0)
                f["spins"] = int(ev.get("spins") or f["spins"])
                self._live_balance(ev.get("balance"))
            elif t == "feature_end":
                f.update(ended=True, pending=None, end=ev)
            elif t in FEATURE_PRIORITY:
                if t == "retrigger":
                    f["spins"] = int(ev.get("spins") or f["spins"])
                cur = f.get("pending")
                if (cur is None or now - cur["_at"] > FEATURE_STALE
                        or FEATURE_PRIORITY[t] >= FEATURE_PRIORITY.get(cur.get("type"), 0)):
                    f["pending"] = dict(ev, _at=now)
        f = self.feature
        if not f or f.get("ended") or now < f["next_ok"] or self.speaker.pending() > 0:
            return False
        ev = f.get("pending")
        if ev is not None and now - ev["_at"] > FEATURE_STALE and ev.get("type") != "feature_triggered":
            f["pending"] = ev = None
        if ev is None:
            if now - f["last_line"] < FEATURE_QUIET or f["left"] < 2:
                return False
            ev = {"type": "quiet", "left": f["left"]}
        event, fmt, facts = self._feature_words(ev, f)
        line = self.brain.feature_line(event, game, facts, llm_share=self.feature_llm_share, **fmt)
        self.say(line, "feature", "happy" if ev.get("type") != "quiet" else "idle", droppable=True)
        f["pending"] = None
        f["last_line"] = now
        f["lines"] += 1
        est = len(line.text) / SPOKEN_CPS
        # after the start line the stage plays the surprise clip once he's quiet: leave it room
        hold = FEATURE_AFTER_TRIGGER if ev.get("type") == "feature_triggered" else 0.0
        f["next_ok"] = now + max(self.rng.uniform(*self.feature_gap), est + 3.0, hold)
        self.counts["feature_lines"] += 1
        self.silent_since = None
        return True

    def _live_balance(self, value) -> None:
        """The panel's demo balance while a feature pays (absolute, so the round event at the end just overwrites
        it, nothing is added twice). Sent only on a change."""
        try:
            bal = round(float(value), 2)
        except (TypeError, ValueError):
            return
        if bal != self.live_balance:
            self.live_balance = bal
            self.notify({"type": "live_balance", "balance": bal})

    @staticmethod
    def _feature_words(ev: dict, f: dict) -> tuple[str, dict, dict]:
        """(pool event, placeholders as spoken words, facts for a model line)."""
        t = ev.get("type")
        w = lambda v: number_words(int(round(float(v or 0))))
        left = int(ev.get("left", f.get("left", 0)) or 0)
        if t == "feature_triggered":
            event = "feature_start_buy" if ev.get("bought") else "feature_start"
            fmt = {"spins": w(ev.get("spins"))}
        elif t == "retrigger":
            event, fmt = "feature_retrigger", {"added": w(ev.get("added")), "spins": w(ev.get("spins"))}
        elif t == "multiplier":
            event, fmt = "feature_multi", {"value": w(ev.get("value"))}
        elif t == "big_hit":
            event, fmt = "feature_hit", {"x": w(ev.get("x"))}
        elif t == "milestone":
            event = "feature_milestone_done" if left <= 2 else "feature_milestone"
            fmt = {"x": w(ev.get("x")), "left": w(left)}
        else:
            event, fmt = "feature_quiet", {"left": w(left)}
        return event, fmt, {"event": event, **fmt}

    def _loading_tick(self, game_id: str):
        """While a game loads: the card or the browsing page per the adapter, his pick line, waiting asides."""
        name = self.game_names.get(game_id, game_id)
        said_pick = [False]

        def tick():
            self.beat()
            try:
                st = self.adapter.screen_state()
            except Exception:
                st = "loading"
            browsing = st in ("browsing", "picked")
            self.show_screen("browsing" if browsing else ("ready" if st == "ready" else "loading"), name)
            try:
                if st == "picked" and not said_pick[0] and self.speaker.pending() == 0:
                    said_pick[0] = True
                    self.say(self.brain.fixed("browse_pick", game=name), "segment", "idle")
                else:
                    self.fill_air(self._ev("browse_aside", game_id) if browsing else "wait_loading")
            except Exception as e:
                log.warning("loading line failed: %r", e)
        return tick

    def resting(self) -> bool:
        return self.talk_pace < 1.0 and self.now() < self.rest_until

    def beat(self, **extra) -> None:
        self.heartbeat_fn(last_speech=self.last_speech, rounds=self.counts["rounds"],
                          replies=self.counts["chat_replies"], chat_backlog=len(self.chatq),
                          segment=self.segment.kind if self.segment else None, llm_calls=self.brain.llm_calls,
                          chat_ok=self.chat.healthy(), doing=self.doing, doing_since=self.doing_since,
                          ending=self.ending, **({"credits": self.ender.snapshot()} if self.ender else {}), **extra)

    def busy(self, what: str) -> None:
        """What the loop is blocked on right now; goes into the heartbeat so a stall can be traced."""
        self.doing, self.doing_since = what, self.now()

    # ---- chat ----

    def ingest_chat(self) -> None:
        try:
            msgs: list[ChatMessage] = self.chat.poll()
        except Exception as e:
            log.warning("chat poll failed: %r", e)
            return
        for m in msgs:
            d = self.mod.review(m)
            if d.action == "ignore":
                self.counts["ignored"] += 1
                continue
            self.brain.mem.note_seen(m.username)
            action = self.brain.mem.route_crisis(m.username, d.speak_name, d.action)
            action = self.brain.mem.route_minor(m.username, action)
            if action == "ignore":
                self.counts["ignored"] += 1
                continue
            if action != d.action:
                keep = clean_chat_text(m.text) if action == "crisis_followup" else ""
                d = Decision(action, d.reason, d.speak_name, keep)
            ids = []
            if d.action in SHOWN_ACTIONS:
                text = d.clean_text or clean_chat_text(m.text)
                if text:
                    self.chat_seq += 1
                    cid = f"c{self.chat_seq}"
                    ids.append(cid)
                    # the on-screen list shows the Kick username (a name unfit to say or show reads "friend")
                    shown = d.speak_name if d.speak_name == "friend" else _screen_name(m.username)
                    self.notify({"type": "chat", "id": cid, "name": shown, "user": m.username.lower(), "text": text})
            play = None
            if d.action == "pass":
                self._note_vote(d.speak_name, d.clean_text)
                self.brain.mem.note_said(m.username, d.speak_name, d.clean_text)
                play = self._play_ask(d.clean_text)
            self.chatq.add(QueuedChat(d.speak_name, m.username, d.clean_text, m.ts or self.now(), d.action, ids,
                                      solo=play is not None, play=play))

    def _play_ask(self, text: str) -> tuple | None:
        """A concrete ask for how he plays a house original next ("go 10 mines", "daredevil", "100x"), or None.
        Only game settings: anything about money or bet size is never one (plays.parse_request)."""
        seg = self.segment
        current = seg.game_id if seg is not None and seg.kind == PLAY and self.is_original(seg.game_id) else None
        try:
            ask = plays.parse_request(text, current)
        except Exception as e:
            log.warning("play ask not read: %r", e)
            return None
        if ask is None or not self.is_original(ask[0]):
            return None
        return ask

    def _note_vote(self, name: str, text: str) -> None:
        if self.now() > self.vote_open_until:
            return
        low = text.lower()
        for gid, gname in self.game_names.items():
            if gname.lower() in low.split() or gid in low.split():
                self.votes.setdefault(name, gid)
                return

    def answer(self, q: QueuedChat, fast: bool = False) -> None:
        if q.play is not None and q.action == "pass" and not self.brain.mem.in_crisis(q.username):
            # a play ask: he takes it, by name, and the next round of that game follows it
            gid, req = q.play
            self.play_requests[gid] = {**req, "name": q.name}
            seg = self.segment
            now = seg is not None and seg.kind == PLAY and seg.game_id == gid
            line = self.brain.plan_line({"short": plays.request_text(gid, req)}, self.game_names.get(gid, gid),
                                        name=q.name, now=now)
            log.info("play ask from %s for %s: %s", q.name, gid, req)
            self.counts["chat_replies"] += 1
            self.counts["play_asks"] = self.counts.get("play_asks", 0) + 1
            self.counts["answered"] += q.count
            self._said_reply(line, "happy", {"names": [q.name]}, q)
            return
        if q.action == "crisis_followup":
            line = self.brain.crisis_followup(q.name, q.text, q.username)
            mood = "serious"
            self.quiet_until = self.now() + CRISIS_QUIET
        elif q.action in FIXED_ACTIONS:
            self.counts["fixed"] += 1
            line = self.brain.fixed(FIXED_ACTIONS[q.action], name=q.name)
            mood = "serious" if q.action in URGENT_ACTIONS else "idle"
            if q.action in URGENT_ACTIONS:
                # he goes quiet after these, no quip follows
                self.quiet_until = self.now() + (CRISIS_QUIET if q.action == "crisis" else 6)
        else:
            line = self.brain.reply_chat(q.name, q.text, q.username, fast=fast)
            mood = "happy"
            if self.brain.mem.in_crisis(q.username):   # the model caught what the rules didn't
                mood = "serious"
                self.quiet_until = self.now() + CRISIS_QUIET
                self._hide(q.ids)
        self.counts["chat_replies"] += 1
        self.counts["answered"] += q.count
        # his reply goes into the on-screen chat list, except anything serious (crisis, support, age, chasing)
        shown = {"names": [q.name]} if q.action not in URGENT_ACTIONS and q.action != "crisis_followup" \
            and mood != "serious" else None
        self._said_reply(line, mood, shown, q)

    def _said_reply(self, line: SafeLine, mood: str, shown: dict | None, *qs: QueuedChat) -> bool:
        if self.say(line, "chat", mood, reply_to=shown):
            return True
        # the voice failed (TTS down): nothing was said, so they go back to the front, never dropped
        log.warning("chat reply not spoken, re-queued")
        for q in qs:
            q.solo = True
        self.chatq.push_front(list(qs))
        self.counts["answered"] -= sum(q.count for q in qs)
        return False

    def _hide(self, ids: list) -> None:
        for cid in ids or []:
            self.notify({"type": "chat_hide", "id": cid})

    def answer_batch(self, batch: list[QueuedChat], rollcall: bool = False) -> None:
        """One line naming every viewer in the batch."""
        line = self.brain.reply_batch([(q.name, q.text, q.username) for q in batch], rollcall=rollcall,
                                      context=self._chat_context())
        if line is None:
            # the model flagged someone: each is answered alone (the right one gets the fixed line), in order
            for q in batch:
                q.solo = True
            self.chatq.push_front(batch[1:])
            self.answer(batch[0], fast=True)
            return
        self.counts["chat_replies"] += 1
        self.counts["batches"] += 1
        self.counts["answered"] += sum(q.count for q in batch)
        parts = self.brain.split_line(line)
        shown = {"names": [q.name for q in batch]}
        # a long batched answer is said as two lines: the second goes right after the first (step: "followup")
        if self._said_reply(parts[0], "happy", shown, *batch) and len(parts) > 1:
            self.followups.extend((p, shown) for p in parts[1:])

    def _chat_context(self) -> str:
        seg = self.segment
        game = self.game_names.get(seg.game_id, seg.game_id) if seg and seg.kind == PLAY and seg.game_id else ""
        out = f"you're playing {game} in demo mode right now." if game else ""
        if self.now() > self.vote_open_until:
            out += " the show picks the next game itself, chat doesn't."
        return out.strip()

    def answer_next(self) -> bool:
        """Answers whatever chat is next: urgent first, then in arrival order, batched when chat is busy.
        Returns whether anything was answered."""
        if self.chatq.urgent:
            self.answer(self.chatq.pop())
            return True
        waiting = self.chatq.viewers()
        if waiting >= BATCH_AT:
            rollcall = waiting >= ROLLCALL_AT
            batch = self.chatq.pop_batch(ROLLCALL_MAX if rollcall else BATCH_MAX)
            if len(batch) > 1:
                self.answer_batch(batch, rollcall=rollcall)
                return True
            self.chatq.push_front(batch)
        q = self.chatq.pop()
        if q is None:
            return False
        self.answer(q, fast=len(self.chatq) >= FAST_AT)
        return True

    def _maybe_packed(self) -> bool:
        """A packed chat makes him slower on purpose, and he says so now and then."""
        if self.chatq.viewers() >= 8 and self.now() - self.last_packed > 300:
            self.last_packed = self.now()
            self.say(self.brain.fixed("chat_packed"), "chat", "tired")
            return True
        return False

    # ---- games ----

    def enter_game(self, game_id: str) -> bool:
        self.busy(f"load_game {game_id}")
        try:
            # a browsing adapter takes up to ~45 s (look around, then the game's own load)
            limit = 75 if getattr(self.adapter, "browse", False) else 30
            _call_with_timeout(lambda: self.adapter.load_game(game_id), limit, self._loading_tick(game_id), every=0.5)
            return True
        except GameGone as e:
            self._game_gone(game_id, e)
            return False
        except Exception as e:
            log.warning("load_game %s failed: %r", game_id, e)
            self._game_failed(game_id)
            return False

    def _game_gone(self, game_id: str, why) -> None:
        """The adapter turned this game off for good (a zero-bet guard on the house originals): out of the rotation,
        no stall line, the show carries on with the other games and talk."""
        log.error("game %s off for the rest of the show: %s", game_id, why)
        self.benched[game_id] = float("inf")
        if self.segment:
            self.segment.done = self.segment.target
        self.show_screen("recovering", self.game_names.get(game_id, game_id))

    def _game_failed(self, game_id: str | None, hang: bool = False) -> None:
        self.counts["failures"] += 1
        self.consecutive_fails += 1
        if hang:
            self.hangs += 1
        self.show_screen("recovering", self.game_names.get(game_id or "", game_id or "") if game_id else None)
        if game_id and self.consecutive_fails >= 3:
            self.benched[game_id] = self.now() + 600
            self.consecutive_fails = 0
            if self.segment:
                self.segment.done = self.segment.target
        threading.Thread(target=self._safe_recover, daemon=True).start()
        self.say(self.brain.fixed("stall"), "system", "tired")

    def _safe_recover(self) -> None:
        try:
            self.adapter.recover()
        except Exception as e:
            log.warning("adapter recover raised: %r", e)

    def play_one(self, seg: Segment) -> None:
        if not self.adapter.is_demo():
            log.error("adapter reports non-demo surface; refusing to play")
            self.refuse_play = True
            self.show_screen("offline", "")
            seg.done = seg.target
            return
        bet = self.rng.choice(self.bets)
        self.busy(f"play_round {seg.game_id}")
        name = self.game_names.get(seg.game_id, seg.game_id)
        self._poll_events()           # anything left over from an earlier round is stale
        self.feature = None
        self.live_balance = None
        try:
            opts = seg.options
            if self.is_original(seg.game_id):
                opts = {**(seg.options or {}), "plan": self._plan_for(seg.game_id, name)}
            result = _call_with_timeout(
                (lambda: self.adapter.play_round(bet, opts)) if opts else (lambda: self.adapter.play_round(bet)),
                self.round_timeout, self._waiting(self._ev("wait_round", seg.game_id), name), every=0.5)
        except TimeoutError as e:
            self.feature = None
            log.warning("round timed out: %s", e)
            self._game_failed(seg.game_id, hang=True)
            return
        except NotDemoError:
            self.refuse_play = True
            self.show_screen("offline", "")
            seg.done = seg.target
            return
        except GameGone as e:
            self.feature = None
            self._game_gone(seg.game_id, e)
            return
        except Exception as e:
            self.feature = None
            log.warning("round failed: %r", e)
            self._game_failed(seg.game_id)
            return
        if not result.is_demo:
            log.error("round result flagged non-demo; refusing to play further")
            self.refuse_play = True
            self.show_screen("offline", "")
            seg.done = seg.target
            return
        self.consecutive_fails = 0
        self.counts["rounds"] += 1
        self.rounds_since_chat += 1
        seg.done += 1
        stats = self.brain.mem.stats
        stats["rounds"] += 1
        new_best = result.multiplier > stats["best_mult"] and result.multiplier >= 3
        stats["best_mult"] = max(stats["best_mult"], result.multiplier)
        self._poll_events()           # feature_end and whatever else came with the last response
        feature_seen = bool(self.feature)
        if feature_seen:
            self.counts["features"] = self.counts.get("features", 0) + 1
        self.feature = None
        # feature_seen: the surprise clip already played when the free spins started; the end gets win or loss only
        self.notify({"type": "round", "game": name, **result.to_dict(), **({"feature_seen": True} if feature_seen else {})})
        self._screen_after_play(name)
        # Every round gets a spoken line. Plain ones (no bonus, under 2x, not a session best) get a 2-6 word one.
        plain = self.brain.is_plain(result) and not new_best
        if not plain and len(self.chatq) >= BUSY_CHAT and not new_best and result.multiplier < 10 \
                and not (result.detail or {}).get("free_spins_triggered") and not (result.detail or {}).get("bonus_buy"):
            plain = True     # busy chat: a notable-but-not-big round gets the short line so chat gets its turn
            self.counts["busy_short"] = self.counts.get("busy_short", 0) + 1
        self.counts["short_rounds" if plain else "full_rounds"] = self.counts.get("short_rounds" if plain else "full_rounds", 0) + 1
        mood = "excited" if result.multiplier >= 10 else ("happy" if result.won else "tired")
        line = self.brain.comment_round(result, name, seg.title, how=self.game_how.get(seg.game_id, ""), short=plain)
        self.say(line, "round", mood, droppable=plain)
        if feature_seen:
            self.fill_hold = FEATURE_END_QUIET

    def _say_decision(self, ev: dict, game: str) -> None:
        """A blackjack decision, said before it's pressed: "16 vs a 10. ugh. hitting." (by name when chat pushed it)."""
        try:
            self.say(self.brain.decision_line(ev, game), "feature", "idle")
            self.counts["decisions"] = self.counts.get("decisions", 0) + 1
        except Exception as e:
            log.warning("decision line failed: %r", e)

    def _plan_for(self, game_id: str, name: str) -> dict:
        """The next round's plan for a house original: a chat ask if one is waiting (already answered by name),
        else his own pick, said out loud about one round in three."""
        req = self.play_requests.pop(game_id, None)
        plan = plays.plan_round(game_id, self.rng, req)
        if req is not None:
            plan["said"] = True
            plan["asked_by"] = req.get("name")
        if plan.get("announce"):
            # an all-in mines round is always said first ("3 mines, going 9 deep. this is stupid.")
            try:
                self.say(self.brain.plan_line(plan, name), "segment", "idle")
                plan["said"] = True
            except Exception as e:
                log.warning("plan line failed: %r", e)
        elif req is None and self.rng.random() < self.plan_share[1 if plan.get("risky") else 0]:
            try:
                self.say(self.brain.plan_line(plan, name), "segment", "idle")
                plan["said"] = True
            except Exception as e:
                log.warning("plan line failed: %r", e)
        return plan

    # ---- segments ----

    def talk_kinds(self) -> set:
        return {BIT, CHAT} | set(self.brain.persona.segments)

    def talk_overdue(self) -> bool:
        return bool(self.talk_cap) and self.now() - self.last_game_activity >= self.talk_cap

    def start_segment(self) -> None:
        games = self.games()
        force = self.talk_overdue() and not self.refuse_play
        seg = self.sched.next(games, force_play=True) if force else self.sched.next(games)
        self.segment = seg
        self.notify({"type": "segment", "kind": seg.kind, "title": seg.title, "game": self.game_names.get(seg.game_id),
                     "topic": seg.topic})
        log.info("segment %s game=%s topic=%s target=%s", seg.kind, seg.game_id, seg.topic, seg.target)
        if not games:
            self.show_screen("offline", "")
        if seg.kind == PLAY:
            self.last_game_activity = self.now()
            # The card goes up and he says where he's going right away; the load (10-25 s) gets pool asides.
            name = self.game_names.get(seg.game_id, seg.game_id)
            self.show_screen("loading", name)
            start = self._ev("browse_start", seg.game_id) if getattr(self.adapter, "browse", False) \
                else "game_switch_start"
            if not self.brain.persona.canned.get(start):
                start = "game_switch"
            self.say(self.brain.fixed(start, game=name, line=""), "segment", "idle")
            if self.enter_game(seg.game_id):
                self._screen_after_play(name)
                note = self.brain.persona.game_line(seg.game_id)
                if note and self.speaker.pending() == 0:   # never on top of the switch line
                    self.say(self.brain.fixed("game_switch", game=name, line=note), "segment", "idle")
            else:
                seg.done = seg.target
        elif seg.kind == VOTE:
            self.votes = {}
            self.vote_open_until = self.now() + 40
            self.say(self.brain.fixed("invite_vote"), "segment", "happy")
        elif seg.kind == RG:
            self.say(self.brain.fixed("rg_reminder"), "segment", "serious")
            seg.done = seg.target
        elif seg.kind == RECAP:
            st = self.brain.mem.stats
            self.say(self.brain.fixed("recap", rounds=st["rounds"], best=st["best_mult"]), "segment", "idle")
            seg.done = seg.target
        elif seg.kind == SCHOOL:
            info = next((g for g in games if g.id == seg.game_id), None)
            if info:
                self.say(self.brain.fixed("game_school", game=info.name, how=info.how_it_works), "segment", "idle")
            seg.done = seg.target
        elif seg.kind == CHAT and not games:
            self.say(self.brain.fixed("talk_only"), "segment", "idle")

    def close_vote(self, seg: Segment) -> None:
        tally: dict[str, list[str]] = {}
        for name, gid in self.votes.items():
            tally.setdefault(gid, []).append(name)
        if tally:
            gid = max(tally, key=lambda g: len(tally[g]))
            self.sched.force_next_game(gid)
            self.say(self.brain.fixed("vote_result", game=self.game_names.get(gid, gid), name=tally[gid][0]),
                     "segment", "happy")
        else:
            self.say(self.brain.fixed("no_vote"), "segment", "tired")
        seg.done = seg.target

    def step(self) -> str:
        self.busy("step")
        self.beat()
        self.ingest_chat()
        if self.speaker.pending() > 0:
            self.was_speaking = True
            return "speaking"
        if self.was_speaking:
            # the line just finished: a short breath before anything else is said or spun
            self.was_speaking = False
            self.gap_until = self.now() + self.rng.uniform(*self.after_line_gap)
        if self.now() < self.gap_until:
            return "gap"
        if self.chatq.urgent:
            self.answer(self.chatq.pop())
            return "urgent"
        if self.followups:
            line, shown = self.followups.popleft()
            self.say(line, "chat", "happy", reply_to=shown)
            return "followup"
        seg = self.segment
        if seg is not None and 0 < seg.done < seg.target and seg.kind in self.talk_kinds() and self.talk_overdue() \
                and self.games() and not self.refuse_play:
            # a long stretch of talk with no game: end it, the next segment is play
            log.info("talk segment %s cut after %.0f s with no game activity", seg.kind, self.now() - self.last_game_activity)
            seg.done = seg.target
            if self.quiet_until == self.talk_gap_until:   # a talk gap, never a crisis quiet
                self.quiet_until = 0.0
        if self.now() < self.quiet_until:
            return "quiet"
        if self.fill_air():
            return "aside"
        if self._maybe_packed():
            return "packed"
        seg = self.segment
        if seg is None or seg.finished:
            self.start_segment()
            return "segment"
        if seg.kind == PLAY:
            # queued chat goes first and the spin waits, a few replies at most before the next spin
            if len(self.chatq) and self.chat_streak < CHAT_STREAK and self.answer_next():
                self.chat_streak += 1
                self.rounds_since_chat = 0
                return "chat"
            self.chat_streak = 0
            self.play_one(seg)
            self.last_game_activity = self.now()
            return "round"
        if seg.kind == VOTE:
            if self.now() >= self.vote_open_until:
                self.close_vote(seg)
                return "vote_closed"
            if self.answer_next():
                return "chat"
            return "voting"
        if seg.kind == CHAT:
            if self.answer_next():
                seg.done += 1
                return "chat"
            if self.resting():
                return "resting"
            seg.done += 1
            self.say(self.brain.fixed("idle"), "idle", "tired")
            return "idle"
        if seg.kind == BIT and seg.kind not in self.brain.persona.segments:
            if self.resting():
                return "resting"
            seg.done += 1
            self.say(self.brain.bit(seg.topic, seg.done, seg.target, self.sched.is_night()), "bit", "idle")
            return "bit"
        if seg.kind in self.brain.persona.segments:
            # Talk segments leave room for chat, and some leave long silences on purpose.
            if self.answer_next():
                return "chat"
            if self.resting():
                return "resting"
            seg.done += 1
            self.say(self.brain.talk_segment(seg.kind, seg.topic, seg.done, seg.target, self.sched.is_night()),
                     "bit", "tired" if seg.gap > 5 else "idle")
            self.quiet_until = self.talk_gap_until = self.now() + seg.gap
            return "talk"
        seg.done = seg.target
        return "noop"

    # ---- the end of the show ----

    def end_reason(self) -> str | None:
        if self.ender is None:
            return None
        try:
            return self.ender.should_end()
        except Exception:
            log.exception("end check failed")
            return None

    def _wait_quiet(self, limit: float, every: float = 0.25) -> bool:
        """Waits until nothing is queued or playing (bounded), heartbeating. Returns whether it got quiet."""
        for _ in range(max(1, int(limit / every))):
            if self.speaker.pending() == 0:
                return True
            self.beat()
            self.sleep(every)
        return self.speaker.pending() == 0

    def sign_off(self, reason: str) -> dict:
        """Lines already queued play out, then one goodbye (subtitles only when the voice already refused), then
        the picture holds a few seconds and the show-over flag goes down. Nothing new starts once this begins."""
        self.ending = reason
        self.busy(f"sign_off {reason}")
        left = self.ender.remaining() if self.ender and hasattr(self.ender, "remaining") else None
        log.warning("show ending (%s), credits left %s", reason, left)
        self.notify({"type": "segment", "kind": "signoff", "title": "that's the show", "game": None, "topic": None})
        self._wait_quiet(self.signoff_wait)
        event = "signoff_silent" if reason == "voice_quota" and self.brain.persona.canned.get("signoff_silent") \
            else "signoff"
        line = self.brain.fixed(event)
        if not self.say(line, "system", "tired"):
            # no voice at all, not even the silent stand-in: the subtitle still goes up
            dur = max(3.0, len(line.text) / SPOKEN_CPS)
            self.notify({"type": "speak", "text": line.text, "mood": "tired", "duration": dur, "kind": "system"})
            for _ in range(int(dur / 0.5) + 1):
                self.beat()
                self.sleep(0.5)
        self._wait_quiet(self.signoff_wait)
        for _ in range(int(self.signoff_hold / 0.5)):
            self.beat()
            self.sleep(0.5)
        self.counts["signoff"] = 1
        info = {"reason": reason, "line": line.text, "credits_left": left, "rounds": self.counts["rounds"],
                "answered": self.counts["answered"]}
        try:
            self.brain.mem.save(force=True)
        except Exception:
            log.exception("memory save at sign-off failed")
        self.on_show_over(**info)
        self.busy("show_over")
        self.beat()
        log.warning("show over: %s", info)
        return info

    def run(self, stop=lambda: False, idle_sleep: float = 0.25) -> None:
        self.show_screen("loading", "")
        self.busy("adapter start")
        try:
            first = "back" if self.brain.mem.stats.get("lines", 0) else "hello"
            self.say(self.brain.fixed(first), "system", "idle")
        except Exception:
            log.exception("opening line failed")
        # the game browser can take a minute to come up: asides fill it, and its own timeout still raises
        _call_with_timeout(self.adapter.start, 200, self._waiting("wait_loading"))
        self.chat.start()
        while not stop():
            reason = self.end_reason()
            if reason:
                self.sign_off(reason)
                return
            try:
                self.step()
            except Exception:
                log.exception("step failed, carrying on")
                self.counts["failures"] += 1
                self.beat(step_error=True)
                self.sleep(1.0)
            if self.hangs >= HANGS_BEFORE_EXIT:
                # Timed-out threads can't be killed. A clean restart reclaims them.
                log.error("%d hung rounds, exiting for a clean restart", self.hangs)
                self.brain.mem.save(force=True)
                raise SystemExit(3)
            self.sleep(idle_sleep)


def _screen_name(username: str) -> str:
    """The Kick username as the on-screen chat list shows it: no zero-width tricks, at most 24 characters."""
    from .safety.text import ZERO_WIDTH
    n = ZERO_WIDTH.sub("", username or "").strip()
    return n[:24] or "friend"


def build_adapter(cfg: Config) -> GameAdapter:
    if cfg.game == "fake":
        from .games.fake import FakeGameAdapter
        return FakeGameAdapter()
    if cfg.game == "housebets":
        from .games.housebets import HousebetsSlotsAdapter
        games = [g for g in os.environ.get("HUBIE_HB_GAMES", "").split(",") if g.strip()]
        return HousebetsSlotsAdapter(display=cfg.display if cfg.scene_bg == "x11" else None,
                                     width=cfg.width, height=cfg.height, games=games or None,
                                     allow_unverified=os.environ.get("HUBIE_HB_UNVERIFIED") == "1")
    raise ValueError(f"unknown game adapter {cfg.game}")


def build_chat(cfg: Config) -> ChatSource:
    """A missing chat setting costs the show its chat, never the show: no crash loop over config."""
    try:
        if cfg.chat == "fake":
            from .chat.fake import FakeChatSource
            return FakeChatSource(per_minute=cfg.fake_chat_per_min)
        if cfg.chat == "kick":
            from .chat.kick import KickChatSource
            return KickChatSource(os.environ["KICK_PUSHER_URL"], os.environ["KICK_CHATROOM_ID"])
        if cfg.chat == "kick_webhook":
            from .chat.kick_webhook import KickWebhookSource
            return KickWebhookSource(int(os.environ.get("KICK_WEBHOOK_PORT", "18795")))
        raise ValueError(f"unknown chat source {cfg.chat}")
    except (KeyError, ValueError, OSError) as e:
        log.error("chat source %s not usable (%r), running without chat", cfg.chat, e)
        from .chat.base import NoChat
        return NoChat()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config()
    cfg.ensure_dirs()
    if show_over(cfg.show_over_flag):
        log.warning("the show is over (%s), not starting", cfg.show_over_flag)
        return
    persona = Persona.load(cfg.persona)
    memory = Memory(cfg.data_dir / "memory.json")
    env = dict(os.environ)
    extra = []
    if cfg.game == "housebets" and os.environ.get("HUBIE_HB_GAMES", "").strip().lower() == "none":
        # originals only (rule: "Plays House originals"): old slot hits in memory must not come up on air
        import re as _re
        from .games.housebets import GAMES as _SLOTS
        extra = [r"\b" + _re.escape(g.name.lower()) + r"\b" for g in _SLOTS] + [
            r"\bsugar rush\b", r"\bsweet bonanza\b", r"\bgates of olympus\b"]
    brain = Brain(persona, build_provider(cfg.provider, env), memory, OutputFilter(extra_patterns=extra),
                  short_llm_share=float(os.environ.get("HUBIE_SHORT_LLM_SHARE", "0.35") or 0))
    speaker = Speaker(build_tts(cfg.tts, env), cfg.queue_dir)
    ender = end_watch_from_env(env, speaker.tts)
    ender.start()
    sched = Scheduler(memory, persona.topics, persona.night_topics, night_offset_hours=cfg.night_offset_hours,
                      favorite_games=persona.favorite_games, talk_segments=persona.segments, schedule=persona.schedule)
    adapter = build_adapter(cfg)
    if cfg.click_mouth_dir and cfg.scene == "ffmpeg":
        # the stage plans the click clip and answers when the claw lands; the adapter sends the spin then
        adapter.click_cue = lambda why: (ask_stage(cfg.stage_url, {"type": "click", "why": why}) or {}).get("at")
    orch = Orchestrator(adapter, build_chat(cfg), brain, speaker, sched, ChatModerator(),
                        notify=lambda ev: post_stage(cfg.stage_url, ev),
                        heartbeat=lambda **kw: write_status(cfg.status_dir, "brain", **kw),
                        round_timeout=float(getattr(adapter, "round_timeout", 45.0)),
                        talk_pace=float(os.environ.get("HUBIE_TALK_PACE", "1") or 1),
                        fill_after=float(os.environ.get("HUBIE_FILL_AFTER", str(FILL_AFTER)) or 0),
                        talk_cap=float(os.environ.get("HUBIE_TALK_CAP", str(TALK_CAP)) or 0),
                        ender=ender, on_show_over=lambda **info: mark_show_over(cfg.show_over_flag, **info))
    try:
        orch.run()
    finally:
        ender.stop()
        memory.save(force=True)
        adapter.close()


if __name__ == "__main__":
    main()
