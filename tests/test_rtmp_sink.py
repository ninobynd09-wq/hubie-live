"""Local RTMP sink: output reaches it, and the encoder heals when the sink restarts."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from stack import ROOT, Stack

pytestmark = pytest.mark.chaos
URL = "rtmp://127.0.0.1:19351/live/test"


def start_sink(out: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(ROOT / "tools/local_rtmp_sink.py"), URL, str(out)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def probe(path: Path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout


def test_rtmp_sink_and_recovery(tmp_path):
    out = tmp_path / "sink"
    out.mkdir()
    sink = start_sink(out)
    time.sleep(2)
    s = Stack(tmp_path / "stack", ":89", 18793, output=URL).start()
    try:
        s.wait_flowing(120)
        time.sleep(10)
        files = sorted(out.glob("*.flv"))
        assert files and files[-1].stat().st_size > 100_000
        info = probe(files[-1])
        assert "h264" in info and "1280,720" in info and "aac" in info
        enc = s.pid("encoder")
        # Sink's ffmpeg dies: the encoder must notice and reconnect once the loop relaunches it.
        child = psutil.Process(sink.pid).children()[0]
        before = len(files)
        os.kill(child.pid, signal.SIGKILL)
        end = time.time() + 90
        while time.time() < end and s.pid("encoder") == enc:
            time.sleep(1)
        assert s.pid("encoder") != enc
        s.wait_flowing(60)
        time.sleep(10)
        files = sorted(out.glob("*.flv"))
        assert len(files) > before and files[-1].stat().st_size > 50_000
    finally:
        s.stop()
        for p in psutil.Process(sink.pid).children(recursive=True):
            p.kill()
        sink.kill()
        sink.wait()
