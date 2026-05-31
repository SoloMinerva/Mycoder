"""Memory system — file-based memory with MEMORY.md index."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from .frontmatter import parse_frontmatter, format_frontmatter

SideQueryFn = Callable[[str, str], Any]

# ─── Constants ────────────────────────────────────────────────

VALID_TYPES = {"user", "feedback", "project", "reference"}
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25000

CLEANUP_WATER_LINES = 150
CLEANUP_WATER_BYTES = 20_000
CLEANUP_MAX_AGE_DAYS = 30


# ─── Types ────────────────────────────────────────────────────

class MemoryEntry:
    __slots__ = ("name", "description", "type", "filename", "content")

    def __init__(self, name: str, description: str, type: str, filename: str, content: str):
        self.name = name
        self.description = description
        self.type = type
        self.filename = filename
        self.content = content


# ─── Paths ────────────────────────────────────────────────────

def _project_hash() -> str:
    return hashlib.sha256(str(Path.cwd()).encode()).hexdigest()[:16]


def get_memory_dir() -> Path:
    d = Path.home() / ".mycoder" / "projects" / _project_hash() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_index_path() -> Path:
    return get_memory_dir() / "MEMORY.md"


# ─── Slugify ──────────────────────────────────────────────────

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower())
    s = s.strip("_")
    return s[:40]


# ─── CRUD ─────────────────────────────────────────────────────

def list_memories() -> list[MemoryEntry]:
    d = get_memory_dir()
    entries: list[MemoryEntry] = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            result = parse_frontmatter(f.read_text())
            meta = result.meta
            if not meta.get("name") or not meta.get("type"):
                continue
            t = meta["type"] if meta["type"] in VALID_TYPES else "project"
            entries.append(MemoryEntry(
                name=meta["name"],
                description=meta.get("description", ""),
                type=t,
                filename=f.name,
                content=result.body,
            ))
        except Exception:
            pass
    entries.sort(key=lambda e: (d / e.filename).stat().st_mtime, reverse=True)
    return entries


def save_memory(name: str, description: str, type: str, content: str) -> str:
    d = get_memory_dir()
    filename = f"{type}_{_slugify(name)}.md"
    text = format_frontmatter({"name": name, "description": description, "type": type}, content)
    (d / filename).write_text(text)
    _update_memory_index()
    return filename


def delete_memory(filename: str) -> bool:
    filepath = get_memory_dir() / filename
    if not filepath.exists():
        return False
    filepath.unlink()
    _update_memory_index()
    return True


# ─── Cleanup ──────────────────────────────────────────────────

def _last_cleanup_path() -> Path:
    return get_memory_dir() / ".last_cleanup"


def get_last_cleanup_ts() -> float | None:
    p = _last_cleanup_path()
    if not p.exists():
        return None
    try:
        return float(p.read_text().strip())
    except Exception:
        return None


def set_last_cleanup_ts(ts: float | None = None) -> None:
    ts = ts if ts is not None else time.time()
    _last_cleanup_path().write_text(str(ts))


def cleanup_warning() -> str | None:
    index = _get_index_path()
    lines_count = 0
    byte_count = 0
    if index.exists():
        text = index.read_text()
        lines_count = text.count("\n") + 1
        byte_count = len(text.encode())

    triggers: list[str] = []
    if lines_count >= CLEANUP_WATER_LINES or byte_count >= CLEANUP_WATER_BYTES:
        triggers.append(f"index at {lines_count} lines / {byte_count // 1024}KB")

    last = get_last_cleanup_ts()
    if last is not None:
        age_days = int((time.time() - last) / 86400)
        if age_days >= CLEANUP_MAX_AGE_DAYS:
            triggers.append(f"{age_days} days since last cleanup")

    if not triggers:
        return None
    return "Memory: " + ", ".join(triggers) + " — run /memory cleanup"


CLEANUP_PROMPT = """You are a memory curator for an AI coding assistant. Below is the list of all saved memories (filename, type, description) and a summary of recent conversation.

Judge which memories are now stale, redundant, superseded, or clearly no longer useful. Be conservative — only flag memories you are confident should be deleted.

Reasons to DELETE:
- Project task has been completed or direction has clearly changed
- Content is contradicted or superseded by a newer memory
- Project memory with an expired deadline

Reasons to KEEP:
- User identity / role / preferences (user, feedback types) — these persist
- Any memory the user might still reference
- If unsure, keep it

Return strictly this JSON (no other text, no markdown fences):
{
  "delete": [
    {"filename": "...", "reason": "brief one-line reason"}
  ]
}"""


async def cleanup_suggestion(side_query: SideQueryFn, recent_conversation: str = "") -> list[dict]:
    memories = list_memories()
    if not memories:
        return []

    lines = []
    for m in memories:
        lines.append(f"- [{m.type}] {m.filename}: {m.description}")
    manifest = "\n".join(lines)

    user_msg = (
        f"All saved memories:\n{manifest}\n\n"
        f"Recent conversation:\n{recent_conversation or '(none provided)'}"
    )

    try:
        text = await side_query(CLEANUP_PROMPT, user_msg)
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        result = parsed.get("delete", [])
        valid_names = {m.filename for m in memories}
        return [d for d in result if isinstance(d, dict) and d.get("filename") in valid_names]
    except Exception as e:
        print(f"[memory] cleanup suggestion failed: {e}")
        return []


def apply_cleanup(filenames: list[str]) -> int:
    d = get_memory_dir()
    count = 0
    for name in filenames:
        if name in ("MEMORY.md", ".last_cleanup"):
            continue
        p = d / name
        if p.exists():
            p.unlink()
            count += 1
    _update_memory_index()
    set_last_cleanup_ts()
    return count


# ─── Index ────────────────────────────────────────────────────

def _update_memory_index() -> None:
    memories = list_memories()
    lines = ["# Memory Index", ""]
    for m in memories:
        lines.append(f"- **[{m.name}]({m.filename})** ({m.type}) — {m.description}")
    _get_index_path().write_text("\n".join(lines))


def load_memory_index() -> str:
    index_path = _get_index_path()
    if not index_path.exists():
        return ""
    content = index_path.read_text()
    lines = content.split("\n")
    if len(lines) > MAX_INDEX_LINES:
        content = "\n".join(lines[:MAX_INDEX_LINES]) + "\n\n[... truncated ...]"
    if len(content.encode()) > MAX_INDEX_BYTES:
        content = content[:MAX_INDEX_BYTES] + "\n\n[... truncated ...]"
    return content


# ─── System prompt section ────────────────────────────────────

def build_memory_prompt_section() -> str:
    _update_memory_index()
    index = load_memory_index()
    memory_dir = str(get_memory_dir())

    return f"""# Memory System

You have a persistent, file-based memory system at `{memory_dir}`.

## Memory Types
- **user**: User's role, preferences, knowledge level
- **feedback**: Corrections and guidance from the user
- **project**: Ongoing work, goals, decisions
- **reference**: Pointers to external resources

## How to Save Memories
Use write_file to create a memory file:

```markdown
---
name: memory name
description: one-line description
type: user|feedback|project|reference
---
Memory content here.
```

Save to: `{memory_dir}/`
Filename format: {{type}}_{{slugified_name}}.md

## What NOT to Save
- Code patterns or architecture (read the code instead)
- Git history (use git log)
- Ephemeral task details
{chr(10) + "## Current Memory Index" + chr(10) + index if index else chr(10) + "(No memories saved yet.)"}"""
