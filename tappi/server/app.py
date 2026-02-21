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

from tappi.agent.config import get_agent_config, get_workspace, is_configured
from tappi.agent.loop import Agent

app = FastAPI(title="tappi", docs_url=None, redoc_url=None)

# Mount static files (CSS, JS) — must be before routes so /static/* is served
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Global agent instance (per server process)
_agent: Agent | None = None
_agent_lock = threading.Lock()
_ws_clients: list[WebSocket] = []
_chat_task: asyncio.Task | None = None  # tracks the running chat task
_research_abort = threading.Event()  # shared abort signal for research
_research_agents: list[Agent] = []  # active research sub-agents (for probe)

# ── Cron run tracking ──
# Active and recent cron runs, keyed by run_id
# Each entry: {job_id, job_name, task, run_id, status, started, ended, events[], result, agent}
_cron_runs: dict[str, dict] = {}
_cron_runs_lock = threading.Lock()
_MAX_CRON_HISTORY = 50  # keep last N completed runs


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
        # Set CDP_URL if configured (connects to external browser like OpenClaw)
        cdp_url = cfg.get("cdp_url")
        if cdp_url:
            import os
            os.environ["CDP_URL"] = cdp_url
        _agent = Agent(
            browser_profile=cfg.get("browser_profile"),
            on_tool_call=_on_tool_call,
            on_message=_on_message,
            on_job_trigger=_on_job_change,
            on_token_update=_on_token_update,
            on_subtask_progress=_on_subtask_progress,
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


def _on_subtask_progress(data: dict) -> None:
    """Broadcast subtask decomposition progress to WebSocket clients."""
    msg = json.dumps({"type": "subtask_progress", **data})
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
    from tappi.agent.config import get_provider_credentials_status
    cfg = get_agent_config()
    providers_cfg = cfg.get("providers", {})

    safe = {
        "provider": cfg.get("provider"),
        "model": cfg.get("model"),
        "workspace": cfg.get("workspace"),
        "browser_profile": cfg.get("browser_profile"),
        "cdp_url": cfg.get("cdp_url", ""),
        "shell_enabled": cfg.get("shell_enabled", True),
        "decompose_enabled": cfg.get("decompose_enabled", True),
        "timeout": cfg.get("timeout", 300),
        "main_max_tokens": cfg.get("main_max_tokens", cfg.get("max_tokens", 8192)),
        "subagent_max_tokens": cfg.get("subagent_max_tokens", cfg.get("max_tokens", 4096)),
        "configured": is_configured(),
        "credentials": get_provider_credentials_status(),
    }

    # Include non-secret provider fields (base_url, api_version, region, etc.)
    from tappi.agent.config import PROVIDERS
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
    from tappi.agent.sessions import list_sessions
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
    from tappi.agent.sessions import delete_session
    if delete_session(session_id):
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "Session not found"}, status_code=404)


@app.get("/api/sessions/{session_id}/export")
async def export_session_api(session_id: str) -> JSONResponse:
    """Export a session as markdown."""
    from tappi.agent.sessions import export_session_markdown
    md = export_session_markdown(session_id)
    if md:
        return JSONResponse({"markdown": md})
    return JSONResponse({"error": "Session not found"}, status_code=404)


@app.get("/api/probe")
async def probe_agent() -> JSONResponse:
    """Probe the agent's current activity state (chat, subtask, or research)."""
    # Fall through to global chat agent — its probe() now delegates
    # to active subtask runner's sub-agent automatically.
    try:
        agent = _get_agent()
        info = agent.probe()
        info["source"] = "chat"

        # Also check research sub-agents (legacy / standalone research)
        if info.get("state") in (None, "idle", "done"):
            for ra in list(_research_agents):
                ra_info = ra.probe()
                if ra_info.get("state") and ra_info["state"] != "idle":
                    ra_info["source"] = "research"
                    return JSONResponse(ra_info)

        return JSONResponse(info)
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
    _research_agents.clear()

    def on_agent_created(agent: Agent) -> None:
        """Track research sub-agents for probe."""
        _research_agents.clear()  # only track the current one
        _research_agents.append(agent)

    def _run() -> None:
        from tappi.agent.research import run_research
        try:
            result = run_research(
                query=query,
                on_progress=on_progress,
                browser_profile=cfg.get("browser_profile"),
                num_agents=num_agents,
                abort_event=_research_abort,
                on_agent_created=on_agent_created,
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
        finally:
            _research_agents.clear()

    threading.Thread(target=_run, daemon=True).start()
    return JSONResponse({"ok": True, "message": "Research started"})


# ── Validate API key ──


@app.post("/api/credentials/check")
async def check_credentials(body: dict) -> JSONResponse:
    """Live-resolve credentials for a provider (checks boto3 chain, ADC, etc.).

    This goes beyond config + env vars — it checks the full credential chain
    including ~/.aws/credentials, SSO cache, gcloud ADC, and more.
    """
    from tappi.agent.config import resolve_provider_credentials
    import asyncio

    provider = body.get("provider", "")
    if not provider:
        return JSONResponse({"error": "provider required"}, status_code=400)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, resolve_provider_credentials, provider)
    return JSONResponse(result)


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
            from tappi.agent.models import fetch_models
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
    from tappi.agent.tools.cron import _load_jobs
    jobs = _load_jobs()
    return JSONResponse({"jobs": jobs})


@app.get("/api/jobs/runs")
async def list_job_runs(job_id: str | None = None, limit: int = 20) -> JSONResponse:
    """List recent cron job runs (active + completed)."""
    with _cron_runs_lock:
        runs = list(_cron_runs.values())
    if job_id:
        runs = [r for r in runs if r.get("job_id") == job_id]
    # Sort newest first
    runs.sort(key=lambda r: r.get("started", 0), reverse=True)
    runs = runs[:limit]
    # Strip agent reference (not serializable) and cap events
    safe_runs = []
    for r in runs:
        safe = {k: v for k, v in r.items() if k != "agent"}
        safe["events"] = safe.get("events", [])[-50:]  # last 50 events
        if safe.get("result"):
            safe["result"] = safe["result"][:5000]
        safe_runs.append(safe)
    return JSONResponse({"runs": safe_runs})


@app.get("/api/jobs/runs/{run_id}")
async def get_job_run(run_id: str) -> JSONResponse:
    """Get details of a specific cron run including events."""
    with _cron_runs_lock:
        run = _cron_runs.get(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    safe = {k: v for k, v in run.items() if k != "agent"}
    return JSONResponse(safe)


@app.get("/api/jobs/runs/{run_id}/probe")
async def probe_job_run(run_id: str) -> JSONResponse:
    """Probe a running cron job's agent."""
    with _cron_runs_lock:
        run = _cron_runs.get(run_id)
    if not run:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    agent = run.get("agent")
    if not agent:
        return JSONResponse({"state": run.get("status", "done")})
    info = agent.probe()
    info["run_id"] = run_id
    return JSONResponse(info)


@app.post("/api/jobs/trigger")
async def trigger_job(body: dict) -> JSONResponse:
    """Trigger a job to run immediately. Returns the run_id for live tracking."""
    from tappi.agent.tools.cron import _load_jobs
    job_id = body.get("job_id", "")
    if not job_id:
        return JSONResponse({"error": "job_id required"}, status_code=400)
    jobs = _load_jobs()
    job = jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)

    import uuid as _uuid
    run_id = f"{job_id}_{int(time.time())}_{_uuid.uuid4().hex[:4]}"
    task_text = job.get("task", "")
    job_name = job.get("name", task_text[:50])

    # Start in background thread
    t = threading.Thread(
        target=_run_scheduled_task,
        args=[task_text, job_id, job_name],
        daemon=True,
    )
    t.start()

    # Wait briefly for the run record to appear
    await asyncio.sleep(0.3)

    # Find the run_id (it was created inside _run_scheduled_task)
    with _cron_runs_lock:
        # Find the most recent run for this job_id
        matching = [r for r in _cron_runs.values()
                    if r.get("job_id") == job_id and r.get("status") == "running"]
        matching.sort(key=lambda r: r.get("started", 0), reverse=True)
    if matching:
        return JSONResponse({"ok": True, "run_id": matching[0]["run_id"]})
    return JSONResponse({"ok": True, "run_id": None})


@app.get("/api/profiles")
async def list_browser_profiles() -> JSONResponse:
    """List browser profiles."""
    from tappi.profiles import list_profiles
    profiles = list_profiles()
    return JSONResponse({"profiles": profiles})


@app.post("/api/profiles")
async def create_browser_profile(body: dict) -> JSONResponse:
    """Create a new browser profile."""
    from tappi.profiles import create_profile
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
    from tappi.profiles import get_profile
    from tappi.core import Browser

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


@app.post("/api/cdp/check")
async def check_cdp_connection(body: dict) -> JSONResponse:
    """Check if a CDP URL is reachable."""
    import json as _json
    from urllib.request import urlopen
    from urllib.error import URLError

    cdp_url = body.get("cdp_url", "").rstrip("/")
    if not cdp_url:
        return JSONResponse({"connected": False, "error": "No URL provided"})
    try:
        data = _json.loads(urlopen(f"{cdp_url}/json/version", timeout=3).read())
        return JSONResponse({"connected": True, "browser": data.get("Browser", "unknown")})
    except (URLError, OSError) as e:
        return JSONResponse({"connected": False, "error": str(e)})


@app.get("/api/profiles/status")
async def profile_status() -> JSONResponse:
    """Check which profiles have a running browser."""
    import json as _json
    from urllib.request import urlopen
    from urllib.error import URLError
    from tappi.profiles import list_profiles

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
    from tappi.agent.config import load_config, save_config
    config = load_config()
    agent_cfg = config.get("agent", {})

    allowed = {"model", "shell_enabled", "browser_profile", "cdp_url", "decompose_enabled",
               "timeout", "main_max_tokens", "subagent_max_tokens"}
    for key in allowed:
        if key in body:
            agent_cfg[key] = body[key]

    config["agent"] = agent_cfg
    save_config(config)

    # Live-update the running agent if applicable
    if _agent:
        if "browser_profile" in body:
            _agent._browser._default_profile = body["browser_profile"]
        if "cdp_url" in body:
            cdp_url = body["cdp_url"]
            if cdp_url:
                import os
                os.environ["CDP_URL"] = cdp_url
            else:
                import os
                os.environ.pop("CDP_URL", None)
            # Force reconnection on next tool call
            _agent._browser._browser = None
        if "decompose_enabled" in body:
            _agent._decompose_enabled = body["decompose_enabled"]

    return JSONResponse({"ok": True})


@app.get("/api/providers")
async def list_providers() -> JSONResponse:
    """List available providers with metadata (no models — use /api/models)."""
    from tappi.agent.config import PROVIDERS
    result = {}
    for key, info in PROVIDERS.items():
        entry = {
            "name": info["name"],
            "default_model": info["default_model"],
            "note": info.get("note", ""),
            "is_oauth": info.get("is_oauth", False),
            "env_key": info.get("env_key", ""),
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
    from tappi.agent.models import fetch_models
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
    from tappi.agent.config import load_config, save_config
    from tappi.profiles import get_profile, create_profile

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
    from tappi.agent.config import PROVIDERS as PROVIDER_DEFS
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
    if "decompose_enabled" in body:
        agent_cfg["decompose_enabled"] = body["decompose_enabled"]
    if body.get("timeout") is not None:
        agent_cfg["timeout"] = int(body["timeout"])
    if body.get("main_max_tokens") is not None:
        agent_cfg["main_max_tokens"] = min(int(body["main_max_tokens"]), 64000)
    if body.get("subagent_max_tokens") is not None:
        agent_cfg["subagent_max_tokens"] = min(int(body["subagent_max_tokens"]), 64000)

    # Browser profile — create if needed
    if browser_profile:
        try:
            get_profile(browser_profile)
        except ValueError:
            create_profile(browser_profile)
        agent_cfg["browser_profile"] = browser_profile

    # CDP URL override (connect to external browser)
    if "cdp_url" in body:
        agent_cfg["cdp_url"] = body["cdp_url"]

    config["agent"] = agent_cfg
    save_config(config)

    # Reset the global agent so it picks up new config
    global _agent
    _agent = None

    return JSONResponse({"ok": True, "configured": True})


# ── WebSocket for live updates ──


def _process_file_attachments(message: str, files: list[dict]) -> str:
    """Process file attachments from the web UI into the user message.

    For images: embed as [IMAGE:base64:mime] markers that loop.py parses.
    For text/code files: prepend file contents to the message.
    For PDFs: extract text and prepend.
    """
    import base64 as _b64

    parts = []
    image_markers = []

    for f in files:
        name = f.get("name", "file")
        ftype = f.get("type", "")
        data = f.get("data", "")  # base64 data (may have data: URL prefix)

        # Strip data URL prefix if present
        if data.startswith("data:"):
            # data:image/png;base64,AAAA...
            _, data = data.split(",", 1)

        if ftype.startswith("image/"):
            # Images: pass as vision marker
            image_markers.append(f"[IMAGE:{data}:{ftype}]")
        elif ftype == "application/pdf":
            # PDFs: try to extract text
            try:
                raw = _b64.b64decode(data)
                # Save to temp, extract with PDFTool
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp.write(raw)
                    tmp_path = tmp.name
                try:
                    from tappi.agent.tools.pdf import PDFTool
                    pdf = PDFTool()
                    text = pdf.execute(action="read", path=tmp_path)
                    parts.append(f"[Attached PDF: {name}]\n{text[:20000]}")
                except Exception:
                    parts.append(f"[Attached PDF: {name} — could not extract text]")
                finally:
                    import os
                    os.unlink(tmp_path)
            except Exception:
                parts.append(f"[Attached PDF: {name} — could not decode]")
        else:
            # Text files: decode and prepend
            try:
                raw = _b64.b64decode(data)
                text = raw.decode("utf-8", errors="replace")
                if len(text) > 30000:
                    text = text[:30000] + "\n... (truncated)"
                parts.append(f"[Attached file: {name}]\n```\n{text}\n```")
            except Exception:
                parts.append(f"[Attached file: {name} — could not decode]")

    result = message
    if parts:
        result = "\n\n".join(parts) + "\n\n" + result
    if image_markers:
        result = result + "\n" + "\n".join(image_markers)
    return result


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

                # Process file attachments
                user_message = msg.get("message", "")
                files = msg.get("files", [])
                if files:
                    user_message = _process_file_attachments(user_message, files)

                await ws.send_text(json.dumps({"type": "thinking"}))

                loop = asyncio.get_event_loop()
                _chat_task = asyncio.ensure_future(
                    loop.run_in_executor(None, agent.chat, user_message)
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
    job_name = job.get("name", task_text[:50])

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
                _run_scheduled_task, trigger,
                args=[task_text, jid, job_name], id=jid,
            )
    elif job.get("schedule_type") == "interval":
        minutes = job.get("interval_minutes", 60)
        _scheduler.add_job(
            _run_scheduled_task,
            IntervalTrigger(minutes=minutes),
            args=[task_text, jid, job_name],
            id=jid,
        )
    elif job.get("schedule_type") == "date":
        _scheduler.add_job(
            _run_scheduled_task,
            DateTrigger(run_date=job["run_at"]),
            args=[task_text, jid, job_name],
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
        # Execute immediately in a thread with full streaming
        task_text = job.get("task", "")
        job_name = job.get("name", task_text[:50])
        if task_text:
            import threading
            threading.Thread(
                target=_run_scheduled_task,
                args=[task_text, jid, job_name],
                daemon=True,
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

    from tappi.agent.tools.cron import _load_jobs

    _scheduler = BackgroundScheduler()
    jobs = _load_jobs()

    for jid, job in jobs.items():
        job["id"] = jid  # ensure id is set
        _add_job_to_scheduler(job)

    _scheduler.start()


def _run_scheduled_task(task: str, job_id: str = "", job_name: str = "") -> None:
    """Execute a scheduled task in a fresh agent context with full streaming."""
    import uuid as _uuid

    run_id = f"{job_id or 'manual'}_{int(time.time())}_{_uuid.uuid4().hex[:4]}"
    cfg = get_agent_config()

    run_record: dict[str, Any] = {
        "job_id": job_id,
        "job_name": job_name or task[:50],
        "task": task,
        "run_id": run_id,
        "status": "running",
        "started": time.time(),
        "ended": None,
        "events": [],  # capped event log for history
        "result": None,
        "agent": None,
    }

    with _cron_runs_lock:
        _cron_runs[run_id] = run_record
        # Prune old completed runs
        completed = [rid for rid, r in _cron_runs.items()
                     if r["status"] in ("done", "error") and rid != run_id]
        for old_rid in completed[:-_MAX_CRON_HISTORY]:
            del _cron_runs[old_rid]

    # Broadcast that a cron run started
    _broadcast(json.dumps({
        "type": "cron_run_start",
        "run_id": run_id,
        "job_id": job_id,
        "job_name": run_record["job_name"],
        "task": task,
    }))

    def _cron_tool_call(name: str, params: dict, result: str) -> None:
        event = {"type": "tool_call", "tool": name, "params": params, "result": result[:2000]}
        run_record["events"].append(event)
        # Cap events list
        if len(run_record["events"]) > 200:
            run_record["events"] = run_record["events"][-200:]
        _broadcast(json.dumps({**event, "source": "cron", "run_id": run_id}))

    def _cron_subtask_progress(data: dict) -> None:
        run_record["events"].append(data)
        if len(run_record["events"]) > 200:
            run_record["events"] = run_record["events"][-200:]
        _broadcast(json.dumps({**data, "source": "cron", "run_id": run_id}))

    def _cron_token_update(usage: dict) -> None:
        _broadcast(json.dumps({
            "type": "token_update", "source": "cron", "run_id": run_id, **usage,
        }))

    agent = Agent(
        browser_profile=cfg.get("browser_profile"),
        on_tool_call=_cron_tool_call,
        on_subtask_progress=_cron_subtask_progress,
        on_token_update=_cron_token_update,
    )
    if not cfg.get("shell_enabled", True):
        agent._shell.enabled = False
    run_record["agent"] = agent

    try:
        result = agent.chat(task)
        run_record["status"] = "done"
        run_record["result"] = result

        # Log to disk
        log_dir = get_workspace() / ".cron_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{run_id}.log"
        log_file.write_text(f"Task: {task}\n\nResult:\n{result}\n")

        _broadcast(json.dumps({
            "type": "cron_run_done",
            "run_id": run_id,
            "job_id": job_id,
            "job_name": run_record["job_name"],
            "result": result[:5000],
        }))
    except Exception as e:
        run_record["status"] = "error"
        run_record["result"] = str(e)

        log_dir = get_workspace() / ".cron_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{run_id}_error.log"
        log_file.write_text(f"Task: {task}\n\nError:\n{e}\n")

        _broadcast(json.dumps({
            "type": "cron_run_error",
            "run_id": run_id,
            "job_id": job_id,
            "error": str(e),
        }))
    finally:
        run_record["ended"] = time.time()
        run_record["agent"] = None  # release agent reference
        try:
            agent.cleanup_browser()
        except Exception:
            pass


# ── Server entry point ──


def start_server(host: str = "127.0.0.1", port: int = 8321) -> None:
    """Start the web server."""
    global _loop
    import uvicorn

    print(f"\n🌐 tappi agent running at http://{host}:{port}\n")

    _start_scheduler()

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(server.serve())


# ── Fallback HTML ──
# The UI is now served from static/index.html, static/style.css, static/app.js.
# This minimal fallback only appears if the static directory is missing.

_FALLBACK_HTML = """<!DOCTYPE html>
<html><head><title>tappi</title></head>
<body style="font-family:sans-serif;padding:40px;background:#0d1117;color:#e6edf3">
<h1>tappi</h1>
<p>Static UI files not found. Reinstall tappi: <code>pip install --upgrade tappi</code></p>
</body></html>"""
