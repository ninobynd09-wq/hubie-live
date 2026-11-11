"""Watches the watchdog: restarts the supervisor if it dies or stops heartbeating.
Run this under systemd (deploy/hubie-stream.service) so the guardian itself is covered.

Once the show has signed off (the show-over flag in the run dir, hubie/credits.py) it restarts nothing and exits 0,
and it won't start at all while the flag is there, so no relaunch from anywhere brings the show back."""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .checks import status_fresh
from .config import Config, load_dotenv
from .credits import show_over
from .supervisor import kill_tree

log = logging.getLogger("hubie.guardian")
OVER_EXIT_WAIT = 60.0   # after the flag: how long the supervisor gets to stop its parts before it is stopped


def guard(cfg: Config, cmd: list[str] | None = None, stale_after: float = 20.0, stopping: dict | None = None,
          max_backoff: float = 30.0) -> int:
    cfg.ensure_dirs()
    cmd = cmd or [sys.executable, "-m", "hubie.supervisor"]
    stopping = stopping if stopping is not None else {"flag": False}
    flag = cfg.show_over_flag
    if show_over(flag):
        log.warning("the show is over (%s); not starting. preview.py start or `python -m hubie new-show` clears it",
                    flag)
        return 0
    status = cfg.status_dir / "supervisor.json"
    backoff = 1.0
    cwd = str(Path(__file__).resolve().parent.parent)
    proc = None
    while not stopping["flag"]:
        status.unlink(missing_ok=True)
        with open(cfg.log_dir / "supervisor.log", "ab") as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, cwd=cwd, start_new_session=True,
                                    stdin=subprocess.DEVNULL)
        (cfg.pid_dir.parent / "supervisor.pid").write_text(str(proc.pid))
        log.info("supervisor pid %d", proc.pid)
        started = time.time()
        over_since = None
        while not stopping["flag"]:
            if proc.poll() is not None:
                log.warning("supervisor exited rc=%s", proc.returncode)
                break
            if show_over(flag):
                # the supervisor is stopping its parts: no heartbeat kill now, only a bounded wait
                over_since = over_since or time.time()
                if time.time() - over_since > OVER_EXIT_WAIT:
                    log.warning("show over but the supervisor is still up, stopping it")
                    break
            elif time.time() - started > 25 and status_fresh(status, stale_after):
                log.warning("supervisor heartbeat stale, killing it")
                proc.send_signal(signal.SIGKILL)
                proc.wait()
                break
            time.sleep(1)
        if show_over(flag):
            log.warning("show over (%s): not restarting anything", (show_over(flag) or {}).get("reason"))
            break
        if time.time() - started > 60:
            backoff = 1.0
        if not stopping["flag"]:
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
    return 0


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    stopping = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stopping.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stopping.update(flag=True))
    sys.exit(guard(Config(), stale_after=float(os.environ.get("HUBIE_GUARDIAN_STALE", "20")), stopping=stopping))


if __name__ == "__main__":
    main()
