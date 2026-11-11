"""Music bed: gain, ducking timing, mixing math and the never-stall fallbacks."""
from __future__ import annotations

import math
import random
import threading
import time

from pathlib import Path

import numpy as np
import pytest

from hubie.audio import music as M
from hubie.audio.music import Ducker, MusicBed, db_to_amp, mix_chunk

CHUNK_S = 0.04
CHUNK = int(M.RATE * M.FRAME * CHUNK_S)


def tone(n_bytes: int, value: int) -> bytes:
    return np.full(n_bytes // 2, value, dtype=np.int16).tobytes()


def level(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16)


def test_db_to_amp():
    assert db_to_amp(0) == pytest.approx(1.0)
    assert db_to_amp(-20) == pytest.approx(0.1)
    assert db_to_amp(-6.0206) == pytest.approx(0.5, rel=1e-4)


def test_ducker_idle_holds_base_gain():
    d = Ducker(-12, 9)
    for _ in range(50):
        a0, a1 = d.step(False, CHUNK_S)
    assert a0 == a1 == pytest.approx(db_to_amp(-12))


def test_ducker_attack_and_release_times():
    d = Ducker(-12, 9, attack_s=0.15, release_s=0.6)
    t = 0.0
    while d.d < 1 - math.exp(-1):          # one time constant = 63 %
        d.step(True, CHUNK_S)
        t += CHUNK_S
    assert 0.12 <= t <= 0.2
    for _ in range(100):                   # fully ducked: base gain minus the duck
        d.step(True, CHUNK_S)
    assert d.gain_db_now() == pytest.approx(-21)
    t = 0.0
    while d.d > math.exp(-1):
        d.step(False, CHUNK_S)
        t += CHUNK_S
    assert 0.55 <= t <= 0.68
    for _ in range(300):
        d.step(False, CHUNK_S)
    assert d.gain_db_now() == pytest.approx(-12)


def test_ducker_negative_duck_value_still_ducks_down():
    d = Ducker(-12, -9)
    for _ in range(100):
        d.step(True, CHUNK_S)
    assert d.gain_db_now() == pytest.approx(-21)


def test_ducker_is_continuous_between_chunks():
    d = Ducker(-12, 9)
    prev_end = None
    for i in range(40):
        a0, a1 = d.step(i < 20, CHUNK_S)
        if prev_end is not None:
            assert a0 == pytest.approx(prev_end)
        prev_end = a1


def test_mix_chunk_adds_scaled_music_and_keeps_length():
    voice = tone(CHUNK, 1000)
    music = tone(CHUNK, 8000)
    out = level(mix_chunk(voice, music, 0.25, 0.25))
    assert len(out) * 2 == len(voice)
    assert np.all(out == 3000)


def test_mix_chunk_ramps_across_the_chunk():
    out = level(mix_chunk(tone(CHUNK, 0), tone(CHUNK, 10000), 1.0, 0.0)).reshape(-1, 2)
    assert out[0, 0] == 10000 and out[0, 1] == 10000
    assert out[-1, 0] < 100
    assert np.all(np.diff(out[:, 0].astype(int)) <= 0)


def test_mix_chunk_clips_instead_of_wrapping():
    out = level(mix_chunk(tone(CHUNK, 30000), tone(CHUNK, 30000), 1.0, 1.0))
    assert np.all(out == 32767)
    out = level(mix_chunk(tone(CHUNK, -30000), tone(CHUNK, -30000), 1.0, 1.0))
    assert np.all(out == -32768)


def test_mix_chunk_short_music_pads_with_silence():
    out = level(mix_chunk(tone(CHUNK, 100), tone(CHUNK // 2, 1000), 1.0, 1.0))
    half = len(out) // 2
    assert np.all(out[:half] == 1100) and np.all(out[half:] == 100)


def fake_dir(tmp_path, names=("a.mp3", "b.mp3", "c.ogg")):
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    (tmp_path / "LICENSES.md").write_text("not a track")
    return tmp_path


def wait_for(cond, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_bed_mixes_buffered_music_at_base_gain(tmp_path):
    bed = MusicBed(fake_dir(tmp_path), gain_db=-20, duck_db=9,
                   decoder=lambda p: [tone(M.RATE * M.FRAME, 10000)] * 3).start()
    try:
        assert wait_for(lambda: bed.buffered_s() >= 1.0)
        out = level(bed.mix(tone(CHUNK, 0), speaking=False))
        assert np.all(np.abs(out - 1000) <= 1)       # 10000 * -20 dB
    finally:
        bed.stop()


def test_bed_ducks_while_speaking(tmp_path):
    bed = MusicBed(fake_dir(tmp_path), gain_db=-20, duck_db=10,
                   decoder=lambda p: [tone(M.RATE * M.FRAME, 10000)] * 5).start()
    try:
        assert wait_for(lambda: bed.buffered_s() >= 2.0)
        for _ in range(30):                          # 1.2 s of speech: long past the 150 ms attack
            out = bed.mix(tone(CHUNK, 0), speaking=True)
        assert np.all(np.abs(level(out) - 316) <= 2)  # 10000 * -30 dB
    finally:
        bed.stop()


def test_bed_without_music_returns_voice_unchanged(tmp_path):
    bed = MusicBed(fake_dir(tmp_path), decoder=lambda p: [])   # nothing decodes: never started anyway
    voice = tone(CHUNK, 1234)
    assert bed.mix(voice, speaking=True) == voice
    assert bed.underruns == 1


def test_bed_survives_a_decoder_that_raises(tmp_path):
    calls = []

    def bad(p):
        calls.append(p.name)
        raise RuntimeError("boom")
    bed = MusicBed(fake_dir(tmp_path), decoder=bad, retry_s=0.05).start()
    try:
        assert wait_for(lambda: len(calls) >= 6)       # keeps retrying, does not die
        voice = tone(CHUNK, 77)
        assert bed.mix(voice, speaking=False) == voice
        assert bed.errors >= 6
        assert bed._thread.is_alive()
    finally:
        bed.stop()


def test_bed_skips_one_bad_track(tmp_path):
    def dec(p):
        if p.name == "b.mp3":
            raise RuntimeError("corrupt")
        return [tone(M.RATE * M.FRAME, 500)] * 2
    bed = MusicBed(fake_dir(tmp_path), decoder=dec, buffer_s=60).start()
    try:
        assert wait_for(lambda: bed.tracks_played >= 4)
        assert bed.errors >= 1
    finally:
        bed.stop()


def test_mix_never_waits_on_a_stuck_decoder(tmp_path):
    gate = threading.Event()

    def stuck(p):
        gate.wait(5)
        return []
    bed = MusicBed(fake_dir(tmp_path), decoder=stuck).start()
    try:
        t0 = time.monotonic()
        for _ in range(25):
            bed.mix(tone(CHUNK, 0), speaking=False)
        assert time.monotonic() - t0 < 0.5
    finally:
        gate.set()
        bed.stop()


def test_shuffle_never_repeats_a_track_back_to_back(tmp_path):
    played = []

    def dec(p):
        played.append(p.name)
        return [tone(M.RATE * M.FRAME, 1)]
    bed = MusicBed(fake_dir(tmp_path), decoder=dec, buffer_s=600, rng=random.Random(3)).start()
    try:
        assert wait_for(lambda: len(played) >= 60)
    finally:
        bed.stop()
    assert set(played) == {"a.mp3", "b.mp3", "c.ogg"}
    assert all(x != y for x, y in zip(played, played[1:]))


def test_from_env(tmp_path):
    d = fake_dir(tmp_path)
    assert MusicBed.from_env({"HUBIE_MUSIC_DIR": ""}) is None             # empty = off
    assert MusicBed.from_env({"HUBIE_MUSIC_DIR": str(tmp_path / "nope")}) is None
    bed = MusicBed.from_env({"HUBIE_MUSIC_DIR": str(d), "HUBIE_MUSIC_GAIN_DB": "-15",
                             "HUBIE_MUSIC_DUCK_DB": "8"})
    assert bed.ducker.gain_db == -15 and bed.ducker.duck_db == 8
    bed = MusicBed.from_env({"HUBIE_MUSIC_DIR": str(d), "HUBIE_MUSIC_GAIN_DB": "loud"})
    assert bed.ducker.gain_db == M.DEFAULT_GAIN_DB


def test_real_ffmpeg_decoder_on_a_repo_track():
    tracks = M.list_tracks(M.DEFAULT_DIR)
    if not tracks:
        pytest.skip("no music in assets/music")
    it = M.ffmpeg_decoder(tracks[0])
    first = next(iter(it))
    it.close()
    assert len(first) == M.RATE * M.FRAME // 5


def test_mixer_turns_bed_off_on_failure(monkeypatch):
    from hubie.audio.mixer import Mixer

    class Boom:
        def mix(self, *a):
            raise ValueError("bad")

        def stop(self):
            pass
    m = Mixer.__new__(Mixer)
    m.music = Boom()
    voice = tone(CHUNK, 5)
    assert m._with_music(voice, True) == voice
    assert m.music is None
    assert m._with_music(voice, True) == voice


# -- packs ---------------------------------------------------------------------------------------------------
def make_packs(root, spec):
    """spec: {"house": ["h1.mp3", ...], ...} -> folders under root."""
    for pack, names in spec.items():
        d = root / pack
        d.mkdir()
        for n in names:
            (d / n).write_bytes(b"x")
        (d / "LICENSES.md").write_text("not a track")
    return root


def recorder(played, value=1, blocks=1):
    def dec(p):
        played.append(f"{p.parent.name}/{p.name}")
        return [tone(M.RATE * M.FRAME, value)] * blocks
    return dec


def test_default_level_is_louder_and_default_pack_is_house():
    assert M.DEFAULT_GAIN_DB == -8.0 and M.DEFAULT_DUCK_DB == 9.0
    assert M.DEFAULT_DIR == M.MUSIC_ROOT / "house"


def test_resolve_packs_names_paths_and_empty_folders(tmp_path):
    make_packs(tmp_path, {"lofi": ["a.mp3"], "night": ["b.mp3"], "empty": []})
    assert M.resolve_packs("lofi, night", tmp_path) == [tmp_path / "lofi", tmp_path / "night"]
    assert M.resolve_packs("empty,nope,,lofi,lofi", tmp_path) == [tmp_path / "lofi"]
    assert M.resolve_packs(str(tmp_path / "night"), Path("/nowhere")) == [tmp_path / "night"]
    assert M.resolve_packs("", tmp_path) == []


def test_packs_rotate_every_n_tracks_and_each_pack_plays_all_before_repeating(tmp_path):
    make_packs(tmp_path, {"a": ["1.mp3", "2.mp3", "3.mp3"], "b": ["4.mp3", "5.mp3", "6.mp3", "7.mp3"]})
    played = []
    bed = MusicBed(tmp_path / "a", packs=[tmp_path / "a", tmp_path / "b"], pack_tracks=2,
                   decoder=recorder(played), buffer_s=600, rng=random.Random(1)).start()
    try:
        assert wait_for(lambda: len(played) >= 24)
    finally:
        bed.stop()
    packs = [x.split("/")[0] for x in played[:24]]
    assert packs == ["a", "a", "b", "b"] * 6
    a = [x for x in played if x.startswith("a/")]
    b = [x for x in played if x.startswith("b/")]
    assert set(a[:3]) == {"a/1.mp3", "a/2.mp3", "a/3.mp3"}          # a full pass before any repeat
    assert set(b[:4]) == {"b/4.mp3", "b/5.mp3", "b/6.mp3", "b/7.mp3"}
    assert all(x != y for x, y in zip(played, played[1:]))


def test_from_env_packs(tmp_path):
    make_packs(tmp_path, {"lofi": ["a.mp3"], "night": ["b.mp3"], "house": ["c.mp3"]})
    env = {"HUBIE_MUSIC_ROOT": str(tmp_path), "HUBIE_MUSIC_PACKS": "night,lofi", "HUBIE_MUSIC_PACK_TRACKS": "3"}
    bed = MusicBed.from_env(env)
    assert bed.packs == [tmp_path / "night", tmp_path / "lofi"] and bed.dir == tmp_path / "night"
    assert bed.pack_tracks == 3
    assert bed.ducker.gain_db == -8.0
    # unknown pack names fall back to HUBIE_MUSIC_DIR
    bed = MusicBed.from_env({"HUBIE_MUSIC_ROOT": str(tmp_path), "HUBIE_MUSIC_PACKS": "jazz",
                             "HUBIE_MUSIC_DIR": str(tmp_path / "lofi")})
    assert bed.packs == [tmp_path / "lofi"]
    assert MusicBed.from_env({"HUBIE_MUSIC_ROOT": str(tmp_path), "HUBIE_MUSIC_PACKS": "jazz",
                              "HUBIE_MUSIC_DIR": str(tmp_path / "nope")}) is None
    assert MusicBed.from_env({"HUBIE_MUSIC_DIR": "", "HUBIE_MUSIC_PACKS": "lofi"}) is None   # off wins
    bed = MusicBed.from_env({"HUBIE_MUSIC_DIR": str(tmp_path / "house"), "HUBIE_MUSIC_PACK_FILE": "/x/pack"})
    assert bed.pack_file == Path("/x/pack") and bed.packs == [tmp_path / "house"]


def test_pack_file_switches_between_tracks_and_bad_content_falls_back(tmp_path):
    make_packs(tmp_path, {"house": ["h1.mp3", "h2.mp3"], "lofi": ["l1.mp3", "l2.mp3"]})
    sel = tmp_path / "pack.txt"
    played = []
    gate = threading.Semaphore(0)

    def dec(p):
        gate.acquire(timeout=3)
        played.append(f"{p.parent.name}/{p.name}")
        return [tone(M.RATE * M.FRAME, 1)]
    bed = MusicBed(tmp_path / "house", decoder=dec, buffer_s=600, pack_file=sel, music_root=tmp_path,
                   rng=random.Random(2)).start()
    try:
        gate.release(3)
        assert wait_for(lambda: len(played) >= 3)
        assert all(x.startswith("house/") for x in played)
        sel.write_text("lofi\n")
        gate.release(4)
        assert wait_for(lambda: len(played) >= 7)
        assert all(x.startswith("lofi/") for x in played[4:7])        # the one in flight may still be house
        sel.write_text("no-such-pack")                                 # unknown: back to the env choice
        n = len(played)
        gate.release(4)
        assert wait_for(lambda: len(played) >= n + 4)
        assert all(x.startswith("house/") for x in played[n + 1:n + 4])
        assert bed.status()["music_pack"] == "house"
    finally:
        gate.release(100)
        bed.stop()


def test_pack_switch_fades_out_the_current_track_early(tmp_path):
    make_packs(tmp_path, {"house": ["h1.mp3"], "lofi": ["l1.mp3"]})
    sel = tmp_path / "pack.txt"
    sel.write_text("house")
    started = []

    def dec(p):
        started.append(p.parent.name)
        for _ in range(600):                        # a 2-minute "track" in 0.2 s blocks, ~4 s to decode
            time.sleep(0.006)
            yield tone(M.RATE * M.FRAME // 5, 10000)
    bed = MusicBed(tmp_path / "house", decoder=dec, buffer_s=600, pack_file=sel, music_root=tmp_path).start()
    try:
        assert wait_for(lambda: bed.buffered_s() >= 5)
        bed._switch_checked = 0.0
        sel.write_text("lofi")
        def snapshot():
            with bed._lock:
                return level(bytes(bed._buf)).reshape(-1, 2)[:, 0]

        def lofi_audio_arrived():
            pcm = snapshot()
            return "lofi" in started and len(pcm) and (pcm == 0).any() and pcm[-1] == 10000
        assert wait_for(lofi_audio_arrived, timeout=10)
        pcm = snapshot()
        # house: full level, then a ~1.5 s fade down to silence, then lofi starts again at full level
        zero = np.flatnonzero(pcm == 0)
        assert len(zero) and zero[0] > 0
        fade = pcm[zero[0] - int(1.4 * M.RATE):zero[0]]
        assert fade[0] > 9000 and np.all(np.diff(fade.astype(int)) <= 0)
        assert pcm[zero[-1] + 1] == 10000
        assert zero[0] < 30 * M.RATE                 # the house track did not run to its end
    finally:
        bed.stop()
