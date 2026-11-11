"""Replays a fixed run of demo slot rounds through the brain and measures how often a round line
is too close to one of the previous 10 round lines (hubie/brain/similar.py decides "too close").

    python3 tools/round_repeat_check.py                       # mock provider, free
    python3 tools/round_repeat_check.py --provider anthropic  # real Haiku, about 40 to 60 calls (under $0.01)

--code points at another checkout (for a before/after comparison); the measure always comes from this one.
No game site, no voice, no Kick: rounds are generated here, lines are only printed.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]

SLOTS = [
    ("fruit_party", "Fruit Party", "Cluster slot, fruit clusters of 5 or more pay, random multipliers on wins."),
    ("gates_of_olympus_1000", "Gates of Olympus 1000", "Zeus slot, 8 or more matching symbols anywhere pay, tumbles, random multiplier orbs."),
    ("sweet_bonanza_1000", "Sweet Bonanza 1000", "Candy slot, 8 or more matching symbols anywhere pay, wins tumble away, lollipops start free spins."),
    ("sugar_rush_1000", "Sugar Rush 1000", "Cluster slot, wins leave multiplier spots behind that stack up."),
    ("fruit_party", "Fruit Party", "Cluster slot, fruit clusters of 5 or more pay, random multipliers on wins."),
]
CHAT = [("larry", "this sloth has been on the same slot for an hour lol"),
        ("mira_k", "hubie do u ever actually stand up"),
        ("devo", "the candy one is so loud")]


def rounds(n: int, seed: int):
    from hubie.games.base import RoundResult
    r = random.Random(seed)
    per = max(1, n // len(SLOTS))
    for i in range(n):
        gid, name, how = SLOTS[min(i // per, len(SLOTS) - 1)]
        x = r.random()
        if x < 0.6:
            mult, tumbles = 0.0, 0
        elif x < 0.8:
            mult, tumbles = round(r.uniform(0.1, 0.8), 2), r.randint(1, 2)
        elif x < 0.95:
            mult, tumbles = round(r.uniform(1.2, 4), 2), r.randint(2, 4)
        else:
            mult, tumbles = round(r.uniform(8, 40), 2), r.randint(3, 6)
        fs = mult >= 8
        detail = {"slot": name, "provider": "Pragmatic Play", "tumbles": tumbles, "free_spins_triggered": fs,
                  "demo_currency": "demo $"}
        if fs:
            detail["free_spins_count"] = 10
        yield RoundResult(gid, f"replay-{i}", 1.0, mult, round(mult, 2), 1000.0, True, detail), name, how


def load_measure():
    spec = importlib.util.spec_from_file_location("hubie_similar_measure", HERE / "hubie/brain/similar.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="mock", choices=["mock", "anthropic"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--code", default=str(HERE), help="checkout whose brain is tested")
    ap.add_argument("--out", default="")
    ap.add_argument("--short", type=int, default=1, help="1: plain rounds get the short line (live behaviour), 0: all full")
    ap.add_argument("--short-llm-share", type=float, default=0.35)
    a = ap.parse_args()
    measure = load_measure()
    sys.path.insert(0, a.code)
    from hubie.brain.brain import Brain
    from hubie.brain.memory import Memory
    from hubie.brain.persona import Persona
    from hubie.brain.provider import AnthropicProvider, MockProvider
    from hubie.config import load_dotenv
    from hubie.safety.output_filter import OutputFilter

    if a.provider == "anthropic":
        load_dotenv(HERE / ".env")
        provider = AnthropicProvider(model=a.model, api_key=os.environ.get("ANTHROPIC_API_KEY"))
    else:
        provider = MockProvider(a.seed)
    mem = Memory()
    for name, text in CHAT:
        mem.note_said(name, name, text)
    brain = Brain(Persona.load(Path(a.code) / "persona/hubie.draft.yaml"), provider, mem, OutputFilter(), seed=a.seed)
    if hasattr(brain, "short_llm_share"):
        brain.short_llm_share = a.short_llm_share
    out, said = [], []
    for res, name, how in rounds(a.rounds, a.seed):
        before = dict(brain.source_counts)
        short = bool(a.short) and hasattr(brain, "is_plain") and brain.is_plain(res)
        line = brain.comment_round(res, name, "Now playing", how=how, **({"short": True} if short else {})).text
        src = next((k for k, v in brain.source_counts.items() if v != before.get(k)), "?")
        ignore = set(measure.tokens(name))
        hit = measure.too_similar(line, said[-10:], ignore)
        said.append(line)
        rec = {"game": name, "mult": res.multiplier, "line": line, "source": src, "repeat_of": hit,
               "angle": getattr(brain, "last_angle", None)}
        out.append(rec)
        print(f"{'R ' if hit else '  '}{name[:14]:14} {res.multiplier:6.2f}  [{src}{'/' + rec['angle'] if rec['angle'] else ''}] {line}")
    reps = sum(1 for r in out if r["repeat_of"])
    summary = {"rounds": len(out), "repeats": reps, "repeat_rate": round(reps / max(1, len(out)), 3),
               "short_lines": sum(1 for r in out if r["angle"] == "short"),
               "short_words_max": max([len(r["line"].split()) for r in out if r["angle"] == "short"] or [0]),
               "sources": brain.source_counts, "llm_calls": brain.llm_calls,
               "rejects": dict(__import__("collections").Counter(
                   f"{k}: {r.split(' | ')[0].split(chr(34))[0].strip()}" for k, r in brain.rejects).most_common())}
    if a.provider == "anthropic":
        u = provider.usage
        summary["usage"] = u
        summary["approx_usd"] = round((u["input"] * 0.10 + u["cached"] * 0.01 + u["cache_write"] * 0.125 + u["output"] * 0.50) / 1e6, 4)
        summary["avg_input_per_call"] = round((u["input"] + u["cached"] + u["cache_write"]) / max(1, u["calls"]))
    print(json.dumps(summary))
    if a.out:
        Path(a.out).write_text("\n".join(json.dumps(r) for r in out) + "\n" + json.dumps(summary) + "\n")


if __name__ == "__main__":
    main()
