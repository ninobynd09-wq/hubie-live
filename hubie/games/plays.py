"""How Hubie plays the house originals (zero bet, so he can afford to be brave): a plan per round, chat requests for
the next round, and the few words that describe a plan on stream. Pure functions, no browser.

A plan only ever names game settings (mines count, picks, road level, steps, plinko risk and rows, a target).
Nothing in here touches an amount: the adapter keeps every bet at 0 whatever the plan says."""
from __future__ import annotations

import random
import re

MINES_COUNTS = [1, 3, 5, 10, 20]
MINES_WEIGHTS = [1, 4, 4, 2, 1]                     # mostly 3-5, sometimes 10/20
MINES_DEPTH = {1: (2, 6), 3: (1, 5), 5: (1, 4), 10: (1, 3), 20: (1, 2)}   # tiles to open: deeper with fewer mines
ROAD_LEVELS = ["Easy", "Medium", "Hard", "Daredevil"]
ROAD_WEIGHTS = [4, 4, 2, 1]                         # mostly easy/medium, sometimes hard, rarely daredevil
ROAD_STEPS = {"Easy": (1, 6), "Medium": (1, 5), "Hard": (1, 4), "Daredevil": (1, 3)}
PLINKO_RISKS = ["Low", "Medium", "High"]
PLINKO_RISK_WEIGHTS = [3, 4, 3]
PLINKO_ROWS = list(range(8, 17))
LIMBO_TARGETS = [1.5, 2, 5, 10, 25, 50, 100]
LIMBO_WEIGHTS = [6, 8, 4, 2, 1, 0.6, 0.4]           # 25/50/100x now and then
LIMBO_QUICK = {1.5: "1.5x", 2: "2x", 5: "5x", 10: "10x", 100: "100x"}   # the game's own quick buttons
DICE_SAFE = [50.5, 25.0, 75.0, 66.0, 33.0, 10.0]
DICE_RISKY = [90.0, 92.0, 94.0, 95.0]               # 10% down to 5% win chance
DICE_RISKY_SHARE = 0.18
# Drama (2026-11-08): now and then a burst of plinko balls in one round, or mines "all in" (deep on few mines).
# Each ball is its own zero-bet request through the same guard; "all in" is only how deep he goes, never an amount.
PLINKO_BURST_SHARE = 0.25
PLINKO_BURST_BALLS = (3, 5)
PLINKO_MAX_BALLS = 5
MINES_ALLIN_SHARE = 1 / 6
MINES_ALLIN_COUNTS = [1, 2, 3]
MINES_ALLIN_DEPTH = (8, 10)

KENO_RISKS = ["Classic", "Low", "Medium", "High"]
KENO_RISK_WEIGHTS = [3, 3, 3, 2]
KENO_PICKS = list(range(1, 11))
KENO_PICK_WEIGHTS = [1, 2, 3, 4, 4, 3, 3, 2, 1, 1]  # mostly 3-7 numbers, sometimes 1 or all 10

GAME_OF_API = {"mines2": "mines", "road": "hubies_road", "plinko2": "plinko", "limbo": "limbo", "dice": "dice",
               "keno2": "keno", "blackjack7": "blackjack"}


def _num(v: float) -> str:
    return f"{v:g}"


def plan_round(game_id: str, rng: random.Random | None = None, request: dict | None = None) -> dict:
    """A plan for the next round of this original. request (from parse_request) wins over the dice."""
    rng = rng or random.Random()
    req = dict(request or {})
    p: dict = {"game": game_id}
    if game_id == "mines":
        allin = bool(req.get("allin")) or (not req and rng.random() < MINES_ALLIN_SHARE)
        if allin:
            n = int(req.get("mines") or rng.choice(MINES_ALLIN_COUNTS))
            n = max(1, min(24, n))
            depth = int(req.get("depth") or rng.randint(*MINES_ALLIN_DEPTH))
            p.update(mines=n, depth=max(1, min(depth, 24 - n)), allin=True, announce=True)
            p["risky"] = True
        else:
            n = int(req.get("mines") or rng.choices(MINES_COUNTS, MINES_WEIGHTS)[0])
            n = max(1, min(24, n))
            lo, hi = MINES_DEPTH.get(n, (1, 2) if n > 10 else (1, 4))
            depth = rng.randint(lo, hi)
            if req.get("deeper"):
                depth = min(max(depth + 2, hi), 24 - n, 8)
            p.update(mines=n, depth=max(1, min(depth, 25 - n)))
            p["risky"] = n >= 10 or p["depth"] >= 5
    elif game_id == "hubies_road":
        level = req.get("level") or rng.choices(ROAD_LEVELS, ROAD_WEIGHTS)[0]
        lo, hi = ROAD_STEPS[level]
        steps = rng.randint(lo, hi)
        if req.get("deeper"):
            steps = min(max(steps + 2, hi), 8)
        p.update(level=level, steps=steps)
        p["risky"] = level in ("Hard", "Daredevil") or steps >= 5
    elif game_id == "plinko":
        p.update(risk=req.get("risk") or rng.choices(PLINKO_RISKS, PLINKO_RISK_WEIGHTS)[0],
                 rows=int(req.get("rows") or rng.choice(PLINKO_ROWS)))
        if req.get("balls"):
            balls = int(req["balls"])
        elif not req and rng.random() < PLINKO_BURST_SHARE:
            balls = rng.randint(*PLINKO_BURST_BALLS)
        else:
            balls = 1
        p["balls"] = max(1, min(PLINKO_MAX_BALLS, balls))
        p["risky"] = (p["risk"] == "High" and p["rows"] >= 14) or p["balls"] > 1
    elif game_id == "limbo":
        t = float(req.get("target") or rng.choices(LIMBO_TARGETS, LIMBO_WEIGHTS)[0])
        p.update(target=t)
        p["risky"] = t >= 10
    elif game_id == "dice":
        if req.get("roll_over"):
            ro = float(req["roll_over"])
        elif rng.random() < DICE_RISKY_SHARE:
            ro = rng.choice(DICE_RISKY)
        else:
            ro = rng.choice(DICE_SAFE)
        p.update(roll_over=ro)
        p["risky"] = ro >= 90
    elif game_id == "keno":
        picks = int(req.get("picks") or rng.choices(KENO_PICKS, KENO_PICK_WEIGHTS)[0])
        picks = max(1, min(10, picks))
        p.update(risk=req.get("risk") or rng.choices(KENO_RISKS, KENO_RISK_WEIGHTS)[0], picks=picks,
                 numbers=sorted(rng.sample(range(1, 41), picks)))
        p["risky"] = p["risk"] == "High" and picks >= 7
    elif game_id == "blackjack":
        if req.get("push") in ("hit", "stand"):
            p["push"] = req["push"]
        p["risky"] = False
    else:
        p["risky"] = False
    if req.get("name"):
        p["asked_by"] = req["name"]
    p["text"] = plan_text(p)
    p["short"] = plan_short(p)
    return p


def plan_short(p: dict) -> str:
    """The plan's headline, for the line after the round: "10 mines", "daredevil", "100x", "over 95"."""
    g = p.get("game")
    if g == "mines":
        if p.get("allin"):
            return f"{p['depth']} deep on {p['mines']} mine{'s' if p['mines'] != 1 else ''}"
        return f"{p['mines']} mine{'s' if p['mines'] != 1 else ''}"
    if g == "hubies_road":
        return p["level"].lower()
    if g == "plinko":
        if int(p.get("balls") or 1) > 1:
            return f"{p['balls']} balls"
        return f"{p['risk'].lower()} risk"
    if g == "keno":
        return f"{p['picks']} number{'s' if p['picks'] != 1 else ''}"
    return plan_text(p)


def request_text(game_id: str, req: dict) -> str:
    """What a chat ask was, in his words: "10 mines", "daredevil", "going deeper", "100x"."""
    parts = []
    if "mines" in req:
        parts.append(f"{req['mines']} mine{'s' if req['mines'] != 1 else ''}")
    if "level" in req:
        parts.append(str(req["level"]).lower())
    if "risk" in req:
        parts.append(f"{str(req['risk']).lower()} risk")
    if "rows" in req:
        parts.append(f"{req['rows']} rows")
    if "target" in req:
        parts.append(f"{_num(float(req['target']))}x")
    if "roll_over" in req:
        parts.append(f"over {_num(float(req['roll_over']))}")
    if int(req.get("balls") or 1) > 1:
        parts.append(f"{req['balls']} balls")
    if req.get("allin"):
        parts.append(f"{req['depth']} deep" if req.get("depth") else "going way deep")
    if "picks" in req:
        parts.append(f"{req['picks']} number{'s' if req['picks'] != 1 else ''}")
    if req.get("push"):
        parts.append("hitting" if req["push"] == "hit" else "standing")
    if req.get("deeper"):
        parts.append("going deeper")
    return ", ".join(parts)


def plan_text(p: dict) -> str:
    """A few spoken words for a plan: "10 mines, 3 tiles", "daredevil, 2 hops", "100x"."""
    g = p.get("game")
    if g == "mines":
        d = p["depth"]
        return f"{p['mines']} mine{'s' if p['mines'] != 1 else ''}, {d} tile{'s' if d != 1 else ''}"
    if g == "hubies_road":
        s = p["steps"]
        return f"{p['level'].lower()}, {s} hop{'s' if s != 1 else ''}"
    if g == "plinko":
        b = int(p.get("balls") or 1)
        return f"{p['risk'].lower()} risk, {p['rows']} rows" + (f", {b} balls" if b > 1 else "")
    if g == "limbo":
        return f"{_num(p['target'])}x"
    if g == "dice":
        return f"over {_num(p['roll_over'])}"
    if g == "keno":
        return f"{p['risk'].lower()} risk, {p['picks']} number{'s' if p['picks'] != 1 else ''}"
    if g == "blackjack":
        return {"hit": "hitting", "stand": "standing"}.get(p.get("push") or "", "one hand")
    return ""


# ── chat requests ──
# Only game settings. Anything that mentions money or a bet size is not a request he takes.
MONEY = re.compile(r"\$|€|£|\b(bet\s*(size|amount|more|higher|max)|max\s*bet|all\s*in|usd|usdt|dollars?|bucks|cash|"
                   r"real|deposit|withdraw|tip|stake|wager|balance|money|crypto|btc|eth|sol)\b", re.I)
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "twenty": 20}
ALLIN = re.compile(r"\b(?:go(?:ing)?\s+)?all[\s-]*in\b")
DEEP_N = re.compile(r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten)\s*(?:tiles?\s+)?deep\b")
# a short command only: "hit", "stand pls", "hubie hit", "blackjack: stand!" (not "great hit" or chatter)
BJ_PUSH = re.compile(r"^(?:@?hubie[,:]?\s+|blackjack[,:]?\s+|bj[,:]?\s+)?(hit|stand)(?:\s+(?:me|it|pls|please|now|hubie))?"
                     r"\s*[!.]*$")
BALLS = re.compile(r"\b(\d{1,2}|two|three|four|five)\s*(?:plinko\s+)?balls?\b")
GAME_WORDS = {"mines": r"\bmines?\b", "hubies_road": r"\broad\b|\bhubie'?s road\b|\bhops?\b",
              "plinko": r"\bplinko\b|\brows?\b", "limbo": r"\blimbo\b", "dice": r"\bdice\b|\broll\b",
              "keno": r"\bkeno\b", "blackjack": r"\bblackjack\b|\bbj\b"}


def parse_request(text: str, current: str | None = None) -> tuple[str, dict] | None:
    """(game_id, request) for a concrete play ask in chat, else None. current = the original on screen now, used
    when the ask doesn't name a game ("go deeper", "100x")."""
    t = (text or "").lower()
    if not t.strip():
        return None
    # "go all in" / "9 deep": mines, deep on few mines. Only when the rest of the ask has no money in it and no
    # target (a bare "all in 100x" still reads as a bet size and is not taken).
    allin = bool(ALLIN.search(t))
    if allin:
        rest = ALLIN.sub(" ", t)
        if MONEY.search(rest) or re.search(r"\b\d+(?:\.\d+)?\s*x\b", rest):
            return None
        t = rest
    elif MONEY.search(t):
        return None
    m = DEEP_N.search(t)
    if allin or m:
        req: dict = {"allin": True}
        if m:
            d = WORDNUM.get(m.group(1)) or int(m.group(1))
            if not 2 <= d <= 24:
                return None
            req["depth"] = d
        mm = re.search(r"\b(\d{1,2}|one|two|three|five|ten|twenty)\s*mines?\b", t)
        if mm:
            n = WORDNUM.get(mm.group(1)) or int(mm.group(1))
            if not 1 <= n <= 23:
                return None
            req["mines"] = n
        return "mines", req
    named0 = [g for g, pat in GAME_WORDS.items() if re.search(pat, t)]
    # blackjack: "hit" / "stand" pushes his next decision (only on the table, or naming blackjack)
    if current == "blackjack" or "blackjack" in named0:
        m = BJ_PUSH.match(t.strip())
        if m:
            return "blackjack", {"push": m.group(1)}
    # keno: "keno 8 numbers", "pick 5" (on keno), "high risk keno"
    if "keno" in named0 or (current == "keno" and not named0):
        req = {}
        m = re.search(r"\b(?:pick\s*)?(10|[1-9]|one|two|three|four|five|six|seven|eight|nine|ten)\s*(?:numbers?|nums?|spots?|picks?)\b|"
                      r"\bpick\s*(10|[1-9]|one|two|three|four|five|six|seven|eight|nine|ten)\b", t)
        if m:
            v = m.group(1) or m.group(2)
            req["picks"] = WORDNUM.get(v) or int(v)
        m = re.search(r"\b(classic|low|medium|high)\b", t)
        if m:
            req["risk"] = m.group(1).capitalize()
        if req:
            return "keno", req
    # plinko burst: "drop 5 balls", "5 balls", "five balls"
    m = BALLS.search(t)
    if m:
        n = WORDNUM.get(m.group(1)) or int(m.group(1))
        if 2 <= n:
            req = {"balls": min(n, PLINKO_MAX_BALLS)}
            mr = re.search(r"\b(low|medium|high)\b", t)
            if mr:
                req["risk"] = mr.group(1).capitalize()
            mr = re.search(r"\b(8|9|1[0-6])\s*rows?\b", t)
            if mr:
                req["rows"] = int(mr.group(1))
            return "plinko", req
    named = [g for g, pat in GAME_WORDS.items() if re.search(pat, t)]
    deeper = bool(re.search(r"\b(go|dig)\s+deep(er)?\b|\bdeeper\b|\bmore (tiles|hops|steps)\b|\bfurther\b", t))
    # mines count: "10 mines", "go 10 mines", "ten mines"
    m = re.search(r"\b(\d{1,2}|one|three|five|ten|twenty)\s*mines?\b", t)
    if m:
        n = WORDNUM.get(m.group(1)) or int(m.group(1))
        if 1 <= n <= 24:
            return "mines", {"mines": n, **({"deeper": True} if deeper else {})}
    # road level
    m = re.search(r"\b(daredevil|hard|medium|easy)\b", t)
    if m and (m.group(1) == "daredevil" or "hubies_road" in named or (current == "hubies_road" and not named)):
        return "hubies_road", {"level": m.group(1).capitalize(), **({"deeper": True} if deeper else {})}
    # plinko risk / rows
    if "plinko" in named or (current == "plinko" and not named):
        req = {}
        m = re.search(r"\b(low|medium|high)\b", t)
        if m:
            req["risk"] = m.group(1).capitalize()
        m = re.search(r"\b(8|9|1[0-6])\s*rows?\b", t)
        if m:
            req["rows"] = int(m.group(1))
        if req:
            return "plinko", req
    # a target multiplier: limbo, or dice as a payout ("100x")
    m = re.search(r"\b(\d{1,4}(?:\.\d{1,2})?)\s*x\b", t)
    if m:
        x = float(m.group(1))
        game = "limbo" if "limbo" in named or (not named and current != "dice") else ("dice" if "dice" in named or current == "dice" else None)
        if game == "limbo" and 1.01 <= x <= 1000:
            return "limbo", {"target": x}
        if game == "dice" and 1.02 <= x <= 49.5:
            return "dice", {"roll_over": round(min(98.0, max(2.0, 100 - 99 / x)), 2)}
    m = re.search(r"\b(?:roll\s*)?over\s*(\d{1,2}(?:\.\d{1,2})?)\b", t)
    if m and ("dice" in named or current == "dice"):
        v = float(m.group(1))
        if 2 <= v <= 98:
            return "dice", {"roll_over": v}
    if deeper and current in ("mines", "hubies_road") and (not named or current in named):
        return current, {"deeper": True}
    return None


# ── blackjack: basic strategy, no splits ──
# Only hit, stand, and double when the server prices the double at 0. Pairs play as their total.

def card_value(card: str | None) -> int:
    """A card code ("TS", "AS", "6C", "10H") as the dealer's up-card value: 2-10, aces 11."""
    r = str(card or "")[:-1].upper()
    if r == "A":
        return 11
    if r in ("T", "J", "Q", "K", "10"):
        return 10
    try:
        return int(r)
    except ValueError:
        return 0


def hand_words(value: int, soft: bool) -> str:
    return f"soft {value}" if soft and value < 21 else str(value)


def dealer_words(up: int) -> str:
    return "an ace" if up == 11 else ("an 8" if up == 8 else f"a {up}")


def basic_move(value: int, soft: bool, up: int, can_double: bool) -> str:
    """hit / stand / double for one hand (dealer stands or hits soft 17 alike here: the difference is tiny)."""
    dbl = "double" if can_double else "hit"
    if soft:
        if value >= 19:
            return "stand"
        if value == 18:
            if 3 <= up <= 6:
                return "double" if can_double else "stand"
            return "stand" if up in (2, 7, 8) else "hit"
        if value == 17:
            return dbl if 3 <= up <= 6 else "hit"
        if value in (15, 16):
            return dbl if 4 <= up <= 6 else "hit"
        if value in (13, 14):
            return dbl if 5 <= up <= 6 else "hit"
        return "hit"
    if value >= 17:
        return "stand"
    if 13 <= value <= 16:
        return "stand" if 2 <= up <= 6 else "hit"
    if value == 12:
        return "stand" if 4 <= up <= 6 else "hit"
    if value == 11:
        return dbl if up != 11 else "hit"
    if value == 10:
        return dbl if 2 <= up <= 9 else "hit"
    if value == 9:
        return dbl if 3 <= up <= 6 else "hit"
    return "hit"


def bj_decide(hand: dict, dealer_up: str | None, options: dict, push: str | None = None) -> tuple[str, bool]:
    """(move, pushed): the move for this hand. A chat push (hit/stand) wins when the game allows it. Double only
    with two cards and the game's own double priced at 0 (doubleCost and quote 0)."""
    opts = options or {}
    if push in ("hit", "stand") and opts.get(push):
        return push, True
    up = card_value(dealer_up)
    can_double = bool(opts.get("double")) and len(hand.get("cards") or []) == 2 and \
        opts.get("doubleCost", 1) in (0, 0.0, "0") and opts.get("quote", 0) in (0, 0.0, "0")
    move = basic_move(int(hand.get("value") or 0), bool(hand.get("soft")), up, can_double)
    if not opts.get(move):
        move = "stand" if opts.get("stand") else "hit"
    return move, False
