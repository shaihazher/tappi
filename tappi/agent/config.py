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
        "default_model": "bedrock/us.anthropic.claude-sonnet-4-6-v1:0",
        "note": "Uses AWS credentials. Tappi reads from config first, then falls back to environment variables. You can also use an AWS CLI named profile.",
        "fields": [
            {"key": "aws_access_key_id", "label": "AWS Access Key ID", "env": "AWS_ACCESS_KEY_ID", "secret": True},
            {"key": "aws_secret_access_key", "label": "AWS Secret Access Key", "env": "AWS_SECRET_ACCESS_KEY", "secret": True},
            {"key": "aws_region", "label": "AWS Region", "env": "AWS_DEFAULT_REGION", "placeholder": "us-east-1", "alt_env": ["AWS_REGION_NAME", "AWS_REGION"]},
            {"key": "aws_profile", "label": "AWS Profile (optional)", "env": "AWS_PROFILE", "placeholder": "default"},
        ],
    },
    "azure": {
        "name": "Azure OpenAI",
        "env_key": "AZURE_API_KEY",
        "default_model": "azure/gpt-4o",
        "note": "Requires API key, endpoint URL, and API version from your Azure OpenAI resource.",
        "fields": [
            {"key": "api_key", "label": "API Key", "env": "AZURE_API_KEY", "secret": True},
            {"key": "base_url", "label": "Endpoint URL", "env": "AZURE_API_BASE", "placeholder": "https://your-resource.openai.azure.com"},
            {"key": "api_version", "label": "API Version", "env": "AZURE_API_VERSION", "placeholder": "2024-02-01"},
        ],
    },
    "vertex": {
        "name": "Google Vertex AI",
        "env_key": "GOOGLE_APPLICATION_CREDENTIALS",
        "default_model": "vertex_ai/gemini-2.0-flash",
        "note": "Uses Google Cloud auth. Set credentials file path + project ID, or use gcloud CLI / ADC auth.",
        "fields": [
            {"key": "credentials_path", "label": "Service Account JSON Path", "env": "GOOGLE_APPLICATION_CREDENTIALS", "placeholder": "/path/to/service-account.json"},
            {"key": "project", "label": "Project ID", "env": "VERTEXAI_PROJECT", "placeholder": "my-gcp-project"},
            {"key": "location", "label": "Location", "env": "VERTEXAI_LOCATION", "placeholder": "us-central1"},
        ],
    },
}


def resolve_provider_credentials(provider: str) -> dict[str, Any]:
    """Live-resolve credentials for a provider, including file-based sources.

    Unlike get_provider_credentials_status() which only checks config + env vars,
    this also checks boto3 credential chain, gcloud ADC, Azure CLI, etc.

    Returns:
        {
            "resolved": True/False,
            "source": "config" | "env" | "aws_profile" | "aws_sso" | "aws_credentials_file" | "gcloud_adc" | ...,
            "details": {...},  # provider-specific
            "error": "..." | None,
        }
    """
    agent_cfg = get_agent_config()
    pcfg = agent_cfg.get("providers", {}).get(provider, {})

    if provider == "bedrock":
        return _resolve_aws_credentials(pcfg)
    elif provider == "vertex":
        return _resolve_vertex_credentials(pcfg)
    elif provider == "azure":
        return _resolve_azure_credentials(pcfg)
    else:
        # Simple API key providers — just check config + env
        key = get_provider_key(provider)
        if key:
            source = "config" if pcfg.get("api_key") else "env"
            return {"resolved": True, "source": source, "error": None}
        return {"resolved": False, "source": None, "error": "No API key found"}


def _resolve_aws_credentials(pcfg: dict) -> dict[str, Any]:
    """Resolve AWS credentials through the full boto3 chain."""
    # Check tappi config first
    if pcfg.get("aws_access_key_id") and pcfg.get("aws_secret_access_key"):
        return {
            "resolved": True,
            "source": "config",
            "details": {"region": pcfg.get("aws_region", "not set")},
            "error": None,
        }

    # Check env vars
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return {
            "resolved": True,
            "source": "env",
            "details": {"region": os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "not set"))},
            "error": None,
        }

    # Try boto3 credential chain (reads ~/.aws/credentials, SSO cache, IMDS, etc.)
    try:
        import boto3
        import botocore.exceptions
        session = boto3.Session(profile_name=pcfg.get("aws_profile") or None)
        creds = session.get_credentials()
        if creds:
            frozen = creds.get_frozen_credentials()
            if frozen.access_key:
                # Determine the source
                method = getattr(creds, "method", "unknown")
                # boto3 method names: explicit, env, shared-credentials-file,
                # sso, assume-role, iam-role, etc.
                source_map = {
                    "explicit": "config",
                    "env": "env",
                    "shared-credentials-file": "~/.aws/credentials",
                    "custom-process": "credential_process",
                    "sso": "AWS SSO",
                    "assume-role": "assume-role",
                    "iam-role": "instance-profile",
                    "container-role": "container-role",
                }
                source = source_map.get(method, method)
                region = session.region_name or "not set"
                return {
                    "resolved": True,
                    "source": source,
                    "details": {
                        "region": region,
                        "access_key_prefix": frozen.access_key[:8] + "...",
                        "method": method,
                    },
                    "error": None,
                }
    except ImportError:
        return {
            "resolved": False,
            "source": None,
            "error": "boto3 not installed — install with: pip install boto3",
        }
    except Exception as e:
        return {
            "resolved": False,
            "source": None,
            "error": f"AWS credential resolution failed: {e}",
        }

    return {"resolved": False, "source": None, "error": "No AWS credentials found"}


def _resolve_vertex_credentials(pcfg: dict) -> dict[str, Any]:
    """Resolve Google Cloud credentials."""
    # Config path
    creds_path = pcfg.get("credentials_path") or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if creds_path and Path(creds_path).exists():
        return {
            "resolved": True,
            "source": "service_account" if pcfg.get("credentials_path") else "env (GOOGLE_APPLICATION_CREDENTIALS)",
            "details": {"path": creds_path, "project": pcfg.get("project", "not set")},
            "error": None,
        }

    # Check for Application Default Credentials (gcloud auth application-default login)
    adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if adc_path.exists():
        return {
            "resolved": True,
            "source": "gcloud ADC",
            "details": {"path": str(adc_path), "project": pcfg.get("project", "not set")},
            "error": None,
        }

    return {"resolved": False, "source": None, "error": "No Google Cloud credentials found"}


def _resolve_azure_credentials(pcfg: dict) -> dict[str, Any]:
    """Resolve Azure OpenAI credentials."""
    key = pcfg.get("api_key") or os.environ.get("AZURE_API_KEY", "")
    if key:
        source = "config" if pcfg.get("api_key") else "env"
        return {"resolved": True, "source": source, "error": None}

    # Check for Azure CLI auth
    azure_profile = Path.home() / ".azure" / "azureProfile.json"
    if azure_profile.exists():
        return {
            "resolved": True,
            "source": "Azure CLI",
            "details": {},
            "error": None,
        }

    return {"resolved": False, "source": None, "error": "No Azure credentials found"}


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
                # Check config first
                config_val = pcfg.get(fkey, "")
                # Check primary env var, then alt_env vars
                env_val = ""
                env_source_name = f.get("env", "")
                if env_source_name:
                    env_val = os.environ.get(env_source_name, "")
                if not env_val:
                    for alt in f.get("alt_env", []):
                        env_val = os.environ.get(alt, "")
                        if env_val:
                            env_source_name = alt
                            break
                val = config_val or env_val
                if val:
                    any_configured = True
                    source = "config" if config_val else f"env ({env_source_name})"
                    if f.get("secret"):
                        masked = val[:4] + "..." + val[-4:] if len(val) > 10 else "***"
                    else:
                        masked = val
                    field_status[fkey] = {"configured": True, "masked": masked, "source": source}
                else:
                    field_status[fkey] = {"configured": False, "env_var": env_source_name}
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
