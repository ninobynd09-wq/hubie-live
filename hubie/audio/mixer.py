"""Mixer: writes a continuous real-time PCM stream to the encoder's FIFO.

Silence when idle, voice clips from the queue when not. The FIFO is non-blocking:
if the encoder stops reading, audio is dropped and counted rather than the mixer
hanging. So a stale mixer heartbeat always means the mixer itself is in trouble.
"""
from __future__ import annotations

import audioop
import errno
import fcntl
import json
import logging
import os
import struct
import termios
import threading
import time
import wave
from pathlib import Path

from ..config import Config, load_dotenv
from .music import MusicBed
from ..util import post_stage, write_status

log = logging.getLogger("hubie.mixer")
RATE = 48000
CHANNELS = 2
BYTES_PER_SEC = RATE * CHANNELS * 2
CHUNK_S = 0.04
CHUNK = int(BYTES_PER_SEC * CHUNK_S)
F_SETPIPE_SZ = 1031
SILENCE = b"\x00" * CHUNK


def envelope(pcm_mono: bytes, step_s: float = 0.05) -> list[float]:
    """Mouth shapes for the avatar: normalised loudness every 50 ms."""
    step = int(RATE * step_s) * 2
    vals = [audioop.rms(pcm_mono[i:i + step], 2) for i in range(0, len(pcm_mono), step)]
    peak = max(vals) if vals else 0
    return [round(v / peak, 2) if peak else 0.0 for v in vals]


class Clip:
    def __init__(self, meta_path: Path):
        self.meta_path = meta_path
        self.meta = json.loads(meta_path.read_text())
        self.wav_path = meta_path.parent / self.meta["wav"]
        with wave.open(str(self.wav_path), "rb") as w:
            mono = w.readframes(w.getnframes())
        self.env = envelope(mono)
        self.pcm = audioop.tostereo(mono, 2, 1, 1)
        self.pos = 0

    def read(self, n: int) -> bytes:
        out = self.pcm[self.pos:self.pos + n]
        self.pos += n
        return out

    @property
    def finished(self) -> bool:
        return self.pos >= len(self.pcm)

    def remove(self) -> None:
        for p in (self.meta_path, self.wav_path):
            try:
                p.unlink()
            except OSError:
                pass


def newer_round(metas) -> bool:
    """True if any of these queued lines is a round line (they are all newer than the one being looked at)."""
    for p in metas:
        try:
            if json.loads(p.read_text()).get("kind") == "round":
                return True
        except (OSError, ValueError):
            continue
    return False


class Mixer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.queue_dir = cfg.queue_dir
        self.fd = os.open(cfg.fifo, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.fcntl(self.fd, F_SETPIPE_SZ, 32768)
        except OSError:
            pass
        self.voice: Clip | None = None
        self.voice_t0 = 0.0
        self.gap_until = 0.0
        self.written = 0
        self.dropped = 0
        self.clips = 0
        self.last_clip_end = time.time()
        self.leftover = b""
        self.skipped = 0     # plain-round lines skipped because a newer round line was already waiting
        self.music: MusicBed | None = None
        try:
            self.music = MusicBed.from_env()
            if self.music:
                self.music.start()
        except Exception as e:   # no music is fine; a mixer that won't start is not
            log.warning("music bed off: %r", e)
            self.music = None

    def _next_clip(self) -> None:
        now = time.time()
        if now < self.gap_until:
            return
        metas = sorted(p for p in self.queue_dir.glob("*.json") if not p.name.startswith("."))
        for i, meta in enumerate(metas):
            try:
                clip = Clip(meta)
            except Exception as e:
                log.warning("bad clip %s: %r", meta.name, e)
                for q in (meta, meta.with_suffix(".wav")):
                    try:
                        q.unlink()
                    except OSError:
                        pass
                continue
            if clip.meta.get("expires_at", now + 1) < now:
                clip.remove()
                continue
            if clip.meta.get("droppable") and newer_round(metas[i + 1:]):
                # a plain-round line never queues up behind the show: the newer round has its own line
                clip.remove()
                self.skipped += 1
                continue
            self.voice = clip
            # the clip's first chunk is written this tick; it reaches the encoder once whatever already sits in the
            # FIFO has been read. The stage plays the mouth from this moment, not from when the event arrives
            self.voice_t0 = time.time() + self._backlog() / BYTES_PER_SEC
            threading.Thread(target=post_stage, args=(self.cfg.stage_url, {
                "type": "speak", "text": clip.meta["text"], "mood": clip.meta.get("mood", "idle"),
                "duration": clip.meta["duration"], "env": clip.env,
                "kind": clip.meta.get("kind"), "reply_to": clip.meta.get("reply_to"),
                "visemes": clip.meta.get("visemes"), "audio_t0": round(self.voice_t0, 4)}), daemon=True).start()
            return

    def _backlog(self) -> int:
        """Bytes written but not yet read by the encoder (FIFO contents plus the half frame held back)."""
        try:
            n = struct.unpack("i", fcntl.ioctl(self.fd, termios.FIONREAD, b"\0" * 4))[0]
        except (OSError, ValueError, AttributeError):
            n = 0
        return max(0, n) + len(getattr(self, "leftover", b""))

    def _chunk(self, tick: int) -> bytes:
        return self._with_music(self._voice_chunk(tick), self.voice is not None)

    def _voice_chunk(self, tick: int) -> bytes:
        if self.voice is None and tick % 5 == 0:
            self._next_clip()
        if self.voice is None:
            return SILENCE
        data = self.voice.read(CHUNK)
        if self.voice.finished:
            self.voice.remove()
            self.voice = None
            self.clips += 1
            self.last_clip_end = time.time()
            self.gap_until = time.time() + 0.35
        return data + SILENCE[len(data):]

    def _with_music(self, data: bytes, speaking: bool) -> bytes:
        """The music bed under this chunk. Any failure turns the bed off and the voice goes out as it was."""
        if getattr(self, "music", None) is None:
            return data
        try:
            return self.music.mix(data, speaking)
        except Exception as e:
            log.warning("music bed failed, off for this run: %r", e)
            try:
                self.music.stop()
            except Exception:
                pass
            self.music = None
            return data

    def _write(self, data: bytes) -> None:
        buf = self.leftover + data
        self.leftover = b""
        try:
            n = os.write(self.fd, buf)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                self.dropped += len(buf)
                return
            raise
        self.written += n
        if n < len(buf):
            rest = buf[n:]
            # Finish the sample frame that was cut in half so channels stay aligned.
            keep = (4 - n % 4) % 4
            self.leftover = rest[:keep]
            self.dropped += len(rest) - keep

    def run(self) -> None:
        next_t = time.monotonic()
        tick = 0
        last_status = 0.0
        recent_dropped = 0
        while True:
            self._write(self._chunk(tick))
            tick += 1
            now = time.time()
            if now - last_status >= 1.0:
                write_status(self.cfg.status_dir, "mixer", written=self.written, dropped=self.dropped,
                             dropped_delta=self.dropped - recent_dropped, clips=self.clips, skipped=self.skipped,
                             playing=bool(self.voice), last_clip_end=self.last_clip_end,
                             **(self.music.status() if self.music else {"music_track": None}))
                recent_dropped = self.dropped
                last_status = now
            next_t += CHUNK_S
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:   # woke from a long stall: don't burst to catch up
                next_t = time.monotonic()


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config()
    cfg.ensure_dirs()
    Mixer(cfg).run()


if __name__ == "__main__":
    main()
