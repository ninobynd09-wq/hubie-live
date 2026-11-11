"""Persistent on-screen text. Burned in by the encoder so it survives anything
the browser does, including the browser being dead."""
from __future__ import annotations

import os

STRIP_TEXT = "18+   |   Gamble responsibly   |   AI-generated character   |   Demo play, no real money"
TITLE = os.environ.get("HUBIE_STREAM_TITLE",
                       "Hubie, live 24/7: demo games and chat | AI-generated streamer | 18+")


def required_phrases() -> list[str]:
    return ["18+", "Gamble responsibly", "AI-generated"]


def title_is_compliant(title: str) -> bool:
    low = title.lower()
    return "ai" in low.split() or "ai-generated" in low
