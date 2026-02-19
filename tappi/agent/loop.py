"""Agent loop — the core multi-turn, multi-step LLM executor.

This is intentionally simple: send messages → get response → if tool
calls, execute them and loop → if text, return it. No framework needed.

Uses LiteLLM for provider-agnostic LLM calls.
"""

from __future__ import annotations

import json
import time
import sys
from pathlib import Path
from typing import Any, Callable

from tappi.agent.config import (
    get_agent_config,
    get_model,
    get_provider,
    get_provider_key,
    get_workspace,
    PROVIDERS,
)
from tappi.agent.tools.browser import BrowserTool, TOOL_SCHEMA as BROWSER_SCHEMA
from tappi.agent.tools.files import FilesTool, TOOL_SCHEMA as FILES_SCHEMA
from tappi.agent.tools.pdf import PDFTool, TOOL_SCHEMA as PDF_SCHEMA
from tappi.agent.tools.spreadsheet import SpreadsheetTool, TOOL_SCHEMA as SPREADSHEET_SCHEMA
from tappi.agent.tools.shell import ShellTool, TOOL_SCHEMA as SHELL_SCHEMA
from tappi.agent.tools.cron import CronTool, TOOL_SCHEMA as CRON_SCHEMA


SYSTEM_PROMPT = """\
You are a capable AI assistant with browser control, file management, and \
automation skills. You operate within a designated workspace directory and \
can control a web browser to accomplish tasks.

Today's date is {today}.

## Your Tools

- **browser**: Navigate, click, type, read pages, take screenshots. Use your \
real browser with saved logins. Use action="search" to Google something.
- **files**: Read, write, list, move, copy, delete, **grep** files in the workspace. \
Use action="grep" with a query to search file contents (case-insensitive, \
returns file:line:content matches).
- **pdf**: Read text from PDFs, create PDFs from HTML.
- **spreadsheet**: Read/write CSV and Excel files.
- **shell**: Run shell commands (working directory = workspace).
- **cron**: Schedule recurring tasks.

## Key Patterns

1. **Browser workflow**: open URL → wait (if needed) → elements → click/type → \
text (read result). Always call 'elements' after navigating to see the page.
2. **Wait for page loads**: After clicking or navigating, use browser wait \
(1000-2000ms) before reading elements. Pages need time to render.
3. **Be persistent**: If elements aren't found, scroll down or wait longer. \
Web pages load dynamically.
4. **File paths**: All file paths are relative to the workspace directory.

## Context & Memory

Your context window is {context_limit:,} tokens. You're currently at \
{context_used:,} tokens ({context_pct}% used).

When your context gets large, it may be **compacted** — your full conversation \
is saved to `context_dumps/` and replaced with a summary. If that happens, \
you'll be told where the dump is. Use `files grep` to look up specific details \
from prior conversation — do NOT read the full dump file (it's too large for \
your context). Grep for keywords instead.

## Important

- Think step by step for complex tasks.
- When browsing, always check elements after navigation.
- If something fails, try an alternative approach.
- Report what you did and any results clearly.

Workspace: {workspace}
"""


class Agent:
    """Multi-turn LLM agent with tool execution.

    The agent maintains conversation history and executes tools in a
    loop until the LLM produces a text response (no more tool calls).

    For complex tasks, the agent decomposes the task into subtasks, each
    executed by a focused mini-agent with its designated tool. The final
    subtask compiles all outputs into a coherent response.

    Args:
        workspace: Override workspace directory.
        browser_profile: Default browser profile to use.
        on_tool_call: Callback when a tool is called (name, params, result).
        on_message: Callback when the LLM produces text.
        on_token_update: Callback when token usage updates (usage_dict).
        on_subtask_progress: Callback for subtask decomposition progress.
        max_iterations: Safety limit on tool call loops (default: 50).
    """

    def __init__(
        self,
        workspace: Path | None = None,
        browser_profile: str | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        on_job_trigger: Callable[[dict], None] | None = None,
        on_token_update: Callable[[dict], None] | None = None,
        on_subtask_progress: Callable[[dict], None] | None = None,
        max_iterations: int = 50,
    ) -> None:
        self.workspace = workspace or get_workspace()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.max_iterations = max_iterations
        self.on_tool_call = on_tool_call
        self.on_message = on_message
        self.on_token_update = on_token_update
        self.on_subtask_progress = on_subtask_progress

        # Decomposition toggle — read from config, can be overridden at runtime
        cfg = get_agent_config()
        self._decompose_enabled = cfg.get("decompose_enabled", True)

        # Initialize tools — browser downloads go to workspace/downloads
        downloads_dir = str(self.workspace / "downloads")
        self._browser = BrowserTool(default_profile=browser_profile, download_dir=downloads_dir)
        self._files = FilesTool(workspace=self.workspace)
        self._pdf = PDFTool(workspace=self.workspace)
        self._spreadsheet = SpreadsheetTool(workspace=self.workspace)
        self._shell = ShellTool(workspace=self.workspace)
        self._cron = CronTool(on_job_change=on_job_trigger)

        self._tools = {
            "browser": self._browser,
            "files": self._files,
            "pdf": self._pdf,
            "spreadsheet": self._spreadsheet,
            "shell": self._shell,
            "cron": self._cron,
        }

        self._tool_schemas = [
            BROWSER_SCHEMA,
            FILES_SCHEMA,
            PDF_SCHEMA,
            SPREADSHEET_SCHEMA,
            SHELL_SCHEMA,
            CRON_SCHEMA,
        ]

        # Conversation history
        self.messages: list[dict[str, Any]] = []
        self._custom_system_prompt: str | None = None  # set externally for sub-agents

        # Token tracking
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

        # Session management
        self.session_id: str | None = None
        self._last_prompt_tokens: int = 0  # context size from most recent LLM call

        # Loop control
        self._abort = False  # set True to stop the loop on next iteration
        self._last_activity: dict[str, Any] = {}  # last tool call info + timestamp

        # Subtask runner reference (for probe to see active sub-agent)
        self._active_runner: Any = None

    def _get_timeout(self) -> int:
        """Get LLM call timeout from config (default 300s)."""
        cfg = get_agent_config()
        return cfg.get("timeout", 300)

    def _get_max_tokens(self) -> int:
        """Get max output tokens from config.

        Uses subagent_max_tokens if this is a sub-agent (custom system prompt),
        otherwise main_max_tokens. Falls back to legacy max_tokens, then defaults.
        """
        cfg = get_agent_config()
        if self._custom_system_prompt:
            return min(cfg.get("subagent_max_tokens", cfg.get("max_tokens", 4096)), 64000)
        return min(cfg.get("main_max_tokens", cfg.get("max_tokens", 8192)), 64000)

    def _build_system_prompt(self) -> str:
        """Build system prompt with current context usage stats."""
        from datetime import date as _date
        from tappi.agent.sessions import get_context_limit

        model = get_model()
        context_limit = get_context_limit(model)
        context_used = self._last_prompt_tokens
        context_pct = round(context_used / context_limit * 100) if context_limit else 0
        today = _date.today().strftime("%B %d, %Y")

        fmt = dict(
            workspace=self.workspace,
            context_limit=context_limit,
            context_used=context_used,
            context_pct=context_pct,
            today=today,
        )

        if self._custom_system_prompt:
            return self._custom_system_prompt.format(**fmt)

        return SYSTEM_PROMPT.format(**fmt)

    def _setup_litellm(self) -> None:
        """Configure LiteLLM with the right provider credentials."""
        import litellm

        provider = get_provider()
        key = get_provider_key(provider)

        if not key:
            info = PROVIDERS.get(provider, {})
            raise ValueError(
                f"No API key found for {provider}.\n"
                f"Set it via: bpy setup\n"
                f"Or env var: {info.get('env_key', '?')}"
            )

        # Set the appropriate env vars for LiteLLM
        import os
        info = PROVIDERS.get(provider, {})

        if provider == "openrouter":
            os.environ["OPENROUTER_API_KEY"] = key
        elif provider in ("anthropic", "claude_max"):
            os.environ["ANTHROPIC_API_KEY"] = key
        elif provider == "openai":
            os.environ["OPENAI_API_KEY"] = key
        elif provider == "bedrock":
            # Bedrock uses AWS env vars — assume they're set
            pass
        elif provider == "azure":
            os.environ["AZURE_API_KEY"] = key
            agent_cfg = get_agent_config()
            azure_cfg = agent_cfg.get("providers", {}).get("azure", {})
            if azure_cfg.get("base_url"):
                os.environ["AZURE_API_BASE"] = azure_cfg["base_url"]
            if azure_cfg.get("api_version"):
                os.environ["AZURE_API_VERSION"] = azure_cfg["api_version"]
        elif provider == "vertex":
            # Vertex uses GOOGLE_APPLICATION_CREDENTIALS
            pass

    def _build_llm_kwargs(self) -> dict:
        """Build common kwargs for LLM calls."""
        import litellm

        self._setup_litellm()
        model = get_model()
        provider = get_provider()

        messages = [{"role": "system", "content": self._build_system_prompt()}] + self.messages

        kwargs = dict(
            model=model,
            messages=messages,
            tools=self._tool_schemas,
            tool_choice="auto",
            max_tokens=self._get_max_tokens(),
            timeout=self._get_timeout(),
        )

        # Reasoning effort — optional, off by default
        cfg = get_agent_config()
        reasoning = cfg.get("reasoning_effort")
        if reasoning:
            kwargs["reasoning_effort"] = reasoning

        # OpenRouter compatibility
        if provider == "openrouter":
            key = get_provider_key(provider)
            kwargs["api_key"] = key
            kwargs["base_url"] = "https://openrouter.ai/api/v1"
            kwargs["model"] = f"openai/{model}"

        return kwargs

    def _call_llm(self) -> dict:
        """Make a single LLM call and return the response (non-streaming)."""
        import litellm
        kwargs = self._build_llm_kwargs()
        response = litellm.completion(**kwargs)
        self._track_usage(response)
        return response

    def _call_llm_stream(self, on_chunk: Callable[[str], None] | None = None):
        """Make a streaming LLM call. Returns a synthetic response object.

        Streams text chunks via on_chunk callback. Accumulates tool calls.
        Returns an object matching the non-streaming response shape.
        """
        import litellm
        kwargs = self._build_llm_kwargs()
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        response_stream = litellm.completion(**kwargs)

        # Accumulate the full response from chunks
        content_parts = []
        tool_calls_map: dict[int, dict] = {}  # index -> {id, name, arguments}
        finish_reason = None

        for chunk in response_stream:
            if not chunk.choices:
                # Final chunk with usage info
                usage = getattr(chunk, "usage", None)
                if usage:
                    self._track_usage_raw(
                        getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0,
                    )
                continue

            delta = chunk.choices[0].delta
            finish_reason = chunk.choices[0].finish_reason or finish_reason

            # Text content
            if delta and delta.content:
                content_parts.append(delta.content)
                if on_chunk:
                    on_chunk(delta.content)

            # Tool calls (accumulated across chunks)
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    if tc_delta.id:
                        tool_calls_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_map[idx]["arguments"] += tc_delta.function.arguments

        # Build synthetic response matching non-streaming shape
        content = "".join(content_parts) if content_parts else None

        # Build tool_calls list
        tool_calls = None
        if tool_calls_map:
            tool_calls = []
            for idx in sorted(tool_calls_map.keys()):
                tc = tool_calls_map[idx]
                tool_calls.append(type("ToolCall", (), {
                    "id": tc["id"],
                    "function": type("Function", (), {
                        "name": tc["name"],
                        "arguments": tc["arguments"],
                    })(),
                })())

        # Build response object
        message = type("Message", (), {
            "content": content,
            "tool_calls": tool_calls,
        })()
        choice = type("Choice", (), {
            "message": message,
            "finish_reason": finish_reason,
        })()
        return type("Response", (), {"choices": [choice]})()

    def _track_usage(self, response) -> None:
        """Track token usage from a non-streaming response."""
        usage = getattr(response, "usage", None)
        if usage:
            self._track_usage_raw(
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
            )

    def _track_usage_raw(self, prompt_tokens: int, completion_tokens: int) -> None:
        """Track raw token counts."""
        self._last_prompt_tokens = prompt_tokens
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens = self.prompt_tokens + self.completion_tokens
        if self.on_token_update:
            self.on_token_update(self.get_token_usage())

    def _execute_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool and return the result string."""
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: {name}"

        result = tool.execute(**arguments)

        if self.on_tool_call:
            self.on_tool_call(name, arguments, result)

        return result

    def _do_context_dump(self, reason: str = "compaction") -> Path:
        """Dump current conversation to a file and compact messages.

        Args:
            reason: Why the dump happened ("compaction" or "flush").

        Returns:
            Path to the dump file.
        """
        from tappi.agent.sessions import get_context_limit

        model = get_model()
        context_limit = get_context_limit(model)

        dump_path = self.workspace / "context_dumps" / f"dump_{int(time.time())}.md"
        dump_path.parent.mkdir(parents=True, exist_ok=True)

        dump_lines = [
            f"# Context Dump — {time.strftime('%Y-%m-%d %H:%M:%S')} ({reason})",
            f"Model: {model} | Tokens: {self.total_tokens:,} / {context_limit:,}",
            f"Messages: {len(self.messages)}\n",
        ]
        for msg in self.messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if msg.get("tool_calls"):
                tc_info = ", ".join(
                    tc["function"]["name"] for tc in msg["tool_calls"]
                )
                dump_lines.append(f"## [{role}] tool_calls: {tc_info}")
                if content:
                    dump_lines.append(content[:2000])
            elif role == "tool":
                dump_lines.append(f"## [tool] {msg.get('tool_call_id', '')}")
                dump_lines.append((content or "")[:2000])
            else:
                dump_lines.append(f"## [{role}]")
                dump_lines.append((content or "")[:5000])
            dump_lines.append("")

        dump_path.write_text("\n".join(dump_lines))

        # Build summary
        summary_parts = ["## Conversation Summary (context compacted)\n"]
        for msg in self.messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if role == "user":
                summary_parts.append(f"**User:** {content[:500]}")
            elif role == "assistant" and content:
                summary_parts.append(f"**Assistant:** {content[:1000]}")
            elif role == "tool":
                summary_parts.append(f"*[tool result: {len(content or '')} chars]*")

        summary = "\n".join(summary_parts)
        if len(summary) > 8000:
            summary = summary[:8000] + "\n\n*[summary truncated]*"

        # Replace messages with compact summary
        dump_rel = dump_path.relative_to(self.workspace)
        self.messages.clear()

        # Reset token counts
        self._last_prompt_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

        self.messages.append({
            "role": "user",
            "content": (
                f"[CONTEXT COMPACTED] Your conversation history was too large and has "
                f"been compacted. The full raw conversation was saved to: `{dump_rel}`\n\n"
                f"Your context window is {context_limit:,} tokens. After compaction you're "
                f"near 0% — you have the full window available again.\n\n"
                f"**To recover details from before compaction:** use `files` with "
                f'action="grep", query="<keyword>" and path="{dump_rel.parent}" to '
                f"search for specific information. Do NOT read the full dump file — "
                f"it's too large. Grep for what you need.\n\n"
                f"{summary}\n\n"
                "Continue from where we left off."
            ),
        })

        return dump_path

    def _check_context_compact(self) -> None:
        """If context window usage exceeds 75%, dump and compact."""
        from tappi.agent.sessions import get_context_limit

        model = get_model()
        context_limit = get_context_limit(model)
        threshold = int(context_limit * 0.75)

        if self._last_prompt_tokens < threshold:
            return

        self._do_context_dump("compaction")

    def chat(self, user_message: str) -> str:
        """Send a message and get a response.

        For complex tasks, decomposes into subtasks and runs each via a
        focused mini-agent. For simple tasks (conversational, single tool
        call), uses the direct tool-calling loop.

        Args:
            user_message: The user's message/task.

        Returns:
            The agent's final text response.
        """
        # If this agent has a custom system prompt, it's a sub-agent —
        # skip decomposition and go straight to the direct loop.
        if self._custom_system_prompt:
            return self._chat_direct(user_message)

        # If decomposition is disabled, go direct
        if not self._decompose_enabled:
            return self._chat_direct(user_message)

        # Try to decompose the task
        self._last_activity = {"state": "decomposing", "time": time.time()}
        try:
            from tappi.agent.decompose import decompose_task, SubtaskRunner, Subtask
            subtasks = decompose_task(user_message)
        except Exception:
            subtasks = None

        if subtasks is None:
            # Simple task — use direct loop
            return self._chat_direct(user_message)

        # Complex task — run via subtask decomposition
        return self._chat_decomposed(user_message, subtasks)

    def _chat_decomposed(self, user_message: str, subtasks: list) -> str:
        """Execute a complex task via subtask decomposition.

        Each subtask runs in a fresh mini-agent. Results are compiled by
        the final compilation subtask. The main agent's conversation
        history gets a summary of what happened.
        """
        import threading
        from tappi.agent.decompose import SubtaskRunner, Subtask

        self.messages.append({"role": "user", "content": user_message})
        self._abort = False
        self._last_activity = {"state": "running_subtasks", "time": time.time()}

        # Notify UI of decomposition plan
        if self.on_subtask_progress:
            self.on_subtask_progress({
                "type": "plan",
                "subtasks": [s.to_dict() for s in subtasks],
            })

        abort_event = threading.Event()

        def on_subtask_start(st: Subtask) -> None:
            _active_subtask_index[0] = st.index
            self._last_activity = {
                "state": "subtask",
                "index": st.index,
                "total": st.total,
                "task": st.task[:100],
                "tool": st.tool,
                "time": time.time(),
            }
            if self.on_subtask_progress:
                self.on_subtask_progress({
                    "type": "subtask_start",
                    "subtask": st.to_dict(),
                })

        def on_subtask_done(st: Subtask) -> None:
            if self.on_subtask_progress:
                self.on_subtask_progress({
                    "type": "subtask_done",
                    "subtask": st.to_dict(),
                })

        # Stream chunks: broadcast to UI via subtask_progress callback
        # Include the active subtask index so the UI knows where to render
        _active_subtask_index = [0]  # mutable container for closure

        def on_stream_chunk(chunk: str) -> None:
            if self.on_subtask_progress:
                self.on_subtask_progress({
                    "type": "stream_chunk",
                    "chunk": chunk,
                    "index": _active_subtask_index[0],
                })

        runner = SubtaskRunner(
            subtasks=subtasks,
            workspace=self.workspace,
            browser_profile=self._browser._default_profile if hasattr(self._browser, '_default_profile') else None,
            on_subtask_start=on_subtask_start,
            on_subtask_done=on_subtask_done,
            on_tool_call=self.on_tool_call,
            on_token_update=self.on_token_update,
            on_stream_chunk=on_stream_chunk,
            abort_event=abort_event,
            original_task=user_message,
        )
        self._active_runner = runner

        result = runner.run()
        self._active_runner = None

        # Add summary to conversation history
        summary_parts = [f"**Task decomposed into {len(subtasks)} subtasks:**\n"]
        for st in subtasks:
            status_icon = "✅" if st.status == "done" else "❌"
            summary_parts.append(f"{status_icon} **Step {st.index + 1}** ({st.tool}): {st.task[:100]}")

        if result.get("final_output"):
            summary_parts.append(f"\n**Output directory:** `{result['output_dir']}`")
            summary_parts.append(f"**Duration:** {result['duration_seconds']}s")

        summary = "\n".join(summary_parts)
        self.messages.append({"role": "assistant", "content": summary})

        # Track aggregate tokens
        self.total_tokens += result.get("total_tokens", 0)

        # Return the final compiled output or summary
        final_output = result.get("final_output", "")
        if final_output:
            response = final_output
        else:
            response = summary

        if self.on_message:
            self.on_message(response)

        self._last_activity = {"state": "done", "time": time.time()}
        return response

    def _chat_direct(self, user_message: str) -> str:
        """Direct tool-calling loop for simple tasks and sub-agents.

        Runs until the LLM produces a final text response (no more tool calls).
        Context is automatically compacted when token usage exceeds 75%.
        """
        self.messages.append({"role": "user", "content": user_message})

        # Snapshot current browser tabs so cleanup knows what's pre-existing
        try:
            self._browser.snapshot_tabs()
        except Exception:
            pass

        iteration = 0
        self._abort = False
        self._last_activity = {"state": "starting", "time": time.time()}
        while True:
            iteration += 1

            # Check abort flag (set by flush())
            if self._abort:
                self._abort = False
                self._do_context_dump("flush")
                self._last_activity = {"state": "flushed", "time": time.time()}
                return "(Flushed — context saved to context_dumps/. Use grep to recover details.)"

            # Check if context needs compacting before calling LLM
            self._last_activity = {"state": "calling_llm", "iteration": iteration, "time": time.time()}
            self._check_context_compact()

            # Stream all LLM calls — text chunks fire on_stream callback
            # (for sub-agents this streams findings to the UI)
            stream_cb = getattr(self, '_on_stream_chunk', None)
            response = self._call_llm_stream(on_chunk=stream_cb)
            choice = response.choices[0]
            msg = choice.message

            # Add assistant message to history
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if msg.content:
                assistant_msg["content"] = msg.content
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]
            self.messages.append(assistant_msg)

            # If no tool calls, check if the model emitted tool calls as text
            # (common with weaker models like Qwen, Llama, etc.)
            if not msg.tool_calls and msg.content:
                parsed = self._try_parse_text_tool_call(msg.content)
                if parsed:
                    # Re-inject as a proper tool call
                    tc_id = f"text_tc_{iteration}"
                    self.messages[-1]["tool_calls"] = [{
                        "id": tc_id,
                        "type": "function",
                        "function": {
                            "name": parsed["name"],
                            "arguments": json.dumps(parsed["args"]),
                        },
                    }]
                    # Strip the tool call text from the content
                    if self.messages[-1].get("content"):
                        cleaned = self._strip_tool_call_text(self.messages[-1]["content"])
                        if cleaned.strip():
                            self.messages[-1]["content"] = cleaned
                        else:
                            del self.messages[-1]["content"]

                    result = self._execute_tool(parsed["name"], parsed["args"])
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result,
                    })
                    continue  # Let LLM see the result

                text = msg.content or ""
                if self.on_message:
                    self.on_message(text)
                return text

            if not msg.tool_calls:
                return ""

            # Execute each tool call
            for tc in msg.tool_calls:
                if self._abort:
                    break

                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                self._last_activity = {
                    "state": "tool_call",
                    "tool": tc.function.name,
                    "params": args,
                    "iteration": iteration,
                    "time": time.time(),
                }
                result = self._execute_tool(tc.function.name, args)

                # Add tool result to history
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Continue loop — LLM will see tool results and decide next step

            # Safety valve — respect max_iterations
            if iteration >= self.max_iterations:
                return f"(Safety limit: {self.max_iterations} iterations reached.)"

    def _try_parse_text_tool_call(self, text: str) -> dict | None:
        """Try to extract a tool call from text output.

        Handles patterns like:
          browser{"action": "open", "url": "..."}
          browser({"action": "open"})
          ```json\n{"name": "browser", "arguments": {...}}\n```
        """
        import re

        # Pattern 1: toolname{...} or toolname({...})
        tool_names = "|".join(re.escape(n) for n in self._tools.keys())
        m = re.search(rf'({tool_names})\s*\(?\s*(\{{.*?\}})\s*\)?', text, re.DOTALL)
        if m:
            name = m.group(1)
            try:
                args = json.loads(m.group(2))
                if isinstance(args, dict):
                    return {"name": name, "args": args}
            except json.JSONDecodeError:
                pass

        # Pattern 2: JSON block with name + arguments/parameters
        json_blocks = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        for block in json_blocks:
            try:
                obj = json.loads(block)
                if "name" in obj and isinstance(obj.get("arguments") or obj.get("parameters"), dict):
                    name = obj["name"]
                    args = obj.get("arguments") or obj.get("parameters", {})
                    if name in self._tools:
                        return {"name": name, "args": args}
            except json.JSONDecodeError:
                pass

        return None

    def _strip_tool_call_text(self, text: str) -> str:
        """Remove the tool call portion from text, keeping surrounding prose."""
        import re
        tool_names = "|".join(re.escape(n) for n in self._tools.keys())
        # Remove toolname{...} patterns
        text = re.sub(rf'({tool_names})\s*\(?\s*\{{.*?\}}\s*\)?', '', text, flags=re.DOTALL)
        # Remove ```json blocks with tool calls
        text = re.sub(r'```(?:json)?\s*\{[^`]*?"name"[^`]*?\}\s*```', '', text, flags=re.DOTALL)
        return text.strip()

    def probe(self) -> dict[str, Any]:
        """Return the agent's current activity state (for UI probe button).

        If a subtask runner is active, probes the active mini-agent for
        detailed tool-call state instead of just the outer "subtask" status.
        """
        activity = dict(self._last_activity)

        # If we have an active runner with a sub-agent, get its live state
        runner = self._active_runner
        if runner and runner.active_agent:
            sub_activity = runner.active_agent.probe()
            # Merge subtask info with sub-agent's live state
            activity["sub_agent"] = sub_activity
            # Show the sub-agent's actual state (tool_call, calling_llm, etc.)
            if sub_activity.get("state") and sub_activity["state"] != "idle":
                activity["detail_state"] = sub_activity["state"]
                if sub_activity.get("tool"):
                    activity["detail_tool"] = sub_activity["tool"]
                if sub_activity.get("params"):
                    activity["detail_params"] = sub_activity["params"]
                if sub_activity.get("elapsed_seconds"):
                    activity["detail_elapsed"] = sub_activity["elapsed_seconds"]
            # Use sub-agent's token usage (current context window)
            if sub_activity.get("token_usage"):
                activity["token_usage"] = sub_activity["token_usage"]

        if activity.get("time"):
            activity["elapsed_seconds"] = round(time.time() - activity["time"], 1)
        activity["message_count"] = len(self.messages)
        if "token_usage" not in activity:
            activity["token_usage"] = self.get_token_usage()
        return activity

    def flush(self) -> str:
        """Abort the running loop, dump context, return summary.

        Safe to call from another thread while chat() is running.
        """
        self._abort = True
        # The actual dump happens when the loop sees _abort on next iteration.
        # Return immediately — the loop will produce the dump and exit.
        return "Flush requested — agent will stop after current step."

    def reset(self) -> None:
        """Clear conversation history and token counts, clean up browser."""
        try:
            self.cleanup_browser()
        except Exception:
            pass
        self.messages.clear()
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.session_id = None
        self._abort = False
        self._last_activity = {}

    def get_history(self) -> list[dict[str, Any]]:
        """Get conversation history."""
        return list(self.messages)

    def get_token_usage(self) -> dict[str, Any]:
        """Get current token usage stats with context limit info.

        Uses _last_prompt_tokens (actual context window from last LLM call)
        for percentage/warnings, not cumulative totals.
        """
        from tappi.agent.sessions import get_context_limit

        model = get_model()
        context_limit = get_context_limit(model)
        # _last_prompt_tokens = actual context window size (what matters)
        context_used = self._last_prompt_tokens
        usage_pct = (context_used / context_limit * 100) if context_limit else 0

        return {
            "total_tokens": self.total_tokens,  # cumulative (for cost tracking)
            "prompt_tokens": self.prompt_tokens,  # cumulative
            "completion_tokens": self.completion_tokens,  # cumulative
            "context_used": context_used,  # actual context window usage
            "context_limit": context_limit,
            "usage_percent": round(usage_pct, 1),
            "warning": usage_pct >= 75,
            "critical": usage_pct >= 90,
            "model": model,
        }

    def load_session(self, session_id: str) -> bool:
        """Load a saved session's messages into the agent.

        Returns True if loaded successfully.
        """
        from tappi.agent.sessions import load_session as _load

        session = _load(session_id)
        if not session:
            return False

        self.messages = session.get("messages", [])
        self.total_tokens = session.get("total_tokens", 0)
        self.prompt_tokens = session.get("prompt_tokens", 0)
        self.completion_tokens = session.get("completion_tokens", 0)
        self.session_id = session_id
        return True

    def cleanup_browser(self) -> str:
        """Close any browser tabs opened during this agent's session."""
        return self._browser.cleanup()

    def save_session(self, title: str | None = None) -> dict[str, Any]:
        """Save current conversation as a session.

        Returns session metadata.
        """
        from tappi.agent.sessions import save_session, generate_session_id

        if not self.session_id:
            self.session_id = generate_session_id()

        return save_session(
            session_id=self.session_id,
            messages=self.messages,
            model=get_model(),
            provider=get_provider(),
            total_tokens=self.total_tokens,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            title=title,
        )
