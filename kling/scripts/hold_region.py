# usage: hold_region.py in.mp4 out.mp4 x0 y0 x1 y1 [feather]
#   holds an elliptical region (inside the box) at frame 0 on every frame, feathered, with this frame's overall colour
#   offset in the ring added so the light still breathes. For a paw that must not move at all (idle_v3: the paw on the
#   mouse slid ~10 px mid-take). h264 crf 18, no audio.
import sys, subprocess, json, numpy as np, cv2
ACC = ["-sws_flags", "accurate_rnd+full_chroma_int"]
i, o = sys.argv[1], sys.argv[2]
x0, y0, x1, y1 = map(int, sys.argv[3:7]); fe = float(sys.argv[7]) if len(sys.argv) > 7 else 14
p = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height,r_frame_rate", "-of", "json", i], capture_output=True).stdout)["streams"][0]
w, h, fps = p["width"], p["height"], p["r_frame_rate"]
raw = subprocess.run(["ffmpeg", "-v", "error", "-i", i, *ACC, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
f = np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3).astype(np.float32)
m = np.zeros((h, w), np.float32)
cv2.ellipse(m, ((x0 + x1) // 2, (y0 + y1) // 2), ((x1 - x0) // 2, (y1 - y0) // 2), 0, 0, 360, 1, -1)
m = cv2.GaussianBlur(m, (0, 0), fe)
ring = (m > 0.05) & (m < 0.5)
out = np.empty_like(f)
for k in range(len(f)):
    held = f[0] + (f[k][ring].mean(0) - f[0][ring].mean(0))
    out[k] = f[k] * (1 - m[..., None]) + held * m[..., None]
enc = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", fps,
                        "-i", "-", *ACC, "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", o], stdin=subprocess.PIPE)
enc.stdin.write(np.clip(out, 0, 255).astype(np.uint8).tobytes()); enc.stdin.close(); enc.wait()
print("wrote", o, len(f), "frames")
