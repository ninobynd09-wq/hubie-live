"""The automatic end: credit math, planned end time, quota refusals, the sign-off flow, and nothing restarting after."""
import subprocess
import sys
import time
from datetime import datetime, timezone

import httpx
import pytest

from hubie import credits
from hubie.config import Config
from hubie.credits import EndWatch, mark_show_over, parse_end_at, show_over
from hubie.guardian import guard
from hubie.safety.output_filter import OutputFilter
from hubie.supervisor import Part, Supervisor
from hubie.tts.eleven import ElevenLabsTTS, is_quota_error
from hubie.brain.persona import Persona

from test_orchestrator import PERSONA, build


class Voice:
    name = "elevenlabs"

    def __init__(self):
        self.chars_sent = 0
        self.quota_fails = 0


# ---- threshold math ----

def test_remaining_counts_local_chars_between_polls():
    v = Voice()
    w = EndWatch(threshold=2500, fetch=lambda: (110_000, 100_000), voice=v)
    assert w.remaining() is None and w.should_end() is None    # no poll yet: no guess
    assert w.poll()
    assert w.remaining() == 10_000
    v.chars_sent += 7_400
    assert w.remaining() == 2_600 and w.should_end() is None
    v.chars_sent += 100
    assert w.remaining() == 2_500 and w.should_end() == "credits"


def test_poll_resets_the_local_count_and_failures_keep_the_last_reading():
    v = Voice()
    reading = [(110_000, 50_000)]
    w = EndWatch(threshold=2500, fetch=lambda: reading[0], voice=v)
    w.poll()
    v.chars_sent = 1_000
    reading[0] = (110_000, 51_000)     # the server now includes those 1000
    w.poll()
    assert w.remaining() == 59_000
    reading[0] = None                  # a failed poll (bad JSON, timeout) keeps the last good number
    w.fetch = lambda: (_ for _ in ()).throw(httpx.ConnectError("down"))
    assert not w.poll()
    assert w.remaining() == 59_000 and w.poll_errors == 1


def test_from_env_threshold_default_and_no_polling_without_paid_voice():
    w = credits.from_env({}, object())
    assert w.threshold == 2500 and w.fetch is None and w.voice is None
    w = credits.from_env({"HUBIE_SIGNOFF_CHARS": "4000"}, object())
    assert w.threshold == 4000


def test_from_env_finds_the_eleven_engine_in_the_chain():
    class Chain:
        engines = [Voice(), object()]
    w = credits.from_env({"ELEVENLABS_API_KEY": "k"}, Chain())
    assert w.voice is Chain.engines[0] and w.fetch is not None


# ---- planned end ----

def test_parse_end_at_unix_and_iso():
    assert parse_end_at("") is None and parse_end_at(None) is None
    assert parse_end_at("1762628400") == 1762628400.0
    utc = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc).timestamp()
    assert parse_end_at("2026-11-08T18:00:00Z") == utc
    assert parse_end_at("2026-11-08 18:00") == utc            # no zone = UTC
    assert parse_end_at("2026-11-08T20:00:00+02:00") == utc
    assert parse_end_at("tomorrow-ish") is None


def test_end_at_reached():
    clock = [1000.0]
    w = EndWatch(end_at=1060.0, now=lambda: clock[0])
    assert w.should_end() is None
    clock[0] = 1060.0
    assert w.should_end() == "end_at"


# ---- quota refusals ----

def _eleven(status, body):
    t = ElevenLabsTTS("k", base="http://stub")
    resp = httpx.Response(status, text=body, request=httpx.Request("POST", "http://stub"))
    return t, resp


@pytest.mark.parametrize("status,body,quota", [
    (401, '{"detail":{"status":"quota_exceeded"}}', True), (402, "", True), (401, "invalid key", True),
    (429, "too many concurrent", False), (500, "", False), (400, '{"detail":{"status":"quota_exceeded"}}', True)])
def test_quota_error_kinds(status, body, quota):
    assert is_quota_error(status, body) is quota


def test_eleven_counts_quota_fails_and_chars(monkeypatch, tmp_path):
    t, bad = _eleven(401, '{"detail":{"status":"quota_exceeded"}}')
    monkeypatch.setattr(httpx, "post", lambda *a, **k: bad)
    for _ in range(2):
        with pytest.raises(Exception):
            t.synthesize("hello there", tmp_path / "a.wav")
    assert t.quota_fails == 2 and t.chars_sent == 0
    w = EndWatch(voice=t)
    assert w.should_end() == "voice_quota"
    import base64
    good = httpx.Response(200, json={"audio_base64": base64.b64encode(b"\0\0" * 2400).decode()},
                          request=httpx.Request("POST", "http://stub"))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: good)
    t.synthesize("hello there", tmp_path / "b.wav")
    assert t.quota_fails == 0 and t.chars_sent == len("hello there")
    assert w.should_end() is None


def test_one_quota_refusal_is_not_enough():
    v = Voice()
    v.quota_fails = 1
    assert EndWatch(voice=v).should_end() is None


# ---- the sign-off flow ----

class Ender:
    def __init__(self, after_steps=0, reason="credits"):
        self.calls, self.after, self.reason = 0, after_steps, reason

    def should_end(self):
        self.calls += 1
        return self.reason if self.calls > self.after else None

    def remaining(self):
        return 2400

    def snapshot(self):
        return {"credits_left": 2400}


def _orch(ender):
    flags = []
    orch, sp, clock = build(ender=ender, on_show_over=lambda **info: flags.append(info), signoff_hold=1.0)
    orch.sleep = lambda s: clock.__setitem__(0, clock[0] + max(s, 2.0))
    return orch, sp, flags


def test_signoff_is_the_last_line_and_marks_the_show_over():
    orch, sp, flags = _orch(Ender(after_steps=40))
    orch.run()
    assert len(flags) == 1 and flags[0]["reason"] == "credits"
    kind, text = sp.lines[-1]
    assert kind == "system" and text in Persona.load(PERSONA).canned["signoff"]
    assert orch.counts["rounds"] > 0
    assert sum(1 for k, t in sp.lines if t in Persona.load(PERSONA).canned["signoff"]) == 1


def test_signoff_waits_for_queued_speech_first():
    orch, sp, flags = _orch(Ender())
    pending = [3]
    sp.pending = lambda: max(0, pending[0])
    real_sleep_calls = []

    def sleep(s):
        real_sleep_calls.append(s)
        pending[0] -= 1
    orch.sleep = sleep
    lines_before = len(sp.lines)
    orch.sign_off("credits")
    assert pending[0] <= 0 and len(sp.lines) == lines_before + 1 and flags


def test_voice_quota_signs_off_in_writing():
    orch, sp, flags = _orch(Ender(reason="voice_quota"))
    orch.run()
    assert sp.lines[-1][1] in Persona.load(PERSONA).canned["signoff_silent"]
    assert flags[0]["reason"] == "voice_quota"


def test_no_voice_at_all_still_shows_the_subtitle():
    orch, sp, flags = _orch(Ender())
    events = []
    orch.notify = events.append
    orch.speaker.say = lambda *a, **k: False
    orch.sign_off("credits")
    assert any(e.get("type") == "speak" and e["text"] for e in events) and flags


def test_every_signoff_line_passes_the_filter():
    canned = Persona.load(PERSONA).canned
    f = OutputFilter()
    for line in canned["signoff"] + canned["signoff_silent"]:
        assert f.approve(line) is not None, line
    assert len(canned["signoff"]) >= 3


# ---- nothing restarts after the end ----

def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBIE_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("HUBIE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUBIE_SCENE", "ffmpeg")
    monkeypatch.setenv("HUBIE_SCENE_BG", "art")
    cfg = Config()
    cfg.ensure_dirs()
    return cfg


def test_supervisor_restarts_nothing_after_the_flag(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    sup = Supervisor(cfg)
    sup.parts = {"brain": Part("brain", lambda: [sys.executable, "-c", "pass"])}
    sup.order = ["brain"]
    spawned = []
    sup.spawn = lambda part: spawned.append(part.name)
    mark_show_over(cfg.show_over_flag, reason="credits")
    sup.tick()
    sup.restart(sup.parts["brain"], "exited rc=0")
    assert spawned == [] and sup.stop and sup.over["reason"] == "credits"


def test_supervisor_does_not_start_while_the_show_is_over(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBIE_OUTPUT", f"file:{tmp_path / 'rec'}")
    cfg = Config()
    mark_show_over(cfg.show_over_flag, reason="end_at")
    sup = Supervisor(cfg)
    sup.spawn = lambda part: pytest.fail("spawned after the show ended")
    sup.run()


def test_guardian_does_not_start_while_the_show_is_over(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    mark_show_over(cfg.show_over_flag, reason="credits")
    marker = tmp_path / "ran"
    assert guard(cfg, cmd=[sys.executable, "-c", f"open({str(marker)!r}, 'w')"]) == 0
    time.sleep(0.2)
    assert not marker.exists()


def test_guardian_does_not_relaunch_after_the_supervisor_ends_the_show(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    count = tmp_path / "count"
    # a stand-in supervisor: counts its launches, writes the flag, exits 0
    code = (f"import json,pathlib; c=pathlib.Path({str(count)!r}); c.write_text(str(int(c.read_text() or 0)+1) "
            f"if c.exists() else '1'); pathlib.Path({str(cfg.show_over_flag)!r}).write_text(json.dumps({{'reason':'credits'}}))")
    t0 = time.time()
    assert guard(cfg, cmd=[sys.executable, "-c", code]) == 0
    assert count.read_text() == "1" and time.time() - t0 < 10
    assert show_over(cfg.show_over_flag)["reason"] == "credits"


def test_guardian_still_relaunches_a_crashed_supervisor(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    count = tmp_path / "count"
    # crashes once, then ends the show
    code = (f"import json,pathlib,sys; c=pathlib.Path({str(count)!r}); n=int(c.read_text()) + 1 if c.exists() else 1; "
            f"c.write_text(str(n)); "
            f"sys.exit(1) if n == 1 else pathlib.Path({str(cfg.show_over_flag)!r}).write_text(json.dumps({{'reason':'x'}}))")
    assert guard(cfg, cmd=[sys.executable, "-c", code]) == 0
    assert count.read_text() == "2"
