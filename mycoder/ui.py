"""Terminal UI rendering — colored output, spinner, tool display."""

from __future__ import annotations

import sys
import threading
import time

# Force UTF-8 output on Windows to support Unicode symbols
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console

console = Console(highlight=False, force_terminal=True)


def print_welcome(model: str = "", cwd: str = "") -> None:
    from pathlib import Path
    if not cwd:
        cwd = str(Path.cwd())
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]
    if len(cwd) > 40:
        cwd = "…" + cwd[-39:]
    model = model or "default"
    console.print()
    console.print(f"  [bold cyan]MyCoder[/bold cyan][dim] — a minimal coding agent[/dim]")
    console.print(f"  [dim]Model:[/dim] {model}")
    console.print(f"  [dim]Dir:  [/dim] {cwd}")
    console.print(f"  [dim]Type your request · exit to quit[/dim]")
    console.print("[dim]  Commands: /clear /cost /compact /memory[/dim]\n")


def print_assistant_text(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def print_tool_call(name: str, inp: dict) -> None:
    icon = _get_tool_icon(name)
    summary = _get_tool_summary(name, inp)
    console.print(f"\n  [yellow]{icon} {name}[/yellow][dim] {summary}[/dim]")


def print_tool_result(name: str, result: str, duration: float | None = None) -> None:
    if name in ("edit_file", "write_file") and not result.startswith("Error"):
        _print_file_change_result(name, result)
        if duration is not None:
            console.print(f"[green]  ✓[/green][dim] {name} ({_fmt_duration(duration)})[/dim]")
        return
    max_len = 500
    truncated = result
    if len(result) > max_len:
        truncated = result[:max_len] + f"\n  ... ({len(result)} chars total)"
    lines = "\n".join("  " + l for l in truncated.split("\n"))
    console.print(f"[dim]{lines}[/dim]")
    if duration is not None:
        console.print(f"[green]  ✓[/green][dim] {name} ({_fmt_duration(duration)})[/dim]")


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.2f}s"
    return f"{seconds / 60:.1f}m"


def _print_file_change_result(_name: str, result: str) -> None:
    lines = result.split("\n")
    console.print(f"[dim]  {lines[0]}[/dim]")
    max_display = 40
    content_lines = lines[1:]
    for line in content_lines[:max_display]:
        if not line.strip():
            continue
        if line.startswith("@@"):
            console.print(f"[cyan]  {line}[/cyan]")
        elif line.startswith("- "):
            console.print(f"[red]  {line}[/red]")
        elif line.startswith("+ "):
            console.print(f"[green]  {line}[/green]")
        else:
            console.print(f"[dim]  {line}[/dim]")
    if len(content_lines) > max_display:
        console.print(f"[dim]  ... ({len(content_lines) - max_display} more lines)[/dim]")


def print_error(msg: str) -> None:
    console.print(f"\n  [red]Error: {msg}[/red]")


def print_confirmation(command: str) -> None:
    console.print(f"\n  [yellow]⚠ Dangerous command:[/yellow] [white]{command}[/white]")


def print_divider() -> None:
    console.print(f"\n[dim]  {'─' * 50}[/dim]")


def print_cost(input_tokens: int, output_tokens: int) -> None:
    cost = (input_tokens / 1_000_000) * 3 + (output_tokens / 1_000_000) * 15
    console.print(f"\n[dim]  Tokens: {input_tokens} in / {output_tokens} out (~${cost:.4f})[/dim]")


def print_retry(attempt: int, max_retries: int, reason: str) -> None:
    console.print(f"\n  [yellow]↻ Retry {attempt}/{max_retries}: {reason}[/yellow]")


def print_info(msg: str) -> None:
    console.print(f"\n  [cyan]ℹ {msg}[/cyan]")


# ─── Spinner ──────────────────────────────────────────────

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_spinner_thread: threading.Thread | None = None
_spinner_stop = threading.Event()


def start_spinner(label: str = "Thinking") -> None:
    global _spinner_thread
    if _spinner_thread is not None:
        return
    _spinner_stop.clear()

    def _run() -> None:
        frame = 0
        sys.stdout.write(f"\n  {SPINNER_FRAMES[0]} {label}...")
        sys.stdout.flush()
        while not _spinner_stop.is_set():
            time.sleep(0.08)
            frame = (frame + 1) % len(SPINNER_FRAMES)
            sys.stdout.write(f"\r  {SPINNER_FRAMES[frame]} {label}...")
            sys.stdout.flush()

    _spinner_thread = threading.Thread(target=_run, daemon=True)
    _spinner_thread.start()


def stop_spinner() -> None:
    global _spinner_thread
    if _spinner_thread is None:
        return
    _spinner_stop.set()
    _spinner_thread.join(timeout=1)
    _spinner_thread = None
    sys.stdout.write("\r\033[K")
    sys.stdout.flush()


# ─── Tool icons and summaries ───────────────────────────────

_TOOL_ICONS = {
    "read_file": "📖",
    "write_file": "✏️",
    "edit_file": "🔧",
    "list_files": "📁",
    "grep_search": "🔍",
    "run_shell": "💻",
    "web_fetch": "🌐",
}


def _get_tool_icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "🔨")


def _get_tool_summary(name: str, inp: dict) -> str:
    if name == "read_file":
        return inp.get("file_path", "")
    if name == "write_file":
        return inp.get("file_path", "")
    if name == "edit_file":
        return inp.get("file_path", "")
    if name == "list_files":
        return inp.get("pattern", "")
    if name == "grep_search":
        return f'"{inp.get("pattern", "")}" in {inp.get("path", ".")}'
    if name == "run_shell":
        cmd = inp.get("command", "")
        return cmd[:60] + "..." if len(cmd) > 60 else cmd
    if name == "web_fetch":
        return inp.get("url", "")
    return ""
