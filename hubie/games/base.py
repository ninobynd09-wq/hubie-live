"""GameAdapter: what the orchestrator needs from any game surface.

The real adapter (a browser driving the demo-mode games) implements this same
interface later. The orchestrator never knows which one it is talking to.
"""
from __future__ import annotations

import abc
import time
from dataclasses import asdict, dataclass, field
from typing import Any


class GameError(Exception):
    """A round could not be played. The orchestrator recovers and moves on."""


class NotDemoError(GameError):
    """The surface is not in demo mode. The orchestrator refuses to play."""


class GameGone(GameError):
    """This game is off for the rest of the show (e.g. a zero-bet guard tripped on the house originals). Other
    games carry on: the orchestrator drops it from the rotation and ends its segment, without stopping play."""


@dataclass(frozen=True)
class GameInfo:
    id: str
    name: str
    kind: str
    how_it_works: str


@dataclass
class RoundResult:
    game_id: str
    round_id: str
    bet: float
    multiplier: float
    payout: float
    balance: float
    is_demo: bool
    detail: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    @property
    def zero_bet(self) -> bool:
        """A free round (house originals at a 0 bet): bet and payout are 0, the multiplier carries the result and
        detail["won"] whether it paid more than the stake would have."""
        return self.bet == 0 and "won" in (self.detail or {})

    @property
    def won(self) -> bool:
        if self.zero_bet:
            return bool(self.detail["won"])
        return self.payout > self.bet

    @property
    def came_back(self) -> bool:
        """Something came back (any win, or part of the stake)."""
        return self.multiplier > 0 if self.zero_bet else self.payout > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["won"] = self.won
        return d


class GameAdapter(abc.ABC):
    name = "abstract"
    # Set by the orchestrator when the stage has a click clip: called right before a spin (or bonus-buy confirm)
    # is sent, returns the wall-clock time the on-screen claw lands, or None. The adapter then sends the real
    # click at that moment (at most CLICK_MAX_HOLD later); with no answer it clicks at once. Never blocks a spin.
    click_cue = None
    CLICK_MAX_HOLD = 2.0

    def click_at(self, why: str = "spin") -> float | None:
        """When to send the click the stage just planned (wall clock), or None to send it now."""
        if not self.click_cue:
            return None
        try:
            at = self.click_cue(why)
            at = float(at) if at is not None else None
        except Exception:
            return None
        if at is None or not (time.time() < at <= time.time() + self.CLICK_MAX_HOLD):
            return None
        return at

    @abc.abstractmethod
    def start(self) -> None:
        """Open the surface and get it ready for demo play."""

    @abc.abstractmethod
    def list_games(self) -> list[GameInfo]:
        """Games available right now."""

    @abc.abstractmethod
    def load_game(self, game_id: str) -> None:
        """Switch to a game. Raises GameError on failure."""

    @abc.abstractmethod
    def play_round(self, bet: float, options: dict | None = None) -> RoundResult:
        """Play one demo round and return the result. Raises GameError.
        options carries per-game choices such as {"cashout": 1.9}."""

    @abc.abstractmethod
    def is_demo(self) -> bool:
        """True only when the surface is verifiably in demo mode."""

    @abc.abstractmethod
    def recover(self) -> None:
        """Get back to a known good state after a failure. Must not raise."""

    def screen_ready(self) -> bool:
        """True when the game area may be shown on stream (a game is running). Until then the stage keeps a
        "switching game" card over it. Adapters with nothing to hide just say yes."""
        return True

    def screen_state(self) -> str:
        """What the stage should show over the game box: "ready" (the game), "browsing" or "picked" (the adapter
        is visibly browsing to the next game, the page is safe to show), anything else keeps the card up."""
        return "ready" if self.screen_ready() else "loading"

    def poll_events(self) -> list[dict]:
        """Live events from inside a round still running (free spins triggered, a retrigger, a big multiplier,
        the feature total crossing 10x/25x/... the bet, feature_end). Called from another thread while
        play_round runs; returns what came in since the last call. Adapters without them say nothing."""
        return []

    def close(self) -> None:
        return None
