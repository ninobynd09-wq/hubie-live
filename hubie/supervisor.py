"""Watchdog. Owns every process, detects dead or frozen parts, restarts only what
broke, never gives up and never needs a human. See README for the check table."""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import psutil

from . import checks
from .config import Config, load_dotenv
from .credits import show_over
from .pipeline.commands import browser_cmd, encoder_cmd, scene_cmd, xvfb_cmd
from .pipeline.output_guard import check_output
from .stage.desk import desk_ready, prepare as prepare_desk
from .stage.scene import prepare as prepare_scene
from .util import atomic_write_json, read_json, write_status

log = logging.getLogger("hubie.watchdog")
LOG_MAX = 5 * 1024 * 1024   # these live in /dev/shm, so they are RAM
QUEUE_FILE_MAX_AGE = 600.0
ORDER = ["xvfb", "stage", "mixer", "encoder", "browser", "brain"]
# Without a browser scene there is no Chrome, and no display unless the game browser needs one.
def active_parts(cfg: Config) -> list[str]:
    if cfg.scene == "chrome":
        return list(ORDER)
    drop = {"browser"} | ({"xvfb"} if cfg.scene_bg != "x11" else set())
    return [n for n in ORDER if n not in drop]


DEPENDENTS = {"xvfb": ["browser", "encoder"]}


def kill_tree(pid: int, term_wait: float = 1.5) -> None:
    """Kill one process and its descendants, by exact PID only."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    try:
        procs = root.children(recursive=True) + [root]
    except psutil.Error:
        procs = [root]
    for p in procs:
        try:
            p.send_signal(signal.SIGCONT)
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=term_wait)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=2)


def tree_rss_mb(pid: int) -> float:
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return 0.0
    total = 0
    for p in procs:
        total += _pss_bytes(p.pid)
    return total / (1024 * 1024)


def _pss_bytes(pid: int) -> int:
    """Proportional memory: shared pages split between the processes using them. Plain RSS counts a
    Chrome's shared pages once per process and reads several times what the browser really holds."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return psutil.Process(pid).memory_info().rss
    except psutil.Error:
        return 0


@dataclass
class Part:
    name: str
    cmd: Callable[[], list[str]]
    rss_limit_mb: int = 0
    env: dict = field(default_factory=dict)
    proc: subprocess.Popen | None = None
    started: float = 0.0
    unhealthy_since: float | None = None
    next_restart: float = 0.0
    fail_streak: int = 0
    healthy_since: float | None = None
    restarts: list[float] = field(default_factory=list)
    open_incident: dict | None = None


class Supervisor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.ensure_dirs()
        self.env = dict(os.environ)
        self.env["PYTHONUNBUFFERED"] = "1"
        self.env["HUBIE_RUN_DIR"] = str(cfg.run_dir)
        self.env["HUBIE_DATA_DIR"] = str(cfg.data_dir)
        self.encoder_state: dict = {}
        self.probe = checks.PictureProbe(cfg.probe)
        self.browser_frozen_restarts = 0
        self.fifo_keeper = -1
        self.stop = False
        self.started = time.time()
        py = sys.executable
        self.parts: dict[str, Part] = {p.name: p for p in [
            Part("xvfb", lambda: xvfb_cmd(cfg)),
            Part("stage", lambda: [py, "-m", "hubie.stage.server"]),
            Part("mixer", lambda: [py, "-m", "hubie.audio.mixer"]),
            Part("encoder", lambda: (encoder_cmd if cfg.scene == "chrome" else scene_cmd)(cfg), rss_limit_mb=cfg.wd_rss_encoder_mb),
            Part("browser", lambda: browser_cmd(cfg), rss_limit_mb=cfg.wd_rss_browser_mb,
                 env={"DISPLAY": cfg.display}),
            Part("brain", lambda: [py, "-m", "hubie.orchestrator"], rss_limit_mb=cfg.wd_rss_brain_mb),
        ] if p.name in active_parts(cfg)}
        self.order = [n for n in ORDER if n in self.parts]
        self.last_housekeeping = 0.0
        self.over: dict | None = None    # the show signed off (flag in the run dir): stop everything, restart nothing

    # ---- plumbing ----

    def _make_fifo(self) -> None:
        f = self.cfg.fifo
        if f.exists() and not stat.S_ISFIFO(f.stat().st_mode):
            f.unlink()
        if not f.exists():
            os.mkfifo(f)
        # Keep a writer open here so the encoder never sees EOF when the mixer restarts.
        if self.fifo_keeper < 0:
            self.fifo_keeper = os.open(f, os.O_RDWR | os.O_NONBLOCK)

    def _pidfile(self, name: str) -> Path:
        return self.cfg.pid_dir / f"{name}.json"

    def _record_pid(self, part: Part) -> None:
        pid = part.proc.pid
        try:
            ct = psutil.Process(pid).create_time()
        except psutil.Error:
            ct = 0
        atomic_write_json(self._pidfile(part.name), {"pid": pid, "create_time": ct, "part": part.name})

    def clean_stale(self) -> None:
        """After a supervisor crash: remove the previous run's children, matched by
        PID plus create time so a recycled PID is never touched."""
        for f in self.cfg.pid_dir.glob("*.json"):
            d = read_json(f) or {}
            try:
                p = psutil.Process(d.get("pid", -1))
                if abs(p.create_time() - d.get("create_time", 0)) < 1.0:
                    log.info("removing stale %s pid %s", d.get("part"), p.pid)
                    kill_tree(p.pid)
            except (psutil.Error, ValueError):
                pass
            f.unlink(missing_ok=True)
        num = self.cfg.display.lstrip(":")
        lock = Path(f"/tmp/.X{num}-lock")
        if lock.exists():
            try:
                pid = int(lock.read_text().strip())
                if psutil.pid_exists(pid):
                    raise SystemExit(f"display {self.cfg.display} is held by another process (pid {pid}); pick another")
            except ValueError:
                pass
            lock.unlink(missing_ok=True)
            Path(f"/tmp/.X11-unix/X{num}").unlink(missing_ok=True)
        for p in self.cfg.queue_dir.glob("*"):   # stale speech is worse than none
            if p.is_file():
                p.unlink(missing_ok=True)

    def spawn(self, part: Part) -> None:
        log_path = self.cfg.log_dir / f"{part.name}.log"
        logf = open(log_path, "ab")
        env = {**self.env, **part.env}
        stdout = subprocess.PIPE if part.name == "encoder" else logf
        if part.name == "encoder":
            self.encoder_state = {"frame_changed": time.time()}
            self.probe.reset()
        cwd = str(Path(__file__).resolve().parent.parent)
        part.proc = subprocess.Popen(part.cmd(), stdout=stdout, stderr=logf, env=env, cwd=cwd,
                                     start_new_session=True, stdin=subprocess.DEVNULL)
        logf.close()
        part.started = time.time()
        part.unhealthy_since = None
        part.healthy_since = None
        self._record_pid(part)
        if part.name == "encoder":
            threading.Thread(target=self._drain_progress, args=(part.proc,), daemon=True).start()
        log.info("started %s pid %d", part.name, part.proc.pid)

    def _drain_progress(self, proc: subprocess.Popen) -> None:
        last = None
        for raw in proc.stdout:
            try:
                checks.parse_progress_line(raw.decode("ascii", "ignore"), self.encoder_state)
                if self.encoder_state.get("frame") != last:
                    # the stage times reaction clips to the cam loops from this (hubie/stage/desk.py)
                    last = self.encoder_state.get("frame")
                    write_status(self.cfg.status_dir, "encoder_clock", frame=last, fps=self.cfg.fps)
            except Exception:
                pass

    def stop_part(self, part: Part) -> None:
        if part.proc is None:
            return
        kill_tree(part.proc.pid)
        try:
            part.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        part.proc = None

    # ---- incidents ----

    def log_event(self, **ev) -> None:
        ev["ts"] = time.time()
        with open(self.cfg.incidents, "a") as f:
            f.write(json.dumps(ev) + "\n")

    def check_show_over(self) -> bool:
        if self.over is None:
            self.over = show_over(self.cfg.show_over_flag)
            if self.over is not None:
                log.warning("show over (%s): stopping, nothing gets restarted", self.over.get("reason"))
                self.log_event(event="show_over", reason=self.over.get("reason"))
                self.stop = True
        return self.over is not None

    def restart(self, part: Part, reason: str, cascade: bool = True) -> None:
        now = time.time()
        if now < part.next_restart or self.check_show_over():
            return
        log.warning("restarting %s: %s", part.name, reason)
        last = None
        if part.name == "brain":
            # what the brain was doing when it went quiet or stale, so a stall can be traced afterwards
            b = read_json(self.cfg.status_dir / "brain.json") or {}
            last = {k: b.get(k) for k in ("doing", "doing_since", "segment", "rounds", "last_speech", "ts", "chat_backlog")}
            log.warning("brain before restart: %s", json.dumps(last))
        if part.open_incident is None:
            part.open_incident = {"part": part.name, "reason": reason, "detected_at": now}
            if last:
                part.open_incident["brain_last"] = last
        self.stop_part(part)
        part.fail_streak += 1
        part.restarts = [t for t in part.restarts if now - t < 300] + [now]
        part.next_restart = now + min(2 ** (part.fail_streak - 1), self.cfg.wd_max_backoff)
        if part.name == "stage" and "browser" in self.parts:
            # Page heartbeat will pause while the stage is away.
            self.parts["browser"].started = now
        try:
            self.spawn(part)
        except Exception as e:
            log.error("spawn %s failed: %r", part.name, e)
        if cascade:
            for dep in DEPENDENTS.get(part.name, []):
                d = self.parts.get(dep)
                if d is None:
                    continue
                d.next_restart = 0
                self.restart(d, f"{part.name} restarted", cascade=False)

    def _resolve(self, part: Part) -> None:
        now = time.time()
        if part.healthy_since is None:
            part.healthy_since = now
        if part.open_incident:
            inc = part.open_incident
            part.open_incident = None
            self.log_event(event="recovered", part=part.name, reason=inc["reason"], detected_at=inc["detected_at"],
                           recovered_at=now, downtime=round(now - inc["detected_at"], 2),
                           **({"brain_last": inc["brain_last"]} if inc.get("brain_last") else {}))
        if now - part.healthy_since > 60:
            part.fail_streak = 0

    # ---- per-part health ----

    def check_part(self, part: Part) -> str | None:
        cfg = self.cfg
        now = time.time()
        if part.proc is None:
            return "not running"
        rc = part.proc.poll()
        if rc is not None:
            return f"exited rc={rc}"
        grace = cfg.wd_start_grace if part.name in ("browser", "encoder", "brain") else 3.0
        if now - part.started < grace:
            return None
        if part.rss_limit_mb and tree_rss_mb(part.proc.pid) > part.rss_limit_mb:
            return f"memory over {part.rss_limit_mb} MB"
        if part.name == "xvfb":
            return checks.x_server_alive(cfg.display)
        if part.name == "stage":
            return checks.stage_alive(cfg.stage_url)
        if part.name == "mixer":
            return checks.status_fresh(cfg.status_dir / "mixer.json", cfg.wd_status_stale)
        if part.name == "encoder":
            return self._check_encoder(now)
        if part.name == "browser":
            return self._check_browser(now)
        if part.name == "brain":
            return self._check_brain(now)
        return None

    def _upstream_settling(self, now: float, *names: str, window: float = 6.0) -> bool:
        return any(now - self.parts[n].started < window for n in names if n in self.parts)

    def _check_encoder(self, now: float) -> str | None:
        if self._upstream_settling(now, "xvfb"):
            return None
        # A stopped mixer stalls the audio input; that is the mixer's fault, not the encoder's.
        if checks.status_fresh(self.cfg.status_dir / "mixer.json", self.cfg.wd_status_stale):
            return None
        if "xvfb" in self.parts and self.parts["xvfb"].unhealthy_since:
            return None
        age = now - self.encoder_state.get("frame_changed", self.parts["encoder"].started)
        if age > self.cfg.wd_encoder_stale:
            return f"no new frames for {age:.0f}s"
        if self.probe.file_age() > self.cfg.wd_encoder_stale:
            return f"probe not updating for {self.probe.file_age():.0f}s"
        m = read_json(self.cfg.status_dir / "mixer.json") or {}
        if m.get("dropped_delta", 0) > 150000 and now - self.parts["mixer"].started > 10:
            return "not reading audio"
        # With no browser the encoder draws the picture itself, so a frozen picture is its fault.
        if "browser" not in self.parts:
            self.probe.update()
            if self.probe.frozen_for() > self.cfg.wd_frozen_picture:
                return f"picture frozen {self.probe.frozen_for():.0f}s"
        return None

    def _check_browser(self, now: float) -> str | None:
        if self._upstream_settling(now, "xvfb", "stage", "encoder", window=self.cfg.wd_start_grace):
            return None
        if self.parts["stage"].unhealthy_since or self.parts["encoder"].unhealthy_since:
            return None
        r = checks.browser_process_alive(self.cfg.stage_port + 1)
        if r:
            return r
        st = read_json(self.cfg.status_dir / "stage.json") or {}
        page_age = now - st.get("page_ts", 0)
        if page_age > self.cfg.wd_page_stale:
            return f"page heartbeat stale {page_age:.0f}s"
        raf_age = now - st.get("raf_changed", 0)
        if raf_age > self.cfg.wd_page_stale:
            return f"page frames stopped {raf_age:.0f}s"
        self.probe.update()
        # Blank first: a blank page is also static, and blank is the more useful diagnosis.
        if self.probe.blank_for() > self.cfg.wd_frozen_picture - 2:
            return f"picture blank {self.probe.blank_for():.0f}s"
        if self.probe.frozen_for() > self.cfg.wd_frozen_picture:
            return f"picture frozen {self.probe.frozen_for():.0f}s"
        return None

    def _check_brain(self, now: float) -> str | None:
        r = checks.status_fresh(self.cfg.status_dir / "brain.json", self.cfg.wd_brain_stale)
        if r:
            return r
        if self.parts["mixer"].unhealthy_since or now - self.parts["mixer"].started < 15:
            return None
        m = read_json(self.cfg.status_dir / "mixer.json") or {}
        last = max(m.get("last_clip_end", 0), self.parts["brain"].started)
        if not m.get("playing") and now - last > self.cfg.wd_dead_air:
            return f"dead air {now - last:.0f}s"
        return None

    def _confirmed_working(self, part: Part, now: float) -> bool:
        """Recovery time ends when the part shows real output, not when its grace period ends."""
        if part.name == "encoder":
            return int(self.encoder_state.get("frame", 0) or 0) > 0
        if part.name == "browser":
            st = read_json(self.cfg.status_dir / "stage.json") or {}
            return st.get("page_ts", 0) > part.started
        if part.name in ("mixer", "brain"):
            d = read_json(self.cfg.status_dir / f"{part.name}.json") or {}
            return d.get("ts", 0) > part.started
        return now - part.started >= 1.0

    # ---- main loop ----

    def tick(self) -> None:
        if self.check_show_over():
            return
        now = time.time()
        self.probe.update()
        for name in self.order:
            part = self.parts[name]
            reason = self.check_part(part)
            if reason is None:
                part.unhealthy_since = None
                if self._confirmed_working(part, now):
                    self._resolve(part)
                continue
            exited = part.proc is None or part.proc.poll() is not None
            part.unhealthy_since = part.unhealthy_since or now
            # Exited: act now. Unresponsive: require it to persist a few seconds.
            if exited or now - part.unhealthy_since >= 3.0:
                self.restart(part, reason)
        self._escalate(now)
        if now - self.last_housekeeping > 30:
            self.last_housekeeping = now
            self.housekeeping()
        write_status(self.cfg.status_dir, "supervisor", uptime=now - self.started,
                     encoder={k: v for k, v in self.encoder_state.items() if k != "frame_changed"},
                     parts={n: {"pid": p.proc.pid if p.proc else None, "restarts_5m": len(p.restarts)}
                            for n, p in self.parts.items()})

    def _escalate(self, now: float) -> None:
        """A part that keeps failing may be a symptom. Reset the whole stack once in a while."""
        if self.over is not None:
            return
        for p in self.parts.values():
            recent = [t for t in p.restarts if now - t < 300]
            if len(recent) >= 8:
                log.error("%s restarted %d times in 5 min, resetting whole stack", p.name, len(recent))
                self.log_event(event="stack_reset", part=p.name)
                for q in reversed(list(self.parts.values())):
                    self.stop_part(q)
                    q.restarts = []
                    q.next_restart = 0
                for name in self.order:
                    self.spawn(self.parts[name])
                return

    def housekeeping(self) -> None:
        # Bound child logs. Files are opened in append mode so truncating is safe.
        for f in self.cfg.log_dir.glob("*.log"):
            try:
                if f.stat().st_size > LOG_MAX:
                    os.truncate(f, 0)
            except OSError:
                pass
        # A brain killed between writing a clip and its sidecar leaves files nobody will play.
        cutoff = time.time() - QUEUE_FILE_MAX_AGE
        for f in self.cfg.queue_dir.glob("*"):
            try:
                if f.stat().st_mtime < cutoff:
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
            except OSError:
                pass
        # Bound local recordings: delete oldest of our own segment files only.
        try:
            mode, target = check_output(self.cfg.output)
        except Exception:
            return
        if mode == "hls":
            # The encoder deletes its own old segments; a restarted encoder leaves the last one's behind.
            cutoff = time.time() - 60
            for f in Path(target).glob("seg_*.ts*"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
                except OSError:
                    pass
        if mode == "file":
            segs = sorted(Path(target).glob("hubie_*.ts"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in segs)
            limit = self.cfg.record_max_gb * 1024 ** 3
            floor = self.cfg.disk_floor_gb * 1024 ** 3
            # The box also runs live bots: their disk space outranks our recordings.
            while segs[:-1] and (total > limit or shutil.disk_usage(target).free < floor):
                old = segs.pop(0)
                total -= old.stat().st_size
                old.unlink(missing_ok=True)
                log.info("pruned %s", old.name)
        # Bound the incident log.
        try:
            if self.cfg.incidents.stat().st_size > 5 * 1024 * 1024:
                lines = self.cfg.incidents.read_text().splitlines()[-5000:]
                self.cfg.incidents.write_text("\n".join(lines) + "\n")
        except OSError:
            pass

    def shutdown(self, *_):
        self.stop = True

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.shutdown)
        signal.signal(signal.SIGINT, self.shutdown)
        check_output(self.cfg.output)   # refuse to even start on a remote target
        if self.check_show_over():
            log.warning("the show is over (%s); not starting. preview.py start or `python -m hubie new-show` "
                        "clears it", self.cfg.show_over_flag)
            return
        self.clean_stale()
        self._make_fifo()
        if self.cfg.scene != "chrome":
            prepare_scene(self.cfg.scene_dir, self.cfg.art_dir)
            if desk_ready(self.cfg):
                try:
                    prepare_desk(self.cfg)
                except Exception as e:   # the encoder's own prepare retries; never block startup on art
                    log.warning("desk prepare failed: %r", e)
        self.log_event(event="supervisor_start")
        for name in self.order:
            self.spawn(self.parts[name])
        try:
            while not self.stop:
                try:
                    self.tick()
                except Exception:
                    log.exception("watchdog tick failed, continuing")
                time.sleep(self.cfg.wd_tick)
        finally:
            brain = self.parts.get("brain")
            if self.over is not None and brain and brain.proc:
                try:   # the brain wrote the flag and is closing its game browser: let it finish
                    brain.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            for p in reversed(list(self.parts.values())):
                self.stop_part(p)
            self.log_event(event="supervisor_stop", **({"show_over": True} if self.over is not None else {}))


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Supervisor(Config()).run()


if __name__ == "__main__":
    main()
