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
    """Get agent configuration (no secrets)."""
    cfg = get_agent_config()
    safe = {
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "workspace": cfg.get("workspace"),
        "browser_profile": cfg.get("browser_profile"),
        "shell_enabled": cfg.get("shell_enabled", True),
        "configured": is_configured(),
    }
    return JSONResponse(safe)


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

    allowed = {"model", "shell_enabled", "browser_profile"}
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
        result[key] = {
            "name": info["name"],
            "default_model": info["default_model"],
            "note": info.get("note", ""),
            "is_oauth": info.get("is_oauth", False),
        }
    return JSONResponse(result)


@app.get("/api/models/{provider}")
async def get_models(provider: str, api_key: str | None = None) -> JSONResponse:
    """Fetch available models for a provider (live from API, cached 10min)."""
    from browser_py.agent.models import fetch_models
    import asyncio

    loop = asyncio.get_event_loop()
    models = await loop.run_in_executor(None, fetch_models, provider, api_key)
    return JSONResponse({"models": models})


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

    # Store API key
    if api_key:
        providers_cfg = agent_cfg.get("providers", {})
        providers_cfg.setdefault(provider, {})["api_key"] = api_key
        agent_cfg["providers"] = providers_cfg

    # Azure extra fields
    if provider == "azure":
        if body.get("azure_endpoint"):
            agent_cfg.setdefault("providers", {}).setdefault("azure", {})["base_url"] = body["azure_endpoint"]
        if body.get("azure_api_version"):
            agent_cfg.setdefault("providers", {}).setdefault("azure", {})["api_version"] = body["azure_api_version"]

    if model:
        agent_cfg["model"] = model
    if workspace:
        from pathlib import Path
        ws = Path(workspace).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)
        agent_cfg["workspace"] = str(ws)

    agent_cfg["shell_enabled"] = shell_enabled

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

                await ws.send_text(json.dumps({"type": "thinking"}))

                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None, agent.chat, msg.get("message", "")
                )

                await ws.send_text(json.dumps({
                    "type": "response",
                    "content": result,
                }))

            elif msg.get("type") == "reset":
                agent = _get_agent()
                agent.reset()
                await ws.send_text(json.dumps({"type": "reset_ok"}))

    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


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
  #sidebar nav { flex: 1; padding: 8px 0; }
  #sidebar nav a { display: flex; align-items: center; gap: 8px; padding: 8px 16px;
    color: var(--text-dim); text-decoration: none; font-size: 13px; cursor: pointer;
    border-left: 3px solid transparent; }
  #sidebar nav a:hover { color: var(--text); background: rgba(255,255,255,0.04); }
  #sidebar nav a.active { color: var(--accent); border-left-color: var(--accent);
    background: rgba(88,166,255,0.08); }
  #sidebar .version { padding: 12px 16px; font-size: 11px; color: var(--text-dim);
    border-top: 1px solid var(--border); }

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
</style>
</head>
<body>

<div id="sidebar">
  <div class="logo">🌐 browser-py</div>
  <nav>
    <a class="active" data-page="chat" onclick="showPage('chat')">💬 Chat</a>
    <a data-page="profiles" onclick="showPage('profiles')">🌍 Browser Profiles</a>
    <a data-page="jobs" onclick="showPage('jobs')">⏰ Scheduled Jobs</a>
    <a data-page="settings" onclick="showPage('settings')">⚙️ Settings</a>
  </nav>
  <div class="version" id="version-info">browser-py</div>
</div>

<div id="main">
  <!-- Setup Page (shown when not configured) -->
  <div class="page" id="page-setup">
    <header><h2>🔧 Setup</h2></header>
    <div class="page-content">
      <div class="card">
        <h3>Welcome to browser-py</h3>
        <p>Let's configure your AI agent. This takes about 30 seconds.</p>
      </div>

      <div class="card">
        <h3>1. LLM Provider</h3>
        <div class="field">
          <label>Provider</label>
          <select id="setup-provider" onchange="onProviderChange()">
            <option value="">— Select —</option>
          </select>
        </div>
        <div id="setup-provider-note" style="font-size:12px;color:var(--text-dim);margin-bottom:12px;display:none"></div>
        <div class="field" id="setup-key-field">
          <label id="setup-key-label">API Key</label>
          <input type="password" id="setup-key" placeholder="sk-..." autocomplete="off">
          <p id="setup-key-hint" style="font-size:11px;color:var(--text-dim);margin-top:4px"></p>
        </div>
        <div id="setup-azure-fields" style="display:none">
          <div class="field">
            <label>Azure Endpoint URL</label>
            <input type="text" id="setup-azure-endpoint" placeholder="https://your-resource.openai.azure.com">
          </div>
          <div class="field">
            <label>API Version</label>
            <input type="text" id="setup-azure-version" value="2024-02-01">
          </div>
        </div>
      </div>

      <div class="card">
        <h3>2. Model</h3>
        <div class="field">
          <label>Model</label>
          <select id="setup-model"></select>
        </div>
        <div class="field">
          <label>Or type a custom model name</label>
          <input type="text" id="setup-model-custom" placeholder="Leave empty to use selection above">
        </div>
      </div>

      <div class="card">
        <h3>3. Workspace Directory</h3>
        <p>All file operations are sandboxed to this directory.</p>
        <div class="field">
          <label>Path</label>
          <input type="text" id="setup-workspace" placeholder="~/browser-py-workspace">
        </div>
      </div>

      <div class="card">
        <h3>4. Browser Profile</h3>
        <p>The browser profile the agent uses. Each profile keeps its own logins and cookies.</p>
        <div class="field">
          <label>Profile</label>
          <select id="setup-browser-profile"></select>
        </div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <input type="text" id="setup-new-profile" placeholder="New profile name" style="background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text);font-size:13px;flex:1">
          <button class="btn secondary" onclick="setupCreateProfile()" style="white-space:nowrap">Create</button>
        </div>
      </div>

      <div class="card">
        <h3>5. Permissions</h3>
        <div class="field" style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="setup-shell" style="width:auto" checked>
          <label for="setup-shell" style="margin:0;text-transform:none">Allow shell commands</label>
        </div>
      </div>

      <button class="btn" onclick="submitSetup()" style="width:100%;padding:12px;font-size:15px" id="setup-submit">
        Save &amp; Start
      </button>
      <div id="setup-error" style="color:var(--danger);font-size:13px;margin-top:8px;display:none"></div>
    </div>
  </div>

  <!-- Chat Page -->
  <div class="page" id="page-chat">
    <header>
      <h2>Chat</h2>
      <button class="btn secondary" onclick="resetChat()" style="margin-left:auto">New Chat</button>
      <div class="status" id="status">Connecting...</div>
    </header>
    <div id="chat-messages"></div>
    <div id="input-area">
      <textarea id="input" placeholder="What should I do?" rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}"></textarea>
      <button id="send" onclick="send()">Send</button>
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
        <h3>LLM Provider</h3>
        <div class="field">
          <label>Provider</label>
          <select id="cfg-provider" onchange="onCfgProviderChange()"></select>
        </div>
        <div class="field" id="cfg-key-field">
          <label id="cfg-key-label">API Key</label>
          <input type="password" id="cfg-key" placeholder="Enter new key to change (leave empty to keep current)" autocomplete="off">
        </div>
        <div id="cfg-azure-fields" style="display:none">
          <div class="field">
            <label>Azure Endpoint URL</label>
            <input type="text" id="cfg-azure-endpoint">
          </div>
          <div class="field">
            <label>API Version</label>
            <input type="text" id="cfg-azure-version">
          </div>
        </div>
        <div class="field">
          <label>Model</label>
          <select id="cfg-model-select" onchange="document.getElementById('cfg-model').value=this.value"></select>
        </div>
        <div class="field">
          <label>Custom model (overrides dropdown)</label>
          <input type="text" id="cfg-model" placeholder="Leave empty to use dropdown">
        </div>
      </div>
      <div class="card">
        <h3>Workspace</h3>
        <div class="field">
          <label>Directory</label>
          <input type="text" id="cfg-workspace">
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
      </div>
      <button class="btn" onclick="saveSettings()" style="width:100%;padding:10px">Save Settings</button>
      <div id="cfg-saved" style="color:var(--success);font-size:13px;margin-top:8px;display:none">✓ Saved</div>
    </div>
  </div>
</div>

<script>
const chatEl = document.getElementById('chat-messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const status = document.getElementById('status');
let ws, providers = {};

// ── Init ──
async function init() {
  // Load providers
  const pres = await fetch('/api/providers');
  providers = await pres.json();

  // Check if configured
  const cres = await fetch('/api/config');
  const cfg = await cres.json();

  if (!cfg.configured) {
    await initSetupPage(cfg);
    showPage('setup');
  } else {
    connect();
    loadVersionInfo(cfg);
  }
}

function loadVersionInfo(cfg) {
  document.getElementById('version-info').textContent =
    `${providers[cfg.provider]?.name || cfg.provider} · ${cfg.model?.split('/').pop() || ''}`;
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
}

// ── Setup Page ──
async function initSetupPage(cfg) {
  const sel = document.getElementById('setup-provider');
  sel.innerHTML = '<option value="">— Select —</option>' +
    Object.entries(providers).map(([k,v]) => {
      const tag = v.is_oauth ? ' ⭐ no API cost' : '';
      return `<option value="${k}">${v.name}${tag}</option>`;
    }).join('');

  // Default workspace
  const wsInput = document.getElementById('setup-workspace');
  if (!wsInput.value) {
    wsInput.value = cfg?.workspace || '~/browser-py-workspace';
  }

  // Load existing profiles into dropdown
  await loadSetupProfiles();

  // If already configured, pre-fill
  if (cfg?.provider) {
    sel.value = cfg.provider;
    onProviderChange();
    if (cfg.model) {
      const custom = document.getElementById('setup-model-custom');
      const msel = document.getElementById('setup-model');
      if ([...msel.options].some(o => o.value === cfg.model)) {
        msel.value = cfg.model;
      } else {
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

async function onProviderChange() {
  const p = document.getElementById('setup-provider').value;
  const info = providers[p] || {};

  // Key label & hint
  const keyLabel = document.getElementById('setup-key-label');
  const keyInput = document.getElementById('setup-key');
  const keyHint = document.getElementById('setup-key-hint');
  const keyField = document.getElementById('setup-key-field');
  const note = document.getElementById('setup-provider-note');
  const azureFields = document.getElementById('setup-azure-fields');

  azureFields.style.display = p === 'azure' ? 'block' : 'none';

  if (info.is_oauth) {
    keyLabel.textContent = 'OAuth Token';
    keyInput.placeholder = 'sk-ant-oat01-...';
    keyHint.textContent = 'From your Claude Max/Pro subscription. Same token Claude Code uses.';
    keyField.style.display = '';
  } else if (p === 'bedrock' || p === 'vertex') {
    keyField.style.display = 'none';
    note.style.display = 'block';
    note.textContent = info.note;
  } else {
    keyLabel.textContent = 'API Key';
    keyInput.placeholder = 'sk-...';
    keyHint.textContent = '';
    keyField.style.display = '';
  }
  note.style.display = info.note && p !== 'bedrock' && p !== 'vertex' ? 'block' : 'none';
  if (p !== 'bedrock' && p !== 'vertex') note.textContent = info.note || '';

  // Fetch models live
  await loadModelsForProvider(p, 'setup-model', info.default_model);
}

// Debounced key input → refresh models
let _keyTimer;
document.getElementById('setup-key')?.addEventListener('input', function() {
  clearTimeout(_keyTimer);
  _keyTimer = setTimeout(async () => {
    const p = document.getElementById('setup-provider').value;
    const key = this.value.trim();
    if (key.length > 10 && p) {
      await loadModelsForProvider(p, 'setup-model', providers[p]?.default_model, key);
    }
  }, 800);
});

async function loadModelsForProvider(provider, selectId, defaultModel, apiKey) {
  const msel = document.getElementById(selectId);
  msel.innerHTML = '<option value="">Loading models...</option>';

  try {
    let url = '/api/models/' + provider;
    if (apiKey) url += '?api_key=' + encodeURIComponent(apiKey);
    const res = await fetch(url);
    const data = await res.json();
    const models = data.models || [];

    if (!models.length) {
      msel.innerHTML = '<option value="">No models found</option>';
      return;
    }

    msel.innerHTML = models.map(m => {
      const label = m.name !== m.id ? `${m.name} (${m.id})` : m.id;
      return `<option value="${m.id}">${label}</option>`;
    }).join('');

    // Select default
    if (defaultModel && [...msel.options].some(o => o.value === defaultModel)) {
      msel.value = defaultModel;
    }
  } catch(e) {
    msel.innerHTML = '<option value="">Failed to load models</option>';
  }
}

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

  const api_key = document.getElementById('setup-key')?.value || '';
  const modelCustom = document.getElementById('setup-model-custom').value.trim();
  const modelSelect = document.getElementById('setup-model').value;
  const model = modelCustom || modelSelect;
  const workspace = document.getElementById('setup-workspace').value.trim() || '~/browser-py-workspace';
  const browser_profile = document.getElementById('setup-browser-profile').value || 'default';
  const shell_enabled = document.getElementById('setup-shell').checked;

  if (!api_key && provider !== 'bedrock' && provider !== 'vertex') {
    errEl.textContent = 'API key is required.';
    errEl.style.display = 'block';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Saving...';

  const body = { provider, model, workspace, browser_profile, shell_enabled };
  if (api_key) body.api_key = api_key;
  if (provider === 'azure') {
    body.azure_endpoint = document.getElementById('setup-azure-endpoint')?.value || '';
    body.azure_api_version = document.getElementById('setup-azure-version')?.value || '';
  }

  try {
    const res = await fetch('/api/setup', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await res.json();
    if (data.error) {
      errEl.textContent = data.error;
      errEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Save & Start';
      return;
    }

    // Success — go to chat
    connect();
    const cres = await fetch('/api/config');
    const cfg = await cres.json();
    loadVersionInfo(cfg);
    showPage('chat');
    addMsg('Setup complete! How can I help?', 'agent');
  } catch(e) {
    errEl.textContent = 'Setup failed: ' + e;
    errEl.style.display = 'block';
  }
  btn.disabled = false;
  btn.textContent = 'Save & Start';
}

// ── WebSocket ──
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => { status.textContent = 'Connected'; };
  ws.onclose = () => { status.textContent = 'Disconnected'; setTimeout(connect, 2000); };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'thinking') {
      removeThinking();
      addMsg('Thinking...', 'agent thinking');
    } else if (msg.type === 'tool_call') {
      removeThinking();
      const action = msg.params?.action || '';
      const detail = action ? ` → ${action}` : '';
      let text = `🔧 ${msg.tool}${detail}`;
      if (msg.result) text += '\\n' + msg.result.slice(0, 500);
      addMsg(text, 'tool');
    } else if (msg.type === 'response') {
      removeThinking();
      addMsg(msg.content, 'agent');
      sendBtn.disabled = false;
      input.focus();
    } else if (msg.type === 'reset_ok') {
      chatEl.innerHTML = '';
      addMsg('Chat cleared. How can I help?', 'agent');
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
  const text = input.value.trim();
  if (!text || sendBtn.disabled) return;
  addMsg(text, 'user');
  ws.send(JSON.stringify({ type: 'chat', message: text }));
  input.value = '';
  input.style.height = 'auto';
  sendBtn.disabled = true;
}

function resetChat() {
  if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'reset' }));
}

input.addEventListener('input', function() {
  this.style.height = 'auto';
  this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});

// ── Profiles ──
async function loadProfiles() {
  const res = await fetch('/api/profiles/status');
  const data = await res.json();
  const el = document.getElementById('profiles-list');
  if (!data.profiles?.length) { el.innerHTML = '<div class="empty">No profiles yet. Create one below.</div>'; return; }
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
async function loadSettingsPage() {
  const cres = await fetch('/api/config');
  const cfg = await cres.json();

  // Provider dropdown
  const provSel = document.getElementById('cfg-provider');
  provSel.innerHTML = Object.entries(providers).map(([k,v]) =>
    `<option value="${k}" ${k === cfg.provider ? 'selected' : ''}>${v.name}</option>`
  ).join('');
  onCfgProviderChange();

  // Model — fetch live
  await loadModelsForProvider(cfg.provider, 'cfg-model-select', cfg.model);
  const msel = document.getElementById('cfg-model-select');
  // If current model is in the list, clear custom input; otherwise put it there
  if ([...msel.options].some(o => o.value === cfg.model)) {
    msel.value = cfg.model;
    document.getElementById('cfg-model').value = '';
  } else {
    document.getElementById('cfg-model').value = cfg.model || '';
  }

  // Workspace
  document.getElementById('cfg-workspace').value = cfg.workspace || '';

  // Shell
  document.getElementById('cfg-shell').checked = cfg.shell_enabled !== false;

  // Profiles dropdown
  const pres = await fetch('/api/profiles');
  const pdata = await pres.json();
  const psel = document.getElementById('cfg-profile');
  psel.innerHTML = (pdata.profiles || []).map(p =>
    `<option value="${p.name}" ${p.name === cfg.browser_profile ? 'selected' : ''}>${p.name} (port ${p.port})</option>`
  ).join('');

  document.getElementById('cfg-azure-fields').style.display =
    cfg.provider === 'azure' ? 'block' : 'none';
}

async function onCfgProviderChange() {
  const p = document.getElementById('cfg-provider').value;
  const info = providers[p] || {};

  // Key field label
  const keyLabel = document.getElementById('cfg-key-label');
  if (info.is_oauth) keyLabel.textContent = 'OAuth Token';
  else keyLabel.textContent = 'API Key';

  const keyField = document.getElementById('cfg-key-field');
  keyField.style.display = (p === 'bedrock' || p === 'vertex') ? 'none' : '';

  document.getElementById('cfg-azure-fields').style.display = p === 'azure' ? 'block' : 'none';

  // Fetch models live
  await loadModelsForProvider(p, 'cfg-model-select', info.default_model);
}

async function saveSettings() {
  const provider = document.getElementById('cfg-provider').value;
  const api_key = document.getElementById('cfg-key').value.trim();
  const modelCustom = document.getElementById('cfg-model').value.trim();
  const modelSelect = document.getElementById('cfg-model-select').value;
  const model = modelCustom || modelSelect;
  const workspace = document.getElementById('cfg-workspace').value.trim();
  const shell_enabled = document.getElementById('cfg-shell').checked;
  const browser_profile = document.getElementById('cfg-profile').value;

  const body = { provider, model, workspace, browser_profile, shell_enabled };
  if (api_key) body.api_key = api_key;
  if (provider === 'azure') {
    body.azure_endpoint = document.getElementById('cfg-azure-endpoint')?.value || '';
    body.azure_api_version = document.getElementById('cfg-azure-version')?.value || '';
  }

  await fetch('/api/setup', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });

  const savedEl = document.getElementById('cfg-saved');
  savedEl.style.display = 'block';
  setTimeout(() => savedEl.style.display = 'none', 3000);

  const cres = await fetch('/api/config');
  loadVersionInfo(await cres.json());
}

init();
</script>
</body>
</html>
"""
