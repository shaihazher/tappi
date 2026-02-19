"""Cron tool — schedule recurring tasks via APScheduler.

Jobs are stored in a SQLite database inside ~/.tappi/jobs.db
so they persist across restarts. Each job triggers an agent loop
that executes the task description.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cron",
        "description": (
            "Schedule recurring tasks. The agent will wake up at the scheduled "
            "time and execute the task description (e.g., 'Go to instagram.com "
            "and post a photo'). Jobs persist across restarts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "remove", "pause", "resume", "run_now"],
                    "description": (
                        "Cron action:\n"
                        "- add: Create a new scheduled job (requires 'task' and schedule params)\n"
                        "- list: List all scheduled jobs\n"
                        "- remove: Delete a job (requires 'job_id')\n"
                        "- pause: Pause a job (requires 'job_id')\n"
                        "- resume: Resume a paused job (requires 'job_id')\n"
                        "- run_now: Execute a job immediately (requires 'job_id')"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "Task description for the agent to execute when triggered",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable job name (optional, auto-generated if omitted)",
                },
                "job_id": {
                    "type": "string",
                    "description": "Job ID for remove/pause/resume/run_now",
                },
                "cron": {
                    "type": "string",
                    "description": (
                        "Cron expression: 'minute hour day_of_month month day_of_week'. "
                        "Examples: '0 9 * * *' (daily 9 AM), '*/30 * * * *' (every 30 min), "
                        "'0 9 * * 1-5' (weekdays 9 AM)"
                    ),
                },
                "interval_minutes": {
                    "type": "integer",
                    "description": "Run every N minutes (alternative to cron expression)",
                },
                "run_at": {
                    "type": "string",
                    "description": "One-shot: run at this ISO datetime (e.g., '2026-03-01T09:00:00')",
                },
                "timezone": {
                    "type": "string",
                    "description": "Timezone for cron schedule (default: system local, e.g., 'America/Chicago')",
                },
            },
            "required": ["action"],
        },
    },
}


# Job store — in-memory with SQLite persistence
_JOBS_FILE = Path.home() / ".tappi" / "jobs.json"


def _load_jobs() -> dict[str, dict]:
    """Load jobs from disk."""
    if _JOBS_FILE.exists():
        try:
            return json.loads(_JOBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_jobs(jobs: dict[str, dict]) -> None:
    """Save jobs to disk."""
    _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _JOBS_FILE.write_text(json.dumps(jobs, indent=2) + "\n")


class CronTool:
    """Cron scheduling backed by APScheduler.

    The actual scheduler is started by the server (bpy serve).
    This tool manages job definitions. The server reads them and
    schedules them with APScheduler.
    """

    def __init__(self, on_job_change: Callable | None = None) -> None:
        self._on_change = on_job_change

    def execute(self, **params: Any) -> str:
        action = params.get("action", "")
        try:
            if action == "add":
                return self._add(params)
            elif action == "list":
                return self._list()
            elif action == "remove":
                return self._remove(params)
            elif action == "pause":
                return self._set_paused(params, True)
            elif action == "resume":
                return self._set_paused(params, False)
            elif action == "run_now":
                return self._run_now(params)
            else:
                return f"Unknown action: {action}"
        except Exception as e:
            return f"Error: {e}"

    def _add(self, params: dict) -> str:
        task = params.get("task", "")
        if not task:
            return "Error: 'task' description required."

        cron_expr = params.get("cron")
        interval = params.get("interval_minutes")
        run_at = params.get("run_at")

        if not cron_expr and not interval and not run_at:
            return "Error: Provide 'cron', 'interval_minutes', or 'run_at' for scheduling."

        job_id = str(uuid.uuid4())[:8]
        name = params.get("name") or task[:50]

        job = {
            "id": job_id,
            "name": name,
            "task": task,
            "paused": False,
            "created": datetime.now().isoformat(),
        }

        if cron_expr:
            job["schedule_type"] = "cron"
            job["cron"] = cron_expr
            job["timezone"] = params.get("timezone", "")
        elif interval:
            job["schedule_type"] = "interval"
            job["interval_minutes"] = interval
        elif run_at:
            job["schedule_type"] = "date"
            job["run_at"] = run_at

        jobs = _load_jobs()
        jobs[job_id] = job
        _save_jobs(jobs)

        if self._on_change:
            self._on_change("add", job)

        if cron_expr:
            schedule_desc = cron_expr
        elif interval:
            schedule_desc = f"every {interval}m"
        else:
            schedule_desc = f"at {run_at}"
        return f"Job created: {job_id}\nName: {name}\nSchedule: {schedule_desc}\nTask: {task}"

    def _list(self) -> str:
        jobs = _load_jobs()
        if not jobs:
            return "No scheduled jobs."

        lines = []
        for jid, job in jobs.items():
            status = "⏸ paused" if job.get("paused") else "▶ active"
            sched = job.get("cron") or f"every {job.get('interval_minutes')}m" or job.get("run_at", "?")
            lines.append(f"  [{jid}] {job['name']} — {sched} ({status})")

        return "Scheduled jobs:\n" + "\n".join(lines)

    def _remove(self, params: dict) -> str:
        job_id = params.get("job_id", "")
        if not job_id:
            return "Error: 'job_id' required."

        jobs = _load_jobs()
        if job_id not in jobs:
            return f"Job not found: {job_id}"

        name = jobs[job_id]["name"]
        del jobs[job_id]
        _save_jobs(jobs)

        if self._on_change:
            self._on_change("remove", {"id": job_id})

        return f"Removed job: {job_id} ({name})"

    def _set_paused(self, params: dict, paused: bool) -> str:
        job_id = params.get("job_id", "")
        if not job_id:
            return "Error: 'job_id' required."

        jobs = _load_jobs()
        if job_id not in jobs:
            return f"Job not found: {job_id}"

        jobs[job_id]["paused"] = paused
        _save_jobs(jobs)

        if self._on_change:
            self._on_change("pause" if paused else "resume", {"id": job_id})

        action = "Paused" if paused else "Resumed"
        return f"{action} job: {job_id} ({jobs[job_id]['name']})"

    def _run_now(self, params: dict) -> str:
        job_id = params.get("job_id", "")
        if not job_id:
            return "Error: 'job_id' required."

        jobs = _load_jobs()
        if job_id not in jobs:
            return f"Job not found: {job_id}"

        if self._on_change:
            self._on_change("run_now", jobs[job_id])
            return f"Triggered immediate run: {job_id} ({jobs[job_id]['name']})"

        return f"Job found but no scheduler connected. Start the server with 'bpy serve'."
