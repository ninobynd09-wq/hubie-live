"""FakeGameAdapter: plausible demo rounds with no site involved.

Game names here are generic stand-ins, not a claim about the real catalogue.
Failure injection lets tests prove the orchestrator survives a broken table.
"""
from __future__ import annotations

import random
import time

from .base import GameAdapter, GameError, GameInfo, RoundResult

GAMES = [
    GameInfo("dice", "Dice", "threshold", "Pick a target, roll under it, smaller target pays more."),
    GameInfo("limbo", "Limbo", "threshold", "Pick a multiplier, the game draws one, match or beat it."),
    GameInfo("crash", "Crash", "cashout", "A multiplier climbs until it crashes. Cash out before that."),
    GameInfo("mines", "Mines", "grid", "Reveal safe tiles on a grid. Each tile pays more, a mine ends it."),
    GameInfo("plinko", "Plinko", "drop", "A ball bounces down pegs into a paying slot."),
    GameInfo("wheel", "Wheel", "spin", "Spin a wheel of multipliers, land where you land."),
    GameInfo("hilo", "HiLo", "cards", "Guess if the next card is higher or lower."),
    GameInfo("keno", "Keno", "pick", "Pick numbers, the game draws, matches pay."),
    GameInfo("coinflip", "Coinflip", "flip", "Call heads or tails. That's it."),
    GameInfo("dragon_tower", "Dragon Tower", "ladder", "Climb floor by floor, each safe step pays more, a wrong step ends it."),
    GameInfo("blackjack", "Blackjack", "cards", "Get closer to 21 than the dealer without going over."),
    GameInfo("roulette", "Roulette", "spin", "Bet on where the ball lands on the wheel."),
    GameInfo("baccarat", "Baccarat", "cards", "Bet on player or banker, closest to nine wins."),
    GameInfo("max_win", "Max Win", "slot", "Spin the reels, there is a spin and a number."),
]


class FakeGameAdapter(GameAdapter):
    name = "fake"

    def __init__(self, seed: int | None = None, start_balance: float = 1000.0,
                 round_seconds: tuple[float, float] = (1.0, 3.0),
                 fail_rate: float = 0.0, hang_rate: float = 0.0, hang_seconds: float = 3600.0,
                 demo: bool = True, sleep=time.sleep):
        self.rng = random.Random(seed)
        self.balance = start_balance
        self.start_balance = start_balance
        self.round_seconds = round_seconds
        self.fail_rate = fail_rate
        self.hang_rate = hang_rate
        self.hang_seconds = hang_seconds
        self.demo = demo
        self.sleep = sleep
        self.current = GAMES[0].id
        self.n = 0
        self.started = False
        self.recoveries = 0

    def start(self) -> None:
        self.started = True

    def list_games(self) -> list[GameInfo]:
        return list(GAMES)

    def load_game(self, game_id: str) -> None:
        if game_id not in {g.id for g in GAMES}:
            raise GameError(f"unknown game {game_id}")
        self.current = game_id

    def is_demo(self) -> bool:
        return self.demo

    def recover(self) -> None:
        self.recoveries += 1
        self.started = True

    def _outcome(self, options: dict | None = None) -> tuple[float, dict]:
        r, g = self.rng, self.current
        options = options or {}
        if g == "dice":
            target = r.choice([25, 50, 66, 75, 90])
            roll = round(r.uniform(0, 100), 2)
            return (round(99 / target, 2) if roll < target else 0.0), {"target": target, "roll": roll}
        if g == "limbo":
            target = options.get("cashout") or r.choice([1.5, 2, 3, 5, 10])
            drawn = round(min(0.99 / max(r.random(), 1e-4), 10000), 2)
            return (target if drawn >= target else 0.0), {"target": target, "drawn": drawn}
        if g == "crash":
            bust = round(min(0.99 / max(r.random(), 1e-4), 10000), 2)
            cash = r.choice([1.3, 1.5, 2, 3, 5])
            return (cash if bust >= cash else 0.0), {"cashout_at": cash, "crashed_at": bust}
        if g == "mines":
            mines, picks = r.choice([1, 3, 5]), r.randint(1, 5)
            safe = 25 - mines
            p, m = 1.0, 0.99
            for i in range(picks):
                p *= (safe - i) / (25 - i)
                m = 0.99 / p
            return (round(m, 2) if r.random() < p else 0.0), {"mines": mines, "picks": picks}
        if g == "plinko":
            slots = [8.9, 3, 1.4, 1.1, 1, 0.5, 1, 1.1, 1.4, 3, 8.9]
            i = min(sum(r.random() < 0.5 for _ in range(10)), 10)
            return slots[i], {"slot": i}
        if g == "wheel":
            m = r.choice([0, 0, 0, 0, 0, 1.2, 1.2, 1.5, 1.5, 1.7, 2, 3, 5])
            return m, {"landed": m}
        if g == "hilo":
            card, nxt = r.randint(1, 13), r.randint(1, 13)
            guess = "higher" if card <= 7 else "lower"
            hit = nxt > card if guess == "higher" else nxt < card
            return (round(r.uniform(1.1, 2.4), 2) if hit else 0.0), {"card": card, "next": nxt, "guess": guess}
        if g == "coinflip":
            call, flip = r.choice(["heads", "tails"]), r.choice(["heads", "tails"])
            return (1.98 if call == flip else 0.0), {"call": call, "flip": flip}
        if g == "dragon_tower":
            floors = r.randint(1, 4)
            return (round(1.46 ** floors, 2) if r.random() < (2 / 3) ** floors else 0.0), {"floors_tried": floors}
        if g == "blackjack":
            player, dealer = r.randint(15, 24), r.randint(17, 25)
            won = player <= 21 and (dealer > 21 or player > dealer)
            push = player <= 21 and player == dealer
            return (2.0 if won else (1.0 if push else 0.0)), {"player": player, "dealer": dealer}
        if g == "roulette":
            n = r.randint(0, 36)
            return (2.0 if n and n % 2 == 0 else 0.0), {"bet_on": "even", "landed": n}
        if g == "baccarat":
            side, winner = r.choice(["player", "banker"]), r.choices(["player", "banker", "tie"], [44.6, 45.9, 9.5])[0]
            m = 1.0 if winner == "tie" else ((2.0 if side == "player" else 1.95) if side == winner else 0.0)
            return m, {"side": side, "winner": winner}
        if g == "max_win":
            m = r.choices([0, 0.5, 1, 2, 5, 20, 100], [70, 12, 8, 6, 3, 0.9, 0.1])[0]
            return m, {"spin": "done"}
        picks = r.randint(3, 6)
        hits = sum(r.random() < 0.25 for _ in range(picks))
        return [0, 0, 1.2, 2.5, 6, 15, 40][hits], {"picks": picks, "hits": hits}

    def play_round(self, bet: float, options: dict | None = None) -> RoundResult:
        if self.rng.random() < self.fail_rate:
            raise GameError("table glitch (injected)")
        if self.rng.random() < self.hang_rate:
            self.sleep(self.hang_seconds)
        lo, hi = self.round_seconds
        if hi > 0:
            self.sleep(self.rng.uniform(lo, hi))
        self.n += 1
        mult, detail = self._outcome(options)
        payout = round(bet * mult, 2)
        self.balance = round(self.balance - bet + payout, 2)
        if self.balance < bet * 2:
            self.balance = self.start_balance
            detail["demo_refill"] = True
        return RoundResult(self.current, f"fake-{self.n}", bet, mult, payout,
                           self.balance, self.demo, detail)
