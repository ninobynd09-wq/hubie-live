"""Small shared helpers: atomic status files and stage notifications."""
from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_status(status_dir: Path, part: str, **fields: Any) -> None:
    fields["ts"] = time.time()
    fields["pid"] = os.getpid()
    atomic_write_json(Path(status_dir) / f"{part}.json", fields)


def ask_stage(base_url: str, event: dict, timeout: float = 0.35) -> dict | None:
    """Tell the stage something and read its answer (the click plan). None on any failure."""
    try:
        req = urllib.request.Request(
            base_url + "/event", data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read() or b"{}")
            return out if isinstance(out, dict) else None
    except Exception:
        return None


def post_stage(base_url: str, event: dict, timeout: float = 0.5) -> bool:
    """Tell the stage something. Cosmetic: failure never affects the caller."""
    try:
        req = urllib.request.Request(
            base_url + "/event", data=json.dumps(event).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False
