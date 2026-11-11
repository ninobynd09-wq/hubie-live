#!/usr/bin/env python3
"""Long unattended run check. Samples the running stack once a minute and hurts it on a schedule:
kills or freezes one part at a time, by the exact PID from its pid file. Local only, mock brain.
Usage: HUBIE_RUN_DIR=... soak.py <hours> <out.jsonl> <rec_dir> <data_dir> <guardian_unit> [hurt_every_seconds]"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

hours, out, rec, data, unit = float(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
every = float(sys.argv[6]) if len(sys.argv) > 6 else 1500
run = Path(os.environ["HUBIE_RUN_DIR"])
HURTS = [("mixer", "KILL"), ("brain", "KILL"), ("encoder", "KILL"), ("stage", "KILL"), ("encoder", "STOP"), ("mixer", "STOP"),
         ("brain", "STOP"), ("supervisor", "KILL"), ("guardian", "KILL")]


def read(p):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return {}


def pid_of(part):
    if part == "supervisor":
        try:
            return int((run / "supervisor.pid").read_text())
        except (OSError, ValueError):
            return None
    if part == "guardian":
        r = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit], capture_output=True, text=True)
        return int(r.stdout.strip() or 0) or None
    return read(run / "pids" / f"{part}.json").get("pid")


def size(p):
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


cpu = {}


def tree(pid):
    try:
        r = psutil.Process(pid)
        return [r] + r.children(recursive=True)
    except psutil.Error:
        return []


def sample(hurt=None):
    row = {"ts": time.time(), "hurt": hurt, "parts": {}}
    for name in ("stage", "mixer", "encoder", "brain", "supervisor", "guardian"):
        pid = pid_of(name)
        rss = 0
        cpu_s = 0.0
        for p in tree(pid) if pid else []:
            try:
                rss += p.memory_info().rss
                t = p.cpu_times()
                cpu_s += t.user + t.system
            except psutil.Error:
                pass
        row["parts"][name] = {"pid": pid, "rss_mb": round(rss / 1e6, 1), "cpu_s": round(cpu_s, 1)}
    sv, br, mx = read(run / "status/supervisor.json"), read(run / "status/brain.json"), read(run / "status/mixer.json")
    row.update(frame=(sv.get("encoder") or {}).get("frame"), speed=(sv.get("encoder") or {}).get("speed"), drop=(sv.get("encoder") or {}).get("drop_frames"),
               rounds=br.get("rounds"), replies=br.get("replies"), llm_calls=br.get("llm_calls"), clips=mx.get("clips"), brain_ts=br.get("ts"),
               sv_ts=sv.get("ts"), shm_mb=round(size(run) / 1e6, 1), rec_mb=round(size(rec) / 1e6, 1), data_mb=round(size(data) / 1e6, 2),
               disk_free_gb=round(shutil.disk_usage(rec).free / 1e9, 1), box_avail_mb=round(psutil.virtual_memory().available / 1e6))
    return row


end = time.time() + hours * 3600
next_hurt = time.time() + every
i = 0
with open(out, "a") as f:
    while time.time() < end:
        hurt = None
        if time.time() >= next_hurt:
            part, how = HURTS[i % len(HURTS)]
            i += 1
            next_hurt = time.time() + every
            pid = pid_of(part)
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL if how == "KILL" else signal.SIGSTOP)
                    hurt = f"{how} {part} {pid}"
                except OSError as e:
                    hurt = f"failed {part}: {e}"
        f.write(json.dumps(sample(hurt)) + "\n")
        f.flush()
        time.sleep(60)
