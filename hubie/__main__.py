"""CLI: python -m hubie run | supervise | status | report | new-show

new-show clears the show-over flag the automatic sign-off leaves (hubie/credits.py), so `run` starts again."""
from __future__ import annotations

import json
import sys
import time

from .config import Config, load_dotenv
from .util import read_json


def status() -> None:
    cfg = Config()
    print(json.dumps(read_json(cfg.status_dir / "supervisor.json", {}), indent=2))
    over = read_json(cfg.show_over_flag) if cfg.show_over_flag.exists() else None
    print("show_over", json.dumps(over))
    for p in ("brain", "mixer", "stage"):
        print(p, json.dumps(read_json(cfg.status_dir / f"{p}.json", {})))


def report() -> None:
    cfg = Config()
    events = []
    if cfg.incidents.exists():
        events = [json.loads(x) for x in cfg.incidents.read_text().splitlines() if x.strip()]
    rec = [e for e in events if e.get("event") == "recovered"]
    print(f"events: {len(events)}  recoveries: {len(rec)}")
    for e in rec[-30:]:
        print(f"{time.strftime('%H:%M:%S', time.gmtime(e['detected_at']))}  {e['part']:8s} "
              f"{e['downtime']:6.1f}s  {e['reason']}")


def main() -> None:
    load_dotenv()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        from .guardian import main as m
        m()
    elif cmd == "supervise":
        from .supervisor import main as m
        m()
    elif cmd == "status":
        status()
    elif cmd == "report":
        report()
    elif cmd == "new-show":
        from .credits import clear_show_over
        print("cleared" if clear_show_over(Config().show_over_flag) else "no show-over flag")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
