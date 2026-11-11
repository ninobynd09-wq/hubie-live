"""Text normalisation shared by chat moderation and the output filter."""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path

ZERO_WIDTH = re.compile("[​-‏⁠-⁯﻿­]")
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
# Common Cyrillic and Greek look-alikes that NFKC leaves untouched.
_HOMOGLYPH = str.maketrans("аеорсуіхνοαе", "aeopcyixvoae")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = ZERO_WIDTH.sub("", t).translate(_HOMOGLYPH).lower()
    t = t.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", t).strip()


def despace(text: str) -> str:
    """Join runs of single letters: 'd e p o s i t' becomes 'deposit'."""
    return re.sub(r"\b(?:\w[ .\-_]){2,}\w\b", lambda m: re.sub(r"[ .\-_]", "", m.group(0)), text)


def deleet(text: str) -> str:
    return text.translate(_LEET)


def collapse_repeats(text: str) -> str:
    return re.sub(r"(.)\1+", r"\1", text)


def _load_hashes() -> set[str]:
    hashes: set[str] = set()
    p = Path(__file__).with_name("blocklist_hashes.txt")
    if p.exists():
        hashes |= {x.strip() for x in p.read_text().split() if x.strip()}
    extra = os.environ.get("HUBIE_BLOCKLIST_FILE")
    if extra and Path(extra).exists():
        for w in Path(extra).read_text().split():
            hashes.add(hashlib.sha256(w.strip().lower().encode()).hexdigest())
    return hashes


_HASHES = _load_hashes()


def contains_blocked_word(text: str) -> bool:
    """Blocklist is stored as SHA-256 of single words so the public repo doesn't spell it out."""
    n = despace(normalize(text))
    for variant in {n, deleet(n), collapse_repeats(deleet(n))}:
        for tok in re.findall(r"[a-z]+", variant):
            for form in (tok, collapse_repeats(tok), tok.rstrip("s")):
                if hashlib.sha256(form.encode()).hexdigest() in _HASHES:
                    return True
    return False
