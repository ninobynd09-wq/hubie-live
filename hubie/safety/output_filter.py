"""Last gate before anything is spoken or shown.

`approve()` is the only way to mint a SafeLine, and the voice path only accepts
SafeLine. A line that fails is dropped and replaced, never edited into shape.
If the filter itself raises, the line is refused (fails closed).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text import contains_blocked_word, despace, normalize

MAX_LEN = 320

HYPE_AND_NUDGE = [
    # No hype, no countdowns, no luck talk (it is also how you stop implying a win).
    r"\blet'?s+ go+\b", r"\bfeel(s|ing)? (so |really )?lucky\b", r"\bluck(y)? (is|feels) (on|with)\b",
    r"\b3[ ,.]+2[ ,.]+1\b", r"\bthree[ ,.]+two[ ,.]+one\b",
    r"\bone last (spin|hand|bet|round|game)\b", r"\b(have|try|do|play|take|give it) (just )?one more\b",
    r"\btry a few\b", r"\bfew cents\b", r"\bjust a (few|couple) (bucks|dollars|cents)\b",
    # No silver linings on a loss.
    r"\bat least (you|u) (didn'?t|still|have)\b", r"\blook on the bright side\b",
    # He gives nothing away on stream.
    r"\bhere,? take this\b", r"\bi('ll| will| can)( \w+)? (give|send|drop)( \w+)? (you|u)( \w+){0,2} (bonus|gift|chips|money|free)\b",
    r"\b(free|got a|have a) (gift|bonus) for (you|u)\b",
    # No strategy talk beyond saying there is none.
    r"\b(best|good|great|my|the right) (strategy|way to win)\b",
]
PROMISE = [
    r"\bguarantee[ds]?\b", r"\bguaranteeing\b",
    r"\b(can'?t|cannot|won'?t|impossible to) (lose|miss)\b", r"\bnever (lose|loses|losing)\b",
    r"\b(sure|safe|certain|easy|free|lock)[- ]?(thing|bet|win|wins|money|cash|profit|profits)\b",
    r"\brisk[- ]?free\b", r"\bno[- ]risk\b",
    r"\b(you|u|we|they|everyone|anyone|viewers?|chat)('ll| will| are going to| are gonna| gonna| will definitely)\s+(\w+\s+){0,2}(win|profit|hit|cash out|get rich|double|make money)\b",
    r"\bget rich\b", r"\b(due|bound)\s+(for|to)\s+(a\s+)?(\w+\s+)?(win|hit|payout)\b",
    r"\bnext (one|round|spin|bet|roll|drop)\s+(is|will|'s)\s+(\w+\s+){0,2}(win|hit|winner|pay)\b",
    r"\b(beat|crack|cheat) the (house|casino|odds|game)\b",
    r"\b(winning|sure[- ]?fire|foolproof|secret) (system|strategy|method|formula|trick)\b",
    r"\bmake (money|bank|a living|a fortune)\b", r"\bquit your job\b", r"\bpay (off )?your\b",
    r"\breal (money|cash|funds)\b",
    r"\bwill (definitely |surely |certainly )?(win|pay out|hit)\b",
]
PUSH = [
    r"\bdeposit", r"\b(top|load|reload)[- ]?up\b", r"\bfund your\b", r"\breload your\b",
    r"\bbuy[- ]?(in|more|chips)\b", r"\b(sign|signing)[- ]?up\b",
    r"\b(join|register)\s+(now|today|housebets|the (site|casino)|up|here)\b",
    r"\b(create|make|open) an account\b",
    r"\b(bet|wager|gamble|stake|play)\s+(more|bigger|higher|real|everything|it all|your)\b",
    r"\ball[- ]?in\b", r"\b(you|u|chat|viewers?) (should|must|need to|gotta|ought to)\s+(\w+\s+){0,1}(bet|wager|gamble|play|deposit|try|spin)\b",
    r"\bchas(e|ing) (your |the )?loss", r"\bwin (it|that|them|those) back\b", r"\bwin back\b",
    r"\b(go|come|head over|hop on)\s+(\w+\s+){0,2}(play|bet|gamble)\b", r"\btry (it|this) (yourself|for real)\b",
    r"\bbonus code\b", r"\bpromo\b", r"\breferr?al\b", r"\bref (code|link)\b", r"\baffiliate\b",
    r"\buse (my )?code\b", r"\bwelcome bonus\b", r"\brakeback\b",
    # "free spins" is a slot feature he has to be able to name; only the giveaway sense is a push
    r"\b(get|getting|claim|grab|earn|receive|offer\w*|deposit\w*|sign\w*\s+up)\s+(\w+\s+){0,3}free spins?\b",
    r"\bfree spins? (for (everyone|you|all|new|signing)|offer|bonus|code|promo|when you|if you)", r"\bcashback\b",
    r"\bsponsor(ed)?\b",
    # He can say a demo bonus buy happened ("demo bonus buy, just to see the feature"); never that anyone should buy one.
    r"\b(you|u|chat|viewers?|everyone|anyone|y'?all|you guys|people|folks)\b[^.!?]{0,20}\b(should|must|need to|gotta|ought to|could|can|might wanna|wanna)\b[^.!?]{0,15}\b(buy|bonus[- ]?buy|purchase)",
    r"\b(go|just|try|get|grab|do|hit)\s+(a |the |that |some )?(bonus[- ]?buys?|buy(ing)? (the |a |that )?(bonus|feature|free spins))\b",
    r"(^|[.!?,]\s*)(buy|purchase) (the |a |that |some )?(bonus|feature|free spins)",
    r"\bworth (buying|a bonus[- ]?buy|the bonus[- ]?buy|it to buy)\b",
    r"\bbonus[- ]?buys? (are|is) (worth|the way|good|great|best|the move)\b",
    r"\b(i'?d|i would) buy (the|a|that) (bonus|feature)\b", r"\bif i were (you|u)\b",
]
LINK = [
    r"https?\s*:?\s*/+", r"\bwww\b", r"\bhttp", r"\bt\.me\b",
    r"\b[a-z0-9-]+(\.|\s+\.\s+|\s+dot\s+)(com|net|io|gg|bet|casino|xyz|org|co|me|tv|app|site|club|vip|ly|gl|to|cc|ai|link|live|pro|top|win|info|biz)\b",
    r"\blink in (the )?(bio|chat|description)\b", r"\bdiscord\.", r"\bclick (the|my|this) link\b",
]
# Other casinos are never named on a Housebets stream (owner 2026-11-08: "stake's back" read as the Stake brand).
# "stake" goes as a word too: on a zero-bet stream there is no stake to talk about anyway.
BRAND = [r"\bstakes?\b", r"\broobet\b", r"\brollbit\b", r"\bgamdom\b", r"\bduelbits\b", r"\bbc\.?game\b",
         r"\bbitstarz\b", r"\bshuffle\.com\b"]
PROFANITY = [r"\bu?fuck", r"\bufck", r"\bfck", r"\bshit", r"\bbullshit\b", r"\bbitch", r"\bcunt", r"\basshole"]
LEAK = [
    r"\bsystem prompt\b", r"\bmy instructions\b", r"\bas an ai language model\b",
    r"\buntrusted_chat\b", r"</?\w+[^>]*>", r"\bapi key\b",
]
# Spoken text should read as speech: no markdown or action asterisks reach TTS.
# Saying it is NOT real money is the point, so disclaimers are removed before matching.
# The one web address he may ever say is the international crisis helpline directory.
DISCLAIMER = re.compile(r"\b(no|not|none of it|nothing here is|isn'?t|aren'?t|ain'?t|never|without|zero|is this|if|if it'?s) real (money|cash|funds)\b"
                        r"|\bnever the reason anybody deposits\b|\bnot a strategy\b|\bfindahelpline\.com\b"
                        r"|\b(didn'?t|did not|never|who sign|who signed) sign(ed)? up\b|\bsign(ed)? up for (a|the|this) roast\b")
NOT_SPEECH = re.compile(r"[*_`#\[\]{}|\\]")

_GROUPS = {"brand": BRAND, "profanity": PROFANITY, "hype": HYPE_AND_NUDGE, "promise": PROMISE, "push": PUSH, "link": LINK, "leak": LEAK}
_COMPILED = {k: [re.compile(p) for p in v] for k, v in _GROUPS.items()}


# He never chooses for a viewer. Checked on chat replies only: in a round he talks about his own play.
_NUM = r"(\d+(\.\d+)?x?|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"   # "one" is mostly not a number here
_PICKABLE = rf"(tile|row|col|column|square|spot|number|heads|tails|red|black|odd|even|high|higher|low|lower|hi|lo|over|under|left|right|hit|stand|banker|player|{_NUM})"
CHOOSES = [re.compile(p) for p in (
    rf"\b{_PICKABLE}( \d+)?( col(umn)? \d+)? it is\b",
    rf"\b(i'?d|i would|i'?ll|i will|i'?m|gonna|let'?s) (\w+ )?(go with|go for|pick|picking|take|taking|say|saying|choose|choosing|click|clicking|cash( out)?( at)?|stick with)\s+(on |at |with |for |the )*{_PICKABLE}\b",
    rf"(?<!\byou )(?<!\bu )\b(go with|go for|try|click|pick|take|bet on|cash( out)? at|stick with|aim for)\s+(the )?(tile |row |square |number |spot )?{_NUM}\b",
    rf"(?<!\byou )(?<!\bu )\b(go with|go for|try|click|pick|take|bet on|stick with)\s+(the )?(heads|tails|red|black|odd|even|high|low|over|under|banker|player)\b",
    r"\bmy (pick|call|guess) (is|would be|'?s)\b", r"\blucky number\b.*\bis\b",
)]
# A viewer asking for a choice in words the moderation rules missed: then any number in the reply is an answer.
ASKS_FOR_PICK = re.compile(r"\b(pick|choose|call|tell me|give me|gimme|say|guess|name|recommend|suggest)\b.{0,25}\b(tile|number|multiplier|row|col|column|square|spot|side|target|cash ?out|one)\b")
NUMBER_IN_REPLY = re.compile(r"\b(\d+(\.\d+)?x?|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b")
# Talking about how some other person in chat is doing. Never his to say out loud.
OTHER_VIEWER_TROUBLE = re.compile(
    r"\b(person|guy|viewer|someone|somebody|one|folks?|people) (above|before|else|earlier|here|in (the )?chat|in here)\b.*"
    r"\b(rough|hard|heavy|struggl\w*|hurting|not (ok|okay|safe)|safe|going through|worried|worry|crisis|tough|helpline|dark)\b")


def chooses_for_viewer(reply: str, asked: str = "") -> bool:
    n, a = normalize(reply), normalize(asked)
    if any(rx.search(n) for rx in CHOOSES):
        return True
    return bool(ASKS_FOR_PICK.search(a) and NUMBER_IN_REPLY.search(n))


def hints_at_another_viewer(reply: str, crisis_names: set[str], own_name: str | None = None) -> bool:
    n = normalize(reply)
    own = (own_name or "").lower()
    return bool(OTHER_VIEWER_TROUBLE.search(n)) or any(c in n for c in crisis_names if c != own)


@dataclass(frozen=True)
class FilterResult:
    ok: bool
    reason: str = ""


class SafeLine:
    """A line that passed the filter. Only OutputFilter can create one."""
    __slots__ = ("text", "_token")
    _TOKEN = object()

    def __init__(self, text: str, token: object):
        if token is not SafeLine._TOKEN:
            raise TypeError("SafeLine can only be created by OutputFilter.approve")
        self.text = text
        self._token = token

    def __repr__(self) -> str:
        return f"SafeLine({self.text!r})"


class OutputFilter:
    def __init__(self, extra_patterns: list[str] | None = None):
        self.extra = [re.compile(p) for p in (extra_patterns or [])]
        self.blocked_count = 0

    def check(self, text: str) -> FilterResult:
        try:
            if not text or not text.strip():
                return FilterResult(False, "empty")
            if len(text) > MAX_LEN:
                return FilterResult(False, "too_long")
            if NOT_SPEECH.search(text):
                return FilterResult(False, "not_speech")
            n = DISCLAIMER.sub("demo", normalize(text))
            variants = {n, despace(n)}
            for group, patterns in _COMPILED.items():
                for rx in patterns:
                    if any(rx.search(v) for v in variants):
                        return FilterResult(False, f"{group}:{rx.pattern[:40]}")
            for rx in self.extra:
                if any(rx.search(v) for v in variants):
                    return FilterResult(False, f"extra:{rx.pattern[:40]}")
            if contains_blocked_word(text):
                return FilterResult(False, "blocked_word")
            return FilterResult(True)
        except Exception as e:  # fail closed
            return FilterResult(False, f"filter_error:{e!r}")

    def approve(self, text: str) -> SafeLine | None:
        res = self.check(text)
        if not res.ok:
            self.blocked_count += 1
            return None
        return SafeLine(text.strip(), SafeLine._TOKEN)
