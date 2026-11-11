"""MockTTS: speech-shaped tone with no dependencies. Duration tracks word count."""
from __future__ import annotations

import array
import math
import wave
from pathlib import Path

from .base import RATE, TTSEngine


class MockTTS(TTSEngine):
    name = "mock"

    def __init__(self, seconds_per_word: float = 0.28):
        self.spw = seconds_per_word

    def synthesize(self, text: str, out_wav: Path) -> float:
        dur = max(0.4, len(text.split()) * self.spw)
        n = int(dur * RATE)
        samples = array.array("h", (
            int(9000 * (0.55 + 0.45 * math.sin(2 * math.pi * 4 * i / RATE)) * math.sin(2 * math.pi * 170 * i / RATE))
            for i in range(n)))
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(samples.tobytes())
        return dur
