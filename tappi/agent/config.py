"""Agent configuration — provider keys, workspace, model selection.

All config lives in ~/.tappi/config.json alongside profile data.
The agent section is nested under "agent" key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".tappi"
CONFIG_FILE = CONFIG_DIR / "config.json"

# Provider defaults
PROVIDERS = {
    "openrouter": {
        "name": "OpenRouter",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "anthropic/claude-sonnet-4-6",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "anthropic": {
        "name": "Anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
    },
    "claude_max": {
        "name": "Claude Max (OAuth)",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
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
        "default_model": "bedrock/anthropic.claude-sonnet-4-6-v1:0",
        "note": "Uses AWS credentials. Configure Access Key + Secret Key + Region, or use env vars / AWS CLI profile.",
        "fields": [
            {"key": "aws_access_key_id", "label": "AWS Access Key ID", "env": "AWS_ACCESS_KEY_ID", "secret": True},
            {"key": "aws_secret_access_key", "label": "AWS Secret Access Key", "env": "AWS_SECRET_ACCESS_KEY", "secret": True},
            {"key": "aws_region", "label": "AWS Region", "env": "AWS_REGION_NAME", "placeholder": "us-east-1"},
            {"key": "aws_profile", "label": "AWS Profile (optional)", "env": "AWS_PROFILE", "placeholder": "default"},
        ],
    },
    "azure": {
        "name": "Azure OpenAI",
        "env_key": "AZURE_API_KEY",
        "default_model": "azure/gpt-4o",
        "note": "Requires API key, endpoint URL, and API version from your Azure OpenAI resource.",
        "fields": [
            {"key": "api_key", "label": "API Key", "secret": True},
            {"key": "base_url", "label": "Endpoint URL", "placeholder": "https://your-resource.openai.azure.com"},
            {"key": "api_version", "label": "API Version", "placeholder": "2024-02-01"},
        ],
    },
    "vertex": {
        "name": "Google Vertex AI",
        "env_key": "GOOGLE_APPLICATION_CREDENTIALS",
        "default_model": "vertex_ai/gemini-2.0-flash",
        "note": "Uses Google Cloud auth. Set credentials file path + project ID, or use gcloud CLI auth.",
        "fields": [
            {"key": "credentials_path", "label": "Service Account JSON Path", "env": "GOOGLE_APPLICATION_CREDENTIALS", "placeholder": "/path/to/service-account.json"},
            {"key": "project", "label": "Project ID", "env": "VERTEXAI_PROJECT", "placeholder": "my-gcp-project"},
            {"key": "location", "label": "Location", "env": "VERTEXAI_LOCATION", "placeholder": "us-central1"},
        ],
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
    return agent_cfg.get("model", "claude-sonnet-4-6")


def get_workspace() -> Path:
    """Get the workspace directory (sandboxed file operations)."""
    agent_cfg = get_agent_config()
    ws = agent_cfg.get("workspace")
    if ws:
        return Path(ws).expanduser().resolve()
    # Default: ~/tappi-workspace
    return Path.home() / "tappi-workspace"


def get_provider() -> str:
    """Get the configured provider name."""
    agent_cfg = get_agent_config()
    return agent_cfg.get("provider", "openrouter")


def is_configured() -> bool:
    """Check if the agent has been set up."""
    agent_cfg = get_agent_config()
    return bool(agent_cfg.get("provider") and agent_cfg.get("workspace"))


def get_provider_credentials_status() -> dict[str, Any]:
    """Get credential status for all providers (masked, never raw keys).

    Returns dict like:
    {
        "openrouter": {"configured": True, "masked": "sk-or...ce3e", "source": "config"},
        "bedrock": {"configured": True, "fields": {"aws_access_key_id": {"configured": True, "masked": "AKIA...XYZ"}, ...}},
    }
    """
    agent_cfg = get_agent_config()
    providers_cfg = agent_cfg.get("providers", {})
    result: dict[str, Any] = {}

    for pkey, pinfo in PROVIDERS.items():
        pcfg = providers_cfg.get(pkey, {})
        fields = pinfo.get("fields")

        if fields:
            # Multi-field provider (Bedrock, Azure, Vertex)
            field_status = {}
            any_configured = False
            for f in fields:
                fkey = f["key"]
                val = pcfg.get(fkey) or os.environ.get(f.get("env", ""), "")
                if val:
                    any_configured = True
                    source = "config" if pcfg.get(fkey) else "env"
                    if f.get("secret"):
                        masked = val[:4] + "..." + val[-4:] if len(val) > 10 else "***"
                    else:
                        masked = val
                    field_status[fkey] = {"configured": True, "masked": masked, "source": source}
                else:
                    field_status[fkey] = {"configured": False}
            result[pkey] = {"configured": any_configured, "fields": field_status}
        else:
            # Single API key provider
            key = pcfg.get("api_key", "")
            env_key = os.environ.get(pinfo.get("env_key", ""), "")
            val = key or env_key
            if val:
                source = "config" if key else "env"
                masked = val[:8] + "..." + val[-4:] if len(val) > 14 else val[:4] + "..."
                result[pkey] = {"configured": True, "masked": masked, "source": source}
            else:
                result[pkey] = {"configured": False}

    return result
