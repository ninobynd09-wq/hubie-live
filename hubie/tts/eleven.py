"""ElevenLabs voice through the with-timestamps endpoint: the audio plus when each character is spoken,
which drives Hubie's mouth (hubie/audio/lipsync.py). Same price per character as plain speech.
Key from ELEVENLABS_API_KEY; voice and model from env."""
from __future__ import annotations

import base64
import tempfile
import wave
from pathlib import Path

import httpx

from .base import RATE, TTSEngine, TTSError, normalize_wav

API = "https://api.elevenlabs.io"
RICK = "wcATjh8zBDfepqUbl99V"


def is_quota_error(status: int, body: str = "") -> bool:
    """Out of credits (or the key no longer works): ElevenLabs answers 401 with quota_exceeded, or 402."""
    return status in (401, 402) or (status >= 400 and "quota_exceeded" in (body or ""))


class ElevenLabsTTS(TTSEngine):
    name = "elevenlabs"

    def __init__(self, key: str, voice: str = RICK, model: str = "eleven_v4_turbo", base: str = API,
                 timeout: float = 20.0, settings: dict | None = None):
        if not key:
            raise ValueError("ELEVENLABS_API_KEY is not set")
        self.key, self.voice, self.model, self.base, self.timeout = key, voice, model, base.rstrip("/"), timeout
        self.settings = settings or {"stability": 0.55, "similarity_boost": 0.8, "style": 0.0,
                                     "use_speaker_boost": True}
        self.last_alignment: dict | None = None
        self.chars_sent = 0      # characters billed so far (hubie/credits.py counts between credit polls)
        self.quota_fails = 0     # quota-type refusals in a row (401/402/quota_exceeded); a success resets it

    def synthesize(self, text: str, out_wav: Path) -> float:
        self.last_alignment = None
        url = f"{self.base}/v1/text-to-speech/{self.voice}/with-timestamps?output_format=pcm_24000"
        try:
            r = httpx.post(url, json={"text": text, "model_id": self.model, "voice_settings": self.settings},
                           headers={"xi-api-key": self.key}, timeout=self.timeout)
            if is_quota_error(r.status_code, r.text if r.status_code >= 400 else ""):
                self.quota_fails += 1
                raise TTSError(f"quota {r.status_code}")
            r.raise_for_status()
            d = r.json()
            pcm = base64.b64decode(d["audio_base64"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            raise TTSError(type(e).__name__) from e
        self.quota_fails = 0
        self.chars_sent += len(text)
        return self.write(pcm, d.get("alignment") or d.get("normalized_alignment"), out_wav)

    def write(self, pcm: bytes, alignment: dict | None, out_wav: Path) -> float:
        """24 kHz mono PCM from the API to the 48 kHz WAV the mixer plays."""
        with tempfile.NamedTemporaryFile(dir=str(Path(out_wav).parent), suffix=".wav") as tmp:
            with wave.open(tmp.name, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(pcm[: len(pcm) // 2 * 2])
            try:
                dur = normalize_wav(Path(tmp.name), Path(out_wav))
            except Exception as e:
                raise TTSError(f"bad audio: {e!r}") from e
        if isinstance(alignment, dict) and alignment.get("characters"):
            self.last_alignment = alignment
        return dur


class SilentTTS(TTSEngine):
    """Last resort when the voice is down: silence as long as the line, so the subtitles still carry it.
    (No free voice stands in for Hubie.)"""
    name = "silent"

    def __init__(self, seconds_per_word: float = 0.32):
        self.spw = seconds_per_word

    def synthesize(self, text: str, out_wav: Path) -> float:
        dur = max(0.8, len(text.split()) * self.spw)
        with wave.open(str(out_wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(b"\0\0" * int(dur * RATE))
        return dur
