"""The voice path: SafeLine -> TTS -> file queue -> mixer. The queue is on disk so
brain and mixer can each crash and restart without losing the other."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ..safety.output_filter import SafeLine
from . import lipsync
from ..tts.base import TTSEngine
from ..util import atomic_write_json

log = logging.getLogger("hubie.speaker")


class Speaker:
    def __init__(self, tts: TTSEngine, queue_dir: Path, ttl: float = 90.0, now=time.time):
        self.tts = tts
        self.dir = Path(queue_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl
        self.now = now
        self.seq = int(self.now() * 1000)

    def say(self, line: SafeLine, kind: str = "line", mood: str = "idle", channel: str = "voice",
            reply_to: dict | None = None, droppable: bool = False) -> bool:
        if not isinstance(line, SafeLine):
            raise TypeError("Speaker only accepts SafeLine")
        self.seq += 1
        stem = f"{self.seq:015d}"
        wav = self.dir / f"{stem}.wav"
        spoken = line.text.replace("findahelpline.com", "find a helpline dot com")
        try:
            dur = self.tts.synthesize(spoken, wav)
        except Exception as e:
            log.error("tts failed: %r", e)
            return False
        atomic_write_json(self.dir / f"{stem}.json", {
            "wav": wav.name, "text": line.text, "kind": kind, "mood": mood, "channel": channel,
            "duration": dur, "queued_at": self.now(), "expires_at": self.now() + self.ttl,
            "visemes": lipsync.plan(spoken, wav, getattr(self.tts, "last_alignment", None)),
            **({"reply_to": reply_to} if reply_to else {}), **({"droppable": True} if droppable else {})})
        return True

    def pending(self) -> int:
        """Lines queued or playing (the mixer removes the sidecar when a clip ends)."""
        return sum(1 for p in self.dir.glob("*.json") if not p.name.startswith("."))
