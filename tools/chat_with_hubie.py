#!/usr/bin/env python3
"""Talk to Hubie in text, in a terminal. No server, nothing public, no stream.

  chat_with_hubie.py                     interactive
  chat_with_hubie.py --say "yo hubie"    one reply and exit (keeps memory between runs)
  chat_with_hubie.py --mock              free canned test, no model

Real model only when ANTHROPIC_API_KEY is set. Commands while chatting:
  /round mines   play a fake round and hear the comment
  /segment story run a talk segment (/segments lists them)
  /name lena     change who you are in chat
  /debug         show moderation decision, line source and time
  /quit
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hubie.brain.provider import MockProvider, ResilientProvider  # noqa: E402
from hubie.chat_session import ChatSession  # noqa: E402
from hubie.config import Config, load_dotenv  # noqa: E402


def build_provider(mock: bool):
    if mock:
        return MockProvider(), "mock (no model)"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("needs ANTHROPIC_API_KEY in the environment or in .env. Or use --mock for a free canned test.")
    from hubie.brain.provider import AnthropicProvider
    model = os.environ.get("HUBIE_ANTHROPIC_MODEL", "claude-opus-5-5")
    inner = AnthropicProvider(model=model, api_key=os.environ["ANTHROPIC_API_KEY"],
                              base_url=os.environ.get("HUBIE_ANTHROPIC_BASE_URL") or None, timeout=40.0)
    return ResilientProvider(inner, hard_timeout=45.0, max_calls_per_hour=300), model


def show(turn, debug: bool) -> None:
    print(f"hubie: {turn.text}" if turn.text else "hubie: (doesn't read that out)")
    if debug:
        print(f"  [{turn.action} {turn.reason} | source {turn.source} | {turn.seconds:.1f}s]")


def handle(session, name: str, text: str, debug: bool) -> str:
    """Runs one input. Returns the possibly changed viewer name."""
    if text.startswith("/round"):
        parts = text.split()
        show(session.play_round(parts[1] if len(parts) > 1 else "mines"), debug)
    elif text == "/segments":
        print(", ".join(session.persona.segments))
    elif text.startswith("/segment"):
        parts = text.split()
        show(session.segment(parts[1] if len(parts) > 1 else "story"), debug)
    elif text.startswith("/name "):
        return text.split(None, 1)[1].strip()
    else:
        show(session.chat(name, text), debug)
    return name


def main() -> None:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--say")
    ap.add_argument("--name", default="tester")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--memory", help="memory file, kept separate from the stream's")
    ap.add_argument("--fresh", action="store_true", help="forget this test's chat memory first")
    args = ap.parse_args()
    provider, label = build_provider(args.mock)
    cfg = Config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    memory = Path(args.memory) if args.memory else cfg.data_dir / "chat_test_memory.json"
    if args.fresh:
        memory.unlink(missing_ok=True)
    session = ChatSession(cfg.persona, provider, memory)
    if args.say:
        handle(session, args.name, args.say, args.debug)
        return
    print(f"talking to hubie as {args.name} | brain: {label} | persona: {session.persona.status} | /quit to leave")
    name, debug = args.name, args.debug
    while True:
        try:
            text = input(f"{name}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text == "/quit":
            return
        if text == "/debug":
            debug = not debug
            continue
        name = handle(session, name, text, debug)


if __name__ == "__main__":
    main()
