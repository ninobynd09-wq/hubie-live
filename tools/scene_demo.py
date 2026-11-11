#!/usr/bin/env python3
"""Records the desk scene running for real (stage server, mixer, encoder) with fake rounds, fake chat and
saved voice samples, so no TTS, LLM, game or Kick call is made. Prints the encoder's CPU and memory.

  .venv/bin/python tools/scene_demo.py OUT.mp4 [--seconds 38] [--bg path.png]
  .venv/bin/python tools/scene_demo.py OUT.mp4 --pacing     # the real orchestrator paces scripted rounds

--pacing runs the real Orchestrator, Speaker and mixer on scripted slot rounds: every round gets a line (short
ones for plain rounds, cut word by word from the saved Rick takes), the next spin waits for the line to finish
plus the 0.8-1.5 s gap, and one dead bonus gets the full fixture line and the loss reaction. No TTS call is made.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VA = ROOT / "voice_audition/work"
FIX = ROOT / "tests/fixtures/eleven/rick_with_timestamps.json"
LINE_A = "yo. mines again. first tile is always the mine, it's physics. damn. anyway, slow and steady, bruh."
LINE_B = "it's three am and the lobby's gone quiet. top shelf banana's still up there. basically a museum piece."
FIX_TEXT = "mm. big bonus, but it paid peanuts. oh well, slow and steady, bruh. the banana stays on the shelf."


# Short lines for --pacing, cut from the saved Rick takes: (caption, [(source, start, end), ...]). Word times are
# faster-whisper word timestamps of those files.
RICK_ALL = ROOT / "voice_audition/eleven_rick.mp3"
SHORT_CUTS = {
    "yo. it's three am.": [(RICK_ALL, 0.0, 0.34), (VA / "eleven_rick_eleven_v4_turbo_B.mp3", 0.0, 0.95)],
    "gone quiet.": [(VA / "eleven_rick_eleven_v4_turbo_B.mp3", 1.92, 2.68)],
    "damn. anyway.": [(VA / "eleven_rick_eleven_v4_turbo_A.mp3", 5.24, 5.66), (VA / "eleven_rick_eleven_v4_turbo_A.mp3", 6.38, 6.84)],
    "it's physics.": [(RICK_ALL, 3.96, 4.62)],
    "basically a museum piece.": [(RICK_ALL, 14.92, 16.48)],
}
# (multiplier, won, free spins, line). Plain rounds must come out short, the dead bonus full.
PACING_SCRIPT = [
    (0.0, False, False, "yo. it's three am."),
    (0.0, False, False, "gone quiet."),
    (0.4, False, False, "damn. anyway."),
    (0.4, False, True, FIX_TEXT),
    (0.0, False, False, "it's physics."),
    (1.3, True, False, "basically a museum piece."),
]


# --v5: game switch card, switch line, a waiting aside, short lines, the recent-spins strip, the play label and the
# Housebets top bar. Audio is cut word by word from saved takes (the live preview recording and the Rick takes);
# the captions are exactly what those cuts say.
V5_REC = Path("/tmp/claude-0/-root/d438af93-d4b5-49ed-b8a8-997cbfd4437d/scratchpad/v5/rec.wav")
V5_B = VA / "eleven_rick_eleven_v4_turbo_B.mp3"
V5_A = VA / "eleven_rick_eleven_v4_turbo_A.mp3"
V5_CUTS = {
    "s_gone": ("it's gone.", [(V5_B, 0.0, 0.26), (V5_B, 1.96, 2.14)]),
    "s_back": ("a little came back.", [(V5_REC, 81.04, 81.86), (V5_REC, 27.94, 28.5)]),
    "s_win": ("a small one landed.", [(V5_REC, 172.0, 173.58)]),
    "switch": ("sweet bonanza super scatter now.", [(V5_REC, 56.22, 59.04)]),
    "wait": ("lobby's gone quiet.", [(V5_B, 1.56, 2.6)]),
    "s_damn": ("damn. anyway.", [(V5_A, 5.24, 5.66), (V5_A, 6.38, 6.84)]),
}


def v5_backgrounds(work: Path) -> dict:
    """The game box as the stream sees it with HUBIE_HB_TOPBAR=1: the site's own header (from a saved page shot)
    on top, the game at 16:9 under it, the site's dark colour in the side margins."""
    from PIL import Image
    from hubie.games.housebets import TOPBAR_H, GameView
    v = GameView.make(1280, 720, topbar=True)
    hdr = Image.open(ROOT / "shots/housebets/hb_game_authed.png").convert("RGB").crop((0, 0, 1366, TOPBAR_H))
    hdr = hdr.resize((1280, round(TOPBAR_H * 1280 / 1366)))
    out = {}
    for key, shot in (("gates", "shots/hb_loaded_gates_of_olympus_ss.png"),
                      ("bonanza", "shots/hb_sweet_bonanza_1000_7.png")):
        img = Image.new("RGB", (1280, 720), (11, 25, 32))
        img.paste(hdr, (0, 0))
        g = Image.open(ROOT / shot).convert("RGB").resize((v.w, v.h))
        img.paste(g, (int(v.x0), int(v.y0)))
        out[key] = work / f"bg_{key}.png"
        img.save(out[key])
    return out


def h_backgrounds(work: Path) -> dict:
    """The 1280x720 game display as the live adapter leaves it: a house original at 0.9 zoom (with the DEMO PLAY
    patch over the balance) and a slot under the site's top bar."""
    from PIL import Image
    out = {}
    for key, shot in (("road", "qa/originals_1108/fix_live/hb_hubies_road_10.png"),
                      ("slot", "qa/originals_1108/adapter_live/hb_sweet_bonanza_1000_16.png")):
        img = Image.new("RGB", (1280, 720), (11, 25, 32))
        img.paste(Image.open(ROOT / shot).convert("RGB"), (0, 0))
        out[key] = work / f"bg_{key}.png"
        img.save(out[key])
    return out


def cut_clip(parts, out: Path) -> None:
    """Join word-timed pieces of saved takes, 0.18 s apart, with short fades so no cut clicks."""
    import wave
    pcm = b""
    for k, (src, s0, s1) in enumerate(parts):
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-af",
                            f"atrim={s0}:{s1},asetpts=N/SR/TB,afade=t=in:d=0.02,afade=t=out:st={s1 - s0 - 0.04:.3f}:d=0.04",
                            "-ac", "1", "-ar", "48000", "-f", "s16le", "-"], capture_output=True, check=True)
        pcm += (b"\x00\x00" * int(0.18 * 48000) if k else b"") + r.stdout
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(pcm)


def pacing_setup(cfg, clips: Path, prepared: dict, eleven, fx, spin: float):
    """Builds the real orchestrator around scripted rounds and saved clips; returns a function that plays the script."""
    import wave
    from hubie.audio.queue import Speaker
    from hubie.brain.brain import Brain
    from hubie.brain.memory import Memory
    from hubie.brain.persona import Persona
    from hubie.brain.provider import MockProvider
    from hubie.brain.scheduler import PLAY, Scheduler, Segment
    from hubie.chat.base import NoChat
    from hubie.games.base import GameAdapter, GameInfo, RoundResult
    from hubie.orchestrator import Orchestrator
    from hubie.safety.moderation import ChatModerator
    from hubie.tts.base import TTSEngine
    from hubie.util import post_stage

    wavs = {FIX_TEXT: (prepared["F"][0], prepared["F"][2])}
    for i, (text, parts) in enumerate(SHORT_CUTS.items()):
        out = clips / f"short{i}.wav"
        cut_clip(parts, out)
        wavs[text] = (out, None)

    class SavedTTS(TTSEngine):
        """The saved clip for each scripted line, after the ~0.7 s an ElevenLabs call takes."""
        name = "saved"
        last_alignment = None

        def synthesize(self, text, out_wav):
            src, al = wavs[text]
            time.sleep(0.7)
            shutil.copy(src, out_wav)
            self.last_alignment = al
            with wave.open(str(out_wav)) as w:
                return w.getnframes() / w.getframerate()

    script = list(PACING_SCRIPT)
    game = GameInfo("sweet_bonanza_1000", "Sweet Bonanza 1000", "slot", "Candy slot, tumbles.")

    class Scripted(GameAdapter):
        name = "scripted"
        n = 0
        bal = 1004.6
        def start(self): pass
        def list_games(self): return [game]
        def load_game(self, gid): pass
        def is_demo(self): return True
        def recover(self): pass

        def play_round(self, bet, options=None):
            mult, won, fs, _ = script[self.n]
            time.sleep(spin)                               # the reels; the next spin only starts after the gap
            self.n += 1
            self.bal = round(self.bal - 2.0 + 2.0 * mult, 2)
            detail = {"slot": game.name, "tumbles": 1 if mult else 0, "free_spins_triggered": fs}
            if fs:
                detail["free_spins_count"] = 10
            return RoundResult(game.id, f"demo-{self.n}", 2.0, mult, round(2.0 * mult, 2), self.bal, True, detail)

    persona = Persona.load(ROOT / "persona/hubie.draft.yaml")
    mem = Memory()
    log = []

    class ScriptedBrain(Brain):
        """Real brain, except the words are the ones the saved audio says. Checks the orchestrator asked for the
        right kind of line: short for plain rounds, full for the dead bonus."""
        def comment_round(self, result, game_name, segment_title="Now playing", how="", short=False):
            text = script[len(log)][3]
            assert short == (text != FIX_TEXT), (text, short)
            log.append((round(time.time() - start[0], 2), text, short))
            print(f"{log[-1][0]:6.2f}s  round {len(log)}  {'short' if short else 'FULL '}  {text}", flush=True)
            return self.filter.approve(text)

    brain = ScriptedBrain(persona, MockProvider(1), mem)
    orch = Orchestrator(Scripted(), NoChat(), brain, Speaker(SavedTTS(), cfg.queue_dir),
                        Scheduler(mem, persona.topics, persona.night_topics), ChatModerator(),
                        notify=lambda ev: post_stage(cfg.stage_url, ev), bet_options=(2,), seed=3)
    start = [0.0]

    def run():
        start[0] = time.time()
        post_stage(cfg.stage_url, {"type": "segment", "kind": "play", "title": "late night spins", "game": game.name})
        orch.games()
        orch.segment = Segment(PLAY, "late night spins", target=len(script), game_id=game.id)
        states = []
        while orch.counts["rounds"] < len(script) or orch.speaker.pending():
            st = orch.step()
            if not states or states[-1][1] != st:
                states.append((round(time.time() - start[0], 2), st))
            time.sleep(0.1)
        print("states:", " ".join(f"{t}:{s}" for t, s in states), flush=True)
    return run


# --v6: layout 1 (cam top, live chat list, stats bottom) with the REAL orchestrator, moderation and chat queue on a
# scripted chat: every message answered by name (Haiku writes the replies, at most ~$0.02), a busy burst batched, a
# blocked link never shown, rounds every few seconds with their lines. Round lines are cut from saved takes; the chat
# replies have no TTS call allowed, so they are captioned and mouthed but silent.
V6_CHAT = [   # (seconds, username, text)
    (0.6, "Kokax", "wsp g"),
    (0.9, "mina_k", "olympus again lol"),
    (1.2, "tiltedtoby", "pick a tile for me"),
    (1.5, "spam_bot", "check out my channel www.spam-example.xyz free crypto"),
    (1.8, "3amgrinder", "whats in the banana stash"),
    (2.1, "devo", "gg on that bonus"),
    (2.4, "lena_k", "first time here, hi hubie"),
    (20.0, "pixel_pia", "is the bow tie real"),
    (20.8, "devo", "also what game next"),
    (21.2, "NightOwl42", "how long have you been live"),
]
V6_ROUNDS = [  # (multiplier, free spins)
    (0.0, False), (0.45, False), (1.3, False), (0.0, False), (0.4, True), (0.0, False), (1.6, False), (0.3, False),
    (0.0, False), (1.2, False), (0.0, False), (0.5, False),
]


def v6_setup(cfg, clips: Path, prepared: dict, spin: float, seconds: float):
    """The real orchestrator, brain (chat replies on Haiku, round words from saved cuts), moderation and chat queue,
    on a scripted chat. Returns a function that runs it and prints what was asked, shown and said."""
    import wave
    from hubie.audio import lipsync, queue as aq
    from hubie.audio.queue import Speaker
    from hubie.brain.brain import Brain
    from hubie.brain.memory import Memory
    from hubie.brain.persona import Persona
    from hubie.brain.provider import AnthropicProvider, MockProvider, ResilientProvider, RoutedProvider
    from hubie.brain.scheduler import PLAY, Scheduler, Segment
    from hubie.chat.fake import FakeChatSource
    from hubie.games.base import GameAdapter, GameInfo, RoundResult
    from hubie.orchestrator import Orchestrator
    from hubie.safety.moderation import ChatModerator
    from hubie.tts.base import TTSEngine
    from hubie.util import post_stage

    shorts = {}
    for key in ("s_gone", "s_back", "s_win", "s_damn"):
        shorts[prepared[key][1]] = prepared[key][0]
    for i, (text, parts) in enumerate(SHORT_CUTS.items()):
        out = clips / f"short{i}.wav"
        cut_clip(parts, out)
        shorts[text] = out
    voiced = {**shorts, FIX_TEXT: prepared["F"][0]}
    silent = set()

    class DemoTTS(TTSEngine):
        """Saved audio for the round lines; anything else (chat replies) is a silent clip of speaking length."""
        name = "saved"
        last_alignment = None

        def synthesize(self, text, out_wav):
            time.sleep(0.5)
            self.last_alignment = prepared["F"][2] if text == FIX_TEXT else None
            if text in voiced:
                shutil.copy(voiced[text], out_wav)
            else:
                dur = max(1.2, len(text) / 15.0)
                with wave.open(str(out_wav), "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
                    w.writeframes(b"\x00\x00" * int(dur * 48000))
                silent.add(Path(out_wav).name)
            with wave.open(str(out_wav)) as w:
                return w.getnframes() / w.getframerate()

    real_plan = lipsync.plan

    def plan(text, wav, alignment=None, fps=lipsync.FPS):   # silent clips still get a mouth, from the text
        if Path(wav).name in silent:
            with wave.open(str(wav)) as w:
                n = int(w.getnframes() / w.getframerate() / 0.05)
            return lipsync.track_from_env(text, [0.6] * n)
        return real_plan(text, wav, alignment, fps)
    aq.lipsync.plan = plan

    game = GameInfo("gates_of_olympus_1000", "Gates of Olympus 1000", "slot", "Tumbles and multiplier orbs.")
    results = list(V6_ROUNDS)

    class Scripted(GameAdapter):
        name = "scripted"
        n = 0
        bal = 99991.70
        def start(self): pass
        def list_games(self): return [game]
        def load_game(self, gid): pass
        def is_demo(self): return True
        def recover(self): pass

        def play_round(self, bet, options=None):
            mult, fs = results[self.n % len(results)]
            time.sleep(spin)
            self.n += 1
            self.bal = round(self.bal - 2.0 + 2.0 * mult, 2)
            detail = {"slot": game.name, "free_spins_triggered": fs, **({"free_spins_count": 10} if fs else {})}
            return RoundResult(game.id, f"demo-{self.n}", 2.0, mult, round(2.0 * mult, 2), self.bal, True, detail)

    persona = Persona.load(ROOT / "persona/hubie.draft.yaml")
    mem = Memory()
    order = list(shorts)

    class DemoBrain(Brain):
        """Real brain; only the round words are the ones the saved audio says."""
        def comment_round(self, result, game_name, segment_title="Now playing", how="", short=False):
            if not short:
                return self.filter.approve(FIX_TEXT)
            text = order[self.rounds_said % len(order)]
            self.rounds_said += 1
            return self.filter.approve(text)

    key = os.environ.get("ANTHROPIC_API_KEY") or next((ln.split("=", 1)[1].strip().strip('"') for ln in
                                                        (ROOT / ".env").read_text().splitlines()
                                                        if ln.startswith("ANTHROPIC_API_KEY=")), None)
    os.environ["HUBIE_USAGE_LOG"] = str(clips.parent / "usage.jsonl")
    haiku = AnthropicProvider(model="claude-haiku-5-5", api_key=key)
    provider = ResilientProvider(RoutedProvider(MockProvider(1), {"chat": haiku, "chat_fast": haiku}),
                                 max_calls_per_hour=40)
    brain = DemoBrain(persona, provider, mem, short_llm_share=0.0)
    brain.rounds_said = 0
    chat = FakeChatSource()
    events = []

    def notify(ev):
        events.append(ev)
        post_stage(cfg.stage_url, ev)
    orch = Orchestrator(Scripted(), chat, brain, Speaker(DemoTTS(), cfg.queue_dir),
                        Scheduler(mem, persona.topics, persona.night_topics), ChatModerator(),
                        notify=notify, bet_options=(2,), seed=3)
    said = []
    real_say = orch.speaker.say

    def say(line, kind="line", mood="idle", **kw):
        ok = real_say(line, kind=kind, mood=mood, **kw)
        said.append((round(time.time() - start[0], 1), kind, line.text, kw.get("reply_to")))
        print(f"{said[-1][0]:6.1f}s  {kind:7s} {line.text}" + (f"   -> {kw['reply_to']['names']}" if kw.get("reply_to") else ""),
              flush=True)
        return ok
    orch.speaker.say = say
    start = [0.0]

    def run():
        start[0] = time.time()
        post_stage(cfg.stage_url, {"type": "segment", "kind": "play", "title": "spinning slowly", "game": game.name})
        post_stage(cfg.stage_url, {"type": "game_state", "state": "ready", "game": game.name})
        orch.games()
        orch.segment = Segment(PLAY, "spinning slowly", target=999, game_id=game.id)
        pending = list(V6_CHAT)

        def feeder():
            for at, u, t in pending:
                d = start[0] + at - time.time()
                if d > 0:
                    time.sleep(d)
                chat.push(u, t)
                print(f"{time.time() - start[0]:6.1f}s  CHAT    {u}: {t}", flush=True)
        threading.Thread(target=feeder, daemon=True).start()
        while time.time() - start[0] < seconds - 2.5:
            orch.step()
            time.sleep(0.1)
        shown = [e["name"] for e in events if e["type"] == "chat"]
        print("shown on screen:", shown, flush=True)
        print("counts:", {k: orch.counts.get(k) for k in ("rounds", "chat_replies", "answered", "batches", "ignored")},
              "queue left:", len(orch.chatq), "llm calls:", brain.llm_calls, flush=True)
    return run


# --v7: desk Hubie v3 with the click clip. A fake game drawn live (Gates of Olympus still, reels that roll from the
# moment it is clicked) is piped into the game box; each spin asks the real stage for the click plan through the same
# GameAdapter.click_at the live adapter uses, waits until the claw lands and only then "clicks" the game. One spin is a
# bonus-buy confirm, and a line is spoken across one click. Writes spins.json (planned and actual click times).
V7_SPINS = [1.4, 6.9, 12.1, 17.3, 23.2, 28.4, 33.4]   # when each spin is asked for (s); the 4th is talked over
V7_BUY = 4                                              # index of the bonus-buy confirm


class FakeReels:
    """640x360 game picture at the stream rate: the still, and after a click the six reels roll (blurred) and stop
    one by one. Frames are drawn from the wall clock when written, like a browser on the display."""
    W, H = 640, 360
    COLS = [(102, 175), (175, 247), (247, 320), (320, 393), (393, 466), (466, 538)]
    TOP, BOT = 33, 310

    def __init__(self, shot: Path, fps: int):
        import numpy as np
        from PIL import Image, ImageFilter
        img = Image.open(shot).convert("RGB").resize((self.W, self.H))
        self.still = np.asarray(img).copy()
        reel = img.crop((0, self.TOP, self.W, self.BOT))
        self.blur = np.asarray(reel.filter(ImageFilter.BoxBlur(0)).resize((self.W, (self.BOT - self.TOP) * 3 // 3))).copy()
        k = 19
        b = self.blur.astype(np.float32)
        acc = sum(np.roll(b, d, axis=0) for d in range(-k // 2, k // 2 + 1)) / k
        self.blur = acc.astype(np.uint8)
        self.fps = fps
        self.spin_t = None
        self.clicks: list[float] = []

    def click(self) -> None:
        self.spin_t = time.time()
        self.clicks.append(self.spin_t)

    def frame(self, now: float):
        import numpy as np
        f = self.still.copy()
        if self.spin_t is not None and now >= self.spin_t:
            dt = now - self.spin_t
            hgt = self.BOT - self.TOP
            for c, (x0, x1) in enumerate(self.COLS):
                if dt < 1.5 + 0.15 * c:
                    off = int(dt * 1400) % hgt
                    f[self.TOP:self.BOT, x0:x1] = np.roll(self.blur[:, x0:x1], off, axis=0)
            if dt < 0.2:   # the spin button flashes as it is pressed
                f[300:350, 470:525] = np.clip(f[300:350, 470:525].astype(np.int16) + 90, 0, 255).astype(np.uint8)
        return f

    def run(self, fifo: Path, stop) -> None:
        fd = open(fifo, "wb", buffering=0)
        t0 = time.time()
        k = 0
        while not stop.is_set():
            due = t0 + k / self.fps
            d = due - time.time()
            if d > 0:
                time.sleep(d)
            try:
                fd.write(self.frame(time.time()).tobytes())
            except (BrokenPipeError, OSError):
                break
            k += 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=38)
    ap.add_argument("--bg", default=str(ROOT / "shots/funmode_game_1280.png"))
    ap.add_argument("--sync", action="store_true", help="beeps with a wide mouth on each, to measure A/V offset")
    ap.add_argument("--pacing", action="store_true", help="real orchestrator pacing: short lines on plain rounds")
    ap.add_argument("--v5", action="store_true", help="switch card, switch line, aside, short lines, spins strip")
    ap.add_argument("--spin", type=float, default=5.5, help="--pacing: seconds a spin takes before it lands")
    ap.add_argument("--v6", action="store_true", help="layout 1: chat list, every message answered by name")
    ap.add_argument("--v7", action="store_true", help="desk Hubie v3: the click clip lands as each spin is sent")
    ap.add_argument("--h", action="store_true", help="look H: a house original, chat, two lines with a gap (bubble "
                                                     "dims), clicks, then a switch to a slot under the card")
    ap.add_argument("--react", action="store_true", help="look H, no speech: the win, loss and surprise reaction "
                                                         "clips each asked for once (5 s cooldown), to see them play")
    a = ap.parse_args()
    if a.react:
        a.h = True
        os.environ["HUBIE_REACT_COOLDOWN"] = "5"
    work = Path(tempfile.mkdtemp(prefix="hubie_demo_", dir="/dev/shm"))
    v5_bg = None
    if a.v5 or a.v6:
        sys.path.insert(0, str(ROOT))
        v5_bg = v5_backgrounds(work)
        shutil.copy(v5_bg["gates"], work / "bg.png")
        a.bg = str(work / "bg.png")
        a.seconds = 56.0
    if a.v6:
        a.seconds = 60.0
    v7_extra = {}
    h_bg = None
    if a.h:
        h_bg = h_backgrounds(work)
        shutil.copy(h_bg["road"], work / "bg.png")
        a.bg = str(work / "bg.png")
        a.seconds = 26.0
    if a.v7 or a.h:
        a.seconds = 38.0
        v7_extra = {"HUBIE_MOUTH_DIR": str(ROOT / "assets/cam_visemes_v3"),
                    "HUBIE_CLICK_MOUTH_DIR": str(ROOT / "assets/cam_visemes_v3/click"),
                    "HUBIE_CAM_IDLE": str(ROOT / "shots/kling_idle_v3.mp4"), "HUBIE_CAM_ZOOM": "1.0",
                    **({f"HUBIE_REACT_{k.upper()}": str(ROOT / f"shots/kling_react_{k}_v3.mp4")
                        for k in ("win", "loss", "surprise", "banana")} if a.h else {})}
    env = dict(os.environ, HUBIE_RUN_DIR=str(work / "run"), HUBIE_SCENE="ffmpeg", HUBIE_SCENE_BG="image",
               HUBIE_SCENE_BG_IMAGE=a.bg, HUBIE_OUTPUT=f"file:{work / 'rec'}", HUBIE_STAGE_PORT="18890",
               HUBIE_MOUTH_PORT="18893", HUBIE_SEGMENT_SECONDS="3600", PYTHONUNBUFFERED="1", **v7_extra)
    os.environ.update(env)
    from hubie.audio import lipsync
    from hubie.config import Config
    from hubie.games.fake import FakeGameAdapter
    from hubie.pipeline.commands import scene_cmd
    from hubie.tts.eleven import ElevenLabsTTS
    from hubie.util import post_stage

    cfg = Config()
    cfg.ensure_dirs()
    os.mkfifo(cfg.fifo)
    keeper = os.open(cfg.fifo, os.O_RDWR | os.O_NONBLOCK)
    # a session that has been live a while, so the panel reads like mid-stream
    cfg.scene_dir.mkdir(parents=True, exist_ok=True)
    (cfg.scene_dir / "session.json").write_text(json.dumps({
        "live_since": time.time() - 5012, "spins": 214, "best": 12.4, "balance": 1004.6,
        "game": "Gates of Olympus Super Scatter" if a.v5 else ("Gates of Olympus 1000" if a.v6 else
                                                                ("Hubie's Road" if a.h else "Sweet Bonanza 1000")),
        "segment": "", "chat": [],
        "recent": [{"g": "Olympus SS", "m": 0.0, "w": False}, {"g": "Olympus SS", "m": 2.6, "w": True}] if a.v5 else [],
        "screen": {"state": "ready", "game": "Hubie's Road" if a.h else ""}}))
    py = str(ROOT / ".venv/bin/python")
    logs = open(work / "parts.log", "w")
    procs = {"stage": subprocess.Popen([py, "-m", "hubie.stage.server"], cwd=ROOT, env=env, stdout=logs, stderr=logs)}
    time.sleep(1.5)
    cmd = scene_cmd(cfg)
    if a.v5:   # re-read the game still twice a second, so the demo can swap games under the card
        i = cmd.index(a.bg)
        cmd[i - 1:i] = ["-loop", "1", "-framerate", "2", "-f", "image2", "-i"]
    if a.sync:   # burn the wall clock (as read when each frame is drawn) into the corner, for timing checks
        clock = work / "clock.txt"
        clock.write_text("0")

        def tick():
            while True:
                t = time.time()
                tmp = work / ".clock"
                tmp.write_text(f"{t % 100:06.3f}")
                tmp.replace(clock)
                time.sleep(0.005)
        threading.Thread(target=tick, daemon=True).start()
        i = cmd.index("-filter_complex")
        cmd[i + 1] = cmd[i + 1].replace("[vo];", f",drawbox=x=0:y=0:w=260:h=60:color=white:t=fill,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:fontsize=44:fontcolor=black:x=8:y=8:reload=1:textfile={clock}[vo];", 1)
    reels, reel_stop = None, threading.Event()
    if a.v7:   # the game box is the fake game, drawn live and piped in
        reels = FakeReels(ROOT / "shots/hb_loaded_gates_of_olympus_ss.png", cfg.fps)
        fifo = work / "game.rgb"
        os.mkfifo(fifo)
        i = cmd.index(a.bg)
        cmd[i - 1:i + 1] = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-video_size", f"{reels.W}x{reels.H}",
                            "-framerate", str(cfg.fps), "-thread_queue_size", "2", "-i", str(fifo)]
        threading.Thread(target=reels.run, args=(fifo, reel_stop), daemon=True).start()
    procs["encoder"] = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=logs)

    def progress():   # what the supervisor does: the encoder's frame counter, for timing reaction clips
        from hubie import checks
        from hubie.util import write_status
        st, last = {}, None
        for raw in procs["encoder"].stdout:
            checks.parse_progress_line(raw.decode("ascii", "ignore"), st)
            if st.get("frame") != last:
                last = st.get("frame")
                write_status(cfg.status_dir, "encoder_clock", frame=last, fps=cfg.fps)
    threading.Thread(target=progress, daemon=True).start()
    procs["mixer"] = subprocess.Popen([py, "-m", "hubie.audio.mixer"], cwd=ROOT, env=env, stdout=logs, stderr=logs)

    # clips, made the way the Speaker makes them
    clips = work / "clips"
    clips.mkdir()

    def wav_from(src: Path, name: str) -> Path:
        out = clips / f"{name}.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "48000", str(out)], check=True)
        return out

    fx = json.loads(FIX.read_text())
    tts = ElevenLabsTTS("fixture-only")
    tts.write(base64.b64decode(fx["audio_base64"]), fx["alignment"], clips / "fix.wav")
    prepared = {
        "A": (wav_from(VA / "eleven_rick_eleven_v4_turbo_A.mp3", "A"), LINE_A, None),
        "F": (clips / "fix.wav", FIX_TEXT, tts.last_alignment),
        "B": (wav_from(VA / "eleven_rick_eleven_v4_turbo_B.mp3", "B"), LINE_B, None),
    }
    if a.v5 or a.v6 or a.v7 or a.h:
        for key, (text, parts) in V5_CUTS.items():
            cut_clip(parts, clips / f"{key}.wav")
            prepared[key] = (clips / f"{key}.wav", text, None)
    if a.sync:   # 0.4 s beep / 0.6 s gap, mouth wide exactly on each beep
        import array, math, wave as wv
        pcm = array.array("h")
        for k in range(8):
            pcm.extend(int(12000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(int(0.4 * 48000)))
            pcm.extend([0] * int(0.6 * 48000))
        with wv.open(str(clips / "beep.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(pcm.tobytes())
        prepared["S"] = (clips / "beep.wav", "beep " * 8, None)
    seq = [0]

    def queue(key: str, kind: str, reply_to: dict | None = None) -> None:
        wav, text, al = prepared[key]
        seq[0] += 1
        stem = f"{int(time.time() * 1000):013d}{seq[0]:02d}"
        shutil.copy(wav, cfg.queue_dir / f"{stem}.wav")
        import wave
        with wave.open(str(wav)) as w:
            dur = w.getnframes() / w.getframerate()
        cues = ([[k * 1.0, lipsync.WIDE] for k in range(8)] + [[k * 1.0 + 0.4, lipsync.REST] for k in range(8)]
                if key == "S" else lipsync.plan(text, wav, al))
        meta = {"wav": f"{stem}.wav", "text": text, "kind": kind, "mood": "idle", "channel": "voice", "duration": dur,
                "queued_at": time.time(), "expires_at": time.time() + 90,
                "visemes": sorted(cues), **({"reply_to": reply_to} if reply_to else {})}
        tmp = cfg.queue_dir / f".{stem}.json"
        tmp.write_text(json.dumps(meta))
        tmp.replace(cfg.queue_dir / f"{stem}.json")

    game = FakeGameAdapter(seed=11, round_seconds=(0, 0))
    game.current = "max_win"

    def rnd(force: float | None = None) -> None:
        r = game.play_round(2.0).to_dict()
        if force is not None:   # a scripted bonus round, to show a reaction clip
            r.update(multiplier=force, payout=round(2.0 * force, 2), won=force > 1,
                     detail={"free_spins_triggered": True, "free_spins_count": 10})
            game.balance = round(game.balance + 2.0 * force, 2)
            r["balance"] = game.balance
        post_stage(cfg.stage_url, {"type": "round", "game": "Sweet Bonanza 1000", **r})

    def chat(name, text):
        post_stage(cfg.stage_url, {"type": "chat", "name": name, "text": text})
        return {"name": name, "text": text}

    def v5_round(game, mult, buy=False):
        detail = {"slot": game, "free_spins_triggered": buy}
        if buy:
            detail.update(bonus_buy=True, buy_cost_demo=200.0, feature_win_demo=round(200 * mult, 2), feature_multiplier=mult,
                          free_spins_count=15)
        game_bal[0] = round(game_bal[0] - (200 if buy else 2) + (200 if buy else 2) * mult, 2)
        post_stage(cfg.stage_url, {"type": "round", "game": game, "multiplier": mult, "won": mult > 1,
                                   "bet": 200.0 if buy else 2.0, "payout": round((200 if buy else 2) * mult, 2),
                                   "balance": game_bal[0], "is_demo": True, "detail": detail})
    game_bal = [99991.70]
    st = lambda state, game: post_stage(cfg.stage_url, {"type": "game_state", "state": state, "game": game})
    seg = lambda title, game: post_stage(cfg.stage_url, {"type": "segment", "kind": "play", "title": title, "game": game})
    GO, SB = "Gates of Olympus Super Scatter", "Sweet Bonanza Super Scatter"
    v5_timeline = [
        (0.2, lambda: seg("spinning slowly", GO)), (0.3, lambda: st("ready", GO)),
        (1.0, lambda: v5_round(GO, 0.0)), (1.6, lambda: queue("s_gone", "round")),
        (5.0, lambda: v5_round(GO, 0.45)), (5.6, lambda: queue("s_back", "round")),
        (9.5, lambda: v5_round(GO, 1.3)), (10.1, lambda: queue("s_win", "round")),
        (14.5, lambda: st("loading", SB)), (14.6, lambda: seg("slots, at sloth speed", SB)),
        (14.8, lambda: queue("switch", "segment")),
        (20.0, lambda: (shutil.copy(v5_bg["bonanza"], work / ".bg.png"), os.replace(work / ".bg.png", work / "bg.png"))),
        (27.0, lambda: queue("wait", "aside")),
        (31.0, lambda: st("ready", SB)),
        (32.5, lambda: v5_round(SB, 0.0)), (33.1, lambda: queue("s_damn", "round")),
        (37.0, lambda: v5_round(SB, 0.77, buy=True)), (37.6, lambda: queue("F", "round")),
        (47.5, lambda: v5_round(SB, 1.6)), (48.1, lambda: queue("s_win", "round")),
        (52.0, lambda: v5_round(SB, 0.3)), (52.6, lambda: queue("s_back", "round")),
    ]

    pacing_run = None
    if a.pacing:
        pacing_run = pacing_setup(cfg, clips, prepared, tts, fx, a.spin)
        a.seconds = 600.0
    timeline = [] if a.pacing else [
        (0.2, lambda: post_stage(cfg.stage_url, {"type": "segment", "kind": "slots", "title": "late night spins",
                                                 "game": "Sweet Bonanza 1000"})),
        (1.2, rnd), (2.6, rnd),
        (3.0, lambda: queue("A", "round")),
        (6.0, rnd), (9.5, rnd),
        (12.4, lambda: rnd(0.4)),           # dead bonus (0.4x): the loss reaction plays at the next loop start
        (16.0, rnd), (19.5, rnd),
        (22.6, lambda: queue("F", "chat", chat("tiltedtoby", "did the bonus pay or nah"))),
        (26.0, rnd), (29.5, rnd), (33.0, rnd),
    ]
    if a.v5:
        timeline = v5_timeline
    if a.v6:
        pacing_run = v6_setup(cfg, clips, prepared, 3.0, a.seconds)
        timeline = []
    spins_log = []
    if a.v7:
        from hubie.games.base import GameAdapter
        from hubie.util import ask_stage

        class Cue(GameAdapter):   # only the click hand-off of the real adapter interface is used
            name = "v7"
            def start(self): pass
            def list_games(self): return []
            def load_game(self, gid): pass
            def play_round(self, bet, options=None): pass
            def is_demo(self): return True
            def recover(self): pass
        cue = Cue()
        cue.click_cue = lambda why: (ask_stage(cfg.stage_url, {"type": "click", "why": why}) or {}).get("at")
        GO = "Gates of Olympus 1000"
        bal = [99991.70]
        mults = [0.0, 0.45, 1.3, 0.0, 0.77, 2.4, 0.0]
        lines = {0: "s_gone", 1: "s_back", 2: "s_win", 5: "s_win", 6: "s_damn"}

        def spin(k: int) -> None:
            def go():
                why = "buy" if k == V7_BUY else "spin"
                asked = time.time()
                at = cue.click_at(why)
                if at:
                    time.sleep(max(0.0, at - time.time()))
                reels.click()
                spins_log.append({"k": k, "why": why, "asked": asked - t0, "at": (at - t0) if at else None,
                                  "clicked": reels.clicks[-1] - t0, "hold": reels.clicks[-1] - asked})
                print(f"{reels.clicks[-1] - t0:6.2f}s  {why} sent, held {reels.clicks[-1] - asked:.2f} s", flush=True)
                time.sleep(2.4)
                m, buy = mults[k], k == V7_BUY
                cost = 200.0 if buy else 2.0
                bal[0] = round(bal[0] - cost + cost * m, 2)
                detail = {"slot": GO, "free_spins_triggered": buy}
                if buy:
                    detail.update(bonus_buy=True, buy_cost_demo=cost, free_spins_count=15)
                post_stage(cfg.stage_url, {"type": "round", "game": GO, "multiplier": m, "won": m > 1, "bet": cost,
                                           "payout": round(cost * m, 2), "balance": bal[0], "is_demo": True,
                                           "detail": detail})
                if k in lines:
                    time.sleep(0.4)
                    queue(lines[k], "round")
            threading.Thread(target=go, daemon=True).start()
        timeline = [(0.2, lambda: post_stage(cfg.stage_url, {"type": "segment", "kind": "play",
                                                               "title": "spinning slowly", "game": GO})),
                    (0.3, lambda: post_stage(cfg.stage_url, {"type": "game_state", "state": "ready", "game": GO}))]
        timeline += [(at, (lambda k=k: spin(k))) for k, at in enumerate(V7_SPINS)]
        timeline.append((V7_SPINS[3] - 0.9, lambda: queue("F", "line")))   # talking across the 4th click
        timeline.sort(key=lambda x: x[0])
    if a.h:
        from hubie.util import ask_stage
        RD, SB = "Hubie's Road", "Sweet Bonanza 1000"
        clicked = []
        click = lambda: clicked.append((round(time.time() - t0, 2),
                                        ask_stage(cfg.stage_url, {"type": "click", "why": "spin"})))
        hround = lambda m: post_stage(cfg.stage_url, {"type": "round", "game": RD, "multiplier": m, "won": m > 1,
                                                      "bet": 0.0, "payout": 0.0, "balance": 1000.0, "is_demo": True,
                                                      "detail": {}})
        swap = lambda: (shutil.copy(h_bg["slot"], work / ".bg.png"), os.replace(work / ".bg.png", work / "bg.png"))
        timeline = [
            (0.2, lambda: seg("hop hop", RD)), (0.3, lambda: st("ready", RD)),
            (0.6, lambda: chat("Kokax", "yo hubie")), (1.0, lambda: chat("Binlala", "go 10 mines")),
            (1.4, lambda: chat("mintyfresh", "road next pls")),
            (1.8, click), (2.4, lambda: hround(1.48)), (2.8, lambda: queue("A", "round")),
            (6.0, lambda: chat("deltaV", "that limbo hit was clean")),
            # a gap with no speech: the bubble keeps the line, dimmed
            (14.2, lambda: queue("s_damn", "round", chat("Ravsy", "hubie whats ur fav game"))),
            (15.0, click), (16.0, lambda: hround(8.04)), (16.3, lambda: hround(12.0)),
            (18.0, lambda: st("loading", SB)), (18.1, lambda: seg("slots, at sloth speed", SB)), (19.0, swap),
            (22.0, lambda: st("ready", SB)),
        ]
        h_clicks = clicked
        if a.react:
            react = lambda name: post_stage(cfg.stage_url, {"type": "react", "name": name})
            timeline = [(0.2, lambda: seg("hop hop", RD)), (0.3, lambda: st("ready", RD)),
                        (1.0, lambda: react("win")), (10.5, lambda: react("loss")), (21.5, lambda: react("surprise"))]
    if a.sync:
        def qlog():
            print(f"queued beep clip at {time.time() % 100:.3f}", flush=True)
            queue("S", "line")
        timeline = [(2.0, qlog), (12.0, qlog)]
    # wait for the encoder to produce frames before the clock starts
    deadline = time.time() + 20
    while time.time() < deadline and not cfg.probe.exists():
        time.sleep(0.2)
    time.sleep(1.0)
    t0 = time.time()
    rec_start = t0

    import psutil
    enc = psutil.Process(procs["encoder"].pid)
    samples = []

    def sampler():
        enc.cpu_percent()
        time.sleep(3)
        while time.time() - t0 < a.seconds - 1:
            time.sleep(1)
            if a.sync:
                import fcntl, struct, termios
                q = struct.unpack("i", fcntl.ioctl(keeper, termios.FIONREAD, b"\0\0\0\0"))[0]
                sz = fcntl.fcntl(keeper, 1032)
                print(f"fifo backlog {q} bytes of {sz}", flush=True)
            try:
                m = enc.memory_full_info()
                samples.append((enc.cpu_percent(), m.rss / 1e6, m.pss / 1e6))
            except psutil.Error:
                break
    threading.Thread(target=sampler, daemon=True).start()

    for at, fn in timeline:
        d = t0 + at - time.time()
        if d > 0:
            time.sleep(d)
        fn()
    if pacing_run:
        pacing_run()
        a.seconds = time.time() - t0 + 2.5
    time.sleep(max(0, t0 + a.seconds - time.time()))
    reel_stop.set()
    for name in ("mixer", "encoder", "stage"):
        procs[name].terminate()
    for p in procs.values():
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    os.close(keeper)
    segs = sorted((work / "rec").glob("*.ts"))
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segs))
    # the recording starts before the clock; keep the window the timeline ran in
    first_ts = None
    if segs:
        first_ts = time.mktime(time.strptime(segs[0].stem.split("_", 1)[1], "%Y%m%d_%H%M%S"))
    skip = max(0.0, rec_start - first_ts - 0.5) if first_ts else 0.0
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-ss", f"{skip:.2f}",
                    "-t", str(a.seconds), "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", a.out], check=True)
    if samples:
        cpu = sorted(s[0] for s in samples)
        print(f"encoder cpu % of one core: mean {sum(cpu) / len(cpu):.1f}  median {cpu[len(cpu) // 2]:.1f}  "
              f"max {cpu[-1]:.1f}; rss {max(s[1] for s in samples):.0f} MB  pss {max(s[2] for s in samples):.0f} MB")
    print("work dir", work, "skip", round(skip, 2))
    if a.v7:
        (work / "spins.json").write_text(json.dumps({"skip": skip, "spins": spins_log}, indent=1))
        print("spins:", json.dumps(spins_log), flush=True)


if __name__ == "__main__":
    main()
