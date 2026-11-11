"""Desk layout: lip-sync planning, the ElevenLabs timestamps adapter, reactions, subtitles and the encoder
command. No network: the voice adapter runs on a saved response."""
import asyncio
import base64
import json
import time
import wave
from pathlib import Path

import pytest

from hubie.audio import lipsync as L
from hubie.config import ROOT, Config

FIX = ROOT / "tests/fixtures/eleven/rick_with_timestamps.json"


def fixture():
    return json.loads(FIX.read_text())


def shape_at(cues, t):
    cur = cues[0][1]
    for ct, s in cues:
        if ct <= t:
            cur = s
    return cur


# ---- lip-sync planning ----

def test_alignment_cues_close_on_silence_and_mbp_and_open_on_vowels():
    al = fixture()["alignment"]
    chars, st, en = al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]
    cues = L.from_alignment(chars, st, en)
    times = [t for t, _ in cues]
    assert times == sorted(times) and cues[0] == [0.0, L.REST] and cues[-1][1] == L.REST
    assert all(b != a for (_, a), (_, b) in zip(cues, cues[1:]))      # no repeats
    text = "".join(chars)
    # "mm." at the start keeps the lips shut
    assert shape_at(cues, (st[0] + en[1]) / 2) == L.REST
    # the b of "bonus" is closed, its o is round
    i = text.index("bonus")
    assert shape_at(cues, (st[i] + en[i]) / 2) == L.REST
    assert shape_at(cues, (st[i + 1] + en[i + 1]) / 2) == L.ROUND
    # the mouth opens somewhere in every long word and plenty of vowels show
    shapes = {s for _, s in cues}
    assert {L.ROUND, L.WIDE, L.OPEN} <= shapes
    # nothing but a vowel holds for less than two frames
    for (t, s), (t2, _) in zip(cues, cues[1:]):
        if s not in L.VOWELS and s != L.REST:
            assert t2 - t >= L.MIN_HOLD - 1e-6


def test_quiet_audio_softens_vowels_and_mismatched_alignment_is_safe():
    cues = L.from_alignment(list("ba"), [0.0, 0.1], [0.1, 0.4], env=[0.0] * 10)
    assert {s for _, s in cues} == {L.REST}
    assert L.from_alignment(list("ab"), [0.0], [0.1, 0.2]) == [[0.0, L.REST]]


def test_text_and_env_fallback_follows_voiced_parts():
    env = [0.0] * 4 + [0.8] * 10 + [0.0] * 6 + [0.7] * 8 + [0.0] * 2
    cues = L.from_text_and_env("hello there. no way", env)
    assert shape_at(cues, 0.1) == L.REST                  # before speech
    assert shape_at(cues, 0.85) == L.REST                 # the pause
    assert any(s in L.VOWELS for _, s in cues)
    assert cues[-1][1] == L.REST
    assert L.from_text_and_env("", [0.0] * 5) == [[0.0, L.REST]]


def test_loudness_only_planner_and_plan_never_raises(tmp_path):
    cues = L.from_env([0.0, 0.2, 0.2, 0.5, 0.9, 0.9, 0.0])
    assert [s for _, s in cues][:2] == [L.REST, L.SLIGHT] and L.WIDE in {s for _, s in cues}
    assert L.plan("hi", tmp_path / "missing.wav") == [[0.0, L.REST]]
    w = tmp_path / "quiet.wav"
    with wave.open(str(w), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(24000); f.writeframes(b"\0\0" * 24000)
    assert L.plan("hi there", w) == [[0.0, L.REST]]                 # silence never opens the mouth


def _fixture_track(lead=0.6, seconds=10.0):
    """The Rick fixture laid out like tools/lipsync/closeup.py: lead-in silence, the clip, silence."""
    import numpy as np
    fx = fixture()
    pcm = np.frombuffer(base64.b64decode(fx["audio_base64"]), np.int16).astype(np.float32)
    pcm = np.concatenate([np.zeros(int(lead * 24000)), pcm, np.zeros(int(seconds * 24000))])[:int(seconds * 24000)]
    step = 24000 // L.FPS
    rms = np.array([np.sqrt((pcm[i:i + step] ** 2).mean()) for i in range(0, len(pcm), step)])
    voiced = np.sort(rms[rms > 0.02 * rms.max()])
    level = list(np.minimum(1.0, rms / voiced[int(len(voiced) * 0.9)]))
    al = fx["alignment"]
    hints = L.hints_from_alignment(al["characters"], [t + lead for t in al["character_start_times_seconds"]],
                                   [t + lead for t in al["character_end_times_seconds"]])
    trk = L.track(hints, level)
    per_frame, j = [], 0
    for i in range(len(level)):
        while j + 1 < len(trk) and trk[j + 1][0] <= i / L.FPS + 0.5 / L.FPS:
            j += 1
        per_frame.append(trk[j][1])
    return level, per_frame


def test_frame_track_follows_loudness():
    """The owner's audit rules, frame by frame on the Rick fixture: never closed while loud except a short m/b/p
    closure, never open on silence two frames after the voice stops, no closed -> wide snaps, no long holds."""
    level, shape = _fixture_track()
    n = len(level)
    run = 0
    for i in range(n):   # loud and closed
        run = run + 1 if (level[i] >= 0.25 and shape[i] == L.REST) else 0
        assert run <= L.CLOSE_FRAMES + 1, f"closed while loud at {i / L.FPS:.2f}s"
    for i in range(n):   # open on silence (two frames of lead before the voice are allowed, an opening in-between)
        if shape[i] != L.REST and max(level[max(0, i - 2):i + 3]) < 0.03:
            raise AssertionError(f"open on silence at {i / L.FPS:.2f}s")
    jumps = [i for i in range(1, n) if L.between(shape[i - 1], shape[i])]   # a change that wants an in-between
    for i in jumps:   # no snaps closed <-> open or wider; a skipped in-between (no room for two frames) is rare
        assert abs(L.RANK[shape[i]] - L.RANK[shape[i - 1]]) <= 2.5, f"snap at {i / L.FPS:.2f}s"
    assert len(jumps) <= 3, [round(i / L.FPS, 2) for i in jumps]
    changes = [i for i in range(1, n) if shape[i] != shape[i - 1]]
    i = 0
    while i < n:   # held shapes: a long hold gets a jaw move, unless that would break the changes-per-second cap
        j = i
        while j < n and shape[j] == shape[i]:
            j += 1
        if shape[i] != L.REST and shape[i] in L.NEIGHBOUR and j - i > L.MAX_HOLD + 2:
            busy = max(sum(1 for c in changes if a <= c < a + L.FPS) for a in range(i - L.FPS + 1, j))
            assert busy + 2 > L.MAX_RATE_ALL, f"{L.NAMES[shape[i]]} held {j - i} frames at {i / L.FPS:.2f}s"
        i = j
    # the first word ("mm.", 0.6-1.0 s, loud) moves the mouth; the loudest stretch ("oh well") gets the big shapes
    assert any(s != L.REST for s in shape[int(0.65 * L.FPS):int(1.0 * L.FPS)])
    assert {L.WIDE, L.ROUND} & set(shape[int(4.7 * L.FPS):int(5.0 * L.FPS)])
    import numpy as np
    rank = np.array([L.RANK[s] for s in shape])
    lv = np.array(level)
    assert np.corrcoef(rank[lv >= 0.03], lv[lv >= 0.03])[0, 1] > 0.5   # wider when louder


def test_track_from_env_and_plan_fallbacks(tmp_path):
    trk = L.track_from_env("hello there. no way", [0.0] * 4 + [0.8] * 10 + [0.0] * 6 + [0.7] * 8 + [0.0] * 2)
    assert trk[0] == [0.0, L.REST] and trk[-1][1] == L.REST and any(s != L.REST for _, s in trk)
    assert L.track_from_env("", []) == [[0.0, L.REST]]
    assert L.track(None, []) == [[0.0, L.REST]]


# ---- voice adapter with timestamps ----

def test_elevenlabs_adapter_writes_48k_wav_and_keeps_alignment(tmp_path, monkeypatch):
    import httpx
    from hubie.tts.eleven import ElevenLabsTTS, SilentTTS
    fx = fixture()
    calls = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return fx

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        return Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    tts = ElevenLabsTTS("k-test")
    dur = tts.synthesize("mm. big bonus", tmp_path / "a.wav")
    assert "/with-timestamps" in calls[0][0] and calls[0][2]["xi-api-key"] == "k-test"
    assert abs(dur - len(base64.b64decode(fx["audio_base64"])) / 2 / 24000) < 0.01
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert w.getframerate() == 48000 and w.getnchannels() == 1
    assert tts.last_alignment["characters"][:2] == ["m", "m"]
    assert SilentTTS().synthesize("one two three", tmp_path / "s.wav") >= 0.8
    with pytest.raises(ValueError):
        ElevenLabsTTS("")


def test_chain_passes_alignment_and_speaker_stores_visemes_and_reply(tmp_path):
    from hubie.audio.queue import Speaker
    from hubie.safety.output_filter import OutputFilter
    from hubie.tts.base import ChainTTS
    from hubie.tts.eleven import ElevenLabsTTS
    fx = fixture()

    class Canned(ElevenLabsTTS):
        def synthesize(self, text, out_wav):
            return self.write(base64.b64decode(fx["audio_base64"]), fx["alignment"], out_wav)

    sp = Speaker(ChainTTS([Canned("x")]), tmp_path)
    line = OutputFilter().approve("slow and steady, bruh.")
    assert sp.say(line, kind="chat", reply_to={"name": "toby", "text": "hi"})
    meta = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert meta["reply_to"] == {"name": "toby", "text": "hi"}
    assert meta["visemes"][0] == [0.0, L.REST] and len(meta["visemes"]) > 10


# ---- desk scene ----

def _cfg(tmp_path, monkeypatch, **env):
    for k, v in {"HUBIE_RUN_DIR": str(tmp_path / "run"), "HUBIE_DATA_DIR": str(ROOT / "data"),
                 "HUBIE_SCENE": "ffmpeg", "HUBIE_SCENE_BG": "image", "HUBIE_MOUTH_PORT": "18961",
                 "HUBIE_OUTPUT": f"file:{tmp_path}/rec", **env}.items():
        monkeypatch.setenv(k, v)
    cfg = Config()
    cfg.ensure_dirs()
    return cfg


def test_layout_boxes_fit_and_do_not_overlap():
    from hubie.stage.desk import Geo
    for w, h in ((1280, 720), (1920, 1080)):
        g = Geo(w, h)
        gx, gy, gw, gh = g.game
        cx, cy, cw, ch = g.cam
        assert gx + gw <= cx and cy <= g.s(10)                       # cam beside the game, top of the column
        assert 0.27 <= cw / w <= 0.35 and 0.66 <= gw / w <= 0.76
        px, py, pw, ph = g.plate
        assert py >= gh and py + ph <= h - g.strip_h and px + pw <= gw
        bx, by, bw, bh = g.chat
        assert by >= cy + ch and bx >= gw and by + bh <= g.panel[1]


def test_subtitles_are_two_lines_at_most_and_slots_are_filled_right():
    from hubie.stage.desk import Geo, pages, sub_lines
    g = Geo(1280, 720)
    long = "it's three am and the lobby's gone quiet. top shelf banana's still up there. " * 3
    pg = pages(g, long)
    assert len(pg) > 1 and all(1 <= len(p.split("\n")) <= 2 for p in pg)
    assert sub_lines("one line") == {"sub_top": "", "sub_bot": "", "sub_mid": "one line"}
    assert sub_lines("a\nb") == {"sub_top": "a", "sub_bot": "b", "sub_mid": ""}


def test_reaction_triggers():
    from hubie.stage.desk import reaction_for
    fs = {"free_spins_triggered": True}
    assert reaction_for({"multiplier": 12}, 0) == "win"
    assert reaction_for({"multiplier": 4, "detail": {}}, 3.5) == "win"        # new session best
    assert reaction_for({"multiplier": 4, "detail": {}}, 0) is None
    assert reaction_for({"multiplier": 0.4, "detail": fs}, 50) == "loss"      # dead bonus
    assert reaction_for({"multiplier": 2.0, "detail": fs}, 50) == "surprise"
    assert reaction_for({"multiplier": 0.0}, 5) is None
    assert reaction_for({"multiplier": "x"}, 5) is None


needs_art = pytest.mark.skipif(not (ROOT / f"assets/cam_visemes/mouth_{len(L.NAMES) - 1}.mp4").exists(),
                               reason="mouth set not built (tools/lipsync/build_visemes.py)")


@needs_art
def test_desk_cmd_one_clock_lipsync_select_and_audio_last(tmp_path, monkeypatch):
    from hubie.pipeline.commands import scene_cmd
    cfg = _cfg(tmp_path, monkeypatch)
    cmd = scene_cmd(cfg)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-re") == 1 and cmd[cmd.index("-re") + 1:cmd.index("-re") + 3] == ["-f", "lavfi"]
    n_inputs = cmd.count("-i")
    assert cmd[cmd.index(str(cfg.fifo)) - 1] == "-i" and cmd.index(str(cfg.fifo)) == len(
        cmd) - 1 - cmd[::-1].index(str(cfg.fifo))
    assert f"[{n_inputs - 1}:a]" in fc                      # the FIFO is the last input
    assert "-probesize" in cmd and f"streamselect@mouth=inputs={len(L.NAMES) + 4}" in fc
    assert "zmq=bind_address='tcp\\://127.0.0.1\\:18961'" in fc
    assert "overlay@plate" in fc and "expansion=none" in fc and "strip.txt" in fc and "x11grab" not in fc
    assert "s=1280x720:r=30" in " ".join(cmd) and "-g" in cmd


@needs_art
def test_broken_reaction_clip_is_left_out(tmp_path, monkeypatch):
    from hubie.stage.desk import reaction_paths
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_REACT_WIN=str(bad), HUBIE_REACT_BANANA=str(tmp_path / "missing.mp4"))
    got = reaction_paths(cfg)
    assert "win" not in got and "banana" not in got and "loss" in got
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_CAM_MODE="switch")
    assert reaction_paths(cfg) == {}


@needs_art
def test_reactions_wait_for_loop_start_skip_during_speech_and_cool_down(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    from hubie.util import write_status
    cfg = _cfg(tmp_path, monkeypatch)
    sent = []

    async def run():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        s.loop_s = 0.6
        assert not s.react("win")                                    # no encoder clock yet
        write_status(cfg.status_dir, "encoder_clock", frame=0, fps=30)
        assert s.loop_phase() is not None
        s.speaking = True
        assert not s.react("win")                                    # never starts during speech
        s.speaking = False
        assert s.react("win")
        assert not s.react("loss")                                   # one at a time
        await asyncio.sleep(0.7)
        assert sent and sent[-1][1] == f"streamselect@mouth map {s.reactions['win']}"
        s.set_mouth(3)                                               # speech during a reaction: no mouth
        assert sent[-1][1] == f"streamselect@mouth map {s.reactions['win']}"
        await asyncio.sleep(0.7)
        assert sent[-1][1] == "streamselect@mouth map 3"             # lip-sync resumes after
        assert not s.react("loss")                                   # cooldown
        s.last_react -= 61
        s.speaking = False
        assert s.react("loss")
        s.speaking = True                                            # talking starts while it waits
        await asyncio.sleep(0.7)
        assert all(c != f"streamselect@mouth map {s.reactions['loss']}" for _, c in sent)
        assert sent[-1][1] == "streamselect@mouth map 3"

    asyncio.run(run())


@needs_art
def test_speak_drives_subtitles_plate_chat_row_and_mouth(tmp_path, monkeypatch):
    from hubie.stage.desk import DeskScene
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_LIPSYNC_OFFSET_MS="0")
    sent = []

    async def run():
        s = DeskScene(cfg)
        s.link.send = lambda key, cmd: sent.append((key, cmd))
        s.SUB_HOLD = 0.05
        chat0 = (cfg.scene_dir / "chat.png").read_bytes()
        s.apply({"type": "speak", "text": "100% %{hack} slow and steady", "env": [0.5] * 6,
                 "visemes": [[0.0, 3], [0.15, 0]], "reply_to": {"names": ["toby"]}})
        await asyncio.sleep(0.08)
        assert ("plate", "overlay@plate enable 1") in sent
        assert "%{hack}" in (cfg.scene_dir / "sub_mid.txt").read_text()
        assert ("cam", "streamselect@mouth map 3") in sent
        assert (cfg.scene_dir / "chat.png").read_bytes() != chat0     # his reply went into the chat list
        await asyncio.sleep(0.6)
        assert sent[-1] == ("plate", "overlay@plate enable 0") or ("plate", "overlay@plate enable 0") in sent
        assert ("cam", "streamselect@mouth map 0") in sent
        assert (cfg.scene_dir / "sub_mid.txt").read_text() == " "
        assert not s.speaking
        s.apply({"type": "round", "game": "Sweet Bonanza 1000", "multiplier": 2.5, "balance": 990.5})
        st = json.loads((cfg.scene_dir / "session.json").read_text())
        assert st["spins"] == 1 and st["best"] == 2.5 and st["balance"] == 990.5

    asyncio.run(run())


def test_orchestrator_marks_chat_replies_for_the_chat_list():
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from test_orchestrator import build
    from hubie.orchestrator import QueuedChat
    o, sp, _ = build()
    o.answer(QueuedChat("amy", "amy_1", "what game is next", time.time(), "pass"))
    assert sp.replies[-1] == {"names": ["amy"]}
