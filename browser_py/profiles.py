"""Profile management for browser-py.

Stores profiles in ~/.browser-py/profiles/<name>/ with a central
config at ~/.browser-py/config.json for defaults and port assignments.

Usage:
    browser-py launch              # Launch default profile
    browser-py launch work         # Launch profile "work"
    browser-py launch new          # Create a new profile interactively
    browser-py launch new myname   # Create profile "myname"
    browser-py launch list         # List all profiles
    browser-py launch --default work  # Set "work" as the default
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

CONFIG_DIR = Path.home() / ".browser-py"
CONFIG_FILE = CONFIG_DIR / "config.json"
PROFILES_DIR = CONFIG_DIR / "profiles"
BASE_PORT = 9222


def _load_config() -> dict[str, Any]:
    """Load the config file, or return defaults."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"default": None, "profiles": {}}


def _save_config(config: dict[str, Any]) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def _sanitize_name(name: str) -> str:
    """Clean a profile name to be filesystem-safe."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", name.strip().lower())


def _next_port(config: dict[str, Any]) -> int:
    """Find the next available port starting from BASE_PORT."""
    used = {p.get("port", 0) for p in config.get("profiles", {}).values()}
    port = BASE_PORT
    while port in used:
        port += 1
    return port


def _migrate_legacy() -> None:
    """Migrate old single-profile setup (~/.browser-py/profile/) to named profiles."""
    legacy_dir = CONFIG_DIR / "profile"
    if legacy_dir.exists() and legacy_dir.is_dir():
        config = _load_config()
        if "default" not in config.get("profiles", {}):
            # Move legacy profile to "default" named profile
            new_dir = PROFILES_DIR / "default"
            if not new_dir.exists():
                PROFILES_DIR.mkdir(parents=True, exist_ok=True)
                legacy_dir.rename(new_dir)
                config.setdefault("profiles", {})["default"] = {"port": BASE_PORT}
                if not config.get("default"):
                    config["default"] = "default"
                _save_config(config)


# ── Public API ──


def list_profiles() -> list[dict[str, Any]]:
    """List all profiles with their name, port, path, and default status.

    Returns:
        List of dicts: [{"name": str, "port": int, "path": str, "is_default": bool}]
    """
    _migrate_legacy()
    config = _load_config()
    default_name = config.get("default")
    profiles = config.get("profiles", {})

    # Also scan the profiles directory for any not in config
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            if d.is_dir() and d.name not in profiles:
                profiles[d.name] = {"port": _next_port(config)}
                config["profiles"] = profiles
                _save_config(config)

    result = []
    for name, info in sorted(profiles.items()):
        result.append({
            "name": name,
            "port": info.get("port", BASE_PORT),
            "path": str(PROFILES_DIR / name),
            "is_default": name == default_name,
        })

    return result


def get_profile(name: str | None = None) -> dict[str, Any]:
    """Get a profile by name, or the default profile.

    Returns:
        Dict with name, port, path, is_default, is_new.

    Raises:
        ValueError if the named profile doesn't exist.
    """
    _migrate_legacy()
    config = _load_config()
    profiles = config.get("profiles", {})

    if name is None:
        name = config.get("default")

    # If still no name, use "default"
    if not name:
        name = "default"

    if name in profiles:
        info = profiles[name]
        return {
            "name": name,
            "port": info.get("port", BASE_PORT),
            "path": str(PROFILES_DIR / name),
            "is_default": config.get("default") == name,
            "is_new": not (PROFILES_DIR / name / "Default").exists(),
        }

    raise ValueError(
        f"Profile '{name}' not found.\n"
        f"Available profiles: {', '.join(profiles.keys()) or '(none)'}\n"
        f"Create one with: browser-py launch new {name}"
    )


def create_profile(name: str, port: int | None = None) -> dict[str, Any]:
    """Create a new profile.

    Args:
        name: Profile name (alphanumeric, hyphens, underscores).
        port: CDP port (auto-assigned if not given).

    Returns:
        Dict with name, port, path, is_default.
    """
    _migrate_legacy()
    name = _sanitize_name(name)
    if not name:
        raise ValueError("Profile name cannot be empty.")

    config = _load_config()
    profiles = config.setdefault("profiles", {})

    if name in profiles:
        raise ValueError(f"Profile '{name}' already exists. Use: browser-py launch {name}")

    assigned_port = port or _next_port(config)
    profiles[name] = {"port": assigned_port}

    # Set as default if it's the first profile
    if not config.get("default"):
        config["default"] = name

    _save_config(config)

    # Create the directory
    profile_dir = PROFILES_DIR / name
    profile_dir.mkdir(parents=True, exist_ok=True)

    return {
        "name": name,
        "port": assigned_port,
        "path": str(profile_dir),
        "is_default": config["default"] == name,
    }


def set_default(name: str) -> None:
    """Set a profile as the default.

    Args:
        name: Profile name to set as default.

    Raises:
        ValueError if the profile doesn't exist.
    """
    _migrate_legacy()
    config = _load_config()
    profiles = config.get("profiles", {})

    if name not in profiles:
        raise ValueError(
            f"Profile '{name}' not found.\n"
            f"Available: {', '.join(profiles.keys()) or '(none)'}"
        )

    config["default"] = name
    _save_config(config)


def delete_profile(name: str) -> str:
    """Delete a profile (moves to trash if available, else deletes).

    Args:
        name: Profile name to delete.

    Returns:
        Confirmation message.
    """
    _migrate_legacy()
    config = _load_config()
    profiles = config.get("profiles", {})

    if name not in profiles:
        raise ValueError(f"Profile '{name}' not found.")

    # Remove from config
    del profiles[name]
    if config.get("default") == name:
        config["default"] = next(iter(profiles), None)
    _save_config(config)

    # Remove directory
    profile_dir = PROFILES_DIR / name
    if profile_dir.exists():
        import shutil
        shutil.rmtree(profile_dir)

    return f"Deleted profile '{name}'"
