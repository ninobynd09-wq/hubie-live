#!/usr/bin/env python3
"""Measures how far audio is from picture in a recording: compares when speech starts
(silencedetect) with when the caption changes in the same recording.
Usage: av_sync_check.py recording.ts   Positive offset = audio is later than picture."""
import re
import statistics
import subprocess
import sys

path = sys.argv[1]
err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn", "-af", "silencedetect=n=-40dB:d=0.25",
                      "-f", "null", "-"], capture_output=True, text=True).stderr
speech = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
W, H, FPS = 97, 18, 15
raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path, "-an", "-vf",
                      f"crop=970:178:32:492,fps={FPS},scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                     capture_output=True).stdout
n = len(raw) // (W * H)
frames = [raw[i * W * H:(i + 1) * W * H] for i in range(n)]
# A new line replaces or fills the caption, so an onset is a big frame-to-frame change.
onsets, last = [], -9.0
for i in range(1, n):
    change = sum(abs(a - b) for a, b in zip(frames[i], frames[i - 1])) / (W * H)
    if change > 2.0 and i / FPS - last > 1.0:
        onsets.append(i / FPS)
        last = i / FPS
offsets = []
for t in speech:
    near = [c for c in onsets if abs(c - t) < 1.5]
    if near:
        offsets.append(t - min(near, key=lambda c: abs(c - t)))
if not offsets:
    raise SystemExit(f"no matched events (speech {len(speech)}, captions {len(onsets)})")
half = len(offsets) // 2
print(f"events {len(offsets)}  median offset {statistics.median(offsets):+.3f}s  "
      f"first half {statistics.median(offsets[:half or 1]):+.3f}s  second half {statistics.median(offsets[half:]):+.3f}s")
