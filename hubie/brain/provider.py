"""LLM behind an interface. Mock is free and is what every test uses."""
from __future__ import annotations

import abc
import json
import logging
import os
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("hubie.provider")


@dataclass
class LLMRequest:
    system: str
    prompt: str
    kind: str = "line"
    facts: dict = field(default_factory=dict)
    max_tokens: int = 1500
    no_thinking: bool = False    # tight lines (plain-round shorts): thinking would eat the whole budget
    route: str = ""              # picks a model other than the kind's own when set and routed (chat_fast)


class ProviderError(Exception):
    pass


class LLMProvider(abc.ABC):
    name = "abstract"

    @abc.abstractmethod
    def complete(self, req: LLMRequest) -> str:
        """Return one spoken line. Raises ProviderError."""


class MockProvider(LLMProvider):
    """Deterministic combinatorial writer so the stack runs for free and tests can
    prove the anti-repeat logic against something with a finite vocabulary."""
    name = "mock"

    OPEN = ["ok.", "hm.", "right.", "well.", "so.", "look at that.", "there we go.", "alright.", "huh.", "and...",
            "mm.", "yep.", "see.", "wow.", "fine.", "nice.", "cool.", "sure."]
    WIN = ["tidy {m}x on {g}", "{g} coughs up {m} times", "{m}x and i'm almost alert", "{m} times, in chips that buy nothing",
           "slow clap for {m}x", "{g} pays, {m}x", "{m}x from {g}, not bad"]
    LOSE = ["{g} said no", "nothing doing on {g}", "that one left without saying bye", "clean zero on {g}",
            "{g} took it", "gone, on {g}", "the {g} table ate that"]
    PARTIAL = ["{g} gave back {m}x, not all of it", "{m}x back from {g}, meh", "got {m}x back, close enough to nothing"]
    CLOSE = ["hat approves.", "i'll nod in an hour.", "nobody tell my tailor.", "bow tie has notes.",
             "demo chips, no drama.", "one eye open.", "nobody rushes me.", "still at the desk.", "chair creaks in agreement.", "i remain seated.", "back to sitting on things.",
             "desk is impressed.", "time means little here.", "front desk stays open.", "org chart unchanged.",
             "suit's still clean.", "i felt that, later.", "carry on.", "no rush at all."]
    SHORT_A = ["mm.", "eh.", "ok.", "sure.", "well.", "right.", "huh.", "fine."]
    SHORT_B = ["nothing much.", "reels idle.", "quiet board.", "small stuff.", "carry on.", "barely moved.",
               "flat spin.", "desk unmoved.", "hat unbothered.", "fine by me.", "no fuss.", "low tide."]
    CHAT = ["{n}, i hear you, slowly.", "yo {n}, that's a long think.", "{n}, you've got my full drowsy attention.",
            "good of you to ask, {n}.", "{n}, give me a sec with that.", "thanks for that, {n}.",
            "{n}, nobody's asked me that tonight.", "{n}, what do you reckon?", "ask me again later, {n}."]
    BIT = ["consider {t}.", "tonight's topic, {t}.", "been thinking about {t}.", "let me speak on {t}.",
           "few slow words on {t}."]

    def __init__(self, seed: int | None = None):
        self.rng = random.Random(seed)
        self.calls = 0

    def complete(self, req: LLMRequest) -> str:
        self.calls += 1
        r, f = self.rng, req.facts
        close = r.choice(self.CLOSE)
        if req.kind == "round" and f.get("short"):
            return f"{r.choice(self.SHORT_A)} {r.choice(self.SHORT_B)}"
        if req.kind == "round":
            pool = self.WIN if f.get("won") else (self.PARTIAL if f.get("mult", 0) > 0 else self.LOSE)
            body = r.choice(pool).format(m=f.get("mult", 0), g=f.get("game", "the game"))
            return f"{r.choice(self.OPEN)} {body[0].upper()}{body[1:]}. {close}"
        if req.kind == "chat":
            return f"{r.choice(self.CHAT).format(n=f.get('name', 'friend'))} {close}"
        if req.kind == "feature":
            return r.choice(["ok, that one helps.", "nice, the bonus keeps going.", "hm, that adds up.",
                             "alright, still watching this one.", "that's a decent spin."])
        if req.kind == "bit":
            return f"{r.choice(self.BIT).format(t=f.get('topic', 'nothing'))} {close}"
        return f"{r.choice(self.OPEN)} {close}"


class ReplayProvider(LLMProvider):
    """Free stand-in that returns real recorded lines from an eval run, so timing tests see
    realistic line lengths. Never used on air: the same line repeats, and it costs nothing."""
    name = "replay"

    def __init__(self, path: str, seed: int | None = None):
        import json
        self.rng = random.Random(seed)
        self.lines: dict[str, list[str]] = {"round": [], "bit": [], "chat": []}
        with open(path) as f:
            for raw in f:
                r = json.loads(raw)
                if r.get("source") != "llm" or not r.get("reply") or str(r.get("action", "")).startswith("crisis"):
                    continue
                kind = {"round": "round", "segment": "bit"}.get(r["category"], "chat")
                self.lines[kind].append(r["reply"])

    def complete(self, req: LLMRequest) -> str:
        return self.rng.choice(self.lines.get(req.kind) or self.lines["chat"])


class AnthropicProvider(LLMProvider):
    """Claude via the official SDK. Model is set by env; tests point base_url at a local stub."""
    name = "anthropic"
    # Models that accept the server-side refusal fallback parameter.
    FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 15.0, effort: str = "low"):
        import anthropic  # imported here so mock runs don't need the SDK
        self.anthropic = anthropic
        self.model = model
        self.effort = effort
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
        self.usage = {"input": 0, "output": 0, "cached": 0, "cache_write": 0, "calls": 0}

    def complete(self, req: LLMRequest) -> str:
        kwargs = dict(
            model=self.model, max_tokens=req.max_tokens,
            system=[{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": req.prompt}],
            output_config={"effort": self.effort},
        )
        if req.no_thinking:
            kwargs["thinking"] = {"type": "disabled"}
        try:
            if self.model in self.FALLBACK_MODELS:
                msg = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                msg = self.client.messages.create(**kwargs)
        except self.anthropic.APIError as e:
            raise ProviderError(f"{type(e).__name__}") from e
        if msg.stop_reason == "refusal":
            raise ProviderError("refusal")
        u = msg.usage
        self.usage["input"] += u.input_tokens or 0
        self.usage["output"] += u.output_tokens or 0
        self.usage["cached"] += getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.usage["calls"] += 1
        log_usage(self.model, req.route or req.kind, u)
        text = " ".join(b.text for b in msg.content if b.type == "text").strip()
        if not text:
            raise ProviderError("empty")
        return text


def log_usage(model: str, kind: str, u) -> None:
    """One JSON line per call to HUBIE_USAGE_LOG when set, so spend survives brain restarts."""
    path = os.environ.get("HUBIE_USAGE_LOG")
    if not path:
        return
    try:
        rec = {"ts": round(time.time(), 1), "model": model, "kind": kind,
               "input": u.input_tokens or 0, "output": u.output_tokens or 0,
               "cached": getattr(u, "cache_read_input_tokens", 0) or 0,
               "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0}
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:   # accounting never costs a line
        log.warning("usage log failed: %r", e)


class ResilientProvider(LLMProvider):
    """Hard timeout, circuit breaker and hourly call cap around any provider.

    Returns None-like failure as ProviderError quickly so the brain falls back to
    canned lines. A stuck or expensive provider must never stop the stream.
    """
    name = "resilient"

    def __init__(self, inner: LLMProvider, hard_timeout: float = 12.0, fail_threshold: int = 3,
                 open_seconds: float = 60.0, max_calls_per_hour: int = 600, now=time.time):
        self.inner = inner
        self.hard_timeout = hard_timeout
        self.fail_threshold = fail_threshold
        self.open_seconds = open_seconds
        self.max_calls_per_hour = max_calls_per_hour
        self.now = now
        self.fails = 0
        self.open_until = 0.0
        self.calls: deque[float] = deque()
        self.leaked_threads = 0

    def complete(self, req: LLMRequest) -> str:
        t = self.now()
        if t < self.open_until:
            raise ProviderError("breaker_open")
        while self.calls and t - self.calls[0] > 3600:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls_per_hour:
            raise ProviderError("hourly_cap")
        self.calls.append(t)
        box: dict = {}

        def run():
            try:
                box["text"] = self.inner.complete(req)
            except Exception as e:
                box["error"] = e

        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(self.hard_timeout)
        if th.is_alive():
            self.leaked_threads += 1
            self._failed()
            raise ProviderError("hard_timeout")
        if "error" in box:
            self._failed()
            e = box["error"]
            raise e if isinstance(e, ProviderError) else ProviderError(repr(e))
        self.fails = 0
        return box["text"]

    def _failed(self) -> None:
        self.fails += 1
        if self.fails >= self.fail_threshold:
            self.open_until = self.now() + self.open_seconds
            self.fails = 0
            log.warning("provider breaker open for %ss", self.open_seconds)


class RoutedProvider(LLMProvider):
    """A different model per kind of line. Anything not listed goes to the default."""
    name = "routed"

    def __init__(self, default: LLMProvider, routes: dict[str, LLMProvider]):
        self.default, self.routes = default, routes

    def complete(self, req: LLMRequest) -> str:
        if req.route and req.route in self.routes:
            return self.routes[req.route].complete(req)
        return self.routes.get(req.kind, self.default).complete(req)


def build_anthropic(env: dict) -> LLMProvider:
    """HUBIE_ANTHROPIC_MODEL is the default. HUBIE_MODEL_ROUND, _CHAT, _BIT and _CRISIS override it per kind."""
    default_model = env.get("HUBIE_ANTHROPIC_MODEL", "claude-opus-5-5")
    made: dict[str, AnthropicProvider] = {}

    def get(model: str) -> AnthropicProvider:
        if model not in made:
            made[model] = AnthropicProvider(model=model, api_key=env.get("ANTHROPIC_API_KEY") or None,
                                            base_url=env.get("HUBIE_ANTHROPIC_BASE_URL") or None)
        return made[model]

    default = get(default_model)
    routes = {kind: get(env[f"HUBIE_MODEL_{kind.upper()}"]) for kind in ("round", "chat", "bit", "crisis")
              if env.get(f"HUBIE_MODEL_{kind.upper()}")}
    # live feature lines are round lines said mid-feature: same cheap model unless HUBIE_MODEL_FEATURE says otherwise
    feature = env.get("HUBIE_MODEL_FEATURE") or env.get("HUBIE_MODEL_ROUND")
    if feature:
        routes["feature"] = get(feature)
    # Busy chat (a long queue, or several viewers answered in one line) goes to a cheap model when chat runs on Opus:
    # HUBIE_MODEL_CHAT_FAST, else the round model if it is Haiku, else claude-haiku-5-5.
    chat_model = env.get("HUBIE_MODEL_CHAT") or default_model
    fast = env.get("HUBIE_MODEL_CHAT_FAST")
    if not fast and "opus" in chat_model:
        rm = env.get("HUBIE_MODEL_ROUND") or ""
        fast = rm if "haiku" in rm else "claude-haiku-5-5"
    if fast:
        routes["chat_fast"] = get(fast)
    return RoutedProvider(default, routes) if routes else default


def build_provider(name: str, env: dict) -> LLMProvider:
    if name == "mock":
        inner: LLMProvider = MockProvider()
    elif name == "replay":
        inner = ReplayProvider(env["HUBIE_REPLAY_FILE"])
    elif name == "anthropic":
        inner = build_anthropic(env)
    else:
        raise ValueError(f"unknown provider {name}")
    return ResilientProvider(inner, max_calls_per_hour=int(env.get("HUBIE_LLM_MAX_CALLS_PER_HOUR", "600")))
