# usage: frames_cut.py raw.mp4 out.mp4 SPEC [anchor.mp4]
#   SPEC = comma list of raw frame ranges, "a-b" or "a-b:step" (step 2 = that stretch at double speed), e.g.
#   "16-41,42-56:2,57-66". Like press_cut.py but with no crossfade (a sped-up stretch instead of a splice, so a blink
#   or a glow fade never ghosts): starts on raw frame 0, eases in over 3 frames, blends the last 8 frames toward
#   frame 0. anchor.mp4: frame 0 becomes that clip's frame 0 (the idle loop) and every frame gets its low-frequency
#   colour offset plus the background brightness hold, as in press_cut.py. h264, no audio.
# click_b (2026-11-07): claw lifts 16-31, comes down 33-35, mouse glow flashes 37-41 (peak 44), paw back by 66.
import sys, subprocess, json, numpy as np
ACC = ["-sws_flags", "accurate_rnd+full_chroma_int"]
i, o, spec = sys.argv[1], sys.argv[2], sys.argv[3]
p = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height,r_frame_rate", "-of", "json", i], capture_output=True).stdout)["streams"][0]
w, h, fps = p["width"], p["height"], p["r_frame_rate"]
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", i, *ACC, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
f = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3).astype(np.float32)
if len(sys.argv) > 4:
    import cv2
    a0 = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", sys.argv[4], "-frames:v", "1", *ACC, "-f", "rawvideo",
                                       "-pix_fmt", "rgb24", "-"], capture_output=True).stdout, np.uint8).reshape(h, w, 3).astype(np.float32)
    off = cv2.GaussianBlur(a0 - f[0], (0, 0), 8)
    f = f + off
    f[0] = a0
    bg = np.zeros((h, w), bool); bg[:int(h * .5), int(w * .86):] = True; bg[:int(h * .5), :int(w * .1)] = True
    ref = a0[bg].mean(0)
    for k in range(1, len(f)):
        f[k] += ref - f[k][bg].mean(0)
idx = [0]
for part in spec.split(","):
    rng, _, st = part.partition(":")
    a, b = map(int, rng.split("-"))
    idx += list(range(a, b + 1, int(st or 1)))
seq = [f[k].copy() for k in idx]
for k in range(1, 4):
    a = k / 4
    seq[k] = f[0] * (1 - a) + seq[k] * a
N = 8
for k in range(N):
    a = (k + 1) / (N + 1); j = len(seq) - N + k
    seq[j] = seq[j] * (1 - a) + f[0] * a
enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", fps,
                        "-i", "-", *ACC, "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", o], stdin=subprocess.PIPE)
enc.stdin.write(np.clip(np.array(seq), 0, 255).astype(np.uint8).tobytes()); enc.stdin.close(); enc.wait()
print("wrote", o, len(seq), "frames; raw->out:", " ".join(f"{r}>{n}" for n, r in enumerate(idx) if r in (33, 35, 37, 40)))
