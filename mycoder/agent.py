"""Agent core loop — dual backend (Anthropic + OpenAI compatible), streaming,
parallel tool execution, auto-compact."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Awaitable

import anthropic
import openai

from .tools import (
    tool_definitions,
    execute_tool,
    check_permission,
    CONCURRENCY_SAFE_TOOLS,
    PermissionMode,
)
from .ui import (
    print_assistant_text,
    print_tool_call,
    print_tool_result,
    print_confirmation,
    print_divider,
    print_cost,
    print_info,
    start_spinner,
    stop_spinner,
)
from .session import save_session
from .prompt import build_system_prompt

# ─── Compression constants ───────────────────────────────────

SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIP_THRESHOLD = 0.60
MICROCOMPACT_IDLE_S = 5 * 60
KEEP_RECENT_RESULTS = 3

# ─── Retry with exponential backoff ──────────────────────────


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


async def _with_retry(fn, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else "network error"
            print_info(f"Retry {attempt + 1}/{max_retries} ({reason}), waiting {delay:.1f}s...")
            await asyncio.sleep(delay)


# ─── Model context windows ────────────────────────────────────

MODEL_CONTEXT = {
    "claude-opus-4-7": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}


def _get_context_window(model: str) -> int:
    return MODEL_CONTEXT.get(model, 200000)


# ─── Convert tools to OpenAI format ──────────────────────────


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# ─── Agent ────────────────────────────────────────────────────


class Agent:
    def __init__(
        self,
        *,
        permission_mode: PermissionMode = "default",
        model: str = "claude-sonnet-4-6",
        api_base: str | None = None,
        api_key: str | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
    ):
        self.permission_mode = permission_mode
        self.model = model
        self.use_openai = bool(api_base)
        self.confirm_fn = confirm_fn
        self.effective_window = _get_context_window(model) - 20000
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        self.last_api_call_time = 0.0

        self._aborted = False
        self._current_task: asyncio.Task | None = None
        self._confirmed_paths: set[str] = set()
        self._read_file_state: dict[str, float] = {}

        self._anthropic_messages: list[dict] = []
        self._openai_messages: list[dict] = []

        from .memory import build_memory_prompt_section
        self._system_prompt = build_system_prompt(memory_section=build_memory_prompt_section())

        if self.use_openai:
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs: dict[str, Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None

    # ─── Public API ───────────────────────────────────────────

    async def chat(self, user_message: str) -> None:
        self._aborted = False
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.current_task()
        try:
            await coro
        except asyncio.CancelledError:
            self._aborted = True
        finally:
            self._current_task = None
        print_divider()
        self._auto_save()

    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def clear_history(self) -> None:
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        print_info("Conversation cleared.")

    def show_cost(self) -> None:
        cost = (self.total_input_tokens / 1_000_000) * 3 + (self.total_output_tokens / 1_000_000) * 15
        print_info(f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out | Cost: ${cost:.4f}")

    def restore_session(self, data: dict) -> None:
        if data.get("anthropicMessages"):
            self._anthropic_messages = data["anthropicMessages"]
        if data.get("openaiMessages"):
            self._openai_messages = data["openaiMessages"]
        meta = data.get("metadata") or {}
        self.total_input_tokens = int(meta.get("totalInputTokens") or 0)
        self.total_output_tokens = int(meta.get("totalOutputTokens") or 0)
        count = len(self._openai_messages if self.use_openai else self._anthropic_messages)
        print_info(f"Session restored ({count} messages).")

    # ─── Session ──────────────────────────────────────────────

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "totalInputTokens": self.total_input_tokens,
                    "totalOutputTokens": self.total_output_tokens,
                },
                "anthropicMessages": self._anthropic_messages if not self.use_openai else None,
                "openaiMessages": self._openai_messages if self.use_openai else None,
            })
        except Exception:
            pass

    # ─── Auto-compact ─────────────────────────────────────────

    async def _check_and_compact(self) -> None:
        self._run_compression_pipeline()
        if self.last_input_token_count > self.effective_window * 0.85:
            pct = int(self.last_input_token_count / self.effective_window * 100)
            print_info(f"Context {pct}% full, compacting...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        if self.use_openai:
            await self._compact_openai()
        else:
            await self._compact_anthropic()
        print_info("Conversation compacted.")

    async def _compact_anthropic(self) -> None:
        if len(self._anthropic_messages) < 4:
            return
        last_user_msg = self._anthropic_messages[-1]
        resp = await self._anthropic_client.messages.create(
            model=self.model,
            max_tokens=2048,
            system="You are a conversation summarizer. Be concise but preserve important details.",
            messages=[
                *self._anthropic_messages[:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary = resp.content[0].text if resp.content and resp.content[0].type == "text" else "No summary available."
        self._anthropic_messages = [
            {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            self._anthropic_messages.append(last_user_msg)
        self.last_input_token_count = 0

    async def _compact_openai(self) -> None:
        if len(self._openai_messages) < 5:
            return
        system_msg = self._openai_messages[0]
        last_user_msg = self._openai_messages[-1]
        resp = await self._openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a conversation summarizer. Be concise but preserve important details."},
                *self._openai_messages[1:-1],
                {"role": "user", "content": "Summarize the conversation so far in a concise paragraph, preserving key decisions, file paths, and context needed to continue the work."},
            ],
        )
        summary = resp.choices[0].message.content or "No summary available."
        self._openai_messages = [
            system_msg,
            {"role": "user", "content": f"[Previous conversation summary]\n{summary}"},
            {"role": "assistant", "content": "Understood. I have the context from our previous conversation. How can I continue helping?"},
        ]
        if last_user_msg.get("role") == "user":
            self._openai_messages.append(last_user_msg)
        self.last_input_token_count = 0

    # ─── Multi-tier compression pipeline ──────────────────────

    def _run_compression_pipeline(self) -> None:
        if self.use_openai:
            self._budget_tool_results_openai()
            self._snip_stale_results_openai()
            self._microcompact_openai()
        else:
            self._budget_tool_results_anthropic()
            self._snip_stale_results_anthropic()
            self._microcompact_anthropic()

    def _budget_tool_results_anthropic(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self._anthropic_messages:
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    keep = (budget - 80) // 2
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]

    def _budget_tool_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < 0.5:
            return
        budget = 15000 if utilization > 0.7 else 30000
        for msg in self._openai_messages:
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]

    def _snip_stale_results_anthropic(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        results = []
        for mi, msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get("tool_use_id")
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                        results.append({"mi": mi, "bi": bi, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})
        if len(results) <= KEEP_RECENT_RESULTS:
            return
        to_snip: set[int] = set()
        seen_files: dict[str, list[int]] = {}
        for i, r in enumerate(results):
            if r["name"] == "read_file" and r.get("file_path"):
                seen_files.setdefault(r["file_path"], []).append(i)
        for indices in seen_files.values():
            if len(indices) > 1:
                for j in indices[:-1]:
                    to_snip.add(j)
        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range(snip_before):
            to_snip.add(i)
        for idx in to_snip:
            r = results[idx]
            self._anthropic_messages[r["mi"]]["content"][r["bi"]]["content"] = SNIP_PLACEHOLDER

    def _snip_stale_results_openai(self) -> None:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self._openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER

    def _microcompact_anthropic(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        all_results = []
        for mi, msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                    all_results.append((mi, bi))
        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self._anthropic_messages[mi]["content"][bi]["content"] = "[Old result cleared]"

    def _microcompact_openai(self) -> None:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self._openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"

    def _find_tool_use_by_id(self, tool_use_id: str) -> dict | None:
        for msg in self._anthropic_messages:
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return {"name": block["name"], "input": block.get("input", {})}
        return None

    # ─── Permission & confirmation ────────────────────────────

    async def _confirm_dangerous(self, message: str) -> bool:
        print_confirmation(message)
        if self.confirm_fn:
            return await self.confirm_fn(message)
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    async def _timed_tool_call(self, name: str, inp: dict) -> tuple[str, float]:
        start = time.monotonic()
        result = await execute_tool(name, inp, self._read_file_state)
        return result, time.monotonic() - start

    # ─── Anthropic backend ────────────────────────────────────

    async def _chat_anthropic(self, user_message: str) -> None:
        self._anthropic_messages.append({"role": "user", "content": user_message})

        while True:
            if self._aborted:
                break

            start_spinner()
            early_executions: dict[str, asyncio.Task] = {}

            def _on_tool_block(block: dict):
                if block["name"] not in CONCURRENCY_SAFE_TOOLS:
                    return
                perm = check_permission(block["name"], block["input"], self.permission_mode)
                if perm["action"] == "allow":
                    task = asyncio.create_task(self._timed_tool_call(block["name"], block["input"]))
                    early_executions[block["id"]] = task

            response = await self._call_anthropic_stream(on_tool_block_complete=_on_tool_block)
            stop_spinner()

            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self.last_input_token_count = response.usage.input_tokens
            self.last_api_call_time = time.time()

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            self._anthropic_messages.append({
                "role": "assistant",
                "content": [self._block_to_dict(b) for b in response.content],
            })

            if not tool_uses:
                print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            tool_results: list[dict] = []
            for tu in tool_uses:
                if self._aborted:
                    break
                inp = dict(tu.input) if hasattr(tu.input, "items") else tu.input
                print_tool_call(tu.name, inp)

                early_task = early_executions.get(tu.id)
                if early_task:
                    raw, dur = await early_task
                    print_tool_result(tu.name, raw, duration=dur)
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": raw})
                    continue

                perm = check_permission(tu.name, inp, self.permission_mode)
                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm" and perm.get("message") not in self._confirmed_paths:
                    confirmed = await self._confirm_dangerous(perm["message"])
                    if not confirmed:
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "User denied this action."})
                        continue
                    self._confirmed_paths.add(perm["message"])

                raw, dur = await self._timed_tool_call(tu.name, inp)
                print_tool_result(tu.name, raw, duration=dur)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": raw})

            if tool_results:
                self._anthropic_messages.append({"role": "user", "content": tool_results})

            await self._check_and_compact()

    @staticmethod
    def _block_to_dict(block) -> dict:
        if block.type == "text":
            return {"type": "text", "text": block.text}
        if block.type == "tool_use":
            return {"type": "tool_use", "id": block.id, "name": block.name,
                    "input": dict(block.input) if hasattr(block.input, "items") else block.input}
        return {"type": block.type}

    async def _call_anthropic_stream(self, on_tool_block_complete=None):
        async def _do():
            tool_blocks_by_index: dict[int, dict] = {}
            first_text = True

            async with self._anthropic_client.messages.stream(
                model=self.model,
                max_tokens=16384,
                system=self._system_prompt,
                tools=tool_definitions,
                messages=self._anthropic_messages,
            ) as stream:
                async for event in stream:
                    if not hasattr(event, "type"):
                        continue

                    if event.type == "content_block_start":
                        cb = getattr(event, "content_block", None)
                        if cb and getattr(cb, "type", None) == "tool_use":
                            tool_blocks_by_index[event.index] = {
                                "id": cb.id, "name": cb.name, "input_json": "",
                            }

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            if first_text:
                                stop_spinner()
                                print_assistant_text("\n")
                                first_text = False
                            print_assistant_text(delta.text)
                        elif hasattr(delta, "partial_json"):
                            tb = tool_blocks_by_index.get(event.index)
                            if tb:
                                tb["input_json"] += delta.partial_json

                    elif event.type == "content_block_stop":
                        tb = tool_blocks_by_index.pop(event.index, None)
                        if tb and on_tool_block_complete:
                            try:
                                parsed = json.loads(tb["input_json"] or "{}")
                            except Exception:
                                parsed = {}
                            on_tool_block_complete({
                                "type": "tool_use", "id": tb["id"],
                                "name": tb["name"], "input": parsed,
                            })

                return await stream.get_final_message()

        return await _with_retry(_do)

    # ─── OpenAI-compatible backend ────────────────────────────

    async def _chat_openai(self, user_message: str) -> None:
        self._openai_messages.append({"role": "user", "content": user_message})

        while True:
            if self._aborted:
                break

            start_spinner()
            early_executions: dict[int, asyncio.Task] = {}

            def _on_tool_ready(tc_ready: dict):
                if tc_ready["name"] not in CONCURRENCY_SAFE_TOOLS:
                    return
                perm = check_permission(tc_ready["name"], tc_ready["input"], self.permission_mode)
                if perm["action"] != "allow":
                    return
                task = asyncio.create_task(self._timed_tool_call(tc_ready["name"], tc_ready["input"]))
                early_executions[tc_ready["index"]] = task

            response = await self._call_openai_stream(on_tool_call_ready=_on_tool_ready)
            stop_spinner()

            if response.get("usage"):
                self.total_input_tokens += response["usage"]["prompt_tokens"]
                self.total_output_tokens += response["usage"]["completion_tokens"]
                self.last_input_token_count = response["usage"]["prompt_tokens"]
                self.last_api_call_time = time.time()

            message = (response.get("choices") or [{}])[0].get("message", {})
            self._openai_messages.append(message)

            tool_calls = message.get("tool_calls")
            if not tool_calls:
                print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            # Phase 1: permission check (serial)
            checked: list[dict] = []
            for i, tc in enumerate(tool_calls):
                if self._aborted:
                    break
                if tc.get("type") != "function":
                    continue
                fn_name = tc["function"]["name"]
                try:
                    inp = json.loads(tc["function"]["arguments"])
                except Exception:
                    inp = {}

                print_tool_call(fn_name, inp)

                if i in early_executions:
                    checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True, "early_index": i})
                    continue

                perm = check_permission(fn_name, inp, self.permission_mode)
                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm" and perm.get("message") not in self._confirmed_paths:
                    confirmed = await self._confirm_dangerous(perm["message"])
                    if not confirmed:
                        checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False, "result": "User denied this action."})
                        continue
                    self._confirmed_paths.add(perm["message"])
                checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            # Phase 2: execute (batch parallel for consecutive safe tools)
            batches: list[dict] = []
            for ct in checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and batches and batches[-1]["concurrent"]:
                    batches[-1]["items"].append(ct)
                else:
                    batches.append({"concurrent": safe, "items": [ct]})

            for batch in batches:
                if self._aborted:
                    break
                if batch["concurrent"]:
                    async def _run_safe(ct_item: dict) -> tuple[dict, str]:
                        eidx = ct_item.get("early_index")
                        if eidx is not None and eidx in early_executions:
                            raw, dur = await early_executions[eidx]
                        else:
                            raw, dur = await self._timed_tool_call(ct_item["fn"], ct_item["inp"])
                        print_tool_result(ct_item["fn"], raw, duration=dur)
                        return ct_item, raw

                    results = await asyncio.gather(*[_run_safe(ct) for ct in batch["items"]])
                    for ct_item, res in results:
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                else:
                    for ct in batch["items"]:
                        if not ct["allowed"]:
                            self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": ct["result"]})
                            continue
                        raw, dur = await self._timed_tool_call(ct["fn"], ct["inp"])
                        print_tool_result(ct["fn"], raw, duration=dur)
                        self._openai_messages.append({"role": "tool", "tool_call_id": ct["tc"]["id"], "content": raw})

            await self._check_and_compact()

    async def _call_openai_stream(self, on_tool_call_ready=None) -> dict:
        async def _do():
            stream = await self._openai_client.chat.completions.create(
                model=self.model,
                tools=_to_openai_tools(tool_definitions),
                messages=self._openai_messages,
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None
            started_indices: set[int] = set()

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                if delta and delta.content:
                    if first_text:
                        stop_spinner()
                        print_assistant_text("\n")
                        first_text = False
                    print_assistant_text(delta.content)
                    content += delta.content

                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        existing = tool_calls.get(idx)
                        if existing:
                            if tc.function and tc.function.name:
                                existing["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments
                            if tc.id and not existing["id"]:
                                existing["id"] = tc.id
                        else:
                            tool_calls[idx] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }

                        if on_tool_call_ready and idx not in started_indices:
                            current = tool_calls[idx]
                            if not current["id"] or not current["name"]:
                                continue
                            args = current["arguments"]
                            stripped = args.rstrip()
                            if not stripped or stripped[-1] not in ("}", "]"):
                                continue
                            try:
                                parsed = json.loads(args)
                            except json.JSONDecodeError:
                                continue
                            on_tool_call_ready({"index": idx, "id": current["id"], "name": current["name"], "input": parsed})
                            started_indices.add(idx)

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{"message": {"role": "assistant", "content": content or None, "tool_calls": assembled}, "finish_reason": finish_reason or "stop"}],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)
