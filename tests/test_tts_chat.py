import json
import socket
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hubie.audio.mixer import envelope
from hubie.audio.queue import Speaker
from hubie.chat.fake import FakeChatSource
from hubie.chat.kick import KickChatSource, parse_frame
from hubie.safety.output_filter import OutputFilter
from hubie.tts.base import ChainTTS, TTSEngine, TTSError, normalize_wav
from hubie.tts.flite import FliteTTS
from hubie.tts.http import HttpTTS
from hubie.tts.mock import MockTTS


def wav_info(p):
    with wave.open(str(p), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()


def test_mock_tts_format_and_duration(tmp_path):
    out = tmp_path / "a.wav"
    d = MockTTS().synthesize("one two three four five", out)
    r, c, s, n = wav_info(out)
    assert (r, c, s) == (48000, 1, 2) and abs(n / r - d) < 0.01 and d > 1


def test_flite_local_voice_really_speaks(tmp_path):
    out = tmp_path / "a.wav"
    d = FliteTTS().synthesize("Good evening. It's demo chips, and my hat is fine.", out)
    r, c, s, n = wav_info(out)
    assert (r, c, s) == (48000, 1, 2) and 1.5 < d < 15


def test_flite_handles_awkward_text(tmp_path):
    assert FliteTTS().synthesize("It's 3:00, chat's \"awake\"; 100% = fine, [ok]", tmp_path / "a.wav") > 0.3


class Bad(TTSEngine):
    name = "bad"

    def synthesize(self, text, out_wav):
        raise TTSError("nope")


def test_chain_falls_through(tmp_path):
    assert ChainTTS([Bad(), MockTTS()]).synthesize("hello there", tmp_path / "a.wav") > 0


def test_chain_all_fail(tmp_path):
    with pytest.raises(TTSError):
        ChainTTS([Bad()]).synthesize("x", tmp_path / "a.wav")


def test_http_tts_against_local_stub(tmp_path):
    src = tmp_path / "src.wav"
    MockTTS().synthesize("hi there you", src)
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            got["auth"] = self.headers.get("authorization")
            data = src.read_bytes()
            self.send_response(200)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = HttpTTS(f"http://127.0.0.1:{srv.server_port}/speech", key="k", model="m", voice="v").synthesize("hello", tmp_path / "o.wav")
    finally:
        srv.shutdown()
    assert d > 0 and got["body"]["input"] == "hello" and got["auth"] == "Bearer k"


def test_normalize_resamples_stereo_8bit_free(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    with wave.open(str(a), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00\x02\x00" * 16000)
    d = normalize_wav(a, b)
    assert abs(d - 1.0) < 0.01 and wav_info(b)[:3] == (48000, 1, 2)


def test_speaker_accepts_only_safelines(tmp_path):
    sp = Speaker(MockTTS(), tmp_path)
    with pytest.raises(TypeError):
        sp.say("deposit now")
    assert sp.say(OutputFilter().approve("A perfectly fine line.")) is True
    assert sp.pending() == 1
    meta = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert meta["text"] == "A perfectly fine line." and (tmp_path / meta["wav"]).exists()


def test_speaker_survives_tts_failure(tmp_path):
    assert Speaker(Bad(), tmp_path).say(OutputFilter().approve("Fine.")) is False


def test_envelope_range():
    env = envelope(b"\x00\x10" * 48000)
    assert env and all(0 <= v <= 1 for v in env)
    assert envelope(b"") == []


# ---- chat ----

def test_fake_chat_script_and_rate():
    clock = [0.0]
    s = FakeChatSource(script=[("a", "hi")], per_minute=60, seed=1, now=lambda: clock[0])
    s.start()
    assert [m.username for m in s.poll()] == ["a"]
    clock[0] = 60
    assert 40 < len(s.poll()) < 80


FRAME = json.dumps({"event": "App\\Events\\ChatMessageEvent", "channel": "chatrooms.1.v2",
                    "data": json.dumps({"id": "m1", "content": "hello [emote:1:x]", "type": "message",
                                        "sender": {"id": 9, "username": "lena_k", "slug": "lena-k"}})})


def test_kick_frame_parsing():
    m = parse_frame(FRAME)
    assert m.username == "lena_k" and "hello" in m.text and m.id == "m1"
    for junk in ['{"event":"pusher:ping"}', "not json", '{"event":"App\\\\Events\\\\ChatMessageEvent","data":"nope"}',
                 '{"event":"App\\\\Events\\\\ChatMessageEvent","data":{"content":"x"}}', "[]"]:
        assert parse_frame(junk) is None


def test_kick_source_reconnects_against_local_fake_socket():
    import time
    from websockets.sync.server import serve
    conns = []

    def handler(ws):
        sub = json.loads(ws.recv())
        conns.append(sub)
        ws.send('{"event":"pusher:ping"}')
        assert "pong" in ws.recv()
        ws.send(FRAME.replace("m1", f"m{len(conns)}"))
        ws.send(FRAME)  # a replay of the first message after a reconnect is dropped
        if len(conns) == 1:
            return      # drop the first connection on purpose
        time.sleep(5)
    srv = serve(handler, "127.0.0.1", 0)
    port = srv.socket.getsockname()[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    src = KickChatSource(f"ws://127.0.0.1:{port}/app/test", "123", max_backoff=0.5)
    src.start()
    got = []
    end = time.time() + 8
    while time.time() < end and len(got) < 2:
        got += src.poll()
        time.sleep(0.05)
    src.close()
    srv.shutdown()
    assert len(conns) >= 2
    assert len(got) == len({m.id for m in got}) >= 2
    assert conns[0]["data"]["channel"] == "chatrooms.123.v2"


def test_chat_session_full_path_with_mock(tmp_path):
    from hubie.brain.provider import MockProvider
    from hubie.chat_session import ChatSession
    from hubie.config import ROOT
    s = ChatSession(ROOT / "persona/hubie.draft.yaml", MockProvider(1), tmp_path / "m.json")
    t = s.chat("NightOwl42", "yo hubie how's the hat")
    assert "NightOwl" in t.text and t.action == "pass" and t.source == "llm"
    assert s.chat("NightOwl42", "are you an ai?").action == "ai"
    assert s.chat("spammer", "free crypto at www.x.xyz").text is None
    assert s.play_round("mines").text
    assert s.segment("canopy_math").text
    assert "yo hubie how's the hat" in s.mem.earlier_said("NightOwl42") or s.mem.viewer("NightOwl42")["said"]


def test_chat_tool_refuses_without_key_and_names_it():
    import os
    import subprocess
    import sys
    from hubie.config import ROOT
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["HUBIE_DATA_DIR"] = "/tmp/hubie_chat_tool_test"
    r = subprocess.run([sys.executable, str(ROOT / "tools/chat_with_hubie.py"), "--say", "hi"], capture_output=True, text=True, env=env, cwd=ROOT)
    if not (ROOT / ".env").exists():
        assert r.returncode != 0 and "ANTHROPIC_API_KEY" in r.stderr
    r = subprocess.run([sys.executable, str(ROOT / "tools/chat_with_hubie.py"), "--mock", "--fresh", "--name", "lena", "--say", "yo hubie"],
                       capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0 and "lena" in r.stdout


def _signed(body, mid="01J", ts="2026-11-05T10:00:00Z", etype="chat.message.sent"):
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    sig = base64.b64encode(key.sign(f"{mid}.{ts}.".encode() + body, padding.PKCS1v15(), hashes.SHA256())).decode()
    return pem, {"Kick-Event-Message-Id": mid, "Kick-Event-Message-Timestamp": ts, "Kick-Event-Signature": sig, "Kick-Event-Type": etype}


WEBHOOK_BODY = json.dumps({"message_id": "m1", "sender": {"username": "amy"}, "content": "hi hubie [emote:1:KEKW]"}).encode()


def test_kick_webhook_takes_signed_chat_once_and_drops_forgeries():
    from hubie.chat.kick_webhook import KickWebhookSource
    pem, headers = _signed(WEBHOOK_BODY)
    src = KickWebhookSource(0, key_fetcher=lambda: pem)
    assert src.accept(headers, WEBHOOK_BODY) == 200
    assert src.accept(headers, WEBHOOK_BODY) == 200    # delivered twice, counted once
    got = src.poll()
    assert [(m.username, m.text) for m in got] == [("amy", "hi hubie [emote:1:KEKW]")]
    assert src.accept({**headers, "Kick-Event-Message-Id": "other"}, WEBHOOK_BODY) == 401
    assert src.accept(headers, WEBHOOK_BODY + b" ") == 401
    assert src.poll() == [] and src.rejected == 2


def test_kick_webhook_refetches_a_rotated_key():
    from hubie.chat.kick_webhook import KickWebhookSource
    pem, headers = _signed(WEBHOOK_BODY)
    other, _ = _signed(b"x")
    keys = [other, pem]
    src = KickWebhookSource(0, key_fetcher=lambda: keys.pop(0))
    assert src.accept(headers, WEBHOOK_BODY) == 200
    assert len(src.poll()) == 1


def test_kick_webhook_over_real_http():
    import urllib.error
    import urllib.request
    from hubie.chat.kick_webhook import KickWebhookSource
    pem, headers = _signed(WEBHOOK_BODY)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    src = KickWebhookSource(port, key_fetcher=lambda: pem)
    src.start()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", data=WEBHOOK_BODY, headers=headers), timeout=5) as r:
            assert r.status == 200
        assert src.poll()[0].username == "amy"
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/", data=b"{}", headers=headers), timeout=5)
    finally:
        src.close()


def test_missing_kick_settings_mean_no_chat_not_a_crash(monkeypatch):
    from hubie.chat.base import NoChat
    from hubie.config import Config
    from hubie.orchestrator import build_chat
    monkeypatch.setenv("HUBIE_CHAT", "kick")
    monkeypatch.delenv("KICK_PUSHER_URL", raising=False)
    assert isinstance(build_chat(Config()), NoChat)


def test_routed_provider_sends_each_kind_to_its_model(monkeypatch):
    from hubie.brain.provider import LLMRequest, RoutedProvider, MockProvider
    class Tag(MockProvider):
        def __init__(self, tag):
            super().__init__(1); self.tag = tag
        def complete(self, req):
            return self.tag
    p = RoutedProvider(Tag("opus"), {"round": Tag("haiku")})
    assert p.complete(LLMRequest("s", "p", kind="round")) == "haiku"
    assert p.complete(LLMRequest("s", "p", kind="chat")) == "opus"
