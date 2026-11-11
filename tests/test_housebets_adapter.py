"""Housebets adapter, offline: parsing recorded demo traffic and the fun-mode guard. No browser, no network."""
import asyncio
import json
import re
from pathlib import Path

import pytest

from hubie.games import housebets as hb
from hubie.games.base import GameError, NotDemoError
from hubie.orchestrator import build_adapter
from hubie.config import Config

FIX = Path(__file__).parent / "fixtures" / "pragmatic_sweet_bonanza_1000.json"
DEMO_FRAME = "https://demogamesfree.meiiwmlwqo.net/gs2c/html5Game.do?extGame=1&symbol=vs20fruitswx"
WALLET = "eth:0.00000000:0.00000000|usdt:0.12345678:0.00000000"
DEMO_LAUNCH = "https://demogamesfree.meiiwmlwqo.net/gs2c/openGame.do?gameSymbol=vs20fruitswx"


def good_state(**kw):
    s = hb.DemoState(radios={"real": False, "fun": True}, launch_modes=["fun"], launch_urls=[DEMO_LAUNCH],
                     frame_url=DEMO_FRAME, demo_balance=100000.0,
                     real_balance_start=WALLET, real_balance_now=WALLET)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# ── parsing ──

def test_recorded_rounds_parse_to_real_results():
    ex = json.loads(FIX.read_text())["exchanges"]
    p = hb.PragmaticRound()
    rounds = [r for r in (p.feed(q, a) for q, a in ex) if r]
    assert p.init_balance == 100000.0
    assert [r["bet"] for r in rounds] == [2.0, 2.0, 2.0]
    assert [r["payout"] for r in rounds] == [0.0, 0.0, 2.3]
    assert rounds[2]["multiplier"] == 1.15 and rounds[2]["tumbles"] == 2
    assert rounds[2]["balance"] == 99996.3
    assert not p.busy


def test_tumble_waits_for_collect():
    ex = json.loads(FIX.read_text())["exchanges"]
    p = hb.PragmaticRound()
    for q, a in ex[:6]:
        p.feed(q, a)
    assert p.busy                       # na=c: the win is not paid yet
    assert p.feed(*ex[6])["payout"] == 2.3


def test_free_spins_are_one_round():
    p = hb.PragmaticRound()
    p.feed("action=doInit", "balance=100,000.00&na=s")
    assert p.feed("action=doSpin&c=0.1&l=20", "tw=0.00&balance=99,998.00&na=s&c=0.10&l=20&fs=1&fsmax=10") is None
    assert p.feed("action=doSpin", "tw=5.00&balance=99,998.00&na=s&fs=2&fsmax=10&fswin=5.00") is None
    assert p.feed("action=doSpin", "tw=12.00&balance=99,998.00&na=c&fs_total=10&fswin_total=12.00") is None
    r = p.feed("action=doCollect", "balance=100,010.00&na=s")
    assert r["free_spins"] and r["free_spins_count"] == 10
    assert r["payout"] == 12.0 and r["multiplier"] == 6.0


def test_parse_kv_keeps_commas():
    assert hb.parse_kv("balance=99,998.00&s=1,2,3")["balance"] == "99,998.00"
    assert hb.num("99,998.00") == 99998.0


# ── the guard ──

def test_guard_ok_only_when_everything_says_demo():
    assert hb.demo_verdict(good_state()) == ("ok", [])


@pytest.mark.parametrize("change", [
    {"radios": {"real": True, "fun": False}},
    {"launch_modes": ["fun", "real"]},
    {"launch_modes": ["unreadable"]},
    {"real_balance_now": "eth:0.00000000:0.00000000|usdt:0.17580394:0.00000000"},
    {"frame_url": "https://gamesrealmoney.example.net/gs2c/html5Game.do"},
    {"launch_urls": ["https://real.pragmatic.example/openGame.do"]},
])
def test_guard_stops_for_good_on_real_evidence(change):
    verdict, why = hb.demo_verdict(good_state(**change))
    assert verdict == "real" and why


@pytest.mark.parametrize("change", [
    {"radios": {}},
    {"radios": {"real": False, "fun": False}},
    {"launch_modes": []},
    {"frame_url": ""},
    {"demo_balance": None},
    {"demo_balance": 0.12345678},
    {"demo_balance": 50.0},
    {"real_balance_now": None},
])
def test_guard_refuses_when_unsure(change):
    verdict, why = hb.demo_verdict(good_state(**change))
    assert verdict == "unsure" and why


def test_wallet_fingerprint_reads_cash_and_bonus():
    body = json.dumps({"currencies": [{"currency": "usdt", "cash_balance": "0.12345678", "bonus_balance": "0",
                                       "playable_balance_raw": "0.1234567800001"},
                                      {"currency": "eth", "cash_balance": "0.00000000", "bonus_balance": "0"}]})
    assert hb.wallet_fingerprint(body) == "eth:0.00000000:0|usdt:0.12345678:0"
    assert hb.wallet_fingerprint("not json") is None


def test_launch_call_matching():
    assert hb.is_launch_call("POST", "https://www.housebets.com/api/v1/casino/launch")
    assert not hb.is_launch_call("GET", "https://www.housebets.com/api/v1/casino/launch/currencies?slug=x")
    assert not hb.is_launch_call("POST", "https://www.housebets.com/api/v1/casino/launch/currencies")


def test_demo_hosts():
    assert hb.demo_host_ok("pragmatic", DEMO_FRAME)
    assert not hb.demo_host_ok("pragmatic", "https://demogamesfree.evil.com.attacker.io/x")
    assert not hb.demo_host_ok("pragmatic", "https://games.pragmaticplay.net/gs2c/html5Game.do")
    assert not hb.demo_host_ok("hacksaw", DEMO_FRAME)       # unknown provider is never demo


@pytest.mark.parametrize("label", ["Real Play", "Wallet", "Deposit", "Buy Free Spins", "Log Out",
                                   "Switch and launch", "Start your entry", "real play", "Try fun mode "])
def test_only_fun_mode_can_be_clicked(label):
    with pytest.raises(NotDemoError):
        hb.assert_clickable(label)


def test_fun_mode_label_is_clickable():
    hb.assert_clickable("Try fun mode")
    hb.assert_clickable("Reject")


def test_source_never_targets_real_play():
    src = Path(hb.__file__).read_text()
    clicks = re.findall(r"_click_label\(\s*\"([^\"]+)\"", src)
    assert set(clicks) <= set(hb.CLICKABLE_LABELS)
    assert "get_by_role(\"button\", name=label" in src     # all site clicks go through _click_label
    # the second lookup is _hover_label (browsing): the same allow-list, a mouse move, never a click
    assert src.count("get_by_role(") == 2
    hover = src[src.index("async def _hover_label"):src.index("async def _browse_to")]
    assert "assert_clickable(label)" in hover and ".click(" not in hover


# ── spin path refuses without verified demo (no browser: page stubbed) ──

class FakePage:
    def __init__(self, radios, balance="0.12345678", frames=()):
        self.radios, self.balance = radios, balance
        self.frames = list(frames)
        self.main_frame = object()
        self.clicked = []
        self.mouse = self
        self.keyboard = self
        self.context = FakeContext()

    async def evaluate(self, js):
        if "radiogroup" in js:
            return self.radios
        if "innerText" in js and "decimal" not in js and "header" not in js:
            return "Sweet Bonanza 1000"
        return self.balance

    async def click(self, x, y):
        self.clicked.append((x, y))

    async def press(self, k):
        self.clicked.append(k)


class FakeContext:
    def __init__(self):
        self.routes = []
        self.log = []

    async def route(self, pattern, handler):
        self.routes.append(handler)
        self.log.append("route")

    async def unroute(self, pattern, handler):
        if handler in self.routes:
            self.routes.remove(handler)
        self.log.append("unroute")


class FakeFrame:
    url = DEMO_FRAME


def adapter_with(page, state, wallet_now=WALLET):
    a = hb.HousebetsSlotsAdapter.__new__(hb.HousebetsSlotsAdapter)
    a._wallet_first, a._wallet_latest = WALLET, wallet_now
    a.current, a._page, a.state = hb.BY_ID["sweet_bonanza_1000"], page, state
    a._hard_stop, a._exchanges, a._round_q = [], [], []
    a.parser = hb.PragmaticRound()
    a._lock = asyncio.Lock()
    a.round_timeout, a._n, a.shots_dir = 30, 0, None
    a.bonus_every, a.force_bonus_buy, a._buys_off, a._next_buy = 0, False, set(), float("inf")
    a._buy_armed, a._buy_seen, a.buys_done, a.record = None, [], 0, None
    a.allow_unverified_buys = False
    return a


def test_spin_refused_when_toggle_on_real():
    page = FakePage({"real": True, "fun": False}, frames=[FakeFrame()])
    a = adapter_with(page, good_state())
    with pytest.raises(NotDemoError):
        asyncio.run(a._play())
    assert page.clicked == []                 # never pressed spin
    assert not a.is_demo()                    # and stays stopped


def test_spin_refused_when_demo_balance_missing():
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = adapter_with(page, good_state(demo_balance=None))
    with pytest.raises(GameError):
        asyncio.run(a._play())
    assert page.clicked == []
    assert a.is_demo()                        # unsure is not real: recover, don't end the show


def test_spin_refused_when_real_balance_moved():
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = adapter_with(page, good_state(), wallet_now="eth:0.00000000:0.00000000|usdt:0.10000000:0.00000000")
    with pytest.raises(NotDemoError):
        asyncio.run(a._play())
    assert page.clicked == []


def test_spin_goes_ahead_when_demo_verified():
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = adapter_with(page, good_state())
    ex = json.loads(FIX.read_text())["exchanges"]
    a.parser.feed(*ex[0])

    async def run():
        task = asyncio.ensure_future(a._play())
        await asyncio.sleep(0.1)
        assert page.clicked == [(990, 650)]
        a._exchanges.append(ex[1])
        a._round_q.append(a.parser.feed(*ex[1]))
        page.wait_for_timeout = lambda ms: asyncio.sleep(0)
        return await task

    page.wait_for_timeout = lambda ms: asyncio.sleep(0)
    r = asyncio.run(run())
    assert r.is_demo and r.bet == 2.0 and r.payout == 0.0 and r.balance == 99998.0
    assert r.game_id == "sweet_bonanza_1000" and r.detail["provider"] == "Pragmatic Play"


# ── wiring ──

def test_default_list_is_verified_games_only():
    a = hb.HousebetsSlotsAdapter.__new__(hb.HousebetsSlotsAdapter)
    hb.HousebetsSlotsAdapter.__init__(a, originals=[])
    try:
        assert [g.id for g in a.list_games()] == [g.id for g in hb.GAMES if g.verified]
        assert all(g.kind == "slot" for g in a.list_games())
        with pytest.raises(GameError):
            a.load_game("mines")          # originals off: not a game here
    finally:
        a._L.loop.call_soon_threadsafe(a._L.loop.stop)


def test_env_selects_adapter(monkeypatch):
    monkeypatch.setenv("HUBIE_GAME", "housebets")
    monkeypatch.setenv("HUBIE_HB_GAMES", "sweet_bonanza_1000,gates_of_olympus_1000")
    monkeypatch.setenv("HUBIE_HB_UNVERIFIED", "1")
    a = build_adapter(Config())
    try:
        assert isinstance(a, hb.HousebetsSlotsAdapter) and a.round_timeout > 45
        assert [g.id for g in a.list_games() if g.kind == "slot"] == ["sweet_bonanza_1000", "gates_of_olympus_1000"]
        assert [g.id for g in a.list_games() if g.kind == "original"] == [g.id for g in hb.ORIGINALS]
    finally:
        a._L.loop.call_soon_threadsafe(a._L.loop.stop)


def test_scheduler_rotates_through_slots():
    from hubie.brain.memory import Memory
    from hubie.brain.scheduler import Scheduler
    a = hb.HousebetsSlotsAdapter.__new__(hb.HousebetsSlotsAdapter)
    hb.HousebetsSlotsAdapter.__init__(a)
    try:
        games = a.list_games()
        assert len(games) >= 4
        sched = Scheduler(Memory(None), ["x"], seed=7)
        picks = []
        for _ in range(12):
            gid = sched._pick_game(games)
            sched.state["recent_games"].append(gid)
            picks.append(gid)
        assert all(picks[i] not in picks[max(0, i - 3):i] for i in range(len(picks)))   # no repeat within 3
        assert len(set(picks)) >= 4
    finally:
        a._L.loop.call_soon_threadsafe(a._L.loop.stop)


# ── demo bonus buys ──

BUY_FIX = Path(__file__).parent / "fixtures" / "pragmatic_sweet_bonanza_1000_buy.json"


def test_pur_init_and_cheapest_buy():
    assert hb.parse_pur_init('[{bet:2000,type:"default"},{bet:10000,type:"default"}]') == [2000.0, 10000.0]
    assert hb.parse_pur_init('[{type:"fsbl",bet:2000,bet_level:0}]') == [2000.0]
    assert hb.parse_pur_init(None) == []
    p = hb.PragmaticRound()
    p.feed("action=doInit", 'balance=100,000.00&c=0.10&l=20&purInit=[{bet:10000,type:"default"},{bet:2000,type:"default"}]')
    assert p.cheapest_buy() == (1, 200.0)        # cheapest, never the SUPER one, whatever the order


def test_recorded_buy_parses_to_one_feature_round():
    ex = json.loads(BUY_FIX.read_text())["exchanges"]
    p = hb.PragmaticRound()
    rounds = [r for r in (p.feed(q, a) for q, a in ex) if r]
    assert len(rounds) == 1 and not p.busy
    r = rounds[0]
    assert r["bonus_buy"] and r["pur"] == 0
    assert r["bet"] == r["buy_cost"] == r["buy_paid"] == r["buy_expected"] == 200.0
    assert r["base_bet"] == 2.0
    assert r["free_spins"] and r["free_spins_count"] == 15          # 10 bought + 5 retriggered
    assert r["payout"] == 153.9 and r["multiplier"] == 0.77 and r["balance"] == 99953.9


def test_money_values_read_ocr_noise():
    assert hb.money_values("S2OO.O0") == [200.0]
    assert hb.money_values("$1,000.00") == [1000.0]
    assert hb.money_values("$100,000.00\n") == [100000.0]
    assert hb.money_values("$200,00") == [200.0]
    assert hb.money_values("BUY FREE SPINS") == []


@pytest.mark.parametrize("label", ["BUY SUPER FREE SPINS", "BUY FREE SPINS ", "buy free spins", "DOUBLE CHANCE",
                                   "ANTE BET", "Try fun mode", "Real Play", "BUY CONFIRM SUPER", "BUY BONUS"])
def test_game_allow_list_refuses_everything_else(label):
    with pytest.raises(NotDemoError):
        hb.assert_game_clickable(label)


def test_game_allow_list_is_buy_and_confirm_only():
    assert hb.GAME_CLICKS == ("BUY FREE SPINS", "BUY CONFIRM")
    for label in hb.GAME_CLICKS:
        hb.assert_game_clickable(label)
    with pytest.raises(NotDemoError):
        hb.assert_clickable("BUY FREE SPINS")        # never a site button


def test_source_buy_clicks_only_go_through_the_allow_list():
    src = Path(hb.__file__).read_text()
    labels = re.findall(r"_game_click\(\s*\"([^\"]+)\"", src)
    assert labels and set(labels) <= set(hb.GAME_CLICKS)
    # mouse clicks: spin button, intro, the one inside _game_click, browsing's tile click (the line right after
    # its own allow-list check) and the stuck-spin dismiss click on the button-free top banner
    # (the house originals' own two clicks sit in their section, checked in test_house_originals.py)
    orig = src[src.index("    # ── house originals, zero bet ──"):src.index("    async def _recover(self):")]
    assert orig.count("mouse.click(") == 2
    assert src.count("mouse.click(") - orig.count("mouse.click(") == 5
    assert src.count("mouse.click(*self.view.pt(DISMISS_CLICK))") == 1 and hb.DISMISS_CLICK[1] <= 140
    browse = src[src.index("async def _browse_to"):src.index("async def _fail_shot")]
    assert browse.count("mouse.click(") == 1
    assert "assert_tile_clickable(path, listed)\n        await page.mouse.click(cx, cy)" in browse
    assert "listed = [*self.games, *self.originals]" in browse
    assert "super" not in " ".join(labels).lower()


def test_buy_specs_only_on_pragmatic_games_with_a_buy():
    with_buy = {g.id for g in hb.GAMES if g.buy}
    assert with_buy == {"sweet_bonanza_1000", "gates_of_olympus_1000", "sugar_rush_1000"}
    for g in hb.GAMES:
        if g.buy:
            assert g.buy.confirm and g.buy.dialog_text_box and g.buy.dialog_price_box and g.buy.times_bet == 100
    assert {g.id for g in hb.GAMES if g.buy and g.buy.verified} == {"sweet_bonanza_1000", "gates_of_olympus_1000"}


# purchase check in flight

class FakeRoute:
    def __init__(self, body, url=DEMO_FRAME.replace("html5Game.do", "gameService")):
        self.request = type("R", (), {"post_data": body, "url": url})()
        self.result = None

    async def continue_(self):
        self.result = "continue"

    async def abort(self):
        self.result = "abort"


def run_route(a, body, url=None):
    r = FakeRoute(body) if url is None else FakeRoute(body, url)
    asyncio.run(a._route_game(r))
    return r.result


def test_route_blocks_unarmed_and_wrong_purchases():
    a = adapter_with(FakePage({"real": False, "fun": True}), good_state())
    assert run_route(a, "action=doSpin&c=0.1&l=20&bl=0") == "continue"           # normal spins untouched
    assert run_route(a, "action=doSpin&c=0.1&l=20&pur=0") == "abort"             # nobody armed a buy
    assert "sweet_bonanza_1000" in a._buys_off and a.buys_done == 0
    a._buy_armed = 0
    assert run_route(a, "action=doSpin&c=0.1&l=20&pur=1") == "abort"             # SUPER is index 1
    a._buy_armed = 0
    assert run_route(a, "action=doSpin&c=0.1&l=20&pur=0&bl=1") == "abort"        # ante on
    assert a.is_demo()


def test_route_lets_exactly_one_armed_buy_through():
    a = adapter_with(FakePage({"real": False, "fun": True}), good_state())
    a._buy_armed = 0
    assert run_route(a, "action=doSpin&c=0.1&l=20&pur=0&bl=0") == "continue"
    assert a.buys_done == 1 and a._buy_armed is None
    assert run_route(a, "action=doSpin&c=0.1&l=20&pur=0&bl=0") == "abort"       # a second one is not armed


def test_route_purchase_to_real_host_stops_for_good():
    a = adapter_with(FakePage({"real": False, "fun": True}), good_state())
    a._buy_armed = 0
    assert run_route(a, "action=doSpin&pur=0", "https://games.pragmaticplay.net/gs2c/gameService") == "abort"
    assert not a.is_demo()


# the guard runs before a buy exactly as before a spin

def buy_adapter(page, state, wallet_now=WALLET):
    a = adapter_with(page, state, wallet_now)
    a.force_bonus_buy = True
    a.parser.feed(*json.loads(BUY_FIX.read_text())["exchanges"][0])
    page.wait_for_timeout = lambda ms: asyncio.sleep(0)
    return a


def test_buy_refused_when_toggle_on_real():
    page = FakePage({"real": True, "fun": False}, frames=[FakeFrame()])
    a = buy_adapter(page, good_state())
    with pytest.raises(NotDemoError):
        asyncio.run(a._play())
    assert page.clicked == []


def test_buy_refused_when_real_balance_moved():
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = buy_adapter(page, good_state(), wallet_now="eth:0.00000000:0.00000000|usdt:0.10000000:0.00000000")
    with pytest.raises(NotDemoError):
        asyncio.run(a._play())
    assert page.clicked == []


def test_buy_refused_without_demo_balance():
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = buy_adapter(page, good_state(demo_balance=None))
    with pytest.raises(GameError):
        asyncio.run(a._play())
    assert page.clicked == []


def test_buy_skipped_when_credit_on_screen_is_not_demo(monkeypatch):
    """Credit unreadable or different: nothing bought, the round is a normal spin."""
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = buy_adapter(page, good_state())

    async def credit_says_no(box, amount):
        return False, ["$0.18"]
    a._screen_shows = credit_says_no

    async def run():
        task = asyncio.ensure_future(a._play())
        await asyncio.sleep(0.1)
        assert page.clicked == [(990, 650)]           # the spin button, never the buy button
        ex = json.loads(FIX.read_text())["exchanges"]
        a._exchanges.append(ex[1])
        a._round_q.append({"bet": 2.0, "payout": 0.0, "balance": 99998.0, "multiplier": 0.0, "tumbles": 0,
                           "free_spins": False, "free_spins_count": 0, "bonus_buy": False})
        return await task
    r = asyncio.run(run())
    assert "bonus_buy" not in r.detail and a.buys_done == 0


def test_buy_answered_with_real_looking_balance_stops_for_good(monkeypatch):
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = buy_adapter(page, good_state())

    async def yes(box, amount):
        return True, []

    async def grab(box):
        return b""
    a._screen_shows, a._grab = yes, grab
    monkeypatch.setattr(hb, "dialog_is_standard_buy", lambda png: (True, []))

    async def run():
        task = asyncio.ensure_future(a._play())
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(page.clicked) == 2:
                break
        assert page.clicked == [hb.BY_ID["sweet_bonanza_1000"].buy.button, hb.BY_ID["sweet_bonanza_1000"].buy.confirm]
        a._exchanges.append(("action=doSpin&c=0.1&l=20&pur=0", "balance=0.18&na=s&fs=1&fsmax=10"))
        return await task
    with pytest.raises(NotDemoError):
        asyncio.run(run())
    assert not a.is_demo()
    assert page.context.routes == [] and a._buy_armed is None       # check removed, nothing left armed


def test_buy_goes_ahead_and_reports_the_feature(monkeypatch):
    page = FakePage({"real": False, "fun": True}, frames=[FakeFrame()])
    a = buy_adapter(page, good_state())
    checked = []

    async def yes(box, amount):
        checked.append(amount)
        return True, []

    async def grab(box):
        return b""
    a._screen_shows, a._grab = yes, grab
    monkeypatch.setattr(hb, "dialog_is_standard_buy", lambda png: (True, []))
    ex = json.loads(BUY_FIX.read_text())["exchanges"]

    async def run():
        task = asyncio.ensure_future(a._play())
        for _ in range(50):
            await asyncio.sleep(0.02)
            if len(page.clicked) == 2:
                break
        for q, r in ex[1:]:
            a._exchanges.append((q, r))
            done = a.parser.feed(q, r)
            if done:
                a._round_q.append(done)
        return await task
    r = asyncio.run(run())
    assert checked[:3] == [100000.0, 200.0, 200.0]          # credit, button price, dialog price
    assert 99800.0 in checked                                # cost came off the credit shown
    assert r.bet == 200.0 and r.payout == 153.9 and r.multiplier == 0.77
    assert r.detail["bonus_buy"] is True and r.detail["buy_cost_demo"] == 200.0
    assert r.detail["feature_win_demo"] == 153.9 and r.detail["feature_multiplier"] == 0.77
    assert r.detail["free_spins_triggered"] and r.detail["free_spins_count"] == 15
    assert not a.force_bonus_buy
    assert page.context.log[0] == "route" and page.context.routes == []      # check only around the buy


def test_buy_timing(monkeypatch):
    monkeypatch.setenv("HUBIE_HB_BONUS_BUY_MIN", "0")
    assert hb.bonus_every_s() == 0
    monkeypatch.setenv("HUBIE_HB_BONUS_BUY_MIN", "20")
    assert hb.bonus_every_s() == 1200
    a = adapter_with(FakePage({}), good_state())
    g = hb.BY_ID["sweet_bonanza_1000"]
    assert not a._buy_due(g)                                  # bonus_every 0: never
    a.bonus_every = 1200
    gaps = [a._buy_gap() for _ in range(200)]
    assert 1080 <= min(gaps) and max(gaps) <= 1380           # about 20 minutes, a little jitter
    a._next_buy = 0
    assert a._buy_due(g) and not a._buy_due(hb.BY_ID["fruit_party"])    # no buy button there: just spin
    a._buys_off.add(g.id)
    assert not a._buy_due(g)
    sr = hb.BY_ID["sugar_rush_1000"]
    assert not a._buy_due(sr)                       # mapped but not verified live: the show skips it
    a.allow_unverified_buys = True
    assert a._buy_due(sr)


def test_wallet_only_grew_rebaselines_credits_but_not_drops():
    from hubie.games.housebets import wallet_only_grew as w
    a = "eth:0.00000000:0.00000000|usdt:0.12345678:0.00000000"
    assert w(a, a.replace("0.12345678", "20.31334796"))           # a gift / rakeback
    assert not w(a, a.replace("0.12345678", "0.12000000"))        # money left: still a hard stop
    assert not w(a, a)
    assert not w(a, "eth:0.00000000:0.00000000|usdt:20.0:0.0|btc:1:0")   # new currency
    assert not w(a, "eth:0.0:0.0|usdt:30.0:-1.0")                 # bonus dropped
    assert not w(a, "junk")
