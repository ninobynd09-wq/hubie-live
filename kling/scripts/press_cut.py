# usage: press_cut.py raw.mp4 out.mp4 A B C D [anchor.mp4]
#   keeps raw frames A..B, crossfades 3 frames (B+1..B+3 into C..C+2), keeps C+3..D, starts on raw frame 0 and blends the
#   last 8 frames toward frame 0 (so the clip cuts back into the idle loop's frame 0). h264, no audio.
#   anchor.mp4: frame 0 becomes that clip's frame 0 (the idle loop) and every frame gets its low-frequency colour offset.
# spinpress_a (2026-11-07): the take tapped twice (claw bottoms at 32 and 41); 8 30 40 60 keeps one tap.
import sys, subprocess, json, numpy as np
ACC = ["-sws_flags", "accurate_rnd+full_chroma_int"]   # ffmpeg's default yuv<->rgb darkens dark frames ~1 level per pass
i, o = sys.argv[1], sys.argv[2]
A, B, C, D = map(int, sys.argv[3:7])
p = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height,r_frame_rate", "-of", "json", i], capture_output=True).stdout)["streams"][0]
w, h, fps = p["width"], p["height"], p["r_frame_rate"]
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", i, *ACC, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
f = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3).astype(np.float32)
if len(sys.argv) > 7:
    import cv2
    a0 = np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", sys.argv[7], "-frames:v", "1", *ACC, "-f", "rawvideo",
                                       "-pix_fmt", "rgb24", "-"], capture_output=True).stdout, np.uint8).reshape(h, w, 3).astype(np.float32)
    off = cv2.GaussianBlur(a0 - f[0], (0, 0), 8)
    f = f + off
    f[0] = a0
    # Kling's frames drift in overall brightness: hold each frame's background (room corners, no paw or head) to a0's
    bg = np.zeros((h, w), bool); bg[:int(h * .5), int(w * .86):] = True; bg[:int(h * .5), :int(w * .1)] = True
    ref = a0[bg].mean(0)
    for k in range(1, len(f)):
        f[k] += ref - f[k][bg].mean(0)
seq = [f[0]] + [f[k] for k in range(A, B + 1)]
for k in range(3):
    a = (k + 1) / 4
    seq.append(f[B + 1 + k] * (1 - a) + f[C + k] * a)
seq += [f[k] for k in range(C + 3, D + 1)]
for k in range(1, 4):                       # ease in from frame 0
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
print("wrote", o, len(seq), "frames; splice lands at output frame", 1 + (B - A + 1) + 1)
