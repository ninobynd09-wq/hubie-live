"""Stage server: serves the page Chrome renders and relays events to it over a
websocket. Binds to loopback only. Kept apart from the brain so the picture
stays up while the brain restarts."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from aiohttp import WSMsgType, web

from ..config import Config, load_dotenv
from ..util import write_status
from .desk import DeskScene, desk_ready
from .scene import Scene

log = logging.getLogger("hubie.stage")
STATIC = Path(__file__).with_name("static")


class Stage:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.clients: set[web.WebSocketResponse] = set()
        self.snapshot: dict = {"segment": None, "history": [], "chat": [], "round": None, "stats": {}}
        self.history = deque(maxlen=14)
        self.chat = deque(maxlen=9)
        self.page = {"ts": 0.0, "raf": 0, "raf_changed": 0.0, "hello": 0}
        self.events = 0
        self.scene = None
        if cfg.scene != "chrome":
            self.scene = DeskScene(cfg) if desk_ready(cfg) else Scene(cfg.scene_dir, cfg.art_dir)

    def apply(self, ev: dict) -> dict | None:
        out = self.scene.apply(ev) if self.scene else None
        t = ev.get("type")
        if t == "segment":
            self.snapshot["segment"] = ev
        elif t == "round":
            self.history.append({"game": ev.get("game"), "multiplier": ev.get("multiplier"), "won": ev.get("won")})
            self.snapshot["history"] = list(self.history)
            self.snapshot["round"] = ev
        elif t == "chat":
            self.chat.append({"name": ev.get("name"), "text": ev.get("text")})
            self.snapshot["chat"] = list(self.chat)
        return out if isinstance(out, dict) else None

    async def broadcast(self, ev: dict) -> None:
        data = json.dumps(ev)
        for ws in list(self.clients):
            try:
                await asyncio.wait_for(ws.send_str(data), 1.0)
            except Exception:
                self.clients.discard(ws)

    async def h_event(self, req: web.Request) -> web.Response:
        ev = await req.json()
        self.events += 1
        out = self.apply(ev)
        if ev.get("type") == "click":   # answered straight away: the adapter is holding the spin for this
            return web.json_response({"ok": True, **(out or {"skip": "no desk scene"})})
        await self.broadcast(ev)
        return web.json_response({"ok": True, **(out or {})})

    async def h_health(self, req: web.Request) -> web.Response:
        return web.json_response({"ok": True, "clients": len(self.clients), "events": self.events})

    async def h_index(self, req: web.Request) -> web.Response:
        return web.FileResponse(STATIC / "stage.html", headers={"Cache-Control": "no-store"})

    async def h_config(self, req: web.Request) -> web.Response:
        return web.json_response({"test_hooks": self.cfg.test_hooks})

    async def h_ws(self, req: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=None)
        await ws.prepare(req)
        self.clients.add(ws)
        self.page["hello"] += 1
        await ws.send_str(json.dumps({"type": "snapshot", **self.snapshot}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    d = json.loads(msg.data)
                    if d.get("type") == "hb":
                        now = time.time()
                        self.page["ts"] = now
                        if d.get("raf") != self.page["raf"]:
                            self.page["raf"] = d.get("raf")
                            self.page["raf_changed"] = now
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
        return ws

    async def status_loop(self, app) -> None:
        while True:
            write_status(self.cfg.status_dir, "stage", page_ts=self.page["ts"],
                         raf_changed=self.page["raf_changed"], clients=len(self.clients), hello=self.page["hello"])
            await asyncio.sleep(1.0)


async def _start_status(app):
    app["status_task"] = asyncio.create_task(app["stage"].status_loop(app))
    if hasattr(app["stage"].scene, "start"):
        app["stage"].scene.start()


def make_app(cfg: Config) -> web.Application:
    stage = Stage(cfg)
    app = web.Application(client_max_size=256 * 1024)
    app["stage"] = stage
    app.router.add_get("/", stage.h_index)
    app.router.add_get("/health", stage.h_health)
    app.router.add_get("/config", stage.h_config)
    app.router.add_post("/event", stage.h_event)
    app.router.add_get("/ws", stage.h_ws)
    app.router.add_static("/static/", STATIC)
    app.on_startup.append(_start_status)
    return app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    cfg = Config()
    cfg.ensure_dirs()
    web.run_app(make_app(cfg), host="127.0.0.1", port=cfg.stage_port, print=None, access_log=None,
                handle_signals=True)


if __name__ == "__main__":
    main()
