"""Persona loader. The persona is data in one swappable YAML file."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

REQUIRED_EVENTS = ["round_win", "round_loss", "chat_reply", "idle", "rg_reminder", "support", "age",
                   "advice", "deflect", "last_resort"]


@dataclass
class Persona:
    name: str
    system: str
    canned: dict[str, list[str]]
    topics: list[str]
    night_topics: list[str]
    status: str = "unknown"
    lowercase: bool = False
    tics: dict[str, int] = field(default_factory=dict)
    tails: list[str] = field(default_factory=list)
    favorite_games: list[str] = field(default_factory=list)
    games: dict[str, dict] = field(default_factory=dict)
    segments: dict[str, dict] = field(default_factory=dict)
    schedule: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> "Persona":
        data = yaml.safe_load(Path(path).read_text())
        canned = data.get("canned") or {}
        missing = [e for e in REQUIRED_EVENTS if not canned.get(e)]
        if missing:
            raise ValueError(f"persona missing canned events: {missing}")
        return cls(data["name"], data["system"], canned, data.get("topics") or ["nothing in particular"],
                   data.get("night_topics") or [], data.get("status", "unknown"),
                   bool(data.get("lowercase")), data.get("tics") or {}, data.get("tails") or [],
                   data.get("favorite_games") or [], data.get("games") or {},
                   data.get("segments") or {}, data.get("schedule") or [])

    def game_line(self, game_id: str | None) -> str:
        return (self.games.get(game_id or "") or {}).get("line", "")
