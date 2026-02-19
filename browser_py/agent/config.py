"""Agent configuration — provider keys, workspace, model selection.

All config lives in ~/.browser-py/config.json alongside profile data.
The agent section is nested under "agent" key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".browser-py"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Provider defaults
PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4-20250514",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "anthropic": {
        "name": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
    },
    "claude_max": {
        "name": "Claude Max (OAuth)",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-20250514",
        "note": "Uses Claude Code OAuth token (sk-ant-oat01-...) from your Max/Pro subscription",
        "is_oauth": True,
    },
    "openai": {
        "name": "OpenAI",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o",
    },
    "bedrock": {
        "name": "AWS Bedrock",
        "env_key": "AWS_ACCESS_KEY_ID",
        "default_model": "bedrock/anthropic.claude-sonnet-4-20250514-v1:0",
        "note": "Requires AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION_NAME",
    },
    "azure": {
        "name": "Azure OpenAI",
        "env_key": "AZURE_API_KEY",
        "default_model": "azure/gpt-4o",
        "note": "Requires AZURE_API_KEY, AZURE_API_BASE, AZURE_API_VERSION",
    },
    "vertex": {
        "name": "Google Vertex AI",
        "env_key": "GOOGLE_APPLICATION_CREDENTIALS",
        "default_model": "vertex_ai/gemini-2.0-flash",
        "note": "Requires GOOGLE_APPLICATION_CREDENTIALS and VERTEXAI_PROJECT",
    },
}


def detect_claude_oauth_token() -> str | None:
    """Try to auto-detect Claude Code OAuth token from known locations.

    Checks:
    1. ANTHROPIC_API_KEY env var (if it's an OAuth token)
    2. Claude Code's stored credentials in ~/.claude.json vicinity
    3. Common credential files
    """
    import os

    # Check env var first
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key.startswith("sk-ant-oat"):
        return env_key

    # Check Claude Code config directory
    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        try:
            data = json.loads(claude_json.read_text())
            # Claude Code stores the OAuth account info here
            # The actual token may be in the system keychain
            if data.get("oauthAccount"):
                # Try to find the token via Claude's credential storage
                # Claude Code uses electron-safe-storage / keychain
                pass
        except (json.JSONDecodeError, OSError):
            pass

    return None


def load_config() -> dict[str, Any]:
    """Load full config (profiles + agent settings)."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"default": None, "profiles": {}}


def save_config(config: dict[str, Any]) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def get_agent_config() -> dict[str, Any]:
    """Get just the agent section of config."""
    config = load_config()
    return config.get("agent", {})


def set_agent_config(agent_cfg: dict[str, Any]) -> None:
    """Update the agent section of config (merges)."""
    config = load_config()
    existing = config.get("agent", {})
    existing.update(agent_cfg)
    config["agent"] = existing
    save_config(config)


def get_provider_key(provider: str) -> str | None:
    """Get API key for a provider from config, then env."""
    agent_cfg = get_agent_config()
    providers = agent_cfg.get("providers", {})

    # Check config first
    key = providers.get(provider, {}).get("api_key")
    if key:
        return key

    # Fall back to environment variable
    info = PROVIDERS.get(provider, {})
    env_key = info.get("env_key")
    if env_key:
        return os.environ.get(env_key)

    return None


def get_model() -> str:
    """Get the configured model name."""
    agent_cfg = get_agent_config()
    return agent_cfg.get("model", "anthropic/claude-sonnet-4-20250514")


def get_workspace() -> Path:
    """Get the workspace directory (sandboxed file operations)."""
    agent_cfg = get_agent_config()
    ws = agent_cfg.get("workspace")
    if ws:
        return Path(ws).expanduser().resolve()
    # Default: ~/browser-py-workspace
    return Path.home() / "browser-py-workspace"


def get_provider() -> str:
    """Get the configured provider name."""
    agent_cfg = get_agent_config()
    return agent_cfg.get("provider", "openrouter")


def is_configured() -> bool:
    """Check if the agent has been set up."""
    agent_cfg = get_agent_config()
    return bool(agent_cfg.get("provider") and agent_cfg.get("workspace"))
