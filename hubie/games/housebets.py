"""Housebets adapter: provider slots in the site's "Try fun mode", and the site's own house originals at a
zero bet, never real play.

Housebets' own originals (mines, dice, limbo ...) have no demo mode, but they take a 0 bet on the empty
USDT wallet and play the round out (found 2026-11-08, owner OK'd it): they are Hubie's main games now
(about 80% of play segments, HUBIE_HB_ORIGINALS_SHARE), slots the occasional extra. Their own safety
layers are described at ORIGINALS below. Slots: the site launches them in fun mode on the provider's
demo server (the owner picked this on 2026-11-06).

Money safety, in layers. A spin only happens when every layer says demo:
  1. The only site button this code ever clicks is "Try fun mode" (CLICKABLE_LABELS). Real Play,
     wallet, deposit and bonus-buy labels are refused by assert_clickable().
  2. The site's own Real/Fun radio must read fun=checked, real=unchecked.
  3. The launch request the page sent (observed, not made by us) must say play_mode "fun", and
     the launch URL and game frame must sit on the provider's demo host.
  4. The game's own init response must show a demo balance, never the account's real balance.
  5. The account's real balance in the site header is read before and after every round. If it
     ever moves, play stops for good (NotDemoError).
  6. Demo bonus buys (owner OK'd 2026-11-07, about one every HUBIE_HB_BONUS_BUY_MIN minutes, 0 = off) only
     happen after the same guard, and only through the game's own cheapest "BUY FREE SPINS" button and
     its confirm (GAME_CLICKS, in-game only, never SUPER or higher-priced options). Before the click the
     price on the button (read off the screen) must equal the cheapest buy the game's own init offers,
     and the CREDIT shown in the game must equal the demo balance. While buys are on, every game request
     carrying a purchase is checked in flight and blocked unless it is the one buy we armed. After the
     buy, the cost must come off the demo credit, never the real wallet.
Anything unsure -> GameError (no spin, orchestrator recovers or benches the game).
Anything that looks like real mode -> NotDemoError (orchestrator stops play for good).

Results come from the game's own gameService responses, observed passively in the browser.
Nothing here calls a site or provider API itself.

All Playwright work runs on one private thread with its own event loop, because the orchestrator
calls play_round and recover from different threads.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlparse

from . import plays
from .base import GameAdapter, GameError, GameGone, GameInfo, NotDemoError, RoundResult

log = logging.getLogger("hubie.games.housebets")

BASE = "https://www.housebets.com"
HB_TOOLS = Path(__file__).resolve().parents[2] / "tools" / "housebets"
PROFILE = os.environ.get("HUBIE_HB_PROFILE", "/root/hubie_stream/profiles/hb_profile_no2")
RELAY = os.environ.get("HUBIE_HB_RELAY", "http://127.0.0.1:8899")
DEBUG_PORT = int(os.environ.get("HUBIE_HB_DEBUG_PORT", "9232"))
DISMISS_CLICK = (640, 110)   # top banner of a Pragmatic game at 1280x720: no buttons
RECYCLE_MB = float(os.environ.get("HUBIE_HB_RECYCLE_MB", "2200"))   # fresh game Chrome at a switch above this
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
CHROME_LOG = os.environ.get("HUBIE_HB_CHROME_LOG", "/dev/shm/hubie_hb_chrome.log")
CHALLENGE_MARKS = ("Performing security verification", "Just a moment", "Verifying you are human")

# ── what may be clicked ───────────────────────────────────────────────

CLICKABLE_LABELS = ("Try fun mode", "Reject")          # Reject = the cookie banner
FORBIDDEN = re.compile(r"real\s*play|wallet|deposit|withdraw|buy|purchase|cash\s*out|log\s*out|"
                       r"switch\s*and\s*launch|claim|start your entry|tip|vault", re.I)


def assert_clickable(label: str) -> None:
    """The only gate every site button click goes through."""
    if label not in CLICKABLE_LABELS or FORBIDDEN.search(label):
        raise NotDemoError(f"refusing to click {label!r}")


TILE_PATH = re.compile(r"^/games/[a-z0-9-]+$")


def assert_tile_clickable(path: str, games) -> None:
    """Browsing: the only other site click is the tile of a game on our own list, by its exact link."""
    if not TILE_PATH.match(path or "") or path not in {f"/games/{g.slug}" for g in games}:
        raise NotDemoError(f"refusing to click tile {path!r}")


# What may be clicked inside the game frame (canvas, so by position, each one verified on screen
# first). Only the standard, cheapest feature buy and its confirm. Never SUPER, ante or anything else.
GAME_CLICKS = ("BUY FREE SPINS", "BUY CONFIRM")
GAME_FORBIDDEN = re.compile(r"super|ante|double|chance|bonus\s*bet|real|wallet|deposit|withdraw|cash|"
                            r"max|special|ultra|mega|x\d", re.I)


def assert_game_clickable(label: str) -> None:
    """The only gate every in-game click for a bonus buy goes through."""
    if label not in GAME_CLICKS or GAME_FORBIDDEN.search(label):
        raise NotDemoError(f"refusing to click {label!r} in the game")


# ── games ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BuySpec:
    """The game's standard "BUY FREE SPINS" button and its confirm, at 1280x720 with the game filling
    the window. Boxes are (x0, y0, x1, y1) screen areas read with OCR before each click."""
    button: tuple[int, int]
    price_box: tuple[int, int, int, int]        # the price printed on that button
    confirm: tuple[int, int] | None = None      # None until mapped live: the buy is then never confirmed
    dialog_text_box: tuple[int, int, int, int] | None = None   # the dialog's words (FREE SPINS, no SUPER)
    dialog_price_box: tuple[int, int, int, int] | None = None  # the price the dialog asks
    credit_box: tuple[int, int, int, int] = (300, 647, 440, 672)   # "CREDIT $100,000.00" value
    times_bet: int = 100                        # the cheapest buy costs this many bets
    verified: bool = True                       # a whole buy played live end to end; else tools only


@dataclass(frozen=True)
class SlotGame:
    id: str
    slug: str
    name: str
    how: str
    provider: str = "pragmatic"
    verified: bool = False                      # played live in fun mode end to end
    intro_click: tuple[int, int] | None = None  # at 1280x720 with the game filling the window
    spin_click: tuple[int, int] | None = (990, 650)   # Pragmatic's spin button at 1280x720; None -> Space
    buy: BuySpec | None = None                  # demo bonus buy; None -> this game just spins


GAMES: list[SlotGame] = [
    SlotGame("sweet_bonanza_1000", "pragmaticplay-sweet-bonanza-1000", "Sweet Bonanza 1000",
             "Candy slot, 8 or more matching symbols anywhere pay, wins tumble away, lollipops start free spins.",
             verified=True, intro_click=(1020, 465), spin_click=(990, 650),
             buy=BuySpec(button=(218, 68), price_box=(148, 78, 292, 112), confirm=(862, 481),
                         dialog_text_box=(420, 200, 1050, 410), dialog_price_box=(790, 355, 1015, 402))),
    SlotGame("gates_of_olympus_1000", "pragmaticplay-gates-of-olympus-1000", "Gates of Olympus 1000",
             "Zeus slot, 8 or more matching symbols anywhere pay, tumbles, random multiplier orbs.",
             intro_click=(1100, 440), verified=True,
             buy=BuySpec(button=(95, 140), price_box=(25, 153, 165, 192), confirm=(755, 400),
                         dialog_text_box=(430, 180, 860, 290), dialog_price_box=(505, 285, 785, 350))),
    SlotGame("gates_of_olympus_ss", "pragmaticplay-gates-of-olympus-super-scatter", "Gates of Olympus Super Scatter",
             "Zeus slot with super scatters, pay anywhere, tumbles and multiplier orbs.", verified=True),
    SlotGame("sugar_rush_1000", "pragmaticplay-sugar-rush-1000", "Sugar Rush 1000",
             "Cluster slot, wins leave multiplier spots behind that stack up.", verified=True,
             buy=BuySpec(button=(95, 85), price_box=(30, 98, 165, 124), confirm=(826, 450),
                         dialog_text_box=(330, 160, 940, 370), dialog_price_box=(555, 385, 730, 428),
                         verified=False)),   # 2026-11-07: bought fine, free spins stuck on "Loading Features"
    SlotGame("sweet_bonanza_ss", "pragmaticplay-sweet-bonanza-super-scatter", "Sweet Bonanza Super Scatter",
             "Candy slot with super scatters, pay anywhere and tumbles.", verified=True),
    SlotGame("fruit_party", "pragmaticplay-fruit-party", "Fruit Party",
             "Cluster slot, fruit clusters of 5 or more pay, random multipliers on wins.", verified=True),
    SlotGame("big_bass_bonanza", "pragmaticplay-big-bass-bonanza", "Big Bass Bonanza",
             "Fishing slot, money fish on the reels, the fisherman collects them in free spins."),
    SlotGame("madame_destiny_megaways", "pragmaticplay-madame-destiny-megaways", "Madame Destiny Megaways",
             "Fortune teller megaways slot, reel heights change every spin."),
    SlotGame("fortune_of_olympus", "pragmaticplay-fortune-of-olympus", "Fortune of Olympus",
             "Olympus cluster slot with multipliers."),
]
BY_ID = {g.id: g for g in GAMES}

# Provider demo hosts. A game frame or launch URL off these hosts is not trusted as demo.
DEMO_HOSTS = {
    "pragmatic": re.compile(r"(^|\.)demogamesfree\.[a-z0-9-]+\.[a-z]+$"),
}


def wallet_fingerprint(body: str) -> str | None:
    """The account's real balances, from the page's own GET /api/v1/wallet/balances."""
    import json
    try:
        cur = json.loads(body).get("currencies") or []
    except Exception:
        return None
    parts = sorted(f"{c.get('currency')}:{c.get('cash_balance')}:{c.get('bonus_balance')}" for c in cur)
    return "|".join(parts) or None


def wallet_only_grew(before: str, after: str) -> bool:
    """True when every real balance in `after` is >= the same one in `before` (same currencies) and at
    least one went up. Anything lower, missing, new or unreadable is False, so the guard still stops."""
    def parse(fp: str) -> dict[str, tuple[float, float]] | None:
        out = {}
        try:
            for part in fp.split("|"):
                cur, cash, bonus = part.rsplit(":", 2)
                out[cur] = (float(cash), float(bonus))
        except (ValueError, AttributeError):
            return None
        return out
    a, b = parse(before), parse(after)
    if not a or not b or set(a) != set(b):
        return False
    if any(b[c][i] < a[c][i] for c in a for i in (0, 1)):
        return False
    return any(b[c][i] > a[c][i] for c in a for i in (0, 1))


def is_launch_call(method: str, url: str) -> bool:
    """The page's own game launch (POST /api/v1/casino/launch), not /launch/currencies and the like."""
    u = urlparse(url)
    return method == "POST" and u.hostname == "www.housebets.com" and u.path.rstrip("/") == "/api/v1/casino/launch"


def demo_host_ok(provider: str, url: str) -> bool:
    pat = DEMO_HOSTS.get(provider)
    host = (urlparse(url).hostname or "").lower()
    return bool(pat and host and pat.search(host))


# ── Pragmatic gameService parsing (pure, tested offline) ──────────────

def parse_kv(text: str) -> dict[str, str]:
    """gameService bodies are a=b&c=d. Values may hold commas, never & or =."""
    out: dict[str, str] = {}
    for part in (text or "").split("&"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def num(v: str | None, default: float = 0.0) -> float:
    if v is None or v == "":
        return default
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return default


def parse_pur_init(v: str | None) -> list[float]:
    """purInit from the game's doInit, e.g. [{bet:2000,type:"default"},{bet:10000,type:"default"}]:
    the price of each feature buy in coin units (cost = coin value c x units)."""
    if not v:
        return []
    return [num(x) for x in re.findall(r"bet\"?\s*:\s*\"?([\d.]+)", unquote(v))]


def money_values(text: str) -> list[float]:
    """Money amounts read off the screen ("$200.00", "$1,000.00"; OCR often reads . as :)."""
    out = []
    text = re.sub(r"[OoQ]", "0", text or "")
    for m in re.finditer(r"(\d{1,3}(?:[,.: ]\d{3})*|\d+)[.:,](\d{2})(?!\d)", text):
        out.append(float(re.sub(r"[^\d]", "", m.group(1)) + "." + m.group(2)))
    return out


# Live feature events (2026-11-07): the round result only lands when the whole feature is over, so while free
# spins run the parser also emits small events the orchestrator can talk over. Totals are in base bets (c x l),
# also for a bought feature, so "25x" means the same thing either way.
FEATURE_MILESTONES = (10, 25, 50, 100, 250, 500, 1000)
BIG_MULTI = 10          # one multiplier symbol (Zeus orb, candy bomb) this big gets a line
BIG_SPIN = 20           # one free spin (all its tumbles) paying this many base bets gets a line


def parse_multipliers(res: dict[str, str]) -> list[tuple[str, int]]:
    """Multiplier symbols on the board in one gameService response: [(item, value)]. Pragmatic sends them as
    rmul=sym~pos~value;sym~pos~value (Gates of Olympus orbs, Sweet Bonanza bombs); the value is the last field.
    Anything that doesn't parse is ignored."""
    out = []
    for item in re.split(r"[;,]", unquote(res.get("rmul") or "")):
        parts = [x for x in item.split("~") if x != ""]
        if len(parts) >= 2:
            v = num(parts[-1], 0)
            if v >= 2 and v == int(v):
                out.append((item, int(v)))
    return out


@dataclass
class PragmaticRound:
    """Feeds on one game's request/response pairs and emits finished rounds.

    A round is everything from a fresh doSpin to the point the game is idle again: tumbles
    (rs_p present = the game will send the next doSpin itself), free spins (fs present), and
    the doCollect that pays a win out (na=c means a collect is due). A doSpin carrying pur=N is a
    feature buy: the round's bet is the buy's cost and the free spins that follow belong to it.
    """
    balance: float | None = None          # demo balance, last seen
    init_balance: float | None = None
    pur_units: list[float] = field(default_factory=list)   # feature buy prices from doInit, coin units
    coin: float | None = None             # coin value c, last seen
    lines: float | None = None            # l, last seen
    _open: dict | None = None
    done: list[dict] = field(default_factory=list)
    events: deque = field(default_factory=lambda: deque(maxlen=200))   # live feature events, drained by the adapter

    def _emit(self, kind: str, **data) -> None:
        self.events.append({"type": kind, "ts": time.time(), **data})

    def drain_events(self) -> list[dict]:
        out = []
        while self.events:
            try:
                out.append(self.events.popleft())
            except IndexError:
                break
        return out

    def _feature(self, r: dict, res: dict[str, str]) -> None:
        """Live events for one response of an open round. Only free spins talk; base-game tumbles stay quiet."""
        base = r.get("base_bet") or r["bet"]
        fsmax = int(num(res.get("fsmax") or res.get("fs_total")))
        if not r["free_spins"]:
            return
        if not r["f_on"]:
            r["f_on"], r["f_max"] = True, fsmax
            self._emit("feature_triggered", spins=fsmax, bought=r["bonus_buy"], bet=base)
        elif fsmax > r["f_max"]:
            added = int(num(res.get("fsmore"))) or fsmax - r["f_max"]
            r["f_max"] = fsmax
            self._emit("retrigger", added=added, spins=fsmax)
        fs = int(num(res.get("fs"))) if "fs" in res else None
        if fs is not None:
            r["f_played"] = max(r["f_played"], fs - 1)
        elif "fs_total" in res:
            r["f_played"] = max(r["f_played"], int(num(res.get("fs_total"))))
        left = max(0, r["f_max"] - r["f_played"])
        spin_key = fs if fs is not None else "last"
        for item, v in parse_multipliers(res):
            key = (spin_key, item)
            if key in r["f_multis"]:
                continue
            r["f_multis"].add(key)
            if v >= BIG_MULTI:
                self._emit("multiplier", value=v, left=left)
        if spin_key != r["f_spin"]:
            r["f_spin"], r["f_spin_win"], r["f_spin_said"] = spin_key, 0.0, False
        r["f_spin_win"] += num(res.get("w"))
        if base and not r["f_spin_said"] and r["f_spin_win"] >= BIG_SPIN * base and r["f_played"] > 0:
            r["f_spin_said"] = True
            self._emit("big_hit", x=round(r["f_spin_win"] / base, 1), win=round(r["f_spin_win"], 2), left=left)
        total = max(num(res.get("tw")), num(res.get("fswin_total")))
        if base:
            crossed = [m for m in FEATURE_MILESTONES if total >= m * base and m > r["f_mile"]]
            if crossed:
                r["f_mile"] = crossed[-1]
                self._emit("milestone", x=crossed[-1], total=round(total, 2), left=left)
        # live demo credit: Pragmatic keeps balance at the pre-feature value and pays the feature out at the end,
        # so the running credit is the balance before the round, less its cost, plus the round's win so far
        live = round(r["start"] - r["bet"] + total, 2) if r.get("start") is not None else None
        self._emit("progress", played=r["f_played"], spins=r["f_max"], left=left, total=round(total, 2), balance=live)

    def feed(self, request: str, response: str) -> dict | None:
        req, res = parse_kv(request), parse_kv(response)
        action = req.get("action", "")
        if action == "doInit":
            self.init_balance = self.balance = num(res.get("balance"), None)
            self.pur_units = parse_pur_init(res.get("purInit"))
            self.coin = num(res.get("c"), None)
            bls = (res.get("bls") or "").split(",")
            self.lines = num(res.get("l") or bls[0], None) if (res.get("l") or bls[0]) else None
            return None
        if action == "doSpin":
            if self._open is None:
                c, lines = num(res.get("c") or req.get("c")), num(res.get("l") or req.get("l"))
                self.coin, self.lines = c or self.coin, lines or self.lines
                bet = round(c * lines, 2)
                start = self.balance if self.balance is not None else num(res.get("balance")) + bet
                self._open = {"bet": bet, "start": start, "spins": 0, "tumbles": 0, "fs_max": 0,
                              "fs_played": 0, "free_spins": False, "tw": 0.0, "bonus_buy": False,
                              "f_on": False, "f_max": 0, "f_played": 0, "f_multis": set(), "f_spin": None,
                              "f_spin_win": 0.0, "f_spin_said": False, "f_mile": 0}
                if "pur" in req:
                    idx = int(num(req.get("pur"), -1))
                    expected = (round(c * self.pur_units[idx], 2)
                                if 0 <= idx < len(self.pur_units) and c else None)
                    paid = round(start - num(res.get("balance")), 2) if "balance" in res and start else None
                    cost = paid if paid else expected
                    self._open.update(bonus_buy=True, pur=idx, base_bet=bet, buy_expected=expected,
                                      buy_paid=paid, bet=cost or bet)
            r = self._open
            r["spins"] += 1
            if "rs_p" in res or "rs_t" in res:
                r["tumbles"] = max(r["tumbles"], int(num(res.get("rs_c") or res.get("rs_t"))))
            if "fs" in res or "fsmax" in res or "fs_total" in res:
                r["free_spins"] = True
                r["fs_max"] = max(r["fs_max"], int(num(res.get("fsmax") or res.get("fs_total"))))
                r["fs_played"] = max(r["fs_played"], int(num(res.get("fs") or res.get("fs_total"))))
            r["tw"] = max(r["tw"], num(res.get("tw")), num(res.get("fswin_total")))
            try:
                self._feature(r, res)
            except Exception as e:          # live words are a nicety: never cost the round result
                log.warning("feature event parse failed: %r", e)
            if "balance" in res:
                self.balance = num(res["balance"])
            in_tumble = "rs_p" in res
            in_fs = ("fs" in res and "fs_total" not in res)
            if res.get("na") == "s" and not in_tumble and not in_fs:
                return self._close()
            return None
        if action == "doCollect" or action.startswith("doCollect"):
            if "balance" in res:
                self.balance = num(res["balance"])
            if self._open is not None:
                return self._close()
            return None
        if "balance" in res:      # doBonus and friends: keep the balance honest
            self.balance = num(res["balance"])
        return None

    def cheapest_buy(self) -> tuple[int, float] | None:
        """(index, cost in demo money) of the cheapest feature buy the game offers, at the current coin."""
        if not self.pur_units or not self.coin:
            return None
        idx = min(range(len(self.pur_units)), key=lambda i: self.pur_units[i])
        return idx, round(self.coin * self.pur_units[idx], 2)

    def _close(self) -> dict:
        r, self._open = self._open, None
        end = self.balance if self.balance is not None else r["start"] - r["bet"]
        payout = round(end - (r["start"] - r["bet"]), 2)
        if payout < 0:            # never trust a negative; fall back to the game's own total
            payout = round(r["tw"], 2)
        if r.get("f_on"):
            base = r.get("base_bet") or r["bet"]
            self._emit("feature_end", total=payout, x=round(payout / base, 1) if base else 0.0,
                       multiplier=round(payout / r["bet"], 2) if r["bet"] else 0.0, spins=r["f_max"],
                       bought=r["bonus_buy"])
        out = {"bet": r["bet"], "payout": payout, "balance": round(end, 2),
               "multiplier": round(payout / r["bet"], 2) if r["bet"] else 0.0,
               "tumbles": r["tumbles"], "free_spins": r["free_spins"], "free_spins_count": r["fs_max"],
               "server_calls": r["spins"], "bonus_buy": r["bonus_buy"]}
        if r["bonus_buy"]:
            out.update(pur=r["pur"], base_bet=r["base_bet"], buy_cost=r["bet"], buy_expected=r["buy_expected"],
                       buy_paid=r["buy_paid"], free_spins_played=r["fs_played"])
        self.done.append(out)
        return out

    @property
    def busy(self) -> bool:
        return self._open is not None


# ── demo verdict (pure, tested offline) ───────────────────────────────

@dataclass
class DemoState:
    radios: dict[str, bool] = field(default_factory=dict)     # {"real": False, "fun": True}
    launch_modes: list[str] = field(default_factory=list)     # play_mode of every launch request seen
    launch_urls: list[str] = field(default_factory=list)      # launch_url of every launch response
    frame_url: str = ""                                       # the game frame
    provider: str = "pragmatic"
    demo_balance: float | None = None                         # from the game's own init
    real_balance_start: str | None = None                     # wallet fingerprint, first seen this session
    real_balance_now: str | None = None                       # wallet fingerprint, latest seen


def demo_verdict(s: DemoState) -> tuple[str, list[str]]:
    """'ok' only when every check passes. 'real' on positive evidence of real play (stop for good).
    'unsure' otherwise (skip the spin, recover)."""
    real, unsure = [], []
    if any(m != "fun" for m in s.launch_modes):
        real.append(f"a launch asked for play_mode {[m for m in s.launch_modes if m != 'fun']}")
    if s.radios.get("real") and s.frame_url:
        real.append("site toggle shows Real with a game loaded")
    if s.real_balance_start is not None and s.real_balance_now is not None \
            and s.real_balance_now != s.real_balance_start:
        real.append(f"real balance moved {s.real_balance_start} -> {s.real_balance_now}")
    if s.frame_url and urlparse(s.frame_url).hostname and not demo_host_ok(s.provider, s.frame_url):
        real.append(f"game frame is not on a demo host: {urlparse(s.frame_url).hostname}")
    for u in s.launch_urls:
        if not demo_host_ok(s.provider, u):
            real.append(f"launch url not on a demo host: {urlparse(u).hostname}")
    if real:
        return "real", real
    if s.radios.get("fun") is not True or s.radios.get("real") is not False:
        unsure.append(f"site toggle not on Fun: {s.radios}")
    if not s.launch_modes:
        unsure.append("no fun-mode launch seen for this game")
    if not s.frame_url:
        unsure.append("no game frame")
    if s.demo_balance is None:
        unsure.append("no demo balance from the game yet")
    elif s.real_balance_start and f":{s.demo_balance:.8f}:" in s.real_balance_start:
        unsure.append("game balance equals a real balance")
    elif s.demo_balance < 1000:
        unsure.append(f"game balance {s.demo_balance} does not look like a demo float")
    if s.real_balance_start is None or s.real_balance_now is None:
        unsure.append("real wallet balances not seen")
    return ("unsure", unsure) if unsure else ("ok", [])


# ── the private browser thread ────────────────────────────────────────

FILL_CSS = ("iframe.bg-game-surface{position:fixed!important;inset:0!important;width:100vw!important;"
            "height:100vh!important;z-index:2147483647!important;border:0!important}")
# HUBIE_HB_TOPBAR=1: the site's own top bar (logo, In Game, Wallet, prize) stays on screen above the game, so viewers
# see it's Housebets. The game iframe goes under it at 16:9, centred; the side margins get the site's own dark colour.
# All game coordinates below are measured with the game filling 1280x720 and go through GameView, so they keep working.
TOPBAR_H = 80        # the site's header, CSS px (shots/housebets/hb_game_authed.png: rows 0-79)
TOPBAR_BG = "#0b1920"


def topbar_on() -> bool:
    return os.environ.get("HUBIE_HB_TOPBAR", "0") == "1"


@dataclass(frozen=True)
class GameView:
    """Where the game iframe sits in the window: page = (x0 + x * s, y0 + y * s) for a point measured at 1280x720."""
    x0: float = 0.0
    y0: float = 0.0
    s: float = 1.0
    w: int = 1280
    h: int = 720

    @classmethod
    def make(cls, width: int, height: int, topbar: bool) -> "GameView":
        if not topbar:
            return cls(0.0, 0.0, 1.0, width, height)
        gh = height - TOPBAR_H
        gw = round(gh * 16 / 9)
        return cls(round((width - gw) / 2), TOPBAR_H, gh / 720, gw, gh)

    def pt(self, xy: tuple[int, int]) -> tuple[float, float]:
        return self.x0 + xy[0] * self.s, self.y0 + xy[1] * self.s

    def box(self, b: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        x0, y0 = self.pt(b[:2])
        x1, y1 = self.pt(b[2:])
        return int(x0), int(y0), int(round(x1)), int(round(y1))

    def css(self) -> str:
        if self.y0 == 0 and self.x0 == 0:
            return FILL_CSS
        return (f"iframe.bg-game-surface{{position:fixed!important;left:{self.x0:.0f}px!important;top:{self.y0:.0f}px!important;"
                f"right:auto!important;bottom:auto!important;width:{self.w}px!important;height:{self.h}px!important;"
                f"z-index:2147483647!important;border:0!important}}"
                f"body::after{{content:'';position:fixed;left:0;right:0;top:{self.y0:.0f}px;bottom:0;"
                f"background:{TOPBAR_BG};z-index:2147483646;pointer-events:none}}")


# The text in the top bar strip, with each piece's x. The bar is safe as is when it shows "In Game" and no amount left
# of the Wallet button (the prize widget, right of it, is fine); otherwise the DEMO PLAY patch must cover the balance.
TOPBAR_TEXT_JS = """(h) => { const out = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = w.nextNode())) { const t = n.textContent.trim(); if (!t) continue;
    const r = document.createRange(); r.selectNodeContents(n); const b = r.getBoundingClientRect();
    if (b.width > 0 && b.height > 0 && b.top < h && b.bottom > 0) out.push([t, b.left]); }
  return out; }"""
MONEY_RE = re.compile(r"\d[\d,]*\.\d{2}|[$€£]\s*\d")


def topbar_safe(items: list) -> tuple[bool, str]:
    """True only if the strip shows "In Game" and no amount left of the Wallet button."""
    try:
        texts = [(str(t), float(x)) for t, x in items]
    except (TypeError, ValueError):
        return False, "unreadable"
    if not any(t.lower() == "in game" for t, _ in texts):
        return False, "no In Game in the top bar"
    wallet = [x for t, x in texts if t.lower() == "wallet"]
    if not wallet:
        return False, "no Wallet button in the top bar"
    for t, x in texts:
        if x < wallet[0] and MONEY_RE.search(t):
            return False, f"amount in the top bar: {t[:20]}"
    return True, ""

RADIOS_JS = "Object.fromEntries([...document.querySelectorAll('[role=radiogroup] input[type=radio]')]" \
            ".filter(i=>i.value==='real'||i.value==='fun').map(i=>[i.value,i.checked]))"


def browse_on() -> bool:
    return os.environ.get("HUBIE_HB_BROWSE", "0") == "1"


# Covers the account's balance in the site header with a "DEMO PLAY" patch on every page of the site, from the
# first frame a page draws (an init script, so a full page load is covered too). The header shows the real
# balance everywhere except inside a game. The patch sits over the pill around any amount left of the Wallet
# button, re-placed on every DOM change and every animation frame. window.__hbPatchState says whether every
# amount found is under the patch; browsing is only shown on stream while it does.
PATCH_INIT_JS = r"""(() => { try {
  if (window.top !== window || window.__hbPatch) return; window.__hbPatch = 1;
  const H = 80, money = /\d[\d,]*\.\d{2}|[$€£]\s*\d|\d\.\d{4,}/;
  let box = null;
  const mk = () => { box = document.createElement('div'); box.id = '__hubie_patch';
    box.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;display:none;box-sizing:border-box;' +
      'background:#0f2730;border:1px solid #1f4650;border-radius:10px;color:#40e0d0;' +
      'font:700 14px/1 Poppins,Inter,Arial,sans-serif;letter-spacing:.08em;align-items:center;justify-content:center';
    box.textContent = 'DEMO PLAY'; document.documentElement.appendChild(box); };
  const scan = () => {
    if (!document.body) return { ok: true, n: 0 };
    if (!box || !box.isConnected) mk();
    const items = []; const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
    while ((n = w.nextNode())) { const t = n.textContent.trim(); if (!t) continue;
      const r = document.createRange(); r.selectNodeContents(n); const b = r.getBoundingClientRect();
      if (b.width > 0 && b.height > 0 && b.top < H && b.bottom > 0) items.push([t, b, n]); }
    const wallet = items.find(([t]) => t.toLowerCase() === 'wallet');
    const cut = wallet ? wallet[1].left : Infinity;
    let R = null, k = 0;
    for (const [t, b, n] of items) { if (!(b.left < cut && money.test(t))) continue; k++;
      let el = n.parentElement, best = b;
      while (el && el !== document.body) { const r = el.getBoundingClientRect(); if (r.width > 320 || r.height > 72) break; if (r.width > 0 && r.height > 0) best = r; el = el.parentElement; }
      R = R ? { l: Math.min(R.l, best.left), t: Math.min(R.t, best.top), r: Math.max(R.r, best.right), b: Math.max(R.b, best.bottom) }
            : { l: best.left, t: best.top, r: best.right, b: best.bottom }; }
    if (!R) { box.style.display = 'none'; return { ok: true, n: 0 }; }
    Object.assign(box.style, { display: 'flex', left: (R.l - 3) + 'px', top: (R.t - 3) + 'px',
      width: (R.r - R.l + 6) + 'px', height: (R.b - R.t + 6) + 'px' });
    return { ok: true, n: k, rect: R };
  };
  const tick = () => { try { window.__hbPatchState = scan(); } catch (e) { window.__hbPatchState = { ok: false, err: String(e) }; } };
  new MutationObserver(tick).observe(document, { subtree: true, childList: true, characterData: true });
  const loop = () => { tick(); requestAnimationFrame(loop); }; loop(); setInterval(tick, 250);
} catch (e) { window.__hbPatchState = { ok: false, err: String(e) }; } })();"""

# The patch really covers every amount: re-checked from outside before anything is shown.
PATCH_OK_JS = """() => { const s = window.__hbPatchState; if (!s || !s.ok) return false; if (!s.n) return true;
  const p = document.getElementById('__hubie_patch'); if (!p || p.style.display === 'none') return false;
  const b = p.getBoundingClientRect(), R = s.rect; if (!(R.r > R.l && R.b > R.t)) return false;
  return b.left <= R.l && b.top <= R.t && b.right >= R.r && b.bottom >= R.b; }"""

# Xvfb draws no pointer: a drawn arrow follows the real mouse events while Hubie browses.
CURSOR_JS = """(on) => { let c = document.getElementById('__hubie_cursor');
  if (!on) { if (c) c.remove(); return true; }
  if (c) return true;
  c = document.createElement('div'); c.id = '__hubie_cursor';
  c.style.cssText = 'position:fixed;left:0;top:0;width:24px;height:24px;z-index:2147483647;pointer-events:none;' +
    'transform:translate(640px,400px);filter:drop-shadow(0 1px 2px rgba(0,0,0,.6))';
  c.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24"><path d="M3 2 L3 19 L7.6 14.8 L10.8 21.6 L13.8 20.2 ' +
    'L10.7 13.5 L17 13.2 Z" fill="#fff" stroke="#0b1920" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  document.documentElement.appendChild(c);
  document.addEventListener('mousemove', e => { const k = document.getElementById('__hubie_cursor');
    if (k) k.style.transform = `translate(${e.clientX - 3}px,${e.clientY - 2}px)`; }, { capture: true, passive: true });
  return true; }"""

# ── a person's mouse ──────────────────────────────────────────────────


def _bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
            u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1])


def _ease(t: float) -> float:
    """Slow start, fast middle, slow landing (a smooth S)."""
    return t * t * (3 - 2 * t)


def human_path(x0: float, y0: float, x1: float, y1: float, rng: random.Random | None = None,
               overshoot: bool | None = None) -> list[tuple[float, float, float]]:
    """A hand's mouse move from (x0, y0) to exactly (x1, y1): a curve bent by random control points, eased in and
    out with a little speed variation and sub-pixel shake, now and then a short hesitation, and on longer moves
    sometimes a small overshoot past the target and a slower correction back. [(x, y, seconds before this point)],
    the last point is always (x1, y1)."""
    rng = rng or random
    dx, dy = x1 - x0, y1 - y0
    dist = (dx * dx + dy * dy) ** 0.5
    if dist < 2:
        return [(x1, y1, 0.0)]
    if overshoot is None:
        overshoot = dist > 140 and rng.random() < 0.3
    nx, ny = -dy / dist, dx / dist            # unit normal: the side the hand's arc bends to
    end = (x1, y1)
    if overshoot:
        k = min(16.0, max(4.0, dist * rng.uniform(0.03, 0.07)))
        end = (x1 + dx / dist * k + nx * rng.uniform(-4, 4), y1 + dy / dist * k + ny * rng.uniform(-4, 4))
    bend = dist * rng.uniform(0.06, 0.22) * rng.choice((-1, 1))
    c1 = (x0 + dx * rng.uniform(0.2, 0.4) + nx * bend * rng.uniform(0.6, 1.2),
          y0 + dy * rng.uniform(0.2, 0.4) + ny * bend * rng.uniform(0.6, 1.2))
    c2 = (x0 + dx * rng.uniform(0.6, 0.85) + nx * bend * rng.uniform(0.3, 0.9),
          y0 + dy * rng.uniform(0.6, 0.85) + ny * bend * rng.uniform(0.3, 0.9))
    # Fitts-like: longer moves take longer, never the same twice
    dur = (0.16 + 0.11 * math.log2(1 + dist / 30)) * rng.uniform(0.85, 1.25)
    n = max(8, min(60, int(dur * 60)))
    pts, hes = [], (rng.randint(n // 3, 2 * n // 3) if dist > 120 and rng.random() < 0.18 else -1)
    for i in range(1, n + 1):
        t = _ease(i / n)
        x, y = _bezier((x0, y0), c1, c2, end, t)
        if i < n:
            j = 0.6 * (1 - t)                 # shake, gone by the landing
            x, y = x + rng.uniform(-j, j), y + rng.uniform(-j, j)
        dt = dur / n * rng.uniform(0.75, 1.3)
        if i == hes:
            dt += rng.uniform(0.06, 0.16)
        pts.append((x, y, dt))
    if overshoot:
        m = rng.randint(5, 9)
        sx, sy = end
        cdur = rng.uniform(0.12, 0.22)
        for i in range(1, m + 1):
            t = _ease(i / m)
            pts.append((sx + (x1 - sx) * t, sy + (y1 - sy) * t, cdur / m * rng.uniform(0.8, 1.2) +
                        (rng.uniform(0.04, 0.1) if i == 1 else 0.0)))
    pts[-1] = (x1, y1, pts[-1][2])
    return pts


# Game tiles on the page: links to /games/<slug>. The target by its exact path; others only to hover.
TILES_JS = """(path) => { const norm = a => { try { const u = new URL(a.href, location.href);
    return u.origin === location.origin ? u.pathname.replace(/\/$/, '') : ''; } catch (e) { return ''; } };
  const all = [...document.querySelectorAll('a[href]')].filter(a => /^\/games\/[a-z0-9-]+$/.test(norm(a)) && norm(a) !== '/games/slots');
  const rect = a => { const r = a.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; };
  const big = a => { const r = a.getBoundingClientRect(); return r.width > 60 && r.height > 60; };
  const t = all.find(a => norm(a) === path && big(a));
  return { target: t ? rect(t) : null, total: all.length,
    others: all.filter(a => a !== t && big(a)).map(rect).filter(r => r[1] > 110 && r[1] + r[3] < 700 && r[0] > 80) }; }"""

# Is the point really on the target tile's link (nothing on top, not a neighbour)?
POINT_JS = """([x, y, path]) => { const el = document.elementFromPoint(x, y); const a = el && el.closest('a[href]');
  if (!a) return false; try { const u = new URL(a.href, location.href);
    return u.origin === location.origin && u.pathname.replace(/\/$/, '') === path; } catch (e) { return false; } }"""

SEARCH_JS = """() => { const i = document.querySelector('input[placeholder*="Search" i]'); if (!i) return false;
  i.focus(); i.value = ''; return document.activeElement === i; }"""


# ── House originals at a zero bet (owner OK'd 2026-11-08) ───────────────
#
# Housebets' own games (dice, limbo, plinko, mines, Hubie's Road) have no demo mode, but they take a bet of 0 on a
# wallet holding 0: the round plays out on screen and nothing moves. They run on the site page itself (open shadow
# roots under <dice-game> etc.); every bet is a POST to games.housebets.com/<api>/bet, every mines/road step a POST
# to /<api>/action. Money safety, in layers, any breach turns the originals off for good (GameGone; slots and talk
# carry on) and, if real money could have moved, stops all play (NotDemoError):
#   1. Clicks only through ORIGINAL_CLICKS: each element found by its own selector inside the game, its text checked
#      against the allow-listed pattern and ORIG_FORBIDDEN (max, 1/2, 2x, auto, wallet, deposit, switch ...), and
#      the point under the mouse proven to be that element before the click.
#   2. Before every bet: the site's currency pill reads USDT (the 0 wallet; USDC holds dust and is never used), the
#      game's own /limits answer says its wallet is USDT, and the bet field reads 0 (cleared and re-read if not).
#   3. While an original is loaded, every POST to games.housebets.com passes check_original_request(): a bet goes
#      through only with amount 0 in the USDT currency, an action only on a round we saw start at stake 0. Anything
#      else is aborted in flight.
#   4. Every answer (check_original_response): stake, payout and value 0, currency USDT, settled rounds with
#      round.stake 0 and round.payout 0. Any balance the game reads (/user/balance, sometimes) must not go down,
#      and neither may the account's wallet (each page load, and the site's live wallet feed over its websocket).

ORIGINALS_HOST = "games.housebets.com"
ORIGINALS_ROUTE = re.compile(r"^https://games\.housebets\.com/")
SAFE_SYMBOL = "USDT"


@dataclass(frozen=True)
class OriginalGame:
    id: str
    slug: str
    name: str
    how: str
    api: str                  # games.housebets.com/<api>/bet
    tag: str                  # the game's element on the page
    provider: str = "housebets"


ORIGINALS: list[OriginalGame] = [
    OriginalGame("hubies_road", "housegames-hubie-s-road", "Hubie's Road",
                 "Hubie hops lane to lane over the water, each safe lane pays more, a shark ends it. Easy to Daredevil. Cash out any time.",
                 "road", "road-game"),
    OriginalGame("mines", "housegames-mines", "Mines",
                 "5 by 5 grid hiding 1 to 20 mines, more mines pay more. Every gem pays more, a mine ends it. Cash out any time.",
                 "mines2", "mines2-game"),
    OriginalGame("dice", "housegames-dice", "Dice",
                 "A roll from 0 to 100 has to land over the target. The higher the target, the more it pays.",
                 "dice", "dice-game"),
    OriginalGame("limbo", "housegames-limbo", "Limbo",
                 "Pick a target multiplier, the game draws one, it has to reach the target.", "limbo", "limbo-game"),
    OriginalGame("plinko", "housegames-plinko", "Plinko",
                 "A ball bounces down 8 to 16 rows of pegs into a paying slot, the edges pay most, high risk pays the edges more.", "plinko2", "plinko2-game"),
    OriginalGame("keno", "housegames-keno", "Keno",
                 "Pick 1 to 10 numbers out of 40, the game draws 10, more hits pay more. Classic to High risk.",
                 "keno2", "keno2-game"),
    OriginalGame("blackjack", "housegames-blackjack", "Blackjack",
                 "One hand against the dealer: get closer to 21 than the dealer without going over. Hit or stand.",
                 "blackjack7", "blackjack7-game"),
]
ORIG_BY_ID = {g.id: g for g in ORIGINALS}
ORIG_BY_API = {g.api: g for g in ORIGINALS}

# label -> (selector inside the game element, pattern the element's text must match; None = an input, no text)
ORIGINAL_CLICKS = {
    "BET": ("button.hd-p-betbtn, button.hl-p-betbtn, button.hp-p-betbtn", r"Bet"),
    "GO": ("button.hs-p-mainbtn", r"Go"),
    "CASH OUT": ("button.hl-p-betbtn--cashout, button.hs-p-cashbtn", r"Cash out(\s+0(\.0+)?)?"),
    "TILE": ("button.hl-g-tile", r""),
    "BET AMOUNT": ('input[aria-label="Bet Amount"]', None),
    "ROLL OVER": ("input#hd-g-in-target", None),
    "LIMBO TARGET": ("button.hl-p-qt", r"(1\.5|2|5|10|100)x"),
    "LIMBO TARGET INPUT": ('input[aria-label="Target Multiplier"]', None),
    "PLINKO RISK": ("button.hp-p-riskbtn", r"Low|Medium|High"),
    "PLINKO ROWS": ("input#hp-p-rows", None),
    "ROAD LEVEL": ("button.hs-p-qt", r"Easy|Medium|Hard|Daredevil"),
    "MINES COUNT": ("select#hl-p-mines", None),
    # keno: the board's numbers (aria "Number 7") and the risk row; Clear table only to start a fresh pick
    "KENO NUMBER": ("button.hl-g-tile", r"\d{1,2}"),
    "KENO RISK": ("button.hl-p-qt", r"Classic|Low|Medium|High"),
    "KENO CLEAR": ("button.hl-btn-secondary", r"Clear table"),
    # blackjack: only hit, stand, and double while the server says doubling costs 0; split, insurance, side bets,
    # boxes, skip and cash out are never pressed. "No insurance" declines the offer.
    "BJ HIT": ("button", r"Hit"),
    "BJ STAND": ("button", r"Stand"),
    "BJ DOUBLE": ("button", r"(2x )?Double"),
    "BJ NO INSURANCE": ("button", r"No( insurance)?|Decline|No thanks"),
}
# Labels that may read a word ORIG_FORBIDDEN otherwise refuses: Double, only when the server prices it at 0
# (checked in _bj_move before the click; the request and answer guards check it again).
ORIG_FORBIDDEN_OK = {"BJ DOUBLE": re.compile(r"(2x )?Double")}
# Inputs that are only ever set to a game setting, with the values they may take (never an amount).
ORIG_SETTING_OK = {
    "MINES COUNT": lambda v: re.fullmatch(r"\d{1,2}", v) is not None and 1 <= int(v) <= 24,
    "PLINKO ROWS": lambda v: re.fullmatch(r"\d{1,2}", v) is not None and 8 <= int(v) <= 16,
    "LIMBO TARGET INPUT": lambda v: re.fullmatch(r"\d{1,4}(\.\d{1,2})?", v) is not None and 1.01 <= float(v) <= 1000,
    "ROLL OVER": lambda v: re.fullmatch(r"\d{1,2}(\.\d{1,2})?", v) is not None and 2 <= float(v) <= 98,
}
# Originals are shown a little smaller so the whole game, its bottom row (total profit / first gem) included, fits the
# 1280x720 window (at 1.0 the road and mines games run ~25 px past the bottom). Clicks stay exact: boxes and
# elementFromPoint both read the zoomed page. The DEMO PLAY patch sits outside <body>, unzoomed, over real pixels.
ORIG_ZOOM = os.environ.get("HUBIE_HB_ORIG_ZOOM", "0.9")
# Per game (by api), measured 2026-11-08 against the H game window's crop (VIEW_ORIGINAL, page y 86..674): at 0.9 the
# dice panel ends at y ~660 and the tops of its footer bar (Fair / Normal) peek in under it; at 0.85 the whole dice panel,
# footer included, sits inside. Road and mines show their Total profit row at 0.9 (y ~646), plinko its multiplier row
# (y ~648), limbo all of it. HUBIE_HB_ORIG_ZOOM_<API> (e.g. HUBIE_HB_ORIG_ZOOM_DICE=0.8) overrides one game.
ORIG_ZOOM_BY_API = {"dice": "0.85"}
# Look mix (HUBIE_LOOK=mix, the default) crops the panel down to y ~664 (VIEW_ORIGINAL_MIX in stage/desk.py): there dice
# at 0.9 ends above the footer bar (y ~668), at 0.85 the footer bar would sit inside the crop.
ORIG_ZOOM_BY_API_MIX: dict = {}


def orig_zoom(api: str) -> float:
    look = os.environ.get("HUBIE_LOOK", "mix").strip().lower()
    by = ORIG_ZOOM_BY_API if look in ("h", "desk1") else ORIG_ZOOM_BY_API_MIX
    v = os.environ.get(f"HUBIE_HB_ORIG_ZOOM_{api.upper()}") or by.get(api) or ORIG_ZOOM
    try:
        z = float(v)
    except (TypeError, ValueError):
        z = 0.9
    return z if 0.5 <= z <= 1.0 else 0.9
ORIG_FORBIDDEN = re.compile(r"max|½|2×|auto|deposit|withdraw|wallet|\btip|claim|switch|vault|real|buy|setting|"
                            r"log\s*out|currency|rotate|seed|half|double", re.I)


def assert_original_clickable(label: str, text: str = "", aria: str = "") -> None:
    """The only gate every click on a house original goes through."""
    if label not in ORIGINAL_CLICKS:
        raise GameGone(f"refusing to click {label!r} in an original")
    css, pattern = ORIGINAL_CLICKS[label]
    ok_word = ORIG_FORBIDDEN_OK.get(label)
    seen = text or ""
    if ok_word is not None and ok_word.fullmatch(seen.strip()) and not (aria or "").strip():
        seen = ""                                   # the one allowed reading of this label
    if ORIG_FORBIDDEN.search(seen) or ORIG_FORBIDDEN.search(aria or ""):
        raise GameGone(f"refusing to click {label!r}: it reads {text!r} / {aria!r}")
    if pattern is not None and not re.fullmatch(pattern, (text or "").strip()):
        raise GameError(f"{label} shows {text!r}, not {pattern!r}")


def _is_zero(v) -> bool:
    """0 as a number or a numeric string, nothing else (None, bools, junk are not zero)."""
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return v == 0
    if isinstance(v, str) and re.fullmatch(r"\s*0*\.?0*\s*", v) and re.search(r"0", v):
        return True
    return False


def bet_field_zero(value: str | None) -> bool:
    """The bet field reads 0: empty (its placeholder shows 0.00000000) or any spelling of zero."""
    v = (value or "").strip()
    return v == "" or bool(re.fullmatch(r"0+(\.0*)?|\.0+", v))


def assert_setting_ok(label: str, value: str) -> None:
    """A game setting typed or set into an allow-listed input: only the values that setting may take."""
    ok = ORIG_SETTING_OK.get(label)
    if ok is None or not ok(str(value)):
        raise GameGone(f"refusing to set {label!r} to {value!r}")


def outcome_class(mult: float) -> str:
    """A free round's result from its multiplier: above 1 a win, exactly 1 even, below 1 a loss."""
    m = round(float(mult or 0), 4)
    return "win" if m > 1 else ("even" if m == 1 else "loss")


def played_settings(g, bet: dict) -> dict:
    """The settings the server says the round was really played with (from the /bet answer)."""
    game = (bet or {}).get("game") or {}
    if g.api == "mines2":
        return {"mines": game.get("mines")}
    if g.api == "road":
        return {"level": str(game.get("difficulty") or "").capitalize() or None}
    if g.api == "plinko2":
        return {"risk": str(game.get("risk") or "").capitalize() or None, "rows": game.get("rows")}
    if g.api == "limbo":
        return {"target": _f(game.get("betNumber"), None)}
    if g.api == "dice":
        return {"roll_over": _f(game.get("rollOver", game.get("betNumber")), None)}
    if g.api == "keno2":
        spots = game.get("spots")
        return {"risk": str(game.get("risk") or "").capitalize() or None,
                "picks": len(spots) if isinstance(spots, list) else None}
    return {}


def plan_matches(plan: dict, played: dict) -> bool:
    """Every setting the plan named is what the round was played with."""
    for k, v in played.items():
        if k not in plan or plan[k] is None:
            continue
        if v is None:
            return False
        if isinstance(plan[k], (int, float)) and not isinstance(plan[k], bool):
            if abs(float(plan[k]) - float(v)) > 0.011:
                return False
        elif str(plan[k]).lower() != str(v).lower():
            return False
    return True


MONEY_KEYS = ("amount", "stake", "originalAmount", "betAmount", "value", "payout", "perfectPairs",
              "twentyOnePlusThree", "insurance", "cost")
# Any request key that names money (a side bet, a cost) must be 0 too, whatever the game calls it.
MONEY_KEY_LIKE = re.compile(r"amount|stake|cost|wager|pairs|three|side|insur|payout|value", re.I)
# Games whose every POST under /<api>/ (bet aside) is a step of a round: it must name a round seen starting at 0.
STRICT_ACTION_APIS = {"blackjack7"}
# Games whose Bet button is greyed out between rounds until the card is filled in (keno: no numbers picked yet).
IDLE_BET_DISABLED = {"keno2"}
POST_RISKY = re.compile(r"bet|cash|wager|stake|withdraw|deposit|tip|vault|claim|buy", re.I)


def check_original_request(url: str, body: str | None, zero_rounds: set, usdt_ids: set) -> tuple[bool, str]:
    """A POST to games.housebets.com, in flight. (True, "") lets it through; (False, why) aborts it."""
    path = urlparse(url).path.rstrip("/")
    last = path.rsplit("/", 1)[-1]
    first = path.strip("/").split("/", 1)[0]
    if first in STRICT_ACTION_APIS and last not in ("bet", "game", "fairness"):
        last = "action"
    if last not in ("bet", "action"):
        if POST_RISKY.search(path):
            return False, f"unknown money-like POST {path}"
        return True, ""
    import json
    try:
        d = json.loads(body or "")
    except Exception:
        return False, f"{path}: body not readable JSON"
    if not isinstance(d, dict):
        return False, f"{path}: body is not an object"
    for k in MONEY_KEYS:
        if k in d and not _is_zero(d[k]):
            return False, f"{path}: {k} is {d[k]!r}, not 0"
    for k, v in d.items():
        if MONEY_KEY_LIKE.search(k) and not _is_zero(v):
            return False, f"{path}: {k} is {v!r}, not 0"
    if "seats" in d and d["seats"] != 1:
        return False, f"{path}: {d['seats']!r} boxes, only 1 is played"
    if "currency" in d and d["currency"] not in usdt_ids:
        return False, f"{path}: currency {d['currency']!r} is not the zero wallet {sorted(usdt_ids)}"
    if last == "bet":
        if "amount" not in d:
            return False, f"{path}: no amount in the bet"
        if "currency" not in d:
            return False, f"{path}: no currency in the bet"
        return True, ""
    if d.get("roundId") not in zero_rounds:
        return False, f"{path}: action on round {d.get('roundId')!r}, not one seen starting at 0"
    return True, ""


def check_original_response(data: dict, usdt_ids: set) -> tuple[str, str]:
    """A bet/action/game answer. ("ok", "") or ("stop", why); "real" when a stake or payout above 0 shows
    real money may have moved."""
    if not isinstance(data, dict) or not isinstance(data.get("game"), dict):
        return "stop", "answer has no game"
    g = data["game"]
    if isinstance(g.get("hands"), list) and "stakes" in g:
        return _check_blackjack(data, usdt_ids)
    inner = g.get("game") if isinstance(g.get("game"), dict) else g     # dice/limbo/plinko nest the bet one deeper
    for k in ("amount", "originalAmount", "stake", "payout", "value"):
        if k in inner and not _is_zero(inner[k]):
            return "real", f"{k} {inner[k]!r} in the answer"
    if "stake" not in inner and "amount" not in inner:
        return "stop", "answer shows no stake"
    cur = inner.get("currency", g.get("currency"))
    if cur not in usdt_ids:
        return "stop", f"answer in currency {cur!r}, not {sorted(usdt_ids)}"
    rnd = data.get("round")
    settled = rnd is not None or inner.get("active") is False or g.get("phase") == "settled"
    if settled:
        if not isinstance(rnd, dict):
            return "stop", "settled answer without its round"
        for k in ("stake", "payout"):
            if not _is_zero(rnd.get(k)):
                return "real", f"round {k} {rnd.get(k)!r}"
    return "ok", ""


def _check_blackjack(data: dict, usdt_ids: set) -> tuple[str, str]:
    """A blackjack answer: every stake (main, side bets, each hand, the total), payout and insurance at 0."""
    g = data["game"]
    stakes = g.get("stakes")
    if not isinstance(stakes, dict) or not stakes:
        return "stop", "blackjack answer without its stakes"
    for k, v in stakes.items():
        if not _is_zero(v):
            return "real", f"stakes.{k} {v!r}"
    for k in ("staked", "payout"):
        if k not in g:
            return "stop", f"blackjack answer without {k}"
        if not _is_zero(g[k]):
            return "real", f"{k} {g[k]!r}"
    for h in g["hands"]:
        for k in ("stake", "payout"):
            if not isinstance(h, dict) or not _is_zero(h.get(k)):
                return "real", f"hand {k} {(h or {}).get(k)!r}"
    ins = g.get("insurance") or {}
    if not _is_zero(ins.get("payout", 0)) or (ins.get("taken") and not _is_zero(ins.get("cost", 0))):
        return "real", f"insurance {ins!r}"
    if g.get("currency") not in usdt_ids:
        return "stop", f"answer in currency {g.get('currency')!r}, not {sorted(usdt_ids)}"
    if g.get("active") is False or g.get("phase") == "settled":
        rnd = data.get("round")
        if not isinstance(rnd, dict):
            return "stop", "settled answer without its round"
        for k in ("stake", "payout"):
            if not _is_zero(rnd.get(k)):
                return "real", f"round {k} {rnd.get(k)!r}"
    return "ok", ""


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _num(v) -> str:
    return f"{_f(v):g}"


def original_outcome(g: OriginalGame, bet: dict, final: dict, picks: int = 0) -> dict:
    """What a finished original round did, from the game's own answers (bet = the /bet answer, final = the last
    answer of the round). multiplier is what a stake would have paid back (0 = lost)."""
    if g.api == "keno2":
        game = bet["game"]
        m = _f(game.get("multiplier"))
        picks, hits = len(game.get("spots") or []), int(_f(game.get("hits")))
        risk = str(game.get("risk") or "")
        return {"multiplier": m, "won": m > 1, "hits": hits, "picks": picks, "risk": risk,
                "outcome": f"keno {hits} hit{'s' if hits != 1 else ''} out of {picks}, {m:g}x" + (f", {risk} risk" if risk else "")}
    if g.api == "blackjack7":
        return blackjack_outcome(final)
    if g.api in ("dice", "limbo", "plinko2"):
        game = bet["game"]
        inner = game.get("game") or {}
        if g.api == "dice":
            m = _f(inner.get("payoutMultiplier"))
            mode = game.get("mode") or game.get("betDirection") or "over"
            roll, target = _f(game.get("outcome")), _f(game.get("rollOver"))
            text = f"rolled {roll:.2f}, needed {mode} {target:g}"
            return {"multiplier": m, "won": m > 1, "outcome": text, "roll": roll, "target": target, "mode": mode,
                    "win_chance": _f(game.get("winChance"))}
        if g.api == "limbo":
            m = _f(inner.get("payoutMultiplier"))
            drawn, target = _f(game.get("outcome")), _f(game.get("betNumber"))
            return {"multiplier": m, "won": m > 1, "outcome": f"limbo {drawn:g}x vs {target:g}x target",
                    "drawn": drawn, "target": target}
        m = _f(game.get("multiplier", inner.get("payoutMultiplier")))
        risk = game.get("risk") or ""
        return {"multiplier": m, "won": m > 1, "outcome": f"plinko {m:g}x" + (f", {risk} risk" if risk else ""),
                "bucket": game.get("bucket"), "risk": risk}
    fg = final["game"]
    hit = fg.get("hit") is not None or fg.get("outcome") in ("hit", "mine", "trap", "bust", "lose", "loss")
    m = 0.0 if hit else _f(fg.get("multiplier"))
    if g.api == "mines2":
        opened = fg.get("opened") or []
        gems = len([t for t in opened if t != fg.get("hit")])
        if hit:
            text = "mine on the first tile" if gems == 0 else f"{gems} gem{'s' if gems != 1 else ''} then a mine"
        else:
            text = f"{gems} gem{'s' if gems != 1 else ''} then cashed out {m:.2f}x"
        return {"multiplier": m, "won": m > 1, "outcome": text, "gems": gems, "picks": picks or gems + int(hit),
                "mines": fg.get("mines"), "first_click": hit and gems == 0}
    pos = int(_f(fg.get("position")))
    if hit:
        lane = fg.get("trapLane") or fg.get("hit") or pos + 1
        text = (f"road: hit on lane {lane}, the first hop" if pos == 0 else
                f"road: {pos} safe step{'s' if pos != 1 else ''}, hit on lane {lane}")
    else:
        text = f"road: {pos} step{'s' if pos != 1 else ''}, cashed out {m:.2f}x"
    return {"multiplier": m, "won": m > 1, "outcome": text, "steps": pos, "difficulty": fg.get("difficulty"),
            "first_click": hit and pos == 0}


BJ_OPEN_PHASES = ("playing", "insurance")     # a hand still to play: the insurance question comes first
BJ_PAYS = {"win": 2.0, "blackjack": 2.5, "push": 1.0, "lose": 0.0, "loss": 0.0, "bust": 0.0}


def bj_double_free(game: dict) -> bool:
    """Doubling here keeps every stake at 0: the game offers it, prices it at 0, and nothing is staked."""
    opts = game.get("options") or {}
    stakes = game.get("stakes") or {}
    return bool(opts.get("double")) and _is_zero(opts.get("doubleCost")) and _is_zero(opts.get("quote", 0)) \
        and bool(stakes) and all(_is_zero(v) for v in stakes.values()) and _is_zero(game.get("staked"))


def blackjack_outcome(final: dict) -> dict:
    """A settled blackjack hand from the game's last answer. multiplier is what the stake (doubled or not) would have
    paid back: a win 2x, a natural 2.5x, a push 1x, a loss 0."""
    g = final["game"]
    hand = (g.get("hands") or [{}])[0]
    dealer = g.get("dealer") or {}
    res = str(hand.get("outcome") or "").lower()
    natural = bool(hand.get("natural"))
    if natural and res == "win":
        res = "blackjack"
    if res not in BJ_PAYS:
        raise GameError(f"blackjack: unknown hand outcome {hand.get('outcome')!r}")
    m = BJ_PAYS[res]
    pv, dv = int(_f(hand.get("value"))), int(_f(dealer.get("value")))
    moves = [str(a) for a in (g.get("actions") or [])]
    doubled = "double" in moves
    if natural:
        text = "blackjack, dealt 21" + (", dealer had one too: push" if res == "push" else "")
    elif pv > 21:
        text = f"busted on {pv}"
    elif dv > 21:
        text = f"{pv} and the dealer bust with {dv}: win"
    else:
        text = f"{pv} vs dealer {dv}: " + {"win": "win", "push": "push"}.get(res, "loss")
    if doubled:
        text += ", doubled"
    return {"multiplier": m, "won": m > 1, "outcome": "blackjack " + text, "hand": pv, "dealer": dv,
            "moves": moves, "doubled": doubled, "natural": natural, "first_click": False}


def burst_outcome(outs: list[dict]) -> dict:
    """A plinko burst (several balls in one round), from each ball's original_outcome: the round pays the balls'
    average (what the stakes would have paid back in all), and the text lists every ball and the best one."""
    mults = [round(_f(o.get("multiplier")), 4) for o in outs]
    if not mults:
        raise GameError("plinko burst without a ball")
    avg = round(sum(mults) / len(mults), 4)
    best = max(mults)
    risk = next((o.get("risk") for o in outs if o.get("risk")), "")
    text = f"{len(mults)} balls: " + ", ".join(f"{m:g}x" for m in mults) + f", best {best:g}x"
    return {"multiplier": avg, "won": avg > 1, "outcome": text + (f", {risk} risk" if risk else ""),
            "balls": mults, "best": best, "burst": len(mults), "risk": risk}


def originals_from_env() -> list[str]:
    """HUBIE_HB_ORIGINALS: 1 (default) = all five, 0 = none, or a comma list of ids."""
    v = os.environ.get("HUBIE_HB_ORIGINALS", "1").strip()
    if v in ("", "0", "off"):
        return []
    if v in ("1", "on", "all"):
        return [g.id for g in ORIGINALS]
    return [x.strip() for x in v.split(",") if x.strip() in ORIG_BY_ID]


# The site's currency pill (left of Wallet): the icon's alt text of every money button up there.
CURRENCY_JS = """() => { const vis = b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < 80; };
  const w = [...document.querySelectorAll('button')].find(b => vis(b) && b.innerText.trim() === 'Wallet');
  const cut = w ? w.getBoundingClientRect().left : 1e9; const out = [];
  for (const b of document.querySelectorAll('button')) { if (!vis(b) || b.getBoundingClientRect().left >= cut) continue;
    if (!/\\d[.,]\\d/.test(b.innerText)) continue;
    out.push([...b.querySelectorAll('img')].map(i => i.alt).filter(Boolean).join(',')); }
  return out; }"""

# Is the point really on this element (deepest element through the open shadow roots)?
ON_TARGET_JS = """(el, [x, y]) => { let e = document.elementFromPoint(x, y);
  while (e && e.shadowRoot) { const d = e.shadowRoot.elementFromPoint(x, y); if (!d || d === e) break; e = d; }
  return !!e && (e === el || el.contains(e)); }"""


# What is at a point (deepest through open shadow roots), for the log when a click is refused.
WHAT_AT_JS = """([x, y]) => { let e = document.elementFromPoint(x, y);
  while (e && e.shadowRoot) { const d = e.shadowRoot.elementFromPoint(x, y); if (!d || d === e) break; e = d; }
  if (!e) return 'nothing'; return (e.tagName + '.' + String(e.className || '').slice(0, 60) + ' ' +
    (e.innerText || '').trim().slice(0, 30)).trim(); }"""

# The game element still in its logged-out look (the page draws that first, for ~2-4 s, until the session is read):
# a "Log in" / "Sign up" / "Register" button anywhere inside it, open shadow roots included.
ORIG_LOGGED_OUT_JS = """(g) => { const walk = (root) => { for (const e of root.querySelectorAll('button, a, [role=button]')) {
    const t = (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim();
    if (/^(log ?in|sign ?(in|up)|register)$/i.test(t)) return true; }
  for (const e of root.querySelectorAll('*')) if (e.shadowRoot && walk(e.shadowRoot)) return true; return false; };
  return walk(g.shadowRoot || g) || walk(g); }"""


class _Loop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="housebets-browser", daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except (TimeoutError, concurrent.futures.TimeoutError):
            fut.cancel()
            raise GameError(f"browser step gave up after {timeout}s")
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            raise GameError("browser step cancelled")


def bonus_every_s() -> float:
    """Seconds between demo bonus buys, from HUBIE_HB_BONUS_BUY_MIN (default 20, 0 = never)."""
    try:
        return max(0.0, float(os.environ.get("HUBIE_HB_BONUS_BUY_MIN", "20"))) * 60
    except ValueError:
        return 20 * 60


GAME_SERVICE_RE = re.compile(r".*/gameService.*")


class _BuySkipped(GameError):
    """A check before the buy click said no. Nothing was bought; the round spins as usual."""


# Game text is white or yellow on colour, drawn on a canvas. Each pass is (channel, threshold, scale,
# digits only); OCR is noisy, so a check passes when any pass reads exactly the expected amount.
OCR_PASSES = (("L", 200, 4, True), ("L", 180, 4, True), ("max", 200, 4, True), ("L", 220, 4, True),
              ("max", 220, 4, True), ("blue", 180, 4, False), ("L", 200, 4, False), ("max", 180, 2, True),
              ("blue", 150, 2, False), ("L", 180, 2, False), ("blue", 180, 2, False), ("blue", 210, 2, False),
              ("blue", 150, 4, False))
TEXT_PASSES = (("L", 180, 2, False), ("max", 200, 2, False), ("L", 200, 2, False))


def ocr_png(png: bytes, chan: str = "L", thr: int = 200, scale: int = 4, digits: bool = False,
            psm: str = "7") -> str:
    import io
    from PIL import Image, ImageChops, ImageOps
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    r, g, b = im.split()
    gray = {"L": im.convert("L"), "max": ImageChops.lighter(ImageChops.lighter(r, g), b), "blue": b}[chan]
    gray = ImageOps.invert(gray.point(lambda v: 255 if v > thr else 0))
    path = f"/dev/shm/hubie_hb_ocr_{os.getpid()}_{threading.get_ident()}.png"
    gray.save(path)
    args = ["tesseract", path, "-", "--psm", psm] + (["-c", "tessedit_char_whitelist=$0123456789.,"] if digits else [])
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def shows_amount(png: bytes, amount: float) -> tuple[bool, list[str]]:
    """True when some OCR pass reads exactly this amount in the picture."""
    seen = []
    for chan, thr, scale, digits in OCR_PASSES:
        text = ocr_png(png, chan, thr, scale, digits).strip()
        seen.append(text)
        if any(abs(v - amount) < 0.005 for v in money_values(text)):
            return True, seen
    return False, seen


def dialog_is_standard_buy(png: bytes) -> tuple[bool, list[str]]:
    """The confirm dialog names free spins and never SUPER (or any other special buy)."""
    seen = [ocr_png(png, chan, thr, scale, digits, psm="6").replace("\n", " ").strip()
            for chan, thr, scale, digits in TEXT_PASSES]
    if any(re.search(r"super|ultra|mega|special|\bx\s*\d", t, re.I) for t in seen):
        return False, seen
    return any(re.search(r"free\s*s\w{1,2}in", t, re.I) for t in seen), seen   # OCR: SPINS, SDINS, SBINS


class HousebetsSlotsAdapter(GameAdapter):
    name = "housebets"
    view = GameView()          # replaced per instance; class defaults keep half-built test adapters working
    _screen_ok = False
    browse = False
    _phase = "loading"
    round_timeout = 150.0      # tumbles and free spins run long; orchestrator reads this
    spin_timeout = 150.0       # a normal spin, tumbles and triggered free spins included
    buy_timeout = 270.0        # a bought feature: intro, 10+ free spins, total screen

    def __init__(self, display: str | None = None, width: int = 1280, height: int = 720,
                 games: list[str] | None = None, allow_unverified: bool = False, shots_dir: str | None = None,
                 originals: list[str] | None = None):
        self.display = display
        self.width, self.height = width, height
        wanted = games or [g.id for g in GAMES if g.verified or allow_unverified]
        self.games = [BY_ID[g] for g in wanted if g in BY_ID]
        # house originals at a zero bet (HUBIE_HB_ORIGINALS, default all five)
        self.originals = [ORIG_BY_ID[g] for g in (originals if originals is not None else originals_from_env())
                          if g in ORIG_BY_ID]
        self._orig_stop: list[str] = []          # why the originals are off for good (empty = on)
        self._orig_routed = False
        self._usdt_ids: set = set()              # the game server's currency number(s) for USDT, from /limits
        self._orig_limits: dict = {}             # api -> (currency, symbol) of the wallet the game plays from
        self._zero_rounds: set = set()           # mines/road rounds seen starting at stake 0
        self._orig_active: dict = {}             # api -> a round left open, from /<api>/game
        self._orig_game_seen: set = set()        # apis whose /game answer (open round or none) came since the load
        self._orig_q: list[dict] = []            # bet/action answers, in order
        self._orig_events: list[dict] = []       # live events for the orchestrator (blackjack decisions)
        self._orig_seen: list[str] = []          # every POST the route saw (path:ok/blocked)
        self._orig_balance: dict = {}            # currency -> balance the game read, first seen
        self._orig_balance_seen = 0
        self._ws_wallet: dict = {}
        self.orig_record: list | None = None     # tools: every original answer of the run
        self._resyncs = 0                        # game pages reloaded because screen and server disagreed
        self.shots_dir = shots_dir
        self.current: SlotGame | None = None
        self.state = DemoState()
        self.parser = PragmaticRound()
        self._xvfb = None
        self._chrome = None
        self._pw = None
        self._page = None
        self._L = _Loop()
        self._lock = asyncio.Lock()
        self._exchanges: list[tuple[str, str]] = []
        self._round_q: list[dict] = []
        self._hard_stop: list[str] = []
        self._wallet_first: str | None = None
        self._wallet_latest: str | None = None
        self._n = 0
        self.bonus_every = bonus_every_s()
        if self.bonus_every:
            self.round_timeout = self.buy_timeout     # the orchestrator waits this long for a round
        self._next_buy = time.monotonic() + self._buy_gap()
        self.force_bonus_buy = False   # tools: make the next round a buy if the game has one
        self.allow_unverified_buys = os.environ.get("HUBIE_HB_BUY_UNVERIFIED") == "1"
        self._buys_off: set[str] = set()   # games whose buy failed a check this session
        self._buy_armed: int | None = None  # the purchase index we are about to confirm, else None
        self._buy_seen: list[str] = []      # purchase requests seen in flight (index, allowed?)
        self.buys_done = 0
        self.record: list | None = None     # tools: every game exchange of the run, across reloads
        self.view = GameView.make(width, height, topbar_on())
        self._screen_ok = False             # game running in fun mode and, with the top bar, the bar is safe to show
        self.browse = browse_on()           # switch games by browsing the slots page like a person
        self._phase = "loading"             # loading / browsing / picked, for the stage while not ready
        self._mouse = (640.0, 400.0)
        self._mouse_home = self._mouse      # where the hand came to rest (the idle drift stays around it)
        self._mouse_t = 0.0                 # when the last real move ended
        self._drift_task = None

    # ── GameAdapter ──

    def start(self) -> None:
        self._L.run(self._start(), 180)

    def list_games(self) -> list[GameInfo]:
        out = [GameInfo(g.id, g.name, "slot", g.how) for g in self.games]
        if not self._orig_stop:
            out += [GameInfo(g.id, g.name, "original", g.how) for g in self.originals]
        return out

    def load_game(self, game_id: str) -> None:
        g = ORIG_BY_ID.get(game_id) if game_id in {o.id for o in self.originals} else BY_ID.get(game_id)
        if not g or (g not in self.games and g not in self.originals):
            raise GameError(f"unknown game {game_id}")
        if isinstance(g, OriginalGame) and self._orig_stop:
            raise GameGone("house originals off: " + self._orig_stop[0])
        self._L.run(self._load_any(g), 90)

    def screen_ready(self) -> bool:
        """The stream may show the game area: a game is loaded in fun mode and the top bar, if shown, reads
        "In Game" with no balance. Anything else and the stage keeps its switching card up."""
        return self._screen_ok and not self._hard_stop

    def topbar_report(self, shot: str | None = None) -> dict:
        """Tools: what the top bar check sees right now, plus a screenshot."""
        async def go():
            items = await self._page.evaluate(TOPBAR_TEXT_JS, max(self.view.y0, TOPBAR_H))
            patch = await self._page.evaluate("() => window.__hbPatchState || null")
            if shot:
                await self._page.screenshot(path=shot)
            return {"items": items, "safe": topbar_safe(items), "patch": patch, "patch_ok": await self._patched(),
                    "screen_ok": self._screen_ok, "state": self.screen_state()}
        return self._L.run(go(), 30)

    def screen_state(self) -> str:
        """ready (the game), browsing / picked (the site page, balance covered), else loading (the card)."""
        if self.screen_ready():
            return "ready"
        return self._phase if self._phase in ("browsing", "picked") else "loading"

    def poll_events(self) -> list[dict]:
        out = []
        try:
            out = [e for e in self.parser.drain_events()]
        except Exception:
            pass
        evs = getattr(self, "_orig_events", None)
        if evs:
            n = len(evs)
            out += evs[:n]
            del evs[:n]
        return out

    def _dump_feature(self, g: SlotGame, start: int) -> None:
        """Keeps the raw traffic of the last few feature rounds (data/feature_traffic, gitignored): real fixtures for
        the live feature events, e.g. which fields carry Zeus orbs and candy bombs."""
        d = os.environ.get("HUBIE_HB_FEATURE_DUMP", str(Path(__file__).resolve().parents[2] / "data" / "feature_traffic"))
        if not d or d == "0":
            return
        try:
            import json
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, f"{time.strftime('%Y%m%d_%H%M%S')}_{g.id}.json")
            with open(path, "w") as f:
                json.dump({"source": f"{g.name}, live feature round", "exchanges": self._exchanges[start:]}, f, indent=0)
            for old in sorted(Path(d).glob("*.json"))[:-8]:
                old.unlink(missing_ok=True)
        except Exception as e:
            log.warning("feature traffic not saved: %r", e)

    def is_demo(self) -> bool:
        """False only on positive evidence of real play. Uncertainty is handled per spin."""
        return not self._hard_stop

    def play_round(self, bet: float, options: dict | None = None) -> RoundResult:
        """bet is advisory: the slot keeps its own demo bet, and the result reports the real one.
        About every HUBIE_HB_BONUS_BUY_MIN minutes the round is a demo bonus buy instead (see _buy_due)."""
        if isinstance(self.current, OriginalGame):
            return self._L.run(self._play_original((options or {}).get("plan")), 100)
        return self._L.run(self._play(), self.round_timeout - 5)

    def _buy_gap(self) -> float:
        return self.bonus_every * random.uniform(0.9, 1.15) if self.bonus_every else float("inf")

    def _buy_due(self, g: SlotGame | None) -> bool:
        if g is None or g.buy is None or g.id in self._buys_off:
            return False
        if not g.buy.verified and not self.allow_unverified_buys:
            return False
        if self.force_bonus_buy:
            return True
        return bool(self.bonus_every) and time.monotonic() >= self._next_buy

    def wallet_check(self) -> tuple[str | None, str | None]:
        """(first, now) real wallet fingerprints, now = a fresh read from the site's own page load."""
        return self._L.run(self._wallet_check(), 60)

    async def _wallet_check(self):
        async with self._lock:
            await self._orig_route_off()
            self._wallet_latest = None
            self._screen_ok = False
            await self._page.goto(f"{BASE}/games/slots", wait_until="domcontentloaded", timeout=45000)
            for _ in range(20):
                if self._wallet_latest is not None:
                    break
                await self._page.wait_for_timeout(1000)
            self.current = None
            return self._wallet_first, self._wallet_latest

    def recover(self) -> None:
        try:
            self._L.run(self._recover(), 150)
        except Exception as e:
            log.warning("recover failed: %r", e)

    def close(self) -> None:
        try:
            self._L.run(self._close(), 30)
        except Exception:
            pass
        self._kill_procs()

    # ── processes ──

    def _ensure_display(self) -> str:
        def live(d):
            return bool(d) and os.path.exists(f"/tmp/.X11-unix/X{d.lstrip(':').split('.')[0]}")
        if self.display:
            self.used_display = self.display
            return self.display              # the stream's display: the watchdog owns it
        if live(os.environ.get("DISPLAY")):
            self.used_display = os.environ["DISPLAY"]
            return os.environ["DISPLAY"]
        if not shutil.which("Xvfb"):
            raise GameError("no display and no Xvfb")
        d = next(f":{n}" for n in range(94, 140) if not live(f":{n}") and not os.path.exists(f"/tmp/.X{n}-lock"))
        self._xvfb = subprocess.Popen(["Xvfb", d, "-screen", "0", f"{self.width}x{self.height}x24", "-nolisten", "tcp"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(50):                 # Chrome exits at once if the X socket is not there yet
            if os.path.exists(f"/tmp/.X11-unix/X{d[1:]}"):
                break
            time.sleep(0.2)
        time.sleep(0.5)
        self.used_display = d
        return d

    def _relay_up(self) -> None:
        sys.path.insert(0, str(HB_TOOLS))
        try:
            import hb_browser   # noqa: the other session's tested relay starter
            hb_browser.relay_up()
        finally:
            sys.path.remove(str(HB_TOOLS))

    def _launch_chrome(self) -> None:
        disp = self._ensure_display()
        for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                os.remove(os.path.join(PROFILE, n))
            except OSError:
                pass
        env = dict(os.environ, DISPLAY=disp)
        self._chrome = subprocess.Popen([
            "google-chrome", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
            f"--proxy-server={RELAY}", f"--user-data-dir={PROFILE}", f"--user-agent={UA}",
            "--kiosk", "--window-position=0,0", f"--window-size={self.width},{self.height}",
            "--no-first-run", "--hide-crash-restore-bubble", "--autoplay-policy=no-user-gesture-required",
            "about:blank"], env=env, stdout=subprocess.DEVNULL, stderr=open(CHROME_LOG, "ab"), start_new_session=True)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
                return
            except Exception:
                time.sleep(0.5)
        raise GameError("chrome did not open its debug port")

    def _kill_procs(self) -> None:
        for p in (self._chrome, self._xvfb):
            if p and p.poll() is None:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(p.pid, sig)
                        p.wait(3)
                        break
                    except Exception:
                        pass
        self._chrome = None
        self._xvfb = None

    # ── async side ──

    @staticmethod
    def _tree_mb() -> float:
        try:
            from hubie.supervisor import tree_rss_mb
            return tree_rss_mb(os.getpid())
        except Exception:
            return 0.0

    async def _start(self):
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self):
        from playwright.async_api import async_playwright
        await asyncio.to_thread(self._relay_up)
        await asyncio.to_thread(self._launch_chrome)
        self._pw = await async_playwright().start()
        browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = browser.contexts[0]
        self._page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            await ctx.add_init_script(PATCH_INIT_JS)    # balance patch on every site page, from its first frame
        except Exception as e:
            log.warning("balance patch not installed: %r", e)
        ctx.on("request", self._on_request)
        ctx.on("response", self._on_response)
        self._orig_routed = False
        if self._drift_task is None or self._drift_task.done():
            self._drift_task = asyncio.ensure_future(self._drift_loop())
        try:
            self._page.on("websocket", self._on_ws)
        except Exception as e:
            log.warning("live wallet feed not watched: %r", e)

    async def _route_game(self, route):
        """While a buy is under way (buy button to the buy's answer), every game request passes here. A purchase (pur=N) goes through only when it is
        the cheapest buy we armed a moment ago, on a demo host, with no ante. Anything else is blocked."""
        req = route.request
        try:
            kv = parse_kv(req.post_data or "")
        except Exception:
            kv = {"pur": "unreadable"}
        if "pur" not in kv:
            await route.continue_()
            return
        ok = (self._buy_armed is not None and kv.get("pur") == str(self._buy_armed)
              and self.current is not None and demo_host_ok(self.current.provider, req.url)
              and kv.get("bl", "0") == "0")
        self._buy_seen.append(f"{kv.get('pur')}:{'ok' if ok else 'blocked'}")
        if ok:
            self._buy_armed = None          # one purchase per arming
            self.buys_done += 1
            await route.continue_()
            return
        log.error("blocked a feature purchase request %s (armed %s)", kv.get("pur"), self._buy_armed)
        if self.current:
            self._buys_off.add(self.current.id)
        if self.current is None or not demo_host_ok(self.current.provider, req.url):
            self._hard_stop.append(f"purchase request to non-demo host {urlparse(req.url).hostname}")
        await route.abort()

    def _on_request(self, req):
        try:
            if is_launch_call(req.method, req.url):
                import json
                body = json.loads(req.post_data or "{}")
                self.state.launch_modes.append(str(body.get("play_mode")))
                if body.get("play_mode") != "fun":
                    self._hard_stop.append(f"launch with play_mode {body.get('play_mode')!r}")
        except Exception as e:
            log.warning("launch request not readable: %r", e)
            self.state.launch_modes.append("unreadable")

    def _on_response(self, resp):
        url = resp.url
        if is_launch_call(resp.request.method, url):
            asyncio.ensure_future(self._read_launch(resp))
        elif url.startswith(BASE + "/api/v1/wallet/balances"):
            asyncio.ensure_future(self._read_wallet(resp))
        elif "/gameService" in url:
            asyncio.ensure_future(self._read_game(resp))
        elif urlparse(url).hostname == ORIGINALS_HOST:
            asyncio.ensure_future(self._read_original(resp))

    async def _read_launch(self, resp):
        try:
            import json
            body = json.loads(await resp.text())
            self.state.launch_urls.append(body.get("launch_url", ""))
            if body.get("play_mode") != "fun":
                self._hard_stop.append(f"launch answered play_mode {body.get('play_mode')!r}")
        except Exception as e:
            log.warning("launch response not readable: %r", e)

    async def _read_wallet(self, resp):
        try:
            fp = wallet_fingerprint(await resp.text())
        except Exception:
            return
        if not fp:
            return
        if self._wallet_first is None:
            self._wallet_first = fp
        elif fp != self._wallet_first:
            if wallet_only_grew(self._wallet_first, fp):
                # a credit from outside (gift, rakeback, a deposit): demo play can't add real money, and it
                # can't be a real bet either. New baseline (owner 2026-11-07: a $20 gift froze the preview)
                log.warning("real wallet went up from outside, new baseline: %s -> %s", self._wallet_first, fp)
                self._wallet_first = fp
            else:
                self._hard_stop.append("real wallet balances changed during the session")
        self._wallet_latest = fp

    async def _read_game(self, resp):
        if isinstance(self.current, OriginalGame):
            return          # a slot answer still in flight from the page we left
        try:
            req, text = resp.request.post_data or "", await resp.text()
        except Exception as e:
            log.warning("game response not readable: %r", e)
            return
        if not demo_host_ok(self.current.provider if self.current else "pragmatic", resp.url):
            self._hard_stop.append(f"game traffic from non-demo host {urlparse(resp.url).hostname}")
            return
        self._exchanges.append((req, text))
        if self.record is not None:
            self.record.append((req, text))
        done = self.parser.feed(req, text)
        if self.parser.init_balance is not None and self.state.demo_balance is None:
            self.state.demo_balance = self.parser.init_balance
        if done:
            self._round_q.append(done)

    async def _text(self) -> str:
        return await self._page.evaluate("document.body ? document.body.innerText : ''")

    async def _click_label(self, label: str, timeout=4000) -> bool:
        assert_clickable(label)
        loc = self._page.get_by_role("button", name=label, exact=True)
        if await loc.count() != 1:
            return False
        if (await loc.inner_text()).strip() != label:
            raise NotDemoError(f"button text does not match {label!r}")
        await loc.click(timeout=timeout)
        return True

    async def _pass_challenge(self) -> None:
        sys.path.insert(0, str(HB_TOOLS))
        try:
            import hb_browser
            ok = await hb_browser._pass_challenge(self._page)
        finally:
            sys.path.remove(str(HB_TOOLS))
        if not ok:
            raise GameError("site security check did not clear")

    def _game_frame(self):
        for f in self._page.frames:
            if f is not self._page.main_frame and demo_host_ok(self.current.provider, f.url) and "html5Game" in f.url:
                return f
        return None

    async def _snapshot(self) -> None:
        s = self.state
        s.radios = await self._page.evaluate(RADIOS_JS)
        s.real_balance_start, s.real_balance_now = self._wallet_first, self._wallet_latest
        f = self._game_frame()
        others = [x.url for x in self._page.frames if "gs2c" in x.url or "html5Game" in x.url]
        s.frame_url = f.url if f else (others[0] if others else "")
        s.provider = self.current.provider

    async def _load_any(self, g):
        if isinstance(g, OriginalGame):
            await self._load_original(g)
        else:
            await self._load(g)

    async def _load(self, g: SlotGame):
        try:
            await self._load_locked(g)
        finally:
            self._phase = "loading"     # browsing is over either way: a running game shows, anything else is carded

    async def _load_locked(self, g: SlotGame):
        async with self._lock:
            if self._page is not None and self._tree_mb() > RECYCLE_MB:
                # the game Chrome only grows (~1 GB/hour of slots); a fresh one at a game switch, behind the
                # switching card, beats the watchdog killing everything mid-round at its limit
                log.info("game switch: browser at %.0f MB, starting a fresh one", self._tree_mb())
                await self._close()
                await asyncio.to_thread(self._kill_procs)
                self._page = None
            if self._page is None:
                await self._start_locked()
            await self._orig_route_off()
            self.current = g
            self._screen_ok = False
            self.state = DemoState(provider=g.provider)
            self.parser = PragmaticRound()
            self._round_q.clear()
            self._exchanges.clear()
            self._wallet_latest = None
            self._phase = "loading"
            browsed = False
            if self.browse:
                try:
                    browsed = await asyncio.wait_for(self._browse_to(g), 35)
                except NotDemoError:
                    raise
                except Exception as e:
                    log.warning("%s: browsing failed (%r), going straight to the game", g.name, e, exc_info=True)
                if not browsed:
                    self._phase = "loading"
                    await self._cursor(False)
            if not browsed:
                await self._page.goto(f"{BASE}/games/{g.slug}", wait_until="domcontentloaded", timeout=60000)
            await self._page.wait_for_timeout(2500 if browsed else 5000)
            page_text = await self._text()
            if any(m in page_text for m in CHALLENGE_MARKS):
                await self._pass_challenge()
            try:
                await self._click_label("Reject", 1500)
            except Exception:
                pass
            for _ in range(15):          # the page fetches the wallet itself; we only listen
                if self._wallet_latest is not None:
                    break
                await self._page.wait_for_timeout(1000)
            if self._wallet_latest is None:
                await self._fail_shot(g)
                raise GameError("real wallet balances not seen (logged out?); refusing to launch")
            if browsed:
                await self._hover_label("Try fun mode")
            if not await self._click_label("Try fun mode"):
                raise GameError(f"{g.name}: no Try fun mode button, skipping")
            if browsed:
                await self._cursor(False)
            for _ in range(40):           # wait for the game's own init (demo balance)
                await self._page.wait_for_timeout(1000)
                if self.state.demo_balance is not None and self._game_frame():
                    break
            await self._page.add_style_tag(content=self.view.css())
            await self._page.wait_for_timeout(7000)       # art finishes loading after the game's init
            await self._snapshot()
            verdict, why = demo_verdict(self.state)
            if verdict == "real":
                self._hard_stop.extend(why)
                raise NotDemoError("; ".join(why))
            if verdict != "ok":
                await self._fail_shot(g)
                raise GameError(f"{g.name} not verified demo: {'; '.join(why)}")
            if g.intro_click:
                await self._page.mouse.click(*self.view.pt(g.intro_click))
            else:
                await self._press_space()
            await self._page.wait_for_timeout(2500)
            if self.shots_dir:
                try:
                    await self._page.screenshot(path=os.path.join(self.shots_dir, f"hb_loaded_{g.id}.png"))
                except Exception:
                    pass
            await self._check_screen()
            log.info("loaded %s in fun mode, demo balance %s", g.name, self.state.demo_balance)

    # ── browsing to a game like a person ──

    async def _cursor(self, on: bool) -> None:
        try:
            await self._page.evaluate(CURSOR_JS, on)
        except Exception:
            pass

    async def _patched(self) -> bool:
        try:
            return bool(await self._page.evaluate(PATCH_OK_JS))
        except Exception:
            return False

    async def _show_if_patched(self, phase: str) -> None:
        """The stage shows the page only while the balance patch is proven in place."""
        self._phase = phase if await self._patched() else "loading"

    def _clamp(self, x: float, y: float) -> tuple[float, float]:
        """Below the header (its balance menu opens on hover), clear of the icon rail and the edges."""
        return min(max(x, 90.0), self.width - 20.0), min(max(y, 110.0), self.height - 20.0)

    async def _move(self, x: float, y: float, pause: tuple[float, float] = (0.0, 0.0)) -> None:
        """A visible mouse move the way a hand makes it (human_path), kept below the header. It ends exactly on
        (x, y): every click is sent at the point that was checked."""
        x, y = self._clamp(x, y)
        x0, y0 = self._mouse
        for px, py, dt in human_path(x0, y0, x, y):
            px, py = self._clamp(px, py)
            await self._page.mouse.move(px, py)
            await asyncio.sleep(dt)
        self._mouse = self._mouse_home = (x, y)
        self._mouse_t = time.monotonic()
        if pause[1]:
            await asyncio.sleep(random.uniform(*pause))

    async def _drift_loop(self) -> None:
        """While nothing else is using the mouse (the adapter lock is free) and an original is on screen, the
        resting hand drifts a pixel or three now and then, the way a mouse never sits perfectly still."""
        while True:
            await asyncio.sleep(random.uniform(0.7, 2.2))
            try:
                if (self._page is None or self._lock.locked() or not isinstance(self.current, OriginalGame)
                        or not self._screen_ok or time.monotonic() - self._mouse_t < 0.8):
                    continue
                async with self._lock:
                    x0, y0 = self._mouse
                    hx, hy = self._mouse_home
                    # small steps around where it came to rest, never wandering off
                    tx = min(max(x0 + random.uniform(-3, 3), hx - 7), hx + 7)
                    ty = min(max(y0 + random.uniform(-2, 2), hy - 5), hy + 5)
                    tx, ty = self._clamp(tx, ty)
                    for i in range(1, random.randint(3, 6) + 1):
                        await self._page.mouse.move(x0 + (tx - x0) * i / 6, y0 + (ty - y0) * i / 6)
                        await asyncio.sleep(random.uniform(0.03, 0.07))
                    await self._page.mouse.move(tx, ty)
                    self._mouse = (tx, ty)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    async def _rest_mouse(self, g: OriginalGame) -> None:
        """After a round, sometimes the hand drifts off the button to rest somewhere over the game instead of
        hovering exactly on it (it comes back for the next click)."""
        try:
            if random.random() < 0.5:
                b = await self._game_el().first.bounding_box()
                if b:
                    if random.random() < 0.6:     # over the play area (the right part of the game)
                        x = b["x"] + b["width"] * random.uniform(0.45, 0.85)
                        y = b["y"] + b["height"] * random.uniform(0.3, 0.75)
                    else:                         # just off the button, a little lower and aside
                        x, y = self._mouse[0] + random.uniform(-90, 90), self._mouse[1] + random.uniform(40, 130)
                    await self._move(x, y)
            self._mouse_home = self._mouse
        except (GameGone, NotDemoError):
            raise
        except Exception:
            pass

    async def _hover_label(self, label: str) -> None:
        assert_clickable(label)
        try:
            b = await self._page.get_by_role("button", name=label, exact=True).first.bounding_box(timeout=3000)
            if b:
                await self._move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2, (0.5, 0.9))
        except Exception:
            pass

    async def _browse_to(self, g) -> bool:
        """Slots page (house games page for an original), a slow scroll, a hover over one or two other tiles, then
        the game's own tile. Clicks only that tile (its exact link, from our game list); anything else is hover or
        scroll. False = not found."""
        path = f"/games/{g.slug}"
        listed = [*self.games, *self.originals]
        assert_tile_clickable(path, listed)
        page = self._page
        await page.goto(BASE + ("/house-games" if isinstance(g, OriginalGame) else "/games/slots"),
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1500)
        text = await self._text()
        if any(m in text for m in CHALLENGE_MARKS):
            return False
        try:
            await self._click_label("Reject", 1500)
        except Exception:
            pass
        await self._cursor(True)
        self._mouse = (640.0, 400.0)
        await self._move(700, 420)
        await self._show_if_patched("browsing")
        t_end = time.monotonic() + 22
        info = await page.evaluate(TILES_JS, path)
        for _ in range(random.randint(1, 2)):         # look at a tile or two first, hover only
            if info["others"]:
                x, y, w, h = random.choice(info["others"])
                await self._move(x + w / 2, y + h / 2, (0.7, 1.3))
        for _ in range(random.randint(1, 2)):         # a slow look further down the list, whatever is on top
            await page.mouse.wheel(0, random.randint(280, 420))
            await asyncio.sleep(random.uniform(1.2, 1.9))
            await self._show_if_patched("browsing")
        searched = False
        while time.monotonic() < t_end:
            await self._show_if_patched("browsing")
            info = await page.evaluate(TILES_JS, path)
            t = info["target"]
            if t and t[1] >= 110 and t[1] + t[3] <= self.height - 10:
                break
            if t is None and not searched and (info["total"] == 0 or time.monotonic() > t_end - 14):
                # not on screen after a look around: type its name into the page's own search (no click)
                searched = True
                if await page.evaluate(SEARCH_JS):
                    await page.keyboard.type(g.name, delay=random.randint(70, 120))
                    await page.wait_for_timeout(1500)
                    await self._move(self._mouse[0], 420)
                continue
            dy = max(-420, min(420, (t[1] + t[3] / 2) - 400)) if t else 360
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(random.uniform(1.0, 1.8))
            if t is None and random.random() < 0.4:
                info = await page.evaluate(TILES_JS, path)
                if info["others"]:
                    x, y, w, h = random.choice(info["others"])
                    await self._move(x + w / 2, y + h / 2, (0.5, 1.0))
        else:
            return False
        x, y, w, h = t
        cx, cy = x + w / 2 + random.uniform(-w / 8, w / 8), y + h * 0.45
        await self._move(cx, cy, (0.0, 0.0))
        await self._show_if_patched("picked")
        await asyncio.sleep(random.uniform(0.8, 1.4))
        if not await page.evaluate(POINT_JS, [cx, cy, path]):
            return False
        assert_tile_clickable(path, listed)
        await page.mouse.click(cx, cy)
        try:
            await page.wait_for_url(lambda u: urlparse(u).path.rstrip("/") == path, timeout=15000)
        except Exception:
            return False
        if isinstance(g, OriginalGame):
            self._phase = "loading"       # its page draws logged out first: the card is up until the game is ready
        else:
            await self._show_if_patched("picked")
        await self._cursor(True)          # a full page load drops it; a client-side one keeps it
        return True

    async def _fail_shot(self, g: SlotGame) -> None:
        if self.shots_dir:
            try:
                await self._page.screenshot(path=os.path.join(self.shots_dir, f"hb_fail_{g.id}.png"))
            except Exception:
                pass

    async def _stuck_shot(self, g: SlotGame) -> None:
        """What the game showed when the first spin press did nothing (newest 10 kept), for later fixes."""
        d = os.environ.get("HUBIE_HB_STUCK_DIR", "/dev/shm/hubie_stream/stuck")
        try:
            os.makedirs(d, exist_ok=True)
            await self._page.screenshot(path=os.path.join(d, f"{time.strftime('%Y%m%d_%H%M%S')}_{g.id}.png"))
            for old in sorted(os.listdir(d))[:-10]:
                os.remove(os.path.join(d, old))
            log.info("%s: first spin press did nothing, retrying (shot in %s)", g.name, d)
        except Exception:
            pass

    async def _press_space(self):
        f = self._game_frame()
        if f is None:
            raise GameError("game frame gone")
        await f.evaluate("window.focus()")
        await self._page.keyboard.press("Space")

    async def _guard(self) -> None:
        """Runs before every spin. Raises unless every layer says demo."""
        if self._hard_stop:
            raise NotDemoError("; ".join(self._hard_stop))
        await self._snapshot()
        verdict, why = demo_verdict(self.state)
        if verdict == "real":
            self._hard_stop.extend(why)
            raise NotDemoError("; ".join(why))
        if verdict != "ok":
            raise GameError("demo not verified: " + "; ".join(why))

    async def _play(self) -> RoundResult:
        async with self._lock:
            g = self.current
            if g is None or self._page is None:
                raise GameError("no game loaded")
            page_text = await self._text()
            if any(m in page_text for m in CHALLENGE_MARKS):
                raise GameError("challenge on screen")
            await self._guard()
            if self._buy_due(g):
                self.force_bonus_buy = False
                self._next_buy = time.monotonic() + self._buy_gap()
                try:
                    r = await self._buy(g)
                    return await self._finish(g, r)
                except NotDemoError:
                    raise
                except _BuySkipped as e:          # nothing was bought: spin as usual, try again next gap
                    log.warning("%s: bonus buy skipped: %s", g.name, e)
                    await self._guard()
                except GameError:
                    self._buys_off.add(g.id)      # the game did not look as mapped: no more buys here
                    raise
            r = await self._spin(g)
            return await self._finish(g, r)

    async def _click_hold(self, why: str, during=None) -> None:
        """Asks the stage for the click clip and waits until its claw lands, so the real click is sent on that
        frame (about a second). `during` (a coroutine) runs while it waits, e.g. the demo guard. Any failure
        or no answer: no wait at all."""
        at = None
        if self.click_cue:
            try:
                at = await asyncio.wait_for(asyncio.to_thread(self.click_at, why), 0.5)
            except Exception:
                at = None
        if during is not None:
            await during
        if at is not None:
            delay = at - time.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def _spin(self, g: SlotGame) -> dict:
        before = self._round_start = len(self._exchanges)
        self._round_q.clear()
        t0 = time.monotonic()
        presses = 0
        while len(self._exchanges) == before:
            # cluster slots keep the button busy while the last drop animates: press again,
            # a few times, re-checking demo each time
            if presses < 4 and time.monotonic() - t0 >= presses * 3.5:
                if presses:
                    await self._guard()
                    if presses == 1:
                        await self._stuck_shot(g)
                    if g.spin_click:
                        # a win-total / "press anywhere" screen after a feature eats spin-button presses:
                        # a click on the game's top banner dismisses it. No button lives up there (buy dialogs
                        # and their YES sit mid-screen), so it can never confirm anything
                        await self._page.mouse.click(*self.view.pt(DISMISS_CLICK))
                        await asyncio.sleep(0.4)
                else:
                    await self._click_hold("spin")    # the on-screen claw lands as the spin is sent
                    t0 = time.monotonic()
                if g.spin_click:
                    await self._page.mouse.click(*self.view.pt(g.spin_click))
                else:
                    await self._press_space()
                presses += 1
            if time.monotonic() - t0 > 15:
                raise GameError(f"{g.name}: spin did not register")
            await asyncio.sleep(0.2)
        return await self._wait_round(g, t0, self.spin_timeout - 15)

    async def _wait_round(self, g: SlotGame, t0: float, limit: float) -> dict:
        last_change, seen = time.monotonic(), len(self._exchanges)
        while not self._round_q:
            await asyncio.sleep(0.3)
            if len(self._exchanges) != seen:
                last_change, seen = time.monotonic(), len(self._exchanges)
            elif time.monotonic() - last_change > 5 and self.parser.busy:
                # free spins or a win screen waiting for a press; nudge the game itself.
                # Space in the game frame only: it never reaches a site button.
                await self._guard()
                await self._press_space()
                last_change = time.monotonic()
            if time.monotonic() - t0 > limit:
                raise GameError(f"{g.name}: round did not finish")
        return self._round_q.pop(0)

    async def _check_screen(self) -> None:
        """Sets screen_ready(). Without the top bar the game covers the window, so a verified load is enough."""
        ok, why = self._game_frame() is not None, "game frame gone"
        if ok and self.view.y0 > 0:
            # The bar may read "In Game", but live (2026-11-07) it kept the real balance next to Wallet while a
            # fun-mode game ran. Either the bar shows no amount left of Wallet, or the DEMO PLAY patch covers it.
            try:
                ok, why = topbar_safe(await self._page.evaluate(TOPBAR_TEXT_JS, self.view.y0))
            except Exception as e:
                ok, why = False, f"top bar unreadable: {e!r}"
            if not ok and await self._patched():
                ok, why = True, ""
        if not ok and self._screen_ok:
            log.warning("game area hidden on stream: %s", why)
        self._screen_ok = ok

    async def _finish(self, g: SlotGame, r: dict) -> RoundResult:
        await self._page.wait_for_timeout(2500)       # let the win animation land on screen
        await self._check_screen()
        await self._snapshot()
        verdict, why = demo_verdict(self.state)
        if verdict == "real":
            self._hard_stop.extend(why)
            raise NotDemoError("; ".join(why))
        self._n += 1
        if r["free_spins"]:
            self._dump_feature(g, getattr(self, "_round_start", 0))
        detail = {"slot": g.name, "provider": "Pragmatic Play", "tumbles": r["tumbles"],
                  "free_spins_triggered": r["free_spins"], "demo_currency": "demo $"}
        if r["free_spins"]:
            detail["free_spins_count"] = r["free_spins_count"]
        if r.get("bonus_buy"):
            detail.update(bonus_buy=True, buy_cost_demo=r["buy_cost"], feature_win_demo=r["payout"],
                          feature_multiplier=r["multiplier"], note="demo money, bought the feature just to see it")
        if self.shots_dir:
            try:
                await self._page.screenshot(path=os.path.join(self.shots_dir, f"hb_{g.id}_{self._n}.png"))
            except Exception:
                pass
        return RoundResult(g.id, f"hb-{g.id}-{self._n}", r["bet"], r["multiplier"], r["payout"],
                           r["balance"], True, detail)

    # ── demo bonus buy ──

    async def _grab(self, box: tuple[int, int, int, int]) -> bytes:
        x0, y0, x1, y1 = self.view.box(box)
        try:     # a busy game page can stall a screenshot for minutes (Kick test 2026-11-08: a buy hung 265 s)
            return await asyncio.wait_for(
                self._page.screenshot(clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}), 10)
        except asyncio.TimeoutError:
            raise GameError("screen grab took over 10 s")

    async def _screen_shows(self, box: tuple[int, int, int, int], amount: float) -> tuple[bool, list[str]]:
        return await asyncio.to_thread(shows_amount, await self._grab(box), amount)

    async def _game_click(self, label: str, xy: tuple[int, int]) -> None:
        """The only way a bonus-buy click reaches the game: allow-listed label, game frame on a demo host."""
        assert_game_clickable(label)
        if self._game_frame() is None:
            raise _BuySkipped("game frame gone")
        await self._page.mouse.click(*self.view.pt(xy))

    async def _shot(self, name: str) -> None:
        if self.shots_dir:
            try:
                await self._page.screenshot(path=os.path.join(self.shots_dir, name))
            except Exception:
                pass

    async def _buy(self, g: SlotGame) -> dict:
        """One demo feature buy, then the free spins it brings, as one round.
        Raises _BuySkipped (nothing bought), NotDemoError (stop for good) or GameError."""
        spec = g.buy
        p = self.parser
        if p.busy:
            raise _BuySkipped("game busy")
        cheapest = p.cheapest_buy()
        if not cheapest:
            raise _BuySkipped("game offers no feature buy in its init")
        idx, cost = cheapest
        bet = round((p.coin or 0) * (p.lines or 0), 2)
        if not bet or abs(cost - spec.times_bet * bet) > 0.01:
            raise _BuySkipped(f"cheapest buy {cost} is not {spec.times_bet}x the bet {bet}")
        demo = p.balance
        if demo is None or demo < 1000 or cost > demo / 20:
            raise _BuySkipped(f"demo balance {demo} too low for a {cost} buy")
        # the CREDIT the game shows must be the demo balance, never something else
        shown, seen = await self._screen_shows(spec.credit_box, demo)
        if not shown:
            raise _BuySkipped(f"credit on screen {seen[:3]} is not the demo balance {demo}")
        # the button about to be pressed must carry the cheapest price, not SUPER's
        ok, seen = await self._screen_shows(spec.price_box, cost)
        if not ok:
            raise _BuySkipped(f"buy button shows {seen[:3]}, expected {cost:.2f}")
        await self._shot(f"hb_buy_{g.id}_before.png")
        await self._guard()
        # Purchase check in flight, only around the buy: with a route on, Playwright passes every request
        # of the page through this process, which slowed a feature's asset loading (Sugar Rush, 2026-11-07).
        ctx = self._page.context
        log.info("%s: demo buy starting (%.2f)", g.name, cost)
        await asyncio.wait_for(ctx.route(GAME_SERVICE_RE, self._route_game), 10)
        try:
            return await self._buy_checked(g, spec, idx, cost, demo)
        finally:
            self._buy_armed = None
            try:
                await asyncio.wait_for(ctx.unroute(GAME_SERVICE_RE, self._route_game), 10)
            except Exception as e:
                log.warning("unroute failed: %r", e)

    async def _buy_checked(self, g: SlotGame, spec: BuySpec, idx: int, cost: float, demo: float) -> dict:
        before = self._round_start = len(self._exchanges)
        self._round_q.clear()
        try:
            await asyncio.wait_for(self._buy_confirm(g, spec, idx, cost, before), 60)
        except asyncio.TimeoutError:
            raise GameError(f"{g.name}: buy steps before the purchase took over 60 s")
        return await self._buy_after(g, spec, idx, cost, demo, before)

    async def _buy_confirm(self, g: SlotGame, spec: BuySpec, idx: int, cost: float, before: int) -> None:
        """Everything up to the purchase request being seen: dialog check, claw, confirm."""
        await self._game_click("BUY FREE SPINS", spec.button)
        await self._page.wait_for_timeout(1800)
        await self._shot(f"hb_buy_{g.id}_dialog.png")
        if len(self._exchanges) != before:
            raise GameError("the game sent something on the buy button alone")
        if spec.confirm is None or spec.dialog_text_box is None or spec.dialog_price_box is None:
            raise GameError(f"{g.name}: buy confirm not mapped yet (dialog screenshot saved)")
        for _ in range(3):              # the dialog glows and settles: read it again before giving up
            ok, seen = await asyncio.to_thread(dialog_is_standard_buy, await self._grab(spec.dialog_text_box))
            if ok or any(re.search(r"super", t, re.I) for t in seen):
                break
            await self._page.wait_for_timeout(700)
        if not ok:
            raise GameError(f"{g.name}: buy dialog does not read as the standard free spins buy: {seen}")
        ok, seen = await self._screen_shows(spec.dialog_price_box, cost)
        if not ok:
            raise GameError(f"{g.name}: buy dialog price {seen[:3]}, expected {cost:.2f}")
        await self._click_hold("buy", self._guard())   # demo check runs while the claw comes down
        self._buy_armed = idx
        t0 = time.monotonic()
        try:
            await self._game_click("BUY CONFIRM", spec.confirm)
            while len(self._exchanges) == before:
                if time.monotonic() - t0 > 12:
                    raise GameError(f"{g.name}: buy did not register")
                await asyncio.sleep(0.2)
        finally:
            self._buy_armed = None

    async def _buy_after(self, g: SlotGame, spec: BuySpec, idx: int, cost: float, demo: float, before: int) -> dict:
        t0 = time.monotonic()
        req, res = self._exchanges[before]
        kv, rv = parse_kv(req), parse_kv(res)
        if kv.get("pur") != str(idx):
            raise GameError(f"{g.name}: first request after confirm was not buy {idx}: {kv.get('action')}")
        after = num(rv.get("balance"), None)
        if after is None or after < 1000:
            self._hard_stop.append(f"buy answered balance {after}, not a demo float")
            raise NotDemoError(self._hard_stop[-1])
        if abs((demo - after) - cost) > 0.01:
            self._buys_off.add(g.id)
            log.error("%s: buy took %s off the demo balance, expected %s", g.name, demo - after, cost)
        if self._wallet_latest != self._wallet_first:
            self._hard_stop.append("real wallet moved around a bonus buy")
            raise NotDemoError(self._hard_stop[-1])
        # the cost must come off the credit the game shows; if that can't be seen, no more buys here
        credit_ok = False
        for _ in range(4):
            await self._page.wait_for_timeout(700)
            credit_ok, seen = await self._screen_shows(spec.credit_box, round(demo - cost, 2))
            if credit_ok:
                break
        if not credit_ok:
            self._buys_off.add(g.id)
            log.error("%s: credit on screen %s after a %s buy, expected %s", g.name, seen[:3], cost, demo - cost)
        else:
            log.info("%s: demo buy %s, credit on screen %s -> %s", g.name, cost, demo, demo - cost)
        await self._shot(f"hb_buy_{g.id}_bought.png")
        try:                              # the buy is through: free spins load and play unrouted
            await self._page.context.unroute(GAME_SERVICE_RE, self._route_game)
        except Exception as e:
            log.warning("unroute failed: %r", e)
        mid_shot = asyncio.ensure_future(self._mid_shot(g))
        try:
            r = await self._wait_round(g, t0, self.buy_timeout - 20)
        finally:
            mid_shot.cancel()
        if not r.get("bonus_buy"):
            raise GameError(f"{g.name}: round after the buy did not parse as a buy")
        r["credit_checked"] = credit_ok
        return r

    async def _mid_shot(self, g: SlotGame) -> None:
        await asyncio.sleep(25)
        await self._shot(f"hb_buy_{g.id}_freespins.png")

    # ── house originals, zero bet ──

    def _orig_breach(self, why: str, real: bool = False) -> None:
        """A zero-bet guard tripped: no more originals this session. real = money may have moved: stop all play."""
        if why not in self._orig_stop:
            log.error("house originals off for good: %s", why)
            self._orig_stop.append(why)
        if real and why not in self._hard_stop:
            self._hard_stop.append(f"house original: {why}")

    def originals_stopped(self) -> list[str]:
        return list(self._orig_stop)

    async def _orig_route_on(self) -> None:
        if self._orig_routed:
            return
        await asyncio.wait_for(self._page.context.route(ORIGINALS_ROUTE, self._route_original), 10)
        self._orig_routed = True

    async def _orig_route_off(self) -> None:
        if not self._orig_routed or self._page is None:
            self._orig_routed = False
            return
        try:
            await asyncio.wait_for(self._page.context.unroute(ORIGINALS_ROUTE, self._route_original), 10)
        except Exception as e:
            log.warning("originals unroute failed: %r", e)
        self._orig_routed = False

    async def _route_original(self, route):
        """Every request to games.housebets.com while an original is loaded. GETs pass; POSTs go through
        check_original_request: a bet only at 0 in USDT, an action only on a round seen starting at 0."""
        req = route.request
        try:
            if req.method in ("GET", "HEAD", "OPTIONS"):
                await route.continue_()
                return
            try:
                body = req.post_data
            except Exception:
                body = None
            ok, why = check_original_request(req.url, body, self._zero_rounds, self._usdt_ids)
            if self._orig_stop and urlparse(req.url).path.rstrip("/").rsplit("/", 1)[-1] in ("bet", "action"):
                ok, why = False, "originals are off: " + self._orig_stop[0]
            self._orig_seen.append(f"{urlparse(req.url).path}:{'ok' if ok else 'blocked'}")
            if ok:
                await route.continue_()
                return
            self._orig_breach(why)
            await route.abort()
        except Exception as e:
            self._orig_breach(f"request check failed: {e!r}")
            try:
                await route.abort()
            except Exception:
                pass

    def _on_ws(self, ws) -> None:
        """The site's live wallet feed: a real balance going down at any time stops all play."""
        def frame(payload):
            try:
                text = payload if isinstance(payload, str) else payload.decode("utf-8", "replace")
                if '"wallet"' not in text or "cash_balance" not in text:
                    return
                import json
                msg = json.loads(text)
                d = msg.get("data") or {}
                if msg.get("topic") != "wallet" or not isinstance(d, dict) or "currency" not in d:
                    return
                cur = str(d["currency"]).lower()
                now = (_f(d.get("cash_balance")), _f(d.get("bonus_balance")))
                was = self._ws_wallet.get(cur)
                self._ws_wallet[cur] = now if was is None or now[0] >= was[0] and now[1] >= was[1] else was
                if was is not None and (now[0] < was[0] or now[1] < was[1]):
                    self._hard_stop.append(f"live wallet feed: {cur} went down {was} -> {now}")
                    self._orig_breach(f"{cur} balance went down", real=True)
            except Exception:
                pass
        try:
            ws.on("framereceived", frame)
        except Exception:
            pass

    async def _read_original(self, resp):
        """Answers from games.housebets.com: the wallet behind each game (/limits), the USDT balance it reads, a
        round left open (/game), and every bet/action answer of a round."""
        import json
        try:
            url, method = resp.url, resp.request.method
            path = urlparse(url).path.rstrip("/")
            parts = path.strip("/").split("/")
            if method == "GET" and len(parts) == 2 and parts[0] == "limits":
                w = (json.loads(await resp.text()) or {}).get("wallet") or {}
                self._orig_limits[parts[1]] = (w.get("currency"), w.get("symbol"))
                if w.get("symbol") == SAFE_SYMBOL and w.get("currency") is not None:
                    self._usdt_ids.add(w["currency"])
                return
            if method == "GET" and path == "/user/balance":
                cur = dict(parse_qsl(urlparse(url).query)).get("currency")
                bal = _f((json.loads(await resp.text()) or {}).get("balance"), None)
                if bal is None:
                    return
                was = self._orig_balance.get(cur)
                if was is None or bal > was:
                    self._orig_balance[cur] = bal       # first seen, or money in from outside
                elif bal < was:
                    self._orig_breach(f"currency {cur} balance went down {was} -> {bal}", real=True)
                self._orig_balance_seen += 1
                return
            if len(parts) >= 2 and parts[0] in STRICT_ACTION_APIS and method == "POST" and parts[1] != "bet":
                parts = [parts[0], "action"]            # blackjack's steps (hit, stand, ...) are its actions
            if len(parts) != 2 or parts[0] not in ORIG_BY_API or parts[1] not in ("bet", "action", "game"):
                return
            text = await resp.text()
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if parts[1] == "game":
                self._orig_game_seen.add(parts[0])
                g = (data or {}).get("game") if isinstance(data, dict) else None
                if isinstance(g, dict) and g.get("active"):
                    verdict, why = check_original_response(data, self._usdt_ids)
                    if verdict == "ok":
                        self._zero_rounds.add(g.get("roundId"))
                        self._orig_active[parts[0]] = data
                    else:
                        self._orig_breach(f"round left open: {why}", real=verdict == "real")
                return
            if resp.status >= 400 or data is None:
                self._orig_q.append({"api": parts[0], "kind": parts[1], "error": f"{resp.status} {text[:200]}"})
                return
            verdict, why = check_original_response(data, self._usdt_ids)
            if verdict != "ok":
                self._orig_breach(f"{path}: {why}", real=verdict == "real")
            elif isinstance(data["game"], dict) and data["game"].get("active") and data["game"].get("roundId"):
                self._zero_rounds.add(data["game"]["roundId"])
            base = [self._orig_balance[str(c)] for c in self._usdt_ids if str(c) in self._orig_balance]
            if "balance" in data and base and _f(data["balance"], -1) < min(base):
                self._orig_breach(f"{path}: balance {data['balance']!r} below {min(base)}", real=True)
            self._orig_q.append({"api": parts[0], "kind": parts[1], "data": data, "ok": verdict == "ok"})
            if self.orig_record is not None:
                try:
                    req_body = resp.request.post_data
                except Exception:
                    req_body = None
                self.orig_record.append({"path": path, "request": req_body, "response": data})
        except Exception as e:
            log.warning("original answer not readable: %r", e)

    def _game_el(self):
        return self._page.locator(self.current.tag)

    async def _orig_find(self, label: str, pick: str | None = None, enabled: bool = True):
        """The one visible element for an allow-listed label (pick = its aria-label, e.g. "Tile 7"). enabled=False
        also returns a disabled one (only to read it: the bet field is disabled while a round is open)."""
        css, pattern = ORIGINAL_CLICKS[label]
        if pick is not None and re.fullmatch(r"[A-Za-z0-9 ]{1,20}", pick):
            # straight to the one with that aria-label (a keno card has 40 numbers); still checked below
            css = ", ".join(f'{c.strip()}[aria-label="{pick}"]' for c in css.split(","))
        loc = self._game_el().locator(css)
        for i in range(await loc.count()):
            el = loc.nth(i)
            if not await el.is_visible():
                continue
            aria = await el.get_attribute("aria-label") or ""
            if pick is not None and aria != pick:
                continue
            text = "" if pattern is None else re.sub(r"\s+", " ", (await el.inner_text()).strip())
            if pattern is not None and not re.fullmatch(pattern, text):
                continue
            assert_original_clickable(label, text, aria)
            if enabled and not await el.is_enabled():
                continue
            return el, text
        return None, ""

    async def _orig_wait_find(self, label: str, pick: str | None = None, wait: float = 0.0, enabled: bool = True):
        """_orig_find, retried for up to `wait` seconds (buttons come back after the game's own animations)."""
        t0 = time.monotonic()
        while True:
            el, text = await self._orig_find(label, pick, enabled=enabled)
            if el is not None or time.monotonic() - t0 >= wait:
                return el, text
            await asyncio.sleep(0.25)

    async def _orig_click(self, label: str, pick: str | None = None, paw: bool = False, during=None,
                          wait: float = 0.0) -> str:
        """The only way a click reaches an original: allow-listed label, element proven under the mouse."""
        el, text = await self._orig_wait_find(label, pick, wait)
        if el is None:
            raise GameError(f"{self.current.name}: {label}{' ' + pick if pick else ''} not on screen")
        box = await el.bounding_box()
        if not box:
            raise GameError(f"{label} has no box")
        x = box["x"] + box["width"] / 2 + random.uniform(-box["width"] / 6, box["width"] / 6)
        y = box["y"] + box["height"] / 2 + random.uniform(-box["height"] / 6, box["height"] / 6)
        await self._move(x, y, (0.15, 0.35))
        if paw:
            await self._click_hold("spin", during)     # the on-screen paw lands as the bet goes in
        elif during is not None:
            await during
        if not await el.evaluate(ON_TARGET_JS, [x, y]):
            # the game may have redrawn the button under the mouse (cash out updates its own label): look it up
            # again, same checks, and click only if the fresh one is what's under the mouse
            el, text = await self._orig_find(label, pick)
            if el is None or not await el.evaluate(ON_TARGET_JS, [x, y]):
                try:
                    what = await self._page.evaluate(WHAT_AT_JS, [x, y])
                except Exception:
                    what = "?"
                raise GameError(f"{label}: something else is under the mouse ({what})")
        assert_original_clickable(label, text, await el.get_attribute("aria-label") or "")
        await self._page.mouse.click(x, y)
        return text

    async def _orig_type(self, label: str, value: str) -> str:
        """Types a game setting (roll over, limbo target) into an allow-listed input and returns what it reads after.
        Never the bet amount: that field is only ever cleared to 0 by _orig_guard."""
        assert_setting_ok(label, value)
        await self._orig_click(label)
        await self._page.keyboard.press("Control+a")
        await self._page.keyboard.type(value, delay=random.randint(60, 110))
        await self._page.keyboard.press("Tab")
        await self._page.wait_for_timeout(300)
        el, _ = await self._orig_find(label)
        return await el.input_value() if el is not None else ""

    async def _orig_set(self, label: str, value: str) -> str:
        """A select (mines count) or a slider (plinko rows), allow-listed, to an allowed setting value."""
        assert_setting_ok(label, value)
        el, _ = await self._orig_find(label)
        if el is None:
            raise GameError(f"{self.current.name}: {label} not on screen")
        assert_original_clickable(label, "", await el.get_attribute("aria-label") or "")
        box = await el.bounding_box()
        if box:
            await self._move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, (0.1, 0.25))
        if label == "MINES COUNT":
            await el.select_option(value=value)
        else:
            if await el.get_attribute("type") != "range":
                raise GameError(f"{label} is not a slider")
            await el.fill(value)
        await self._page.wait_for_timeout(300)
        return await el.input_value()

    async def _orig_guard(self) -> None:
        """Before every bet: originals on, no real-play stop, USDT selected (site pill and the game's own wallet),
        the bet field at 0 (read even while disabled by an open round; cleared only when it can be typed in)."""
        if self._hard_stop:
            raise NotDemoError("; ".join(self._hard_stop))
        if self._orig_stop:
            raise GameGone("house originals off: " + self._orig_stop[0])
        g = self.current
        pills = await self._page.evaluate(CURRENCY_JS)
        if pills != [SAFE_SYMBOL]:
            raise GameError(f"currency pill reads {pills}, not {SAFE_SYMBOL}")
        cur, sym = self._orig_limits.get(g.api, (None, None))
        if sym != SAFE_SYMBOL or cur not in self._usdt_ids:
            raise GameError(f"{g.name}: game wallet {sym!r} ({cur!r}), not {SAFE_SYMBOL}")
        if self._wallet_first is None or self._wallet_latest is None:
            raise GameError("real wallet balances not seen")
        el, _ = await self._orig_find("BET AMOUNT", enabled=False)
        if el is None:
            raise GameError(f"{g.name}: no bet field")
        v = await el.input_value()
        if not bet_field_zero(v):
            if not await el.is_enabled():
                raise GameError(f"{g.name}: bet field reads {v!r} and can't be cleared")
            log.warning("%s: bet field read %r, clearing it", g.name, v)
            await self._orig_click("BET AMOUNT")
            await self._page.keyboard.press("Control+a")
            await self._page.keyboard.press("Backspace")
            await self._page.keyboard.type("0")
            await self._page.keyboard.press("Tab")
            await self._page.wait_for_timeout(400)
            el, _ = await self._orig_find("BET AMOUNT")
            v = await el.input_value() if el is not None else None
            if el is None or not bet_field_zero(v):
                raise GameError(f"{g.name}: bet field reads {v!r} after clearing, not 0")

    async def _load_original(self, g: OriginalGame):
        try:
            await self._load_original_locked(g)
        finally:
            self._phase = "loading"

    async def _load_original_locked(self, g: OriginalGame):
        async with self._lock:
            if self._orig_stop:
                raise GameGone("house originals off: " + self._orig_stop[0])
            if self._page is not None and self._tree_mb() > RECYCLE_MB:
                log.info("game switch: browser at %.0f MB, starting a fresh one", self._tree_mb())
                await self._close()
                await asyncio.to_thread(self._kill_procs)
                self._page = None
                self._orig_routed = False
            if self._page is None:
                await self._start_locked()
            await self._orig_open(g, browse=self.browse)
            await self._shot(f"hb_loaded_{g.id}.png")
            log.info("loaded house original %s at a zero bet (USDT)%s", g.name,
                     ", an open round to finish" if g.api in self._orig_active else "")

    async def _orig_open(self, g: OriginalGame, browse: bool = False) -> None:
        """Opens (or re-opens) the game page clean: the wallet, the game's /limits and its bet field all seen, and a
        round left open on the account picked up from the game's own /game answer (it is finished next round)."""
        self.current = g
        self._screen_ok = False
        self._phase = "loading"
        self._round_q.clear()
        self._orig_q.clear()
        self._orig_active.pop(g.api, None)
        self._orig_game_seen.discard(g.api)
        self._wallet_latest = None
        await self._orig_route_on()
        browsed = False
        if browse:
            try:
                browsed = await asyncio.wait_for(self._browse_to(g), 35)
            except NotDemoError:
                raise
            except Exception as e:
                log.warning("%s: browsing failed (%r), going straight to the game", g.name, e)
            if not browsed:
                self._phase = "loading"
        # the game page draws its logged-out look first (Log in in the bet box, $ 0.00000000): from the tile click on,
        # the switching card stays up until the game is proven ready below
        self._phase = "loading"
        if not browsed:
            await self._page.goto(f"{BASE}/games/{g.slug}", wait_until="domcontentloaded", timeout=60000)
        await self._page.wait_for_timeout(2500 if browsed else 4000)
        text = await self._text()
        if any(m in text for m in CHALLENGE_MARKS):
            await self._pass_challenge()
        try:
            await self._click_label("Reject", 1500)
        except Exception:
            pass
        el = None
        for _ in range(30):        # the page loads the wallet, the game element and its /limits itself
            el, _t = await self._orig_find("BET AMOUNT", enabled=False) if await self._game_el().count() else (None, "")
            if self._wallet_latest is not None and el is not None and g.api in self._orig_limits:
                break
            await self._page.wait_for_timeout(700)
        if self._wallet_latest is None:
            await self._fail_shot(g)
            raise GameError("real wallet balances not seen (logged out?); refusing to play")
        if g.api not in self._orig_limits:
            await self._fail_shot(g)
            raise GameError(f"{g.name}: the game's wallet (limits) not seen")
        if g.api in ("mines2", "road") or (el is not None and not await el.is_enabled()):
            # mines and road ask the server for a round left open (/game) as the page loads, and only switch the
            # screen to it once that answer is in: until then the page shows an idle Bet that would not work
            for _ in range(32):
                if g.api in self._orig_game_seen and (g.api in self._orig_active or await self._orig_idle_ok(g)):
                    break
                await self._page.wait_for_timeout(250)
            if g.api not in self._orig_game_seen:
                log.info("%s: no open-round answer seen while loading", g.name)
        if not await self._orig_ready(g):
            log.info("%s: game not showing its logged-in controls yet after the wait", g.name)
        await self._orig_zoom()
        try:
            await self._page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        await self._cursor(True)
        await self._orig_guard()
        await self._page.wait_for_timeout(500)      # the zoom's relayout painted before the card comes down
        await self._check_original_screen(g)

    async def _orig_logged_out(self) -> bool:
        try:
            return bool(await self._game_el().first.evaluate(ORIG_LOGGED_OUT_JS))
        except Exception:
            return True

    async def _orig_ready(self, g: OriginalGame, wait: float = 10.0) -> bool:
        """The game shows its logged-in self: no Log in button left in it, its USDT wallet answered (/limits), and its
        own controls up (Bet with that exact label and the bet field, or an open round's Go / Cash out / tiles)."""
        t0 = time.monotonic()
        while True:
            ok = g.api in self._orig_limits and self._wallet_latest is not None and not await self._orig_logged_out()
            if ok:
                if g.api in self._orig_active:
                    for label in ("GO", "CASH OUT", "TILE", "BJ HIT", "BJ STAND", "BJ NO INSURANCE"):
                        if (await self._orig_find(label, enabled=False))[0] is not None:
                            return True
                elif await self._orig_idle_ok(g):
                    return True
            if time.monotonic() - t0 >= wait:
                return False
            await self._page.wait_for_timeout(250)

    async def _orig_idle_ok(self, g: OriginalGame) -> bool:
        """No round open: the game's own Bet button is up and the bet field can be typed in."""
        el, _ = await self._orig_find("BET AMOUNT")
        bet, _ = await self._orig_find("BET", enabled=g.api not in IDLE_BET_DISABLED)
        return el is not None and bet is not None

    async def _orig_zoom(self) -> None:
        z = orig_zoom(self.current.api) if isinstance(self.current, OriginalGame) else orig_zoom("")
        try:
            await self._page.evaluate(f"document.body && (document.body.style.zoom = '{z:g}')")
        except Exception as e:
            log.info("originals zoom not set: %r", e)

    async def _check_original_screen(self, g: OriginalGame | None = None) -> None:
        """The page may be shown when the game is there, logged in (no Log in button in it) and the DEMO PLAY patch
        covers the header balance. Right after a load (g given) its own controls must be up too."""
        try:
            ok = await self._game_el().first.is_visible() and await self._patched() and not await self._orig_logged_out()
            if ok and g is not None:
                ok = await self._orig_ready(g, wait=3.0)
        except Exception:
            ok = False
        if not ok and self._screen_ok:
            log.warning("original hidden on stream: game gone or balance not covered")
        self._screen_ok = ok

    async def _orig_wait(self, n0: int, kind: str, timeout: float = 12.0) -> dict:
        """The next answer of this kind (bet/action) after index n0, checked; raises on an error or a breach."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for item in self._orig_q[n0:]:
                if item["kind"] != kind or item["api"] != self.current.api:
                    continue
                if self._orig_stop:
                    raise GameGone("house originals off: " + self._orig_stop[0])
                if "error" in item:
                    raise GameError(f"{self.current.name}: {kind} answered {item['error']}")
                return item["data"]
            if self._orig_stop:
                raise GameGone("house originals off: " + self._orig_stop[0])
            await asyncio.sleep(0.1)
        raise GameError(f"{self.current.name}: no {kind} answer")

    async def _orig_press(self, label: str, kind: str, pick: str | None = None, paw: bool = False,
                          wait: float = 0.0) -> dict:
        n0 = len(self._orig_q)
        await self._orig_click(label, pick, paw=paw, during=self._orig_guard() if paw else None, wait=wait)
        return await self._orig_wait(n0, kind)

    async def _orig_apply(self, g: OriginalGame, plan: dict) -> None:
        """Puts the plan's settings on the game's own controls: mines count, road level, plinko risk and rows, limbo
        target, dice roll-over. Only allow-listed controls with checked values; none of them touch the amount. A
        setting that doesn't take is skipped (the round says what was really played)."""
        async def step(what, coro):
            try:
                await coro
            except (GameGone, NotDemoError):
                raise
            except Exception as e:
                log.info("%s: %s not set: %r", g.name, what, e)
        if g.api == "mines2" and plan.get("mines"):
            await step("mines count", self._orig_set("MINES COUNT", str(int(plan["mines"]))))
        elif g.api == "road" and plan.get("level"):
            await step("level", self._orig_pick_text("ROAD LEVEL", plan["level"]))
        elif g.api == "plinko2":
            if plan.get("risk"):
                await step("risk", self._orig_pick_text("PLINKO RISK", plan["risk"]))
            if plan.get("rows"):
                await step("rows", self._orig_set("PLINKO ROWS", str(int(plan["rows"]))))
        elif g.api == "limbo" and plan.get("target"):
            t = float(plan["target"])
            if t in plays.LIMBO_QUICK:
                await step("target", self._orig_pick_text("LIMBO TARGET", plays.LIMBO_QUICK[t]))
            else:
                await step("target", self._orig_type("LIMBO TARGET INPUT", f"{t:g}"))
        elif g.api == "dice" and plan.get("roll_over"):
            await step("roll over", self._orig_type("ROLL OVER", f"{float(plan['roll_over']):.2f}"))
        elif g.api == "keno2" and plan.get("numbers"):
            await step("numbers", self._keno_pick(plan))
        await self._page.wait_for_timeout(random.randint(300, 700))

    async def _keno_pick(self, plan: dict) -> None:
        """A fresh keno card: the table cleared, the risk set, then the plan's numbers (1-40, at most 10) one by one."""
        nums = [int(n) for n in plan["numbers"]]
        if not 1 <= len(nums) <= 10 or len(set(nums)) != len(nums) or not all(1 <= n <= 40 for n in nums):
            raise GameError(f"keno numbers {nums!r} not a card")
        el, _ = await self._orig_find("KENO CLEAR")
        if el is not None:
            await self._orig_click("KENO CLEAR")
            await self._page.wait_for_timeout(350)
        if plan.get("risk"):
            await self._orig_pick_text("KENO RISK", plan["risk"])
        for n in nums:
            await self._orig_click("KENO NUMBER", f"Number {n}")
            await self._page.wait_for_timeout(random.randint(120, 300))

    async def _orig_pick_text(self, label: str, want: str) -> None:
        loc = self._game_el().locator(ORIGINAL_CLICKS[label][0])
        for i in range(await loc.count()):
            el = loc.nth(i)
            if await el.is_visible() and (await el.inner_text()).strip() == want:
                await self._orig_click_el(label, el)
                return
        raise GameError(f"{label} {want!r} not on screen")

    async def _orig_click_el(self, label: str, el) -> None:
        text = re.sub(r"\s+", " ", (await el.inner_text()).strip())
        assert_original_clickable(label, text, await el.get_attribute("aria-label") or "")
        if not await el.is_enabled():
            return
        box = await el.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self._move(x, y, (0.15, 0.35))
        if not await el.evaluate(ON_TARGET_JS, [x, y]):
            raise GameError(f"{label}: something else is under the mouse")
        await self._page.mouse.click(x, y)

    async def _orig_resync(self, g: OriginalGame, why: str) -> dict | None:
        """The game's screen and the server disagree (no GO after a bet went in, no Bet button between rounds):
        reload the game page clean and take the open round, if any, from the server's own /game answer."""
        log.warning("%s: %s; reloading the game page", g.name, why)
        self._resyncs += 1
        await self._orig_open(g)
        return self._orig_active.pop(g.api, None)

    async def _orig_steps(self, g: OriginalGame, bet: dict, plan: dict) -> tuple[dict, int]:
        """Mines/road after the bet: open tiles / hop lanes as planned, cash out if still alive. (final, picks)"""
        game = bet["game"]
        final = bet
        picks = 0
        want = int(plan.get("depth") or plan.get("steps") or random.randint(1, 3))
        resynced = False
        for _ in range(40):
            if game.get("phase") != "playing":
                break
            done = len(game.get("opened") or []) if g.api == "mines2" else int(_f(game.get("position")))
            can_cash = bool((game.get("options") or {}).get("cashout"))
            if done >= want and can_cash:
                label, pick, kind = "CASH OUT", None, "action"
            elif g.api == "mines2":
                opened = set(game.get("opened") or [])
                free = [t for t in range(1, int(game.get("grid") or 25) + 1) if t - 1 not in opened]
                label, pick, kind = "TILE", f"Tile {random.choice(free)}", "action"
            else:
                label, pick, kind = "GO", None, "action"
            try:
                final = await self._orig_press(label, kind, pick=pick, wait=6.0)
            except GameError as e:
                if "not on screen" not in str(e) or resynced:
                    raise
                resynced = True
                left = await self._orig_resync(g, str(e))
                if left is None:
                    raise GameError(f"{g.name}: round gone after a reload") from e
                final, game = left, left["game"]
                continue
            game = final["game"]
            if label == "TILE":
                picks += 1
            await self._page.wait_for_timeout(random.randint(700, 1300) if g.api == "mines2" else random.randint(1100, 1700))
        if game.get("phase") != "settled":
            raise GameError(f"{g.name}: round did not settle ({game.get('phase')})")
        return final, picks

    async def _bj_steps(self, g: OriginalGame, bet: dict, plan: dict) -> tuple[dict, int]:
        """A blackjack hand after the deal: insurance declined, then basic strategy (or chat's push on the first
        decision) until it settles. Each decision is said out loud first (a "decision" event the orchestrator speaks),
        then pressed. Double only while the server prices it at 0; never split, insurance, side bets or cash out."""
        final, game = bet, bet["game"]
        push = plan.get("push")
        decisions = 0
        resynced = False
        for _ in range(14):
            if game.get("phase") not in BJ_OPEN_PHASES:
                break
            ins = game.get("insurance") or {}
            if ins.get("offered") and not ins.get("decided"):
                label, move = "BJ NO INSURANCE", "no insurance"
            else:
                turn = int(_f(game.get("turn"), 0))
                hands = game.get("hands") or []
                if not 0 <= turn < len(hands):
                    raise GameError(f"{g.name}: no hand to play (turn {game.get('turn')!r})")
                hand = hands[turn]
                up = ((game.get("dealer") or {}).get("cards") or [None])[0]
                opts = game.get("options") or {}
                move, pushed = plays.bj_decide(hand, up, opts, push if decisions == 0 else None)
                if move == "double" and not bj_double_free(game):
                    move = "hit"
                label = {"hit": "BJ HIT", "stand": "BJ STAND", "double": "BJ DOUBLE"}[move]
                self._orig_events.append({"type": "decision", "move": move, "hand": int(_f(hand.get("value"))),
                                          "soft": bool(hand.get("soft")), "dealer": plays.card_value(up),
                                          "pushed_by": plan.get("asked_by") if pushed else None})
                decisions += 1
                await self._page.wait_for_timeout(2600)      # the line is said before the press
            try:
                final = await self._orig_press(label, "action", wait=6.0)
            except GameError as e:
                if "not on screen" not in str(e) or resynced:
                    raise
                resynced = True
                left = await self._orig_resync(g, str(e))
                if left is None:
                    raise GameError(f"{g.name}: hand gone after a reload") from e
                final, game = left, left["game"]
                continue
            game = final["game"]
            await self._page.wait_for_timeout(random.randint(900, 1400))
        if game.get("phase") != "settled":
            raise GameError(f"{g.name}: hand did not settle ({game.get('phase')})")
        await self._page.wait_for_timeout(1200)
        return final, decisions

    async def _orig_burst(self, g: OriginalGame, first: dict, balls: int) -> list[dict]:
        """The rest of a plinko burst after the first ball: each one its own zero-bet request, the full guard before
        every click, and the next ball only once the last one's bet was answered and the game's own Bet button is
        back (the site sets the pace). Each answer is checked as it comes; a ball that won't go ends the burst."""
        out: list[dict] = []
        prev = first
        for _ in range(balls - 1):
            verdict, why = check_original_response(prev, self._usdt_ids)
            if verdict != "ok" or not isinstance(prev.get("round"), dict):
                self._orig_breach(f"burst ball answer: {why or 'no round'}", real=verdict == "real")
                raise GameGone("house originals off: " + self._orig_stop[-1])
            await self._page.wait_for_timeout(random.randint(250, 550))
            await self._orig_guard()
            try:
                prev = await self._orig_press("BET", "bet", wait=6.0)
            except GameError as e:
                if "not on screen" in str(e):        # the Bet button didn't come back in time: a shorter burst
                    log.info("%s: burst cut at %d balls: %s", g.name, len(out) + 1, e)
                    break
                raise
            out.append(prev)
        if out:
            verdict, why = check_original_response(out[-1], self._usdt_ids)
            if verdict != "ok" or not isinstance(out[-1].get("round"), dict):
                self._orig_breach(f"burst ball answer: {why or 'no round'}", real=verdict == "real")
                raise GameGone("house originals off: " + self._orig_stop[-1])
        return out

    async def _play_original(self, plan: dict | None = None) -> RoundResult:
        async with self._lock:
            g = self.current
            if not isinstance(g, OriginalGame) or self._page is None:
                raise GameError("no original loaded")
            text = await self._text()
            if any(m in text for m in CHALLENGE_MARKS):
                raise GameError("challenge on screen")
            plan = dict(plan or plays.plan_round(plays.GAME_OF_API.get(g.api, g.id)))
            await self._orig_zoom()
            await self._orig_guard()
            fp_before = self._wallet_latest
            picks = 0
            resumed = self._orig_active.pop(g.api, None)
            if resumed is None:
                # the screen must be between rounds (its Bet button up); if not, the page is out of step: reload it
                el, _ = await self._orig_wait_find("BET", wait=4.0, enabled=g.api not in IDLE_BET_DISABLED)
                if el is None:
                    resumed = await self._orig_resync(g, "no Bet button between rounds")
            if resumed is None:
                await self._orig_apply(g, plan)
                await self._orig_guard()
            balls = max(1, min(plays.PLINKO_MAX_BALLS, int(plan.get("balls") or 1))) if g.api == "plinko2" else 1
            burst: list[dict] = []
            if g.api in ("dice", "limbo", "plinko2", "keno2"):
                if resumed is not None:
                    raise GameError(f"{g.name}: an open round in an instant game")
                bet = await self._orig_press("BET", "bet", paw=True, wait=4.0)
                final = bet
                if balls > 1:
                    burst = await self._orig_burst(g, bet, balls)
                await self._page.wait_for_timeout({"plinko2": 4200, "dice": 1600, "limbo": 1800, "keno2": 2600}[g.api])
            else:
                if resumed is None:
                    try:
                        bet = await self._orig_press("BET", "bet", paw=True, wait=4.0)
                    except GameError as e:
                        # the screen switched to a round the server had open after all: reload and finish that one
                        if "not on screen" not in str(e):
                            raise
                        resumed = await self._orig_resync(g, str(e))
                        if resumed is None:
                            raise
                        bet = resumed
                else:
                    bet = resumed
                await self._page.wait_for_timeout(random.randint(600, 1100))
                if g.api == "blackjack7":
                    final, picks = await self._bj_steps(g, bet, plan)
                else:
                    final, picks = await self._orig_steps(g, bet, plan)
                await self._page.wait_for_timeout(1200)
            # after the round: the answers were checked as they came; the round must have settled at 0, the game's
            # USDT balance and the account's wallet must not have moved
            verdict, why = check_original_response(final, self._usdt_ids)
            if verdict != "ok" or not isinstance(final.get("round"), dict):
                self._orig_breach(f"settled answer: {why or 'no round'}", real=verdict == "real")
                raise GameGone("house originals off: " + self._orig_stop[-1])
            if self._hard_stop:
                raise NotDemoError("; ".join(self._hard_stop))
            if self._orig_stop:
                raise GameGone("house originals off: " + self._orig_stop[0])
            if fp_before is not None and self._wallet_latest not in (None, fp_before) \
                    and not wallet_only_grew(fp_before, self._wallet_latest):
                self._orig_breach("real wallet moved during an original", real=True)
                raise NotDemoError(self._hard_stop[-1])
            out = original_outcome(g, bet, final, picks)
            played = played_settings(g, bet)
            kept = plan_matches(plan, played)
            if burst:
                out = burst_outcome([out] + [original_outcome(g, b, b) for b in burst])
                kept = kept and all(plan_matches(plan, played_settings(g, b)) for b in burst)
            await self._check_original_screen()
            await self._rest_mouse(g)
            self._n += 1
            await self._shot(f"hb_{g.id}_{self._n}.png")
            detail = {"game": g.name, "house_original": True, "zero_bet": True, "outcome": out["outcome"],
                      "won": out["won"], "multiplier": out["multiplier"], "result": outcome_class(out["multiplier"]),
                      "played": played, "plan": {k: v for k, v in plan.items() if k != "game"},
                      "plan_kept": resumed is None and kept}
            if resumed is not None:
                detail["resumed"] = True
            for k in ("picks", "gems", "steps", "first_click", "risk", "target", "drawn", "roll", "mines",
                      "difficulty", "balls", "best", "burst", "hits", "hand", "dealer", "moves", "doubled",
                      "natural"):
                if k in out and out[k] not in (None, ""):
                    detail[k] = out[k]
            log.info("%s: %s (%sx, zero bet) plan %s%s", g.name, out["outcome"], out["multiplier"], plan.get("text"),
                     "" if detail["plan_kept"] else " (not as planned)")
            return RoundResult(g.id, f"hb-{g.id}-{self._n}", 0.0, round(out["multiplier"], 2), 0.0, None, True, detail)

    async def _recover(self):
        self._screen_ok = False
        g = self.current
        alive = self._chrome is not None and self._chrome.poll() is None
        if not alive or self._page is None or self._page.is_closed():
            await self._close()
            await asyncio.to_thread(self._kill_procs)
            self._page = None
        if g:
            await self._load_any(g)
        elif self._page is None:
            await self._start()

    async def _close(self):
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._page = None
