"""TTS behind an interface. Engines write 48 kHz mono s16 WAV via normalize_wav."""
from __future__ import annotations

import abc
import audioop
import logging
import wave
from pathlib import Path

log = logging.getLogger("hubie.tts")
RATE = 48000


class TTSError(Exception):
    pass


class TTSEngine(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def synthesize(self, text: str, out_wav: Path) -> float:
        """Write speech to out_wav and return duration in seconds. Raises TTSError."""


def normalize_wav(src: Path, dst: Path) -> float:
    """Convert any PCM WAV to 48 kHz mono s16 with no external process."""
    with wave.open(str(src), "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if ch == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    elif ch != 1:
        raise TTSError(f"unsupported channel count {ch}")
    if rate != RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, RATE, None)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return len(pcm) / 2 / RATE


class ChainTTS(TTSEngine):
    """First engine that works wins, so a cloud voice outage degrades instead of silencing."""
    name = "chain"

    def __init__(self, engines: list[TTSEngine]):
        self.engines = engines

    last_alignment: dict | None = None

    def synthesize(self, text: str, out_wav: Path) -> float:
        last: Exception | None = None
        self.last_alignment = None
        for e in self.engines:
            try:
                dur = e.synthesize(text, out_wav)
                # character timings for the mouth, from whichever engine spoke
                self.last_alignment = getattr(e, "last_alignment", None)
                return dur
            except Exception as ex:
                log.warning("tts engine %s failed: %r", e.name, ex)
                last = ex
        raise TTSError(f"all engines failed: {last!r}")
