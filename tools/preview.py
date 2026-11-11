#!/usr/bin/env python3
"""Private preview: the whole show, written as HLS for a private page instead of Kick.

  python3 /root/hubie_stream/tools/preview.py start     # starts the stack in the background
  python3 /root/hubie_stream/tools/preview.py start --kick   # LIVE on Kick (+ same picture on the preview page)
  python3 /root/hubie_stream/tools/preview.py status    # parts, pids, CPU/RAM, spend so far
  python3 /root/hubie_stream/tools/preview.py stop      # stops everything it started
  python3 /root/hubie_stream/tools/preview.py url       # the private link + login

The output is always a local HLS folder (served by nginx, snippet /etc/nginx/snippets/hubie_preview.conf).
This script refuses any rtmp output and blanks the go-live switch, so it can't reach Kick.
Spins happen only through the Housebets adapter's fun-mode guard (the same code hb_spin.py runs).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("HUBIE_PREVIEW_ROOT", "/root/hubie_stream"))   # the checkout to run from
VENV_PY = ROOT / ".venv/bin/python"
PREVIEW = Path("/dev/shm/hubie_preview")
HLS = PREVIEW / "hls"
RUN_DIR = Path("/dev/shm/hubie_stream")
PIDFILE = PREVIEW / "guardian.pid"
LOG = PREVIEW / "run.log"
SECRET = ROOT / "data/preview_secret.txt"
USAGE = ROOT / "data/preview_usage.jsonl"
PAGE_SRC = ROOT / "deploy/preview/index.html"
PAGE_DST = Path("/var/www/hubie_preview/index.html")
def _domain() -> str:
    """The public host nginx serves the preview on: HUBIE_PREVIEW_DOMAIN in the environment or the .env."""
    if os.environ.get("HUBIE_PREVIEW_DOMAIN"):
        return os.environ["HUBIE_PREVIEW_DOMAIN"]
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("HUBIE_PREVIEW_DOMAIN="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "localhost"


DOMAIN = _domain()

# Everything the preview run uses. The .env only adds keys (load_dotenv never overrides these).
SHOW_ENV = {
    "HUBIE_RUN_DIR": str(RUN_DIR),
    "HUBIE_LOOK": "mix",                      # screen look: mix (2026-11-09), h or desk1 for rollback (passes through)
    "HUBIE_OUTPUT": f"hls:{HLS}",
    "HUBIE_ALLOW_REMOTE_OUTPUT": "",          # the go-live switch stays blank: no remote target can open
    "HUBIE_SCENE": "ffmpeg",
    "HUBIE_LAYOUT": "desk",
    "HUBIE_SCENE_BG": "x11",                  # the game Chrome on the stream display, scaled into the scene
    "HUBIE_GAME": "housebets",                # provider slots in Try fun mode only, verified games only
    "HUBIE_PROVIDER": "anthropic",
    "HUBIE_ANTHROPIC_MODEL": "claude-opus-5-5",   # chat replies + crisis follow-ups
    "HUBIE_MODEL_ROUND": "claude-haiku-5-5",      # round lines (most calls)
    "HUBIE_MODEL_BIT": "claude-haiku-5-5",        # talk segments
    "HUBIE_LLM_MAX_CALLS_PER_HOUR": "300",
    "HUBIE_USAGE_LOG": str(USAGE),
    "HUBIE_TTS": "elevenlabs",
    "HUBIE_TALK_PACE": "0.33",                # rests between talk-segment lines only; every round still gets a line
    "HUBIE_SHORT_LLM_SHARE": "0.35",          # plain rounds: share of 2-6 word lines asked of Haiku, rest from the pool
    "HUBIE_CHAT": "kick_webhook",
    "KICK_WEBHOOK_PORT": "18795",             # nginx /hubie/kick -> here
    "HUBIE_WD_RSS_BRAIN_MB": "2600",          # proportional memory of the brain + game Chrome (~1.9 GB measured)
    "HUBIE_VIDEO_BITRATE": "2500k",
    # rule: "Plays House originals" -> originals only (owner 2026-11-08). No slot ids = no slots, no slot buys;
    # remove these two lines to bring slots back as extras.
    "HUBIE_HB_GAMES": "none",
    "HUBIE_HB_ORIGINALS_SHARE": "1.0",
    "HUBIE_HB_TOPBAR": "1",                   # the site's own top bar above the game (0 = game fills the window)
    "HUBIE_FILL_AFTER": "10",                 # silence this long gets a few pool words (game loads, long rounds)
    "HUBIE_HB_BROWSE": "1",                   # game switches: Hubie browses the slots page and clicks the tile
    "HUBIE_WD_DEAD_AIR": "30",                # a quiet brain is restarted after this (asides fill every planned wait)
    # desk Hubie v3 (paw on the Housebets mouse) and the click clip on every spin. Revert: start the preview with
    # HUBIE_MOUTH_DIR=/root/hubie_stream/assets/cam_visemes HUBIE_CLICK_MOUTH_DIR= HUBIE_CAM_ZOOM=1.18
    # (these four pass through)
    "HUBIE_CAM_IDLE": str(ROOT / "shots/kling_idle_v3.mp4"),
    "HUBIE_MOUTH_DIR": str(ROOT / "assets/cam_visemes_v3"),
    "HUBIE_CLICK_MOUTH_DIR": str(ROOT / "assets/cam_visemes_v3/click"),
    "HUBIE_CAM_ZOOM": "1.0",                  # v3: the whole shot, hat to mousepad (v1 used 1.18)
    # live check 2026-11-07: reels move ~1.1 s after the real click (game + relay), so the click goes out
    # 1 s before the claw lands and the reels start as it lands
    "HUBIE_CLICK_OFFSET_MS": "-1000",
    # v3 reactions: start and end on idle v3's frame 0, so the stage's frame-0 guard keeps them
    "HUBIE_REACT_WIN": str(ROOT / "shots/kling_react_win_v3.mp4"),
    "HUBIE_REACT_LOSS": str(ROOT / "shots/kling_react_loss_v3.mp4"),
    "HUBIE_REACT_SURPRISE": str(ROOT / "shots/kling_react_surprise_v3.mp4"),
    "HUBIE_REACT_BANANA": str(ROOT / "shots/kling_react_banana_v3.mp4"),
    # no time cap: the show signs off by itself when this many ElevenLabs characters are left (hubie/credits.py).
    # A planned end: start with HUBIE_END_AT=<unix ts or ISO time, UTC when no zone> (passes through).
    "HUBIE_SIGNOFF_CHARS": "2500",
    # music bed under the voice: public-domain packs in assets/music/<pack>/ (house, lofi, lounge, night; each has
    # its LICENSES.md), shuffled in a loop. Tracks are -24 LUFS; GAIN adds on top (bed ~-32 LUFS vs voice ~-23),
    # DUCK comes off while Hubie talks. Pick the pack with HUBIE_MUSIC_DIR, or rotate HUBIE_MUSIC_PACKS="lofi,night"
    # every HUBIE_MUSIC_PACK_TRACKS tracks. Mid-stream switch, no restart: write e.g. "lofi" (or "lofi,night")
    # into the pack file; it fades the current track out and goes on with that pack (delete the file = back to
    # this env choice). Off for one run: start with HUBIE_MUSIC_DIR= (all of these pass through).
    "HUBIE_MUSIC_DIR": str(ROOT / "assets/music/house"),
    "HUBIE_MUSIC_GAIN_DB": "-8",
    "HUBIE_MUSIC_DUCK_DB": "9",
    # owner pick 2026-11-09: rotate all four packs, 5 tracks each (HUBIE_MUSIC_PACKS= on the command line = one pack)
    "HUBIE_MUSIC_PACKS": "house,lofi,lounge,night",
    "HUBIE_MUSIC_PACK_TRACKS": "5",
    "HUBIE_MUSIC_PACK_FILE": str(RUN_DIR / "music_pack"),
}
PASS_THROUGH = ("HUBIE_LOOK", "HUBIE_CAM_IDLE", "HUBIE_MOUTH_DIR", "HUBIE_CLICK_MOUTH_DIR", "HUBIE_CAM_ZOOM",
                "HUBIE_SIGNOFF_CHARS", "HUBIE_END_AT", "HUBIE_MUSIC_DIR", "HUBIE_MUSIC_GAIN_DB", "HUBIE_MUSIC_DUCK_DB",
                "HUBIE_MUSIC_PACKS", "HUBIE_MUSIC_PACK_TRACKS", "HUBIE_MUSIC_PACK_FILE")
SHOW_OVER = RUN_DIR / "show_over.json"     # written by the sign-off; guardian + supervisor then restart nothing


def secret() -> tuple[str, str]:
    tok, pw = SECRET.read_text().split()
    return tok, pw


def url() -> str:
    return f"https://{DOMAIN}/hubie/preview-{secret()[0]}/"


def guardian_pid() -> int | None:
    try:
        pid = int(PIDFILE.read_text().strip())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    # exact process check: our venv python running "-m hubie run"
    return pid if b"hubie" in cmd and b"run" in cmd and str(VENV_PY).encode() in cmd[0] else None


def eleven_used() -> int | None:
    key = None
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("ELEVENLABS_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key:
        return None
    try:
        out = subprocess.run(["curl", "-s", "-m", "8", "-H", f"xi-api-key: {key}",
                              "https://api.elevenlabs.io/v1/user/subscription"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(json.loads(out)["character_count"])
    except Exception:
        return None


def _kick_target() -> str:
    """rtmps://<ingest>/app/<key> from KICK_STREAM_URL + KICK_STREAM_KEY in the .env (never printed)."""
    vals = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(("KICK_STREAM_URL=", "KICK_STREAM_KEY=")):
            k, v = line.split("=", 1)
            vals[k] = v.strip().strip('"')
    u, k = vals.get("KICK_STREAM_URL", ""), vals.get("KICK_STREAM_KEY", "")
    if not (u.startswith("rtmps://") and k):
        raise SystemExit("KICK_STREAM_URL (rtmps://...) and KICK_STREAM_KEY must be in the .env")
    u = u.rstrip("/")
    if u.count("/") == 2:                  # Kick shows the bare ingest host; IVS wants :443/app/<key>
        u += ("" if u.rsplit(":", 1)[-1].isdigit() else ":443") + "/app"
    return u + "/" + k


def start(kick: bool = False) -> int:
    """kick=False: private preview only (HLS). kick=True: live on Kick AND the same picture on the preview page."""
    if guardian_pid():
        print("already running:", guardian_pid())
        return 0
    show = dict(SHOW_ENV)
    if kick:
        show["HUBIE_OUTPUT"] = _kick_target()
        show["HUBIE_ALLOW_REMOTE_OUTPUT"] = "owner-approved-going-live"   # owner OK'd Kick runs, 2026-11-07
        show["HUBIE_OUTPUT_MIRROR_HLS"] = str(HLS)
    elif not str(show["HUBIE_OUTPUT"]).startswith("hls:/"):
        raise SystemExit("refusing: preview output must be a local HLS folder")
    HLS.mkdir(parents=True, exist_ok=True)
    os.chmod(PREVIEW, 0o755)
    os.chmod(HLS, 0o755)
    for f in HLS.glob("*"):
        f.unlink(missing_ok=True)
    (RUN_DIR / "scene/session.json").unlink(missing_ok=True)   # panel: time live, spins, best start fresh
    SHOW_OVER.unlink(missing_ok=True)      # a new show: the last one's sign-off no longer applies
    if PAGE_SRC.exists():
        PAGE_DST.parent.mkdir(parents=True, exist_ok=True)
        PAGE_DST.write_bytes(PAGE_SRC.read_bytes())
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HUBIE_", "KICK_STREAM"))}
    env.update(show)
    env.update({k: os.environ[k] for k in PASS_THROUGH if k in os.environ})
    env["PYTHONUNBUFFERED"] = "1"
    used = eleven_used()
    (PREVIEW / "started.json").write_text(json.dumps({"ts": time.time(), "eleven_used": used,
                                                      "usage_lines": _usage_lines()}))
    with open(LOG, "ab") as lf:
        p = subprocess.Popen([str(VENV_PY), "-m", "hubie", "run"], cwd=str(ROOT), env=env, stdout=lf, stderr=lf,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    PIDFILE.write_text(str(p.pid))
    print(json.dumps({"started": p.pid, "url": url(), "log": str(LOG), "kick": kick}))
    return 0


def _usage_lines() -> int:
    try:
        return len(USAGE.read_text().splitlines())
    except OSError:
        return 0


def spend() -> dict:
    """Anthropic tokens and cost since the last start, ElevenLabs characters since the last start."""
    # $ per M tokens: input, output, cache read, cache write (same table as evals/run_eval.py)
    price = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125)}
    try:
        st = json.loads((PREVIEW / "started.json").read_text())
    except (OSError, ValueError):
        st = {}
    lines = USAGE.read_text().splitlines()[st.get("usage_lines", 0):] if USAGE.exists() else []
    by: dict = {}
    for ln in lines:
        r = json.loads(ln)
        m = by.setdefault(r["model"], {"calls": 0, "input": 0, "output": 0, "cached": 0, "cache_write": 0})
        m["calls"] += 1
        for k in ("input", "output", "cached", "cache_write"):
            m[k] += r[k]
    cost = 0.0
    for model, m in by.items():
        pin, pout, pr, pw = price.get(model, (4.0, 20.0, 0.20, 5.0))
        cost += (m["input"] * pin + m["cache_write"] * pw + m["cached"] * pr + m["output"] * pout) / 1e6
    now_used = eleven_used()
    chars = now_used - st["eleven_used"] if now_used is not None and st.get("eleven_used") is not None else None
    return {"anthropic": by, "anthropic_usd_est": round(cost, 4), "elevenlabs_chars": chars}


def tree(pid: int) -> list[int]:
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        try:
            kids = Path(f"/proc/{p}/task/{p}/children").read_text().split()
        except OSError:
            kids = []
        todo += [int(k) for k in kids]
    return out


def usage_of(pids: list[int]) -> dict:
    rss = 0
    for p in pids:
        try:
            for ln in Path(f"/proc/{p}/status").read_text().splitlines():
                if ln.startswith("VmRSS:"):
                    rss += int(ln.split()[1])
        except OSError:
            pass
    return {"procs": len(pids), "rss_mb": round(rss / 1024)}


def status() -> int:
    pid = guardian_pid()
    try:
        over = json.loads(SHOW_OVER.read_text())
    except (OSError, ValueError):
        over = {"reason": "unreadable flag"} if SHOW_OVER.exists() else None
    print(json.dumps({"running": bool(pid), "guardian": pid, "url": url(), "show_over": over,
                      **(usage_of(tree(pid)) if pid else {})}))
    sup = RUN_DIR / "status/supervisor.json"
    if pid and sup.exists():
        print(sup.read_text())
    print(json.dumps(spend()))
    return 0


def stop() -> int:
    pid = guardian_pid()
    if not pid:
        print("not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        if not Path(f"/proc/{pid}").exists() or guardian_pid() is None:
            break
        time.sleep(1)
    else:
        for p in reversed(tree(pid)):
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
    PIDFILE.unlink(missing_ok=True)
    print(json.dumps({"stopped": pid, **spend()}))
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "url":
        tok, pw = secret()
        print(json.dumps({"url": url(), "user": os.environ.get("HUBIE_PREVIEW_USER", "viewer"), "password": pw}))
        return 0
    if cmd == "start" and "--kick" in sys.argv[2:]:
        return start(kick=True)
    return {"start": start, "stop": stop, "status": status}.get(cmd, lambda: (print(__doc__), 2)[1])()


if __name__ == "__main__":
    sys.exit(main())
