"""Command-line entry point: env loading, argument parsing, rendering, confirmation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .agent import Agent
from .llm import LLMClient, LLMError, ToolCall
from .tools import ToolBox

PROG = "errander"


class Palette:
    """ANSI colors; every code is empty when colors are off."""

    def __init__(self, enabled: bool):
        codes = {
            "reset": "0", "bold": "1", "dim": "2",
            "red": "31", "green": "32", "yellow": "33", "cyan": "36",
        }
        for name, code in codes.items():
            setattr(self, name, f"\033[{code}m" if enabled else "")


def load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE lines); never overrides existing variables."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="A tiny terminal coding agent that works with any OpenAI-compatible API.",
    )
    parser.add_argument("task", nargs="*", help="what to do (read from stdin instead when piped)")
    parser.add_argument(
        "-m", "--model",
        default=os.environ.get("ERRANDER_MODEL"),
        help="model name (env: ERRANDER_MODEL)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ERRANDER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1",
        help="OpenAI-compatible API root (env: ERRANDER_BASE_URL)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ERRANDER_API_KEY") or os.environ.get("OPENAI_API_KEY"),
        help="API key (env: ERRANDER_API_KEY)",
    )
    parser.add_argument(
        "--workdir", type=Path, default=Path.cwd(), help="workspace root the agent may touch"
    )
    parser.add_argument("--max-steps", type=int, default=25, help="safety cap on agent steps")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="run shell commands without confirmation"
    )
    parser.add_argument(
        "--no-stream", action="store_true", help="buffer responses instead of streaming"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _short_args(call: ToolCall) -> str:
    args = call.arguments or {}
    if call.name == "write_file" and "path" in args:
        return f"{args['path']}  ({len(args.get('content') or '')} chars)"
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 110 else text[:107] + "..."


def _preview(result: str, width: int = 100) -> str:
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    if not lines:
        return "(no output)"
    head = "  |  ".join(lines[:2])[:width]
    more = f"  (+{len(lines) - 2} lines)" if len(lines) > 2 else ""
    return head + more


def make_confirm(palette: Palette):
    def confirm(command: str) -> bool:
        if not sys.stdin.isatty():
            print(
                f"{palette.yellow}! declining '{command}' — no interactive terminal; "
                f"use --yes to auto-approve{palette.reset}"
            )
            return False
        print(f"{palette.yellow}? run:{palette.reset} {command}")
        try:
            answer = input(f"{palette.yellow}  [y/N]{palette.reset} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in {"y", "yes"}

    return confirm


class Console:
    """Renders agent events; the only place that knows about ANSI colors."""

    def __init__(self, palette: Palette):
        self.c = palette
        self.final_answer_printed = False
        self._streamed = False

    def on_event(self, event: str, *payload: object) -> None:
        if event == "turn":
            print(f"\n{self.c.dim}── step {payload[0]} ─────────────────────────────{self.c.reset}")
        elif event == "delta":
            self._streamed = True
            print(str(payload[0]), end="", flush=True)
        elif event == "assistant":
            message = payload[0]
            content = getattr(message, "content", None)
            if content:
                if self._streamed:
                    print()
                else:
                    print(content)
                self._streamed = False
                self.final_answer_printed = True
        elif event == "tool_call":
            call = payload[0]
            print(f"{self.c.cyan}● {call.name}{self.c.reset} {_short_args(call)}")
        elif event == "tool_result":
            print(f"{self.c.dim}{_preview(str(payload[1]))}{self.c.reset}")


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":
        os.system("")  # enables ANSI escape sequences on classic Windows consoles
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(Path(".env"))

    parser = build_parser()
    args = parser.parse_args(argv)
    palette = Palette(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    console = Console(palette)

    task = " ".join(args.task).strip()
    if not task and not sys.stdin.isatty():
        task = sys.stdin.read().strip()
    if not task:
        parser.print_usage()
        print(f'\nExample:  {PROG} "find all TODO comments and collect them into TODOS.md"')
        return 2
    if not args.api_key:
        print(
            f"{palette.red}error:{palette.reset} no API key — set ERRANDER_API_KEY "
            "(or OPENAI_API_KEY), or pass --api-key"
        )
        print("copy .env.example to .env for ready-made DeepSeek / GLM / OpenAI / Ollama settings")
        return 2
    if not args.model:
        print(
            f"{palette.red}error:{palette.reset} no model — set ERRANDER_MODEL or pass --model "
            "(e.g. deepseek-chat, glm-4.5-flash)"
        )
        return 2

    workdir = args.workdir.resolve()
    client = LLMClient(args.base_url, args.api_key, args.model)
    toolbox = ToolBox(workdir, confirm_run=None if args.yes else make_confirm(palette))
    agent = Agent(
        client,
        toolbox,
        max_steps=args.max_steps,
        stream=not args.no_stream,
        on_event=console.on_event,
    )

    print(
        f"{palette.dim}{PROG} v{__version__} · model {args.model}"
        f" · workspace {workdir}{palette.reset}"
    )
    started = time.monotonic()
    try:
        result = agent.run(task)
    except LLMError as exc:
        print(f"\n{palette.red}LLM error:{palette.reset} {exc}")
        return 1
    except KeyboardInterrupt:
        print(f"\n{palette.yellow}interrupted{palette.reset}")
        return 130

    if result.stopped_reason:
        print(
            f"\n{palette.yellow}! {result.stopped_reason} — partial work may already "
            f"be saved{palette.reset}"
        )
    if result.answer and not console.final_answer_printed:
        print(result.answer)
    tokens = client.prompt_tokens + client.completion_tokens
    stats = [
        f"{result.steps} steps",
        f"{result.tool_calls} tool call{'s' if result.tool_calls != 1 else ''}",
        f"{time.monotonic() - started:.1f}s",
    ]
    if tokens:
        stats.append(f"{tokens:,} tokens")
    print(f"{palette.dim}{' · '.join(stats)}{palette.reset}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
