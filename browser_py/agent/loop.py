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
        max_iterations: Safety limit on tool call loops (default: 50).
    """

    def __init__(
        self,
        workspace: Path | None = None,
        browser_profile: str | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_message: Callable[[str], None] | None = None,
        on_job_trigger: Callable[[dict], None] | None = None,
        max_iterations: int = 50,
    ) -> None:
        self.workspace = workspace or get_workspace()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.max_iterations = max_iterations
        self.on_tool_call = on_tool_call
        self.on_message = on_message

        # Initialize tools
        self._browser = BrowserTool(default_profile=browser_profile)
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

        messages = [{"role": "system", "content": self._system_prompt}] + self.messages

        response = litellm.completion(
            model=model,
            messages=messages,
            tools=self._tool_schemas,
            tool_choice="auto",
            max_tokens=4096,
        )

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

            # If no tool calls, we're done
            if not msg.tool_calls:
                text = msg.content or ""
                if self.on_message:
                    self.on_message(text)
                return text

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

    def reset(self) -> None:
        """Clear conversation history."""
        self.messages.clear()

    def get_history(self) -> list[dict[str, Any]]:
        """Get conversation history."""
        return list(self.messages)
