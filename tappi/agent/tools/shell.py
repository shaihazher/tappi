"""Shell tool — run commands (sandboxed to workspace directory).

Optional tool — can be disabled in config. Runs commands with cwd set
to the workspace directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from tappi.agent.config import get_workspace

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": (
            "Run a shell command. The working directory is always the workspace. "
            "Use for tasks like installing packages, running scripts, converting "
            "files, or checking system info. Output is capped at 10KB."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 30)",
                },
            },
            "required": ["command"],
        },
    },
}


class ShellTool:
    """Sandboxed shell execution."""

    def __init__(self, workspace: Path | None = None, enabled: bool = True) -> None:
        self._workspace = workspace
        self.enabled = enabled

    @property
    def workspace(self) -> Path:
        if self._workspace is None:
            self._workspace = get_workspace()
        self._workspace.mkdir(parents=True, exist_ok=True)
        return self._workspace

    def execute(self, **params: Any) -> str:
        if not self.enabled:
            return "Shell access is disabled. Enable it in settings."

        command = params.get("command", "")
        if not command:
            return "Error: 'command' required"

        timeout = int(params.get("timeout", 30))

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self.workspace),
            )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += "\n"
                output += f"(stderr) {result.stderr}"

            if not output:
                output = "(no output)"

            if result.returncode != 0:
                output += f"\n(exit code: {result.returncode})"

            # Cap at 10KB
            if len(output) > 10_000:
                output = output[:10_000] + "\n... (truncated)"

            return output

        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout}s"
        except Exception as e:
            return f"Error: {e}"
