#!/usr/bin/env python3
"""One-call housebets browser. All the hard-won settings are baked in.

Nothing to remember: the proxy relay, the logged-in profile, plain google-chrome
under xvfb.

  from hb_browser import open_site
  async with open_site() as hb:          # hb.page, hb.browser
      await hb.goto("/games/slots")

From a shell:

  xvfb-run -a python3 hb_browser.py --url /contests
  xvfb-run -a python3 hb_browser.py --url /games/slots --shot out.png
  xvfb-run -a python3 hb_browser.py --serve          # leave Chrome up on :9231

If xvfb is missing this re-execs itself under xvfb-run, so plain
`python3 hb_browser.py --url /contests` also works.
"""
import asyncio, json, os, re, shutil, signal, subprocess, sys, time, urllib.request

BASE = "https://www.housebets.com"
RELAY = "http://127.0.0.1:8899"                     # games proxy (HUBIE_GAMES_PROXY), via relay.py
PROFILE = os.environ.get("HUBIE_HB_PROFILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "profiles", "hb_profile_no2"))   # a logged-in Chrome profile (gitignored)
DEBUG_PORT = int(os.getenv("HB_DEBUG_PORT", "9231"))
RELAY_SCRIPT = "/root/hubie_stream/tools/housebets/relay.py"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
MARK = ("Performing security verification", "Just a moment", "Verifying you are human")


# ── environment plumbing ─────────────────────────────────────────────

def _reexec_under_xvfb():
    """a headed browser needs an X server; do it for the caller"""
    if os.environ.get("DISPLAY"):
        return
    if not shutil.which("xvfb-run"):
        raise SystemExit("no DISPLAY and no xvfb-run — install xvfb")
    os.execvp("xvfb-run", ["xvfb-run", "-a", sys.executable] + sys.argv)


def relay_up():
    """make sure 127.0.0.1:8899 answers; start relay.py in the background if not"""
    try:
        subprocess.run(["curl", "-s", "-m", "6", "--proxy", RELAY, "https://ipinfo.io/json"],
                       capture_output=True, timeout=15, check=True)
        return "already running"
    except Exception:
        pass
    subprocess.Popen([sys.executable, RELAY_SCRIPT],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(30):
        try:
            subprocess.run(["curl", "-s", "-m", "4", "--proxy", RELAY, "https://ipinfo.io/json"],
                           capture_output=True, timeout=10, check=True)
            return "started"
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("proxy relay did not come up on %s" % RELAY)


def kill_port(port=DEBUG_PORT):
    out = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f"--remote-debugging-port={port}" in line and "chrome" in line:
            try:
                os.killpg(int(line.split()[0]), signal.SIGKILL)
            except Exception:
                try:
                    os.kill(int(line.split()[0]), signal.SIGKILL)
                except Exception:
                    pass


def launch_chrome(url="about:blank"):
    """start plain google-chrome on the logged-in profile"""
    for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.remove(os.path.join(PROFILE, n))
        except OSError:
            pass
    kill_port()
    proc = subprocess.Popen([
        "google-chrome", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
        f"--remote-debugging-port={DEBUG_PORT}", "--remote-debugging-address=127.0.0.1",
        f"--proxy-server={RELAY}", f"--user-data-dir={PROFILE}",
        f"--user-agent={UA}", "--window-size=1366,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(120):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=1)
            return proc
        except Exception:
            time.sleep(0.5)
    stop_chrome(proc)
    raise RuntimeError("chrome did not open a debug port")


def stop_chrome(proc, graceful=12):
    """SIGTERM and wait: a killed Chrome never flushes localStorage, which is
    what keeps a logged-in session alive between runs."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        return
    for _ in range(graceful * 2):
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        pass


async def _pass_challenge(pg, rounds=8):
    for _ in range(rounds):
        try:
            text = await pg.evaluate("document.body.innerText")
        except Exception:
            await pg.wait_for_timeout(2000)
            continue
        if not any(m in text for m in MARK):
            return True
        for f in pg.frames:
            if "challenge" not in f.url:
                continue
            try:
                box = await f.locator("body").first.bounding_box()
                if box and box["width"] > 50:
                    await pg.mouse.move(box["x"] - 30, box["y"])
                    await pg.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            except Exception:
                pass
        await pg.wait_for_timeout(2500)
    return False


# ── the thing Claude calls ───────────────────────────────────────────

class Site:
    def __init__(self, browser, page, proc):
        self.browser, self.page, self.proc = browser, page, proc

    async def goto(self, path="/", wait=5000):
        url = path if path.startswith("http") else BASE + path
        await self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await self.page.wait_for_timeout(wait)
        await _pass_challenge(self.page)
        return await self.page.evaluate("document.body.innerText")

    async def text(self):
        return await self.page.evaluate("document.body.innerText")

    async def shot(self, path, full_page=True):
        await self.page.screenshot(path=path, full_page=full_page)
        return path

    async def logged_in(self):
        t = await self.text()
        return ("Log In" not in t[:300]) or ("Wallet" in t)


class _Opener:
    """async context manager: launch, hand over the page, tear down"""

    def __init__(self, path="/", keep_open=False):
        self.path, self.keep_open, self._proc = path, keep_open, None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        relay_up()
        self._proc = launch_chrome(BASE + self.path)
        self._pw = await async_playwright().start()
        browser = await self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{DEBUG_PORT}")
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.set_viewport_size({"width": 1366, "height": 900})
        site = Site(browser, page, self._proc)
        await _pass_challenge(page)
        return site

    async def __aexit__(self, *exc):
        if not self.keep_open:
            try:
                await self._pw.stop()
            except Exception:
                pass
            if self._proc:
                stop_chrome(self._proc)


def open_site(path="/", keep_open=False):
    """async with open_site('/games/slots') as hb: ..."""
    return _Opener(path, keep_open)


# ── CLI ──────────────────────────────────────────────────────────────

async def _main():
    url, shot, want_text, serve = "/", None, False, False
    for i, a in enumerate(sys.argv):
        if a == "--url":
            url = sys.argv[i + 1]
        elif a == "--shot":
            shot = sys.argv[i + 1]
        elif a == "--text":
            want_text = True
        elif a == "--serve":
            serve = True

    async with open_site(url, keep_open=serve) as hb:
        t = await hb.text()
        print("url:", hb.page.url)
        print("logged in:", await hb.logged_in(), "| chars:", len(t))
        print("title:", (t.splitlines() or [""])[0][:80])
        if shot:
            print("shot:", await hb.shot(shot))
        if want_text:
            print("---")
            print(t)
        if serve:
            print("Chrome left running on 127.0.0.1:%d (profile %s)" % (DEBUG_PORT, PROFILE))
            print("attach: p.chromium.connect_over_cdp('http://127.0.0.1:%d')" % DEBUG_PORT)


if __name__ == "__main__":
    _reexec_under_xvfb()
    asyncio.run(_main())
