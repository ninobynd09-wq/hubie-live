"""Kills or freezes each real part and proves the stack heals itself with no help.

Run: pytest -m chaos tests/test_chaos.py   (about 15 minutes, one Chrome at a time)
Every signal goes to an exact PID that this run's own pidfiles recorded.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from stack import Stack

pytestmark = pytest.mark.chaos
RESULTS: list[dict] = []
PARTS = ["xvfb", "stage", "mixer", "encoder", "browser", "brain"]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    s = Stack(tmp_path_factory.mktemp("chaos"), ":88", 18792).start()
    try:
        s.wait_flowing(120)
        yield s
    finally:
        s.stop()
        out = os.environ.get("HUBIE_CHAOS_RESULTS")
        if out:
            Path(out).write_text(json.dumps(RESULTS, indent=1))


def disturb_and_verify(stack: Stack, part: str, sig: int, pid: int, expect_incident: str | None):
    t0 = time.time()
    os.kill(pid, sig)
    end = t0 + 90
    while time.time() < end:
        time.sleep(0.5)
        inc = [e for e in stack.incidents() if e.get("event") == "recovered" and e.get("part") == expect_incident
               and e.get("detected_at", 0) >= t0 - 1]
        if inc:
            break
    assert inc, f"no recovery logged for {expect_incident} after signalling {part}"
    stack.wait_flowing(60)
    RESULTS.append({"part": part, "signal": signal.Signals(sig).name, "reason": inc[0]["reason"],
                    "detect_s": round(inc[0]["detected_at"] - t0, 1), "healthy_after_s": round(time.time() - t0, 1)})
    return inc[0]


@pytest.mark.parametrize("part", PARTS)
def test_kill_part_recovers(stack, part):
    pid = stack.pid(part)
    disturb_and_verify(stack, part, signal.SIGKILL, pid, part)
    assert stack.pid(part) != pid


@pytest.mark.parametrize("part", PARTS)
def test_frozen_part_recovers(stack, part):
    """SIGSTOP: the process is alive but doing nothing, the harder case."""
    pid = stack.pid(part)
    disturb_and_verify(stack, part, signal.SIGSTOP, pid, part)
    assert stack.pid(part) != pid
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGSTOP])
def test_browser_renderer_only(stack, sig):
    """The tab dies or hangs while the main browser process stays up."""
    pid = stack.renderer_pid()
    assert pid
    disturb_and_verify(stack, "renderer", sig, pid, "browser")


def _post(stack, ev):
    from hubie.util import post_stage
    assert post_stage(f"http://127.0.0.1:{stack.env['HUBIE_STAGE_PORT']}", ev, timeout=2)


def test_blank_page_detected_from_pixels(stack):
    """Page still heartbeats but shows nothing. Only the picture probe can catch this."""
    t0 = time.time()
    _post(stack, {"type": "test_blank"})
    end = t0 + 60
    while time.time() < end:
        inc = [e for e in stack.incidents() if e.get("event") == "recovered" and e.get("part") == "browser"
               and e.get("detected_at", 0) >= t0]
        if inc:
            break
        time.sleep(0.5)
    assert inc and "blank" in inc[0]["reason"]
    stack.wait_flowing(60)
    RESULTS.append({"part": "page-blank", "signal": "test hook", "reason": inc[0]["reason"],
                    "detect_s": round(inc[0]["detected_at"] - t0, 1), "healthy_after_s": round(time.time() - t0, 1)})


def test_frozen_animation_detected(stack):
    """rAF loop stops while the socket stays open."""
    t0 = time.time()
    _post(stack, {"type": "test_freeze"})
    end = t0 + 60
    while time.time() < end:
        inc = [e for e in stack.incidents() if e.get("event") == "recovered" and e.get("part") == "browser"
               and e.get("detected_at", 0) >= t0]
        if inc:
            break
        time.sleep(0.5)
    assert inc, "freeze not detected"
    stack.wait_flowing(60)
    RESULTS.append({"part": "page-freeze", "signal": "test hook", "reason": inc[0]["reason"],
                    "detect_s": round(inc[0]["detected_at"] - t0, 1), "healthy_after_s": round(time.time() - t0, 1)})


def test_overlay_is_burned_in_even_while_browser_is_down(stack):
    """Decodes real recorded frames from the moment the browser is dead, then OCRs the strip."""
    from PIL import Image, ImageOps, ImageStat
    pid = stack.pid("browser")
    os.kill(pid, signal.SIGKILL)
    time.sleep(7)
    segs = sorted(stack.rec.glob("hubie_*.ts"), key=lambda p: p.stat().st_mtime)
    out = stack.base / "ov"
    out.mkdir(exist_ok=True)
    # Decoding from well before the kill lands on a keyframe, so frames aren't half decoded.
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "fatal", "-y", "-sseof", "-14", "-i", str(segs[-1]),
                    "-vf", "fps=4", str(out / "f%03d.png")], check=True)
    frames = sorted(out.glob("f*.png"))
    assert frames
    dark = 0
    for f in frames:
        img = Image.open(f).convert("L")
        body = img.crop((0, 0, 1280, 680))
        if ImageStat.Stat(body).stddev[0] < 3:
            dark += 1
            strip = ImageOps.invert(img.crop((0, 686, 1280, 720))).resize((2560, 68))
            crop = out / "strip.png"
            strip.save(crop)
            text = subprocess.run(["tesseract", str(crop), "-", "--psm", "7"], capture_output=True, text=True).stdout
            text = text.replace("Al-generated", "AI-generated")   # OCR confuses capital I and small l
            assert "18+" in text and "Gamble responsibly" in text and "AI-generated" in text, text
    assert dark >= 1, "never caught a frame with the browser down"
    RESULTS.append({"part": "overlay", "frames_with_browser_down_checked": dark})
    stack.wait_flowing(60)


def test_supervisor_killed_guardian_brings_it_back(stack):
    old = stack.supervisor_pid()
    parts_before = {p: stack.pid(p) for p in PARTS}
    t0 = time.time()
    os.kill(old, signal.SIGKILL)
    end = t0 + 60
    while time.time() < end and stack.supervisor_pid() == old:
        time.sleep(0.5)
    assert stack.supervisor_pid() != old
    time.sleep(5)
    stack.wait_flowing(90)
    for p, pid in parts_before.items():    # no orphans from the old supervisor
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE, p
    RESULTS.append({"part": "watchdog", "signal": "SIGKILL", "reason": "exited", "detect_s": 0,
                    "healthy_after_s": round(time.time() - t0, 1)})


def test_supervisor_frozen_guardian_brings_it_back(stack):
    old = stack.supervisor_pid()
    t0 = time.time()
    os.kill(old, signal.SIGSTOP)
    end = t0 + 60
    while time.time() < end and stack.supervisor_pid() == old:
        time.sleep(0.5)
    assert stack.supervisor_pid() != old
    time.sleep(5)
    stack.wait_flowing(90)
    RESULTS.append({"part": "watchdog", "signal": "SIGSTOP", "reason": "heartbeat", "detect_s": 0,
                    "healthy_after_s": round(time.time() - t0, 1)})


def test_recording_holds_video_and_audio_through_all_of_that(stack):
    segs = sorted(stack.rec.glob("hubie_*.ts"), key=lambda p: p.stat().st_mtime)[:-1]
    assert len(segs) > 5
    total_v = total_a = 0.0
    skews = []
    for seg in segs:
        d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,start_time,duration",
                                       "-of", "json", str(seg)], capture_output=True, text=True).stdout)
        by = {s["codec_type"]: s for s in d["streams"]}
        assert "video" in by and "audio" in by, seg
        if "duration" in by["video"] and "duration" in by["audio"]:
            v, a = float(by["video"]["duration"]), float(by["audio"]["duration"])
            total_v += v
            total_a += a
            skews.append(abs(v - a))
    assert total_v > 200
    RESULTS.append({"part": "recording", "segments": len(segs), "video_s": round(total_v), "audio_s": round(total_a),
                    "max_stream_length_gap_s": round(max(skews), 2)})
    longest = max(segs, key=lambda p: p.stat().st_size)
    vd = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(longest), "-af", "volumedetect", "-f", "null", "-"],
                        capture_output=True, text=True).stderr
    assert "max_volume" in vd and "max_volume: -91" not in vd




def test_no_real_money_or_remote_surface_in_stack_commands(stack):
    pid = stack.pid("browser")
    cmd = " ".join(psutil.Process(pid).cmdline())
    assert "127.0.0.1" in cmd and "housebets" not in cmd
    enc = " ".join(psutil.Process(stack.pid("encoder")).cmdline())
    assert "rtmp" not in enc
