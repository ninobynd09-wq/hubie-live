"""Health checks. Each returns None when fine, or a short reason when not.

They look at evidence from outside the part: X protocol replies, heartbeat files
written from inside each main loop, and the pixels the encoder actually sees.
"""
from __future__ import annotations

import hashlib
import math
import os
import socket
import struct
import time
import urllib.request
from pathlib import Path

from .util import read_json


def x_server_alive(display: str, timeout: float = 2.0) -> str | None:
    """Real X11 connection setup: a stopped or hung server won't answer."""
    num = display.lstrip(":").split(".")[0]
    path = f"/tmp/.X11-unix/X{num}"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(struct.pack("<cxHHHHxx", b"l", 11, 0, 0, 0))
            first = s.recv(1)
            if not first:
                return "x server closed the connection"
            return None
    except (OSError, socket.timeout) as e:
        return f"x server not answering ({type(e).__name__})"


def stage_alive(base_url: str, timeout: float = 2.0) -> str | None:
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=timeout) as r:
            return None if r.status == 200 else f"stage http {r.status}"
    except Exception as e:
        return f"stage not answering ({type(e).__name__})"


def browser_process_alive(port: int, timeout: float = 3.0) -> str | None:
    """Chrome's main process answers this. The page can keep rendering after main hangs,
    so a hang there is invisible to the picture and page checks."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as r:
            return None if r.status == 200 else f"browser http {r.status}"
    except Exception as e:
        return f"browser main process not answering ({type(e).__name__})"


def status_fresh(status_file: Path, max_age: float, now: float | None = None) -> str | None:
    d = read_json(status_file)
    if not d or "ts" not in d:
        return "no heartbeat yet"
    age = (now or time.time()) - d["ts"]
    return None if age <= max_age else f"heartbeat stale {age:.0f}s"


def read_pgm(path: Path) -> bytes | None:
    """Pixels of a binary PGM, or None if missing or half written."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    parts = raw.split(None, 3)
    if len(parts) < 4 or parts[0] != b"P5":
        return None
    try:
        w, h = int(parts[1]), int(parts[2])
        maxval = parts[3].split(None, 1)[0]
    except (ValueError, IndexError):
        return None
    start = len(raw) - len(parts[3]) + len(maxval) + 1
    pixels = raw[start:]
    return pixels if len(pixels) == w * h else None


def picture_stats(pixels: bytes) -> tuple[float, float]:
    n = len(pixels)
    mean = sum(pixels) / n
    var = sum((p - mean) ** 2 for p in pixels) / n
    return mean, math.sqrt(var)


class PictureProbe:
    """Tracks the downscaled frame the encoder writes once a second."""

    def __init__(self, path: Path, now=time.time):
        self.path = Path(path)
        self.now = now
        self.last_hash = None
        self.last_change = now()
        self.blank_since = None
        self.last_stats = (0.0, 0.0)

    def reset(self) -> None:
        self.last_hash = None
        self.last_change = self.now()
        self.blank_since = None

    def file_age(self) -> float:
        try:
            return self.now() - os.path.getmtime(self.path)
        except OSError:
            return float("inf")

    def update(self) -> None:
        pixels = read_pgm(self.path)
        if pixels is None:
            return
        h = hashlib.md5(pixels).digest()
        t = self.now()
        if h != self.last_hash:
            self.last_hash = h
            self.last_change = t
        self.last_stats = stats = picture_stats(pixels)
        if stats[1] < 3.0:   # nearly uniform: black X root, white page, crashed tab
            self.blank_since = self.blank_since or t
        else:
            self.blank_since = None

    def frozen_for(self) -> float:
        return self.now() - self.last_change

    def blank_for(self) -> float:
        return self.now() - self.blank_since if self.blank_since else 0.0


def parse_progress_line(line: str, state: dict) -> None:
    if "=" not in line:
        return
    k, v = line.strip().split("=", 1)
    if k == "frame":
        try:
            n = int(v)
        except ValueError:
            return
        if n != state.get("frame"):
            state["frame"] = n
            state["frame_changed"] = time.time()
    elif k in ("fps", "speed", "drop_frames", "dup_frames", "total_size", "out_time_ms", "bitrate"):
        state[k] = v
