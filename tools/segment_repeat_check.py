"""Replays a long quiet stretch of talk-segment lines (watching the floor, the quiet hour, slow lobby walk) with
pool asides in between, and counts segment lines that paraphrase one of the previous 15 spoken lines
(hubie/brain/similar.py talk_similar decides).

    python3 tools/segment_repeat_check.py                       # mock provider, free
    python3 tools/segment_repeat_check.py --provider anthropic  # real Haiku, 20 lines (about $0.01)

No game site, no voice, no Kick: lines are only printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

# (segment kind, topic): the "watching the floor" loop from the 2026-11-07 recording comes first and most often
PLAN = [("lobby_watch", "the floor going quiet")] * 3 + [("quiet_hour", None)] * 3 + \
       [("lobby_watch", "what the reels sound like when nobody's talking")] * 3 + [("bit", "the chair")] * 2 + \
       [("lobby_watch", "how chat moves at this hour")] * 3 + [("lobby_walk", "walking the lobby at sloth speed")] * 3 + \
       [("lobby_watch", "the floor going quiet")] * 3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="mock", choices=["mock", "anthropic"])
    ap.add_argument("--model", default="claude-haiku-5-5")
    ap.add_argument("--lines", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--night", type=int, default=1)
    a = ap.parse_args()
    from hubie.brain.brain import Brain
    from hubie.brain.memory import Memory
    from hubie.brain.persona import Persona
    from hubie.brain.provider import AnthropicProvider, MockProvider
    from hubie.brain.similar import talk_repeat, talk_similar
    from hubie.config import load_dotenv
    from hubie.safety.output_filter import OutputFilter

    if a.provider == "anthropic":
        load_dotenv(HERE / ".env")
        provider = AnthropicProvider(model=a.model, api_key=os.environ.get("ANTHROPIC_API_KEY"))
    else:
        provider = MockProvider(a.seed)
    brain = Brain(Persona.load(HERE / "persona/hubie.draft.yaml"), provider, Memory(), OutputFilter(), seed=a.seed)
    said, out, prev = [], [], None
    for i, (kind, topic) in enumerate(PLAN[:a.lines]):
        part = 1 if (kind, topic) != prev else part + 1
        prev = (kind, topic)
        before = dict(brain.source_counts)
        line = brain.talk_segment(kind, topic, part, 3, bool(a.night)).text
        src = next((k for k, v in brain.source_counts.items() if v != before.get(k)), "?")
        hit = talk_repeat(line, said[-15:])
        said.append(line)
        out.append({"kind": kind, "line": line, "source": src, "angle": (list(brain.seg_angles) or [""])[-1],
                    "repeat_of": hit, "why": talk_similar(line, hit) if hit else None})
        print(f"{'R ' if hit else '  '}{kind:12} [{src}] {line}" + (f"   <- {hit}" if hit else ""))
        if i % 2 == 1:
            aside = brain.aside().text
            said.append(aside)
            print(f"  {'aside':12} [pool] {aside}")
    reps = sum(1 for r in out if r["repeat_of"])
    summary = {"segment_lines": len(out), "near_repeats": reps, "sources": brain.source_counts,
               "llm_calls": brain.llm_calls,
               "rejects": [r.split(" | ")[0][:90] for k, r in brain.rejects if k == "soft"]}
    if a.provider == "anthropic":
        u = provider.usage
        summary["usage"] = u
        summary["approx_usd"] = round((u["input"] * 1.0 + u["cached"] * 0.1 + u["cache_write"] * 1.25 + u["output"] * 5.0) / 1e6, 4)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
