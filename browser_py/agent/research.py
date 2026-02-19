"""Deep research mode — multi-agent sequential research pipeline.

A parent agent decomposes a research query into subtopics, then spawns
5 sequential sub-agents. Each sub-agent gets its own browser session and
focused task. The parent compiles a final report from all findings.

Usage:
    from browser_py.agentresearch import run_research
    report = run_research("What are the best Python web frameworks in 2025?")
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from browser_py.agent.config import get_agent_config, get_model, get_workspace
from browser_py.agent.loop import Agent


# Number of sub-agents to use for research
NUM_SUB_AGENTS = 5

PLANNER_PROMPT = """\
You are a research planner. Today's date is {today}. Given a research query, \
decompose it into exactly {n} focused subtopics that can be independently researched.

Each subtopic should be:
- Specific enough to research in one focused session
- Cover a different angle/aspect of the main query
- Together, they should cover the topic comprehensively

Respond with a JSON array of {n} objects, each with:
- "subtopic": A concise title
- "task": A detailed research task description (what to search for, what to find out)

Example format:
```json
[
  {{"subtopic": "Market Overview", "task": "Research the current market size, growth rate, and key players in..."}},
  ...
]
```

Research query: {query}
"""

SUB_AGENT_SYSTEM_PROMPT = """\
You are a focused web research agent. Today's date is {today}.

You have access to a browser, file tools, and other utilities. \
Your workspace directory is: {workspace}

## Research Workflow

1. **Use browser action="search"** with a query to Google something. \
It returns a clean list of result links with clickable index numbers.
2. **Click results by index** using action="click" to visit a page.
3. **Read page content** with action="text".
4. **Write findings** to a file using the files tool.

That's it: search → click → read → write. Aim for 2-3 sources per query.

## Key Rules

- Get URLs from search results. Use action="search" or action="elements" \
to discover real links before visiting them.
- Use action="text" to read article content (not elements).
- Be efficient — finish in under 10 tool calls.
- You can use files action="grep" to search file contents by keyword.

## Context & Memory

Your context window is {context_limit:,} tokens. If your context fills up, \
it will be compacted — your conversation is saved to `context_dumps/` and \
replaced with a summary. Use `files grep` on the dump to recover specifics. \
Do NOT read the full dump file.
"""

SUB_AGENT_TASK_PROMPT = """\
## Your Research Task
{task}

## Instructions
1. Search Google for this topic (use 1-2 well-crafted queries).
2. Visit 2-3 sources from the search results.
3. Read each source and extract key findings.
4. Write your complete findings to: **{output_file}** (use the files tool, action="write").

## Output Format (for the file)
- Key findings with bullet points
- Statistics and data points
- Source URLs you visited

Write the file now — your research is only captured if it's saved there.
"""

COMPILER_PROMPT = """\
You are a research compiler. You have received findings from {n} research \
sub-agents, each focused on a different aspect of the query.

## Original Query
{query}

## Sub-Agent Findings
{findings}

## Your Task
Compile these findings into a comprehensive, well-structured research report. \
Write the final report to: {output_file}

The report should:
1. Start with an executive summary
2. Organize findings into logical sections
3. Highlight key insights and conclusions
4. Include all source URLs in a References section at the end
5. Note any conflicting information found across sources

Make it thorough but readable. Use markdown formatting.
"""


class ResearchSession:
    """Manages a deep research session with multiple sub-agents.

    Args:
        query: The research question/topic.
        on_progress: Callback for progress updates (stage, message).
        browser_profile: Browser profile for sub-agents.
        num_agents: Number of sub-agents (default: 5).
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
        self.research_dir = self.workspace / "research" / f"research_{int(time.time())}"
        self.research_dir.mkdir(parents=True, exist_ok=True)

    def _progress(self, stage: str, message: str) -> None:
        self.on_progress(stage, message)

    def _create_agent(self, system_prompt: str | None = None) -> Agent:
        """Create a fresh agent instance for a sub-task."""
        cfg = get_agent_config()
        agent = Agent(
            workspace=self.workspace,
            browser_profile=self.browser_profile or cfg.get("browser_profile"),
            max_iterations=500,
        )
        if not cfg.get("shell_enabled", True):
            agent._shell.enabled = False
        if system_prompt:
            agent._custom_system_prompt = system_prompt
        if self.on_agent_created:
            self.on_agent_created(agent)
        return agent

    def plan(self) -> list[dict[str, str]]:
        """Use the LLM to decompose the query into subtopics."""
        self._progress("planning", f"Breaking down query into {self.num_agents} subtopics...")

        agent = self._create_agent()
        today = date.today().strftime("%B %d, %Y")
        prompt = PLANNER_PROMPT.format(n=self.num_agents, query=self.query, today=today)
        try:
            response = agent.chat(prompt)
        finally:
            try:
                agent.cleanup_browser()
            except Exception:
                pass

        # Parse the JSON from the response
        subtopics = self._parse_subtopics(response)

        if not subtopics or len(subtopics) < self.num_agents:
            # Fallback: create generic subtopics
            self._progress("planning", "Generating fallback subtopics...")
            subtopics = [
                {
                    "subtopic": f"Aspect {i+1}",
                    "task": f"Research aspect {i+1} of: {self.query}",
                }
                for i in range(self.num_agents)
            ]

        self._progress("planned", f"Created {len(subtopics)} research subtopics")
        return subtopics

    def _parse_subtopics(self, text: str) -> list[dict[str, str]]:
        """Extract subtopics JSON from the planner's response."""
        import re

        # Try to find JSON array in the response
        # Pattern 1: ```json [...] ```
        match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        # Pattern 2: bare JSON array
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass

        return []

    def research_subtopic(self, index: int, subtopic: dict[str, str]) -> str:
        """Run a sub-agent to research one subtopic.

        Returns the path to the findings file.
        """
        output_file = f"research/{self.research_dir.name}/findings_{index+1}.md"
        abs_output = self.workspace / output_file

        self._progress(
            "researching",
            f"Sub-agent {index+1}/{self.num_agents}: {subtopic['subtopic']}",
        )

        today = date.today().strftime("%B %d, %Y")
        # All placeholders ({today}, {workspace}, {context_*}) are filled
        # dynamically by Agent._build_system_prompt() on each LLM call.
        system = SUB_AGENT_SYSTEM_PROMPT
        agent = self._create_agent(system_prompt=system)
        prompt = SUB_AGENT_TASK_PROMPT.format(
            task=subtopic["task"],
            output_file=output_file,
        )

        response = ""
        try:
            response = agent.chat(prompt)
        except Exception as e:
            # Write error findings
            abs_output.write_text(
                f"# {subtopic['subtopic']}\n\n"
                f"*Research failed: {e}*\n"
            )
        finally:
            # Clean up: close any browser tabs this sub-agent opened
            try:
                agent.cleanup_browser()
            except Exception:
                pass

        # If the agent didn't write the file, save its chat response as findings
        if not abs_output.exists() and response.strip():
            abs_output.parent.mkdir(parents=True, exist_ok=True)
            abs_output.write_text(
                f"# {subtopic['subtopic']}\n\n{response}\n"
            )
        elif not abs_output.exists():
            abs_output.write_text(
                f"# {subtopic['subtopic']}\n\n"
                f"*No findings were produced by the sub-agent.*\n"
            )

        self._progress(
            "researched",
            f"Completed subtopic {index+1}/{self.num_agents}: {subtopic['subtopic']}",
        )

        return str(abs_output)

    def compile(self, findings_paths: list[str]) -> str:
        """Compile all sub-agent findings into a final report.

        Returns the path to the final report.
        """
        self._progress("compiling", "Compiling findings into final report...")

        # Read all findings
        findings_text = ""
        for i, path in enumerate(findings_paths):
            try:
                content = Path(path).read_text()
            except OSError:
                content = "*Findings file not found*"
            findings_text += f"\n### Sub-Agent {i+1} Findings\n\n{content}\n\n---\n"

        output_file = f"research/{self.research_dir.name}/final_report.md"

        agent = self._create_agent()
        prompt = COMPILER_PROMPT.format(
            n=len(findings_paths),
            query=self.query,
            findings=findings_text[:50000],  # Cap to avoid context overflow
            output_file=output_file,
        )

        try:
            agent.chat(prompt)
        except Exception as e:
            # Fallback: concatenate findings
            abs_output = self.workspace / output_file
            abs_output.write_text(
                f"# Research Report: {self.query}\n\n"
                f"*Compilation error: {e}*\n\n"
                f"## Raw Findings\n\n{findings_text}"
            )
        finally:
            try:
                agent.cleanup_browser()
            except Exception:
                pass

        report_path = self.workspace / output_file
        self._progress("complete", f"Report saved to {output_file}")

        if report_path.exists():
            return str(report_path)

        # Fallback
        report_path.write_text(
            f"# Research Report: {self.query}\n\n"
            f"## Combined Findings\n\n{findings_text}"
        )
        return str(report_path)

    def run(self) -> dict[str, Any]:
        """Execute the full research pipeline.

        Returns:
            {
                "query": str,
                "report_path": str,
                "report": str,  # full markdown content
                "subtopics": [...],
                "findings_paths": [...],
                "duration_seconds": float,
            }
        """
        start = time.time()

        # Step 1: Plan
        subtopics = self.plan()

        # Step 2: Research each subtopic sequentially
        findings_paths = []
        for i, subtopic in enumerate(subtopics):
            if self.abort_event and self.abort_event.is_set():
                self._progress("aborted", "Research aborted by user")
                break
            path = self.research_subtopic(i, subtopic)
            findings_paths.append(path)

        if self.abort_event and self.abort_event.is_set():
            # Skip compile, return partial results
            return self._partial_result(findings_paths, start)

        # Step 3: Compile
        report_path = self.compile(findings_paths)

        # Read the final report
        try:
            report_content = Path(report_path).read_text()
        except OSError:
            report_content = "*Report file not found*"

        duration = time.time() - start
        self._progress("done", f"Research complete in {duration:.0f}s")

        return {
            "query": self.query,
            "report_path": report_path,
            "report": report_content,
            "subtopics": subtopics,
            "findings_paths": findings_paths,
            "duration_seconds": duration,
        }

    def _partial_result(self, findings_paths: list[str], start: float) -> dict[str, Any]:
        """Return partial results when research is aborted."""
        findings_text = ""
        for i, path in enumerate(findings_paths):
            try:
                content = Path(path).read_text()
            except OSError:
                content = "*Not found*"
            findings_text += f"\n### Sub-Agent {i+1}\n\n{content}\n\n---\n"

        report = f"# Research Report (partial — aborted)\n\nQuery: {self.query}\n\n{findings_text}"
        report_path = self.research_dir / "partial_report.md"
        report_path.write_text(report)

        return {
            "query": self.query,
            "report_path": str(report_path),
            "report": report,
            "subtopics": [],
            "findings_paths": findings_paths,
            "duration_seconds": time.time() - start,
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
