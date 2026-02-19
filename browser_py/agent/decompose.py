"""Subtask decomposition — break complex tasks into (task, tool) pairs.

The decomposer LLM call converts a user request into an ordered list of
subtasks, each paired with the tool it needs. A runner iterates through
them sequentially, executing each via a focused mini-agent loop.

Key design:
- Subtask agents browse/gather data, then produce their report as their
  TEXT RESPONSE (not a file write tool call). The runner captures and saves it.
- The final compilation step is a single LLM call (no tools) that reads all
  subtask outputs and produces a final report as text.
- Subtask text responses stream to WebSocket for live UI updates but do NOT
  pollute the main agent's context.

Deep research is a specialization: fixed 5-subtopic decomposition where
each subtask uses the browser/search tool and must visit 3 URLs.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable

from browser_py.agent.config import get_agent_config, get_model, get_provider, get_provider_key, PROVIDERS


# ── Prompts ──

DECOMPOSE_PROMPT = """\
You are a task decomposition planner. Today is {today}.

Given a user task, decide:
1. If it's **simple** (answerable directly, single tool call, or conversational), \
return a JSON object: {{"simple": true}}
2. If it's **complex** (multi-step, needs research, file creation, etc.), decompose \
it into a list of subtasks.

For complex tasks, return a JSON array of subtask objects. Each subtask has:
- "task": Detailed description of what to do
- "tool": Primary tool to use ("browser", "files", "shell", "pdf", "spreadsheet")
- "output": Where to write results — a filename like "step_1_results.md"

Rules:
- Each subtask should be independently executable with a clear output.
- The LAST subtask is ALWAYS a compilation step with tool "compile".
- Compilation takes all prior outputs and produces the final answer.
- Keep the list short — 3-7 subtasks is ideal, max 10.
- Each subtask's "task" should include enough context to execute without seeing the original query.

Example response for a complex task:
```json
[
  {{"task": "Search Google for 'best Python web frameworks 2025' and extract the top 5 results with descriptions", "tool": "browser", "output": "step_1_search.md"}},
  {{"task": "Visit each framework's official site and note key features, performance claims, and community size", "tool": "browser", "output": "step_2_details.md"}},
  {{"task": "Compile all findings into a comprehensive comparison report with recommendations", "tool": "compile", "output": "final_report.md"}}
]
```

Example response for a simple task:
```json
{{"simple": true}}
```

User task: {task}
"""

SUBTASK_SYSTEM_PROMPT = """\
You are a focused research agent. Today is {today}.

You have ONE job: complete the task below using the {tool} tool, then \
write your findings as your final response.

Your workspace is: {workspace}

## Rules
- Stay focused — do NOT go on tangents.
- Be EFFICIENT. Aim for under 10 tool calls total.
- When you have enough information, STOP browsing and write your report.
- Your final text response IS your output — do NOT call any file write tool.
- Include source URLs as citations in your report.
- Write a thorough report with key facts, data points, and citations. \
Not a summary — a proper report.
"""

COMPILE_SYSTEM_PROMPT = """\
You are a compilation agent. Today is {today}.

## Original Task
{original_task}

## Subtask Reports
{subtask_reports}

## Instructions
Compile the subtask reports above into a comprehensive, well-structured \
final report. Organize by theme, highlight key insights, include all \
source URLs in a References section. Use markdown formatting.

Write a thorough, readable report — not a summary of summaries.
"""

# ── Deep Research Prompts ──

RESEARCH_DECOMPOSE_PROMPT = """\
You are a research planner. Today is {today}.

Given a research query, decompose it into exactly {n} focused subtopics \
that together comprehensively cover the topic.

Each subtopic should:
- Be specific enough to research in one focused search session
- Cover a different angle/aspect of the main query
- Be independently researchable

Return a JSON array of {n} objects:
- "subtopic": Concise title
- "task": Detailed research instructions (what to search for, what to find)

Research query: {query}
"""

RESEARCH_SUBTASK_SYSTEM_PROMPT = """\
You are a focused web researcher. Today is {today}.

Your workspace is: {workspace}

## Research Workflow
1. Use browser action="search" to Google your topic.
2. From the results, pick exactly 3 URLs that look most relevant.
3. For each URL: open it (action="open"), read its content (action="text"), \
and extract key findings.
4. After visiting all 3 URLs, STOP browsing and write your report as your \
final text response.

## Key Rules
- You MUST visit exactly 3 URLs (not more, not less).
- Use action="text" to read page content.
- Do NOT call any file write tool — your text response IS your output.
- Include source URLs as citations.
- Write detailed findings with data, stats, and key takeaways.
- Be efficient — don't waste tool calls.
"""

RESEARCH_COMPILE_PROMPT = """\
You are a research report compiler. Today is {today}.

## Original Research Query
{query}

## Research Findings

{findings}

## Instructions
Compile the research findings above into a comprehensive, well-structured \
research report.

The report should:
1. Start with an executive summary
2. Organize findings into logical sections
3. Highlight key insights and conclusions
4. Include all source URLs in a References section
5. Note any conflicting information across sources

Use markdown. Be thorough but readable.
"""


# ── Helpers ──

def _make_run_dirname(task: str) -> str:
    """Create a human-friendly directory name from a task description.

    Examples:
        "Compare Python web frameworks" → "compare-python-web-frameworks-feb-19-6pm"
        "Check my Gmail for new emails" → "check-gmail-for-new-emails-feb-19-6pm"
    """
    import re
    from datetime import datetime

    # Clean the task: lowercase, keep alphanumeric + spaces
    clean = re.sub(r'[^a-zA-Z0-9\s]', '', task.lower())
    # Take first ~6 meaningful words
    words = clean.split()[:6]
    # Remove filler words
    fillers = {'the', 'a', 'an', 'and', 'or', 'to', 'for', 'of', 'in', 'on', 'my', 'me', 'is', 'it'}
    words = [w for w in words if w not in fillers] or words[:3]
    slug = '-'.join(words[:5]) or 'task'

    # Add readable timestamp
    now = datetime.now()
    hour = now.strftime("%-I%p").lower()  # e.g. "6pm"
    date_str = now.strftime("%b-%-d").lower()  # e.g. "feb-19"

    return f"{slug}-{date_str}-{hour}"


# ── Data Types ──

class Subtask:
    """A single subtask in a decomposition plan."""

    def __init__(self, task: str, tool: str, output: str, index: int = 0, total: int = 0):
        self.task = task
        self.tool = tool
        self.output = output
        self.index = index
        self.total = total
        self.result: str | None = None
        self.status: str = "pending"  # pending | running | done | failed
        self.duration: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "tool": self.tool,
            "output": self.output,
            "index": self.index,
            "total": self.total,
            "status": self.status,
            "duration": round(self.duration, 1),
        }


# ── LLM Helpers ──

def _call_llm_simple(prompt: str, system: str = "", max_tokens: int = 4096) -> str:
    """Single LLM call without tools — for decomposition."""
    import litellm
    import os

    provider = get_provider()
    key = get_provider_key(provider)
    model = get_model()

    info = PROVIDERS.get(provider, {})
    if provider == "openrouter":
        os.environ["OPENROUTER_API_KEY"] = key
    elif provider in ("anthropic", "claude_max"):
        os.environ["ANTHROPIC_API_KEY"] = key
    elif provider == "openai":
        os.environ["OPENAI_API_KEY"] = key

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    cfg = get_agent_config()
    kwargs = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        timeout=cfg.get("timeout", 300),
    )

    # Reasoning effort — optional, off by default
    reasoning = cfg.get("reasoning_effort")
    if reasoning:
        kwargs["reasoning_effort"] = reasoning

    if provider == "openrouter":
        kwargs["api_key"] = key
        kwargs["base_url"] = "https://openrouter.ai/api/v1"
        kwargs["model"] = f"openai/{model}"

    response = litellm.completion(**kwargs)
    return response.choices[0].message.content or ""


def _call_llm_streaming(system: str, prompt: str, max_tokens: int = 16384,
                         on_chunk: Callable[[str], None] | None = None) -> str:
    """Single LLM call with streaming — for compilation.

    Streams chunks via on_chunk callback, returns full text.
    No tools, just text generation.
    """
    import litellm
    import os

    provider = get_provider()
    key = get_provider_key(provider)
    model = get_model()

    if provider == "openrouter":
        os.environ["OPENROUTER_API_KEY"] = key
    elif provider in ("anthropic", "claude_max"):
        os.environ["ANTHROPIC_API_KEY"] = key
    elif provider == "openai":
        os.environ["OPENAI_API_KEY"] = key

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    cfg = get_agent_config()
    kwargs = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        timeout=cfg.get("timeout", 300),
        stream=True,
    )

    reasoning = cfg.get("reasoning_effort")
    if reasoning:
        kwargs["reasoning_effort"] = reasoning

    if provider == "openrouter":
        kwargs["api_key"] = key
        kwargs["base_url"] = "https://openrouter.ai/api/v1"
        kwargs["model"] = f"openai/{model}"

    response = litellm.completion(**kwargs)

    full_text = []
    for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            full_text.append(delta.content)
            if on_chunk:
                on_chunk(delta.content)

    return "".join(full_text)


# ── Decomposition ──

def decompose_task(task: str) -> list[Subtask] | None:
    """Decompose a task into subtasks. Returns None if the task is simple."""
    today = date.today().strftime("%B %d, %Y")
    prompt = DECOMPOSE_PROMPT.format(task=task, today=today)
    response = _call_llm_simple(prompt)
    return _parse_decomposition(response)


def decompose_research(query: str, num_topics: int = 5) -> list[Subtask]:
    """Decompose a research query into fixed subtopics + compilation."""
    today = date.today().strftime("%B %d, %Y")
    prompt = RESEARCH_DECOMPOSE_PROMPT.format(query=query, n=num_topics, today=today)
    response = _call_llm_simple(prompt)
    subtopics = _parse_subtopics(response)

    if len(subtopics) < num_topics:
        subtopics = [
            {"subtopic": f"Aspect {i+1}", "task": f"Research aspect {i+1} of: {query}"}
            for i in range(num_topics)
        ]

    total = num_topics + 1
    subtasks = []
    for i, st in enumerate(subtopics[:num_topics]):
        subtasks.append(Subtask(
            task=st["task"], tool="browser",
            output=f"findings_{i+1}.md", index=i, total=total,
        ))

    file_list = ", ".join(f"findings_{i+1}.md" for i in range(num_topics))
    subtasks.append(Subtask(
        task=f"Compile all {num_topics} research findings ({file_list}) into a final report",
        tool="compile", output="final_report.md", index=num_topics, total=total,
    ))
    return subtasks


def _parse_decomposition(text: str) -> list[Subtask] | None:
    """Parse the decomposer response into subtasks or None (simple)."""
    import re

    match = re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', text, re.DOTALL)
    raw = match.group(1) if match else None

    if not raw:
        match = re.search(r'(\{[^{}]*"simple"[^{}]*\})', text, re.DOTALL)
        if match:
            raw = match.group(1)

    if not raw:
        match = re.search(r'(\[.*\])', text, re.DOTALL)
        if match:
            raw = match.group(1)

    if not raw:
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, dict) and parsed.get("simple"):
        return None

    if not isinstance(parsed, list) or len(parsed) < 2:
        return None

    total = len(parsed)
    subtasks = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            continue
        subtasks.append(Subtask(
            task=item.get("task", ""),
            tool=item.get("tool", "browser"),
            output=item.get("output", f"step_{i+1}.md"),
            index=i, total=total,
        ))
    return subtasks if len(subtasks) >= 2 else None


def _parse_subtopics(text: str) -> list[dict[str, str]]:
    """Extract subtopics JSON from the planner's response."""
    import re
    match = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return []


# ── Subtask Runner ──

class SubtaskRunner:
    """Executes a list of subtasks sequentially.

    Browsing subtasks: mini-agent with tools → text response = output.
    Compile subtask: single streaming LLM call → text = output.
    All outputs saved to disk by the runner, not by the agents.

    Args:
        subtasks: Ordered list of Subtask objects (last should be compile).
        workspace: Working directory for all file I/O.
        browser_profile: Browser profile for browser-using subtasks.
        on_subtask_start: Callback(subtask) when a subtask begins.
        on_subtask_done: Callback(subtask) when a subtask completes.
        on_tool_call: Callback(name, params, result) for tool execution events.
        on_token_update: Callback(usage_dict) for token tracking.
        on_stream_chunk: Callback(chunk_text) for streaming text to UI.
        abort_event: Threading event to cancel early.
        original_task: The original user task (for compilation context).
        research_query: If set, use research-specific prompts.
    """

    def __init__(
        self,
        subtasks: list[Subtask],
        workspace: Path,
        browser_profile: str | None = None,
        on_subtask_start: Callable[[Subtask], None] | None = None,
        on_subtask_done: Callable[[Subtask], None] | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
        on_token_update: Callable[[dict], None] | None = None,
        on_stream_chunk: Callable[[str], None] | None = None,
        abort_event: Any = None,
        original_task: str = "",
        research_query: str | None = None,
    ) -> None:
        self.subtasks = subtasks
        self.workspace = workspace
        self.browser_profile = browser_profile
        self.on_subtask_start = on_subtask_start or (lambda s: None)
        self.on_subtask_done = on_subtask_done or (lambda s: None)
        self.on_tool_call = on_tool_call
        self.on_token_update = on_token_update
        self.on_stream_chunk = on_stream_chunk
        self.abort_event = abort_event
        self.original_task = original_task
        self.research_query = research_query

        # Working directory for subtask outputs — human-friendly name
        self.run_dir = workspace / _make_run_dirname(original_task or research_query or "task")
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Track the active sub-agent for probe
        self.active_agent: Any = None

        # Cumulative token tracking
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def _build_subtask_system_prompt(self, subtask: Subtask) -> str:
        """Build system prompt for a browsing subtask's mini-agent."""
        today = date.today().strftime("%B %d, %Y")

        if self.research_query and subtask.tool == "browser":
            return RESEARCH_SUBTASK_SYSTEM_PROMPT.format(
                today=today, workspace=self.workspace,
            )

        return SUBTASK_SYSTEM_PROMPT.format(
            today=today, tool=subtask.tool, workspace=self.workspace,
        )

    def _create_mini_agent(self, system_prompt: str) -> 'Agent':
        """Create a focused mini-agent for a single subtask."""
        from browser_py.agent.loop import Agent

        cfg = get_agent_config()
        agent = Agent(
            workspace=self.workspace,
            browser_profile=self.browser_profile or cfg.get("browser_profile"),
            on_tool_call=self.on_tool_call,
            on_token_update=self._on_sub_token_update,
            max_iterations=15,
        )
        if not cfg.get("shell_enabled", True):
            agent._shell.enabled = False
        agent._custom_system_prompt = system_prompt
        # Wire streaming: sub-agent streams text chunks to UI
        agent._on_stream_chunk = self.on_stream_chunk
        return agent

    def _on_sub_token_update(self, usage: dict) -> None:
        if self.on_token_update:
            usage["subtask_total_tokens"] = self.total_tokens
            self.on_token_update(usage)

    def run_subtask(self, subtask: Subtask) -> str:
        """Execute a single subtask. Returns the text output."""
        start = time.time()
        subtask.status = "running"
        self.on_subtask_start(subtask)

        output_path = self.run_dir / subtask.output

        if subtask.tool == "compile":
            # Compilation: single streaming LLM call, no tools
            text = self._run_compile(subtask)
        else:
            # Browsing subtask: mini-agent with tools → text response
            text = self._run_browsing_subtask(subtask)

        # Runner writes to disk — agent never touches files
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text or f"# Subtask {subtask.index + 1}\n\n*No output produced.*\n")

        subtask.status = "done"
        subtask.duration = time.time() - start
        subtask.result = text
        self.on_subtask_done(subtask)
        return text

    def _run_browsing_subtask(self, subtask: Subtask) -> str:
        """Run a browsing subtask via mini-agent. Returns text response."""
        system_prompt = self._build_subtask_system_prompt(subtask)
        agent = self._create_mini_agent(system_prompt)
        self.active_agent = agent

        task_prompt = subtask.task
        if self.research_query:
            task_prompt = (
                f"Research this subtopic: {subtask.task}\n\n"
                f"Search Google, pick 3 relevant URLs from results, visit each one, "
                f"read the content. Then write your detailed findings as your response."
            )

        try:
            response = agent.chat(task_prompt)
        except Exception as e:
            response = f"# Subtask {subtask.index + 1} — FAILED\n\n{e}\n"
        finally:
            try:
                agent.cleanup_browser()
            except Exception:
                pass
            self.active_agent = None

        # Track tokens
        self.total_tokens += agent.total_tokens
        self.prompt_tokens += agent.prompt_tokens
        self.completion_tokens += agent.completion_tokens

        # Note: streaming already happens live via agent._on_stream_chunk
        # during LLM calls. No need to re-send the full response here.

        return response

    def _run_compile(self, subtask: Subtask) -> str:
        """Run compilation as a single streaming LLM call. No tools."""
        today = date.today().strftime("%B %d, %Y")

        # Read all prior subtask outputs
        reports = []
        for st in self.subtasks:
            if st.index >= subtask.index:
                break
            path = self.run_dir / st.output
            if path.exists():
                try:
                    content = path.read_text()
                    reports.append(f"### Subtask {st.index + 1}: {st.task[:80]}\n\n{content}")
                except OSError:
                    reports.append(f"### Subtask {st.index + 1}\n\n*File not found*")

        findings = "\n\n---\n\n".join(reports) if reports else "*No subtask outputs found.*"

        if self.research_query:
            system = f"You are a research report compiler. Today is {today}."
            prompt = RESEARCH_COMPILE_PROMPT.format(
                today=today, query=self.research_query,
                findings=findings,
            )
        else:
            system = f"You are a report compiler. Today is {today}."
            prompt = COMPILE_SYSTEM_PROMPT.format(
                today=today, original_task=self.original_task,
                subtask_reports=findings,
            )

        # Stream compilation to UI
        text = _call_llm_streaming(
            system=system,
            prompt=prompt,
            max_tokens=16384,
            on_chunk=self.on_stream_chunk,
        )
        return text

    def run(self) -> dict[str, Any]:
        """Execute all subtasks sequentially. Returns result dict."""
        start = time.time()

        for subtask in self.subtasks:
            if self.abort_event and self.abort_event.is_set():
                subtask.status = "failed"
                break
            self.run_subtask(subtask)

        duration = time.time() - start

        # Final output = last completed subtask's result
        final_output = ""
        last_done = [s for s in self.subtasks if s.status == "done"]
        if last_done:
            final_output = last_done[-1].result or ""

        return {
            "subtasks": [s.to_dict() for s in self.subtasks],
            "final_output": final_output,
            "output_dir": str(self.run_dir.relative_to(self.workspace)),
            "duration_seconds": round(duration, 1),
            "total_tokens": self.total_tokens,
            "aborted": bool(self.abort_event and self.abort_event.is_set()),
        }
