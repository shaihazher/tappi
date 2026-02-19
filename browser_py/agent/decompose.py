"""Subtask decomposition — break complex tasks into (task, tool) pairs.

The decomposer LLM call converts a user request into an ordered list of
subtasks, each paired with the tool it needs. A runner iterates through
them sequentially, executing each via a focused mini-agent loop. The
final subtask is always a compilation step.

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
You are a focused task executor. Today is {today}.

You have ONE job: complete the task below using the {tool} tool. \
Write your findings/results to: **{output_file}**

Your workspace is: {workspace}

## Context Window
{context_limit:,} tokens available. If compacted, use `files grep` on \
`context_dumps/` to recover details.

## Prior Results
{prior_context}

## Rules
- Stay focused on your specific task.
- Write results to the output file using the files tool.
- Be thorough but efficient.
- When done, confirm what you wrote and where.
"""

COMPILE_SYSTEM_PROMPT = """\
You are a compilation agent. Today is {today}.

Your job: read all the subtask outputs listed below and compile them into \
a comprehensive, well-structured final response.

Your workspace is: {workspace}

## Subtask Outputs
{subtask_outputs}

## Original Task
{original_task}

## Instructions
1. Read each subtask output file using the files tool.
2. Synthesize everything into a coherent final output.
3. Write the compiled result to: **{output_file}**
4. Then provide a summary as your response.

Make it thorough, well-organized, and directly useful. Use markdown formatting.
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
4. Write ALL findings to: **{output_file}** using the files tool.

## Key Rules
- You MUST visit exactly 3 URLs (not more, not less).
- Use action="text" to read page content (not elements).
- Include source URLs in your notes.
- Write findings as bullet points with data, stats, and key takeaways.
- Be efficient — don't waste tool calls.

## Context Window
{context_limit:,} tokens available.
"""

RESEARCH_COMPILE_PROMPT = """\
You are a research report compiler. Today is {today}.

## Original Research Query
{query}

## Instructions
Read all {n} research findings files listed below, then compile them into \
a comprehensive, well-structured research report. Write it to: **{output_file}**

Subtask output files:
{file_list}

The report should:
1. Start with an executive summary
2. Organize findings into logical sections
3. Highlight key insights and conclusions
4. Include all source URLs in a References section
5. Note any conflicting information across sources

Use markdown. Be thorough but readable.
"""


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


# ── Decomposition ──

def _call_llm_simple(prompt: str, system: str = "", max_tokens: int = 4096) -> str:
    """Single LLM call without tools — for decomposition/compilation planning."""
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

    kwargs = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        timeout=get_agent_config().get("timeout", 300),
    )

    if provider == "openrouter":
        kwargs["api_key"] = key
        kwargs["base_url"] = "https://openrouter.ai/api/v1"
        kwargs["model"] = f"openai/{model}"

    response = litellm.completion(**kwargs)
    return response.choices[0].message.content or ""


def decompose_task(task: str) -> list[Subtask] | None:
    """Decompose a task into subtasks. Returns None if the task is simple.

    Makes a single LLM call to analyze the task and either returns None
    (simple, handle directly) or a list of Subtask objects.
    """
    today = date.today().strftime("%B %d, %Y")
    prompt = DECOMPOSE_PROMPT.format(task=task, today=today)

    response = _call_llm_simple(prompt)
    return _parse_decomposition(response)


def decompose_research(query: str, num_topics: int = 5) -> list[Subtask]:
    """Decompose a research query into fixed subtopics + compilation.

    Always returns exactly num_topics + 1 subtasks (last = compile).
    """
    today = date.today().strftime("%B %d, %Y")
    prompt = RESEARCH_DECOMPOSE_PROMPT.format(
        query=query, n=num_topics, today=today,
    )

    response = _call_llm_simple(prompt)
    subtopics = _parse_subtopics(response)

    # Fallback if parsing fails
    if len(subtopics) < num_topics:
        subtopics = [
            {"subtopic": f"Aspect {i+1}", "task": f"Research aspect {i+1} of: {query}"}
            for i in range(num_topics)
        ]

    total = num_topics + 1  # +1 for compilation
    subtasks = []

    for i, st in enumerate(subtopics[:num_topics]):
        subtasks.append(Subtask(
            task=st["task"],
            tool="browser",
            output=f"findings_{i+1}.md",
            index=i,
            total=total,
        ))

    # Compilation subtask
    file_list = ", ".join(f"findings_{i+1}.md" for i in range(num_topics))
    subtasks.append(Subtask(
        task=f"Compile all {num_topics} research findings ({file_list}) into a final report",
        tool="compile",
        output="final_report.md",
        index=num_topics,
        total=total,
    ))

    return subtasks


def _parse_decomposition(text: str) -> list[Subtask] | None:
    """Parse the decomposer response into subtasks or None (simple)."""
    import re

    # Try to extract JSON from response
    # Pattern 1: ```json ... ```
    match = re.search(r'```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```', text, re.DOTALL)
    raw = match.group(1) if match else None

    # Pattern 2: bare JSON
    if not raw:
        match = re.search(r'(\{[^{}]*"simple"[^{}]*\})', text, re.DOTALL)
        if match:
            raw = match.group(1)

    if not raw:
        match = re.search(r'(\[.*\])', text, re.DOTALL)
        if match:
            raw = match.group(1)

    if not raw:
        return None  # Can't parse — treat as simple

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Simple task
    if isinstance(parsed, dict) and parsed.get("simple"):
        return None

    # Complex task — list of subtasks
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
            index=i,
            total=total,
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
    """Executes a list of subtasks sequentially, each with a focused mini-agent.

    Args:
        subtasks: Ordered list of Subtask objects (last should be compile).
        workspace: Working directory for all file I/O.
        browser_profile: Browser profile for browser-using subtasks.
        on_subtask_start: Callback(subtask) when a subtask begins.
        on_subtask_done: Callback(subtask) when a subtask completes.
        on_tool_call: Callback(name, params, result) for tool execution events.
        on_token_update: Callback(usage_dict) for token tracking.
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
        self.abort_event = abort_event
        self.original_task = original_task
        self.research_query = research_query

        # Working directory for subtask outputs
        self.run_dir = workspace / "subtask_runs" / f"run_{int(time.time())}"
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Track the active sub-agent for probe
        self.active_agent: Any = None

        # Cumulative token tracking
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def _build_subtask_system_prompt(self, subtask: Subtask, prior_results: list[tuple[str, str]]) -> str:
        """Build system prompt for a subtask's mini-agent."""
        today = date.today().strftime("%B %d, %Y")
        from browser_py.agent.sessions import get_context_limit
        model = get_model()
        context_limit = get_context_limit(model)
        output_file = str(self.run_dir.relative_to(self.workspace) / subtask.output)

        # Build prior context summary
        if prior_results:
            prior_lines = []
            for name, path in prior_results:
                prior_lines.append(f"- **{name}**: written to `{path}`")
            prior_context = "Previous subtasks completed:\n" + "\n".join(prior_lines)
        else:
            prior_context = "This is the first subtask — no prior results."

        # Research subtask uses specialized prompt
        if self.research_query and subtask.tool == "browser":
            return RESEARCH_SUBTASK_SYSTEM_PROMPT.format(
                today=today,
                workspace=self.workspace,
                output_file=output_file,
                context_limit=context_limit,
            )

        return SUBTASK_SYSTEM_PROMPT.format(
            today=today,
            tool=subtask.tool,
            output_file=output_file,
            workspace=self.workspace,
            context_limit=context_limit,
            prior_context=prior_context,
        )

    def _build_compile_prompt(self, subtask: Subtask) -> str:
        """Build system prompt for the compilation subtask."""
        today = date.today().strftime("%B %d, %Y")
        output_file = str(self.run_dir.relative_to(self.workspace) / subtask.output)

        # Collect all prior output files
        subtask_outputs = []
        for st in self.subtasks:
            if st.index == subtask.index:
                break
            if st.status == "done":
                path = str(self.run_dir.relative_to(self.workspace) / st.output)
                subtask_outputs.append(f"- `{path}` — {st.task[:100]}")

        outputs_text = "\n".join(subtask_outputs) if subtask_outputs else "No prior outputs found."

        # Research compilation
        if self.research_query:
            file_list = "\n".join(f"- `{self.run_dir.relative_to(self.workspace) / st.output}`"
                                  for st in self.subtasks if st.index < subtask.index)
            return RESEARCH_COMPILE_PROMPT.format(
                today=today,
                query=self.research_query,
                n=subtask.index,
                output_file=output_file,
                file_list=file_list,
            )

        return COMPILE_SYSTEM_PROMPT.format(
            today=today,
            workspace=self.workspace,
            subtask_outputs=outputs_text,
            original_task=self.original_task,
            output_file=output_file,
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
            max_iterations=50,
        )
        if not cfg.get("shell_enabled", True):
            agent._shell.enabled = False
        agent._custom_system_prompt = system_prompt
        return agent

    def _on_sub_token_update(self, usage: dict) -> None:
        """Aggregate token usage from sub-agents."""
        # We track cumulative across all subtasks
        if self.on_token_update:
            usage["subtask_total_tokens"] = self.total_tokens
            self.on_token_update(usage)

    def run_subtask(self, subtask: Subtask, prior_results: list[tuple[str, str]]) -> str:
        """Execute a single subtask and return the output file path (relative)."""
        start = time.time()
        subtask.status = "running"
        self.on_subtask_start(subtask)

        output_rel = str(self.run_dir.relative_to(self.workspace) / subtask.output)
        output_abs = self.run_dir / subtask.output

        # Pick system prompt based on whether this is compile or regular
        if subtask.tool == "compile":
            system_prompt = self._build_compile_prompt(subtask)
            max_tokens = get_agent_config().get("main_max_tokens", 16384)
        else:
            system_prompt = self._build_subtask_system_prompt(subtask, prior_results)
            max_tokens = get_agent_config().get("subagent_max_tokens", 4096)

        agent = self._create_mini_agent(system_prompt)
        self.active_agent = agent

        # Build the task prompt
        task_prompt = subtask.task
        if self.research_query and subtask.tool == "browser":
            task_prompt = (
                f"Research this subtopic: {subtask.task}\n\n"
                f"Search Google, pick 3 relevant URLs from results, visit each one, "
                f"read the content, and write detailed findings to: {output_rel}"
            )
        elif subtask.tool != "compile":
            task_prompt = f"{subtask.task}\n\nWrite your results to: {output_rel}"
        else:
            task_prompt = (
                f"{subtask.task}\n\n"
                f"Read all the subtask output files listed in your instructions, "
                f"compile them, and write the final result to: {output_rel}"
            )

        try:
            response = agent.chat(task_prompt)
        except Exception as e:
            response = f"Error: {e}"
            output_abs.parent.mkdir(parents=True, exist_ok=True)
            output_abs.write_text(f"# Subtask {subtask.index + 1} — FAILED\n\n{e}\n")
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

        # If the agent didn't write the output file, save its response
        if not output_abs.exists() and response.strip():
            output_abs.parent.mkdir(parents=True, exist_ok=True)
            output_abs.write_text(f"# Subtask {subtask.index + 1}\n\n{response}\n")

        subtask.status = "done"
        subtask.duration = time.time() - start
        subtask.result = output_rel
        self.on_subtask_done(subtask)

        return output_rel

    def run(self) -> dict[str, Any]:
        """Execute all subtasks sequentially. Returns result dict."""
        start = time.time()
        prior_results: list[tuple[str, str]] = []

        for subtask in self.subtasks:
            if self.abort_event and self.abort_event.is_set():
                subtask.status = "failed"
                break

            output_path = self.run_subtask(subtask, prior_results)
            prior_results.append((subtask.task[:80], output_path))

        duration = time.time() - start

        # Read the final output (last subtask's output)
        final_output = ""
        last_done = [s for s in self.subtasks if s.status == "done"]
        if last_done:
            final_path = self.run_dir / last_done[-1].output
            if final_path.exists():
                try:
                    final_output = final_path.read_text()
                except OSError:
                    pass

        return {
            "subtasks": [s.to_dict() for s in self.subtasks],
            "final_output": final_output,
            "output_dir": str(self.run_dir.relative_to(self.workspace)),
            "duration_seconds": round(duration, 1),
            "total_tokens": self.total_tokens,
            "aborted": bool(self.abort_event and self.abort_event.is_set()),
        }
