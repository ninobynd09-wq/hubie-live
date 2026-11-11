"""Offline: paint Hubie's own talking mouths into the idle loop. One full copy of the idle loop per mouth shape.

Every shape except rest starts from the idle frame with its own smile line erased (inpainted with the
surrounding fur, found again on every frame inside a band that follows the tracked face), so a pasted mouth
never sits next to a second one. Rest is the idle loop untouched, i.e. his own closed smile. Each talking mouth
is cut from his Kling talking frames along its own outline (no fixed ellipse), lined up on the smile's corners
(the open shapes) or left where the tracked face puts them (round, f/v, under the philtrum), colour-matched to the frame and feathered in.

  python3 tools/lipsync/build_visemes.py tracks.json out_dir [--still FRAME] [--check]
tracks.json comes from tools/lipsync/track.py (reference = idle frame 0). LIPSYNC_IDLE=clip builds on another base
loop (it must start on the desk still and be a key in tracks.json). --still writes a contact sheet of every
shape on one frame instead of the loops; --check also writes full-size crops of every shape."""
import json
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from track import frames  # noqa: E402
from hubie.audio.lipsync import NAMES  # noqa: E402

IDLE = os.environ.get("LIPSYNC_IDLE", "shots/kling_idle_v1.mp4")   # base loop (key in tracks.json)
V1, V2 = "shots/kling_talk_v1.mp4", "shots/kling_talk_v2.mp4"
# shape -> (clip, frame, how it lines up), picked by eye from stabilised contact sheets.
# The v2 take smiles with the mouth open, so its shapes run corner to corner like the idle smile: they make the
# openness ladder. Round and ee come from v1, which shapes vowels more.
PICKS = {
    "slight": (V2, 41, "corners"),   # lips just parted
    "half": (V2, 66, "corners"),     # parted, tip of the tongue
    "open": (V2, 12, "corners"),
    "wide": (V2, 37, "corners"),
    "round": (V1, 92, "centre"),     # o, oo, w (made a bit wider, see ROUND_GROW)
    "ee": (V1, 54, "corners"),       # spread, teeth
    "fv": (V2, 84, "centre"),        # top teeth on the lower lip
    "round_s": (V1, 92, "centre", 0.62),   # round with the jaw less dropped: a held o moves between the two
}
NOSE = ((570, 241), (34, 18))        # reference-space ellipse kept out of every mouth mask
BOX = (494, 226, 662, 312)           # reference-space search box for the mouth
ROUND_GROW = (1.2, 1.1)              # round is narrower than the smile; grown about its top lip so wide -> round
                                     # doesn't jump in width
DARK = 110                           # mouth line and inside, against cream fur at ~215-230


def ref_space(f, W):
    return cv2.warpAffine(f, W, (f.shape[1], f.shape[0]), flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP)


def to_frame(img, W, shape, interp=cv2.INTER_CUBIC):
    return cv2.warpAffine(img, W, (shape[1], shape[0]), flags=interp, borderMode=cv2.BORDER_REPLICATE)


def nose_mask(shape):
    m = np.zeros(shape[:2], np.uint8)
    cv2.ellipse(m, NOSE[0], NOSE[1], 0, 0, 360, 255, -1)
    return m


def box_mask(shape):
    m = np.zeros(shape[:2], np.uint8)
    x0, y0, x1, y1 = BOX
    m[y0:y1, x0:x1] = 255
    return m


def mouth_lines(img):
    """Dark mouth pixels (line, inside, corners) in the box, nose left out, small specks dropped."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    d = ((g < DARK) & (box_mask(img.shape) > 0) & (nose_mask(img.shape) == 0)).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(d)
    keep = np.zeros_like(d)
    for i in range(1, n):
        x, y, w, h, a = st[i]
        x0, y0, x1, y1 = BOX
        edge = x <= x0 or y <= y0 or x + w >= x1 or y + h >= y1
        if a >= 25 and w >= 12 and not edge:
            keep[lab == i] = 1
    return keep


def corners(line):
    ys, xs = np.nonzero(line)
    i, j = xs.argmin(), xs.argmax()
    return np.float32([[xs[i], ys[i]], [xs[j], ys[j]]])


def blob(line):
    """The mouth as a filled shape: the outline closed up, holes (tongue, teeth) filled."""
    c = cv2.morphologyEx(line * 255, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    cs, _ = cv2.findContours(c, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = np.zeros_like(c)
    cv2.drawContours(out, cs, -1, 255, -1)
    return out


def corner_fit(src, dst, depth=0.35):
    """2x3 warp taking the two src corners exactly onto the two dst corners: rotation, plus a stretch along the
    corner line and only `depth` of that stretch across it, so a narrower smile widens without getting taller."""
    sv, dv = src[1] - src[0], dst[1] - dst[0]
    s = float(np.linalg.norm(dv) / max(1e-6, np.linalg.norm(sv)))
    a = float(np.arctan2(dv[1], dv[0]) - np.arctan2(sv[1], sv[0]))
    u = sv / np.linalg.norm(sv)
    U = np.array([[u[0], -u[1]], [u[1], u[0]]], np.float32)               # columns: along, across
    S = U @ np.diag([s, 1 + (s - 1) * depth]).astype(np.float32) @ U.T
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]], np.float32)
    M = R @ S
    t = dst[0] - M @ src[0]
    return np.hstack([M, t[:, None]]).astype(np.float32), s, float(np.degrees(a))


class Shapes:
    def __init__(self, T):
        self.T = T
        self.cache = {}
        idle0 = frames(IDLE)[0]
        self.idle_line = mouth_lines(idle0)
        self.idle_corners = corners(self.idle_line)
        # band the idle smile can move in, relative to the tracked face
        self.band = cv2.dilate(self.idle_line * 255, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (17, 17)))
        self.band[nose_mask(idle0.shape) > 0] = 0
        self.sprites = {}
        self.info = {}
        for name, (clip, i, how, *jaw) in PICKS.items():
            self.sprites[name] = self._cut(clip, i, how, name, jaw[0] if jaw else 1.0)

    def _frames(self, clip):
        if clip not in self.cache:
            self.cache[clip] = frames(clip)
        return self.cache[clip]

    def _cut(self, clip, i, how, name, jaw=1.0):
        f = ref_space(self._frames(clip)[i], np.array(self.T[clip][i]["w"], np.float32))
        line = mouth_lines(f)
        b = blob(line)
        c = corners(line)
        if how == "corners":
            A, s, deg = corner_fit(c, self.idle_corners)
        else:   # stays where the tracked face put it, under its philtrum; round grows about its top lip
            gx, gy = ROUND_GROW if name.startswith("round") else (1.0, 1.0)
            ys, xs = np.nonzero(b)
            cx, top = (c[0, 0] + c[1, 0]) / 2, float(ys.min())
            A = np.float32([[gx, 0, cx * (1 - gx)], [0, gy, top * (1 - gy)]])
            s, deg = gx, 0.0
        h, w = f.shape[:2]
        sprite = cv2.warpAffine(f, A, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        b = cv2.warpAffine(b, A, (w, h), flags=cv2.INTER_LINEAR)
        if jaw != 1.0:   # lift the lower lip toward the top one: the same mouth, less open
            top = float(np.nonzero(b > 127)[0].min())
            J = np.float32([[1, 0, 0], [0, jaw, top * (1 - jaw)]])
            sprite = cv2.warpAffine(sprite, J, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            b = cv2.warpAffine(b, J, (w, h), flags=cv2.INTER_LINEAR)
        # the mask: the mouth plus a margin of its own fur (lip shading), feathered; nothing near the nose
        m = cv2.dilate(b, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13))).astype(np.float32) / 255
        m = cv2.GaussianBlur(m, (0, 0), 3.0)
        m[nose_mask(f.shape) > 0] = 0
        m = cv2.GaussianBlur(m, (0, 0), 1.0)
        self.info[name] = {"clip": clip, "frame": i, "align": how, "scale": round(s, 3), "rot": round(deg, 2),
                           "corners": c.round(1).tolist()}
        return sprite, m

    def erase(self, f, W):
        """The idle frame with its smile line (and the bottom of the philtrum) inpainted away."""
        band = to_frame(self.band, W, f.shape, cv2.INTER_NEAREST)
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        line = ((g < 175) & (band > 0)).astype(np.uint8) * 255
        line = cv2.dilate(line, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7)))
        return cv2.inpaint(f, line, 6, cv2.INPAINT_TELEA)

    def paste(self, base, name, W):
        sprite, m = self.sprites[name]
        s = to_frame(sprite, W, base.shape).astype(np.float32)
        m = to_frame(m, W, base.shape, cv2.INTER_LINEAR)
        ring = (m > 0.03) & (m < 0.35)
        if ring.any():   # match the fur to this frame's light, per channel
            s += base[ring].astype(np.float32).mean(0) - s[ring].mean(0)
        m3 = m[..., None]
        return np.clip(base.astype(np.float32) * (1 - m3) + s * m3, 0, 255).astype(np.uint8)

    def frame(self, f, W, name, erased=None):
        if name == "rest":
            return f
        return self.paste(self.erase(f, W) if erased is None else erased, name, W)


def main():
    T = json.loads(Path(sys.argv[1]).read_text())
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    args = sys.argv[3:]
    sh = Shapes(T)
    print(json.dumps(sh.info, indent=1))
    idle = frames(IDLE)
    assert tuple(NAMES) == ("rest", *PICKS), NAMES
    if "--still" in args:
        k = int(args[args.index("--still") + 1])
        W = np.array(T[IDLE][k]["w"], np.float32)
        er = sh.erase(idle[k], W)
        tiles = [sh.frame(idle[k], W, n, er) for n in NAMES]
        crop = [t[200:340, 460:700] for t in [er] + tiles]
        sheet = np.hstack(crop)
        cv2.imwrite(str(out / f"still_{k}.png"), cv2.resize(sheet, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
        if "--check" in args:
            for n, t in zip(NAMES, tiles):
                cv2.imwrite(str(out / f"check_{k}_{n}.png"), cv2.resize(t[180:360, 440:720], None, fx=3, fy=3,
                                                                       interpolation=cv2.INTER_CUBIC))
        return
    h, w = idle[0].shape[:2]
    procs = {}
    for k, n in enumerate(NAMES):
        procs[n] = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                                     "-s", f"{w}x{h}", "-r", "24", "-i", "-", "-c:v", "libx264", "-preset", "slow",
                                     "-crf", "12", "-pix_fmt", "yuv420p", str(out / f"mouth_{k}.mp4")],
                                    stdin=subprocess.PIPE)
    for i, f in enumerate(idle):
        W = np.array(T[IDLE][i]["w"], np.float32)
        er = sh.erase(f, W)
        for n in NAMES:
            procs[n].stdin.write(sh.frame(f, W, n, er).tobytes())
    for k, n in enumerate(NAMES):
        procs[n].stdin.close()
        procs[n].wait()
        print("wrote", out / f"mouth_{k}.mp4")


if __name__ == "__main__":
    main()
