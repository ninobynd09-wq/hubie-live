"""Music bed: shuffled background tracks under Hubie's voice, ducked while he talks.

Stream health comes first. Tracks are decoded by ffmpeg in a background thread into a bounded
buffer; the mixer only ever takes what is already there (never waits), so a slow disk, a broken
file or a dead decoder just means silence under the voice, never a stalled PCM writer.

Packs: assets/music/<pack>/ each hold one style (house, lofi, lounge, night; LICENSES.md in each).
HUBIE_MUSIC_DIR names the pack folder to play (default assets/music/house). HUBIE_MUSIC_PACKS="lofi,night"
rotates packs instead, HUBIE_MUSIC_PACK_TRACKS tracks each (default 5); a bare name is looked up under
HUBIE_MUSIC_ROOT (default assets/music), a path is used as it is. HUBIE_MUSIC_PACK_FILE names a small text
file holding the same "a,b" list: it is re-read between tracks, so the pack can be switched mid-stream
without a restart (missing/empty/unknown names = fall back to the env choice).

Levels: the tracks are loudness-normalised to about -24 LUFS. HUBIE_MUSIC_GAIN_DB is added on top
(default -8: the bed sits near -32 LUFS, the voice runs near -23 LUFS), and HUBIE_MUSIC_DUCK_DB more
comes off while a line plays (default 9), with a ~150 ms attack and a ~600 ms release.
HUBIE_MUSIC_DIR set to an empty string turns the bed off.
"""
from __future__ import annotations

import logging
import math
import os
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np

log = logging.getLogger("hubie.music")

RATE = 48000
CHANNELS = 2
FRAME = CHANNELS * 2                     # bytes per stereo s16 frame
EXTS = (".mp3", ".ogg", ".opus", ".wav", ".flac", ".m4a")
MUSIC_ROOT = Path(__file__).resolve().parent.parent.parent / "assets/music"
DEFAULT_PACK = "house"
DEFAULT_DIR = MUSIC_ROOT / DEFAULT_PACK
DEFAULT_GAIN_DB = -8.0
DEFAULT_PACK_TRACKS = 5
DEFAULT_DUCK_DB = 9.0

Decoder = Callable[[Path], Iterable[bytes]]


def db_to_amp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def ffmpeg_decoder(path: Path, block: int = RATE * FRAME // 5) -> Iterator[bytes]:
    """Decode one file to 48 kHz stereo s16le. The pipe blocks when the buffer is full, so ffmpeg idles."""
    p = subprocess.Popen(
        ["nice", "-n", "10", "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-threads", "1",
         "-i", str(path), "-map", "0:a:0", "-vn", "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", str(RATE), "-ac", str(CHANNELS), "pipe:1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert p.stdout is not None
        while True:
            data = p.stdout.read(block)
            if not data:
                break
            yield data
    finally:
        try:
            p.kill()
        except OSError:
            pass
        p.wait()


def resolve_packs(spec: str, root: Path = MUSIC_ROOT) -> list[Path]:
    """"lofi, night" -> [root/lofi, root/night]; an entry with a "/" is a path. Folders without tracks are dropped."""
    out: list[Path] = []
    for name in (spec or "").split(","):
        name = name.strip()
        if not name:
            continue
        d = Path(name) if "/" in name else Path(root) / name
        if d not in out and list_tracks(d):
            out.append(d)
    return out


def list_tracks(music_dir: Path) -> list[Path]:
    try:
        return sorted(p for p in Path(music_dir).iterdir()
                      if p.suffix.lower() in EXTS and not p.name.startswith(".") and p.is_file())
    except OSError:
        return []


class Ducker:
    """Smoothed duck amount 0..1 (1 = fully ducked), stepped once per chunk, ramped across it."""

    def __init__(self, gain_db: float, duck_db: float, attack_s: float = 0.15, release_s: float = 0.6):
        self.gain_db = gain_db
        self.duck_db = abs(duck_db)
        self.attack_s = attack_s
        self.release_s = release_s
        self.d = 0.0

    def gain_db_now(self) -> float:
        return self.gain_db - self.duck_db * self.d

    def step(self, speaking: bool, dt: float) -> tuple[float, float]:
        """Advance by dt seconds; returns (amp at chunk start, amp at chunk end)."""
        start = db_to_amp(self.gain_db_now())
        target = 1.0 if speaking else 0.0
        tau = self.attack_s if target > self.d else self.release_s
        a = 1.0 - math.exp(-dt / tau) if tau > 0 else 1.0
        self.d += (target - self.d) * a
        if abs(self.d - target) < 1e-4:
            self.d = target
        return start, db_to_amp(self.gain_db_now())


def mix_chunk(voice: bytes, music: bytes, amp_start: float, amp_end: float) -> bytes:
    """voice + music * (linear ramp amp_start -> amp_end), stereo s16le, clipped. len(out) == len(voice)."""
    v = np.frombuffer(voice, dtype=np.int16)
    m = np.frombuffer(music[:len(voice)], dtype=np.int16)
    if len(m) < len(v):
        m = np.concatenate([m, np.zeros(len(v) - len(m), dtype=np.int16)])
    frames = len(v) // CHANNELS
    ramp = np.linspace(amp_start, amp_end, frames, endpoint=False, dtype=np.float32) if frames else \
        np.zeros(0, dtype=np.float32)
    out = v[:frames * CHANNELS].astype(np.float32).reshape(frames, CHANNELS)
    out += m[:frames * CHANNELS].astype(np.float32).reshape(frames, CHANNELS) * ramp[:, None]
    np.clip(out, -32768, 32767, out=out)
    res = out.astype(np.int16).tobytes()
    return res + voice[len(res):]


class MusicBed:
    def __init__(self, music_dir: Path, gain_db: float = DEFAULT_GAIN_DB, duck_db: float = DEFAULT_DUCK_DB,
                 attack_s: float = 0.15, release_s: float = 0.6, buffer_s: float = 4.0,
                 decoder: Decoder = ffmpeg_decoder, rng: random.Random | None = None, retry_s: float = 30.0,
                 packs: list[Path] | None = None, pack_tracks: int = DEFAULT_PACK_TRACKS,
                 pack_file: Path | None = None, music_root: Path = MUSIC_ROOT):
        self.dir = Path(music_dir)                  # the pack playing now
        self.packs = [Path(p) for p in packs] if packs else [self.dir]   # the env choice
        self.pack_tracks = max(1, int(pack_tracks))
        self.pack_file = Path(pack_file) if pack_file else None
        self.music_root = Path(music_root)
        self._pack_file_seen: tuple[float, str] | None = None
        self._pack_file_packs: list[Path] = []
        self._queues: dict[Path, list[Path]] = {}
        self._pack_i = 0
        self._in_pack = 0
        self._switch_checked = 0.0
        self.ducker = Ducker(gain_db, duck_db, attack_s, release_s)
        self.max_bytes = int(buffer_s * RATE) * FRAME
        self.decoder = decoder
        self.rng = rng or random.Random()
        self.retry_s = retry_s
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._room = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.track = ""
        self.tracks_played = 0
        self.underruns = 0         # chunks that got less music than asked (silence filled in)
        self.errors = 0

    @classmethod
    def from_env(cls, env=os.environ) -> "MusicBed | None":
        """None when the bed is off (HUBIE_MUSIC_DIR set to ''), or the folder has no tracks."""
        raw = env.get("HUBIE_MUSIC_DIR")
        if raw is not None and raw.strip() == "":
            return None
        d = Path(raw) if raw else DEFAULT_DIR
        root = Path(env.get("HUBIE_MUSIC_ROOT") or MUSIC_ROOT)

        def num(name, default):
            try:
                return float(env.get(name) or default)
            except ValueError:
                return default
        packs = resolve_packs(env.get("HUBIE_MUSIC_PACKS") or "", root)
        if not packs and list_tracks(d):
            packs = [d]
        pack_file = env.get("HUBIE_MUSIC_PACK_FILE") or None
        if not packs:
            log.info("music bed off: no tracks in %s", d)
            return None
        return cls(packs[0], num("HUBIE_MUSIC_GAIN_DB", DEFAULT_GAIN_DB), num("HUBIE_MUSIC_DUCK_DB", DEFAULT_DUCK_DB),
                   packs=packs, pack_tracks=int(num("HUBIE_MUSIC_PACK_TRACKS", DEFAULT_PACK_TRACKS)),
                   pack_file=Path(pack_file) if pack_file else None, music_root=root)

    # -- feeder thread ----------------------------------------------------------------------------------
    def start(self) -> "MusicBed":
        if self._thread is None:
            self._thread = threading.Thread(target=self._feed_forever, name="music-bed", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._room.notify_all()

    def _order(self, tracks: list[Path], last: Path | None) -> list[Path]:
        order = list(tracks)
        self.rng.shuffle(order)
        if len(order) > 1 and order[0] == last:     # never the same track twice in a row across a reshuffle
            order.append(order.pop(0))
        return order

    def _push(self, data: bytes) -> bool:
        with self._lock:
            while len(self._buf) >= self.max_bytes and not self._stop.is_set():
                self._room.wait(0.5)
            if self._stop.is_set():
                return False
            self._buf += data
        return True

    def _switch_requested(self) -> bool:
        """The pack file now names other packs than the one playing (checked at most once a second)."""
        if self.pack_file is None:
            return False
        now = time.monotonic()
        if now - self._switch_checked < 1.0:
            return False
        self._switch_checked = now
        return self.dir not in self._packs_now()

    def _play(self, path: Path) -> int:
        """Decode one track into the buffer. A pack switch fades the rest out over ~1.5 s and ends it early."""
        got = 0
        fade_left = -1                  # frames of fade still to go; -1 = no switch yet
        fade_total = int(1.5 * RATE)
        it = iter(self.decoder(path))
        try:
            for data in it:
                if fade_left < 0 and self._switch_requested():
                    log.info("music pack switch: fading out %s", path.name)
                    fade_left = fade_total
                if fade_left >= 0:
                    a = np.frombuffer(data[:len(data) - len(data) % FRAME], dtype=np.int16)
                    a = a.reshape(-1, CHANNELS).astype(np.float32)
                    n = len(a)
                    g = np.clip((fade_left - np.arange(n, dtype=np.float32)) / fade_total, 0.0, 1.0)
                    data = (a * g[:, None]).astype(np.int16).tobytes()
                    fade_left = max(0, fade_left - n)
                if not self._push(data):
                    break
                got += len(data)
                if fade_left == 0:
                    break
        finally:
            close = getattr(it, "close", None)
            if close:
                close()
        return got

    def _packs_now(self) -> list[Path]:
        """The pack file's choice when it names a pack with tracks, else the env choice. Never raises."""
        if self.pack_file is not None:
            try:
                st = self.pack_file.stat()
                text = self.pack_file.read_text(errors="replace")[:500]
                key = (st.st_mtime, text)
                if key != self._pack_file_seen:
                    self._pack_file_seen = key
                    self._pack_file_packs = resolve_packs(text.replace("\n", ","), self.music_root)
                    if self._pack_file_packs:
                        log.info("music packs from %s: %s", self.pack_file,
                                 ",".join(p.name for p in self._pack_file_packs))
            except OSError:
                self._pack_file_seen = None
                self._pack_file_packs = []
            if self._pack_file_packs:
                return self._pack_file_packs
        return self.packs

    def _next_track(self, last: Path | None) -> Path | None:
        """Next track of the pack in turn; each pack keeps its own shuffled queue (no repeats until it runs out)."""
        packs = self._packs_now()
        if self.dir not in packs:                   # pack choice changed: start the new list from its top
            self._pack_i, self._in_pack = 0, 0
        elif self._in_pack >= self.pack_tracks and len(packs) > 1:
            self._pack_i, self._in_pack = packs.index(self.dir) + 1, 0
        else:
            self._pack_i = packs.index(self.dir)
        for k in range(len(packs)):
            d = packs[(self._pack_i + k) % len(packs)]
            q = self._queues.get(d)
            if not q:
                q = self._queues[d] = self._order(list_tracks(d), last)
            while q and not q[0].is_file():         # deleted since it was listed
                q.pop(0)
            if q:
                if d != self.dir:
                    self._in_pack = 0
                self.dir = d
                return q.pop(0)
        return None

    def _feed_forever(self) -> None:
        last = None
        fails = 0
        while not self._stop.is_set():
            path = self._next_track(last)
            if path is None or fails >= 2 * max(1, len(list_tracks(self.dir))):
                # nothing playable (empty folders, decoder gone): silence, retry later
                self.track = ""
                fails = 0
                self._stop.wait(self.retry_s)
                continue
            self.track = path.name
            try:
                got = self._play(path)
            except Exception as e:      # a bad file or a dead decoder: skip it, the stream goes on
                self.errors += 1
                log.warning("music track %s failed: %r", path.name, e)
                got = 0
            last = path
            if got >= FRAME * RATE:     # at least a second of audio counts as a played track
                fails = 0
                self.tracks_played += 1
                self._in_pack += 1
            else:
                fails += 1

    # -- mixer side (never blocks on the feeder beyond a short lock) --------------------------------------
    def pull(self, n: int) -> bytes:
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            self._room.notify()
        if len(out) < n:
            self.underruns += 1
        return out

    def buffered_s(self) -> float:
        return len(self._buf) / (RATE * FRAME)

    def mix(self, voice: bytes, speaking: bool) -> bytes:
        """The voice chunk with the bed under it. With no music buffered the voice comes back unchanged."""
        n = len(voice) - len(voice) % FRAME
        amp0, amp1 = self.ducker.step(speaking, n / (RATE * FRAME))
        music = self.pull(n)
        if not music:
            return voice
        return mix_chunk(voice, music, amp0, amp1)

    def status(self) -> dict:
        return {"music_pack": self.dir.name, "music_track": self.track, "music_buffer_s": round(self.buffered_s(), 2),
                "music_underruns": self.underruns, "music_duck_db": round(self.ducker.duck_db * self.ducker.d, 2),
                "music_tracks_played": self.tracks_played, "music_errors": self.errors}
