"""Read-only Kick chat over its public Pusher websocket.

Tested only against a local fake socket. Kick's current public app key is not known here (the
old one is refused), so this needs KICK_PUSHER_URL set before it can work. kick_webhook.py is the
documented route. It only ever reads, needs no secret, and reconnects forever with backoff.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque

import websockets.sync.client as ws_client

from .base import ChatMessage, ChatSource

log = logging.getLogger("hubie.chat.kick")


def parse_frame(raw: str) -> ChatMessage | None:
    """Turn one Pusher frame into a ChatMessage, or None if it isn't chat."""
    try:
        frame = json.loads(raw)
        if "ChatMessageEvent" not in str(frame.get("event", "")):
            return None
        data = frame.get("data")
        data = json.loads(data) if isinstance(data, str) else data
        user = (data.get("sender") or {}).get("username")
        text = data.get("content")
        if not user or not isinstance(text, str):
            return None
        return ChatMessage(str(user), text, time.time(), str(data.get("id", "")))
    except (ValueError, AttributeError, TypeError):
        return None


class KickChatSource(ChatSource):
    def __init__(self, url: str, chatroom_id: str, connect=ws_client.connect,
                 max_backoff: float = 30.0, stale_after: float = 300.0):
        self.url = url
        self.channel = f"chatrooms.{chatroom_id}.v2"
        self.connect = connect
        self.max_backoff = max_backoff
        self.stale_after = stale_after
        self.q: queue.Queue[ChatMessage] = queue.Queue(maxsize=2000)
        self.seen: deque[str] = deque(maxlen=500)   # a reconnect can replay messages
        self.stop_evt = threading.Event()
        self.connected = False
        self.last_frame = 0.0
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True, name="kick-chat")
        self.thread.start()

    def _run(self) -> None:
        backoff = 1.0
        while not self.stop_evt.is_set():
            try:
                with self.connect(self.url, open_timeout=10, close_timeout=2) as sock:
                    sock.send(json.dumps({"event": "pusher:subscribe",
                                          "data": {"auth": "", "channel": self.channel}}))
                    self.connected = True
                    backoff = 1.0
                    while not self.stop_evt.is_set():
                        try:
                            raw = sock.recv(timeout=self.stale_after)
                        except TimeoutError:
                            log.warning("no frames for %ss, reconnecting", self.stale_after)
                            break
                        self.last_frame = time.time()
                        if "pusher:ping" in raw:
                            sock.send(json.dumps({"event": "pusher:pong", "data": {}}))
                            continue
                        if "pusher:error" in raw:
                            log.warning("chat socket refused us: %s", raw[:200])
                            break
                        msg = parse_frame(raw)
                        if msg and msg.id and msg.id in self.seen:
                            continue
                        if msg:
                            self.seen.append(msg.id)
                            try:
                                self.q.put_nowait(msg)
                            except queue.Full:
                                pass
            except Exception as e:
                log.warning("chat socket error: %r", e)
            self.connected = False
            if self.stop_evt.wait(backoff):
                break
            backoff = min(backoff * 2, self.max_backoff)

    def poll(self) -> list[ChatMessage]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def healthy(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.stop_evt.set()
