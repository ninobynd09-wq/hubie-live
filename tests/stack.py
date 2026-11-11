"""Starts the real stack for tests, with its own display, port and scratch dirs."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent


class Stack:
    def __init__(self, base: Path, display: str, port: int, output: str | None = None, **env):
        self.base = Path(base)
        self.run_dir = Path(f"/dev/shm/hubie_test_{os.getpid()}_{port}")
        self.data = self.base / "data"
        self.rec = self.base / "rec"
        self.env = {**os.environ, "HUBIE_RUN_DIR": str(self.run_dir), "HUBIE_DATA_DIR": str(self.data),
                    "HUBIE_OUTPUT": output or f"file:{self.rec}", "HUBIE_DISPLAY": display,
                    "HUBIE_STAGE_PORT": str(port), "HUBIE_PROVIDER": "mock", "HUBIE_TTS": "flite", "HUBIE_CHAT": "fake",
                    "HUBIE_FAKE_CHAT_PER_MIN": "12", "HUBIE_TEST_HOOKS": "1", "HUBIE_WD_START_GRACE": "15",
                    "HUBIE_WD_BRAIN_STALE": "15", "HUBIE_GUARDIAN_STALE": "10", "HUBIE_WD_MAX_BACKOFF": "5",
                    "PYTHONUNBUFFERED": "1", **env}
        self.proc: subprocess.Popen | None = None

    def start(self) -> "Stack":
        self.base.mkdir(parents=True, exist_ok=True)
        lf = open(self.base / "guardian.log", "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "hubie", "run"], cwd=ROOT, env=self.env,
                                     stdout=lf, stderr=lf, start_new_session=True)
        return self

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def status(self, name: str) -> dict:
        try:
            return json.loads((self.run_dir / "status" / f"{name}.json").read_text())
        except (OSError, ValueError):
            return {}

    def pid(self, part: str) -> int | None:
        try:
            return json.loads((self.run_dir / "pids" / f"{part}.json").read_text())["pid"]
        except (OSError, ValueError, KeyError):
            return None

    def supervisor_pid(self) -> int | None:
        try:
            return int((self.run_dir / "supervisor.pid").read_text())
        except (OSError, ValueError):
            return None

    def renderer_pid(self) -> int | None:
        root = self.pid("browser")
        try:
            for p in psutil.Process(root).children(recursive=True):
                if "--type=renderer" in " ".join(p.cmdline()):
                    return p.pid
        except psutil.Error:
            pass
        return None

    def incidents(self) -> list[dict]:
        p = self.data / "watchdog.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []

    def probe_bytes(self) -> bytes:
        try:
            return (self.run_dir / "probe.pgm").read_bytes()
        except OSError:
            return b""

    def snapshot(self) -> dict:
        sv = self.status("supervisor")
        return {"frame": int((sv.get("encoder") or {}).get("frame", -1)), "clips": self.status("mixer").get("clips", -1),
                "written": self.status("mixer").get("written", -1), "rounds": self.status("brain").get("rounds", -1),
                "probe": self.probe_bytes(), "page": self.status("stage").get("page_ts", 0),
                "brain_ts": self.status("brain").get("ts", 0), "sv_ts": sv.get("ts", 0)}

    def all_flowing(self, seconds: float = 12.0) -> tuple[bool, str]:
        """True when video, picture, audio, speech, page and brain all advance over a window."""
        a = self.snapshot()
        time.sleep(seconds)
        b = self.snapshot()
        now = time.time()
        problems = []
        if b["frame"] <= a["frame"]:
            problems.append("no new video frames")
        if b["probe"] == a["probe"]:
            problems.append("picture did not change")
        if b["written"] <= a["written"]:
            problems.append("no audio written")
        if now - b["page"] > 8:
            problems.append("page heartbeat stale")
        if now - b["brain_ts"] > 8:
            problems.append("brain heartbeat stale")
        if now - b["sv_ts"] > 5:
            problems.append("watchdog heartbeat stale")
        return (not problems, ", ".join(problems))

    def wait_flowing(self, timeout: float = 90.0) -> None:
        end = time.time() + timeout
        why = ""
        while time.time() < end:
            ok, why = self.all_flowing(6)
            if ok:
                return
        raise AssertionError(f"stack not flowing after {timeout}s: {why}")

    def wait_speech(self, timeout: float = 60.0) -> None:
        start = self.status("mixer").get("clips", 0)
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1)
            if self.status("mixer").get("clips", 0) > start or self.status("mixer").get("clips", 0) >= 1:
                return
        raise AssertionError("no speech played")
