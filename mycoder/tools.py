"""Tool definitions and execution — 7 tools with permission checking."""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

# ─── Permission modes ──────────────────────────────────────

PermissionMode = str  # "default" | "acceptEdits" | "bypassPermissions" | "dontAsk"

READ_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}
EDIT_TOOLS = {"write_file", "edit_file"}
CONCURRENCY_SAFE_TOOLS = {"read_file", "list_files", "grep_search", "web_fetch"}

IS_WIN = sys.platform == "win32"

ToolDef = dict  # Anthropic tool schema dict

# ─── Tool definitions ───────────────────────────────────────

tool_definitions: list[ToolDef] = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the file content with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to read"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to write"},
                "content": {"type": "string", "description": "The content to write to the file"},
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file by replacing an exact string match with new content. The old_string must match exactly (including whitespace and indentation).",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The path to the file to edit"},
                "old_string": {"type": "string", "description": "The exact string to find and replace"},
                "new_string": {"type": "string", "description": "The string to replace it with"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern. Returns matching file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match files (e.g., \"**/*.ts\", \"src/**/*\")"},
                "path": {"type": "string", "description": "Base directory to search from. Defaults to current directory."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_search",
        "description": "Search for a pattern in files. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "The regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in. Defaults to current directory."},
                "include": {"type": "string", "description": "File glob pattern to include (e.g., \"*.ts\", \"*.py\")"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_shell",
        "description": "Execute a shell command and return its output. Use this for running tests, installing packages, git operations, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds (default: 30000)"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch a URL and return its content as text. For HTML pages, tags are stripped to return readable text. For JSON/text responses, content is returned directly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch"},
                "max_length": {"type": "number", "description": "Maximum content length in characters (default 50000)"},
            },
            "required": ["url"],
        },
    },
]


# ─── Tool implementations ────────────────────────────────────

def _read_file(inp: dict) -> str:
    try:
        content = Path(inp["file_path"]).read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        numbered = "\n".join(f"{i+1:4d} | {line}" for i, line in enumerate(lines))
        return numbered
    except Exception as e:
        return f"Error reading file: {e}"


def _write_file(inp: dict) -> str:
    try:
        path = Path(inp["file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(inp["content"], encoding="utf-8")
        lines = inp["content"].split("\n")
        line_count = len(lines)
        preview = "\n".join(f"{i+1:4d} | {l}" for i, l in enumerate(lines[:30]))
        trunc = f"\n  ... ({line_count} lines total)" if line_count > 30 else ""
        return f"Successfully wrote to {inp['file_path']} ({line_count} lines)\n\n{preview}{trunc}"
    except Exception as e:
        return f"Error writing file: {e}"


def _normalize_quotes(s: str) -> str:
    s = re.sub("[''′]", "'", s)
    s = re.sub('[""″]', '"', s)
    return s


def _find_actual_string(file_content: str, search_string: str) -> str | None:
    if search_string in file_content:
        return search_string
    norm_search = _normalize_quotes(search_string)
    norm_file = _normalize_quotes(file_content)
    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx:idx + len(search_string)]
    return None


def _generate_diff(old_content: str, old_string: str, new_string: str) -> str:
    before_change = old_content.split(old_string)[0]
    line_num = before_change.count("\n") + 1
    old_lines = old_string.split("\n")
    new_lines = new_string.split("\n")
    parts = [f"@@ -{line_num},{len(old_lines)} +{line_num},{len(new_lines)} @@"]
    for l in old_lines:
        parts.append(f"- {l}")
    for l in new_lines:
        parts.append(f"+ {l}")
    return "\n".join(parts)


def _edit_file(inp: dict) -> str:
    try:
        path = Path(inp["file_path"])
        content = path.read_text(encoding="utf-8", errors="replace")

        actual = _find_actual_string(content, inp["old_string"])
        if not actual:
            return f"Error: old_string not found in {inp['file_path']}"

        count = content.count(actual)
        if count > 1:
            return f"Error: old_string found {count} times in {inp['file_path']}. Must be unique."

        new_content = content.replace(actual, inp["new_string"], 1)
        path.write_text(new_content, encoding="utf-8")

        diff = _generate_diff(content, actual, inp["new_string"])
        quote_note = " (matched via quote normalization)" if actual != inp["old_string"] else ""
        return f"Successfully edited {inp['file_path']}{quote_note}\n\n{diff}"
    except Exception as e:
        return f"Error editing file: {e}"


def _list_files(inp: dict) -> str:
    try:
        base = Path(inp.get("path") or ".")
        pattern = inp["pattern"]
        files = []
        for p in base.glob(pattern):
            if p.is_file():
                rel = str(p.relative_to(base) if base != Path(".") else p)
                if "node_modules" in rel or ".git" in rel.split(os.sep):
                    continue
                files.append(rel)
                if len(files) >= 200:
                    break
        if not files:
            return "No files found matching the pattern."
        result = "\n".join(files[:200])
        if len(files) > 200:
            result += f"\n... and {len(files) - 200} more"
        return result
    except Exception as e:
        return f"Error listing files: {e}"


def _grep_search(inp: dict) -> str:
    pattern = inp["pattern"]
    path = inp.get("path") or "."
    include = inp.get("include")
    return _grep_python(pattern, path, include)


def _grep_python(pattern: str, directory: str, include: str | None) -> str:
    regex = re.compile(pattern)
    include_pattern = include
    matches: list[str] = []

    def walk(d: str) -> None:
        if len(matches) >= 200:
            return
        try:
            entries = os.listdir(d)
        except Exception:
            return
        for name in entries:
            if name.startswith(".") or name == "node_modules":
                continue
            full = os.path.join(d, name)
            if os.path.isdir(full):
                walk(full)
                continue
            if include_pattern and not fnmatch.fnmatch(name, include_pattern):
                continue
            try:
                text = Path(full).read_text(errors="replace")
                for i, line in enumerate(text.split("\n")):
                    if regex.search(line):
                        matches.append(f"{full}:{i+1}:{line}")
                        if len(matches) >= 200:
                            return
            except Exception:
                pass

    walk(directory)
    if not matches:
        return "No matches found."
    output = "\n".join(matches[:100])
    if len(matches) > 100:
        output += f"\n... and {len(matches) - 100} more matches"
    return output


def _run_shell(inp: dict) -> str:
    try:
        timeout_ms = inp.get("timeout", 30000)
        timeout_s = timeout_ms / 1000
        result = subprocess.run(
            inp["command"],
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        output = result.stdout or ""
        if result.returncode != 0:
            stderr = f"\nStderr: {result.stderr}" if result.stderr else ""
            stdout = f"\nStdout: {result.stdout}" if result.stdout else ""
            return f"Command failed (exit code {result.returncode}){stdout}{stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after {inp.get('timeout', 30000)}ms"
    except Exception as e:
        return f"Error: {e}"


def _web_fetch(inp: dict) -> str:
    import requests
    from bs4 import BeautifulSoup

    url = inp.get("url", "")
    max_length = inp.get("max_length", 50000)
    try:
        resp = requests.get(url, headers={"User-Agent": "mycoder/1.0"}, timeout=30)
        resp.encoding = "utf-8"
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        return f"HTTP error: {e.response.status_code} {e.response.reason}"
    except requests.exceptions.RequestException as e:
        return f"Error fetching {url}: {e}"

    if "html" in resp.headers.get("Content-Type", ""):
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
    else:
        text = resp.text

    if len(text) > max_length:
        text = text[:max_length] + f"\n\n[... truncated at {max_length} characters]"

    return text or "(empty response)"


# ─── Dangerous command patterns ─────────────────────────────

DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s"),
    re.compile(r"\bgit\s+(push|reset|clean|checkout\s+\.)"),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s"),
    re.compile(r">\s*/dev/"),
    re.compile(r"\bkill\b"),
    re.compile(r"\bpkill\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\bdel\s", re.IGNORECASE),
    re.compile(r"\brmdir\s", re.IGNORECASE),
    re.compile(r"\bformat\s", re.IGNORECASE),
    re.compile(r"\btaskkill\s", re.IGNORECASE),
    re.compile(r"\bRemove-Item\s", re.IGNORECASE),
    re.compile(r"\bStop-Process\s", re.IGNORECASE),
]


def is_dangerous(command: str) -> bool:
    return any(p.search(command) for p in DANGEROUS_PATTERNS)


# ─── Permission check ────────────────────────────────────────

def check_permission(tool_name: str, inp: dict, mode: PermissionMode = "default") -> dict:
    """Returns {"action": "allow"|"deny"|"confirm", "message": ...}"""
    if mode == "bypassPermissions":
        return {"action": "allow"}

    if tool_name in READ_TOOLS:
        return {"action": "allow"}

    if mode == "acceptEdits" and tool_name in EDIT_TOOLS:
        return {"action": "allow"}

    needs_confirm = False
    confirm_message = ""

    if tool_name == "run_shell" and is_dangerous(inp.get("command", "")):
        needs_confirm = True
        confirm_message = inp.get("command", "")
    elif tool_name == "write_file" and not Path(inp.get("file_path", "")).exists():
        needs_confirm = True
        confirm_message = f"write new file: {inp.get('file_path', '')}"
    elif tool_name == "edit_file" and not Path(inp.get("file_path", "")).exists():
        needs_confirm = True
        confirm_message = f"edit non-existent file: {inp.get('file_path', '')}"

    if needs_confirm:
        if mode == "dontAsk":
            return {"action": "deny", "message": f"Auto-denied (dontAsk mode): {confirm_message}"}
        return {"action": "confirm", "message": confirm_message}

    return {"action": "allow"}


# ─── Truncate long tool results ─────────────────────────────

MAX_RESULT_CHARS = 50000


def _truncate_result(result: str) -> str:
    if len(result) <= MAX_RESULT_CHARS:
        return result
    keep_each = (MAX_RESULT_CHARS - 60) // 2
    return (
        result[:keep_each]
        + f"\n\n[... truncated {len(result) - keep_each * 2} chars ...]\n\n"
        + result[-keep_each:]
    )


# ─── Execute a tool call ────────────────────────────────────

async def execute_tool(
    name: str, inp: dict, read_file_state: dict[str, float] | None = None
) -> str:
    if name == "read_file":
        result = _read_file(inp)
        if read_file_state is not None and not result.startswith("Error"):
            abs_path = str(Path(inp["file_path"]).resolve())
            try:
                read_file_state[abs_path] = os.path.getmtime(abs_path)
            except OSError:
                pass
        return _truncate_result(result)

    if name in ("write_file", "edit_file") and read_file_state is not None:
        abs_path = str(Path(inp["file_path"]).resolve())
        if os.path.exists(abs_path):
            if abs_path not in read_file_state:
                verb = "writing" if name == "write_file" else "editing"
                return f"Error: You must read this file before {verb}. Use read_file first to see its current contents."
            if os.path.getmtime(abs_path) != read_file_state[abs_path]:
                verb = "writing" if name == "write_file" else "editing"
                return f"Warning: {inp['file_path']} was modified externally since your last read. Please read_file again before {verb}."

    handlers: dict = {
        "write_file": _write_file,
        "edit_file": _edit_file,
        "list_files": _list_files,
        "grep_search": _grep_search,
        "run_shell": _run_shell,
        "web_fetch": _web_fetch,
    }
    handler = handlers.get(name)
    if not handler:
        return f"Unknown tool: {name}"
    result = _truncate_result(handler(inp))

    if name in ("write_file", "edit_file") and read_file_state is not None and not result.startswith("Error"):
        abs_path = str(Path(inp["file_path"]).resolve())
        try:
            read_file_state[abs_path] = os.path.getmtime(abs_path)
        except OSError:
            pass

    return result
