"""CLI entry point and interactive REPL."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from .agent import Agent
from .ui import print_welcome, print_error, print_info
from .session import load_session, get_latest_session_id
from .memory import list_memories, cleanup_warning


def _load_dotenv() -> None:
    from pathlib import Path
    path = Path.cwd() / ".env"
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="mycoder",
        description="MyCoder — a minimal coding agent",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", action="store_true", help="Resume last session")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    return parser.parse_args()


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


async def run_repl(agent: Agent) -> None:
    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if not agent._aborted:
            agent.abort()
            print("\n  (interrupted)")
            sigint_count = 0
            print_info("You> ")
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nBye!\n")
                sys.exit(0)
            print("\n  Press Ctrl+C again to exit.")
            print_info("You> ")

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome(model=agent.model, cwd=os.getcwd())

    warn = cleanup_warning()
    if warn:
        print_info(warn)

    while True:
        try:
            line = input("You> ")
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_info(f"{len(memories)} memories:")
                for m in memories:
                    print(f"    [{m.type}] {m.name} — {m.description}")
            continue

        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))


def main() -> None:
    _load_dotenv()
    args = parse_args()

    if args.help:
        print("""
Usage: mycoder [options] [prompt]

Options:
  --yolo, -y          Skip all confirmation prompts
  --accept-edits      Auto-approve file edits
  --dont-ask          Auto-deny confirmations
  --model, -m         Model to use
  --api-base URL      OpenAI-compatible API base URL
  --resume            Resume last session
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /cost               Show token usage and cost
  /compact            Manually compact conversation

Examples:
  mycoder "fix the bug in app.py"
  mycoder --yolo "run all tests"
  mycoder --resume
  mycoder
""")
        sys.exit(0)

    permission_mode = _resolve_permission_mode(args)
    model = args.model or os.environ.get("MYCODER_MODEL", "claude-sonnet-4-6")
    api_base = args.api_base

    resolved_api_base = api_base
    resolved_api_key: str | None = None
    resolved_use_openai = bool(api_base)

    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
        resolved_api_key = os.environ["OPENAI_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("OPENAI_BASE_URL")
        resolved_use_openai = True
    elif os.environ.get("ANTHROPIC_API_KEY"):
        resolved_api_key = os.environ["ANTHROPIC_API_KEY"]
        resolved_use_openai = False
    elif os.environ.get("OPENAI_API_KEY"):
        resolved_api_key = os.environ["OPENAI_API_KEY"]
        resolved_api_base = resolved_api_base or os.environ.get("OPENAI_BASE_URL")
        resolved_use_openai = True

    if not resolved_api_key and api_base:
        resolved_api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        resolved_use_openai = True

    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set ANTHROPIC_API_KEY for Anthropic,\n"
            "  or OPENAI_API_KEY + OPENAI_BASE_URL for OpenAI-compatible."
        )
        sys.exit(1)

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        api_base=resolved_api_base if resolved_use_openai else None,
        api_key=resolved_api_key,
    )

    if args.resume:
        session_id = get_latest_session_id()
        if session_id:
            session = load_session(session_id)
            if session:
                agent.restore_session(session)
            else:
                print_info("No session found to resume.")
        else:
            print_info("No previous sessions found.")

    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt:
        try:
            asyncio.run(agent.chat(prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
