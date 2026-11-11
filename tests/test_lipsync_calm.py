"""Calm mouth rules (2026-11-08 audit, fixes A and B): min hold, changes-per-second cap, no A-B-A flutter,
smooth loudness gate; and each line's mouth timed from the mixer's real audio start."""
import asyncio
import json
import random
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hubie.audio import lipsync as L   # noqa: E402

FPS = L.FPS


def frames(trk, n):
    out, j = [], 0
    for i in range(n):
        while j + 1 < len(trk) and trk[j + 1][0] <= i / FPS + 0.5 / FPS:
            j += 1
        out.append(trk[j][1])
    return out


def runs(shape):
    out, i = [], 0
    while i < len(shape):
        j = i
        while j < len(shape) and shape[j] == shape[i]:
            j += 1
        out.append((i, j - i, shape[i]))
        i = j
    return out


def chatter(seed, seconds=6.0):
    """Fast speech with loudness jumping every frame or two and a new sound every 40-90 ms: the worst case."""
    rnd = random.Random(seed)
    n = int(seconds * FPS)
    level = [0.0] * 6 + [rnd.choice((0.05, 0.2, 0.5, 0.9, 1.0)) for _ in range(n - 12)] + [0.0] * 6
    hints, t = [], 0.2
    while t < seconds - 0.3:
        d = rnd.uniform(0.04, 0.09)
        hints.append((t, t + d, rnd.choice(("a", "o", "e", "f", "m", "c", "c", None))))
        t += d
    return hints, level


def tracks():
    for seed in range(12):
        hints, level = chatter(seed)
        yield level, frames(L.track(hints, level), len(level))
    yield from (_fixture(),)


def _fixture():
    sys.path.insert(0, str(ROOT / "tests"))
    from test_desk_scene import _fixture_track
    return _fixture_track()


def test_every_shape_holds_two_frames_inbetweens_included():
    for level, shape in tracks():
        rs = runs(shape)
        for k, (i, n, s) in enumerate(rs):
            if s == L.REST and k in (0, len(rs) - 1):
                continue
            assert n >= L.HOLD, f"{L.NAMES[s]} for {n} frame(s) at frame {i}"


def test_changes_per_second_capped():
    worst = 0
    for level, shape in tracks():
        ch = [i for i in range(1, len(shape)) if shape[i] != shape[i - 1]]
        for a in ch:
            busy = sum(1 for c in ch if a <= c < a + FPS)
            # one over the cap only for a closed <-> open in-between, which never snaps
            assert busy <= L.MAX_RATE_ALL + 1, f"{busy} changes in the second from frame {a}"
            worst = max(worst, busy)
        assert len(ch) / (len(shape) / FPS) <= L.MAX_RATE_ALL
    assert worst >= 5   # the chatter really is fast; the cap is what holds it


def test_no_aba_bounce_within_three_frames():
    for level, shape in tracks():
        rs = runs(shape)
        for k in range(1, len(rs) - 1):
            if rs[k - 1][2] == rs[k + 1][2]:
                assert rs[k][1] >= L.BOUNCE, f"{L.NAMES[rs[k - 1][2]]}-{L.NAMES[rs[k][2]]}-back at frame {rs[k][0]}"


def test_short_consonants_merge_into_their_vowel():
    # "ah-t-ah": a one-frame consonant between two vowels of one word disappears into the vowel
    hints = [(0.2, 0.4, "a"), (0.4, 0.433, "c"), (0.433, 0.7, "a")]
    level = [0.0] * 6 + [0.9] * 15 + [0.0] * 9
    shape = frames(L.track(hints, level), len(level))
    assert shape[11] == shape[12] == shape[13] != L.REST    # the t is not a shape of its own


def test_gate_opens_on_the_frame_and_closes_over_two_or_three():
    level = [0.0] * 10 + [0.8] * 20 + [0.0] * 15
    shape = frames(L.track([(10 / FPS, 30 / FPS, "a")], level), len(level))
    first = next(i for i, s in enumerate(shape) if s != L.REST)
    assert 7 <= first <= 10                                  # one frame of lead, at most two more for an in-between
    last = max(i for i, s in enumerate(shape) if s != L.REST)
    assert 29 <= last <= 32                                  # closing takes two or three frames past the voice
    tail = shape[27:last + 1]
    assert len(set(tail)) >= 2                               # through a smaller shape, not one snap shut


def test_a_dip_inside_a_word_does_not_snap_shut():
    level = [0.0] * 5 + [0.8] * 8 + [0.04] + [0.8] * 8 + [0.0] * 8
    shape = frames(L.track([(5 / FPS, 22 / FPS, "a")], level), len(level))
    assert shape[13] != L.REST


# ---- fix B: the line's clock is the mixer's audio start ----

def test_audio_start_maps_mixer_wall_clock_to_monotonic():
    from hubie.stage.desk import audio_start
    assert audio_start({"audio_t0": 1000.25}, now_wall=lambda: 1000.0, now_mono=lambda: 50.0) == 50.25
    assert audio_start({"audio_t0": 990.0}, now_wall=lambda: 1000.0, now_mono=lambda: 50.0) is None   # stale
    assert audio_start({}) is None and audio_start({"audio_t0": "x"}) is None


def test_mixer_stamps_audio_t0_with_fifo_backlog(tmp_path, monkeypatch):
    import os
    from hubie.audio import mixer as mx
    sent = []
    monkeypatch.setattr(mx, "post_stage", lambda url, ev: sent.append(ev))
    q = tmp_path / "q"
    q.mkdir()
    with wave.open(str(q / "001.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(b"\x01\x00" * 4800)
    (q / "001.json").write_text(json.dumps({"wav": "001.wav", "text": "hi", "duration": 0.1, "expires_at": 9e12}))
    r, wfd = os.pipe()
    os.write(wfd, b"\0" * int(mx.BYTES_PER_SEC * 0.1))   # 100 ms already waiting for the encoder
    m = mx.Mixer.__new__(mx.Mixer)
    m.cfg, m.queue_dir, m.gap_until, m.skipped, m.voice, m.fd, m.leftover = (
        SimpleNamespace(stage_url="http://127.0.0.1:1"), q, 0.0, 0, None, wfd, b"")
    t = time.time()
    m._next_clip()
    for _ in range(50):
        if sent:
            break
        time.sleep(0.01)
    os.close(r); os.close(wfd)
    assert sent and sent[0]["type"] == "speak"
    assert 0.09 <= sent[0]["audio_t0"] - t <= 0.15


class _FakeDesk:
    """Just enough of DeskScene for _mouth."""
    def __init__(self, offset_ms=0.0):
        from hubie.stage.desk import DeskScene
        self._mouth = DeskScene._mouth.__get__(self)
        self.cfg = SimpleNamespace(lipsync_offset_ms=offset_ms)
        self.cur_mouth = L.REST
        self.log = []

    def set_mouth(self, k):
        self.cur_mouth = k
        self.log.append((time.monotonic(), k))


def test_mouth_runs_from_the_audio_start_not_the_arrival():
    async def run():
        d = _FakeDesk()
        start = time.monotonic() + 0.15          # audio goes out 150 ms after the event arrives
        await d._mouth([[0.0, L.REST], [0.1, L.HALF], [0.2, L.REST]], start)
        t_half = next(t for t, k in d.log if k == L.HALF)
        assert abs(t_half - (start + 0.1)) < 0.03
    asyncio.run(run())


def test_late_line_eases_open_and_skips_what_is_past():
    async def run():
        d = _FakeDesk()
        start = time.monotonic() - 0.2           # the event came 200 ms after the audio started
        cues = [[0.0, L.REST], [0.05, L.SLIGHT], [0.1, L.HALF], [0.15, L.WIDE], [0.5, L.OPEN], [0.6, L.REST]]
        await d._mouth(cues, start)
        shapes = [k for _, k in d.log]
        assert L.SLIGHT not in shapes and L.HALF in shapes[:1]    # past cues are not flashed through
        assert shapes[0] in L.between(L.REST, L.WIDE)            # eased open through an in-between ...
        assert shapes[1] == L.WIDE                               # ... then the shape that is due
        gap = d.log[1][0] - d.log[0][0]
        assert gap >= L.HOLD / FPS - 0.01                        # held two frames
        assert L.OPEN in shapes and shapes[-1] == L.REST
    asyncio.run(run())


def test_on_time_line_has_no_ease_in():
    async def run():
        d = _FakeDesk()
        await d._mouth([[0.0, L.REST], [0.05, L.WIDE], [0.15, L.REST]], time.monotonic())
        assert [k for _, k in d.log][:2] == [L.REST, L.WIDE]
    asyncio.run(run())


def test_late_line_eases_in_even_when_skipped_cue_just_came_due():
    # 2026-11-09 live bug: the event was ~0.3 s late, the skipped-to cue had only just come due, so no ease-in
    # and the mouth snapped closed -> open. Lateness is now measured from the line's first open cue.
    async def run():
        d = _FakeDesk()
        start = time.monotonic() - 0.301                 # cue at 0.3 s came due ~1 ms ago, the line's opening 0.25 s ago
        cues = [[0.0, L.REST], [0.05, L.OPEN], [0.3, L.WIDE], [0.7, L.REST]]
        await d._mouth(cues, start)
        shapes = [k for _, k in d.log]
        assert shapes[0] in L.between(L.REST, L.WIDE) and shapes[1] == L.WIDE
    asyncio.run(run())
