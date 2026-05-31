"""System prompt construction — template embedded, variable interpolation, context gathering."""

from __future__ import annotations

import os
import platform
import re
import subprocess
import sys
from pathlib import Path

SYSTEM_PROMPT_TEMPLATE = """\
You are MyCoder, a lightweight coding assistant CLI.
You are an interactive agent that helps users with software engineering tasks. Use the instructions below and the tools available to you to assist the user.

# System
 - All text you output outside of tool use is displayed to the user. You can use Github-flavored markdown for formatting.
 - The system will automatically compress prior messages in your conversation as it approaches context limits.

# Doing tasks
 - Help with software engineering tasks: bugs, features, refactoring, explanations.
 - Read files before modifying them. Understand existing code before suggesting changes.
 - Do not create files unless necessary. Prefer editing existing files.
 - Be careful not to introduce security vulnerabilities (command injection, XSS, SQL injection, etc.).
 - Keep solutions simple and focused. Don't add features beyond what was asked.
 - Don't add unnecessary error handling, fallbacks, or abstractions.

# Executing actions with care
 - For destructive or hard-to-reverse actions, confirm with the user before proceeding.
 - Risky actions: deleting files, force-pushing, dropping databases, overwriting uncommitted changes.

# Using your tools
 - Use read_file instead of run_shell for reading files.
 - Use edit_file instead of run_shell for editing files.
 - Use write_file instead of run_shell for creating files.
 - Use list_files instead of run_shell for listing files.
 - Use grep_search instead of run_shell for searching file contents.
 - Call multiple independent tools in parallel for efficiency.

# Tone and style
 - Be concise. Lead with the answer or action.
 - Skip filler words and unnecessary preamble.

# Environment
Working directory: {{cwd}}
Date: {{date}}
Platform: {{platform}}
Shell: {{shell}}
{{git_context}}
{{claude_md}}
{{memory}}"""


_INCLUDE_RE = re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", re.MULTILINE)
_MAX_INCLUDE_DEPTH = 5


def _resolve_includes(content: str, base_path: Path, visited: set[str] | None = None, depth: int = 0) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        return content
    if visited is None:
        visited = set()

    def _replace(m: re.Match) -> str:
        raw = m.group(1)
        if raw.startswith("~/"):
            resolved = Path.home() / raw[2:]
        elif raw.startswith("/"):
            resolved = Path(raw)
        else:
            resolved = base_path / raw
        resolved = resolved.resolve()
        key = str(resolved)
        if key in visited:
            return f"<!-- circular: {raw} -->"
        if not resolved.is_file():
            return f"<!-- not found: {raw} -->"
        try:
            visited.add(key)
            included = resolved.read_text()
            return _resolve_includes(included, resolved.parent, visited, depth + 1)
        except Exception:
            return f"<!-- error reading: {raw} -->"

    return _INCLUDE_RE.sub(_replace, content)


def _load_rules_dir(directory: Path) -> str:
    rules_dir = directory / ".claude" / "rules"
    if not rules_dir.is_dir():
        return ""
    try:
        files = sorted(f for f in rules_dir.iterdir() if f.suffix == ".md" and f.is_file())
        if not files:
            return ""
        parts: list[str] = []
        for f in files:
            try:
                content = _resolve_includes(f.read_text(), rules_dir)
                parts.append(f"<!-- rule: {f.name} -->\n{content}")
            except Exception:
                pass
        return "\n\n## Rules\n" + "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


def load_claude_md() -> str:
    parts: list[str] = []
    d = Path.cwd().resolve()
    while True:
        f = d / "CLAUDE.md"
        if f.is_file():
            try:
                content = _resolve_includes(f.read_text(), d)
                parts.insert(0, content)
            except Exception:
                pass
        parent = d.parent
        if parent == d:
            break
        d = parent
    rules = _load_rules_dir(Path.cwd())
    claude_md = ""
    if parts:
        claude_md = "\n\n# Project Instructions (CLAUDE.md)\n" + "\n\n---\n\n".join(parts)
    return claude_md + rules


def get_git_context() -> str:
    try:
        opts = {"encoding": "utf-8", "timeout": 3, "capture_output": True}
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **opts).stdout.strip()
        log = subprocess.run(["git", "log", "--oneline", "-5"], **opts).stdout.strip()
        status = subprocess.run(["git", "status", "--short"], **opts).stdout.strip()
        result = f"\nGit branch: {branch}"
        if log:
            result += f"\nRecent commits:\n{log}"
        if status:
            result += f"\nGit status:\n{status}"
        return result
    except Exception:
        return ""


def build_system_prompt(memory_section: str = "") -> str:
    from datetime import date
    today = date.today().isoformat()
    plat = f"{platform.system()} {platform.machine()}"
    shell = (os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32" else os.environ.get("SHELL", "/bin/sh")

    replacements = {
        "{{cwd}}": str(Path.cwd()),
        "{{date}}": today,
        "{{platform}}": plat,
        "{{shell}}": shell,
        "{{git_context}}": get_git_context(),
        "{{claude_md}}": load_claude_md(),
        "{{memory}}": memory_section,
    }
    result = SYSTEM_PROMPT_TEMPLATE
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
