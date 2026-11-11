"""The picture without a browser: ffmpeg draws the art and these small text files, and this
writes them. Writes are atomic because ffmpeg re-reads them every frame. Cosmetic: a failure
here must never touch the stream, so nothing raises out of it."""
from __future__ import annotations

import asyncio
import logging
import os
import textwrap
from collections import deque
from pathlib import Path

log = logging.getLogger("hubie.scene")
FILES = ("caption", "game", "history", "segment", "chat")
MOUTH_OPEN_AT = 0.3


def _write(path: Path, text: str) -> None:
    # drawtext draws a box for a trailing newline and can't take an empty file.
    data = text.strip() or " "
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def fit(text: str, width: int, lines: int) -> str:
    wrapped = textwrap.wrap(text or "", width=width, break_long_words=True)
    if len(wrapped) > lines:
        wrapped = wrapped[:lines]
        wrapped[-1] = wrapped[-1][:max(0, width - 3)].rstrip() + "..."
    return "\n".join(wrapped)


def prepare(scene_dir: Path, art_dir: Path) -> None:
    """Everything the encoder needs must exist before it starts, whatever else is down."""
    scene_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        p = scene_dir / f"{name}.txt"
        if not p.exists():
            _write(p, "")
    set_mouth(scene_dir, art_dir, False)


def set_mouth(scene_dir: Path, art_dir: Path, is_open: bool) -> None:
    src = art_dir / ("hubie_open.png" if is_open else "hubie_closed.png")
    tmp = scene_dir / f".hubie.{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(src, tmp)
    os.replace(tmp, scene_dir / "hubie.png")


class Scene:
    def __init__(self, scene_dir: Path, art_dir: Path):
        self.dir, self.art = Path(scene_dir), Path(art_dir)
        prepare(self.dir, self.art)
        self.chat = deque(maxlen=7)
        self.history = deque(maxlen=8)
        self.speech: asyncio.Task | None = None

    def _put(self, name: str, text: str) -> None:
        try:
            _write(self.dir / f"{name}.txt", text)
        except OSError as e:
            log.warning("scene write failed: %r", e)

    def _mouth(self, is_open: bool) -> None:
        try:
            set_mouth(self.dir, self.art, is_open)
        except OSError as e:
            log.warning("mouth swap failed: %r", e)

    def apply(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "segment":
            self._put("segment", fit(str(ev.get("title") or ev.get("kind") or ""), 40, 1))
            if ev.get("game"):
                self._put("game", f"{ev['game']}\nwaiting for the next round")
        elif t == "round":
            mult = ev.get("multiplier", 0)
            word = "won" if ev.get("won") else "lost"
            self._put("game", f"{ev.get('game', '')}\n{mult}x  {word}\ndemo balance {ev.get('balance', 0)}")
            self.history.append(f"{ev.get('game', '')} {mult}x")
            self._put("history", "   ".join(list(self.history)[-4:]))
        elif t == "chat":
            self.chat.append(fit(f"{ev.get('name', '')}: {ev.get('text', '')}", 40, 2))
            self._put("chat", "\n".join(self.chat))
        elif t == "speak":
            self._put("caption", fit(str(ev.get("text", "")), 62, 4))
            if self.speech:
                self.speech.cancel()
            self.speech = asyncio.ensure_future(self._speak(ev.get("env") or [], float(ev.get("duration") or 0)))

    async def _speak(self, env: list, duration: float) -> None:
        last = None
        try:
            for v in env:
                is_open = v >= MOUTH_OPEN_AT
                if is_open != last:
                    self._mouth(is_open)
                    last = is_open
                await asyncio.sleep(0.05)
            self._mouth(False)
            await asyncio.sleep(max(1.0, duration - len(env) * 0.05))
            self._put("caption", "")
        except asyncio.CancelledError:
            pass
