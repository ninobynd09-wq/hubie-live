"""Cloud voice over a simple 'POST text, get audio back' speech endpoint.

Compatible request body: {"model","voice","input","response_format":"wav"}.
Tested only against a local stub. URL and key come from env.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx

from .base import TTSEngine, TTSError, normalize_wav


class HttpTTS(TTSEngine):
    name = "http"

    def __init__(self, url: str, key: str = "", model: str = "", voice: str = "", timeout: float = 15.0):
        self.url, self.key, self.model, self.voice, self.timeout = url, key, model, voice, timeout

    def synthesize(self, text: str, out_wav: Path) -> float:
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        body = {"model": self.model, "voice": self.voice, "input": text, "response_format": "wav"}
        try:
            r = httpx.post(self.url, json=body, headers=headers, timeout=self.timeout)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise TTSError(type(e).__name__) from e
        with tempfile.NamedTemporaryFile(dir=str(Path(out_wav).parent), suffix=".wav") as tmp:
            tmp.write(r.content)
            tmp.flush()
            try:
                return normalize_wav(Path(tmp.name), Path(out_wav))
            except Exception as e:
                raise TTSError(f"bad audio: {e!r}") from e


def build_tts(name: str, env: dict) -> TTSEngine:
    from .base import ChainTTS
    from .flite import FliteTTS
    from .mock import MockTTS
    if name == "mock":
        return MockTTS()
    if name == "flite":
        return ChainTTS([FliteTTS(), MockTTS()])
    if name == "http":
        return ChainTTS([HttpTTS(env["HUBIE_TTS_URL"], env.get("HUBIE_TTS_KEY", ""),
                                 env.get("HUBIE_TTS_MODEL", ""), env.get("HUBIE_TTS_VOICE", "")),
                         FliteTTS(), MockTTS()])
    if name == "elevenlabs":
        from .eleven import RICK, ElevenLabsTTS, SilentTTS
        return ChainTTS([ElevenLabsTTS(env.get("ELEVENLABS_API_KEY", ""), env.get("HUBIE_ELEVEN_VOICE", RICK),
                                       env.get("HUBIE_ELEVEN_MODEL", "eleven_v4_turbo")), SilentTTS()])
    raise ValueError(f"unknown tts {name}")
