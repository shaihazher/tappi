"""FastAPI server — chat API, SSE streaming, cron management, and web UI.

Start with: bpy serve [--port 8321]
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from browser_py.agent.config import get_agent_config, get_workspace, is_configured
from browser_py.agent.loop import Agent

app = FastAPI(title="browser-py", docs_url=None, redoc_url=None)

# Global agent instance (per server process)
_agent: Agent | None = None
_agent_lock = threading.Lock()
_ws_clients: list[WebSocket] = []
_chat_task: asyncio.Task | None = None  # tracks the running chat task
_research_abort = threading.Event()  # shared abort signal for research


def _on_token_update(usage: dict) -> None:
    """Broadcast token usage updates to WebSocket clients."""
    msg = json.dumps({"type": "token_update", **usage})
    _broadcast(msg)


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        if not is_configured():
            raise RuntimeError("Agent not configured. Complete setup first.")
        cfg = get_agent_config()
        _agent = Agent(
            browser_profile=cfg.get("browser_profile"),
            on_tool_call=_on_tool_call,
            on_message=_on_message,
            on_job_trigger=_on_job_change,
            on_token_update=_on_token_update,
        )
        if not cfg.get("shell_enabled", True):
            _agent._shell.enabled = False
    return _agent


def _on_tool_call(name: str, params: dict, result: str) -> None:
    """Broadcast tool calls to connected WebSocket clients."""
    msg = json.dumps({
        "type": "tool_call",
        "tool": name,
        "params": params,
        "result": result[:2000],  # Cap for WS
    })
    _broadcast(msg)


def _on_message(text: str) -> None:
    """Broadcast agent messages to WebSocket clients."""
    msg = json.dumps({"type": "message", "content": text})
    _broadcast(msg)


def _broadcast(msg: str) -> None:
    """Send to all connected WebSocket clients."""
    dead = []
    for ws in _ws_clients:
        try:
            asyncio.run_coroutine_threadsafe(ws.send_text(msg), _loop)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)


_loop: asyncio.AbstractEventLoop = None  # type: ignore


# ── REST API ──


@app.get("/")
async def index() -> HTMLResponse:
    """Serve the chat UI."""
    ui_path = Path(__file__).parent / "static" / "index.html"
    if ui_path.exists():
        return HTMLResponse(ui_path.read_text())
    return HTMLResponse(_FALLBACK_HTML)


@app.post("/api/chat")
async def chat(body: dict) -> JSONResponse:
    """Send a message and get a response (blocking)."""
    message = body.get("message", "")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)

    try:
        agent = _get_agent()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, agent.chat, message)

    return JSONResponse({
        "response": result,
        "history_length": len(agent.messages),
    })


@app.post("/api/reset")
async def reset() -> JSONResponse:
    """Clear conversation history."""
    agent = _get_agent()
    agent.reset()
    return JSONResponse({"ok": True})


@app.get("/api/history")
async def history() -> JSONResponse:
    """Get conversation history."""
    agent = _get_agent()
    return JSONResponse({"messages": agent.get_history()})


@app.get("/api/config")
async def config() -> JSONResponse:
    """Get agent configuration (secrets masked, never raw)."""
    from browser_py.agent.config import get_provider_credentials_status
    cfg = get_agent_config()
    providers_cfg = cfg.get("providers", {})

    safe = {
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "workspace": cfg.get("workspace"),
        "browser_profile": cfg.get("browser_profile"),
        "shell_enabled": cfg.get("shell_enabled", True),
        "timeout": cfg.get("timeout", 300),
        "configured": is_configured(),
        "credentials": get_provider_credentials_status(),
    }

    # Include non-secret provider fields (base_url, api_version, region, etc.)
    from browser_py.agent.config import PROVIDERS
    provider_fields = {}
    for pkey, pinfo in PROVIDERS.items():
        fields = pinfo.get("fields", [])
        pcfg = providers_cfg.get(pkey, {})
        non_secret = {}
        for f in fields:
            if not f.get("secret"):
                non_secret[f["key"]] = pcfg.get(f["key"], "")
        if non_secret:
            provider_fields[pkey] = non_secret
    safe["provider_fields"] = provider_fields

    return JSONResponse(safe)



# ── Session endpoints ──


@app.get("/api/sessions")
async def list_sessions_api() -> JSONResponse:
    """List saved chat sessions."""
    from browser_py.agent.sessions import list_sessions
    return JSONResponse({"sessions": list_sessions()})


@app.post("/api/sessions/save")
async def save_session_api(body: dict) -> JSONResponse:
    """Save the current chat session."""
    try:
        agent = _get_agent()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    title = body.get("title")
    meta = agent.save_session(title=title)
    return JSONResponse({"ok": True, "session": meta})


@app.post("/api/sessions/load")
async def load_session_api(body: dict) -> JSONResponse:
    """Load a saved session into the agent."""
    session_id = body.get("session_id")
    if not session_id:
        return JSONResponse({"error": "session_id required"}, status_code=400)

    try:
        agent = _get_agent()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if agent.load_session(session_id):
        return JSONResponse({
            "ok": True,
            "message_count": len(agent.messages),
            "token_usage": agent.get_token_usage(),
        })
    return JSONResponse({"error": "Session not found"}, status_code=404)


@app.delete("/api/sessions/{session_id}")
async def delete_session_api(session_id: str) -> JSONResponse:
    """Delete a saved session."""
    from browser_py.agent.sessions import delete_session
    if delete_session(session_id):
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "Session not found"}, status_code=404)


@app.get("/api/sessions/{session_id}/export")
async def export_session_api(session_id: str) -> JSONResponse:
    """Export a session as markdown."""
    from browser_py.agent.sessions import export_session_markdown
    md = export_session_markdown(session_id)
    if md:
        return JSONResponse({"markdown": md})
    return JSONResponse({"error": "Session not found"}, status_code=404)


@app.get("/api/probe")
async def probe_agent() -> JSONResponse:
    """Probe the agent's current activity state."""
    try:
        agent = _get_agent()
        return JSONResponse(agent.probe())
    except RuntimeError:
        return JSONResponse({"state": "idle"})


@app.post("/api/flush")
async def flush_agent() -> JSONResponse:
    """Abort the running agent loop, dump context, cancel the task."""
    global _chat_task
    try:
        agent = _get_agent()
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    # Set abort flags
    agent._abort = True
    _research_abort.set()

    # Dump context immediately from here — don't wait for the loop
    dump_path = None
    if agent.messages:
        loop = asyncio.get_event_loop()
        dump_path = await loop.run_in_executor(None, agent._do_context_dump, "flush")

    # Cancel the running chat task if any
    if _chat_task and not _chat_task.done():
        _chat_task.cancel()
        _chat_task = None

    dump_name = str(dump_path.name) if dump_path else "no messages to dump"
    return JSONResponse({"ok": True, "message": f"Flushed — {dump_name}"})


@app.get("/api/tokens")
async def get_tokens() -> JSONResponse:
    """Get current token usage for the active session."""
    try:
        agent = _get_agent()
        return JSONResponse(agent.get_token_usage())
    except RuntimeError:
        return JSONResponse({
            "total_tokens": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "context_limit": 128000,
            "usage_percent": 0,
            "warning": False,
            "critical": False,
            "model": "",
        })


# ── Research endpoint ──


@app.post("/api/research")
async def start_research(body: dict) -> JSONResponse:
    """Start a deep research session (runs in background, streams via WS)."""
    query = body.get("query", "")
    if not query:
        return JSONResponse({"error": "query required"}, status_code=400)

    num_agents = body.get("num_agents", 5)
    cfg = get_agent_config()

    def on_progress(stage: str, message: str) -> None:
        msg = json.dumps({"type": "research_progress", "stage": stage, "message": message})
        _broadcast(msg)

    import threading

    _research_abort.clear()

    def _run() -> None:
        from browser_py.agent.research import run_research
        try:
            result = run_research(
                query=query,
                on_progress=on_progress,
                browser_profile=cfg.get("browser_profile"),
                num_agents=num_agents,
                abort_event=_research_abort,
            )
            if _research_abort.is_set():
                msg = json.dumps({"type": "research_error", "error": "Flushed by user"})
            else:
                msg = json.dumps({
                    "type": "research_complete",
                    "report_path": result["report_path"],
                    "report": result["report"][:50000],
                    "duration": result["duration_seconds"],
                    "subtopics": result["subtopics"],
                })
            _broadcast(msg)
        except Exception as e:
            msg = json.dumps({
                "type": "research_error",
                "error": str(e),
            })
            _broadcast(msg)

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Research started"})


# ── Validate API key ──


@app.post("/api/validate-key")
async def validate_key(body: dict) -> JSONResponse:
    """Validate an API key by making a minimal API call."""
    provider = body.get("provider", "")
    api_key = body.get("api_key", "")

    if not provider or not api_key:
        return JSONResponse({"valid": False, "error": "provider and api_key required"})

    import asyncio
    loop = asyncio.get_event_loop()

    def _validate() -> dict:
        try:
            from browser_py.agent.models import fetch_models
            models = fetch_models(provider, api_key=api_key)
            if models:
                return {"valid": True, "model_count": len(models)}
            return {"valid": False, "error": "No models returned"}
        except Exception as e:
            return {"valid": False, "error": str(e)}

    result = await loop.run_in_executor(None, _validate)
    return JSONResponse(result)


@app.get("/api/jobs")
async def list_jobs() -> JSONResponse:
    """List cron jobs."""
    from browser_py.agent.tools.cron import _load_jobs
    jobs = _load_jobs()
    return JSONResponse({"jobs": jobs})


@app.get("/api/profiles")
async def list_browser_profiles() -> JSONResponse:
    """List browser profiles."""
    from browser_py.profiles import list_profiles
    profiles = list_profiles()
    return JSONResponse({"profiles": profiles})


@app.post("/api/profiles")
async def create_browser_profile(body: dict) -> JSONResponse:
    """Create a new browser profile."""
    from browser_py.profiles import create_profile
    name = body.get("name", "")
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    try:
        profile = create_profile(name)
        return JSONResponse({"profile": profile})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/profiles/launch")
async def launch_browser_profile(body: dict) -> JSONResponse:
    """Launch a browser profile."""
    import json as _json
    from urllib.request import urlopen
    from urllib.error import URLError
    from browser_py.profiles import get_profile
    from browser_py.core import Browser

    name = body.get("name")
    try:
        profile = get_profile(name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    port = profile["port"]

    # Check if already running
    try:
        _json.loads(urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read())
        return JSONResponse({
            "status": "already_running",
            "profile": profile["name"],
            "port": port,
        })
    except (URLError, OSError):
        pass

    # Launch it
    try:
        download_dir = str(get_workspace() / "downloads")
        Browser.launch(
            port=port,
            user_data_dir=profile["path"],
            download_dir=download_dir,
        )
        return JSONResponse({
            "status": "launched",
            "profile": profile["name"],
            "port": port,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/profiles/status")
async def profile_status() -> JSONResponse:
    """Check which profiles have a running browser."""
    import json as _json
    from urllib.request import urlopen
    from urllib.error import URLError
    from browser_py.profiles import list_profiles

    profiles = list_profiles()
    for p in profiles:
        try:
            _json.loads(urlopen(f"http://127.0.0.1:{p['port']}/json/version", timeout=1).read())
            p["running"] = True
        except (URLError, OSError):
            p["running"] = False

    return JSONResponse({"profiles": profiles})


@app.post("/api/config")
async def update_config(body: dict) -> JSONResponse:
    """Update agent configuration (partial — settings page)."""
    from browser_py.agent.config import load_config, save_config
    config = load_config()
    agent_cfg = config.get("agent", {})

    allowed = {"model", "shell_enabled", "browser_profile", "timeout"}
    for key in allowed:
        if key in body:
            agent_cfg[key] = body[key]

    config["agent"] = agent_cfg
    save_config(config)
    return JSONResponse({"ok": True})


@app.get("/api/providers")
async def list_providers() -> JSONResponse:
    """List available providers with metadata (no models — use /api/models)."""
    from browser_py.agent.config import PROVIDERS
    result = {}
    for key, info in PROVIDERS.items():
        entry = {
            "name": info["name"],
            "default_model": info["default_model"],
            "note": info.get("note", ""),
            "is_oauth": info.get("is_oauth", False),
        }
        if info.get("fields"):
            entry["fields"] = info["fields"]
        result[key] = entry
    return JSONResponse(result)


@app.get("/api/models/{provider}")
async def get_models(
    provider: str,
    api_key: str | None = None,
    q: str | None = None,
    tool_use_only: bool = False,
) -> JSONResponse:
    """Fetch available models for a provider (live from API, cached 10min).

    Optional query params:
    - q: filter models by search string
    - tool_use_only: only return models that support tool use
    """
    from browser_py.agent.models import fetch_models
    import asyncio

    # Gather extra credentials from config for Bedrock etc.
    extra = {}
    cfg = get_agent_config()
    pcfg = cfg.get("providers", {}).get(provider, {})
    for key in ("aws_access_key_id", "aws_secret_access_key", "aws_region", "aws_profile"):
        if pcfg.get(key):
            extra[key] = pcfg[key]

    loop = asyncio.get_event_loop()
    models = await loop.run_in_executor(
        None, lambda: fetch_models(provider, api_key, extra, tool_use_only=tool_use_only)
    )

    # Server-side search filter
    if q:
        q_lower = q.lower()
        models = [m for m in models if q_lower in m["id"].lower() or q_lower in m.get("name", "").lower()]

    return JSONResponse({"models": models})


@app.get("/api/browse-dirs")
async def browse_dirs(path: str = "~") -> JSONResponse:
    """List directories at a given path for folder picker."""
    from pathlib import Path as P
    resolved = P(path).expanduser().resolve()
    if not resolved.is_dir():
        return JSONResponse({"error": "Not a directory"}, status_code=400)
    dirs = []
    try:
        for entry in sorted(resolved.iterdir()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                dirs.append(entry.name)
    except PermissionError:
        pass
    return JSONResponse({
        "current": str(resolved),
        "parent": str(resolved.parent) if resolved != resolved.parent else None,
        "dirs": dirs,
    })


@app.post("/api/setup")
async def run_setup(body: dict) -> JSONResponse:
    """Full setup — provider, key, model, workspace, browser, shell."""
    from browser_py.agent.config import load_config, save_config
    from browser_py.profiles import get_profile, create_profile

    config = load_config()
    agent_cfg = config.get("agent", {})

    provider = body.get("provider")
    api_key = body.get("api_key")
    model = body.get("model")
    workspace = body.get("workspace")
    browser_profile = body.get("browser_profile")
    shell_enabled = body.get("shell_enabled", True)

    if not provider:
        return JSONResponse({"error": "provider required"}, status_code=400)

    agent_cfg["provider"] = provider

    # Store API key (simple providers)
    providers_cfg = agent_cfg.get("providers", {})
    if api_key:
        providers_cfg.setdefault(provider, {})["api_key"] = api_key
    agent_cfg["providers"] = providers_cfg

    # Store provider-specific fields (Bedrock, Azure, Vertex, etc.)
    from browser_py.agent.config import PROVIDERS as PROVIDER_DEFS
    pinfo = PROVIDER_DEFS.get(provider, {})
    if pinfo.get("fields"):
        pcfg = providers_cfg.setdefault(provider, {})
        for f in pinfo["fields"]:
            fkey = f["key"]
            val = body.get(fkey)
            if val is not None and val != "":
                pcfg[fkey] = val
    # Legacy Azure fields (backward compat)
    elif provider == "azure":
        if body.get("azure_endpoint"):
            providers_cfg.setdefault("azure", {})["base_url"] = body["azure_endpoint"]
        if body.get("azure_api_version"):
            providers_cfg.setdefault("azure", {})["api_version"] = body["azure_api_version"]

    if model:
        agent_cfg["model"] = model
    if workspace:
        from pathlib import Path
        ws = Path(workspace).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        agent_cfg["workspace"] = str(ws)

    agent_cfg["shell_enabled"] = shell_enabled
    if body.get("timeout") is not None:
        agent_cfg["timeout"] = int(body["timeout"])

    # Browser profile — create if needed
    if browser_profile:
        try:
            get_profile(browser_profile)
        except ValueError:
            create_profile(browser_profile)
        agent_cfg["browser_profile"] = browser_profile

    config["agent"] = agent_cfg
    save_config(config)

    # Reset the global agent so it picks up new config
    global _agent
    _agent = None

    return JSONResponse({"ok": True, "configured": True})


# ── WebSocket for live updates ──


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)

            if msg.get("type") == "chat":
                try:
                    agent = _get_agent()
                except RuntimeError as e:
                    await ws.send_text(json.dumps({
                        "type": "response",
                        "content": f"⚠️ {e}\nPlease complete setup in the Settings page.",
                    }))
                    continue

                # Check context limit before sending
                usage = agent.get_token_usage()
                if usage["critical"]:
                    await ws.send_text(json.dumps({
                        "type": "context_warning",
                        "level": "critical",
                        "message": (
                            f"⚠️ Context is {usage['usage_percent']}% full "
                            f"({usage['total_tokens']:,} / {usage['context_limit']:,} tokens). "
                            "Consider starting a new chat to avoid degraded responses."
                        ),
                        "usage": usage,
                    }))

                await ws.send_text(json.dumps({"type": "thinking"}))

                loop = asyncio.get_event_loop()
                _chat_task = loop.create_task(
                    loop.run_in_executor(None, agent.chat, msg.get("message", ""))
                )
                try:
                    result = await _chat_task
                except asyncio.CancelledError:
                    result = "(Flushed — context saved to context_dumps/.)"
                finally:
                    _chat_task = None

                # Clean up browser tabs opened during this exchange
                try:
                    await loop.run_in_executor(None, agent.cleanup_browser)
                except Exception:
                    pass

                # Auto-save session after each exchange
                session_meta = await loop.run_in_executor(None, agent.save_session)

                await ws.send_text(json.dumps({
                    "type": "response",
                    "content": result,
                    "token_usage": agent.get_token_usage(),
                    "session_id": agent.session_id,
                }))

            elif msg.get("type") == "reset":
                agent = _get_agent()
                # Save current session before reset (if it has messages)
                if agent.messages:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, agent.save_session)
                agent.reset()
                await ws.send_text(json.dumps({"type": "reset_ok"}))

    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        # Clean up browser tabs when client disconnects
        try:
            if _agent:
                _agent.cleanup_browser()
        except Exception:
            pass


# ── Scheduler ──


_scheduler = None


def _add_job_to_scheduler(job: dict) -> None:
    """Add a single job to the running scheduler."""
    if _scheduler is None:
        return

    try:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
        from apscheduler.triggers.date import DateTrigger
    except ImportError:
        return

    jid = job.get("id", "")
    task_text = job.get("task", "")

    # Remove existing job if it exists (for updates)
    try:
        _scheduler.remove_job(jid)
    except Exception:
        pass

    if job.get("paused"):
        return

    if job.get("schedule_type") == "cron":
        parts = job.get("cron", "").split()
        if len(parts) == 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone=job.get("timezone") or None,
            )
            _scheduler.add_job(
                _run_scheduled_task, trigger, args=[task_text], id=jid
            )
    elif job.get("schedule_type") == "interval":
        minutes = job.get("interval_minutes", 60)
        _scheduler.add_job(
            _run_scheduled_task,
            IntervalTrigger(minutes=minutes),
            args=[task_text],
            id=jid,
        )
    elif job.get("schedule_type") == "date":
        _scheduler.add_job(
            _run_scheduled_task,
            DateTrigger(run_date=job["run_at"]),
            args=[task_text],
            id=jid,
        )


def _on_job_change(action: str, job: dict) -> None:
    """Handle live cron job changes from the agent tool."""
    if _scheduler is None:
        return

    jid = job.get("id", "")

    if action == "remove":
        try:
            _scheduler.remove_job(jid)
        except Exception:
            pass
    elif action == "pause":
        try:
            _scheduler.pause_job(jid)
        except Exception:
            pass
    elif action == "resume":
        try:
            _scheduler.resume_job(jid)
        except Exception:
            pass
    elif action == "run_now":
        # Execute immediately in a thread
        task_text = job.get("task", "")
        if task_text:
            import threading
            threading.Thread(
                target=_run_scheduled_task, args=[task_text], daemon=True
            ).start()
    elif action == "add":
        _add_job_to_scheduler(job)


def _start_scheduler() -> None:
    """Start APScheduler for cron jobs."""
    global _scheduler

    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        return  # APScheduler not installed — cron disabled

    from browser_py.agent.tools.cron import _load_jobs

    _scheduler = BackgroundScheduler()
    jobs = _load_jobs()

    for jid, job in jobs.items():
        job["id"] = jid  # ensure id is set
        _add_job_to_scheduler(job)

    _scheduler.start()


def _run_scheduled_task(task: str) -> None:
    """Execute a scheduled task in a fresh agent context."""
    cfg = get_agent_config()
    agent = Agent(
        browser_profile=cfg.get("browser_profile"),
    )
    if not cfg.get("shell_enabled", True):
        agent._shell.enabled = False

    try:
        result = agent.chat(task)
        # Log result
        log_dir = get_workspace() / ".cron_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{int(time.time())}.log"
        log_file.write_text(f"Task: {task}\n\nResult:\n{result}\n")
    except Exception as e:
        log_dir = get_workspace() / ".cron_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{int(time.time())}_error.log"
        log_file.write_text(f"Task: {task}\n\nError:\n{e}\n")
    finally:
        # Clean up browser tabs opened by this task
        try:
            agent.cleanup_browser()
        except Exception:
            pass


# ── Server entry point ──


def start_server(host: str = "127.0.0.1", port: int = 8321) -> None:
    """Start the web server."""
    global _loop
    import uvicorn

    print(f"\n🌐 browser-py agent running at http://{host}:{port}\n")

    _start_scheduler()

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(server.serve())


# ── Fallback HTML (embedded chat UI) ──

_FALLBACK_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>browser-py</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --text-dim: #8b949e; --accent: #58a6ff;
    --tool-bg: #1c2128; --user-bg: #1f3a5f; --agent-bg: #1c2128;
    --danger: #f85149; --success: #3fb950;
  }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text); height: 100vh; display: flex; }

  /* Sidebar */
  #sidebar { width: 220px; border-right: 1px solid var(--border); display: flex;
    flex-direction: column; background: var(--surface); flex-shrink: 0; }
  #sidebar .logo { padding: 16px; font-size: 15px; font-weight: 700;
    border-bottom: 1px solid var(--border); }
  #sidebar nav { flex: 1; padding: 8px 0; overflow-y: auto; }
  #sidebar nav a { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
    color: var(--text-dim); text-decoration: none; font-size: 13px; cursor: pointer;
    border-left: 3px solid transparent; }
  #sidebar nav a:hover { color: var(--text); background: rgba(255,255,255,0.04); }
  #sidebar nav a.active { color: var(--accent); border-left-color: var(--accent);
    background: rgba(88,166,255,0.08); }
  #sidebar .version { padding: 12px 16px; font-size: 11px; color: var(--text-dim);
    border-top: 1px solid var(--border); }

  /* Sessions list in sidebar */
  #sidebar .sessions-section { border-top: 1px solid var(--border); padding: 8px 0; }
  #sidebar .sessions-section .section-title { padding: 4px 16px; font-size: 11px;
    color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  #sidebar .session-item { display: block; padding: 6px 16px; font-size: 12px;
    color: var(--text-dim); cursor: pointer; text-decoration: none;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    border-left: 3px solid transparent; }
  #sidebar .session-item:hover { color: var(--text); background: rgba(255,255,255,0.04); }
  #sidebar .session-item.active { color: var(--accent); border-left-color: var(--accent); }

  /* Token usage bar */
  .token-bar-wrap { padding: 8px 16px; border-top: 1px solid var(--border); }
  .token-bar { height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
  .token-bar .fill { height: 100%; border-radius: 2px; transition: width 0.3s; }
  .token-bar .fill.ok { background: var(--accent); }
  .token-bar .fill.warn { background: #d29922; }
  .token-bar .fill.crit { background: var(--danger); }
  .token-bar-label { font-size: 10px; color: var(--text-dim); margin-top: 2px;
    display: flex; justify-content: space-between; }

  /* Main area */
  #main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  header { padding: 12px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px; }
  header h2 { font-size: 15px; font-weight: 600; }
  header .status { font-size: 12px; color: var(--text-dim); margin-left: auto; }

  /* Pages */
  .page { flex: 1; display: none; flex-direction: column; overflow: hidden;
    min-height: 0; }
  .page.active { display: flex; }

  /* Chat page */
  #chat-messages { flex: 1; overflow-y: auto; padding: 16px 20px; display: flex;
    flex-direction: column; gap: 12px; }
  .msg { max-width: 85%; padding: 10px 14px; border-radius: 12px;
    font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
  .msg.user { background: var(--user-bg); align-self: flex-end;
    border-bottom-right-radius: 4px; }
  .msg.agent { background: var(--agent-bg); align-self: flex-start;
    border-bottom-left-radius: 4px; border: 1px solid var(--border); }
  .msg.tool { background: var(--tool-bg); align-self: flex-start; font-size: 12px;
    font-family: 'SF Mono', Monaco, monospace; color: var(--text-dim);
    border-left: 3px solid var(--accent); max-width: 90%; }
  .msg.tool .tool-name { color: var(--accent); font-weight: 600; }
  .msg.thinking { color: var(--text-dim); font-style: italic; }
  #input-area { padding: 12px 20px; border-top: 1px solid var(--border);
    display: flex; gap: 8px; }
  #input { flex: 1; background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; padding: 10px 14px; color: var(--text); font-size: 14px;
    outline: none; resize: none; min-height: 44px; max-height: 120px;
    font-family: inherit; }
  #input:focus { border-color: var(--accent); }
  #input::placeholder { color: var(--text-dim); }
  #send { background: var(--accent); border: none; border-radius: 8px;
    padding: 10px 20px; color: #fff; font-size: 14px; font-weight: 600;
    cursor: pointer; }
  #send:hover { opacity: 0.9; }
  #send:disabled { opacity: 0.4; cursor: default; }

  /* Settings / config pages */
  .page-content { flex: 1; overflow-y: auto; padding: 24px 32px; max-width: 700px;
    padding-bottom: 80px; }
  .card { background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 20px; margin-bottom: 16px; }
  .card h3 { font-size: 14px; margin-bottom: 12px; color: var(--text); }
  .card p { font-size: 13px; color: var(--text-dim); margin-bottom: 8px; }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 12px; color: var(--text-dim);
    margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.5px; }
  .field input, .field select { background: var(--bg); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 12px; color: var(--text); font-size: 13px;
    width: 100%; outline: none; }
  .field input:focus, .field select:focus { border-color: var(--accent); }
  .btn { background: var(--accent); border: none; border-radius: 6px;
    padding: 8px 16px; color: #fff; font-size: 13px; cursor: pointer;
    font-weight: 500; }
  .btn:hover { opacity: 0.9; }
  .btn.secondary { background: var(--surface); border: 1px solid var(--border);
    color: var(--text-dim); }
  .btn.secondary:hover { color: var(--text); border-color: var(--text-dim); }
  .btn.danger { background: var(--danger); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 11px; font-weight: 600; }
  .badge.active { background: rgba(63,185,80,0.15); color: var(--success); }
  .badge.paused { background: rgba(248,81,73,0.15); color: var(--danger); }

  /* Profile / job list */
  .list-item { display: flex; align-items: center; gap: 12px; padding: 10px 0;
    border-bottom: 1px solid var(--border); font-size: 13px; }
  .list-item:last-child { border-bottom: none; }
  .list-item .name { font-weight: 500; flex: 1; }
  .list-item .meta { color: var(--text-dim); font-size: 12px; }
  .empty { color: var(--text-dim); font-size: 13px; padding: 20px 0; text-align: center; }

  /* Credential status badges */
  .key-status { font-size: 11px; margin-top: 4px; }
  .key-status.configured { color: var(--success); }
  .key-status.missing { color: var(--text-dim); }

  /* Research panel */
  .research-panel { background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 16px; margin: 12px 20px; }
  .research-panel h3 { font-size: 14px; margin-bottom: 8px; }
  .research-panel .progress-steps { margin: 12px 0; }
  .research-panel .step { padding: 6px 0; font-size: 13px; color: var(--text-dim);
    display: flex; align-items: center; gap: 8px; }
  .research-panel .step.active { color: var(--accent); }
  .research-panel .step.done { color: var(--success); }
  .research-report { background: var(--bg); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px; margin-top: 12px; max-height: 60vh;
    overflow-y: auto; font-size: 13px; line-height: 1.6; white-space: pre-wrap; }

  /* Setup wizard steps */
  .wizard-steps { display: flex; gap: 4px; margin-bottom: 20px; }
  .wizard-step { flex: 1; height: 4px; border-radius: 2px; background: var(--border); }
  .wizard-step.done { background: var(--success); }
  .wizard-step.current { background: var(--accent); }
  .wizard-section { display: none; }
  .wizard-section.active { display: block; }
  .wizard-nav { display: flex; gap: 8px; margin-top: 16px; }
  .wizard-nav .btn { flex: 1; }
  .validation-status { font-size: 12px; margin-top: 8px; padding: 8px 12px;
    border-radius: 6px; display: none; }
  .validation-status.checking { display: block; background: rgba(88,166,255,0.1);
    color: var(--accent); }
  .validation-status.valid { display: block; background: rgba(63,185,80,0.1);
    color: var(--success); }
  .validation-status.invalid { display: block; background: rgba(248,81,73,0.1);
    color: var(--danger); }

  /* Tool-use filter toggle */
  .filter-toggle { display: flex; align-items: center; gap: 6px; margin: 8px 0;
    font-size: 12px; color: var(--text-dim); }
  .filter-toggle input { width: auto; }

  /* Context warning banner */
  .context-warning { background: rgba(210,153,34,0.15); border: 1px solid rgba(210,153,34,0.3);
    border-radius: 8px; padding: 10px 14px; margin: 8px 20px; font-size: 13px;
    color: #d29922; display: none; }
  .context-warning.critical { background: rgba(248,81,73,0.15);
    border-color: rgba(248,81,73,0.3); color: var(--danger); }
  .context-warning .dismiss { float: right; cursor: pointer; opacity: 0.7; }
  .context-warning .dismiss:hover { opacity: 1; }

  /* Folder picker modal */
  .folder-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6);
    z-index: 200; display: none; align-items: center; justify-content: center; }
  .folder-modal-overlay.open { display: flex; }
  .folder-modal { background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; width: 480px; max-height: 70vh; display: flex;
    flex-direction: column; overflow: hidden; }
  .folder-modal-header { padding: 14px 16px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; }
  .folder-modal-header h3 { font-size: 14px; margin: 0; }
  .folder-modal-path { padding: 8px 16px; font-size: 12px; color: var(--text-dim);
    border-bottom: 1px solid var(--border); word-break: break-all;
    display: flex; align-items: center; gap: 6px; }
  .folder-modal-path .path-text { flex: 1; font-family: monospace; }
  .folder-modal-list { flex: 1; overflow-y: auto; padding: 4px 0; min-height: 200px; }
  .folder-modal-list .folder-item { display: flex; align-items: center; gap: 8px;
    padding: 7px 16px; cursor: pointer; font-size: 13px; color: var(--text); }
  .folder-modal-list .folder-item:hover { background: rgba(255,255,255,0.05); }
  .folder-modal-list .folder-item .icon { opacity: 0.5; }
  .folder-modal-list .empty { padding: 16px; color: var(--text-dim); font-size: 13px;
    text-align: center; }
  .folder-modal-footer { padding: 12px 16px; border-top: 1px solid var(--border);
    display: flex; gap: 8px; justify-content: flex-end; }
  .folder-input-wrap { display: flex; gap: 6px; }
  .folder-input-wrap input { flex: 1; }
  .folder-input-wrap .btn { flex-shrink: 0; padding: 6px 12px; font-size: 12px; }

  /* Searchable model picker */
  .model-search-wrap { position: relative; }
  .model-search-wrap input { width: 100%; }
  .model-search-wrap .model-dropdown {
    display: none; position: absolute; top: 100%; left: 0; right: 0;
    max-height: 280px; overflow-y: auto; background: var(--surface);
    border: 1px solid var(--border); border-top: none; border-radius: 0 0 6px 6px;
    z-index: 100;
  }
  .model-search-wrap .model-dropdown.open { display: block; }
  .model-search-wrap .model-dropdown .model-opt {
    padding: 6px 12px; font-size: 13px; cursor: pointer; display: flex;
    justify-content: space-between; align-items: center;
  }
  .model-search-wrap .model-dropdown .model-opt:hover,
  .model-search-wrap .model-dropdown .model-opt.highlighted {
    background: rgba(88,166,255,0.12);
  }
  .model-search-wrap .model-dropdown .model-opt .model-id { color: var(--text); }
  .model-search-wrap .model-dropdown .model-opt .model-meta {
    color: var(--text-dim); font-size: 11px;
  }
  .model-search-wrap .model-count {
    font-size: 11px; color: var(--text-dim); margin-top: 4px;
  }
</style>
</head>
<body>

<div id="sidebar">
  <div class="logo">🌐 browser-py</div>
  <nav>
    <a class="active" data-page="chat" onclick="showPage('chat')">💬 Chat</a>
    <a data-page="research" onclick="showPage('research')">🔬 Research</a>
    <a data-page="profiles" onclick="showPage('profiles')">🌍 Browser Profiles</a>
    <a data-page="jobs" onclick="showPage('jobs')">⏰ Scheduled Jobs</a>
    <a data-page="settings" onclick="showPage('settings')">⚙️ Settings</a>
    <div class="sessions-section" id="sessions-section">
      <div class="section-title">Recent Chats</div>
      <div id="sessions-list"></div>
    </div>
  </nav>
  <div class="token-bar-wrap" id="token-bar-wrap" style="display:none">
    <div class="token-bar"><div class="fill ok" id="token-fill" style="width:0%"></div></div>
    <div class="token-bar-label">
      <span id="token-label">0 tokens</span>
      <span id="token-pct">0%</span>
    </div>
  </div>
  <div class="version" id="version-info">browser-py</div>
</div>

<div id="main">
  <!-- Setup Page (shown when not configured) -->
  <div class="page" id="page-setup">
    <header><h2>🔧 Setup</h2></header>
    <div class="page-content">
      <div class="card">
        <h3>Welcome to browser-py</h3>
        <p>Let's configure your AI agent step by step.</p>
      </div>

      <div class="wizard-steps" id="wizard-steps">
        <div class="wizard-step current" data-step="1"></div>
        <div class="wizard-step" data-step="2"></div>
        <div class="wizard-step" data-step="3"></div>
        <div class="wizard-step" data-step="4"></div>
        <div class="wizard-step" data-step="5"></div>
      </div>

      <!-- Step 1: Provider + Key -->
      <div class="wizard-section active" id="wizard-1">
        <div class="card">
          <h3>Step 1: LLM Provider &amp; API Key</h3>
          <div class="field">
            <label>Provider</label>
            <select id="setup-provider" onchange="onSetupProviderChange()">
              <option value="">— Select —</option>
            </select>
          </div>
          <div id="setup-provider-note" style="font-size:12px;color:var(--text-dim);margin-bottom:12px;display:none"></div>
          <div id="setup-key-section"></div>
          <div id="setup-provider-fields"></div>
          <div class="validation-status" id="key-validation"></div>
        </div>
        <div class="wizard-nav">
          <button class="btn" onclick="wizardNext(1)" id="wizard-next-1">Next →</button>
        </div>
      </div>

      <!-- Step 2: Model -->
      <div class="wizard-section" id="wizard-2">
        <div class="card">
          <h3>Step 2: Choose a Model</h3>
          <div class="filter-toggle">
            <input type="checkbox" id="setup-tool-filter" checked onchange="onSetupToolFilterChange()">
            <label for="setup-tool-filter" style="margin:0;text-transform:none">Only show models with tool-use support</label>
          </div>
          <div id="setup-model-picker"></div>
          <div class="field" style="margin-top:8px">
            <label>Or type a custom model name</label>
            <input type="text" id="setup-model-custom" placeholder="Leave empty to use selection above">
          </div>
        </div>
        <div class="wizard-nav">
          <button class="btn secondary" onclick="wizardBack(2)">← Back</button>
          <button class="btn" onclick="wizardNext(2)">Next →</button>
        </div>
      </div>

      <!-- Step 3: Workspace -->
      <div class="wizard-section" id="wizard-3">
        <div class="card">
          <h3>Step 3: Workspace Directory</h3>
          <p>All file operations are sandboxed to this directory.</p>
          <div class="field">
            <label>Path</label>
            <div class="folder-input-wrap">
              <input type="text" id="setup-workspace" placeholder="~/browser-py-workspace">
              <button class="btn" onclick="openFolderPicker('setup-workspace')">Browse</button>
            </div>
          </div>
        </div>
        <div class="wizard-nav">
          <button class="btn secondary" onclick="wizardBack(3)">← Back</button>
          <button class="btn" onclick="wizardNext(3)">Next →</button>
        </div>
      </div>

      <!-- Step 4: Browser Profile -->
      <div class="wizard-section" id="wizard-4">
        <div class="card">
          <h3>Step 4: Browser Profile</h3>
          <p>Each profile keeps its own logins and cookies.</p>
          <div class="field">
            <label>Profile</label>
            <select id="setup-browser-profile"></select>
          </div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
            <input type="text" id="setup-new-profile" placeholder="New profile name" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:13px;flex:1">
            <button class="btn secondary" onclick="setupCreateProfile()" style="white-space:nowrap">Create</button>
          </div>
        </div>
        <div class="wizard-nav">
          <button class="btn secondary" onclick="wizardBack(4)">← Back</button>
          <button class="btn" onclick="wizardNext(4)">Next →</button>
        </div>
      </div>

      <!-- Step 5: Permissions + Finish -->
      <div class="wizard-section" id="wizard-5">
        <div class="card">
          <h3>Step 5: Permissions</h3>
          <div class="field" style="display:flex;align-items:center;gap:8px">
            <input type="checkbox" id="setup-shell" style="width:auto" checked>
            <label for="setup-shell" style="margin:0;text-transform:none">Allow shell commands</label>
          </div>
        </div>
        <div class="wizard-nav">
          <button class="btn secondary" onclick="wizardBack(5)">← Back</button>
          <button class="btn" onclick="submitSetup()" id="setup-submit" style="flex:2">Save &amp; Start</button>
        </div>
      </div>

      <div id="setup-error" style="color:var(--danger);font-size:13px;margin-top:8px;display:none"></div>
    </div>
  </div>

  <!-- Chat Page -->
  <div class="page" id="page-chat">
    <header>
      <h2>Chat</h2>
      <button class="btn secondary" onclick="probeAgent()" id="probe-btn" style="margin-left:auto;font-size:12px;padding:6px 10px" title="Check what the agent is doing right now">🔍 Probe</button>
      <button class="btn danger" onclick="flushAgent()" id="flush-btn" style="font-size:12px;padding:6px 10px" title="Stop the agent and dump context">⏹ Flush</button>
      <button class="btn secondary" onclick="resetChat()" style="font-size:12px;padding:6px 10px">New Chat</button>
      <div class="status" id="status">Connecting...</div>
    </header>
    <div class="context-warning" id="context-warning">
      <span class="dismiss" onclick="this.parentElement.style.display='none'">✕</span>
      <span id="context-warning-text"></span>
    </div>
    <div id="chat-messages"></div>
    <div id="input-area">
      <textarea id="input" placeholder="What should I do?" rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"></textarea>
      <button id="send" onclick="send()">Send</button>
    </div>
  </div>

  <!-- Research Page -->
  <div class="page" id="page-research">
    <header>
      <h2>🔬 Deep Research</h2>
      <button class="btn secondary" onclick="probeAgent()" style="margin-left:auto;font-size:12px;padding:6px 10px" title="Check what the agent is doing right now">🔍 Probe</button>
      <button class="btn danger" onclick="flushAgent()" style="font-size:12px;padding:6px 10px" title="Stop the agent and dump context">⏹ Flush</button>
    </header>
    <div class="page-content">
      <div class="card">
        <h3>Research Query</h3>
        <p>Enter a topic and the agent will deploy 5 sub-agents to research it from multiple angles, then compile a comprehensive report.</p>
        <div class="field">
          <label>What should I research?</label>
          <textarea id="research-query" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;width:100%;min-height:60px;resize:vertical;font-family:inherit" placeholder="e.g., What are the best strategies for SEO in 2025?"></textarea>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <button class="btn" onclick="startResearch()" id="research-start">Start Research</button>
          <span style="font-size:12px;color:var(--text-dim)">Uses 5 sequential sub-agents with browser access</span>
        </div>
      </div>
      <div id="research-progress" style="display:none">
        <div class="card">
          <h3>Research Progress</h3>
          <div class="progress-steps" id="research-steps"></div>
          <div id="research-probe" style="display:none;margin-top:8px;padding:8px 12px;background:var(--bg);border-radius:6px;font-size:12px;color:var(--text-dim);font-family:'SF Mono',Monaco,monospace;white-space:pre-wrap"></div>
        </div>
      </div>
      <div id="research-result" style="display:none">
        <div class="card">
          <h3>Research Report</h3>
          <div style="font-size:12px;color:var(--text-dim);margin-bottom:8px" id="research-meta"></div>
          <div class="research-report" id="research-report-content"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Profiles Page -->
  <div class="page" id="page-profiles">
    <header><h2>Browser Profiles</h2></header>
    <div class="page-content">
      <div class="card">
        <h3>Your Profiles</h3>
        <p>Each profile has its own browser sessions (cookies, logins) and CDP port.</p>
        <div id="profiles-list"><div class="empty">Loading...</div></div>
      </div>
      <div class="card">
        <h3>New Profile</h3>
        <div class="field">
          <label>Profile Name</label>
          <input type="text" id="new-profile-name" placeholder="e.g. work, personal, social">
        </div>
        <button class="btn" onclick="createProfile()">Create Profile</button>
      </div>
    </div>
  </div>

  <!-- Jobs Page -->
  <div class="page" id="page-jobs">
    <header><h2>Scheduled Jobs</h2></header>
    <div class="page-content">
      <div class="card">
        <h3>Cron Jobs</h3>
        <p>Recurring tasks the agent runs automatically.</p>
        <div id="jobs-list"><div class="empty">Loading...</div></div>
      </div>
      <p style="font-size:12px;color:var(--text-dim)">
        💡 Create jobs via chat: "Schedule a task to check my email every morning at 9 AM"
      </p>
    </div>
  </div>

  <!-- Settings Page -->
  <div class="page" id="page-settings">
    <header><h2>Settings</h2></header>
    <div class="page-content">
      <div class="card">
        <h3>LLM Provider &amp; Credentials</h3>
        <div class="field">
          <label>Provider</label>
          <select id="cfg-provider" onchange="onCfgProviderChange()"></select>
        </div>
        <div id="cfg-credentials-status"></div>
        <!-- Dynamic credential fields rendered here -->
        <div id="cfg-key-section"></div>
        <div id="cfg-provider-fields"></div>
      </div>
      <div class="card">
        <h3>Model</h3>
        <div id="cfg-model-picker"></div>
        <div class="field" style="margin-top:8px">
          <label>Custom model (overrides search above)</label>
          <input type="text" id="cfg-model" placeholder="Leave empty to use selection above">
        </div>
      </div>
      <div class="card">
        <h3>Workspace</h3>
        <div class="field">
          <label>Directory</label>
          <div class="folder-input-wrap">
            <input type="text" id="cfg-workspace">
            <button class="btn" onclick="openFolderPicker('cfg-workspace')">Browse</button>
          </div>
        </div>
      </div>
      <div class="card">
        <h3>Permissions</h3>
        <div class="field" style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="cfg-shell" style="width:auto">
          <label for="cfg-shell" style="margin:0;text-transform:none">Allow shell commands</label>
        </div>
        <div class="field">
          <label>Browser Profile</label>
          <select id="cfg-profile"></select>
        </div>
        <div class="field">
          <label>Subagent Timeout (seconds)</label>
          <input type="number" id="cfg-timeout" min="30" max="3600" value="300">
          <p style="font-size:11px;color:var(--text-dim);margin-top:4px">Max time per LLM call. Orphaned threads are killed after this.</p>
        </div>
      </div>
      <button class="btn" onclick="saveSettings()" style="width:100%;padding:10px">Save Settings</button>
      <div id="cfg-saved" style="color:var(--success);font-size:13px;margin-top:8px;display:none">✓ Saved</div>
    </div>
  </div>
</div>

<!-- Folder picker modal -->
<div class="folder-modal-overlay" id="folder-modal">
  <div class="folder-modal">
    <div class="folder-modal-header">
      <h3>📁 Choose Workspace Directory</h3>
      <span style="cursor:pointer;opacity:0.6" onclick="closeFolderPicker()">✕</span>
    </div>
    <div class="folder-modal-path">
      <span class="icon" style="cursor:pointer" id="folder-up" onclick="folderUp()">⬆</span>
      <span class="path-text" id="folder-current-path">/</span>
    </div>
    <div class="folder-modal-list" id="folder-list"></div>
    <div class="folder-modal-footer">
      <button class="btn" style="background:var(--border);color:var(--text)" onclick="closeFolderPicker()">Cancel</button>
      <button class="btn" onclick="selectFolder()">Select This Folder</button>
    </div>
  </div>
</div>

<script>
const chatEl = document.getElementById('chat-messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send');
const statusEl = document.getElementById('status');
let ws, providers = {}, currentCfg = {};

// ── Folder Picker ──
let _folderTarget = null;  // id of the input to fill
let _folderCurrent = '~';

async function openFolderPicker(inputId) {
  _folderTarget = inputId;
  const existing = document.getElementById(inputId).value.trim();
  _folderCurrent = existing || '~';
  await loadFolder(_folderCurrent);
  document.getElementById('folder-modal').classList.add('open');
}

function closeFolderPicker() {
  document.getElementById('folder-modal').classList.remove('open');
  _folderTarget = null;
}

async function loadFolder(path) {
  const list = document.getElementById('folder-list');
  list.innerHTML = '<div class="empty">Loading...</div>';
  try {
    const res = await fetch('/api/browse-dirs?path=' + encodeURIComponent(path));
    const data = await res.json();
    if (data.error) { list.innerHTML = `<div class="empty">${data.error}</div>`; return; }
    _folderCurrent = data.current;
    document.getElementById('folder-current-path').textContent = data.current;
    if (!data.dirs.length) {
      list.innerHTML = '<div class="empty">No subdirectories</div>';
      return;
    }
    list.innerHTML = data.dirs.map(d =>
      `<div class="folder-item" onclick="loadFolder('${(data.current + '/' + d).replace(/'/g, "\\'")}')">` +
      `<span class="icon">📁</span>${d}</div>`
    ).join('');
  } catch (e) {
    list.innerHTML = `<div class="empty">Error: ${e.message}</div>`;
  }
}

function folderUp() {
  const parts = _folderCurrent.split('/');
  if (parts.length > 1) {
    parts.pop();
    loadFolder(parts.join('/') || '/');
  }
}

function selectFolder() {
  if (_folderTarget) {
    document.getElementById(_folderTarget).value = _folderCurrent;
  }
  closeFolderPicker();
}

// Close folder modal on overlay click
document.addEventListener('click', (e) => {
  if (e.target.id === 'folder-modal') closeFolderPicker();
});

// ── Model Picker Component ──
// A searchable combo-box that replaces the old <select>
class ModelPicker {
  constructor(containerId, opts = {}) {
    this.container = document.getElementById(containerId);
    this.allModels = [];
    this.filtered = [];
    this.selectedValue = '';
    this.highlightIdx = -1;
    this.onSelect = opts.onSelect || (() => {});
    this._render();
  }

  _render() {
    this.container.innerHTML = `
      <div class="field">
        <label>MODEL</label>
        <div class="model-search-wrap">
          <input type="text" class="model-search-input" placeholder="Search models..." autocomplete="off">
          <div class="model-dropdown"></div>
          <div class="model-count"></div>
        </div>
      </div>`;
    this.inputEl = this.container.querySelector('.model-search-input');
    this.dropdown = this.container.querySelector('.model-dropdown');
    this.countEl = this.container.querySelector('.model-count');

    this.inputEl.addEventListener('focus', () => this._showDropdown());
    this.inputEl.addEventListener('input', () => this._onInput());
    this.inputEl.addEventListener('keydown', (e) => this._onKeydown(e));
    document.addEventListener('click', (e) => {
      if (!this.container.contains(e.target)) this._hideDropdown();
    });
  }

  setModels(models, defaultValue) {
    this.allModels = models;
    this.filtered = models;
    this._updateCount();
    if (defaultValue) {
      this.selectedValue = defaultValue;
      const m = models.find(m => m.id === defaultValue);
      if (m) {
        this.inputEl.value = m.id;
      } else {
        this.inputEl.value = defaultValue;
      }
    }
    this._renderOptions();
  }

  getValue() {
    return this.selectedValue || this.inputEl.value.trim();
  }

  setLoading() {
    this.allModels = [];
    this.filtered = [];
    this.inputEl.placeholder = 'Loading models...';
    this.countEl.textContent = '';
    this.dropdown.innerHTML = '<div style="padding:8px 12px;color:var(--text-dim);font-size:12px">Loading...</div>';
  }

  _onInput() {
    const q = this.inputEl.value.toLowerCase().trim();
    if (!q) {
      this.filtered = this.allModels;
    } else {
      this.filtered = this.allModels.filter(m =>
        m.id.toLowerCase().includes(q) || (m.name || '').toLowerCase().includes(q)
      );
    }
    this.highlightIdx = -1;
    this._renderOptions();
    this._showDropdown();
    this._updateCount();
    // If user types a model ID directly, accept it
    this.selectedValue = this.inputEl.value.trim();
  }

  _onKeydown(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      this.highlightIdx = Math.min(this.highlightIdx + 1, this.filtered.length - 1);
      this._renderOptions();
      this._scrollToHighlighted();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      this.highlightIdx = Math.max(this.highlightIdx - 1, 0);
      this._renderOptions();
      this._scrollToHighlighted();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (this.highlightIdx >= 0 && this.highlightIdx < this.filtered.length) {
        this._select(this.filtered[this.highlightIdx]);
      }
      this._hideDropdown();
    } else if (e.key === 'Escape') {
      this._hideDropdown();
    }
  }

  _select(model) {
    this.selectedValue = model.id;
    this.inputEl.value = model.id;
    this._hideDropdown();
    this.onSelect(model);
  }

  _showDropdown() {
    if (this.filtered.length) this.dropdown.classList.add('open');
  }
  _hideDropdown() {
    this.dropdown.classList.remove('open');
  }

  _scrollToHighlighted() {
    const el = this.dropdown.querySelector('.highlighted');
    if (el) el.scrollIntoView({ block: 'nearest' });
  }

  _updateCount() {
    const total = this.allModels.length;
    const shown = this.filtered.length;
    if (total > 20) {
      this.countEl.textContent = shown === total
        ? `${total} models available — type to search`
        : `${shown} of ${total} models`;
    } else {
      this.countEl.textContent = '';
    }
    this.inputEl.placeholder = total > 20 ? 'Type to search models...' : 'Search or select a model...';
  }

  _renderOptions() {
    // Cap rendered items at 100 for performance
    const toRender = this.filtered.slice(0, 100);
    this.dropdown.innerHTML = toRender.map((m, i) => {
      const hl = i === this.highlightIdx ? ' highlighted' : '';
      const nameStr = m.name && m.name !== m.id ? m.name : '';
      const ctxStr = m.context ? `${Math.round(m.context/1000)}k ctx` : '';
      const meta = [nameStr, ctxStr].filter(Boolean).join(' · ');
      return `<div class="model-opt${hl}" data-idx="${i}">
        <span class="model-id">${m.id}</span>
        ${meta ? `<span class="model-meta">${meta}</span>` : ''}
      </div>`;
    }).join('');

    if (this.filtered.length > 100) {
      this.dropdown.innerHTML += `<div style="padding:6px 12px;color:var(--text-dim);font-size:11px;text-align:center">
        ${this.filtered.length - 100} more — refine your search</div>`;
    }

    if (!this.filtered.length) {
      this.dropdown.innerHTML = '<div style="padding:8px 12px;color:var(--text-dim);font-size:12px">No models match</div>';
    }

    // Click handlers
    this.dropdown.querySelectorAll('.model-opt').forEach(el => {
      el.addEventListener('click', () => {
        const idx = parseInt(el.dataset.idx);
        this._select(this.filtered[idx]);
      });
    });
  }
}

// ── Provider Fields Rendering ──
function renderProviderFields(provider, containerId, credentials, providerFields) {
  const el = document.getElementById(containerId);
  const info = providers[provider] || {};
  const creds = (credentials || {})[provider] || {};

  if (info.fields) {
    // Multi-field provider
    el.innerHTML = info.fields.map(f => {
      const fieldCred = (creds.fields || {})[f.key] || {};
      const statusHtml = fieldCred.configured
        ? `<div class="key-status configured">✓ Configured (${fieldCred.masked}) — ${fieldCred.source}</div>`
        : `<div class="key-status missing">Not configured</div>`;
      const inputType = f.secret ? 'password' : 'text';
      const placeholder = fieldCred.configured
        ? 'Leave empty to keep current'
        : (f.placeholder || '');
      // Pre-fill non-secret fields from providerFields
      const prefill = !f.secret && providerFields && providerFields[provider]
        ? (providerFields[provider][f.key] || '') : '';
      return `<div class="field">
        <label>${f.label}</label>
        <input type="${inputType}" data-field-key="${f.key}" placeholder="${placeholder}" value="${prefill}" autocomplete="off">
        ${statusHtml}
      </div>`;
    }).join('');
    return;
  }

  // Single API key provider
  const keySection = document.getElementById(containerId.replace('provider-fields', 'key-section'));
  if (keySection) {
    const isOauth = info.is_oauth;
    const label = isOauth ? 'OAuth Token' : 'API Key';
    const placeholder = creds.configured
      ? 'Leave empty to keep current'
      : (isOauth ? 'sk-ant-oat01-...' : 'sk-...');
    const statusHtml = creds.configured
      ? `<div class="key-status configured">✓ Configured (${creds.masked}) — from ${creds.source}</div>`
      : `<div class="key-status missing">Not configured</div>`;
    const hint = isOauth
      ? '<p style="font-size:11px;color:var(--text-dim);margin-top:4px">From your Claude Max/Pro subscription. Same token Claude Code uses.</p>'
      : '';

    keySection.innerHTML = `<div class="field">
      <label>${label}</label>
      <input type="password" id="${containerId.replace('provider-fields','key')}" placeholder="${placeholder}" autocomplete="off">
      ${statusHtml}
      ${hint}
    </div>`;
  }
  el.innerHTML = '';
}

// ── Init ──
async function init() {
  const pres = await fetch('/api/providers');
  providers = await pres.json();

  const cres = await fetch('/api/config');
  currentCfg = await cres.json();

  if (!currentCfg.configured) {
    await initSetupPage(currentCfg);
    showPage('setup');
  } else {
    connect();
    loadVersionInfo(currentCfg);
    loadSessions();
  }
}

function loadVersionInfo(cfg) {
  document.getElementById('version-info').textContent =
    `${providers[cfg.provider]?.name || cfg.provider} · ${(cfg.model || '').split('/').pop() || ''}`;
}

// ── Sessions ──
async function loadSessions() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    const el = document.getElementById('sessions-list');
    const sessions = data.sessions || [];
    if (!sessions.length) {
      el.innerHTML = '<div style="padding:4px 16px;font-size:11px;color:var(--text-dim)">No saved chats</div>';
      return;
    }
    el.innerHTML = sessions.slice(0, 10).map(s => {
      const title = s.title || 'Untitled';
      const active = _get_agent_session_id() === s.id ? ' active' : '';
      return `<div class="session-item${active}" onclick="loadSession('${s.id}')" title="${title}">${title}</div>`;
    }).join('');
  } catch(e) {}
}

function _get_agent_session_id() {
  return window._currentSessionId || '';
}

async function loadSession(id) {
  const res = await fetch('/api/sessions/load', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ session_id: id })
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }

  window._currentSessionId = id;

  // Reload chat history
  const hres = await fetch('/api/history');
  const hdata = await hres.json();
  const chatEl = document.getElementById('chat-messages');
  chatEl.innerHTML = '';
  (hdata.messages || []).forEach(m => {
    if (m.role === 'user' && m.content) addMsg(m.content, 'user');
    else if (m.role === 'assistant' && m.content) addMsg(m.content, 'agent');
  });

  updateTokenBar(data.token_usage);
  loadSessions();
  showPage('chat');
}

// ── Token Usage ──
function updateTokenBar(usage) {
  if (!usage) return;
  const wrap = document.getElementById('token-bar-wrap');
  const fill = document.getElementById('token-fill');
  const label = document.getElementById('token-label');
  const pct = document.getElementById('token-pct');

  wrap.style.display = 'block';
  const pctVal = Math.min(usage.usage_percent || 0, 100);
  fill.style.width = pctVal + '%';
  fill.className = 'fill ' + (usage.critical ? 'crit' : usage.warning ? 'warn' : 'ok');
  label.textContent = `${(usage.total_tokens || 0).toLocaleString()} tokens`;
  pct.textContent = pctVal + '%';

  // Show warning banner
  const warn = document.getElementById('context-warning');
  const warnText = document.getElementById('context-warning-text');
  if (usage.critical) {
    warn.style.display = 'block';
    warn.className = 'context-warning critical';
    warnText.textContent = `Context ${pctVal}% full (${(usage.total_tokens||0).toLocaleString()} / ${(usage.context_limit||0).toLocaleString()}). Start a new chat for best results.`;
  } else if (usage.warning) {
    warn.style.display = 'block';
    warn.className = 'context-warning';
    warnText.textContent = `Context ${pctVal}% full. Consider starting a new chat soon.`;
  } else {
    warn.style.display = 'none';
  }
}

// ── Setup Wizard ──
let wizardStep = 1;
function wizardNext(step) {
  // Validation per step
  if (step === 1) {
    const p = document.getElementById('setup-provider').value;
    if (!p) { showSetupError('Please select a provider.'); return; }
  }
  clearSetupError();
  wizardStep = step + 1;
  renderWizard();
}
function wizardBack(step) {
  wizardStep = step - 1;
  renderWizard();
}
function renderWizard() {
  for (let i = 1; i <= 5; i++) {
    const sec = document.getElementById('wizard-' + i);
    sec.classList.toggle('active', i === wizardStep);
    const stepEl = document.querySelector(`.wizard-step[data-step="${i}"]`);
    stepEl.className = 'wizard-step' + (i < wizardStep ? ' done' : i === wizardStep ? ' current' : '');
  }
}
function showSetupError(msg) {
  const el = document.getElementById('setup-error');
  el.textContent = msg; el.style.display = 'block';
}
function clearSetupError() {
  document.getElementById('setup-error').style.display = 'none';
}

// Tool-use filter for setup
function onSetupToolFilterChange() {
  const p = document.getElementById('setup-provider').value;
  if (p) reloadSetupModels(p);
}
async function reloadSetupModels(provider) {
  const toolOnly = document.getElementById('setup-tool-filter')?.checked || false;
  const info = providers[provider] || {};
  setupModelPicker.setLoading();
  try {
    let url = '/api/models/' + provider;
    const params = [];
    if (toolOnly) params.push('tool_use_only=true');
    if (params.length) url += '?' + params.join('&');
    const res = await fetch(url);
    const data = await res.json();
    setupModelPicker.setModels(data.models || [], info.default_model);
  } catch(e) {
    setupModelPicker.setModels([], null);
  }
}

// ── Research ──
async function startResearch() {
  const query = document.getElementById('research-query').value.trim();
  if (!query) return;

  const btn = document.getElementById('research-start');
  btn.disabled = true;
  btn.textContent = 'Researching...';

  document.getElementById('research-progress').style.display = 'block';
  document.getElementById('research-result').style.display = 'none';
  document.getElementById('research-steps').innerHTML =
    '<div class="step active">⏳ Starting research pipeline...</div>';

  try {
    await fetch('/api/research', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query })
    });
    // Results come via WebSocket
  } catch(e) {
    document.getElementById('research-steps').innerHTML +=
      `<div class="step" style="color:var(--danger)">❌ Error: ${e}</div>`;
    btn.disabled = false;
    btn.textContent = 'Start Research';
  }
}

// ── Navigation ──
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#sidebar nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  const navEl = document.querySelector(`[data-page="${name}"]`);
  if (navEl) navEl.classList.add('active');
  if (name === 'profiles') loadProfiles();
  if (name === 'jobs') loadJobs();
  if (name === 'settings') loadSettingsPage();
  if (name === 'setup') initSetupPage();
  if (name === 'chat') loadSessions();
}

// ── Setup Page ──
let setupModelPicker;
async function initSetupPage(cfg) {
  cfg = cfg || currentCfg;
  const sel = document.getElementById('setup-provider');
  sel.innerHTML = '<option value="">— Select —</option>' +
    Object.entries(providers).map(([k,v]) => {
      const tag = v.is_oauth ? ' ⭐ no API cost' : '';
      return `<option value="${k}">${v.name}${tag}</option>`;
    }).join('');

  // Init model picker
  if (!setupModelPicker) {
    setupModelPicker = new ModelPicker('setup-model-picker');
  }

  const wsInput = document.getElementById('setup-workspace');
  if (!wsInput.value) wsInput.value = cfg?.workspace || '~/browser-py-workspace';

  await loadSetupProfiles();

  if (cfg?.provider) {
    sel.value = cfg.provider;
    await onSetupProviderChange();
    if (cfg.model) {
      const custom = document.getElementById('setup-model-custom');
      setupModelPicker.setModels(setupModelPicker.allModels, cfg.model);
      if (!setupModelPicker.allModels.find(m => m.id === cfg.model)) {
        custom.value = cfg.model;
      }
    }
  }
}

async function loadSetupProfiles() {
  const res = await fetch('/api/profiles');
  const data = await res.json();
  const sel = document.getElementById('setup-browser-profile');
  const profiles = data.profiles || [];
  if (!profiles.length) {
    sel.innerHTML = '<option value="default">default (will be created)</option>';
  } else {
    sel.innerHTML = profiles.map(p =>
      `<option value="${p.name}">${p.name} (port ${p.port})</option>`
    ).join('');
  }
}

async function onSetupProviderChange() {
  const p = document.getElementById('setup-provider').value;
  const info = providers[p] || {};
  const note = document.getElementById('setup-provider-note');

  // Show note
  if (info.note) { note.textContent = info.note; note.style.display = 'block'; }
  else { note.style.display = 'none'; }

  // Render credential fields
  renderProviderFields(p, 'setup-provider-fields', currentCfg.credentials, currentCfg.provider_fields);

  // For single-key providers, render key section
  if (!info.fields) {
    const keySection = document.getElementById('setup-key-section');
    const isOauth = info.is_oauth;
    const creds = (currentCfg.credentials || {})[p] || {};
    const label = isOauth ? 'OAuth Token' : 'API Key';
    const placeholder = creds.configured ? 'Leave empty to keep current' : (isOauth ? 'sk-ant-oat01-...' : 'sk-...');
    const statusHtml = creds.configured
      ? `<div class="key-status configured">✓ Configured (${creds.masked}) — from ${creds.source}</div>`
      : '';
    const hint = isOauth
      ? '<p style="font-size:11px;color:var(--text-dim);margin-top:4px">From your Claude Max/Pro subscription.</p>'
      : '';
    keySection.innerHTML = `<div class="field">
      <label>${label}</label>
      <input type="password" id="setup-key" placeholder="${placeholder}" autocomplete="off">
      ${statusHtml}${hint}
    </div>`;
  } else {
    document.getElementById('setup-key-section').innerHTML = '';
  }

  // Fetch models (with tool-use filter if checked)
  await reloadSetupModels(p);
}

async function loadModelsForPicker(provider, picker, defaultModel, apiKey) {
  picker.setLoading();
  try {
    let url = '/api/models/' + provider;
    if (apiKey) url += '?api_key=' + encodeURIComponent(apiKey);
    const res = await fetch(url);
    const data = await res.json();
    picker.setModels(data.models || [], defaultModel);
  } catch(e) {
    picker.setModels([], null);
  }
}

// Debounced key input → refresh models for setup
document.addEventListener('input', function(e) {
  if (e.target.id !== 'setup-key') return;
  clearTimeout(window._setupKeyTimer);
  window._setupKeyTimer = setTimeout(async () => {
    const p = document.getElementById('setup-provider').value;
    const key = e.target.value.trim();
    if (key.length > 10 && p) {
      await loadModelsForPicker(p, setupModelPicker, providers[p]?.default_model, key);
    }
  }, 800);
});

async function setupCreateProfile() {
  const nameInput = document.getElementById('setup-new-profile');
  const name = nameInput.value.trim();
  if (!name) return;
  const res = await fetch('/api/profiles', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  nameInput.value = '';
  await loadSetupProfiles();
  document.getElementById('setup-browser-profile').value = name;
}

async function submitSetup() {
  const btn = document.getElementById('setup-submit');
  const errEl = document.getElementById('setup-error');
  errEl.style.display = 'none';

  const provider = document.getElementById('setup-provider').value;
  if (!provider) { errEl.textContent = 'Please select a provider.'; errEl.style.display = 'block'; return; }

  const info = providers[provider] || {};
  const modelCustom = document.getElementById('setup-model-custom').value.trim();
  const model = modelCustom || setupModelPicker.getValue();
  const workspace = document.getElementById('setup-workspace').value.trim() || '~/browser-py-workspace';
  const browser_profile = document.getElementById('setup-browser-profile').value || 'default';
  const shell_enabled = document.getElementById('setup-shell').checked;

  const body = { provider, model, workspace, browser_profile, shell_enabled };

  // Collect credentials
  if (info.fields) {
    const fieldInputs = document.querySelectorAll('#setup-provider-fields [data-field-key]');
    fieldInputs.forEach(inp => {
      const val = inp.value.trim();
      if (val) body[inp.dataset.fieldKey] = val;
    });
  } else {
    const keyEl = document.getElementById('setup-key');
    if (keyEl && keyEl.value.trim()) body.api_key = keyEl.value.trim();
  }

  // Check if any credentials exist (from config or new input)
  const creds = (currentCfg.credentials || {})[provider] || {};
  const hasExistingCreds = creds.configured;
  const hasNewKey = body.api_key || Object.keys(body).some(k => info.fields?.some(f => f.key === k));
  if (!hasExistingCreds && !hasNewKey && !['bedrock','vertex'].includes(provider)) {
    errEl.textContent = 'API key is required.';
    errEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Saving...';

  try {
    const res = await fetch('/api/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (data.error) {
      errEl.textContent = data.error; errEl.style.display = 'block';
      btn.disabled = false; btn.textContent = 'Save & Start'; return;
    }
    connect();
    const cres2 = await fetch('/api/config');
    currentCfg = await cres2.json();
    loadVersionInfo(currentCfg);
    loadSessions();
    showPage('chat');
    addMsg('Setup complete! How can I help?', 'agent');
  } catch(e) {
    errEl.textContent = 'Setup failed: ' + e; errEl.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = 'Save & Start';
}

// ── WebSocket ──
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => {
    statusEl.textContent = 'Connected';
    // Fetch current token state on connect
    fetch('/api/tokens').then(r => r.json()).then(u => {
      if (u.total_tokens > 0) updateTokenBar(u);
    }).catch(() => {});
  };
  ws.onclose = () => { statusEl.textContent = 'Disconnected'; setTimeout(connect, 2000); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'thinking') {
      removeThinking();
      addMsg('Thinking...', 'agent thinking');
    } else if (msg.type === 'tool_call') {
      removeThinking();
      const action = msg.params?.action || '';
      const detail = action ? ` \\u2192 ${action}` : '';
      let text = `\\ud83d\\udd27 ${msg.tool}${detail}`;
      if (msg.result) text += '\\n' + msg.result.slice(0, 500);
      addMsg(text, 'tool');
    } else if (msg.type === 'response') {
      removeThinking();
      addMsg(msg.content, 'agent');
      sendBtn.disabled = false;
      inputEl.focus();
      if (msg.token_usage) updateTokenBar(msg.token_usage);
      if (msg.session_id) window._currentSessionId = msg.session_id;
      loadSessions();
    } else if (msg.type === 'token_update') {
      updateTokenBar(msg);
    } else if (msg.type === 'context_warning') {
      updateTokenBar(msg.usage);
    } else if (msg.type === 'reset_ok') {
      chatEl.innerHTML = '';
      addMsg('Chat cleared. How can I help?', 'agent');
      window._currentSessionId = null;
      document.getElementById('token-bar-wrap').style.display = 'none';
      document.getElementById('context-warning').style.display = 'none';
      loadSessions();
    } else if (msg.type === 'research_progress') {
      const steps = document.getElementById('research-steps');
      // Mark previous steps as done
      steps.querySelectorAll('.step.active').forEach(s => {
        s.classList.remove('active');
        s.classList.add('done');
        s.innerHTML = s.innerHTML.replace('⏳', '✅');
      });
      steps.innerHTML += `<div class="step active">⏳ ${msg.message}</div>`;
    } else if (msg.type === 'research_complete') {
      const steps = document.getElementById('research-steps');
      steps.querySelectorAll('.step.active').forEach(s => {
        s.classList.remove('active');
        s.classList.add('done');
        s.innerHTML = s.innerHTML.replace('⏳', '✅');
      });
      steps.innerHTML += '<div class="step done">✅ Research complete!</div>';
      document.getElementById('research-result').style.display = 'block';
      document.getElementById('research-meta').textContent =
        `Completed in ${Math.round(msg.duration)}s · ${(msg.subtopics||[]).length} subtopics researched`;
      document.getElementById('research-report-content').textContent = msg.report || '';
      const btn = document.getElementById('research-start');
      btn.disabled = false;
      btn.textContent = 'Start Research';
    } else if (msg.type === 'research_error') {
      const steps = document.getElementById('research-steps');
      steps.innerHTML += `<div class="step" style="color:var(--danger)">❌ Error: ${msg.error}</div>`;
      const btn = document.getElementById('research-start');
      btn.disabled = false;
      btn.textContent = 'Start Research';
    }
  };
}

function addMsg(text, cls) {
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  if (cls === 'tool') {
    const parts = text.split('\\n');
    const nameSpan = document.createElement('span');
    nameSpan.className = 'tool-name';
    nameSpan.textContent = parts[0];
    div.appendChild(nameSpan);
    if (parts.length > 1) div.appendChild(document.createTextNode('\\n' + parts.slice(1).join('\\n')));
  } else { div.textContent = text; }
  chatEl.appendChild(div);
  chatEl.scrollTop = chatEl.scrollHeight;
}

function removeThinking() {
  const t = chatEl.querySelector('.thinking');
  if (t) t.remove();
}

function send() {
  const text = inputEl.value.trim();
  if (!text || sendBtn.disabled) return;
  addMsg(text, 'user');
  ws.send(JSON.stringify({ type: 'chat', message: text }));
  inputEl.value = '';
  inputEl.style.height = 'auto';
  sendBtn.disabled = true;
}

function _probeText(data) {
  let text = '🔍 Agent status: ' + (data.state || 'idle');
  if (data.tool) text += ' — ' + data.tool + '(' + JSON.stringify(data.params || {}).slice(0, 100) + ')';
  if (data.iteration) text += ' [iteration ' + data.iteration + ']';
  if (data.elapsed_seconds) text += ' (' + data.elapsed_seconds + 's ago)';
  if (data.token_usage) text += '\\nTokens: ' + (data.token_usage.total_tokens || 0).toLocaleString() +
    ' / ' + (data.token_usage.context_limit || 0).toLocaleString() +
    ' (' + (data.token_usage.usage_percent || 0) + '%)';
  return text;
}

function _showOnActivePage(text, cls) {
  const researchPage = document.getElementById('page-research');
  if (researchPage.classList.contains('active')) {
    const el = document.getElementById('research-probe');
    el.style.display = 'block';
    el.textContent = text;
  } else {
    addMsg(text, cls || 'tool');
  }
}

async function probeAgent() {
  try {
    const res = await fetch('/api/probe');
    const data = await res.json();
    _showOnActivePage(_probeText(data), 'tool');
  } catch(e) { _showOnActivePage('Probe failed: ' + e, 'tool'); }
}

async function flushAgent() {
  if (!confirm('Stop the agent and dump context? The current task will be aborted.')) return;
  try {
    const res = await fetch('/api/flush', { method: 'POST' });
    const data = await res.json();
    const text = '⏹ ' + (data.message || 'Flush requested');
    _showOnActivePage(text, 'tool');
    sendBtn.disabled = false;
    // Re-enable research button
    const rbtn = document.getElementById('research-start');
    if (rbtn) { rbtn.disabled = false; rbtn.textContent = 'Start Research'; }
  } catch(e) { _showOnActivePage('Flush failed: ' + e, 'tool'); }
}

async function resetChat() {
  // Save current session first
  if (window._currentSessionId) {
    await fetch('/api/sessions/save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
  }
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'reset' }));
}

inputEl.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// ── Profiles ──
async function loadProfiles() {
  const res = await fetch('/api/profiles/status');
  const data = await res.json();
  const el = document.getElementById('profiles-list');
  if (!data.profiles?.length) { el.innerHTML = '<div class="empty">No profiles yet.</div>'; return; }
  el.innerHTML = data.profiles.map(p => `
    <div class="list-item">
      <span class="name">${p.name}</span>
      <span class="meta">port ${p.port}</span>
      ${p.is_default ? '<span class="badge active">default</span>' : ''}
      ${p.running
        ? '<span class="badge active">running</span>'
        : `<button class="btn" style="padding:4px 12px;font-size:12px" onclick="launchProfile('${p.name}')">Launch</button>`
      }
    </div>
  `).join('');
}

async function launchProfile(name) {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = 'Starting...';
  try {
    const res = await fetch('/api/profiles/launch', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name })
    });
    const data = await res.json();
    if (data.error) { alert(data.error); return; }
    loadProfiles();
  } catch(e) { alert('Failed to launch: ' + e); }
}

async function createProfile() {
  const name = document.getElementById('new-profile-name').value.trim();
  if (!name) return;
  const res = await fetch('/api/profiles', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ name })
  });
  const data = await res.json();
  if (data.error) { alert(data.error); return; }
  document.getElementById('new-profile-name').value = '';
  loadProfiles();
}

// ── Jobs ──
async function loadJobs() {
  const res = await fetch('/api/jobs');
  const data = await res.json();
  const el = document.getElementById('jobs-list');
  const jobs = Object.values(data.jobs || {});
  if (!jobs.length) { el.innerHTML = '<div class="empty">No scheduled jobs</div>'; return; }
  el.innerHTML = jobs.map(j => {
    const sched = j.cron || (j.interval_minutes ? `every ${j.interval_minutes}m` : j.run_at || '?');
    const badge = j.paused ? '<span class="badge paused">paused</span>' : '<span class="badge active">active</span>';
    return `<div class="list-item">
      <span class="name">${j.name}</span>
      <span class="meta">${sched}</span>
      ${badge}
    </div>`;
  }).join('');
}

// ── Settings Page ──
let cfgModelPicker;
async function loadSettingsPage() {
  const cres = await fetch('/api/config');
  currentCfg = await cres.json();
  const cfg = currentCfg;

  // Provider dropdown
  const provSel = document.getElementById('cfg-provider');
  provSel.innerHTML = Object.entries(providers).map(([k,v]) =>
    `<option value="${k}" ${k === cfg.provider ? 'selected' : ''}>${v.name}</option>`
  ).join('');

  // Init model picker
  if (!cfgModelPicker) {
    cfgModelPicker = new ModelPicker('cfg-model-picker');
  }

  await onCfgProviderChange();

  // Set model value
  if (cfg.model) {
    const found = cfgModelPicker.allModels.find(m => m.id === cfg.model);
    if (found) {
      cfgModelPicker.setModels(cfgModelPicker.allModels, cfg.model);
      document.getElementById('cfg-model').value = '';
    } else {
      document.getElementById('cfg-model').value = cfg.model || '';
    }
  }

  // Workspace
  document.getElementById('cfg-workspace').value = cfg.workspace || '';
  // Shell
  document.getElementById('cfg-shell').checked = cfg.shell_enabled !== false;
  document.getElementById('cfg-timeout').value = cfg.timeout || 300;

  // Profiles dropdown
  const pres = await fetch('/api/profiles');
  const pdata = await pres.json();
  const psel = document.getElementById('cfg-profile');
  psel.innerHTML = (pdata.profiles || []).map(p =>
    `<option value="${p.name}" ${p.name === cfg.browser_profile ? 'selected' : ''}>${p.name} (port ${p.port})</option>`
  ).join('');
}

async function onCfgProviderChange() {
  const p = document.getElementById('cfg-provider').value;
  const info = providers[p] || {};

  // Show credential status
  const statusEl = document.getElementById('cfg-credentials-status');
  const creds = (currentCfg.credentials || {})[p] || {};
  if (info.fields) {
    // Multi-field: show per-field status
    document.getElementById('cfg-key-section').innerHTML = '';
    renderProviderFields(p, 'cfg-provider-fields', currentCfg.credentials, currentCfg.provider_fields);
    statusEl.innerHTML = '';
    if (info.note) statusEl.innerHTML = `<p style="font-size:12px;color:var(--text-dim);margin:8px 0">${info.note}</p>`;
  } else {
    // Single key
    document.getElementById('cfg-provider-fields').innerHTML = '';
    const isOauth = info.is_oauth;
    const label = isOauth ? 'OAuth Token' : 'API Key';
    const placeholder = creds.configured ? 'Leave empty to keep current' : (isOauth ? 'sk-ant-oat01-...' : 'sk-...');
    const credStatus = creds.configured
      ? `<div class="key-status configured">✓ Configured (${creds.masked}) — from ${creds.source}</div>`
      : `<div class="key-status missing">Not configured</div>`;
    document.getElementById('cfg-key-section').innerHTML = `<div class="field">
      <label>${label}</label>
      <input type="password" id="cfg-key" placeholder="${placeholder}" autocomplete="off">
      ${credStatus}
    </div>`;
    statusEl.innerHTML = info.note ? `<p style="font-size:12px;color:var(--text-dim);margin:8px 0">${info.note}</p>` : '';
  }

  // Fetch models
  if (!cfgModelPicker) cfgModelPicker = new ModelPicker('cfg-model-picker');
  await loadModelsForPicker(p, cfgModelPicker, info.default_model);
}

async function saveSettings() {
  const provider = document.getElementById('cfg-provider').value;
  const info = providers[provider] || {};
  const modelCustom = document.getElementById('cfg-model').value.trim();
  const model = modelCustom || cfgModelPicker.getValue();
  const workspace = document.getElementById('cfg-workspace').value.trim();
  const shell_enabled = document.getElementById('cfg-shell').checked;
  const browser_profile = document.getElementById('cfg-profile').value;
  const timeout = parseInt(document.getElementById('cfg-timeout').value) || 300;

  const body = { provider, model, workspace, browser_profile, shell_enabled, timeout };

  // Collect credentials
  if (info.fields) {
    const fieldInputs = document.querySelectorAll('#cfg-provider-fields [data-field-key]');
    fieldInputs.forEach(inp => {
      const val = inp.value.trim();
      if (val) body[inp.dataset.fieldKey] = val;
    });
  } else {
    const keyEl = document.getElementById('cfg-key');
    if (keyEl && keyEl.value.trim()) body.api_key = keyEl.value.trim();
  }

  await fetch('/api/setup', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });

  const savedEl = document.getElementById('cfg-saved');
  savedEl.style.display = 'block';
  setTimeout(() => savedEl.style.display = 'none', 3000);

  const cres = await fetch('/api/config');
  currentCfg = await cres.json();
  loadVersionInfo(currentCfg);
}

init();
</script>
</body>
</html>
"""
