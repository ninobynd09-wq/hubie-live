"""ChatSource: where viewer messages come from."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChatMessage:
    username: str
    text: str
    ts: float = field(default_factory=time.time)
    id: str = ""


class ChatSource(abc.ABC):
    @abc.abstractmethod
    def start(self) -> None: ...

    @abc.abstractmethod
    def poll(self) -> list[ChatMessage]:
        """Return new messages. Must never block and never raise."""

    def healthy(self) -> bool:
        return True

    def close(self) -> None:
        return None


class NoChat(ChatSource):
    def start(self) -> None:
        return None

    def poll(self) -> list[ChatMessage]:
        return []

    def healthy(self) -> bool:
        return False
