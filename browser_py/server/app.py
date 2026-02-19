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
        cfg = get_agent_config()
        _agent = Agent(
            browser_profile=cfg.get("browser_profile"),
            on_tool_call=_on_tool_call,
            on_message=_on_message,
            on_job_trigger=_on_job_change,
        )
        # Disable shell if configured
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

    agent = _get_agent()

    # Run in thread to avoid blocking
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


@app.post("/api/config")
async def update_config(body: dict) -> JSONResponse:
    """Update agent configuration."""
    from browser_py.agent.config import load_config, save_config
    config = load_config()
    agent_cfg = config.get("agent", {})

    # Only allow updating safe fields
    allowed = {"model", "shell_enabled", "browser_profile"}
    for key in allowed:
        if key in body:
            agent_cfg[key] = body[key]

    config["agent"] = agent_cfg
    save_config(config)
    return JSONResponse({"ok": True})


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
                agent = _get_agent()
                # Send thinking indicator
                await ws.send_text(json.dumps({"type": "thinking"}))

                # Run agent in thread
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
  .page { flex: 1; display: none; flex-direction: column; overflow: hidden; }
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
  .page-content { flex: 1; overflow-y: auto; padding: 24px 32px; max-width: 700px; }
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
  <!-- Chat Page -->
  <div class="page active" id="page-chat">
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
          <input type="text" id="cfg-provider" disabled>
        </div>
        <div class="field">
          <label>Model</label>
          <input type="text" id="cfg-model">
        </div>
        <p style="font-size:11px">Change provider or API key via CLI: <code>bpy setup</code></p>
      </div>
      <div class="card">
        <h3>Workspace</h3>
        <div class="field">
          <label>Directory</label>
          <input type="text" id="cfg-workspace" disabled>
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
      <button class="btn" onclick="saveSettings()">Save Settings</button>
    </div>
  </div>
</div>

<script>
const chatEl = document.getElementById('chat-messages');
const input = document.getElementById('input');
const sendBtn = document.getElementById('send');
const status = document.getElementById('status');
let ws;

// ── Navigation ──
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#sidebar nav a').forEach(a => a.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.querySelector(`[data-page="${name}"]`).classList.add('active');
  if (name === 'profiles') loadProfiles();
  if (name === 'jobs') loadJobs();
  if (name === 'settings') loadSettings();
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
  const res = await fetch('/api/profiles');
  const data = await res.json();
  const el = document.getElementById('profiles-list');
  if (!data.profiles?.length) { el.innerHTML = '<div class="empty">No profiles yet</div>'; return; }
  el.innerHTML = data.profiles.map(p => `
    <div class="list-item">
      <span class="name">${p.name}</span>
      <span class="meta">port ${p.port}</span>
      ${p.is_default ? '<span class="badge active">default</span>' : ''}
    </div>
  `).join('');
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
  loadSettings(); // refresh profile dropdown
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

// ── Settings ──
async function loadSettings() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  document.getElementById('cfg-provider').value = cfg.provider || '';
  document.getElementById('cfg-model').value = cfg.model || '';
  document.getElementById('cfg-workspace').value = cfg.workspace || '';
  document.getElementById('cfg-shell').checked = cfg.shell_enabled !== false;
  document.getElementById('version-info').textContent = `${cfg.provider} · ${cfg.model?.split('/').pop() || ''}`;

  // Load profiles for dropdown
  const pres = await fetch('/api/profiles');
  const pdata = await pres.json();
  const sel = document.getElementById('cfg-profile');
  sel.innerHTML = (pdata.profiles || []).map(p =>
    `<option value="${p.name}" ${p.name === cfg.browser_profile ? 'selected' : ''}>${p.name} (port ${p.port})</option>`
  ).join('');
}

async function saveSettings() {
  const body = {
    model: document.getElementById('cfg-model').value,
    shell_enabled: document.getElementById('cfg-shell').checked,
    browser_profile: document.getElementById('cfg-profile').value,
  };
  await fetch('/api/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  loadSettings();
  alert('Settings saved. Restart the server for provider/model changes to take effect.');
}

connect();
loadSettings();
</script>
</body>
</html>
"""
