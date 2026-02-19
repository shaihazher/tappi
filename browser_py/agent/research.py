"""Deep research mode — specialized subtask decomposition for research.

Uses the same decompose → run → compile pattern as general task decomposition,
but with fixed rules:
- Exactly N subtopics (default 5)
- Each subtopic: Google search → pick 3 URLs → visit & research → write notes
- Compilation: read all findings → produce structured report (max_tokens 16384)

Usage:
    from browser_py.agent.research import run_research
    result = run_research("What are the best Python web frameworks in 2025?")
"""

from __future__ import annotations

import time
from typing import Any, Callable

from browser_py.agent.config import get_agent_config, get_workspace
from browser_py.agent.decompose import (
    SubtaskRunner,
    Subtask,
    decompose_research,
)


NUM_SUB_AGENTS = 5


class ResearchSession:
    """Manages a deep research session using subtask decomposition.

    Args:
        query: The research question/topic.
        on_progress: Callback for progress updates (stage, message).
        browser_profile: Browser profile for sub-agents.
        num_agents: Number of research subtopics (default: 5).
        abort_event: Threading event — set to abort research early.
        on_agent_created: Callback when a sub-agent is created (for external tracking).
    """

    def __init__(
        self,
        query: str,
        on_progress: Callable[[str, str], None] | None = None,
        browser_profile: str | None = None,
        num_agents: int = NUM_SUB_AGENTS,
        abort_event: Any = None,
        on_agent_created: Callable | None = None,
    ) -> None:
        self.query = query
        self.on_progress = on_progress or (lambda s, m: None)
        self.browser_profile = browser_profile
        self.num_agents = num_agents
        self.abort_event = abort_event
        self.on_agent_created = on_agent_created
        self.workspace = get_workspace()

    def _progress(self, stage: str, message: str) -> None:
        self.on_progress(stage, message)

    def run(self) -> dict[str, Any]:
        """Execute the full research pipeline.

        Returns:
            {
                "query": str,
                "report_path": str,
                "report": str,
                "subtopics": [...],
                "findings_paths": [...],
                "duration_seconds": float,
            }
        """
        start = time.time()

        # Step 1: Decompose into subtopics
        self._progress("planning", f"Breaking down query into {self.num_agents} subtopics...")
        subtasks = decompose_research(self.query, num_topics=self.num_agents)
        self._progress("planned", f"Created {self.num_agents} research subtopics + compilation")

        # Expose subtopic names
        subtopics = [
            {"subtopic": s.task[:80], "task": s.task}
            for s in subtasks if s.tool != "compile"
        ]

        # Step 2: Run all subtasks via SubtaskRunner
        def on_subtask_start(st: Subtask) -> None:
            if st.tool == "compile":
                self._progress("compiling", "Compiling findings into final report...")
            else:
                self._progress(
                    "researching",
                    f"Sub-agent {st.index + 1}/{self.num_agents}: {st.task[:80]}",
                )

        def on_subtask_done(st: Subtask) -> None:
            if st.tool == "compile":
                self._progress("compiled", "Report compiled")
            else:
                self._progress(
                    "researched",
                    f"Completed subtopic {st.index + 1}/{self.num_agents}",
                )

        def on_tool_call(name: str, params: dict, result: str) -> None:
            # Expose active sub-agent for probe
            if self.on_agent_created and runner.active_agent:
                self.on_agent_created(runner.active_agent)

        runner = SubtaskRunner(
            subtasks=subtasks,
            workspace=self.workspace,
            browser_profile=self.browser_profile,
            on_subtask_start=on_subtask_start,
            on_subtask_done=on_subtask_done,
            on_tool_call=on_tool_call,
            abort_event=self.abort_event,
            original_task=self.query,
            research_query=self.query,
        )

        result = runner.run()

        duration = time.time() - start

        # Build findings paths
        findings_paths = [
            str(runner.run_dir / s.output)
            for s in subtasks if s.tool != "compile" and s.status == "done"
        ]

        # Report path = compilation output
        compile_tasks = [s for s in subtasks if s.tool == "compile"]
        report_path = ""
        report_content = ""
        if compile_tasks and compile_tasks[0].status == "done":
            report_path = str(runner.run_dir / compile_tasks[0].output)
            try:
                report_content = (runner.run_dir / compile_tasks[0].output).read_text()
            except OSError:
                report_content = result.get("final_output", "")
        else:
            report_content = result.get("final_output", "")

        if result.get("aborted"):
            self._progress("aborted", "Research aborted by user")
        else:
            self._progress("done", f"Research complete in {duration:.0f}s")

        return {
            "query": self.query,
            "report_path": report_path,
            "report": report_content,
            "subtopics": subtopics,
            "findings_paths": findings_paths,
            "duration_seconds": duration,
        }


def run_research(
    query: str,
    on_progress: Callable[[str, str], None] | None = None,
    browser_profile: str | None = None,
    num_agents: int = NUM_SUB_AGENTS,
    abort_event: Any = None,
    on_agent_created: Callable | None = None,
) -> dict[str, Any]:
    """Convenience function to run a full research session.

    Args:
        query: Research question/topic.
        on_progress: Optional progress callback (stage, message).
        browser_profile: Browser profile for sub-agents.
        num_agents: Number of sub-agents (default: 5).
        abort_event: Threading event to abort research early.
        on_agent_created: Callback when a sub-agent is created.

    Returns:
        Result dict with report_path, report content, and metadata.
    """
    session = ResearchSession(
        query=query,
        on_progress=on_progress,
        browser_profile=browser_profile,
        num_agents=num_agents,
        abort_event=abort_event,
        on_agent_created=on_agent_created,
    )
    return session.run()
