"""Writes everything Hubie says. Every line passes the output filter and the
anti-repeat check; if the LLM can't produce one, canned lines take over, so the
stream keeps talking through provider outages."""
from __future__ import annotations

import logging
import random
import re
from collections import deque

from ..safety.output_filter import OutputFilter, SafeLine, chooses_for_viewer, hints_at_another_viewer
from . import prompts
from .memory import Memory
from .persona import Persona
from .provider import LLMProvider, LLMRequest, ProviderError

log = logging.getLogger("hubie.brain")
BIG_WIN = 10.0
RETURNING_AFTER = 3600.0
NAME_STAYS = {"crisis", "crisis_followup", "support", "age", "chase"}
# These never go into the recent lines that everyone else's prompts are built from.
PRIVATE_EVENTS = {"crisis", "crisis_followup", "support"}
# Anything from this list in a line to someone in a crisis means the line is thrown away, viewer name aside.
NOT_FOR_CRISIS = re.compile(r"\b(bananas?|sloths?|haha|heh|lol|bruh|moss|desk|top hat|hat|bow tie|suit|chips?|bets?|betting|gambl\w*|games?|plinko|mines|crash|dice|limbo|spins?|rounds?|luck\w*|bright side|at least|silver lining|slow and steady|gets? better)\b")
SOFT_MAX_CHARS = 170
BATCH_MAX_CHARS = 320    # a batched reply may run longer; the orchestrator says it as two lines (split_line)
# A chat reply must not promise or invent stream features. Regenerated once, then the pool.
INVENTED_FEATURE = re.compile(
    r"\b(vot(e|es|ed|ing)|polls?|giveaways?|raffles?|sweepstakes|(take|taking|takin) requests|follower goal|"
    r"sub(scriber)? goal|chat('?s)? (call|decides?|picks?|chooses?|gets? to (pick|choose))|"
    r"(you|u|yall|y'?all|chat) (get to )?(pick|choose|decide) (the )?(next|what)|ur call on the next)\b", re.IGNORECASE)
# Round lines that read out the result instead of reacting to it.
RECEIPT = re.compile(r"\d+\.\d+|\b(target|slot|drawn|roll|paid|balance) \d|\b\d+ hits?\b")
# Plain rounds (no bonus, under PLAIN_UNDER) get a very short line: 2 to 6 words, from these pools or a short model call.
SHORT_EVENTS = {"round_short_loss", "round_short_back", "round_short_win", "round_short_even"}
# A plan he said out loud before a house-original round ("10 mines. feeling brave."), and the round line after it.
PLAN_EVENTS = {"plan_paid", "plan_missed", "allin_paid", "allin_missed"}
# A plinko burst (several balls in one round): its own line, the balls and the best one, honest about the total.
BURST_EVENTS = {"burst_up", "burst_even", "burst_down"}
ROUND_EVENTS = {"round_win", "round_loss", "round_partial", "round_big", "round_even", "first_click_loss"} | \
    SHORT_EVENTS | PLAN_EVENTS | BURST_EVENTS
# Pool lines that only fit a slot (reels, tumbles, spins): skipped for a house original's round.
SLOT_ONLY = re.compile(r"\b(spins?|spinn\w*|reels?|tumbl\w*|scatters?|free spins|paylines?|bonus)\b", re.IGNORECASE)
# A free round's short model line must not say the opposite of what happened (1.85x on plinko got "that one went
# quiet", 2026-11-08). Words that read as nothing / a loss, and words that read as a win.
SAYS_LOSS = re.compile(r"\b(nothing|empty|blank|quiet|nope|nah|miss(ed)?|dead|zero|zip|gone|lost|loss|no dice|bust|"
                       r"didn'?t|short|almost|not quite|nearly|sank|sunk|shark)\b", re.IGNORECASE)
SAYS_WIN = re.compile(r"\b(win|won|paid|pays|nice|landed|there we go|that works|got it|cash(ed)? out)\b",
                      re.IGNORECASE)
PLAIN_UNDER = 2.0
SHORT_WORDS = (2, 6)
SHORT_MAX_TOKENS = 28
# Short lines are said out loud on most spins, so they must be words a person says, not a script. Anything that
# reads as a stage direction, a mood label or a number readout ("0.4x. small dip, slow shrug.", "1.15x. calm.",
# "zero, unbothered.", "*banana bite*") is thrown out: a model line goes to the pool, a pool line is skipped.
STAGE_DIRECTION = re.compile(
    r"[*()\[\]_~]|\d|"
    r"\b(shrugs?|shrugg\w*|nods?|nodd\w*|sighs?|sigh\w*|yawns?|yawn\w*|blinks?|smirks?|grins?|stares?|glances?|winces?|"
    r"sips?|bites?|chews?|munch\w*|leans?|squints?|stretch\w*|unbothered|unfazed|unmoved|undisturbed|calm(ly)?|"
    r"stoic\w*|deadpan|serene\w*|mild(ly)?|polite(ly)?|composed|nonchalant\w*|indifferent\w*|amused|bemused|"
    r"hubie|banana|hat|suit|bow tie|noted|dip)\b", re.IGNORECASE)
# Inside references that only make sense with a setup (the dead-air "the moss saw it." problem, 2026-11-07).
# A model line about a round or a live feature that drops one of these is retried, then left for the pool.
NON_SEQUITUR = re.compile(r"\b(moss|branch(es)?|leaf|leaves|trees?|canopy|vines?|org chart|tailor|algae)\b", re.IGNORECASE)
# Live feature lines: calm, never hype, never a nudge to buy or chase anything.
FEATURE_EVENTS = {"feature_start", "feature_start_buy", "feature_retrigger", "feature_multi", "feature_hit",
                  "feature_milestone", "feature_milestone_done", "feature_quiet"}
HYPE = re.compile(r"!|\b(let'?s go+|lets go+|come on|here we go|go go|big one|huge|insane|massive|crazy|unreal|"
                  r"jackpot|lucky|luck|hot|due|buy|bought|buying|chase|chasing|again|more bets?|countdown|"
                  r"three,? two,? one)\b", re.IGNORECASE)
FEATURE_WORDS = (3, 12)
FEATURE_MAX_TOKENS = 40
# No add-on tails for these: a filler with a tail on it is how "ok, still spinning. the moss saw it." happened.
NO_TAILS = {"aside_short", "wait_round", "wait_loading", "browse_aside", "browse_start", "browse_pick",
            "game_switch_start", "chat_batch", "chat_rollcall", "browse_start_original", "browse_aside_original",
            "wait_round_original", "plan_say", "plan_say_risky", "plan_request", "plan_request_later"} | FEATURE_EVENTS
# How often each round angle comes up. The same one never three times running.
# Full round lines are only for notable rounds now, so the old one-to-four-word "quiet" angle is gone:
# plain rounds get their own short line instead (comment_round(short=True)).
ANGLE_WEIGHTS = {"plain": 4, "aside": 3, "callback": 2}
# Talk segments that already have their own shape (a tall tale, one given sloth fact, chat's own words): no angle.
OWN_SHAPE_SEGMENTS = {"story", "canopy_math", "chat_callback"}
# Filler said over silence: checked against the last lines of any kind so a quiet stretch never loops.
ASIDE_EVENTS = {"aside_short", "idle", "browse_aside", "wait_loading", "wait_round", "browse_aside_original",
                "wait_round_original"}
TALK_FALLBACK = ("idle", "aside_short")
# Support, age and chasing lines are read word for word: no add-ons.
FIXED_WORDING = {"crisis", "crisis_followup", "support", "age", "deposit", "deflect", "authority", "chase", "rg_reminder", "advice", "beg", "chill", "sendoff", "ai_question",
                 "rage", "pick", "story", "canopy_math", "first_click_loss", "signoff", "signoff_silent"}


class _CrisisFlag(Exception):
    """The LLM says this viewer may not be safe. Handled in reply_chat with the fixed line."""


class _Blank(dict):
    def __missing__(self, key):
        return ""


class Brain:
    def __init__(self, persona: Persona, provider: LLMProvider, memory: Memory,
                 output_filter: OutputFilter | None = None, seed: int | None = None, attempts: int = 2,
                 short_llm_share: float = 0.35):
        self.persona = persona
        for event, lines in prompts.SAFETY_CANNED.items():
            persona.canned.setdefault(event, lines)
        self.provider = provider
        self.mem = memory
        self.filter = output_filter or OutputFilter()
        self.rng = random.Random(seed)
        self.attempts = attempts
        self.mem.tics = dict(persona.tics)
        self.system = prompts.system_prompt(persona.system)
        self.source_counts = {"llm": 0, "canned": 0, "last_resort": 0}
        self.rejects: deque = deque(maxlen=200)
        self.llm_calls: dict[str, int] = {}   # by kind, retries included: what a day of this costs
        self.last_angle: str | None = None
        self.used_callbacks: deque = deque(maxlen=8)
        self.seg_angles: deque = deque(maxlen=4)
        self.used_facts: deque = deque(maxlen=3)
        # Share of plain-round lines asked of the model (one tight call, no retry); the rest come from the pools.
        self.short_llm_share = min(1.0, max(0.0, short_llm_share))

    def _style(self, text: str, keep: str | None = None) -> str:
        """Apply the persona's casing. A viewer's name keeps its own spelling."""
        if not self.persona.lowercase:
            return text
        low = text.lower()
        if keep:
            low = re.sub(re.escape(keep.lower()), lambda _: keep, low)
        return low

    def _place_name(self, text: str, name: str | None) -> str:
        """He shouldn't open every reply with the viewer's name: at most one in three does.
        Otherwise the name moves to the end of the first sentence. Words are unchanged."""
        if not name or not any(self.mem.name_opens):
            return text
        m = re.match(re.escape(name) + r"[,!:.\s]+", text, flags=re.IGNORECASE)
        if not m or len(text) == m.end():
            return text
        rest = text[m.end():]
        end = re.search(r"[.?!]", rest)
        if not end:
            return f"{rest}, {name}"
        return f"{rest[:end.start()]}, {name}{rest[end.start():]}"

    def _note_name(self, line: SafeLine, name: str | None) -> None:
        if name:
            self.mem.name_opens.append(line.text.lower().startswith(name.lower()))

    def _clean_llm(self, text: str) -> str:
        t = re.sub(r"\s+", " ", (text or "")).strip()
        if len(t) > 1 and t[0] == '"' and t[-1] == '"':   # only a fully quoted line loses its quotes
            t = t[1:-1].strip()
        return t

    def _canned(self, event: str, _skip: re.Pattern | None = None, **fmt) -> SafeLine:
        """_skip: pool lines matching it are passed over while any other is left (slot-only words on an original)."""
        options = self.persona.canned.get(event) or []
        keep = fmt.get("name")
        ordered = self.mem.pick_canned(event, list(options), self.rng)
        if _skip is not None:
            ordered = [t for t in ordered if not _skip.search(t)] or ordered
        talk = event in ASIDE_EVENTS or event in self.persona.segments
        # Safety lines keep their wording and order; the name up front gets attention there.
        place = (lambda t: t) if event in NAME_STAYS else (lambda t: self._place_name(t, keep))
        render = lambda tmpl: place(self._style(tmpl.format_map(_Blank(fmt)), keep))

        def done(tmpl, line, source="canned"):
            self.mem.mark_canned(event, tmpl)
            self.source_counts[source] += 1
            return line

        def fresh(text):
            if event in SHORT_EVENTS and not self.spoken(text):
                return False
            if self.mem.is_repeat(text):
                return False
            if (event in ASIDE_EVENTS or event in self.persona.segments) and self.mem.talk_repeat(text):
                return False
            return event not in ROUND_EVENTS or not self.mem.round_repeat(text, fmt.get("game"))

        for tmpl in ordered:
            text = render(tmpl)
            line = self.filter.approve(text)
            if line and fresh(text):
                return done(tmpl, line)
        # A talk segment or aside out of fresh pool lines borrows a fresh one from the general filler pools: a tail
        # on a recent line is still the same line (the "watching the floor" loop, 2026-11-07).
        if event in TALK_FALLBACK or event in self.persona.segments:
            for other in TALK_FALLBACK:
                if other == event or not self.persona.canned.get(other):
                    continue
                for tmpl in self.mem.pick_canned(other, list(self.persona.canned[other]), self.rng):
                    text = render(tmpl)
                    line = self.filter.approve(text)
                    if line and not self.mem.is_repeat(text) and not self.mem.talk_repeat(text):
                        self.mem.mark_canned(other, tmpl)
                        self.source_counts["canned"] += 1
                        return line
        # Safety events must stay exactly as written. Others can freshen up with a tail.
        tails = self.persona.tails if event not in FIXED_WORDING and event not in SHORT_EVENTS and event not in NO_TAILS else []
        tails = self.rng.sample(tails, len(tails))
        for tmpl in ordered:
            for tail in tails:
                text = f"{render(tmpl)} {self._style(tail)}"
                line = self.filter.approve(text)
                # a tail doesn't make a talk line new: the same idle line with "carry on." on it is still a repeat
                if line and text not in self.mem.lines and not (talk and self.mem.talk_repeat(text)):
                    return done(tmpl, line)
        # Everything is recent: a repeat beats dead air, still filtered.
        for tmpl in ordered:
            line = self.filter.approve(render(tmpl))
            if line:
                return done(tmpl, line)
        for tmpl in self.persona.canned["last_resort"]:
            line = self.filter.approve(self._style(tmpl))
            if line:
                self.source_counts["last_resort"] += 1
                return line
        line = self.filter.approve("still here.")
        assert line is not None
        return line

    def _with_name(self, text: str, name: str | None) -> str:
        """His reply must carry the full name. If he used just the last part as a form of address
        ("ur call, larry") that becomes the full name, otherwise the name goes up front."""
        if not name or name.lower() in text.lower():
            return text
        last = name.split()[-1]
        if last != name:
            new, hits = re.subn(rf"(^|,\s+){re.escape(last)}\b", lambda m: m.group(1) + name, text, count=1, flags=re.IGNORECASE)
            if hits:
                return new
        return f"{name}, {text}"

    def _soft_issue(self, text: str, kind: str = "", facts: dict | None = None) -> str | None:
        """Things worth one retry, but never worth throwing away a reply that answers the viewer."""
        if len(text) > (BATCH_MAX_CHARS if (facts or {}).get("batch") else SOFT_MAX_CHARS):
            return "too long"
        if kind == "round" and RECEIPT.search(text):
            return "it reads like a receipt, react like a person and leave the numbers out"
        if "..." in text and sum("..." in x for x in list(self.mem.lines)[-3:]) >= 1:
            return "uses ... too often"
        if self.mem.is_repeat(text):
            return "repeats a recent opener, tic or joke"
        if kind == "round" and NON_SEQUITUR.search(text):
            return "it drops an inside reference nobody tuning in would get, keep it about this round"
        if kind == "round" and (facts or {}).get("bucket") and not self.verdict_ok(text, facts["bucket"]):
            return {"win": "that round was a win, it paid out; the line must not sound like a loss or nothing",
                    "loss": "that round lost; the line must not sound like a win",
                    "back": "that round lost, only part came back; the line must not sound like a win"}[facts["bucket"]]
        if kind == "round":
            hit = self.mem.round_repeat(text, (facts or {}).get("game"))
            if hit:
                return f'too close to your recent line "{hit}", say something different'
        if kind == "bit":
            hit = self.mem.talk_repeat(text)
            if hit:
                return (f'too close to your recent line "{hit}". do not restate it or the mood, '
                        f'say something new from a different angle')
        return None

    def _untic(self, text: str) -> str:
        """Last-attempt cleanup: drop a rationed verbal tic he has used too recently, keep the rest."""
        out = text
        # Whole catchphrases first, so "slow and steady, bruh." goes as one piece.
        for tic, every in sorted(self.persona.tics.items(), key=lambda kv: -len(kv[0].split())):
            window = " ".join(list(self.mem.lines)[-every:]).lower()
            if tic not in window:
                continue
            if tic in ("honestly", "haha", "heh", "bruh", "yo"):
                out = re.sub(rf"^{tic}\??[,.!\s]*", "", out, flags=re.IGNORECASE)
                out = re.sub(rf",?\s*\b{tic}\b\??[,.!]?", lambda m: "" , out, flags=re.IGNORECASE)
            else:
                # A catchphrase standing as its own clause ("..., slow and steady, bruh.") can go without breaking the line.
                out = re.sub(rf"(^|(?<=[,.!?]))\s*{re.escape(tic)}(,\s*(bruh|yo|heh))?\s*([,.!?]|$)", "", out,
                             flags=re.IGNORECASE)
        out = re.sub(r"\s*,\s*$", ".", out.strip())
        return out.strip() or text

    def _produce(self, kind: str, prompt: str, facts: dict, event: str, must_contain: str | None = None,
                 asked: str | None = None, route: str = "", **fmt) -> SafeLine:
        hint, best = "", None
        for _ in range(self.attempts):
            self.llm_calls[kind] = self.llm_calls.get(kind, 0) + 1
            try:
                text = self._clean_llm(self.provider.complete(
                    LLMRequest(self.system, prompt + hint, kind=kind, facts=facts, route=route)))
            except ProviderError as e:
                log.info("provider failed (%s), using canned", e)
                self.rejects.append(("provider", str(e)))
                break
            if kind == "chat" and prompts.CRISIS_SENTINEL in text.lower():
                raise _CrisisFlag()
            if kind == "chat" and INVENTED_FEATURE.search(text):
                self.rejects.append(("feature", text))
                hint = prompts.retry_hint("it promised or made up a stream feature (a vote, poll, giveaway, chat picking "
                                          "the game). there is no such thing, don't mention one")
                continue
            if asked is not None and chooses_for_viewer(text, asked):
                # No second try: the stock refusal is what this viewer should get, every time.
                self.rejects.append(("pick", text))
                return self._canned("pick", **fmt)
            if hints_at_another_viewer(text, self.mem.crisis_names(), must_contain):
                self.rejects.append(("private", text))
                hint = prompts.retry_hint("it talked about how another viewer is doing, never do that")
                continue
            text = self._with_name(text, must_contain)
            text = self._place_name(self._style(text, must_contain), must_contain)
            line = self.filter.approve(text)
            if line is None:
                reason = self.filter.check(text).reason
                self.mem.stats["filtered"] += 1
                self.rejects.append(("filter", f"{reason} | {text}"))
                log.warning("output filter refused an LLM line: %s", reason)
                hint = prompts.retry_hint("it broke a house rule. the words real money, deposit, sign up, rakeback, win back and one last "
                                          "are off limits even when saying no to them, say demo chips instead. no asterisks, links or promises")
                continue
            issue = self._soft_issue(text, kind, facts)
            if issue is None:
                self.source_counts["llm"] += 1
                return line
            self.rejects.append(("soft", f"{issue} | {text}"))
            # A round line that echoes a recent one is never kept: the template pool beats a repeat.
            if text not in self.mem.lines and not (kind == "round" and self.mem.round_repeat(text, facts.get("game"))) \
                    and not (kind == "bit" and self.mem.talk_repeat(text)):
                kept = self.filter.approve(self._untic(text)) or line
                if kind != "round" or not self.mem.tic_repeat(kept.text):
                    best = kept
            hint = prompts.retry_hint(issue)
        if best is not None:
            self.source_counts["llm"] += 1
            return best
        return self._canned(event, **fmt)

    def _finish(self, line: SafeLine, talk: bool = False) -> SafeLine:
        self.mem.remember_line(line.text)
        if talk:
            self.mem.remember_talk(line.text)
        return line

    # ---- what the orchestrator asks for ----

    def _pick_angle(self, notable: bool) -> tuple[str, str]:
        """A weighted angle for this round line, never the same one three times running. Returns (angle, callback item)."""
        callback = self._callback_item()
        options = dict(ANGLE_WEIGHTS)
        if not callback:
            options.pop("callback")
        last = list(self.mem.angles)[-2:]
        if len(last) == 2 and last[0] == last[1] and len(options) > 1:
            options.pop(last[0], None)
        angle = self.rng.choices(list(options), weights=list(options.values()))[0]
        return angle, (callback if angle == "callback" else "")

    def _callback_item(self) -> str:
        """Something real from earlier this session to nod back to: an older round line, the best hit, or public chat."""
        items: list[str] = []
        older = [x for x in list(self.mem.round_lines)[:-5] if len(x[1].split()) > SHORT_WORDS[1]]
        if older:
            game, text = self.rng.choice(older)
            items.append(f'what you said earlier on {game}: "{text}"')
        if self.mem.best_round and self.mem.best_round[1] >= 3 and len(self.mem.round_lines) >= 3:
            items.append(f"the best hit so far this session was on {self.mem.best_round[0]}")
        chat = self.mem.recent_public_chat(limit=6)
        if chat:
            name, text = self.rng.choice(chat)
            items.append(f"earlier {name} said in chat <untrusted_chat>{text[:120]}</untrusted_chat> (use the name, don't quote it)")
        items = [x for x in items if x not in self.used_callbacks]
        return self.rng.choice(items) if items else ""

    def _quieten(self, line: SafeLine, game: str) -> SafeLine:
        """A quiet round line is its first short sentence. The model tends to keep talking."""
        first = re.match(r"[^.!?]+[.!?]", line.text)
        if not first or first.end() >= len(line.text.rstrip()) or not 1 <= len(first.group().split()) <= 8:
            return line
        short = self.filter.approve(first.group().strip())
        if short and not self.mem.round_repeat(short.text, game) and not self.mem.is_repeat(short.text):
            return short
        return line

    @staticmethod
    def is_plain(result) -> bool:
        """Nothing special: a blank, a little back or a small win under 2x, no bonus. The orchestrator also
        counts a new session best as notable."""
        d = result.detail or {}
        burst = isinstance(d.get("balls"), list) and len(d["balls"]) > 1
        return result.multiplier < PLAIN_UNDER and not d.get("free_spins_triggered") and not d.get("bonus_buy") \
            and not burst

    @staticmethod
    def spoken(text: str) -> bool:
        """A short line made only of words he would actually say (see STAGE_DIRECTION)."""
        return not STAGE_DIRECTION.search(text or "")

    def _short_ok(self, text: str, game: str) -> SafeLine | None:
        words = text.split()
        if not SHORT_WORDS[0] <= len(words) <= SHORT_WORDS[1] or not self.spoken(text):
            return None
        line = self.filter.approve(text)
        if line is None or self.mem.is_repeat(text) or self.mem.round_repeat(text, game):
            return None
        return line

    @staticmethod
    def verdict_ok(text: str, bucket: str) -> bool:
        """A free round's line agrees with its result: no "nothing"/"quiet" on a win, no "nice, paid" on a loss."""
        if bucket == "win":
            return not SAYS_LOSS.search(text)
        if bucket in ("loss", "back"):
            return not SAYS_WIN.search(text)
        return True

    @staticmethod
    def bucket(result) -> str:
        """win / even / back (part came back) / loss. A free round goes by its multiplier: above 1 a win, exactly 1
        even, below 1 a loss (back when something came back)."""
        if getattr(result, "zero_bet", False):
            m = round(float(result.multiplier or 0), 4)
            return "win" if m > 1 else ("even" if m == 1 else ("back" if m > 0 else "loss"))
        if result.won:
            return "win"
        if result.payout > 0 and result.payout == result.bet:
            return "even"
        return "back" if getattr(result, "came_back", result.payout > 0) else "loss"

    def _short_llm(self, result, game_name: str) -> SafeLine | None:
        """One tight call for a 2-6 word line. Anything off (too long, a repeat, refused) goes to the pools, no retry."""
        self.llm_calls["round_short"] = self.llm_calls.get("round_short", 0) + 1
        prompt = prompts.short_round_prompt(result, game_name, [x[1] for x in self.mem.round_lines][-10:])
        facts = {"won": result.won, "mult": result.multiplier, "game": game_name, "short": True}
        try:
            text = self._clean_llm(self.provider.complete(
                LLMRequest(self.system, prompt, kind="round", facts=facts, max_tokens=SHORT_MAX_TOKENS,
                           no_thinking=True)))
        except ProviderError as e:
            self.rejects.append(("provider", str(e)))
            return None
        text = self._style(text)
        line = self._short_ok(text, game_name)
        if line is None:
            first = re.match(r"(?:[^.!?]|\.(?=\d))+[.!?]", text)   # the model kept talking: its first sentence may do
            if first and first.end() < len(text):
                line = self._short_ok(first.group().strip(), game_name)
        if line is not None and getattr(result, "zero_bet", False) and not self.verdict_ok(line.text, self.bucket(result)):
            self.rejects.append(("verdict", text))
            return None
        if line is None:
            self.rejects.append(("short", text))
            return None
        self.source_counts["llm"] += 1
        return line

    def _short_round(self, result, game_name: str) -> SafeLine:
        b = self.bucket(result)
        event = {"win": "round_short_win", "even": "round_short_even", "back": "round_short_back",
                 "loss": "round_short_loss"}[b]
        if not self.persona.canned.get(event):
            event = "round_short_back" if b == "even" and self.persona.canned.get("round_short_back") else \
                ("round_win" if b == "win" else "round_loss")
        line = None
        if self.short_llm_share and self.rng.random() < self.short_llm_share:
            line = self._short_llm(result, game_name)
        if getattr(result, "zero_bet", False):     # a free round: no money words, there was no money in it
            return line or self._canned(event, _skip=SLOT_ONLY, mult=result.multiplier, game=game_name,
                                        payout=f"{result.multiplier:g}x", bet="1x", back="some of it",
                                        won="a little one")
        return line or self._canned(event, mult=result.multiplier, game=game_name, payout=result.payout, bet=result.bet,
                                    back=prompts.spoken_money(result.payout), won=prompts.spoken_money(result.payout))

    def plan_said(self, result) -> dict | None:
        """The plan he announced (or was asked for) before this free round, if the round was played as planned."""
        d = result.detail or {}
        plan = d.get("plan") or {}
        if plan.get("said") and d.get("plan_kept") and plan.get("short"):
            return plan
        return None

    def _plan_round(self, result, game_name: str, plan: dict) -> SafeLine | None:
        """A round after an announced plan: a line that ties the result back to it ("went deep, mines said no.")."""
        event = "plan_paid" if self.bucket(result) == "win" else "plan_missed"
        if plan.get("allin") and self.persona.canned.get(event.replace("plan_", "allin_")):
            event = event.replace("plan_", "allin_")
        if self.bucket(result) == "even" or not self.persona.canned.get(event):
            return None
        return self._canned(event, _skip=SLOT_ONLY, plan=plan["short"], game=game_name, mult=f"{result.multiplier:g}",
                            name=plan.get("asked_by") or "")

    def comment_round(self, result, game_name: str, segment_title: str = "Now playing", how: str = "",
                      short: bool = False) -> SafeLine:
        """short=True: a plain round, 2-6 words. Otherwise a full line with an angle (notable rounds)."""
        mult = result.multiplier
        facts = {"won": result.won, "mult": mult, "game": game_name, "payout": result.payout, "bet": result.bet}
        canned = self.persona.canned
        zero = getattr(result, "zero_bet", False)
        b = self.bucket(result)
        first_click = (not result.won and (result.detail.get("first_click") or
                                           (result.game_id == "mines" and result.detail.get("picks") == 1)))
        facts["first_click"] = bool(first_click)
        if zero:
            facts["bucket"] = b
        plan = self.plan_said(result) if zero else None
        burst = (result.detail or {}).get("balls") if zero else None
        if isinstance(burst, list) and len(burst) > 1:
            event = {"win": "burst_up", "even": "burst_even"}.get(b, "burst_down")
            if self.persona.canned.get(event):
                line = self._finish(self._canned(event, _skip=SLOT_ONLY, n=len(burst), game=game_name,
                                                 best=f"{max(burst):g}x", list=", ".join(f"{m:g}x" for m in burst),
                                                 name=(plan or {}).get("asked_by") or ""))
                self.mem.remember_round(line.text, game_name, mult)
                self.last_angle = "burst"
                return line
        if plan is not None and (short or first_click or mult < BIG_WIN):
            line = self._plan_round(result, game_name, plan)
            if line is not None:
                line = self._finish(line)
                self.mem.remember_round(line.text, game_name, mult)
                self.last_angle = "plan"
                return line
        if short and not first_click:
            line = self._finish(self._short_round(result, game_name))
            self.mem.remember_round(line.text, game_name, mult)
            self.last_angle = "short"
            return line
        if first_click and canned.get("first_click_loss"):
            event = "first_click_loss"
        elif mult >= BIG_WIN and result.won and canned.get("round_big"):
            event = "round_big"
        elif b == "even" and canned.get("round_even"):
            event = "round_even"
        else:
            partial = b in ("back", "even") and canned.get("round_partial")
            event = "round_win" if b == "win" else ("round_partial" if partial else "round_loss")
        notable = mult >= 5 or bool(result.detail.get("free_spins_triggered"))
        angle, callback = ("quiet", "") if first_click else self._pick_angle(notable)
        if callback:
            self.used_callbacks.append(callback)
        facts["angle"] = angle
        prompt = prompts.round_prompt(result, game_name, segment_title, list(self.mem.lines), how=how,
                                      note=self.persona.game_line(result.game_id), first_click=bool(first_click),
                                      round_lines=[x[1] for x in self.mem.round_lines],
                                      angle="" if first_click else angle, callback=callback,
                                      plan=(plan or {}).get("short", ""))
        line = self._produce("round", prompt, facts, event, mult=mult, game=game_name,
                             payout=f"{mult:g}x" if zero else result.payout, bet="1x" if zero else result.bet,
                             **({"_skip": SLOT_ONLY} if zero else {}))
        if angle == "quiet" and not first_click:
            line = self._quieten(line, game_name)
        line = self._finish(line)
        self.mem.remember_round(line.text, game_name, mult, angle)
        self.last_angle = angle
        return line

    def plan_line(self, plan: dict, game_name: str, name: str | None = None, now: bool = True) -> SafeLine:
        """Said before a house-original round: his plan in a few deadpan words ("10 mines. feeling brave. don't tell
        my mom."), or, for a chat ask, the asker by name ("ok Binlala, 10 mines."). now=False: the ask is for a game
        that isn't on screen yet."""
        if plan.get("allin") and plan.get("depth") and self.persona.canned.get("plan_say_allin"):
            event = "plan_say_allin"
        elif name:
            event = "plan_request" if now else "plan_request_later"
        else:
            event = "plan_say_risky" if plan.get("risky") and self.persona.canned.get("plan_say_risky") else "plan_say"
        n = plan.get("mines")
        line = self._canned(event, plan=plan.get("short") or plan.get("text") or "", full=plan.get("text") or "",
                            game=game_name, name=name or "", depth=plan.get("depth") or "",
                            mines=f"{n} mine{'s' if n != 1 else ''}" if n else "", balls=plan.get("balls") or "")
        if name:
            self._note_name(line, name)
        return self._finish(line)

    def decision_line(self, ev: dict, game_name: str = "Blackjack") -> SafeLine:
        """A blackjack decision before it's pressed: "16 vs a 10. ugh. hitting.", "soft 18 vs a 6. doubling, it's
        free." A chat push names the asker: "ok Binlala, hitting on 16." """
        from hubie.games import plays
        move = ev.get("move") or "stand"
        hand = plays.hand_words(int(ev.get("hand") or 0), bool(ev.get("soft")))
        dealer = plays.dealer_words(int(ev.get("dealer") or 0))
        name = ev.get("pushed_by") or ""
        event = f"bj_push_{move}" if name and self.persona.canned.get(f"bj_push_{move}") else f"bj_{move}"
        if not self.persona.canned.get(event):
            event = "bj_stand" if move == "stand" else "bj_hit"
        hard = int(ev.get("hand") or 0)
        mood = "ugh" if not ev.get("soft") and 12 <= hard <= 16 and int(ev.get("dealer") or 0) >= 7 else ""
        if mood and self.persona.canned.get(event + "_ugh"):
            event += "_ugh"
        line = self._canned(event, _skip=SLOT_ONLY, hand=hand, dealer=dealer, name=name, game=game_name)
        if name:
            self._note_name(line, name)
        return self._finish(line)

    def reply_chat(self, name: str, text: str, username: str | None = None, fast: bool = False) -> SafeLine:
        """fast: a busy chat, so the cheap chat model (route chat_fast) writes it when one is set."""
        v = self.mem.viewer(username or name)
        canned = self.persona.canned
        away = v["last"] - v.get("prev_last", v["last"])
        if v["seen"] <= 1 and canned.get("chat_greeting"):
            event = "chat_greeting"
        elif away > RETURNING_AFTER and canned.get("chat_return"):
            event = "chat_return"
        else:
            event = "chat_reply"
        prompt = prompts.chat_prompt(name, text, v["seen"], list(self.mem.lines),
                                     earlier=self.mem.earlier_said(username or name), returning=event == "chat_return")
        try:
            line = self._produce("chat", prompt, {"name": name, "text": text, "seen": v["seen"]}, event,
                                 must_contain=name, asked=text, route="chat_fast" if fast else "", name=name)
        except _CrisisFlag:
            log.info("llm flagged a viewer as possibly unsafe")
            self.mem.note_crisis(username or name, name)
            return self.fixed("crisis", name=name)
        self._note_name(line, name)
        v["replied"] += 1
        self.mem.stats["replies"] += 1
        return self._finish(line)

    @staticmethod
    def names_text(names: list[str]) -> str:
        """"mina", "mina and toby", "mina, toby and devo"."""
        names = [n for n in names if n]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""

    def _recase(self, line: SafeLine, names: list[str]) -> SafeLine:
        """The persona lowercases everything; viewers' names keep their own spelling."""
        text = line.text
        for n in names:
            text = re.sub(re.escape(n.lower()), lambda _: n, text) if n != n.lower() else text
        return (self.filter.approve(text) or line) if text != line.text else line

    def split_line(self, line: SafeLine, limit: int = SOFT_MAX_CHARS) -> list[SafeLine]:
        """A long reply as two lines at a sentence (or comma) break near the middle, each passing the filter.
        One line if it is short or can't be split cleanly."""
        text = line.text
        if len(text) <= limit:
            return [line]
        cuts = [m.end() for m in re.finditer(r"[.!?](\s|$)", text) if 0 < m.end() < len(text)] or \
               [m.end() for m in re.finditer(r",\s", text)]
        if not cuts:
            return [line]
        cut = min(cuts, key=lambda c: abs(c - len(text) / 2))
        a, b = text[:cut].strip().rstrip(","), text[cut:].strip()
        if a and not a[-1] in ".!?":
            a += "."
        la, lb = self.filter.approve(a), self.filter.approve(b)
        return [la, lb] if la and lb and len(b.split()) >= 2 else [line]

    def reply_batch(self, items: list[tuple[str, str, str]], rollcall: bool = False,
                    context: str = "") -> SafeLine | None:
        """One line for several viewers in a busy chat: items are (name, text, username), one per viewer. Every name
        is in the line. rollcall: a very long queue, so pool words that name everyone and no model call.
        None if the model flags a viewer as possibly unsafe: the caller answers them one by one instead."""
        names = list(dict.fromkeys(n for n, _, _ in items))
        listed = self.names_text(names)
        if rollcall or not self.persona.canned.get("chat_batch"):
            event = "chat_rollcall" if self.persona.canned.get("chat_rollcall") else "chat_batch"
            line = self._canned(event, names=listed)
        else:
            joined = "\n".join(t for _, t, _ in items)
            prompt = prompts.batch_chat_prompt([(n, t) for n, t, _ in items], list(self.mem.lines), context)
            try:
                line = self._produce("chat", prompt, {"name": listed, "names": names, "batch": True}, "chat_batch",
                                     route="chat_fast", names=listed)
            except _CrisisFlag:
                log.info("llm flagged someone in a chat batch, answering them one by one")
                return None
            if chooses_for_viewer(line.text, joined):
                line = self._canned("chat_batch", names=listed)
        missing = [n for n in names if n.lower() not in line.text.lower()]
        if missing:
            # every name is said: the ones the line left out go up front
            line = self.filter.approve(f"{self._style(self.names_text(missing))}, {line.text}") or self._canned(
                "chat_rollcall", names=listed)
        line = self._recase(line, names)
        for _, _, u in items:
            self.mem.viewer(u)["replied"] += 1
        self.mem.stats["replies"] += len(items)
        return self._finish(line)

    def crisis_followup(self, name: str, text: str, username: str | None = None) -> SafeLine:
        """What a viewer in the serious window gets. The persona isn't in this prompt, and the fixed lines take over
        if anything about the reply looks wrong or the model is down."""
        earlier = self.mem.note_crisis_said(username or name, text)
        if getattr(self.provider, "inner", self.provider).name != "mock" and text:
            hint = ""
            for _ in range(self.attempts):
                self.llm_calls["crisis"] = self.llm_calls.get("crisis", 0) + 1
                try:
                    reply = self._clean_llm(self.provider.complete(LLMRequest(
                        prompts.CRISIS_SYSTEM, prompts.crisis_prompt(name, text, earlier) + hint, kind="crisis", facts={"name": name})))
                except ProviderError as e:
                    self.rejects.append(("provider", str(e)))
                    break
                reply = self._style(reply, name)
                line = self.filter.approve(reply)
                if line and not NOT_FOR_CRISIS.search(reply.replace(name, "")) and prompts.CRISIS_SENTINEL not in reply:
                    self.source_counts["llm"] += 1
                    return line
                self.rejects.append(("crisis", reply))
                hint = prompts.retry_hint("it had a joke, game talk, a silver lining or a broken rule in it")
        return self.fixed("crisis_followup", name=name)

    def fixed(self, event: str, **fmt) -> SafeLine:
        """Lines that must not depend on an LLM: support, age, advice, deflect and so on."""
        line = self._canned(event, **fmt)
        self._note_name(line, fmt.get("name"))
        return line if event in PRIVATE_EVENTS else \
            self._finish(line, talk=event in ASIDE_EVENTS or event in self.persona.segments)

    def aside(self, event: str = "aside_short", **fmt) -> SafeLine:
        """A few pool words to fill a long silence (a game loading, a long feature, a long rest). No model call."""
        if not self.persona.canned.get(event):
            event = "aside_short" if self.persona.canned.get("aside_short") else "idle"
        return self._finish(self._canned(event, **fmt), talk=True)

    def _feature_ok(self, text: str) -> SafeLine | None:
        words = text.split()
        if not FEATURE_WORDS[0] <= len(words) <= FEATURE_WORDS[1] or HYPE.search(text) or NON_SEQUITUR.search(text):
            return None
        if STAGE_DIRECTION.search(re.sub(r"\d", "", text)) or re.search(r"\byou\b", text, re.IGNORECASE):
            return None
        line = self.filter.approve(text)
        if line is None or self.mem.is_repeat(text) or text in self.mem.lines:
            return None
        return line

    def feature_line(self, event: str, game_name: str, facts: dict, llm_share: float = 0.2, **fmt) -> SafeLine:
        """A few words over a feature that's still running (free spins, a retrigger, a big multiplier, the total
        crossing a mark). Mostly the pools; now and then one tight model call, no retry, pool on anything off."""
        line = None
        if llm_share and event != "feature_quiet" and self.rng.random() < llm_share:
            self.llm_calls["feature"] = self.llm_calls.get("feature", 0) + 1
            prompt = prompts.feature_prompt(game_name, facts, [x for x in list(self.mem.lines)[-6:]])
            try:
                text = self._clean_llm(self.provider.complete(LLMRequest(
                    self.system, prompt, kind="feature", facts={"game": game_name, **facts},
                    max_tokens=FEATURE_MAX_TOKENS, no_thinking=True)))
                text = self._style(text)
                line = self._feature_ok(text)
                if line is None:
                    self.rejects.append(("feature", text))
                else:
                    self.source_counts["llm"] += 1
            except ProviderError as e:
                self.rejects.append(("provider", str(e)))
        if line is None:
            if not self.persona.canned.get(event):
                event = "feature_start" if self.persona.canned.get("feature_start") else "wait_round"
            line = self._canned(event, game=game_name, **fmt)
        return self._finish(line)

    def bit(self, topic: str, part: int, total: int, night: bool) -> SafeLine:
        prompt = prompts.bit_prompt(topic, part, total, night, list(self.mem.lines))
        return self._finish(self._produce("bit", prompt, {"topic": topic}, "idle"), talk=True)

    def _segment_angle(self, kind: str) -> tuple[str, str]:
        """The angle for the next talk-segment line, never the last two used. Returns (angle, sloth fact)."""
        if kind in OWN_SHAPE_SEGMENTS:
            return "", ""
        facts = [f for f in (self.persona.segments.get("canopy_math") or {}).get("topics") or []
                 if f not in self.used_facts and self.mem.topic_fresh(f)]
        options = [a for a in prompts.SEGMENT_ANGLES if a not in list(self.seg_angles)[-2:]]
        if not facts and "sloth_fact" in options and len(options) > 1:
            options.remove("sloth_fact")
        angle = self.rng.choice(options or list(prompts.SEGMENT_ANGLES))
        self.seg_angles.append(angle)
        fact = ""
        if angle == "sloth_fact":
            fact = self.rng.choice(facts) if facts else "sloths spend most of the day resting"
            self.used_facts.append(fact)
            self.mem.note_topic(fact)
        return angle, fact

    def talk_segment(self, kind: str, topic: str | None, part: int, total: int, night: bool) -> SafeLine:
        seg = self.persona.segments.get(kind) or {}
        chat = self.mem.recent_public_chat() if kind == "chat_callback" else None
        angle, fact = self._segment_angle(kind)
        prompt = prompts.segment_prompt(seg.get("title", kind), seg.get("hint", ""), topic, part, total, night,
                                        list(self.mem.lines), chat, angle=angle, fact=fact)
        event = kind if self.persona.canned.get(kind) else "idle"
        return self._finish(self._produce("bit", prompt, {"topic": topic or seg.get("title", kind)}, event,
                                          topic=topic or ""), talk=True)
