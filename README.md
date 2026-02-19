# browser-py

**Your own AI agent that controls a real browser and manages files — running entirely on your machine.**

Give it a task in plain English. It opens your browser, navigates pages, clicks buttons, fills forms, reads content, creates PDFs, updates spreadsheets, and schedules recurring jobs. All your logins and cookies carry over. Everything stays local — your data never leaves your machine.

Think of it as a personal automation assistant with two superpowers: **browser control** and **file management**, sandboxed to one directory. Secure enough for work. Powerful enough to replace most browser automation scripts you've ever written.

### Why browser-py?

- **10x more token-efficient** than screenshot-based agents (Operator, Computer Use). Instead of sending full screenshots, browser-py indexes interactive elements into a compact numbered list — the LLM says `click 3` instead of parsing pixel coordinates from a 1MB image.
- **Better LLM decisions.** Numbered elements with semantic labels (`[3] (button) Submit Order`) give the model structured, unambiguous choices. No hallucinated CSS selectors. No coordinate guessing.
- **Real browser, real sessions.** Connects to Chrome via CDP — your saved logins, cookies, and extensions are all there. Log in once, automate forever.
- **Sandboxed by design.** One workspace directory. One browser. No filesystem access beyond the sandbox. Safe for corporate environments where you can't install full automation platforms.
- **Works everywhere.** Linux, macOS, Windows. Python 3.10+. Single `pip install`.

```bash
pip install browser-py            # CDP library only
pip install browser-py[agent]     # CDP + AI agent + all tools
```

---

## Table of Contents

- [Quick Start](#quick-start)
- [AI Agent Mode](#ai-agent-mode) ← **New**
- [Web UI](#web-ui) ← **New**
- [Tutorial: Your First Automation](#tutorial-your-first-automation)
- [How It Works](#how-it-works)
- [Python Library](#using-as-a-python-library)
- [CLI Reference](#cli-reference)
- [Profiles](#profiles)
- [Shadow DOM Support](#shadow-dom-support)
- [FAQ](#faq)
- [License](#license)

---

## Quick Start

```bash
# Install with agent support
pip install browser-py[agent]

# One-time setup: choose provider, enter API key, set workspace
bpy setup

# Launch a browser
bpy launch

# Chat with the agent
bpy agent "Go to github.com and find today's trending Python repos"

# Or use the web UI
bpy serve
```

---

## AI Agent Mode

The agent is an LLM with 6 tools that can browse the web, read/write files, create PDFs, manage spreadsheets, run shell commands, and schedule recurring tasks — all within a sandboxed workspace directory.

### Setup

```bash
bpy setup
```

The wizard walks you through:

1. **LLM Provider** — OpenRouter, Anthropic, Claude Max (OAuth), OpenAI, AWS Bedrock, Azure, Google Vertex
2. **API Key** — paste your key (or OAuth token for Claude Max)
3. **Model** — defaults per provider, fully configurable
4. **Workspace** — sandboxed directory for all file operations
5. **Browser Profile** — which browser profile the agent uses
6. **Shell Access** — toggle on/off

All config lives in `~/.browser-py/config.json`.

### Providers

| Provider | Auth | Status |
|----------|------|--------|
| **OpenRouter** | API key | ✅ Ready |
| **Anthropic** | API key | ✅ Ready |
| **Claude Max (OAuth)** | OAuth token (`sk-ant-oat01-...`) | ✅ Ready |
| **OpenAI** | API key | ✅ Ready |
| **AWS Bedrock** | AWS credentials | ✅ Ready (via LiteLLM) |
| **Azure OpenAI** | API key + endpoint | ✅ Ready (via LiteLLM) |
| **Google Vertex AI** | Service account | ✅ Ready (via LiteLLM) |

All providers work through [LiteLLM](https://github.com/BerriAI/litellm) — one interface, any model.

#### Claude Max (OAuth) — Use Your Subscription

If you have a Claude Pro/Max subscription ($20-200/mo), you can use your **OAuth token** instead of paying per-API-call. This is the same token Claude Code uses.

```bash
bpy setup
# Choose "Claude Max (OAuth)"
# Paste your token: sk-ant-oat01-...
```

**Where to find your token:**

- If you use Claude Code: check your credentials file or environment
- The token format is `sk-ant-oat01-...` (different from API keys which are `sk-ant-api03-...`)
- It works as a drop-in replacement — no proxy, no special config

### CLI Usage

#### Interactive mode

```bash
bpy agent
```

```
browser-py agent (type 'quit' to exit, 'reset' to clear)

You: Go to hacker news and find the top post about AI
  🔧 browser → launch
  🔧 browser → open
  🔧 browser → elements
  🔧 browser → text

Agent: The top AI-related post on Hacker News right now is "GPT-5 Released"
with 342 points. It links to openai.com/blog/gpt5 and the discussion has
127 comments. Want me to read the article or the comments?
```

#### One-shot mode

```bash
bpy agent "Create a PDF report of today's weather in Houston"
```

The agent figures out the steps: open a weather site → extract data → create HTML → convert to PDF → save to workspace.

### Tools

The agent has 6 tools, each exposed as a JSON schema the LLM calls natively:

| Tool | What it does |
|------|-------------|
| **browser** | Navigate, click, type, read pages, screenshots, tab management. Uses your real browser with saved logins. |
| **files** | Read, write, list, move, copy, delete files — sandboxed to workspace. |
| **pdf** | Read text from PDFs (PyMuPDF), create PDFs from HTML (WeasyPrint). |
| **spreadsheet** | Read/write CSV and Excel (.xlsx) files, create new ones with headers. |
| **shell** | Run shell commands (cwd = workspace). Can be disabled in settings. |
| **cron** | Schedule recurring tasks with cron expressions or intervals. |

### How the Agent Loop Works

```
User message
    ↓
┌──────────────────────────┐
│   LLM (via LiteLLM)      │ ◄── Sees all 6 tools as JSON schemas
│   Decides what to do      │
└──────────┬───────────────┘
           │
           ▼
    ┌─ Tool calls? ──┐
    │                 │
   Yes               No → Return text response
    │
    ▼
Execute each tool call
    │
    ▼
Append results to conversation
    │
    ▼
Loop back to LLM ────────────►  (max 50 iterations)
```

The loop is synchronous — each tool call blocks until complete. No timeouts. The LLM sees tool results and decides the next step, just like a human would.

### Cron (Scheduled Tasks)

Tell the agent to schedule recurring tasks:

```
You: Schedule a job to check trending repos on GitHub every morning at 9 AM
Agent: Done. Created job "GitHub Trends" with schedule "0 9 * * *".
```

Jobs are stored in `~/.browser-py/jobs.json` and persist across restarts. When `bpy serve` is running, APScheduler fires each job in its own agent session.

```bash
# Via CLI
bpy agent "List my scheduled jobs"
bpy agent "Pause the GitHub Trends job"
bpy agent "Remove job abc123"
```

---

## Web UI

```bash
bpy serve                    # http://127.0.0.1:8321
bpy serve --port 9000        # custom port
```

The web UI has 4 sections:

### 💬 Chat

Full chat interface with live tool call visibility. As the agent works, you see each tool call and its result in real-time via WebSocket.

### 🌍 Browser Profiles

View and create browser profiles. Each profile has its own Chrome sessions (cookies, logins) and CDP port. Create profiles for different use cases — work, personal, social media.

### ⏰ Scheduled Jobs

View all cron jobs with their schedule, status (active/paused), and task description. Jobs are created via chat ("schedule a task to...").

### ⚙️ Settings

- **Model** — change the LLM model
- **Browser Profile** — select which profile the agent uses
- **Shell Access** — enable/disable shell commands
- **Workspace** — view the sandboxed directory

> **Note:** Provider and API key changes require `bpy setup` (CLI) — these aren't exposed in the web UI for security.

---

## Tutorial: Your First Automation

### Step 1: Launch the browser

```bash
bpy launch
```

```
✓ Chrome launched on port 9222
  Profile: ~/.browser-py/profiles/default

⚡ First launch — a fresh Chrome window opened.
   Log into the sites you want to automate (Gmail, GitHub, etc.).
   Those sessions will persist for all future launches.
```

**First time only:** A fresh Chrome window opens. Log into the websites you want to automate. Close the window when done. Your sessions are saved in the profile.

### Step 2: Control it

```bash
bpy open github.com         # Navigate
bpy elements                # See what's clickable
bpy click 3                 # Click element [3]
bpy type 5 "hello world"    # Type into element [5]
bpy text                    # Read the page
bpy screenshot page.png     # Screenshot
```

Every interactive element gets a number. Use that number with `click` and `type`.

---

## How It Works

### The connection

```
┌─────────────┐     CDP (WebSocket)     ┌──────────────────┐
│  browser-py  │ ◄──────────────────────► │  Chrome/Chromium  │
│  (your code) │     localhost:9222       │  (your sessions)  │
└─────────────┘                          └──────────────────┘
```

`bpy launch` starts Chrome with `--remote-debugging-port=9222` and a persistent `--user-data-dir`. All commands connect to that port via WebSocket.

### Real mouse events

`click` uses CDP's `Input.dispatchMouseEvent` — real mouse presses, not `.click()`. Works with React, Vue, Angular, and every framework.

### Shadow DOM piercing

The element scanner recursively enters every shadow root. Reddit, GitHub, Salesforce, Angular Material — all work automatically.

### Framework-aware typing

`type` dispatches proper `input` and `change` events using React's native value setter. SPAs with controlled components get the value update correctly.

---

## Using as a Python Library

```python
from browser_py import Browser

Browser.launch()              # Start Chrome
b = Browser()                 # Connect

b.open("https://github.com")
elements = b.elements()       # List interactive elements
b.click(1)                    # Click by index
b.type(2, "search query")     # Type into input
text = b.text()               # Read visible text
b.screenshot("page.png")      # Screenshot
b.upload("~/file.pdf")        # Upload file
```

### Profile management

```python
from browser_py.profiles import create_profile, list_profiles, get_profile

create_profile("work")        # → port 9222
create_profile("personal")    # → port 9223

# Run multiple simultaneously
work = get_profile("work")
Browser.launch(port=work["port"], user_data_dir=work["path"])
b = Browser(f"http://127.0.0.1:{work['port']}")
```

### Agent as a library

```python
from browser_py.agent.loop import Agent

agent = Agent(
    browser_profile="default",
    on_tool_call=lambda name, params, result: print(f"🔧 {name}"),
)

response = agent.chat("Go to github.com and find trending repos")
print(response)

# Multi-turn
response = agent.chat("Now check the first one and summarize the README")
print(response)

# Reset conversation
agent.reset()
```

---

## CLI Reference

### Agent Commands

| Command | Description |
|---------|-------------|
| `bpy setup` | Configure LLM provider, workspace, browser |
| `bpy agent [message]` | Chat with the agent (interactive or one-shot) |
| `bpy serve [--port 8321]` | Start the web UI |

### Browser Commands

| Command | Description |
|---------|-------------|
| `bpy launch [name]` | Start Chrome with a named profile |
| `bpy launch new [name]` | Create a new profile |
| `bpy launch list` | List all profiles |
| `bpy launch --default <name>` | Set the default profile |

### Navigation

| Command | Description |
|---------|-------------|
| `bpy open <url>` | Navigate to URL |
| `bpy url` | Print current URL |
| `bpy back` / `forward` / `refresh` | History navigation |

### Interaction

| Command | Description |
|---------|-------------|
| `bpy elements [selector]` | List interactive elements (numbered) |
| `bpy click <index>` | Click element by number |
| `bpy type <index> <text>` | Type into element |
| `bpy upload <path> [selector]` | Upload file |

### Content

| Command | Description |
|---------|-------------|
| `bpy text [selector]` | Extract visible text |
| `bpy html <selector>` | Get element HTML |
| `bpy eval <js>` | Run JavaScript |
| `bpy screenshot [path]` | Save screenshot |

### Other

| Command | Description |
|---------|-------------|
| `bpy tabs` / `tab <n>` / `newtab` / `close` | Tab management |
| `bpy scroll <dir> [px]` | Scroll the page |
| `bpy wait <ms>` | Wait (for scripts) |

---

## Profiles

Each profile is a separate Chrome session with its own logins, cookies, and CDP port.

```bash
bpy launch                  # Default profile (port 9222)
bpy launch new work         # Create "work" (port 9223)
bpy launch work             # Launch it
bpy launch list             # See all profiles
bpy launch --default work   # Set default
bpy launch delete old       # Remove a profile

# Run multiple simultaneously
bpy launch                  # Terminal 1: default on 9222
bpy launch work             # Terminal 2: work on 9223
CDP_URL=http://127.0.0.1:9223 bpy tabs   # Control work profile
```

Profiles live at `~/.browser-py/profiles/<name>/`. Config at `~/.browser-py/config.json`.

---

## Shadow DOM Support

browser-py automatically pierces shadow DOM boundaries. No configuration needed.

```bash
bpy open reddit.com
bpy elements        # Finds elements inside shadow roots
bpy click 5         # Works normally
```

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CDP_URL` | CDP endpoint URL | `http://127.0.0.1:9222` |
| `NO_COLOR` | Disable colored output | (unset) |
| `ANTHROPIC_API_KEY` | Anthropic/Claude Max key | (from config) |
| `OPENROUTER_API_KEY` | OpenRouter key | (from config) |
| `OPENAI_API_KEY` | OpenAI key | (from config) |

---

## FAQ

**Q: What's the difference between `bpy agent` and `bpy` commands?**
`bpy agent` talks to an LLM that decides what to do. `bpy click 3` directly executes a browser command. Use agent mode for complex multi-step tasks; use direct commands for scripting.

**Q: Can I use my Claude Max subscription instead of paying per-API-call?**
Yes. Choose "Claude Max (OAuth)" during `bpy setup` and paste your OAuth token (`sk-ant-oat01-...`). Same token Claude Code uses.

**Q: Do I need to log in every time?**
No. Log in once during your first `bpy launch`. Sessions persist in the profile directory.

**Q: What browsers are supported?**
Chrome, Chromium, Brave, Microsoft Edge — anything Chromium-based with CDP support.

**Q: Does it work headless?**
Yes. `bpy launch --headless` runs without a visible window. Log in with a visible window first to set up sessions.

**Q: Is my data safe?**
File operations are sandboxed to your workspace directory. The agent cannot access files outside it. Shell access can be disabled. API keys are stored locally in `~/.browser-py/config.json`.

**Q: How is this different from Selenium/Playwright?**

| | browser-py | Selenium | Playwright |
|---|:---:|:---:|:---:|
| Session reuse | ✅ | ❌ | Partial |
| AI agent | ✅ | ❌ | ❌ |
| Shadow DOM | ✅ | ❌ | ❌ |
| Dependencies | 1 (core) | Heavy | Heavy |
| Install size | ~100KB | ~50MB | ~200MB+ |

---

## Architecture

```
browser-py/
├── browser_py/
│   ├── core.py                 # CDP engine (Phase 1)
│   ├── cli.py                  # bpy CLI
│   ├── profiles.py             # Named profile management
│   ├── js_expressions.py       # Injected JS for element scanning
│   ├── agent/
│   │   ├── loop.py             # Agentic while-loop (LiteLLM)
│   │   ├── config.py           # Provider/workspace/model config
│   │   ├── setup.py            # Interactive setup wizard
│   │   └── tools/
│   │       ├── browser.py      # Browser tool (wraps core.py)
│   │       ├── files.py        # Sandboxed file ops
│   │       ├── pdf.py          # PDF read (PyMuPDF) + create (WeasyPrint)
│   │       ├── spreadsheet.py  # CSV + Excel (openpyxl)
│   │       ├── shell.py        # Sandboxed shell execution
│   │       └── cron.py         # APScheduler cron jobs
│   └── server/
│       └── app.py              # FastAPI web UI + API
└── pyproject.toml
```

---

## License

MIT
