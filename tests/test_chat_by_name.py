"""Contest rule "reads chat and answers by name": every message that isn't blocked gets a spoken reply naming the
viewer, in arrival order, batched when chat is busy, never dropped; every round still gets its line. Plus the
on-screen chat list (layout 1): what's shown, what isn't, his replies as gold rows."""
import asyncio
import json
import re
import time

from hubie.brain.provider import LLMRequest, RoutedProvider, build_anthropic
from hubie.brain.scheduler import PLAY, Segment
from hubie.orchestrator import BATCH_AT, ChatQueue, QueuedChat
from hubie.safety.moderation import ChatModerator
from hubie.chat.base import ChatMessage

from test_orchestrator import FakeSpeaker, TimedSpeaker, build, run

NAMES = ["mina_k", "tiltedtoby", "devo", "Kokax", "3amgrinder", "lena_k", "pixel_pia", "ozzy88x", "dr_nap",
         "velvet_vic", "quiet_quinn", "tea_and_toast", "branch_manager", "slowpoke_sam", "mossy_mike", "NightOwl"]


def q(user, text="hi", action="pass", ts=0.0):
    return QueuedChat(user, user, text, ts, action, [f"id-{user}-{text}"])


def spoken_name(user):
    return ChatModerator().review(ChatMessage(user, "hello", 0.0, "x")).speak_name


def play(orch):
    orch.games()
    orch.segment = Segment(PLAY, "slots", target=10_000, game_id="max_win")


# ---- the queue ----

def test_queue_keeps_arrival_order_merges_a_viewers_messages_and_drops_nothing():
    cq = ChatQueue()
    for u, t in [("a", "1"), ("b", "2"), ("a", "3"), ("c", "4"), ("b", "5")]:
        cq.add(q(u, t))
    first = cq.pop()
    assert (first.username, first.text, first.count) == ("a", "1 / 3", 2) and len(first.ids) == 2
    assert [(x.username, x.text) for x in [cq.pop(), cq.pop()]] == [("b", "2 / 5"), ("c", "4")]
    assert cq.pop() is None
    old = q("z", "very old", ts=-10_000)          # no age limit any more: an old message is still answered
    cq.add(old)
    assert cq.pop() is old


def test_urgent_first_and_fixed_lines_are_never_batched():
    cq = ChatQueue()
    cq.add(q("a"))
    cq.add(q("b", "pick for me", "pick"))
    cq.add(q("c"))
    cq.add(q("d", "i lost my rent", "support"))
    assert cq.pop_batch(4) == []                  # urgent waiting: it goes alone, first
    assert cq.pop().action == "support"
    batch = cq.pop_batch(4)
    assert [x.username for x in batch] == ["a", "c"]
    assert cq.pop().action == "pick"


# ---- every message answered by name ----

def _all_named(sp_lines, users):
    said = " ".join(t.lower() for k, t in sp_lines if k == "chat")
    return [u for u in users if spoken_name(u).lower() not in said]


def test_a_quiet_chat_gets_one_reply_per_message_in_arrival_order_by_name():
    script = [(u, f"question {i} about the hat") for i, u in enumerate(NAMES[:3])]
    orch, sp, clock = build(script=script)
    play(orch)
    run(orch, clock, 3, dt=1.0)
    replies = [t for k, t in sp.lines if k == "chat"]
    assert len(replies) == 3
    for r, u in zip(replies, NAMES[:3]):
        assert spoken_name(u).lower() in r.lower()
    assert orch.counts["answered"] == 3 and orch.counts["batches"] == 0


def test_heavy_chat_is_batched_and_every_name_is_answered_while_every_round_gets_its_line():
    events = []
    orch, sp, clock = build(notify=events.append)
    play(orch)
    users, n = [], 0
    for wave in range(6):                          # bursts of 10 viewers, more arriving while he answers
        for i in range(10):
            u = f"{NAMES[(n + i) % len(NAMES)]}{wave}"
            users.append(u)
            orch.chat.push(u, f"yo hubie, message {n + i} about the desk")
        n += 10
        run(orch, clock, 8, dt=2.0)
    run(orch, clock, 40, dt=2.0)
    assert len(orch.chatq) == 0
    assert _all_named(sp.lines, users) == []      # every single viewer heard their name
    assert orch.counts["answered"] == len(users)
    assert orch.counts["batches"] >= 3
    assert sum(1 for k, _ in sp.lines if k == "round") == orch.counts["rounds"] > 5   # no round skipped
    assert orch.counts.get("busy_short", 0) >= 0
    shown = [e for e in events if e["type"] == "chat"]
    assert len(shown) == len(users)               # all ordinary messages went into the on-screen list


def test_batched_line_names_everyone_and_tells_the_stage_who():
    orch, sp, clock = build()
    play(orch)
    for u in NAMES[:BATCH_AT + 1]:
        orch.chat.push(u, "hello hubie")
    orch.ingest_chat()
    assert orch.answer_next()
    line = sp.lines[-1][1].lower()
    names = sp.replies[-1]["names"]
    assert len(names) >= 2
    assert all(n.lower() in line for n in names)


def test_long_queue_uses_a_roll_call_with_no_model_call():
    orch, sp, clock = build()
    play(orch)
    for u in NAMES[:12]:
        orch.chat.push(u, "hello hubie how are you")
    orch.ingest_chat()
    calls = orch.brain.provider.calls
    assert orch.answer_next()
    assert orch.brain.provider.calls == calls
    line = sp.lines[-1][1].lower()
    assert len(sp.replies[-1]["names"]) == 8 and all(n.lower() in line for n in sp.replies[-1]["names"])


def test_a_viewer_with_several_messages_gets_one_reply_answering_all_of_them():
    orch, sp, clock = build(script=[("devo", "gg"), ("devo", "what game next"), ("devo", "nice hat")])
    play(orch)
    out = run(orch, clock, 2, dt=1.0)
    assert out == ["chat", "round"]
    assert orch.counts["answered"] == 3 and "devo" in sp.lines[0][1].lower()


def test_names_keep_their_spelling_in_a_batch():
    orch, sp, clock = build()
    from hubie.brain.brain import Brain
    line = orch.brain.reply_batch([("Kokax", "wsp", "kokax"), ("DeVo", "gg", "devo")], rollcall=True)
    assert "Kokax" in line.text and "DeVo" in line.text
    assert Brain.names_text(["a", "b", "c"]) == "a, b and c"


def test_model_flagging_someone_in_a_batch_falls_back_to_one_by_one():
    orch, sp, clock = build()
    play(orch)
    real = orch.brain.provider.complete
    orch.brain.provider.complete = lambda req: "hubie_crisis" if "Chat is busy" in req.prompt else real(req)
    for u in NAMES[:BATCH_AT]:
        orch.chat.push(u, "hello there hubie")
    orch.ingest_chat()
    orch.answer_next()
    assert orch.counts["batches"] == 0
    assert len(orch.chatq) == BATCH_AT - 1 and all(x.solo for x in orch.chatq.normal)


def test_a_failed_voice_puts_the_message_back_instead_of_dropping_it():
    orch, sp, clock = build(script=[("devo", "hello hubie")])
    play(orch)
    sp.say = lambda *a, **k: False
    orch.ingest_chat()
    orch.answer_next()
    assert len(orch.chatq) == 1 and orch.counts["answered"] == 0


def test_busy_chat_makes_a_notable_round_short_but_never_skips_it():
    from hubie.games.base import RoundResult
    orch, sp, clock = build()
    play(orch)
    for u in NAMES[:3]:
        orch.chatq.add(q(u))
    orch.adapter.play_round = lambda bet, opts=None: RoundResult("max_win", "a", 1.0, 2.5, 2.5, 1000, True, {})
    orch.play_one(orch.segment)
    assert [k for k, _ in sp.lines] == ["round"] and orch.counts["busy_short"] == 1
    assert 2 <= len(sp.lines[0][1].split()) <= 6
    orch.adapter.play_round = lambda bet, opts=None: RoundResult("max_win", "b", 1.0, 15.0, 15.0, 1000, True, {})
    orch.play_one(orch.segment)                    # a big one keeps its full line even when chat is busy
    assert orch.counts["busy_short"] == 1 and len(sp.lines) == 2


def test_pacing_holds_with_a_busy_chat_no_stacking_and_every_round_commented():
    clock = [1_000_000.0]
    orch, _, clock = build(per_minute=20, now=clock)
    sp = TimedSpeaker(clock)
    orch.speaker = sp
    run(orch, clock, 4000, dt=0.25)
    assert sum(1 for x in sp.log if x[2] == "round") == orch.counts["rounds"] > 10
    assert orch.counts["chat_replies"] > 10


# ---- moderation and crisis stay as they were, and stay off the screen ----

def test_blocked_messages_are_not_shown_or_answered():
    events = []
    orch, sp, clock = build(notify=events.append, script=[
        ("spam_bot", "check out my channel www.spam-example.xyz"), ("devo", "nice suit hubie")])
    play(orch)
    run(orch, clock, 3, dt=1.0)
    shown = [e for e in events if e["type"] == "chat"]
    assert [e["name"] for e in shown] == ["devo"]
    spoken = " ".join(t for _, t in sp.lines).lower()
    assert "spam" not in spoken and "devo" in spoken
    assert orch.counts["ignored"] == 1


def test_crisis_message_and_its_reply_are_never_on_screen():
    events = []
    orch, sp, clock = build(notify=events.append, script=[("dara_k", "dont want to live like this")])
    assert orch.step() == "urgent"
    assert not [e for e in events if e["type"] == "chat"]
    assert sp.replies[-1] is None                 # no reply_to: the stage keeps it out of the chat list
    orch.chat.push("dara_k", "tell me a joke")    # their follow-ups route to the crisis line and stay off too
    orch.quiet_until = 0
    orch.step()
    assert not [e for e in events if e["type"] == "chat"] and sp.replies[-1] is None


def test_model_caught_crisis_takes_the_message_back_off_the_screen():
    events = []
    orch, sp, clock = build(notify=events.append, script=[("mira_k", "honestly whats even the use anymore")])
    play(orch)
    orch.brain.provider.complete = lambda req: "hubie_crisis"
    run(orch, clock, 1, dt=1.0)
    cid = next(e["id"] for e in events if e["type"] == "chat")
    assert {"type": "chat_hide", "id": cid} in events
    assert sp.replies[-1] is None


def test_fixed_line_triggers_are_shown_and_answered_by_name():
    events = []
    orch, sp, clock = build(notify=events.append, script=[("tiltedtoby", "pick a tile for me")])
    play(orch)
    run(orch, clock, 1, dt=1.0)
    assert [e["text"] for e in events if e["type"] == "chat"] == ["pick a tile for me"]
    assert sp.replies[-1] == {"names": ["tiltedtoby"]}
    assert "tiltedtoby" in sp.lines[-1][1].lower()


# ---- cheap model for busy chat ----

def test_busy_chat_routes_to_haiku_when_chat_runs_on_opus():
    p = build_anthropic({"HUBIE_ANTHROPIC_MODEL": "claude-opus-5-5", "HUBIE_MODEL_ROUND": "claude-haiku-5-5",
                         "ANTHROPIC_API_KEY": "x"})
    assert isinstance(p, RoutedProvider)
    assert p.routes["chat_fast"].model == "claude-haiku-5-5"
    picked = []
    for k, prov in list(p.routes.items()) + [("default", p.default)]:
        prov.complete = (lambda k: lambda req: picked.append(k) or "ok")(k)
    p.complete(LLMRequest("s", "p", kind="chat"))
    p.complete(LLMRequest("s", "p", kind="chat", route="chat_fast"))
    assert picked == ["default", "chat_fast"]
    p2 = build_anthropic({"HUBIE_ANTHROPIC_MODEL": "claude-sonnet-5-5", "ANTHROPIC_API_KEY": "x"})
    assert not isinstance(p2, RoutedProvider)     # not Opus: no extra route, same model


def test_a_long_queue_sends_single_replies_to_the_fast_route():
    orch, sp, clock = build()
    play(orch)
    routes = []
    real = orch.brain.provider.complete
    orch.brain.provider.complete = lambda req: routes.append(req.route) or real(req)
    for u in NAMES[:3]:
        orch.chatq.add(q(u, "hello hubie"))
    orch.answer_next()                             # 2 still waiting after this one
    orch.answer_next()
    assert routes[0] == "chat_fast" and routes[-1] == ""


# ---- the on-screen chat list ----

def test_chat_list_rows_wrap_cap_and_colours_are_stable(tmp_path):
    from hubie.stage.desk import CHAT_MAX_LINES, Geo, NAME_COLOURS, chat_rows, draw_chat, draw_chrome, name_colour
    g = Geo(1280, 720)
    rows = chat_rows(g, "3amgrinder", "word " * 120, 360)
    assert len(rows) == CHAT_MAX_LINES and rows[-1].endswith("...")
    assert name_colour("Devo") == name_colour("devo") in NAME_COLOURS
    assert len({name_colour(n) for n in NAMES}) >= 5
    img = draw_chat(g, draw_chrome(g), [{"name": "devo", "user": "devo", "text": "gg"}, {"hubie": True, "text": "ty devo"}])
    assert img.size == g.chat[2:]


def test_layout_one_right_column_cam_top_chat_middle_stats_bottom():
    from hubie.stage.desk import Geo
    for w, h in ((1280, 720), (1920, 1080)):
        g = Geo(w, h)
        cx, cy, cw, ch = g.cam
        x, y, cw2, chh = g.chat
        px, py, pw, ph = g.panel
        assert cy <= g.s(10) and cx >= g.game[2]
        assert cy + ch + g.frame_pad <= y and y + chh <= py and py + ph <= h - g.strip_h
        assert chh >= g.s(220)
        assert x >= g.game[2] and x + cw2 <= w


def test_desk_chat_list_follows_events(tmp_path, monkeypatch):
    from test_desk_scene import _cfg
    from hubie.stage.desk import DeskScene
    cfg = _cfg(tmp_path, monkeypatch, HUBIE_CAM_MODE="switch")

    async def go():
        s = DeskScene(cfg)
        s.link.send = lambda *a: None
        s.SUB_HOLD = 0.01
        f = cfg.scene_dir / "chat.png"
        b0 = f.read_bytes()
        s.apply({"type": "chat", "id": "c1", "name": "mina_k", "user": "mina_k", "text": "olympus again lol"})
        b1 = f.read_bytes()
        assert b1 != b0
        s.apply({"type": "speak", "text": "a round line", "duration": 0.05})      # not a chat reply: no row
        await asyncio.sleep(0.1)
        assert f.read_bytes() == b1
        s.apply({"type": "speak", "text": "mina, olympus is home.", "duration": 0.05, "reply_to": {"names": ["mina k"]}})
        await asyncio.sleep(0.1)
        chat = json.loads((cfg.scene_dir / "session.json").read_text())["chat"]
        assert [e.get("hubie", False) for e in chat] == [False, True]
        s.apply({"type": "chat_hide", "id": "c1"})
        chat = json.loads((cfg.scene_dir / "session.json").read_text())["chat"]
        assert len(chat) == 1 and chat[0]["hubie"]

    asyncio.run(go())


# ---- no invented stream features; long batched answers split in two ----

def test_a_reply_that_invents_a_vote_is_regenerated_once_then_the_pool():
    from hubie.brain.brain import INVENTED_FEATURE
    for bad in ["next game's chat's call in the vote, so keep it coming", "giveaway at 100 followers",
                "u pick the next game, devo", "we're running a poll later", "taking requests tonight"]:
        assert INVENTED_FEATURE.search(bad), bad
    for ok in ["olympus is home, mina k.", "pick ur own tile toby", "no idea what's next, the show picks"]:
        assert not INVENTED_FEATURE.search(ok), ok
    orch, sp, clock = build()
    answers = iter(["chat's call in the vote, devo.", "devo, the desk picks, i just sit here."])
    orch.brain.provider.complete = lambda req: next(answers)
    line = orch.brain.reply_chat("devo", "what game next", "devo")
    assert line.text.startswith("devo, the desk picks")
    orch.brain.provider.complete = lambda req: "chat decides the next game in a poll, devo."
    line = orch.brain.reply_chat("devo", "what game next", "devo")
    assert not INVENTED_FEATURE.search(line.text) and "devo" in line.text.lower()


def test_prompts_carry_the_no_invented_features_rule():
    from hubie.brain import prompts
    assert prompts.NO_FEATURES in prompts.chat_prompt("devo", "hi", 1, [])
    p = prompts.batch_chat_prompt([("devo", "what game next")], [], "the show picks the next game itself")
    assert prompts.NO_FEATURES in p and "short real answer" in p and "the show picks" in p


def test_a_long_batched_answer_is_said_as_two_lines_both_in_the_chat_list():
    orch, sp, clock = build()
    play(orch)
    long = ("yo kokax, mina k, devo and lena k. olympus again because it's comfy, mina k, and the banana stash "
            "stays classified. devo, no clue what's next, the show picks it. lena k, welcome in, pull up a chair.")
    orch.brain.provider.complete = lambda req: long
    for u in ["Kokax", "mina_k", "devo", "lena_k"]:
        orch.chat.push(u, "what game next?")
    out = run(orch, clock, 3, dt=1.0)
    assert out[:2] == ["chat", "followup"]
    a, b = sp.lines[0][1], sp.lines[1][1]
    assert len(a) <= 170 and len(b) <= 170 and (a + " " + b).lower().count("devo") >= 2
    assert sp.replies[0] == sp.replies[1] and len(sp.replies[0]["names"]) == 4
    assert orch.counts["answered"] == 4
