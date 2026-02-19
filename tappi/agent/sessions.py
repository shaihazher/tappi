"""Chat session persistence — save, restore, list, and manage sessions.

Sessions are stored as JSON files in ~/.tappi/sessions/.
Each session tracks conversation history, token usage, model info, and metadata.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from tappi.agent.config import CONFIG_DIR

SESSIONS_DIR = CONFIG_DIR / "sessions"

# Known context windows per model family (tokens).
# Used for the 75% warning and "suggest new chat" at max.
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # Anthropic
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "claude-haiku-4": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "o1": 200_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # Google
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.0-pro": 1_000_000,
    "gemini-1.5-pro": 2_000_000,
    "gemini-1.5-flash": 1_000_000,
    # DeepSeek
    "deepseek-chat": 128_000,
    "deepseek-r1": 128_000,
    # Llama
    "llama-3.1-405b": 128_000,
    "llama-3.1-70b": 128_000,
    "llama-3.1-8b": 128_000,
    # Qwen
    "qwen-2.5-72b": 128_000,
    # Mistral
    "mistral-large": 128_000,
}


def get_context_limit(model: str) -> int:
    """Get context window size for a model. Default 128K if unknown."""
    model_lower = model.lower()
    for pattern, limit in MODEL_CONTEXT_LIMITS.items():
        if pattern in model_lower:
            return limit
    return 128_000  # safe default


def _ensure_dir() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)


def generate_session_id() -> str:
    """Generate a unique session ID."""
    return f"session_{int(time.time())}_{uuid.uuid4().hex[:6]}"


def save_session(
    session_id: str,
    messages: list[dict[str, Any]],
    model: str,
    provider: str,
    total_tokens: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    title: str | None = None,
) -> dict[str, Any]:
    """Save a chat session to disk.

    Returns the session metadata dict.
    """
    _ensure_dir()

    # Auto-generate title from first user message if not provided
    if not title:
        for msg in messages:
            if msg.get("role") == "user" and msg.get("content"):
                content = msg["content"]
                title = content[:80] + ("..." if len(content) > 80 else "")
                break
        if not title:
            title = "Untitled"

    session = {
        "id": session_id,
        "title": title,
        "model": model,
        "provider": provider,
        "created_at": None,
        "updated_at": time.time(),
        "message_count": len(messages),
        "total_tokens": total_tokens,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "messages": messages,
    }

    # Preserve created_at from existing session
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            session["created_at"] = existing.get("created_at")
        except (json.JSONDecodeError, OSError):
            pass

    if not session["created_at"]:
        session["created_at"] = time.time()

    path.write_text(json.dumps(session, indent=2) + "\n")
    return {k: v for k, v in session.items() if k != "messages"}


def load_session(session_id: str) -> dict[str, Any] | None:
    """Load a session by ID. Returns full session with messages."""
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    """List sessions sorted by updated_at (newest first).

    Returns metadata only (no messages).
    """
    _ensure_dir()
    sessions = []

    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
            sessions.append({
                "id": data.get("id", path.stem),
                "title": data.get("title", "Untitled"),
                "model": data.get("model", ""),
                "provider": data.get("provider", ""),
                "created_at": data.get("created_at", 0),
                "updated_at": data.get("updated_at", 0),
                "message_count": data.get("message_count", 0),
                "total_tokens": data.get("total_tokens", 0),
            })
        except (json.JSONDecodeError, OSError):
            continue

    sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return sessions[:limit]


def delete_session(session_id: str) -> bool:
    """Delete a session file. Returns True if deleted."""
    path = SESSIONS_DIR / f"{session_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def export_session_markdown(session_id: str) -> str | None:
    """Export a session as a markdown file for the agent's progress save."""
    session = load_session(session_id)
    if not session:
        return None

    lines = [
        f"# Chat Session: {session.get('title', 'Untitled')}",
        f"",
        f"**Model:** {session.get('model', 'unknown')}",
        f"**Tokens used:** {session.get('total_tokens', 0):,}",
        f"",
        "---",
        "",
    ]

    for msg in session.get("messages", []):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append(f"## User\n\n{content}\n")
        elif role == "assistant":
            if content:
                lines.append(f"## Assistant\n\n{content}\n")
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    lines.append(f"*Tool call: {fn.get('name', '?')}*\n")
        elif role == "tool":
            lines.append(f"*Tool result (truncated):* {content[:500]}\n")

    return "\n".join(lines)
