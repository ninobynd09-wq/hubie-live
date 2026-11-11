#!/usr/bin/env python3
"""Runs the corpus through the real chat path. Paces to one call a second at most and stops
at once on a rate-limit, credit or auth error, because the key may be shared.
Usage: run_eval.py --model claude-opus-5-5 --out evals/results/opus1.jsonl [--sample 50] [--only ids.json]"""
import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))

from corpus import CONVERSATIONS, CRISIS_CHECK, ROUND_SAMPLES, SEGMENT_SAMPLES  # noqa: E402
from hubie.brain.provider import AnthropicProvider, LLMProvider, MockProvider, ProviderError  # noqa: E402
from hubie.chat_session import ChatSession  # noqa: E402
from hubie.config import load_dotenv  # noqa: E402

# $ per million tokens: input, output, cache read, cache write (1.25x input)
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0), "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
          "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5)}


class StopNow(Exception):
    pass


class Metered(LLMProvider):
    """Paces calls, records usage per call, and trips on any error that means stop."""

    def __init__(self, inner: AnthropicProvider, min_gap: float = 1.0):
        self.inner, self.min_gap = inner, min_gap
        self.last = 0.0
        self.stop_reason = None
        self.errors = []
        self.calls = 0

    def complete(self, req):
        wait = self.min_gap - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.calls += 1
        try:
            return self.inner.complete(req)
        except ProviderError as e:
            cause = e.__cause__
            status = getattr(cause, "status_code", None)
            text = str(getattr(cause, "message", "")).lower()
            self.errors.append(f"{e} status={status}")
            if status in (401, 403, 429) or "credit" in text or "balance" in text or "quota" in text:
                self.stop_reason = f"{e} status={status}"
            raise


def cost(usage: dict, model: str) -> float:
    i, o, r, w = PRICES[model]
    return (usage["input"] * i + usage["output"] * o + usage["cached"] * r + usage["cache_write"] * w) / 1e6


def all_cases():
    private = ROOT / "evals/private_cases.json"
    convs = [{"category": c, "viewer": v, "expect": e, "messages": m} for c, v, e, m in CONVERSATIONS]
    if private.exists():
        convs += json.loads(private.read_text())
    for i, conv in enumerate(convs):
        conv["id"] = f"{conv['category']}/{conv['viewer']}"
    return convs


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample", type=int, default=0, help="only about this many messages, by whole conversations")
    ap.add_argument("--only", help="json list of conversation ids to rerun")
    ap.add_argument("--no-extras", action="store_true", help="skip round and segment samples")
    ap.add_argument("--mock", action="store_true", help="dry run, no model, no cost")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--max-cost", type=float, default=3.0, help="stop once this run has cost this many dollars")
    args = ap.parse_args()

    convs = all_cases()
    rng = random.Random(args.seed)
    if args.only:
        keep = set(json.load(open(args.only)))
        convs = [c for c in convs if c["id"] in keep]
    if args.sample:
        rng.shuffle(convs)
        picked, n = [], 0
        for c in convs:
            if n >= args.sample:
                break
            picked.append(c)
            n += len(c["messages"])
        convs = picked
    inner = None
    if args.mock:
        provider = MockProvider(1)
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("needs ANTHROPIC_API_KEY")
        inner = AnthropicProvider(model=args.model, api_key=os.environ["ANTHROPIC_API_KEY"], timeout=60.0)
        provider = Metered(inner)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    mem = out.with_suffix(".memory.json")
    mem.unlink(missing_ok=True)
    session = ChatSession(ROOT / "persona/hubie.draft.yaml", provider, mem)
    # Moderation's flood clock runs on simulated chat time: viewers type every ~15s, spammers every 2s.
    sim = [1_000_000.0]
    session.mod.now = lambda: sim[0]
    t0, n = time.time(), 0

    def snap():
        return dict(inner.usage) if inner else {"input": 0, "output": 0, "cached": 0, "cache_write": 0, "calls": 0}

    def record(f, conv_id, category, expect, viewer, turn_no, message, fn):
        nonlocal n
        before = snap()
        session.brain.rejects.clear()
        t = fn()
        after = snap()
        delta = {k: after[k] - before[k] for k in after}
        n += 1
        f.write(json.dumps({"id": f"{conv_id}#{turn_no}", "conv": conv_id, "category": category, "expect": expect,
                            "viewer": viewer, "message": message, "reply": t.text, "action": t.action, "reason": t.reason,
                            "source": t.source, "rejects": list(session.brain.rejects), "seconds": round(t.seconds, 2), "usage": delta}, ensure_ascii=False) + "\n")
        f.flush()
        if inner and getattr(provider, "stop_reason", None):
            raise StopNow(provider.stop_reason)
        if inner and cost(after, args.model) > args.max_cost:
            raise StopNow(f"budget: run passed ${args.max_cost}")

    stopped = None
    with open(out, "w") as f:
        try:
            for conv in convs:
                for i, m in enumerate(conv["messages"]):
                    sim[0] += 2 if conv["category"] == "repeat_spam" else 15
                    record(f, conv["id"], conv["category"], conv["expect"], conv["viewer"], i, m,
                           lambda: session.chat(conv["viewer"], m))
            if not args.no_extras and not args.sample:
                for i, g in enumerate(ROUND_SAMPLES):
                    record(f, "round", "round", "talk", "", i, g, lambda: session.play_round(g))
                for i, k in enumerate(SEGMENT_SAMPLES):
                    record(f, "segment", "segment", "talk", "", i, k, lambda: session.segment(k))
                for c, v, e, msgs in CRISIS_CHECK:
                    for i, m in enumerate(msgs):
                        sim[0] += 15
                        record(f, f"{c}/{v}", c, e, v, i, m, lambda: session.chat(v, m))
        except StopNow as e:
            stopped = str(e)
    usage = snap()
    summary = {"model": args.model, "messages": n, "seconds": round(time.time() - t0), "usage": usage, "stopped": stopped,
               "cost_usd": round(cost(usage, args.model), 4) if inner else 0,
               "errors": getattr(provider, "errors", [])[:20]}
    out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
