#!/usr/bin/env python3
"""The one command that spins housebets slots, in fun mode only.

  python3 /root/hubie_stream/tools/housebets/hb_spin.py --games sweet_bonanza_1000 --rounds 3
  python3 /root/hubie_stream/tools/housebets/hb_spin.py --list
  python3 /root/hubie_stream/tools/housebets/hb_spin.py --games sweet_bonanza_1000 --rounds 1 --bonus-buy

--browse switches games the way the show does with HUBIE_HB_BROWSE=1: the slots page, a slow scroll, a hover
over a tile or two, a click on the game's own tile (the only extra click allowed), then Try fun mode.
--video PATH records the browser's display meanwhile (the drawn pointer and the balance patch included).

--bonus-buy makes each game's first round a DEMO feature buy (the game's cheapest standard buy, never
SUPER), with every check the show's own buys get. At most 3 buys per run.

Every spin goes through HousebetsSlotsAdapter, which refuses to spin unless the site's toggle is on
Fun, the page's own launch asked for play_mode "fun", the game runs on the provider's demo host,
the game shows a demo balance, and the account's real balance has not moved. The only site button
it can click is "Try fun mode" (plus Reject on the cookie banner). One Chrome, closed at exit.
Prints one JSON line per round.
"""
import argparse
import json
import logging
import os
import sys

ROOT = "/root/hubie_stream"
sys.path.insert(0, ROOT)

from hubie.games.base import GameError, NotDemoError          # noqa: E402
from hubie.games.housebets import GAMES, HousebetsSlotsAdapter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", default="sweet_bonanza_1000", help="comma list of game ids")
    ap.add_argument("--rounds", type=int, default=3, help="rounds per game (max 10)")
    ap.add_argument("--shots", default=os.path.join(ROOT, "shots"), help="screenshot dir")
    ap.add_argument("--rotate", type=int, default=0,
                    help="instead of --games order: N segments picked by the show's own scheduler")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--bonus-buy", action="store_true", help="first round per game is a demo feature buy")
    ap.add_argument("--topbar", action="store_true", help="HUBIE_HB_TOPBAR=1 layout; after each load print the top "
                    "bar check and save a screenshot")
    ap.add_argument("--browse", action="store_true", help="reach each game by browsing the slots page")
    ap.add_argument("--video", default="", help="record the browser display to this mp4")
    ap.add_argument("--record", default="", help="save the game traffic of the run to this JSON file")
    a = ap.parse_args()
    if a.list:
        for g in GAMES:
            print(g.id, "verified" if g.verified else "unverified", g.slug)
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ids = [g.strip() for g in a.games.split(",") if g.strip()]
    if a.topbar:
        os.environ["HUBIE_HB_TOPBAR"] = "1"
    os.makedirs(a.shots, exist_ok=True)
    ad = HousebetsSlotsAdapter(games=ids, allow_unverified=True, shots_dir=a.shots)
    if not a.bonus_buy:
        ad.bonus_every = 0          # a short tool run never buys unless asked
    ad.allow_unverified_buys = a.bonus_buy
    ad.browse = a.browse
    traffic = ad.record = []
    order = ids
    if a.rotate:
        from hubie.brain.memory import Memory
        from hubie.brain.scheduler import Scheduler
        sched = Scheduler(Memory(None), ["x"])
        order = []
        for _ in range(min(a.rotate, 12)):
            gid = sched._pick_game(ad.list_games())
            sched.state["recent_games"].append(gid)
            order.append(gid)
        print(json.dumps({"rotation": order}), flush=True)
    code = 0
    rec = None
    try:
        ad.start()
        if a.browse:
            import threading
            import time

            def watch():   # what the stage would show, as the show's orchestrator polls it
                last = None
                while True:
                    st = ad.screen_state()
                    if st != last:
                        print(json.dumps({"screen": st, "t": round(time.time(), 1)}), flush=True)
                        last = st
                    time.sleep(0.25)
            threading.Thread(target=watch, daemon=True).start()
        if a.video:
            import subprocess
            rec = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "x11grab", "-framerate", "15", "-video_size",
                                    f"{ad.width}x{ad.height}", "-draw_mouse", "0", "-i", ad.used_display,
                                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
                                    a.video], stdin=subprocess.PIPE)
        for gid in order:
            try:
                ad.load_game(gid)
            except NotDemoError as e:
                print(json.dumps({"game": gid, "stop": "not demo", "why": str(e)}), flush=True)
                return 2
            except GameError as e:
                print(json.dumps({"game": gid, "skipped": str(e)}), flush=True)
                code = 1
                continue
            if a.topbar:
                print(json.dumps({"topbar": ad.topbar_report(os.path.join(a.shots, f"hb_topbar_{gid}.png"))}), flush=True)
            if a.bonus_buy and ad.buys_done < 3:
                ad.force_bonus_buy = True
            for _ in range(min(a.rounds, 10)):
                if a.bonus_buy and ad.buys_done >= 3:
                    ad.force_bonus_buy = False
                if not ad.is_demo():
                    print(json.dumps({"game": gid, "stop": "not demo"}), flush=True)
                    return 2
                try:
                    r = ad.play_round(1)
                    print(json.dumps(r.to_dict()), flush=True)
                    if a.topbar:
                        print(json.dumps({"topbar_after_round": ad.topbar_report()}), flush=True)
                except NotDemoError as e:
                    print(json.dumps({"game": gid, "stop": "not demo", "why": str(e)}), flush=True)
                    return 2
                except GameError as e:
                    print(json.dumps({"game": gid, "error": str(e)}), flush=True)
                    code = 1
                    ad.recover()
        if ad.is_demo():
            first, now = ad.wallet_check()
            print(json.dumps({"real_wallet_first": first, "real_wallet_now": now, "unchanged": first == now,
                              "demo_buys": ad.buys_done, "purchase_requests": ad._buy_seen}), flush=True)
            if first != now:
                code = 2
    finally:
        if rec:
            try:
                rec.communicate(b"q", timeout=15)
            except Exception:
                rec.kill()
        if a.record:
            with open(a.record, "w") as f:
                json.dump({"exchanges": traffic}, f, indent=0)
        ad.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
