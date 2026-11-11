"""When the show ends on its own: the voice credits are nearly gone, a planned end time was reached, or the voice
started refusing with a quota error. The orchestrator asks should_end() between steps and runs the sign-off.

Credits: ElevenLabs GET /v1/user/subscription every poll_every seconds (on a thread, never on the show loop), and
the characters the voice sent since that poll are counted locally, so the watch reacts long before the next poll.
The API key is only ever put in the request header, never logged or stored here.

The show-over flag (Config.show_over_flag) is what makes the end stick: the supervisor and the guardian see it and
exit without restarting anything. preview.py start (or `python -m hubie new-show`) clears it for the next show.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .util import atomic_write_json, read_json

log = logging.getLogger("hubie.credits")

SUBSCRIPTION_URL = "https://api.elevenlabs.io/v1/user/subscription"
DEFAULT_SIGNOFF_CHARS = 2500
POLL_EVERY = 300.0
QUOTA_FAILS_TO_END = 2     # consecutive quota-type voice refusals (one could be a fluke)


def parse_end_at(value: str | None) -> float | None:
    """HUBIE_END_AT: unix seconds, or an ISO time (no zone given = UTC). Empty or unreadable = no planned end."""
    v = (value or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        log.error("HUBIE_END_AT %r is not a unix time or ISO time, ignored", v)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fetch_subscription(key: str, timeout: float = 8.0) -> tuple[int, int]:
    """(character_limit, character_count) from ElevenLabs. Raises on any failure."""
    import httpx
    r = httpx.get(SUBSCRIPTION_URL, headers={"xi-api-key": key}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    return int(d["character_limit"]), int(d["character_count"])


class EndWatch:
    """voice: the ElevenLabs engine (its chars_sent / quota_fails counters), or None when the show has no paid voice.
    fetch: () -> (limit, count); None = no credit polling (time end only)."""

    def __init__(self, threshold: int = DEFAULT_SIGNOFF_CHARS, end_at: float | None = None, fetch=None, voice=None,
                 poll_every: float = POLL_EVERY, now=time.time):
        self.threshold = int(threshold)
        self.end_at = end_at
        self.fetch = fetch
        self.voice = voice
        self.poll_every = poll_every
        self.now = now
        self.lock = threading.Lock()
        self.limit: int | None = None
        self.count: int | None = None
        self.sent_at_poll = 0
        self.polled_at = 0.0
        self.poll_errors = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- credits ----

    def _sent(self) -> int:
        return int(getattr(self.voice, "chars_sent", 0) or 0)

    def poll(self) -> bool:
        """One subscription read. Returns whether it worked."""
        if self.fetch is None:
            return False
        sent = self._sent()      # read before the call: anything sent during it is counted twice, never missed
        try:
            limit, count = self.fetch()
        except Exception as e:
            self.poll_errors += 1
            log.warning("credit check failed: %s", type(e).__name__)
            return False
        with self.lock:
            self.limit, self.count, self.sent_at_poll, self.polled_at = limit, count, sent, self.now()
        log.info("voice credits: %d of %d left", limit - count, limit)
        return True

    def remaining(self) -> int | None:
        """Characters left: the last poll's limit - count, minus what the voice sent since. None before any poll."""
        with self.lock:
            if self.limit is None or self.count is None:
                return None
            return self.limit - self.count - max(0, self._sent() - self.sent_at_poll)

    def start(self) -> None:
        """Polls now and then every poll_every seconds on a daemon thread."""
        if self.fetch is None or self._thread:
            return

        def loop():
            while not self._stop.is_set():
                self.poll()
                self._stop.wait(self.poll_every)
        self._thread = threading.Thread(target=loop, name="credit-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ---- the decision ----

    def voice_refused(self) -> bool:
        return int(getattr(self.voice, "quota_fails", 0) or 0) >= QUOTA_FAILS_TO_END

    def should_end(self) -> str | None:
        """'credits', 'end_at', 'voice_quota' or None."""
        if self.voice_refused():
            return "voice_quota"
        left = self.remaining()
        if left is not None and left <= self.threshold:
            return "credits"
        if self.end_at is not None and self.now() >= self.end_at:
            return "end_at"
        return None

    def snapshot(self) -> dict:
        return {"credits_left": self.remaining(), "signoff_at": self.threshold, "end_at": self.end_at,
                "polled_at": self.polled_at or None, "voice_refused": self.voice_refused()}


def from_env(env: dict, tts) -> EndWatch:
    """The watch for this run: credit polling only when the paid voice is in the chain and has a key."""
    try:
        threshold = int(env.get("HUBIE_SIGNOFF_CHARS") or DEFAULT_SIGNOFF_CHARS)
    except ValueError:
        threshold = DEFAULT_SIGNOFF_CHARS
    voice = next((e for e in getattr(tts, "engines", [tts]) if getattr(e, "name", "") == "elevenlabs"), None)
    key = env.get("ELEVENLABS_API_KEY", "")
    fetch = (lambda: fetch_subscription(key)) if voice is not None and key else None
    poll = float(env.get("HUBIE_CREDIT_POLL_S") or POLL_EVERY)
    return EndWatch(threshold, parse_end_at(env.get("HUBIE_END_AT")), fetch, voice, poll_every=poll)


# ---- the show-over flag ----

def show_over(flag: Path) -> dict | None:
    """The flag's contents when the show has ended, else None."""
    if not Path(flag).exists():
        return None
    return read_json(flag, {}) or {"reason": "unknown"}


def mark_show_over(flag: Path, **info) -> None:
    Path(flag).parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(flag, {"ts": time.time(), "pid": os.getpid(), **info})


def clear_show_over(flag: Path) -> bool:
    try:
        Path(flag).unlink()
        return True
    except FileNotFoundError:
        return False
