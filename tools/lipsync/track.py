"""Offline: per-frame affine warp of Hubie's face (reference = idle frame 0) for a set of clips, by ECC on the
face with the mouth and eyes masked out. Cartoon sloth, so no landmark model; the nose and eye patches carry it."""
import json
import sys
from pathlib import Path

import cv2
import numpy as np

FACE = (420, 130, 330, 240)            # x, y, w, h in the 1164x776 source, around the face
MOUTH = (480, 245, 200, 90)            # masked out: changes while talking
EYES = [(445, 160, 110, 70), (600, 150, 120, 70)]   # masked out: blinks
SC = 0.5


def frames(path):
    cap = cv2.VideoCapture(str(path))
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        out.append(f)
    return out


def prep(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.GaussianBlur(g, (5, 5), 0)
    return cv2.resize(g, None, fx=SC, fy=SC, interpolation=cv2.INTER_AREA).astype(np.float32) / 255


def mask_for(shape):
    m = np.zeros(shape, np.uint8)
    x, y, w, h = [int(v * SC) for v in FACE]
    m[y:y + h, x:x + w] = 255
    for bx, by, bw, bh in [MOUTH] + EYES:
        x, y, w, h = [int(v * SC) for v in (bx, by, bw, bh)]
        m[y:y + h, x:x + w] = 0
    return m


def track(ref, clip, init=None):
    """Returns 2x3 warps mapping reference coords -> frame coords, full-resolution units."""
    R = prep(ref)
    mask = mask_for(R.shape)
    W = np.eye(2, 3, dtype=np.float32) if init is None else init.copy()
    out = []
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6)
    for f in clip:
        F = prep(f)
        Ws = W.copy(); Ws[:, 2] *= SC
        try:
            cc, Ws = cv2.findTransformECC(R, F, Ws, cv2.MOTION_AFFINE, crit, mask, 5)
        except cv2.error:
            cc = -1
        W = Ws.copy(); W[:, 2] /= SC
        out.append((W.copy(), float(cc)))
    return out


if __name__ == "__main__":
    ref_clip, outp = sys.argv[1], sys.argv[2]
    clips = sys.argv[3:]
    ref = frames(ref_clip)[0]
    res = {}
    for c in clips:
        tr = track(ref, frames(c))
        res[c] = [{"w": w.tolist(), "cc": cc} for w, cc in tr]
        ccs = [cc for _, cc in tr]
        print(c, len(tr), "cc min %.3f mean %.3f" % (min(ccs), sum(ccs) / len(ccs)))
    Path(outp).write_text(json.dumps(res))
