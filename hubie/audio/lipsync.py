"""Mouth for a spoken line, frame by frame.

Two layers. The sound hints (`from_alignment`, `from_text_and_env`, `from_env`) say which kind of mouth each
moment wants: round for o/oo/w, spread for ee, teeth on lip for f/v, closed for m/b/p, open for the rest. Best
source is the voice's own character timings (ElevenLabs with-timestamps); without them the text is laid over the
voiced parts of the audio; last resort is loudness alone.

`track` then runs at the video frame rate against the audio's loudness, which has the final say: the mouth is
never open on silence (it closes within about two frames), never closed while the voice is loud except for a
brief m/b/p closure (two frames, right before the release), and opens wider the louder he is, in steps of his own
shapes (closed, slight, half, open, wide; round small and big). A big change gets in-between frames of his own
in-between shapes (closed -> wide passes through half and open), so it never snaps closed -> wide. No
cross-fades: a 50/50 mix of two mouths shows both, as grey double lips. The result is a list of [seconds, shape]
change points.
Pure functions, no network, no processes."""
from __future__ import annotations

import audioop
import re
import wave
from pathlib import Path

REST, SLIGHT, HALF, OPEN, WIDE, ROUND, EE, FV, ROUND_S = range(9)
NAMES = ("rest", "slight", "half", "open", "wide", "round", "ee", "fv", "round_s")
MIN_HOLD = 0.06          # a consonant shape shorter than this (two frames at 30 fps) only flickers
MIN_VOWEL = 0.03         # vowels are what reads as speech, so they keep even a single frame
PAUSE = 0.14             # a space longer than this lets the mouth close

DIGRAPHS = {"oo": ROUND, "ou": ROUND, "ow": ROUND, "oa": ROUND, "ee": EE, "ea": EE, "ie": EE, "ai": OPEN,
            "ay": OPEN, "au": ROUND, "aw": ROUND, "oy": ROUND, "oi": ROUND, "sh": ROUND, "ch": ROUND, "th": SLIGHT,
            "wh": ROUND, "qu": ROUND, "ph": FV}
LETTERS = {"a": WIDE, "e": EE, "i": EE, "o": ROUND, "u": ROUND, "y": EE,
           "m": REST, "b": REST, "p": REST, "w": ROUND, "f": FV, "v": FV, "r": SLIGHT}
VOWELS = {HALF, OPEN, WIDE, ROUND, ROUND_S, EE}


def letter_shape(ch: str) -> int | None:
    """Shape for one character; None for spaces and punctuation."""
    c = ch.lower()
    if c in LETTERS:
        return LETTERS[c]
    if c.isalpha() or c.isdigit():
        return SLIGHT
    return None


def units(text: str) -> list[tuple[int, int, int | None]]:
    """(start index, end index, shape) per sound-ish unit of the text: digraphs first, then letters."""
    out, i, low = [], 0, text.lower()
    while i < len(text):
        two = low[i:i + 2]
        if two in DIGRAPHS:
            out.append((i, i + 2, DIGRAPHS[two]))
            i += 2
            continue
        sh = letter_shape(text[i])
        # a silent e at the end of a word adds no mouth move of its own
        if low[i] == "e" and i > 1 and (i + 1 == len(low) or not low[i + 1].isalpha()) and low[i - 1].isalpha():
            sh = SLIGHT
        out.append((i, i + 1, sh))
        i += 1
    return out


def finalize(cues: list[tuple[float, int]], end: float) -> list[list]:
    """Sort, drop repeats and too-short shapes, start at 0, end closed."""
    cues = sorted((max(0.0, float(t)), int(s)) for t, s in cues)
    if not cues or cues[0][0] > 0:
        cues.insert(0, (0.0, REST))
    cues.append((max(end, cues[-1][0]), REST))
    runs: list[tuple[float, int]] = []
    for t, s in cues:   # a run of one shape is one cue, so its length is the whole run
        if not runs or runs[-1][1] != s:
            runs.append((t, s))
    cues = runs
    merged: list[list] = []
    for i, (t, s) in enumerate(cues):
        nxt = cues[i + 1][0] if i + 1 < len(cues) else None
        if merged and merged[-1][1] == s:
            continue
        short = nxt is not None and nxt - t < (MIN_VOWEL if s in VOWELS else MIN_HOLD)
        if short and merged and s != REST:
            continue   # too short to see; keep what was showing (a vowel gets a frame, a consonant needs two)
        merged.append([round(t, 3), s])
    out = [merged[0]]
    for t, s in merged[1:]:
        if s != out[-1][1]:
            out.append([t, s])
    return out


def _env_at(env: list[float] | None, t: float, step: float) -> float | None:
    if not env:
        return None
    i = int(t / step)
    return env[i] if 0 <= i < len(env) else 0.0


def _soften(shape: int, level: float | None) -> int:
    """Quiet vowels only part the lips; near silence closes them. Covers small timing errors."""
    if level is None or shape == REST:
        return shape
    if level < 0.03:
        return REST
    if shape in VOWELS and level < 0.1:
        return SLIGHT
    return shape


def from_alignment(chars: list[str], starts: list[float], ends: list[float],
                   env: list[float] | None = None, env_step: float = 0.05) -> list[list]:
    """Character timings straight from the voice. env (loudness per env_step) refines quiet vowels."""
    text = "".join(chars)
    if not (len(chars) == len(starts) == len(ends)) or not chars:
        return [[0.0, REST]]
    cues: list[tuple[float, int]] = []
    for a, b, sh in units(text):
        t0, t1 = float(starts[a]), float(ends[b - 1])
        if sh is None:
            if text[a] == " " and t1 - t0 < PAUSE:
                continue   # word gap in running speech: the mouth doesn't shut
            sh = REST
        mid = (t0 + t1) / 2
        cues.append((t0, _soften(sh, _env_at(env, mid, env_step))))
    return finalize(cues, float(max(ends)))


def voiced(env: list[float], thr: float = 0.08, fill: int = 2) -> list[bool]:
    v = [x >= thr for x in env]
    # close short dips so a word doesn't split in two
    i = 0
    while i < len(v):
        if not v[i]:
            j = i
            while j < len(v) and not v[j]:
                j += 1
            if 0 < i and j < len(v) and j - i <= fill:
                for k in range(i, j):
                    v[k] = True
            i = j
        else:
            i += 1
    return v


def from_text_and_env(text: str, env: list[float], env_step: float = 0.05) -> list[list]:
    """No timings: spread the text's sounds over the voiced stretches of the audio, in order."""
    v = voiced(env)
    total = sum(v)
    us = [(a, b, sh) for a, b, sh in units(" ".join(text.split())) if sh is not None]
    if not total or not us:
        return from_env(env, env_step)
    weights = [2.0 if sh in VOWELS else 1.0 for _, _, sh in us]
    wsum = sum(weights)
    frames = [i for i, x in enumerate(v) if x]   # voiced frame indices, in time order
    cues, acc = [], 0.0
    for (a, b, sh), w in zip(us, weights):
        k = min(len(frames) - 1, int(acc / wsum * len(frames)))
        t = frames[k] * env_step
        cues.append((t, _soften(sh, _env_at(env, t + env_step / 2, env_step))))
        acc += w
    for i, x in enumerate(v):   # silences close, whatever the text says
        if not x and (i == 0 or v[i - 1]):
            cues.append((i * env_step + 1e-3, REST))
    return finalize(cues, len(env) * env_step)


def from_env(env: list[float], env_step: float = 0.05) -> list[list]:
    """Loudness only: closed, slightly open, open, wide."""
    cues = []
    for i, x in enumerate(env or []):
        s = REST if x < 0.08 else SLIGHT if x < 0.3 else OPEN if x < 0.65 else WIDE
        cues.append((i * env_step, s))
    return finalize(cues, len(env or []) * env_step)


def envelope_of(wav: Path, step: float = 0.05) -> list[float]:
    with wave.open(str(wav), "rb") as w:
        rate, width = w.getframerate(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())
        if w.getnchannels() == 2:
            pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    n = int(rate * step) * width
    vals = [audioop.rms(pcm[i:i + n], width) for i in range(0, len(pcm), n)]
    # scale to a loud-but-typical level (90th percentile of the voiced part), not the single loudest blip
    voiced_vals = sorted(v for v in vals if v > 0.02 * (max(vals) if vals else 0))
    ref = voiced_vals[int(len(voiced_vals) * 0.9)] if voiced_vals else 0
    return [round(min(1.0, x / ref), 3) if ref else 0.0 for x in vals]


# ---------- frame by frame ----------

FPS = 30
ON, OFF = 0.08, 0.05     # loudness gate with hysteresis (1.0 = loud-but-typical speech for this clip)
CLOSE_FRAMES = 2         # an m/b/p closes the lips for this many frames: right before the release into the next
                         # sound, or at its start when nothing follows in the word (a hummed "mm")
MAX_HOLD = 6             # a vowel held longer than this gets a small jaw move, so it never sits like a sticker
HYST = 0.5               # openness has to move this far from the shown step before the mouth changes step
LADDER = (REST, SLIGHT, HALF, OPEN, WIDE)   # one take of his smile, opening wider step by step
KIND = {REST: "m", SLIGHT: "c", HALF: "a", OPEN: "a", WIDE: "a", ROUND: "o", ROUND_S: "o", EE: "e", FV: "f"}


def hints_from_alignment(chars: list[str], starts: list[float], ends: list[float]) -> list[tuple]:
    """(start, end, kind) per sound of the text: a open vowel, o round, e spread, f f/v, m m/b/p, c other
    consonant, None for spaces and punctuation."""
    text = "".join(chars)
    out = []
    for a, b, sh in units(text):
        out.append((float(starts[a]), float(ends[b - 1]), None if sh is None else KIND.get(sh, "c")))
    return out


def hints_from_cues(cues: list[list]) -> list[tuple]:
    """Change-point cues from the older planners, as hints. Their REST is a pause or an m/b/p; loudness tells
    those apart, so it is passed on as m (a closure only ever lasts CLOSE_FRAMES)."""
    out = []
    for i, (t, sh) in enumerate(cues):
        t1 = cues[i + 1][0] if i + 1 < len(cues) else t + 0.2
        out.append((float(t), float(t1), KIND.get(int(sh), "c")))
    return out


def frame_kinds(hints: list[tuple], n: int, fps: int = FPS) -> list[str | None]:
    """The kind of mouth each frame wants. A consonant takes the nearest vowel of its word, a step less open (the
    h of "oh" stays round), an m/b/p closes only on its last frames and otherwise hums with the lips just apart."""
    spans = []
    for t0, t1, k in sorted(hints, key=lambda h: h[0]):
        if spans and k == "m" and spans[-1][2] == "m":
            spans[-1] = (spans[-1][0], max(t1, spans[-1][1]), "m")   # "mm" is one closure
        else:
            spans.append((t0, max(t1, t0 + 1e-3), k))
    kinds: list[str | None] = [None] * n
    owner = [-1] * n
    j = 0
    for i in range(n):
        t = (i + 0.5) / fps
        while j + 1 < len(spans) and spans[j + 1][0] <= t:
            j += 1
        if spans and spans[j][0] <= t < spans[j][1] + 0.5 / fps:
            kinds[i], owner[i] = spans[j][2], j
    # word boundaries: spans of kind None split words
    word = []
    w = 0
    for sp in spans:
        if sp[2] is None:
            w += 1
        word.append(w)
    first, last = {}, {}
    for i, o in enumerate(owner):
        first.setdefault(o, i)
        last[o] = i
    for i in range(n):
        k, o = kinds[i], owner[i]
        if k == "m":
            released = o + 1 < len(spans) and spans[o + 1][2] not in (None, "m")
            edge = last[o] - i if released else i - first[o]
            kinds[i] = "M" if edge < CLOSE_FRAMES else "h"
        elif k == "c":
            t = (i + 0.5) / fps
            best = None
            for q, sp in enumerate(spans):
                if word[q] == word[o] and sp[2] in ("a", "o", "e"):
                    d = 0.0 if sp[0] <= t < sp[1] else min(abs(t - sp[0]), abs(t - sp[1]))
                    if best is None or d < best[0]:
                        best = (d, sp[2])
            if best and best[0] <= 0.25:
                kinds[i] = best[1] + "_c"   # that vowel's mouth, a step less open
    return kinds


def openness(level: float) -> float:
    """1 = lips apart (slight) .. 4 = wide, growing with loudness."""
    return 1 + 3 * max(0.0, min(1.0, (level - ON) / (1.0 - ON))) ** 0.7


def _step(o: float, prev: int | None) -> int:
    q = max(1, min(len(LADDER) - 1, int(round(o))))
    if prev is not None and prev in LADDER and abs(o - LADDER.index(prev)) < HYST:
        return prev
    return LADDER[q]


def target(kind: str | None, level: float, prev: int | None = None) -> int:
    """The one shape a frame wants, from its kind and its loudness. Steps only change once the loudness has clearly
    moved (hysteresis), so the mouth doesn't flutter between two steps."""
    o = openness(level)
    if kind == "M":
        return REST
    if kind in ("c", "a_c", None):   # consonants and word gaps in running speech: a step less open
        return _step(1 + (o - 1) * 0.6, prev)
    if kind == "o_c":
        return ROUND_S
    if kind == "e_c":
        return SLIGHT if o < 2.2 else EE
    if kind == "h":   # humming through an m: lips apart, a little more on the louder bits
        return _step(min(2.0, 1 + (o - 1) * 0.6), prev)
    if kind == "o":
        if prev in (ROUND, ROUND_S):
            return ROUND if o >= (2.4 if prev == ROUND else 2.8) else ROUND_S
        return ROUND if o >= 2.6 else ROUND_S
    if kind == "e":   # spread; a loud e (the e of "well") drops the jaw into the open shape
        return SLIGHT if o < 1.4 else EE if o <= 3.0 else OPEN
    if kind == "f":
        return FV
    return _step(o, prev)


NEIGHBOUR = {SLIGHT: (HALF, None), HALF: (OPEN, SLIGHT), OPEN: (WIDE, HALF), WIDE: (None, OPEN),
             ROUND_S: (ROUND, None), ROUND: (None, ROUND_S), EE: (None, SLIGHT)}   # (more open, less open)
RANK = {REST: 0, SLIGHT: 1, FV: 1, EE: 1.5, HALF: 2, ROUND_S: 2.5, OPEN: 3, ROUND: 3.5, WIDE: 4}


def between(x: int, y: int) -> list[int]:
    """The in-between shapes going from x to y: his own shapes of in-between openness (closed -> wide passes
    through half and open), two at most. Neighbours (one step apart) need none. No cross-fades: a 50/50 mix of two
    mouths shows both as grey double lips."""
    lo, hi = sorted((RANK[x], RANK[y]))
    if hi - lo <= 1.5 or {x, y} == {REST, ROUND_S}:   # the small round is itself the way into a round mouth
        return []
    chain = [REST, SLIGHT, ROUND_S] if ROUND in (x, y) or ROUND_S in (x, y) else list(LADDER)
    mid = sorted((s for s in chain if lo < RANK[s] < hi), key=RANK.get)
    if RANK[y] < RANK[x]:
        mid.reverse()
    if len(mid) > 2:
        mid = [mid[len(mid) // 3], mid[(2 * len(mid)) // 3]]
    return mid


def loudness(wav: Path, fps: int = FPS) -> list[float]:
    return envelope_of(wav, 1 / fps)


HOLD = 2                 # every shape on screen, in-betweens too, lasts at least this many frames
BOUNCE = 3               # A -> B -> A is only allowed if B lasts at least this long; a shorter B is flutter
MAX_RATE = 6             # shapes picked from the sound: at most this many changes in any one second ...
MAX_RATE_ALL = 7         # ... and at most this many with the in-betweens and jaw moves added
RELEASE = 0.5            # loudness falls to this fraction per frame at most: opens on the frame, closes over 2-3
SHORT_FAR = 5            # a big shape this short with no room for an in-between becomes the in-between
PRIO = {"c": 0, "i": 1, "v": 2, "M": 3, "gap": 4}   # who goes first when a second has too many changes


class _Run:
    __slots__ = ("s", "n", "cls")

    def __init__(self, s: int, n: int, cls: str):
        self.s, self.n, self.cls = s, n, cls   # shape, frames, v vowel / c consonant / M m-b-p / gap / i in-between

    def __repr__(self):
        return f"{NAMES[self.s]}x{self.n}{self.cls}"


def _runs(shape: list[int], kinds: list[str | None]) -> list[_Run]:
    out: list[_Run] = []
    i = 0
    while i < len(shape):
        j = i
        while j < len(shape) and shape[j] == shape[i]:
            j += 1
        ks = kinds[i:j]
        if shape[i] == REST:
            cls = "M" if "M" in ks else "gap"
        else:
            cls = "v" if sum(k in ("a", "o", "e") for k in ks) * 2 >= len(ks) else "c"
        out.append(_Run(shape[i], j - i, cls))
        i = j
    return out


def _coalesce(runs: list[_Run]) -> list[_Run]:
    out: list[_Run] = []
    for r in runs:
        if r.n <= 0:
            continue
        if out and out[-1].s == r.s:
            a = out[-1]
            if PRIO[r.cls] > PRIO[a.cls]:
                a.cls = r.cls
            a.n += r.n
        else:
            out.append(_Run(r.s, r.n, r.cls))
    return out


def _merge(runs: list[_Run], k: int) -> list[_Run]:
    """Run k disappears into a neighbour: the vowel side if there is one, else the nearer shape, else the longer."""
    r = runs[k]
    cands = [q for q in (k - 1, k + 1) if 0 <= q < len(runs)]
    if not cands:
        return runs

    def score(q):
        o = runs[q]
        return (o.cls == "v", o.cls != "gap", -abs(RANK[o.s] - RANK[r.s]), o.n)
    q = max(cands, key=score)
    runs[q].n += r.n
    r.n = 0
    return _coalesce(runs)


def _starts(runs: list[_Run]) -> list[int]:
    out, p = [], 0
    for r in runs:
        out.append(p)
        p += r.n
    return out


def _worst_window(runs: list[_Run], cap: int, fps: int) -> list[int] | None:
    """Indices of the runs starting (that is, changing) inside the first one-second window that holds more than cap
    changes, or None."""
    ch = _starts(runs)[1:]   # change number a is the start of run a + 1
    for a in range(len(ch)):
        b = a
        while b < len(ch) and ch[b] - ch[a] < fps:
            b += 1
        if b - a > cap:
            return list(range(a + 1, b + 1))
    return None


def _edge(runs: list[_Run], k: int) -> bool:
    """The closed mouth before and after the line is not a shape that needs a hold of its own."""
    return runs[k].s == REST and (k == 0 or k == len(runs) - 1)


def _calm(runs: list[_Run], fps: int, cap: int | None) -> list[_Run]:
    """Min hold, no A-B-A flutter, and (with cap) at most cap changes in any second. Short consonants go first,
    into their vowel."""
    for _ in range(6 * len(runs) + 8):
        # shorter than the hold: into a neighbour (an m/b/p closure last)
        short = [k for k, r in enumerate(runs) if r.n < HOLD and not _edge(runs, k)]
        if short:
            runs = _merge(runs, min(short, key=lambda q: (runs[q].cls == "M", runs[q].n)))
            continue
        # A-B-A with a short B: flutter. A vowel or an m/b/p closure in the middle takes BOUNCE frames from its
        # sides when they can spare them; otherwise B goes
        hit = False
        for k in range(1, len(runs) - 1):
            a, b, c = runs[k - 1], runs[k], runs[k + 1]
            if a.s != c.s or b.n >= BOUNCE:
                continue
            need = BOUNCE - b.n
            if a.s == REST and "gap" in (a.cls, c.cls):   # a quick word between pauses: it gets BOUNCE frames
                donor = max((a, c), key=lambda r: r.n)
                if donor.n - need >= HOLD:
                    donor.n -= need
                    b.n += need
                    hit = True
                    break
                continue
            hit = True
            if b.cls in ("M", "v") or PRIO[b.cls] > PRIO[a.cls]:
                donor = max((a, c), key=lambda r: r.n)
                if donor.n - need >= BOUNCE:   # (not HOLD: the donor must not become a short B itself)
                    donor.n -= need
                    b.n += need
                    break
            b.s, b.cls = a.s, a.cls
            runs = _coalesce(runs)
            break
        if hit:
            continue
        win = _worst_window(runs, cap, fps) if cap else None
        if win:   # too many changes in a second: the weakest shape in it goes
            cand = [k for k in win if not _edge(runs, k)] or win
            runs = _merge(runs, min(cand, key=lambda q: (PRIO[runs[q].cls], runs[q].n)))
            continue
        break
    return runs


def _make_room(runs: list[_Run], keep: set, cap: int, fps: int) -> list[_Run] | None:
    """An in-between matters more than a consonant shape: consonants in a crowded second go, into their vowels,
    until it fits. None if that is not enough."""
    copies = [_Run(r.s, r.n, r.cls) for r in runs]   # the caller keeps its own list if this fails
    keep = {id(c) for r, c in zip(runs, copies) if id(r) in keep}
    runs = copies
    for _ in range(len(runs)):
        win = _worst_window(runs, cap, fps)
        if win is None:
            return runs
        cand = [k for k in win if id(runs[k]) not in keep and runs[k].cls == "c" and not _edge(runs, k)]
        if not cand:
            return None
        k = min(cand, key=lambda q: (PRIO[runs[q].cls], runs[q].n))
        r = runs[k]
        nb = [q for q in (k - 1, k + 1) if 0 <= q < len(runs) and id(runs[q]) not in keep]
        if not nb:
            return None
        q = max(nb, key=lambda q: (runs[q].cls == "v", runs[q].cls != "gap", -abs(RANK[runs[q].s] - RANK[r.s])))
        runs[q].n += r.n
        del runs[k]
        if q > k:
            q -= 1
        if 0 < q and runs[q - 1].s == runs[q].s or q + 1 < len(runs) and runs[q + 1].s == runs[q].s:
            return None   # merging would join two shapes the in-betweens rely on; give up rather than guess
    return None


def _unsnap(runs: list[_Run]) -> list[_Run]:
    """Where a big change still has no in-between (no room or no budget), a short far shape (up to SHORT_FAR frames)
    is cut down to the in-between itself: a quick open after closed shows as half. Adds no change."""
    for _ in range(2 * len(runs) + 4):
        hit = False
        for k in range(1, len(runs)):
            x, y = runs[k - 1], runs[k]
            mids = between(x.s, y.s)
            if not mids:
                continue
            far, near = (x, y) if RANK[y.s] < RANK[x.s] else (y, x)
            if far.n > SHORT_FAR:
                continue
            ok = [m for m in mids if not between(m, near.s)] or mids
            far.s = min(ok, key=lambda m: abs(RANK[m] - RANK[far.s]))
            runs = _coalesce(runs)
            hit = True
            break
        if not hit:
            break
    return runs


def _jaw(runs: list[_Run], lv: list[float], fps: int) -> list[_Run]:
    """A long-held shape gets one jaw move to the neighbouring shape (BOUNCE frames, where the loudness dips or
    peaks most), so it never sits like a sticker. Only where the second still has room for two more changes."""
    k = 0
    while k < len(runs):
        r = runs[k]
        if r.s in NEIGHBOUR and r.n >= 3 * BOUNCE and r.n > MAX_HOLD:
            st = _starts(runs)[k]
            seg = lv[st:st + r.n]
            mean = sum(seg) / len(seg)
            q = max(range(BOUNCE, r.n - 2 * BOUNCE + 1), key=lambda x: abs(sum(seg[x:x + BOUNCE]) / BOUNCE - mean))
            up, down = NEIGHBOUR[r.s]
            peak = sum(seg[q:q + BOUNCE]) / BOUNCE > mean
            alt = up if peak and up is not None else down if down is not None else up
            trial = runs[:k] + [_Run(r.s, q, r.cls), _Run(alt, BOUNCE, "i"), _Run(r.s, r.n - q - BOUNCE, r.cls)] + runs[k + 1:]
            if alt is not None and _worst_window(trial, MAX_RATE_ALL, fps) is None:
                runs = trial
                k += 3
                continue
        k += 1
    return runs


def _inbetweens(runs: list[_Run], fps: int, cap: int | None) -> list[_Run]:
    """Big changes pass through his in-between shapes (closed -> wide via half and open), each held HOLD frames,
    taken from the open side: closing ones before the change, so the mouth is shut when the sound stops, opening
    ones from it. Two if there is room, else the one nearest halfway; then one straddling the change, then one on
    the closed side. A big shape too short for any of that is cut down to the in-between itself. With a cap, an
    in-between that would crowd its second is left out and the change snaps."""
    k, guard = 1, 0
    while k < len(runs) and guard < 8 * len(runs) + 16:
        guard += 1
        x, y = runs[k - 1], runs[k]
        mids = between(x.s, y.s)
        if not mids:
            k += 1
            continue
        closing = RANK[y.s] < RANK[x.s]
        far, near = (x, y) if closing else (y, x)   # far = the more open side
        half = (RANK[x.s] + RANK[y.s]) / 2
        one = [min(mids, key=lambda m: (abs(RANK[m] - half), abs(RANK[m] - RANK[far.s])))]
        options = [(mids, [])] if len(mids) == 1 else [(mids, []), (one, [])]
        options += [(one, "straddle")]
        if len(mids) == 2:   # one on each side
            options += [(mids[:1], mids[1:]) if closing else (mids[1:], mids[:1])]
        options += [([], one)]
        room = False
        for on_far, on_near in options:
            if on_near == "straddle":
                tf, tn, seq = HOLD - HOLD // 2, HOLD // 2, [_Run(on_far[0], HOLD, "i")]
            else:
                tf, tn = HOLD * len(on_far), HOLD * len(on_near)
                seq = [_Run(m, HOLD, "i") for m in ((on_far + on_near) if closing else (on_near + on_far))]
            if far.n - tf < HOLD or (near.n - tn < HOLD and not (near.s == REST and near.n - tn >= 1 and
                                                               (k - 1 == 0 or k == len(runs) - 1))):
                continue
            room = True
            if closing:
                trial = runs[:k - 1] + [_Run(x.s, x.n - tf, x.cls)] + seq + [_Run(y.s, y.n - tn, y.cls)] + runs[k + 1:]
            else:
                trial = runs[:k - 1] + [_Run(x.s, x.n - tn, x.cls)] + seq + [_Run(y.s, y.n - tf, y.cls)] + runs[k + 1:]
            if cap is not None and _worst_window(trial, cap, fps) is not None:
                made = _make_room(trial, {id(r) for r in seq} | {id(trial[k - 1 if closing else k + len(seq)])},
                                  cap, fps)
                # closed <-> open or wider never snaps: that in-between may take one change over the cap
                big = abs(RANK[x.s] - RANK[y.s]) >= 3 and _worst_window(trial, cap + 1, fps) is None
                trial = made if made is not None else trial if big else None
            if trial is not None:
                runs = _coalesce(trial)
                k = runs.index(seq[-1]) + 2 if seq[-1] in runs else k + 1
                break
        else:
            if not room:   # too short for any in-between: the big shape becomes the in-between
                other = near
                ok = [m for m in mids if not between(m, other.s)] or mids
                far.s = min(ok, key=lambda m: abs(RANK[m] - RANK[far.s]))
                far.cls = "i" if far.cls not in ("v", "M") else far.cls
                runs = _coalesce(runs)
                k = max(1, k - 1)
            else:
                k += 1
    return runs


def track(hints: list[tuple] | None, level: list[float], fps: int = FPS) -> list[list]:
    """Per-frame mouth for a clip as [seconds, shape] change points, from the sound hints and the loudness per
    frame. Loudness decides open or closed and how open; hints decide which mouth.
    Calm by rule: the mouth opens on the frame the voice starts (one frame early) and closes over two or three as it
    fades; every shape, in-betweens included, holds at least HOLD frames; an A-B-A with B shorter than BOUNCE frames
    is flutter and goes (a vowel or an m/b/p closure in the middle gets BOUNCE frames instead, when its sides can
    spare them); at most MAX_RATE_ALL changes in any second, short consonants going first, into their vowel."""
    n = len(level)
    if not n:
        return [[0.0, REST]]
    kinds = frame_kinds(hints, n, fps) if hints else ["a"] * n
    lead = [max(level[i], level[i + 1] if i + 1 < n else 0.0) for i in range(n)]   # lips move a frame before sound
    lv, prev = [], 0.0
    for x in lead:   # fast attack, slow release: the jaw does not snap shut in every little dip
        prev = x if x >= prev else max(x, prev * RELEASE)
        lv.append(prev)
    shape: list[int] = []
    on = False
    for i in range(n):
        on = lv[i] > (OFF if on else ON)
        shape.append(target(kinds[i], lv[i], shape[-1] if shape else None) if on else REST)
    shape.append(REST)   # the close after the line counts towards its last second too
    runs = _calm(_runs(shape, kinds + [None]), fps, MAX_RATE)
    runs = _unsnap(_jaw(_inbetweens(runs, fps, MAX_RATE_ALL), lv, fps))
    runs = _calm(runs, fps, None)   # the hold and flutter rules have the last word
    out: list[list] = []
    p = 0
    for r in runs:
        if not out or out[-1][1] != r.s:
            out.append([round(p / fps, 4), r.s])
        p += r.n
    if out[-1][1] != REST:
        out.append([round(min(p, n) / fps, 4), REST])
    return out


def track_from_env(text: str, env: list[float], env_step: float = 0.05, fps: int = FPS) -> list[list]:
    """For a clip that came without a planned track: the text laid over the loudness envelope, frame by frame."""
    if not env:
        return [[0.0, REST]]
    level = [env[min(len(env) - 1, int(i / fps / env_step))] for i in range(int(len(env) * env_step * fps))]
    hints = hints_from_cues(from_text_and_env(re.sub(r"\s+", " ", text), env, env_step)) if text.strip() else None
    return track(hints, level, fps)


def plan(text: str, wav: Path, alignment: dict | None = None, fps: int = FPS) -> list[list]:
    """The mouth track for one clip, from the best source available. Never raises."""
    try:
        level = loudness(wav, fps)
    except Exception:
        level = []
    if not level:
        return [[0.0, REST]]
    try:
        if alignment and alignment.get("characters"):
            ch, st, en = (alignment["characters"], alignment["character_start_times_seconds"],
                          alignment["character_end_times_seconds"])
            if len(ch) == len(st) == len(en):
                return track(hints_from_alignment(ch, st, en), level, fps)
        env = envelope_of(wav)
        if text and text.strip():
            return track(hints_from_cues(from_text_and_env(re.sub(r"\s+", " ", text), env)), level, fps)
        return track(None, level, fps)
    except Exception:
        try:
            return track(None, level, fps)
        except Exception:
            return [[0.0, REST]]
