"""Local CPU voice through ffmpeg's libflite filter. Robotic, free, offline.
It stays as the final fallback even once a better voice is added."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from .base import TTSEngine, TTSError, normalize_wav


class FliteTTS(TTSEngine):
    name = "flite"

    def __init__(self, voice: str = "slt", rate_factor: float = 0.86, timeout: float = 10.0):
        self.voice = voice
        self.rate_factor = rate_factor
        self.timeout = timeout

    def synthesize(self, text: str, out_wav: Path) -> float:
        with tempfile.TemporaryDirectory(dir=str(Path(out_wav).parent)) as td:
            txt = Path(td) / "t.txt"
            txt.write_text(text)
            raw = Path(td) / "r.wav"
            # Pitch down and slow down a touch: the placeholder sloth voice.
            af = f"asetrate={int(16000 * self.rate_factor)},aresample=48000,volume=1.6"
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "lavfi",
                   "-i", f"flite=textfile={txt}:voice={self.voice}", "-af", af, "-ac", "1", str(raw)]
            try:
                subprocess.run(cmd, check=True, timeout=self.timeout, capture_output=True)
            except (subprocess.SubprocessError, OSError) as e:
                raise TTSError(repr(e)) from e
            return normalize_wav(raw, Path(out_wav))
