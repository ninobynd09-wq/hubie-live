#!/usr/bin/env python3
"""CPU and memory per part, averaged over a window. PSS counts shared Chrome pages once.
Usage: HUBIE_RUN_DIR=... measure.py [seconds]"""
import glob
import json
import os
import sys
import time

import psutil

run = os.environ.get("HUBIE_RUN_DIR", "/dev/shm/hubie_stream")
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30
parts = {}
for f in glob.glob(f"{run}/pids/*.json"):
    d = json.load(open(f))
    parts[d["part"]] = d["pid"]
trees = {}
for n, pid in parts.items():
    r = psutil.Process(pid)
    trees[n] = [r] + r.children(recursive=True)
    for p in trees[n]:
        p.cpu_percent()
time.sleep(secs)
rows, tcpu, tpss, trss = [], 0, 0, 0
for n, ps in sorted(trees.items()):
    cpu = rss = pss = 0
    for p in ps:
        try:
            cpu += p.cpu_percent()
            m = p.memory_full_info()
            rss += m.rss
            pss += m.pss
        except psutil.Error:
            pass
    rows.append((n, cpu, pss / 1e6, rss / 1e6, len(ps)))
    tcpu += cpu; tpss += pss / 1e6; trss += rss / 1e6
print(f"{'part':9s}{'cpu %':>8s}{'pss MB':>9s}{'rss MB':>9s}{'procs':>7s}")
for r in rows:
    print(f"{r[0]:9s}{r[1]:8.1f}{r[2]:9.0f}{r[3]:9.0f}{r[4]:7d}")
print(f"{'total':9s}{tcpu:8.1f}{tpss:9.0f}{trss:9.0f}   ({secs:.0f}s window, 100% = one core, {psutil.cpu_count()} cores)")
