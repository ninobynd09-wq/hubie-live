"""How close two spoken lines are. Used to keep round lines from repeating within a game,
and by tools/round_repeat_check.py to measure the repeat rate."""
from __future__ import annotations

import re

WORD = re.compile(r"[a-z0-9']+")
# Small words that carry no phrasing of their own.
STOP = {"a", "an", "the", "and", "or", "but", "so", "of", "on", "in", "at", "to", "for", "with", "that", "this",
        "it", "its", "it's", "is", "was", "be", "i", "i'm", "me", "my", "we", "u", "ur", "you", "one", "just",
        "there", "that's", "not", "no", "yo", "heh", "ok", "oh", "all", "some", "any", "again", "too", "very"}


def tokens(text: str, ignore: set[str] | frozenset = frozenset()) -> list[str]:
    return [t for t in WORD.findall((text or "").lower()) if t not in ignore]


def _grams(toks: list[str], n: int) -> set[tuple]:
    return {tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)}


def similarity(a: str, b: str, ignore: set[str] | frozenset = frozenset()) -> float:
    """0..1. Words in `ignore` (the game's name) don't count: every Fruit Party line says fruit party."""
    ta, tb = tokens(a, ignore), tokens(b, ignore)
    if not ta or not tb:
        return 1.0 if ta == tb else 0.0
    if ta == tb:
        return 1.0
    score = 0.0
    if len(ta) > 1 and len(tb) > 1:
        ga, gb = _grams(ta, 2), _grams(tb, 2)
        score = len(ga & gb) / len(ga | gb)
        # The same four-word run in both reads as the same line, whatever surrounds it.
        if _grams(ta, 4) & _grams(tb, 4):
            score = max(score, 0.9)
    ca, cb = set(ta) - STOP, set(tb) - STOP
    small = min(len(ca), len(cb))
    if small >= 3:
        ov = len(ca & cb) / small                   # same words reshuffled
        if ov >= 0.75:
            score = max(score, ov)
    elif small >= 1 and ca == cb:
        score = max(score, 1.0)
    return score


def too_similar(text: str, recent: list[str], ignore: set[str] | frozenset = frozenset(),
                threshold: float = 0.5) -> str | None:
    """The first recent line `text` is too close to, or None."""
    for old in reversed(recent):
        if similarity(text, old, ignore) >= threshold:
            return old
    return None


# ---- talk lines (segments, asides): stricter, paraphrases count ----

TALK_STOP = STOP | {"by", "kinda", "like", "really", "bit", "little", "here", "out", "up", "are", "am", "have", "has",
                    "had", "do", "does", "what", "when", "who", "how", "if", "then", "than", "about", "from", "into",
                    "they", "them", "their", "he", "she", "his", "her", "our", "us", "i'll", "i've", "don't", "can't",
                    "nothing", "something", "thing", "things", "get", "got", "still", "now", "tonight", "today", "yeah",
                    "huh", "hm", "hmm", "eh", "well", "much", "more", "most", "every", "even", "only", "also", "fine"}


def stem(t: str) -> str:
    """Rough word root, enough to match floor/floor's, rushing/rush, watching/watch, chips/chip."""
    t = t.replace("'s", "").replace("'", "")
    for suf in ("ing", "ed", "ly", "es", "s"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            t = t[: -len(suf)]
            break
    return t


def content_stems(text: str, ignore: set[str] | frozenset = frozenset()) -> list[str]:
    return [stem(t) for t in tokens(text, ignore) if t not in TALK_STOP]


def talk_similar(a: str, b: str, ignore: set[str] | frozenset = frozenset()) -> str | None:
    """Why `a` reads as a paraphrase of `b` (a short reason), or None. Stricter than `similarity`:
    a shared key phrase ("slow stuff", "nobody rushing") or the same handful of words is enough."""
    if similarity(a, b, ignore) >= 0.4:
        return "same line"
    ca, cb = content_stems(a, ignore), content_stems(b, ignore)
    # a two-word key phrase in both, small words left out ("watching the slow stuff" -> watch slow, slow stuff)
    pa = {(x, y) for x, y in zip(ca, ca[1:]) if x != y}
    pb = {(x, y) for x, y in zip(cb, cb[1:]) if x != y}
    shared = pa & pb
    if shared:
        return "phrase " + " ".join(sorted(shared)[0])
    sa, sb = set(ca), set(cb)
    small = min(len(sa), len(sb))
    common = sa & sb
    if len(common) >= 4 or (small >= 2 and len(common) >= 3 and len(common) / small >= 0.5) or \
            (small >= 2 and len(common) == small):
        return "words " + ", ".join(sorted(common))
    return None


def talk_repeat(text: str, recent: list[str], ignore: set[str] | frozenset = frozenset()) -> str | None:
    """The first recent line `text` paraphrases, or None."""
    for old in reversed(recent):
        if talk_similar(text, old, ignore):
            return old
    return None
