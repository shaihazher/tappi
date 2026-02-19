"""Interactive setup wizard for bpy agent.

Handles: provider selection, API key, model, workspace dir, browser profile.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from browser_py.agent.config import (
    load_config,
    save_config,
    PROVIDERS,
    is_configured,
)
from browser_py.profiles import list_profiles, create_profile, get_profile


def _bold(s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[1m{s}\033[0m"


def _dim(s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[2m{s}\033[0m"


def _cyan(s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[36m{s}\033[0m"


def _green(s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[33m{s}\033[0m"


def _input(prompt: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    if default:
        display = f"{prompt} [{default}]: "
    else:
        display = f"{prompt}: "
    try:
        val = input(display).strip()
        return val or default
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


def run_setup() -> None:
    """Run the interactive setup wizard."""
    print()
    print(_bold("🔧 browser-py agent setup"))
    print(_dim("Configure your LLM provider, workspace, and browser.\n"))

    config = load_config()
    agent_cfg = config.get("agent", {})
    providers_cfg = agent_cfg.get("providers", {})

    # ── Step 1: Provider ──
    print(_cyan("Step 1: LLM Provider"))
    print()

    tier1 = ["openrouter", "anthropic", "claude_max", "openai"]
    tier2 = ["bedrock", "azure", "vertex"]

    for i, key in enumerate(tier1, 1):
        info = PROVIDERS[key]
        current = " ✓" if key == agent_cfg.get("provider") else ""
        note = ""
        if key == "claude_max":
            note = _dim(" — use your Max/Pro subscription, no API costs")
        print(f"  {i}. {info['name']}{note}{_green(current)}")

    print(_dim("  --- Cloud providers (require SDK auth) ---"))
    for i, key in enumerate(tier2, len(tier1) + 1):
        info = PROVIDERS[key]
        note = f" — {info.get('note', '')}" if info.get("note") else ""
        current = " ✓" if key == agent_cfg.get("provider") else ""
        print(f"  {i}. {info['name']}{_dim(note)}{_green(current)}")

    print()
    all_providers = tier1 + tier2
    current_idx = all_providers.index(agent_cfg["provider"]) + 1 if agent_cfg.get("provider") in all_providers else 1
    choice = _input("Choose provider", str(current_idx))

    try:
        provider_key = all_providers[int(choice) - 1]
    except (ValueError, IndexError):
        print(f"Invalid choice. Defaulting to openrouter.")
        provider_key = "openrouter"

    agent_cfg["provider"] = provider_key
    print(f"  → {_green(PROVIDERS[provider_key]['name'])}")
    print()

    # ── Step 2: API Key ──
    print(_cyan("Step 2: API Key"))
    info = PROVIDERS[provider_key]

    existing_key = providers_cfg.get(provider_key, {}).get("api_key", "")
    env_key = os.environ.get(info.get("env_key", ""), "")
    has_key = existing_key or env_key

    if has_key:
        masked = (existing_key or env_key)[:8] + "..."
        print(f"  Current key: {_dim(masked)}")
        change = _input("  Change it? (y/N)", "n")
        if change.lower() != "y":
            if existing_key:
                providers_cfg.setdefault(provider_key, {})["api_key"] = existing_key
        else:
            has_key = False

    if not has_key:
        if provider_key == "claude_max":
            # Try auto-detect first
            from browser_py.agent.config import detect_claude_oauth_token
            detected = detect_claude_oauth_token()
            if detected:
                print(f"  {_green('✓')} Auto-detected Claude OAuth token")
                providers_cfg.setdefault(provider_key, {})["api_key"] = detected
            else:
                print(f"  {_yellow('Claude Max OAuth token needed.')}")
                print(f"  {_dim('Get it from Claude Code: run claude and check ~/.claude or your credentials.')}")
                print(f"  {_dim('Token format: sk-ant-oat01-...')}")
                print()
                key = _input(f"  OAuth token (sk-ant-oat01-...)")
                if not key:
                    print(_yellow("  Warning: no token provided. Set ANTHROPIC_API_KEY env var later."))
                providers_cfg.setdefault(provider_key, {})["api_key"] = key
        elif provider_key in ("bedrock", "vertex"):
            print(f"  {_yellow('Note:')} {info.get('note', '')}")
            print(f"  Set these env vars before running bpy agent/serve.")
            providers_cfg.setdefault(provider_key, {})["api_key"] = "env"
        else:
            key = _input(f"  {info['name']} API key")
            if not key:
                print(_yellow("  Warning: no key provided. Set it later or via env var."))
            providers_cfg.setdefault(provider_key, {})["api_key"] = key

        # Azure needs extra config
        if provider_key == "azure":
            base = _input("  Azure endpoint URL")
            version = _input("  API version", "2024-02-01")
            providers_cfg["azure"]["base_url"] = base
            providers_cfg["azure"]["api_version"] = version

    agent_cfg["providers"] = providers_cfg
    print()

    # ── Step 3: Model ──
    print(_cyan("Step 3: Model"))
    default_model = info["default_model"]
    current_model = agent_cfg.get("model", default_model)
    model = _input(f"  Model name", current_model)
    agent_cfg["model"] = model
    print(f"  → {_green(model)}")
    print()

    # ── Step 4: Workspace ──
    print(_cyan("Step 4: Workspace Directory"))
    print(_dim("  All file operations are sandboxed to this directory."))
    default_ws = str(agent_cfg.get("workspace", Path.home() / "browser-py-workspace"))
    workspace = _input("  Workspace path", default_ws)
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    agent_cfg["workspace"] = str(workspace_path)
    print(f"  → {_green(str(workspace_path))}")
    print()

    # ── Step 5: Browser Profile ──
    print(_cyan("Step 5: Browser Profile"))
    profiles = list_profiles()

    if profiles:
        print("  Existing profiles:")
        for p in profiles:
            default = " (default)" if p["is_default"] else ""
            print(f"    {p['name']} — port {p['port']}{default}")
        print()

    current_profile = agent_cfg.get("browser_profile", "")
    profile_name = _input(
        "  Profile name (enter for default, or a new name to create)",
        current_profile or (profiles[0]["name"] if profiles else "default"),
    )

    # Create profile if it doesn't exist
    try:
        get_profile(profile_name)
    except ValueError:
        print(f"  Creating profile '{profile_name}'...")
        create_profile(profile_name)
        print(f"  {_green('✓')} Created")

    agent_cfg["browser_profile"] = profile_name

    # Set browser download dir to workspace
    print(f"  {_dim('Browser downloads will go to the workspace directory.')}")
    print()

    # ── Step 6: Shell access ──
    print(_cyan("Step 6: Shell Access"))
    shell_enabled = agent_cfg.get("shell_enabled", True)
    choice = _input("  Allow shell commands? (Y/n)", "y" if shell_enabled else "n")
    agent_cfg["shell_enabled"] = choice.lower() != "n"
    print()

    # ── Save ──
    config["agent"] = agent_cfg
    save_config(config)

    print(_green("✓ Setup complete!"))
    print()
    print(f"  Config saved to: {_dim(str(Path.home() / '.browser-py' / 'config.json'))}")
    print()
    print(_bold("Next steps:"))
    print(f"  {_dim('Chat:')}     bpy agent \"Go to github.com and find trending repos\"")
    print(f"  {_dim('Web UI:')}   bpy serve")
    print(f"  {_dim('Browser:')} bpy launch {profile_name}")
    print()
