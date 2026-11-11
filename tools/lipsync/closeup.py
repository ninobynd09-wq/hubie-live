"""Offline close-up of the desk cam lip-syncing a saved voice clip, using the same mouth loops, the same frame
planner and the same shape changes the live scene sends the encoder. No TTS call.
  python3 tools/lipsync/closeup.py OUT.mp4 [--seconds 10] [--slow OUT_SLOW.mp4]
--slow also writes a half-speed copy (audio slowed with pitch kept) for frame-by-frame review."""
import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from hubie.audio import lipsync  # noqa: E402

FIX = ROOT / "tests/fixtures/eleven/rick_with_timestamps.json"
FPS = lipsync.FPS


def loops():
    out = []
    for k in range(len(lipsync.NAMES)):
        cap = cv2.VideoCapture(str(ROOT / os.environ.get("LIPSYNC_VISEMES", "assets/cam_visemes") / f"mouth_{k}.mp4"))
        fr = []
        while True:
            ok, f = cap.read()
            if not ok:
                break
            fr.append(f)
        out.append(fr)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("--lead", type=float, default=0.6)
    ap.add_argument("--slow")
    ap.add_argument("--dump", help="write the track and commands here (json)")
    a = ap.parse_args()
    fx = json.loads(FIX.read_text())
    pcm = base64.b64decode(fx["audio_base64"])
    td = Path(tempfile.mkdtemp())
    wav = td / "v.wav"
    lead = b"\0\0" * int(a.lead * 24000)
    tail = b"\0\0" * int(max(0, a.seconds * 24000 - (len(lead) + len(pcm)) / 2))
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(lead + pcm + tail)
    al = fx["alignment"]
    shifted = dict(al, character_start_times_seconds=[t + a.lead for t in al["character_start_times_seconds"]],
                   character_end_times_seconds=[t + a.lead for t in al["character_end_times_seconds"]])
    trk = lipsync.plan("", wav, shifted)
    if a.dump:
        Path(a.dump).write_text(json.dumps({"track": trk}))
    L = loops()
    n = len(L[0])
    h, w = L[0][0].shape[:2]
    z = 1.18
    cw, ch = int(w / z) // 2 * 2, int(h / z) // 2 * 2
    x0, y0 = (w - cw) // 2, int((h - ch) * 0.3)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", "960x640",
                          "-r", str(FPS), "-i", "-", "-i", str(wav), "-c:v", "libx264", "-preset", "slow", "-crf", "19",
                          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
                          a.out], stdin=subprocess.PIPE)
    ci = 0
    for i in range(int(a.seconds * FPS)):
        t = i / FPS
        while ci + 1 < len(trk) and trk[ci + 1][0] <= t + 0.5 / FPS:   # change points are rounded to 0.1 ms
            ci += 1
        f = L[trk[ci][1]][int(t * 24) % n][y0:y0 + ch, x0:x0 + cw]
        p.stdin.write(cv2.resize(f, (960, 640), interpolation=cv2.INTER_AREA).tobytes())
    p.stdin.close()
    p.wait()
    if a.slow:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", a.out, "-filter_complex",
                        "[0:v]setpts=2*PTS[v];[0:a]atempo=0.5[a]", "-map", "[v]", "-map", "[a]", "-r", str(FPS),
                        "-c:v", "libx264", "-preset", "slow", "-crf", "19", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-b:a", "128k", "-movflags", "+faststart", a.slow], check=True)
    print("change points", len(trk))


if __name__ == "__main__":
    main()
