#!/usr/bin/env python3
"""Local RTMP test sink, no server needed: ffmpeg listens on loopback and writes
each incoming stream to an FLV file, then listens again.
Usage: local_rtmp_sink.py rtmp://127.0.0.1:19351/live/test /path/to/outdir"""
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

url, out = sys.argv[1], Path(sys.argv[2])
if urlparse(url).hostname not in ("127.0.0.1", "localhost"):
    raise SystemExit("this sink only listens on loopback")
out.mkdir(parents=True, exist_ok=True)
while True:
    f = out / f"sink_{time.strftime('%H%M%S')}.flv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-listen", "1", "-f", "flv", "-i", url,
                    "-c", "copy", str(f)])
    if f.exists() and f.stat().st_size == 0:
        f.unlink()
    time.sleep(0.5)
