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

from browser_py.agent.config import (
    get_agent_config,
    get_model,
    get_provider,
    get_provider_key,
    get_workspace,
    PROVIDERS,
)
from browser_py.agent.tools.browser import BrowserTool, TOOL_SCHEMA as BROWSER_SCHEMA
from browser_py.agent.tools.files import FilesTool, TOOL_SCHEMA as FILES_SCHEMA
from browser_py.agent.tools.pdf import PDFTool, TOOL_SCHEMA as PDF_SCHEMA
from browser_py.agent.tools.spreadsheet import SpreadsheetTool, TOOL_SCHEMA as SPREADSHEET_SCHEMA
from browser_py.agent.tools.shell import ShellTool, TOOL_SCHEMA as SHELL_SCHEMA
from browser_py.agent.tools.cron import CronTool, TOOL_SCHEMA as CRON_SCHEMA


SYSTEM_PROMPT = """\
You are a capable AI assistant with browser control, file management, and \
automation skills. You operate within a designated workspace directory and \
can control a web browser to accomplish tasks.

## Your Tools

- **browser**: Navigate, click, type, read pages, take screenshots. Use your \
real browser with saved logins.
- **files**: Read, write, list, move, copy, delete files in the workspace.
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

    Args:
        workspace: Override workspace directory.
        browser_profile: Default browser profile to use.
        on_tool_call: Callback when a tool is called (name, params, result).
        on_message: Callback when the LLM produces text.
        on_token_update: Callback when token usage updates (usage_dict).
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
        max_iterations: int = 50,
    ) -> None:
        self.workspace = workspace or get_workspace()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.max_iterations = max_iterations
        self.on_tool_call = on_tool_call
        self.on_message = on_message
        self.on_token_update = on_token_update

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
        self._system_prompt = SYSTEM_PROMPT.format(workspace=self.workspace)

        # Token tracking
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

        # Session management
        self.session_id: str | None = None

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

    def _call_llm(self) -> dict:
        """Make a single LLM call and return the response."""
        import litellm

        self._setup_litellm()
        model = get_model()
        provider = get_provider()

        messages = [{"role": "system", "content": self._system_prompt}] + self.messages

        kwargs = dict(
            model=model,
            messages=messages,
            tools=self._tool_schemas,
            tool_choice="auto",
            max_tokens=4096,
        )

        # OpenRouter: use openai-compatible base_url so ALL model IDs work,
        # including meta-routers like openrouter/free, openrouter/auto.
        # LiteLLM's native openrouter/ prefix chokes on those.
        if provider == "openrouter":
            key = get_provider_key(provider)
            kwargs["api_key"] = key
            kwargs["base_url"] = "https://openrouter.ai/api/v1"
            # Tell LiteLLM to treat this as an OpenAI-compatible call.
            # The model ID is sent as-is to OpenRouter's API.
            kwargs["model"] = f"openai/{model}"

        response = litellm.completion(**kwargs)

        # Track token usage
        usage = getattr(response, "usage", None)
        if usage:
            self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
            self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            self.total_tokens = self.prompt_tokens + self.completion_tokens

            if self.on_token_update:
                self.on_token_update(self.get_token_usage())

        return response

    def _execute_tool(self, name: str, arguments: dict) -> str:
        """Execute a tool and return the result string."""
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: {name}"

        result = tool.execute(**arguments)

        if self.on_tool_call:
            self.on_tool_call(name, arguments, result)

        return result

    def chat(self, user_message: str) -> str:
        """Send a message and get a response. Handles multi-step tool loops.

        Args:
            user_message: The user's message/task.

        Returns:
            The agent's final text response.
        """
        self.messages.append({"role": "user", "content": user_message})

        for iteration in range(self.max_iterations):
            response = self._call_llm()
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
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                result = self._execute_tool(tc.function.name, args)

                # Add tool result to history
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

            # Continue loop — LLM will see tool results and decide next step

        return "(Max iterations reached. The task may be incomplete.)"

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

    def reset(self) -> None:
        """Clear conversation history and token counts."""
        self.messages.clear()
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.session_id = None

    def get_history(self) -> list[dict[str, Any]]:
        """Get conversation history."""
        return list(self.messages)

    def get_token_usage(self) -> dict[str, Any]:
        """Get current token usage stats with context limit info."""
        from browser_py.agent.sessions import get_context_limit

        model = get_model()
        context_limit = get_context_limit(model)
        usage_pct = (self.total_tokens / context_limit * 100) if context_limit else 0

        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
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
        from browser_py.agent.sessions import load_session as _load

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
        from browser_py.agent.sessions import save_session, generate_session_id

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
