"""Frame-by-frame check of a lip-sync close-up (tools/lipsync/closeup.py output): mouth openness (share of dark
pixels in the tracked mouth box) against the audio's loudness, every frame at 30 fps.

  python3 tools/lipsync/check_sync.py VIDEO.mp4 [--plot out.png] [--lead 0.6]
Flags: loud but closed for more than CLOSURE frames (a real m/b/p closure is allowed that long), and open on
silence later than GRACE frames after the voice stops."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
FPS = 30
BOX = (505, 260, 650, 305)          # mouth box in reference (idle frame 0) coordinates, below the nose
LOUD, QUIET = 0.25, 0.03            # loudness, 1.0 = loud-but-typical speech
CLOSURE, GRACE = 3, 2
CLOSED = 0.055   # dark share of the closed smile is ~0.03 over the loop, lips just apart (slight) ~0.086


def video_frames(path):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "gray", "-r", str(FPS),
                        "-"], capture_output=True, check=True)
    fr = np.frombuffer(p.stdout, np.uint8)
    return fr.reshape(-1, 640, 960)


def audio_rms(path, n):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "24000", "-"],
                       capture_output=True, check=True)
    a = np.frombuffer(p.stdout, np.int16).astype(np.float32)
    step = 24000 // FPS
    r = np.array([np.sqrt((a[i * step:(i + 1) * step] ** 2).mean()) if i * step < len(a) else 0 for i in range(n)])
    voiced = np.sort(r[r > 0.02 * r.max()])
    return r / voiced[int(len(voiced) * 0.9)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--plot")
    ap.add_argument("--json")
    a = ap.parse_args()
    T = json.loads((ROOT / os.environ.get("LIPSYNC_TRACKS", "tools/lipsync/tracks_idle_v1.json")).read_text())[
        os.environ.get("LIPSYNC_IDLE", "shots/kling_idle_v1.mp4")]
    fr = video_frames(a.video)
    n = len(fr)
    rms = audio_rms(a.video, n)
    # closeup geometry (tools/lipsync/closeup.py): crop of the 1164x776 loop, scaled to 960x640
    w, h, z = 1164, 776, 1.18
    cw, ch = int(w / z) // 2 * 2, int(h / z) // 2 * 2
    x0, y0 = (w - cw) // 2, int((h - ch) * 0.3)
    sx, sy = 960 / cw, 640 / ch
    op = np.zeros(n)
    for i in range(n):
        W = np.array(T[int(i / FPS * 24) % len(T)]["w"], np.float32)
        pts = np.float32([[BOX[0], BOX[1]], [BOX[2], BOX[1]], [BOX[0], BOX[3]], [BOX[2], BOX[3]]])
        q = pts @ W[:, :2].T + W[:, 2]
        bx0, by0 = int((q[:, 0].min() - x0) * sx), int((q[:, 1].min() - y0) * sy)
        bx1, by1 = int((q[:, 0].max() - x0) * sx), int((q[:, 1].max() - y0) * sy)
        op[i] = (fr[i, by0:by1, bx0:bx1] < 80).mean()
    silent = rms < QUIET
    closed_thr = CLOSED
    closed = op <= closed_thr
    loud = rms >= LOUD
    bad_closed, bad_open = [], []
    i = 0
    while i < n:   # loud and closed: runs longer than a closure
        if loud[i] and closed[i]:
            j = i
            while j < n and closed[j] and rms[j] >= QUIET:
                j += 1
            if j - i > CLOSURE:
                bad_closed.append((i, j))
            i = j
        else:
            i += 1
    lead = 0
    for i in range(n):   # open on silence, later than GRACE frames after the voice
        if silent[i] and not closed[i] and not any(rms[max(0, i - GRACE):i] >= QUIET):
            if i + 1 < n and rms[i + 1] >= QUIET:
                lead += 1    # one frame before the voice starts: the lips moving ahead of the sound, on purpose
            else:
                bad_open.append(i)
    held = 0
    run = 1
    for i in range(1, n):   # same picture of an open mouth for many frames
        run = run + 1 if (not closed[i] and abs(op[i] - op[i - 1]) < 1e-4) else 1
        held = max(held, run)
    corr = float(np.corrcoef(op[~silent], rms[~silent])[0, 1]) if (~silent).sum() > 2 else 0.0
    res = {"frames": n, "closed_threshold": round(float(closed_thr), 4), "loud_frames": int(loud.sum()),
           "loud_closed_frames": int((loud & closed).sum()),
           "loud_closed_runs_over_closure": [(round(i / FPS, 2), round(j / FPS, 2)) for i, j in bad_closed],
           "silent_frames": int(silent.sum()), "silent_open_frames": int((silent & ~closed).sum()),
           "silent_open_after_grace": [round(i / FPS, 2) for i in bad_open],
           "open_one_frame_before_voice": lead,
           "corr_openness_loudness_voiced": round(corr, 3), "longest_identical_open_run": held}
    print(json.dumps(res, indent=1))
    if a.json:
        Path(a.json).write_text(json.dumps({**res, "openness": op.round(4).tolist(), "rms": rms.round(3).tolist()}))
    if a.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        t = np.arange(n) / FPS
        fig, ax = plt.subplots(figsize=(16, 4.5))
        ax.fill_between(t, 0, rms, color="#9db4c9", label="audio loudness (per frame)", step="mid")
        ax2 = ax.twinx()
        ax2.plot(t, op, color="#c2410c", lw=1.4, label="mouth openness (dark share)", drawstyle="steps-mid")
        ax2.axhline(closed_thr, color="#c2410c", lw=0.6, ls=":")
        for i, j in bad_closed:
            ax.axvspan(i / FPS, j / FPS, color="red", alpha=0.25)
        for i in bad_open:
            ax.axvline(i / FPS, color="red", alpha=0.4)
        ax.set_xlabel("seconds")
        ax.set_ylabel("loudness")
        ax2.set_ylabel("openness")
        ax.set_xticks(np.arange(0, t[-1] + 0.01, 0.5))
        ax.grid(axis="x", alpha=0.3)
        fig.legend(loc="upper right")
        fig.tight_layout()
        fig.savefig(a.plot, dpi=110)
    sys.exit(1 if bad_closed or bad_open else 0)


if __name__ == "__main__":
    main()
