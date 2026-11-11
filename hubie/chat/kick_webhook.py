"""Kick chat through Kick's documented webhooks (event chat.message.sent, version 1).

Kick posts each message to a public HTTPS address, which forwards to this loopback port. Every
request is checked against Kick's RSA signature, so nobody else can put words in the chat.
Needs a Kick app; tools/kick_subscribe.py creates the subscription. Read-only: nothing here sends.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .base import ChatMessage, ChatSource

log = logging.getLogger("hubie.chat.webhook")
PUBLIC_KEY_URL = "https://api.kick.com/public/v1/public-key"
MAX_BODY = 64 * 1024


KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "data", "kick_public_key.pem")


def fetch_public_key(url: str = PUBLIC_KEY_URL) -> bytes:
    # Kick's key endpoint refuses Python's default client, so the key is saved to KEY_FILE
    # (refresh: curl -s https://api.kick.com/public/v1/public-key). Live fetch is only a fallback.
    try:
        with open(KEY_FILE, "rb") as f:
            pem = f.read().strip()
        if pem.startswith(b"-----BEGIN PUBLIC KEY-----"):
            return pem
    except OSError:
        pass
    with urllib.request.urlopen(url, timeout=10) as r:
        d = json.loads(r.read())
    return ((d.get("data") or {}).get("public_key") or "").encode()


def verify(pem: bytes, message_id: str, timestamp: str, body: bytes, signature_b64: str) -> bool:
    try:
        key = serialization.load_pem_public_key(pem)
        key.verify(base64.b64decode(signature_b64), f"{message_id}.{timestamp}.".encode() + body,
                   padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def parse_event(body: bytes) -> ChatMessage | None:
    try:
        d = json.loads(body)
        user = (d.get("sender") or {}).get("username")
        text = d.get("content")
        if not user or not isinstance(text, str):
            return None
        return ChatMessage(str(user), text, time.time(), str(d.get("message_id", "")))
    except (ValueError, AttributeError, TypeError):
        return None


class KickWebhookSource(ChatSource):
    def __init__(self, port: int, key_fetcher=fetch_public_key, key_ttl: float = 3600.0):
        self.port, self.fetch, self.key_ttl = port, key_fetcher, key_ttl
        self.key: bytes = b""
        self.key_at = 0.0
        self.q: queue.Queue[ChatMessage] = queue.Queue(maxsize=2000)
        self.seen: deque[str] = deque(maxlen=2000)
        self.lock = threading.Lock()
        self.last_event = 0.0
        self.rejected = 0
        self.server: ThreadingHTTPServer | None = None

    def _pubkey(self, refresh: bool = False) -> bytes:
        if refresh or not self.key or time.time() - self.key_at > self.key_ttl:
            try:
                self.key, self.key_at = self.fetch(), time.time()
            except Exception as e:
                log.warning("couldn't fetch Kick's public key: %r", e)
        return self.key

    def accept(self, headers, body: bytes) -> int:
        """Returns the HTTP status. Kept apart from the socket so it can be tested directly."""
        mid = headers.get("Kick-Event-Message-Id", "")
        ts = headers.get("Kick-Event-Message-Timestamp", "")
        sig = headers.get("Kick-Event-Signature", "")
        ok = verify(self._pubkey(), mid, ts, body, sig)
        if not ok:   # the key can be rotated at any time: try once more with a fresh one
            ok = verify(self._pubkey(refresh=True), mid, ts, body, sig)
        if not ok:
            self.rejected += 1
            return 401
        with self.lock:
            if mid in self.seen:
                return 200
            self.seen.append(mid)
        self.last_event = time.time()
        if headers.get("Kick-Event-Type") != "chat.message.sent":
            return 200
        msg = parse_event(body)
        if msg:
            try:
                self.q.put_nowait(msg)
            except queue.Full:
                pass
        return 200

    def start(self) -> None:
        source = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        self.send_response(413)
                    else:
                        self.send_response(source.accept(self.headers, self.rfile.read(n)))
                except Exception as e:
                    log.warning("webhook request failed: %r", e)
                    self.send_response(400)
                self.end_headers()

            def log_message(self, *_):
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="kick-webhook").start()

    def poll(self) -> list[ChatMessage]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def healthy(self) -> bool:
        return self.server is not None

    def close(self) -> None:
        if self.server:
            self.server.shutdown()
