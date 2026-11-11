"""Stops this repo from ever going live by accident.

Only a file path, a local HLS folder (the private preview) or a loopback RTMP URL is accepted. A remote URL needs the env
var below set to this exact phrase, which nothing in the repo sets.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

GO_LIVE_ENV = "HUBIE_ALLOW_REMOTE_OUTPUT"
GO_LIVE_PHRASE = "owner-approved-going-live"


class OutputRefused(Exception):
    pass


def _is_loopback(host: str) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_output(output: str, env: dict | None = None) -> tuple[str, str]:
    """Return (mode, target) with mode in file|hls|rtmp, or raise OutputRefused."""
    env = os.environ if env is None else env
    if output.startswith("file:"):
        target = output[5:]
        if not target:
            raise OutputRefused("empty file target")
        return "file", target
    if output.startswith("hls:"):
        # A local folder of HLS segments; nginx serves it to the private preview page. Never a URL.
        target = output[4:]
        if not target.startswith("/") or "://" in target:
            raise OutputRefused("hls target must be an absolute local folder")
        return "hls", target
    parsed = urlparse(output)
    if parsed.scheme not in ("rtmp", "rtmps"):
        raise OutputRefused(f"unsupported output scheme {parsed.scheme!r}")
    if _is_loopback(parsed.hostname or ""):
        return "rtmp", output
    if env.get(GO_LIVE_ENV) == GO_LIVE_PHRASE:
        return "rtmp", output
    raise OutputRefused("remote stream targets are refused; this build only outputs locally")


def check_browser_url(url: str) -> str:
    """The browser only ever opens the local stage page in this build."""
    host = urlparse(url).hostname or ""
    if not _is_loopback(host):
        raise OutputRefused(f"browser may only open loopback pages, got {host!r}")
    return url
