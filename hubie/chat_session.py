"""One viewer talking to Hubie, through the same moderation, brain and filter as the
stream. Used by tools/chat_with_hubie.py so a chat test proves the real path."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from .brain.brain import Brain
from .brain.memory import Memory
from .brain.persona import Persona
from .brain.provider import LLMProvider
from .chat.base import ChatMessage
from .games.fake import FakeGameAdapter
from .safety.moderation import FIXED_ACTIONS, ChatModerator, clean_chat_text
from .safety.output_filter import OutputFilter


@dataclass
class Turn:
    text: str | None
    action: str
    reason: str
    source: str
    seconds: float


class ChatSession:
    def __init__(self, persona_path: Path | str, provider: LLMProvider, memory_path: Path | str | None = None):
        self.persona = Persona.load(persona_path)
        self.mem = Memory(memory_path)
        self.brain = Brain(self.persona, provider, self.mem, OutputFilter())
        self.mod = ChatModerator()
        self.games = FakeGameAdapter(round_seconds=(0, 0))

    def _source(self, before: dict) -> str:
        after = self.brain.source_counts
        return next((k for k in after if after[k] > before.get(k, 0)), "none")

    def chat(self, username: str, text: str) -> Turn:
        t0 = time.time()
        before = dict(self.brain.source_counts)
        d = self.mod.review(ChatMessage(username, text))
        if d.action == "ignore":
            return Turn(None, d.action, d.reason, "none", time.time() - t0)
        self.mem.note_seen(username)
        action = self.mem.route_crisis(username, d.speak_name, d.action)
        action = self.mem.route_minor(username, action)
        if action == "ignore":
            return Turn(None, action, "minor_already_told", "none", time.time() - t0)
        shown, reason = d.action, d.reason
        if action == "crisis_followup":
            shown, reason = action, "crisis_window"
            line = self.brain.crisis_followup(d.speak_name, clean_chat_text(text), username)
        elif action in FIXED_ACTIONS:
            line = self.brain.fixed(FIXED_ACTIONS[action], name=d.speak_name)
        else:
            self.mem.note_said(username, d.speak_name, d.clean_text)
            line = self.brain.reply_chat(d.speak_name, d.clean_text, username)
            if self.mem.in_crisis(username):   # the model caught what the rules didn't
                shown, reason = "crisis", "llm_flag"
        self.mem.save(force=True)
        return Turn(line.text, shown, reason, self._source(before), time.time() - t0)

    def play_round(self, game_id: str) -> Turn:
        t0 = time.time()
        before = dict(self.brain.source_counts)
        self.games.load_game(game_id)
        r = self.games.play_round(5)
        info = next(g for g in self.games.list_games() if g.id == game_id)
        line = self.brain.comment_round(r, info.name, how=info.how_it_works)
        self.mem.save(force=True)
        return Turn(line.text, f"round {r.multiplier}x", "", self._source(before), time.time() - t0)

    def segment(self, kind: str, topic: str | None = None) -> Turn:
        t0 = time.time()
        before = dict(self.brain.source_counts)
        meta = self.persona.segments[kind]
        topic = topic or (meta.get("topics") or [None])[0]
        line = self.brain.talk_segment(kind, topic, 1, 1, False)
        self.mem.save(force=True)
        return Turn(line.text, f"segment {kind}", "", self._source(before), time.time() - t0)
