import os
import socket
import subprocess
import threading

import pytest

from hubie import checks
from hubie.config import Config
from hubie.pipeline.commands import browser_cmd, encoder_cmd
from hubie.pipeline.output_guard import GO_LIVE_ENV, GO_LIVE_PHRASE, OutputRefused, check_browser_url, check_output
from hubie.supervisor import Part, Supervisor


def test_output_guard_allows_local_only():
    assert check_output("file:/tmp/x", {})[0] == "file"
    assert check_output("rtmp://127.0.0.1:1935/live/test", {})[0] == "rtmp"
    assert check_output("rtmp://localhost/live", {})[0] == "rtmp"


@pytest.mark.parametrize("url", ["rtmp://abc123def456.global-contribute.live-video.net/app/key",
                                 "rtmps://ingest.example.com/live/key", "rtmp://10.0.0.5/live", "rtmp://127.0.0.1.evil.com/x",
                                 "http://127.0.0.1/x", "srt://127.0.0.1:9000"])
def test_output_guard_refuses_everything_else(url):
    with pytest.raises(OutputRefused):
        check_output(url, {})


def test_output_guard_gate_needs_exact_phrase():
    url = "rtmps://ingest.example.com/live/key"
    with pytest.raises(OutputRefused):
        check_output(url, {GO_LIVE_ENV: "yes"})
    assert check_output(url, {GO_LIVE_ENV: GO_LIVE_PHRASE})[0] == "rtmp"


def test_encoder_cmd_refuses_remote(monkeypatch, tmp_path):
    monkeypatch.delenv(GO_LIVE_ENV, raising=False)
    cfg = Config()
    cfg.run_dir = tmp_path
    cfg.output = "rtmp://example.com/live/key"
    with pytest.raises(OutputRefused):
        encoder_cmd(cfg)


def test_browser_only_opens_loopback():
    with pytest.raises(OutputRefused):
        check_browser_url("https://example.com/")
    cfg = Config()
    cmd = browser_cmd(cfg)
    assert any(a.startswith("--app=http://127.0.0.1:") for a in cmd)
    assert not any("user-agent" in a or "proxy" in a for a in cmd)


def test_encoder_cmd_is_720p_x264_with_safety_strip(tmp_path):
    cfg = Config()
    cfg.run_dir = tmp_path
    cfg.output = f"file:{tmp_path}/rec"
    cmd = " ".join(encoder_cmd(cfg))
    assert "1280x720" in cmd and "libx264" in cmd and "drawtext" in cmd
    assert "18+" in (tmp_path / "strip.txt").read_text()


def test_pgm_reader_and_stats(tmp_path):
    p = tmp_path / "x.pgm"
    p.write_bytes(b"P5\n4 2\n255\n" + bytes([10, 20, 30, 40, 50, 60, 70, 80]))
    px = checks.read_pgm(p)
    assert len(px) == 8
    mean, sd = checks.picture_stats(px)
    assert mean == 45 and sd > 20
    p.write_bytes(b"P5\n4 2\n255\n" + bytes([0]))
    assert checks.read_pgm(p) is None


def test_pgm_with_whitespace_valued_pixels(tmp_path):
    p = tmp_path / "x.pgm"
    pix = bytes([10, 32, 9, 13, 32, 32, 10, 200])
    p.write_bytes(b"P5\n4 2\n255\n" + pix)
    assert checks.read_pgm(p) == pix


def test_picture_probe_frozen_and_blank(tmp_path):
    clock = [100.0]
    p = tmp_path / "x.pgm"
    pr = checks.PictureProbe(p, now=lambda: clock[0])
    p.write_bytes(b"P5\n2 2\n255\n" + bytes([0, 255, 0, 255]))
    pr.update()
    clock[0] = 110.0
    pr.update()
    assert pr.frozen_for() == 10
    p.write_bytes(b"P5\n2 2\n255\n" + bytes([1, 255, 0, 255]))
    pr.update()
    assert pr.frozen_for() == 0
    p.write_bytes(b"P5\n2 2\n255\n" + bytes([5, 5, 5, 5]))
    pr.update()
    clock[0] = 120.0
    pr.update()
    assert pr.blank_for() == 10
    p.write_bytes(b"P5\n2 2\n255\n" + bytes([0, 255, 0, 255]))
    pr.update()
    assert pr.blank_for() == 0


def test_status_fresh(tmp_path):
    from hubie.util import write_status
    assert checks.status_fresh(tmp_path / "none.json", 5) == "no heartbeat yet"
    write_status(tmp_path, "a")
    assert checks.status_fresh(tmp_path / "a.json", 5) is None
    import time
    assert "stale" in checks.status_fresh(tmp_path / "a.json", 5, now=time.time() + 100)


def test_progress_parser():
    st = {}
    checks.parse_progress_line("frame=10\n", st)
    first = st["frame_changed"]
    checks.parse_progress_line("frame=10\n", st)
    assert st["frame_changed"] == first
    checks.parse_progress_line("fps=30.1\n", st)
    assert st["fps"] == "30.1"
    checks.parse_progress_line("garbage", st)


def test_x_probe_on_missing_display():
    assert checks.x_server_alive(":9987") is not None


def _supervisor(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBIE_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("HUBIE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUBIE_OUTPUT", f"file:{tmp_path}/rec")
    return Supervisor(Config())


def test_supervisor_restarts_dead_part_with_backoff_and_logs(tmp_path, monkeypatch):
    sv = _supervisor(tmp_path, monkeypatch)
    part = Part("worker", lambda: ["sleep", "60"])
    sv.spawn(part)
    pid = part.proc.pid
    part.proc.kill()
    part.proc.wait()
    sv.restart(part, "exited")
    assert part.proc.pid != pid and part.fail_streak == 1
    pid2 = part.proc.pid
    sv.restart(part, "again")          # inside backoff: ignored
    assert part.proc.pid == pid2
    sv._resolve(part)
    text = sv.cfg.incidents.read_text()
    assert '"recovered"' in text and '"worker"' in text
    sv.stop_part(part)


def test_stale_cleanup_only_kills_exact_pid_and_create_time(tmp_path, monkeypatch):
    import json
    import time
    sv = _supervisor(tmp_path, monkeypatch)
    ours = subprocess.Popen(["sleep", "60"])
    other = subprocess.Popen(["sleep", "60"])
    import psutil
    (sv.cfg.pid_dir / "a.json").write_text(json.dumps({"pid": ours.pid, "create_time": psutil.Process(ours.pid).create_time(), "part": "a"}))
    (sv.cfg.pid_dir / "b.json").write_text(json.dumps({"pid": other.pid, "create_time": 12345.0, "part": "b"}))
    sv.cfg.display = ":9987"
    sv.clean_stale()
    ours.wait(timeout=5)
    assert other.poll() is None
    other.kill()
    other.wait()


def test_housekeeping_prunes_only_our_segments(tmp_path, monkeypatch):
    sv = _supervisor(tmp_path, monkeypatch)
    rec = tmp_path / "rec"
    rec.mkdir()
    sv.cfg.record_max_gb = 1 / 1024 ** 2   # 1 KB... of segments
    for i in range(4):
        f = rec / f"hubie_{i}.ts"
        f.write_bytes(b"x" * 2048)
        os.utime(f, (1000 + i, 1000 + i))
    keep = rec / "other.ts"
    keep.write_bytes(b"x" * 9999)
    sv.housekeeping()
    left = sorted(p.name for p in rec.iterdir())
    assert "other.ts" in left and "hubie_3.ts" in left and "hubie_0.ts" not in left


def _scene_cfg(tmp_path, **env):
    import pytest as _p
    m = _p.MonkeyPatch()
    for k, v in {"HUBIE_RUN_DIR": str(tmp_path / "run"), "HUBIE_DATA_DIR": str(tmp_path / "data"), "HUBIE_SCENE": "ffmpeg",
                 "HUBIE_OUTPUT": f"file:{tmp_path}/rec", **env}.items():
        m.setenv(k, v)
    cfg = Config()
    cfg.ensure_dirs()
    return cfg, m


def test_scene_cmd_is_720p30_2500k_2s_keyframes_with_strip_and_no_display(tmp_path):
    from hubie.pipeline.commands import scene_cmd
    cfg, m = _scene_cfg(tmp_path)
    cmd = " ".join(scene_cmd(cfg))
    m.undo()
    assert "s=1280x720:r=30" in cmd and "-b:v 2500k" in cmd and "-g 60" in cmd and "-keyint_min 60" in cmd and "libx264" in cmd
    assert "x11grab" not in cmd and "strip.txt" in cmd and "expansion=none" in cmd


def test_scene_cmd_refuses_remote(tmp_path):
    from hubie.pipeline.commands import scene_cmd
    cfg, m = _scene_cfg(tmp_path, HUBIE_OUTPUT="rtmp://stream.example.com/live/key")
    with pytest.raises(OutputRefused):
        scene_cmd(cfg)
    m.undo()


def test_ffmpeg_scene_runs_no_browser_or_display(tmp_path):
    from hubie.supervisor import active_parts
    cfg, m = _scene_cfg(tmp_path)
    assert active_parts(cfg) == ["stage", "mixer", "encoder", "brain"]
    assert set(Supervisor(cfg).parts) == {"stage", "mixer", "encoder", "brain"}
    m.undo()
    cfg, m = _scene_cfg(tmp_path, HUBIE_SCENE="chrome")
    assert "browser" in active_parts(cfg)
    m.undo()


def test_scene_text_is_atomic_bounded_and_never_empty(tmp_path):
    from hubie.stage.scene import Scene, fit
    assert len(fit("word " * 200, 40, 3).splitlines()) == 3
    from hubie.config import ROOT
    s = Scene(tmp_path / "scene", ROOT / "assets/placeholder")
    s.apply({"type": "chat", "name": "amy", "text": "100% %{hack} ok"})
    s.apply({"type": "round", "game": "mines", "multiplier": 1.5, "won": True, "balance": 1000})
    s.apply({"type": "segment", "title": "slow thoughts"})
    assert "%{hack}" in (tmp_path / "scene/chat.txt").read_text()
    assert "demo balance 1000" in (tmp_path / "scene/game.txt").read_text()
    assert (tmp_path / "scene/caption.txt").read_text() == " "
    assert (tmp_path / "scene/hubie.png").resolve().name == "hubie_closed.png"
    assert not list((tmp_path / "scene").glob(".*"))


def test_housekeeping_clears_orphan_clips_and_respects_disk_floor(tmp_path, monkeypatch):
    import shutil
    import time as _t
    cfg, m = _scene_cfg(tmp_path)
    sv = Supervisor(cfg)
    old = cfg.queue_dir / "000.wav"
    old.write_bytes(b"x")
    os.utime(old, (_t.time() - 3600, _t.time() - 3600))
    fresh = cfg.queue_dir / "001.wav"
    fresh.write_bytes(b"x")
    rec = tmp_path / "rec"
    rec.mkdir()
    for i in range(3):
        f = rec / f"hubie_{i}.ts"
        f.write_bytes(b"x")
        os.utime(f, (_t.time() - 100 + i, _t.time() - 100 + i))
    other = rec / "keepme.ts"
    other.write_bytes(b"x")
    real = shutil.disk_usage
    monkeypatch.setattr("hubie.supervisor.shutil.disk_usage", lambda p: type("U", (), {"free": 0}))
    sv.housekeeping()
    m.undo()
    assert not old.exists() and fresh.exists()
    assert [p.name for p in rec.glob("hubie_*.ts")] == ["hubie_2.ts"] and other.exists()
    assert real  # the real call is untouched outside this test


def test_hls_output_is_a_local_folder_with_a_small_window(tmp_path):
    from hubie.config import Config
    from hubie.pipeline.commands import output_args
    assert check_output(f"hls:{tmp_path}", {}) == ("hls", str(tmp_path))
    for bad in ("hls:relative/dir", "hls:rtmp://example.com/live", "hls:"):
        with pytest.raises(OutputRefused):
            check_output(bad, {})
    args = output_args(Config(), "hls", str(tmp_path / "h"))
    assert args[args.index("-f") + 1] == "hls"
    assert "delete_segments" in args[args.index("-hls_flags") + 1]
    assert int(args[args.index("-hls_list_size") + 1]) <= 10
    assert args[-1].endswith("index.m3u8") and not any("://" in a for a in args)


def test_mixer_skips_a_plain_round_line_only_when_a_newer_round_line_waits(tmp_path, monkeypatch):
    import json as _json
    import wave
    from types import SimpleNamespace
    from hubie.audio import mixer as mx
    monkeypatch.setattr(mx, "post_stage", lambda *a, **k: None)
    q = tmp_path / "q"
    q.mkdir()

    def clip(stem, kind, droppable=False):
        with wave.open(str(q / f"{stem}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000); w.writeframes(b"\x01\x00" * 4800)
        meta = {"wav": f"{stem}.wav", "text": stem, "kind": kind, "duration": 0.1, "expires_at": 9e12}
        if droppable:
            meta["droppable"] = True
        (q / f"{stem}.json").write_text(_json.dumps(meta))

    m = mx.Mixer.__new__(mx.Mixer)
    m.cfg, m.queue_dir, m.gap_until, m.skipped, m.voice = SimpleNamespace(stage_url="http://127.0.0.1:1"), q, 0.0, 0, None
    clip("001", "round", droppable=True)
    clip("002", "chat")
    m._next_clip()
    assert m.voice.meta["text"] == "001" and m.skipped == 0      # no newer round: it plays
    m.voice.remove(); m.voice = None
    clip("003", "round", droppable=True)
    clip("004", "round", droppable=True)
    m._next_clip()
    assert m.voice.meta["text"] == "002"                           # chat first, in order
    m.voice.remove(); m.voice = None
    m._next_clip()
    assert m.voice.meta["text"] == "004" and m.skipped == 1        # 003 skipped, the newest round still speaks
    assert not (q / "003.json").exists()


def test_rtmp_with_hls_mirror_uses_tee(tmp_path, monkeypatch):
    from hubie.config import Config
    from hubie.pipeline.commands import output_args
    monkeypatch.delenv("HUBIE_OUTPUT_MIRROR_HLS", raising=False)
    assert output_args(Config(), "rtmp", "rtmps://x:443/app/k") == ["-f", "flv", "rtmps://x:443/app/k"]
    monkeypatch.setenv("HUBIE_OUTPUT_MIRROR_HLS", str(tmp_path / "h"))
    args = output_args(Config(), "rtmp", "rtmps://x:443/app/k")
    assert args[:4] == ["-f", "tee", "-flags", "+global_header"]
    spec = args[4]
    flv, hls = spec.split("|")
    assert flv.startswith("[f=flv") and flv.endswith("]rtmps://x:443/app/k") and "onfail" not in flv
    assert hls.startswith("[f=hls:onfail=ignore") and hls.endswith("index.m3u8")
