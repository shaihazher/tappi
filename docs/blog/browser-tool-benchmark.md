# I Benchmarked 4 Browser Automation Tools with AI Agents. Here's What Actually Happened.

*How tappi, OpenClaw's browser tool, Playwright, and playwright-cli compare when AI agents drive the browser.*

---

Every AI agent framework eventually needs to browse the web. Send an email. Scrape data. Fill a form. The question isn't *whether* your agent needs a browser — it's *which browser tool burns the fewest tokens, completes the fastest, and actually works*.

I ran a controlled experiment: **4 AI agents, 4 different browser tools, 3 real-world tasks**. Same model, same thinking level, same instructions. The results weren't even close.

## What Are These Tools?

Before we dive in, let's set the stage. These are the four browser automation tools I tested, each representing a fundamentally different approach to letting AI agents control a browser.

### tappi

[tappi](https://github.com/shaihazher/tappi) is a lightweight CDP (Chrome DevTools Protocol) browser control tool designed specifically for AI agents. Available as both a Python package (`pip install tappi`) and a CLI (`tappi` / `bjs`), it connects to an already-running Chrome instance via CDP and exposes simple shell commands: `tappi open "url"`, `tappi elements`, `tappi click 5`, `tappi type "hello"`, `tappi text`.

The key design philosophy: **give the agent only what it needs**. Instead of dumping an entire DOM or accessibility tree into the LLM's context, tappi returns compact, indexed element lists. It also pierces shadow DOM boundaries — critical for modern web apps like Reddit, Gmail, and GitHub that use web components extensively.

Because it connects to your existing Chrome via CDP, it inherits all your signed-in sessions, cookies, and extensions. No fresh browser. No login walls.

### OpenClaw Browser Tool

[OpenClaw](https://openclaw.ai) is an AI agent orchestration platform. Its built-in browser tool uses Playwright under the hood to capture full ARIA (accessibility) tree snapshots of web pages. The agent calls it as an MCP tool — `browser navigate`, `browser snapshot`, `browser act` — and gets back a structured representation of the page.

Like tappi, it connects to an existing Chrome profile, so the agent has access to signed-in sessions. The tradeoff: ARIA snapshots are comprehensive but *massive*. A single Reddit page can produce 50K+ tokens of snapshot data.

### Playwright (scripting)

[Playwright](https://playwright.dev/) is Microsoft's popular browser automation framework. In this benchmark, the agent uses Playwright the traditional way: it writes a complete Node.js script using Playwright's API (`chromium.launch()`, `page.goto()`, `page.locator()`, etc.), then executes it.

The agent has to reason about the entire script upfront, launch a fresh browser instance (no saved cookies or sessions), and hope the script works on the first try. There's no interactive feedback loop — if the page doesn't look like what the agent expected, it finds out only after the script fails.

### playwright-cli

[@playwright/cli](https://github.com/microsoft/playwright-mcp) is Microsoft's new command-line tool, released in early 2026 as a companion to Playwright MCP. It's designed specifically for AI coding agents: instead of writing scripts, the agent calls shell commands like `playwright-cli open "url"`, `playwright-cli snapshot`, `playwright-cli click e5`.

The philosophy is similar to tappi — compact commands, YAML-based snapshots — but it launches its own browser instance (no persistent sessions) and runs headless Chrome by default. It was built to reduce token usage compared to Playwright MCP's full accessibility tree dumps.

---

## The Experiment Setup

**Model:** Claude Sonnet 4.6  
**Thinking:** Medium  
**Orchestrator:** OpenClaw (spawned isolated sub-agents for each run)

| Tool | Approach | Session Access |
|------|----------|---------------|
| **tappi** | CDP shell commands (`tappi open`, `tappi elements`, `tappi click`) | ✅ Existing Chrome profile (signed in) |
| **OpenClaw Browser Tool** | Built-in ARIA snapshots via MCP | ✅ Existing Chrome profile (signed in) |
| **Playwright** (scripting) | Agent writes & executes Node.js scripts | ❌ Fresh browser, no cookies |
| **playwright-cli** (@playwright/cli) | Shell commands (`playwright-cli open`, `playwright-cli snapshot`) | ❌ Fresh browser, no cookies |

**Key constraint:** Each agent was *forbidden* from switching tools. If their assigned tool couldn't do the job, they reported failure. No bailouts.

### The Tasks

1. **Reddit Data Extraction** — Go to r/LocalLLaMA, extract top 5 posts this week with titles, upvotes, and top comments
2. **Google Maps Lead Generation** — Find top 5 plumbers in Houston TX with name, rating, phone, and address
3. **Gmail Email Sending** — Navigate Gmail's compose flow, send an email to two recipients

These aren't toy benchmarks. Reddit has aggressive bot detection. Google Maps has complex interactive UI. Gmail requires authentication and has one of the most intricate DOMs on the web.

---

## The Results

### Task 1: Reddit (r/LocalLLaMA Top Posts)

```
Context Tokens Used (lower is better)

tappi         ████████████ 21K
browser tool  ████████████████████████████████████████████████████████████ 118K  ← 5.6x more
playwright    ████████ 14K
playwright-cli████████████ 21K (FAILED)
```

| Tool | Context | Time | Success | Notes |
|------|---------|------|---------|-------|
| **tappi** | 21K | 1m52s | ✅ | Skipped bot comments, got real top comments with authors & upvotes |
| **Browser tool** | **118K** | 3m00s | ✅ | Same quality, but each page snapshot was enormous |
| **Playwright** | 14K | 1m02s | ✅* | Captured automod bot comments instead of real top comments (4/5 posts) |
| **playwright-cli** | 21K | 2m22s | ❌ | Reddit's reCAPTCHA blocked headless Chrome entirely |

**What happened:** The browser tool returned the same quality data as tappi but burned **5.6x more tokens** doing it. Every Reddit page snapshot injected tens of thousands of tokens of DOM data into the model's context. tappi returned compact, actionable element lists.

Playwright scripting technically succeeded but with poor quality — it grabbed whatever was first in the DOM (bot comments), while tappi was able to interactively evaluate comment scores and pick the top *human* comment.

playwright-cli was blocked cold. Reddit detected headless Chrome and served a visual CAPTCHA ("Select all images with crosswalks"). The agent tried `old.reddit.com`, the JSON API, everything — all blocked.

**Interesting:** Playwright *scripting* got through Reddit's bot detection, but playwright-cli didn't. Same underlying browser engine, different fingerprints. The lesson: *how* you launch Chromium matters.

### Task 2: Google Maps (Lead Generation)

```
Context Tokens Used

tappi         ██████████ 16K
browser tool  █████████████ 21K
playwright    ████████████ 18K
playwright-cli████████████ 20K
```

| Tool | Context | Time | Success | Notes |
|------|---------|------|---------|-------|
| **tappi** | 16K | 59s | ✅ | 3 commands total |
| **Browser tool** | 21K | **38s** | ✅ | Single ARIA snapshot captured everything |
| **Playwright** | 18K | 2m34s | ✅ | Script worked but slower to execute |
| **playwright-cli** | 20K | 42s | ✅ | 2 commands: `open` + `snapshot` |

**The equalizer.** Google Maps was the one task where all four tools performed comparably. The page renders all business data in a single view — no pagination, no clicking into individual results. A single snapshot (whether ARIA tree or DOM) captured everything.

The browser tool actually had its best showing here — one snapshot, all data extracted, 38 seconds. playwright-cli was equally elegant: just `open` + `snapshot`, done in 42 seconds.

tappi was slightly more token-efficient (16K vs 20K) because its element output is more compact than a full ARIA tree, but the gap was narrow.

**The takeaway:** When the data is all on one page, the tool differences shrink dramatically. The real differentiation happens on *multi-step, interactive* tasks.

### Task 3: Gmail (Send Email)

```
Context Tokens Used

tappi         ██████████████ 22K
browser tool  ████████████████████████████████████████████████████████████ 113K  ← 5.1x more
playwright    ████████ 12K (FAILED - no auth)
playwright-cli███████ 11K (FAILED - no auth)
```

| Tool | Context | Time | Success | Notes |
|------|---------|------|---------|-------|
| **tappi** | 22K | 1m22s | ✅ | 8 tool calls: open → compose → fill → send |
| **Browser tool** | **113K** | **5m+** | ✅* | Gmail's compose dialog invisible to ARIA tree. Required URL-based workaround |
| **Playwright** | 12K | 26s | ❌ | Redirected to Google sign-in. No auth, no access |
| **playwright-cli** | 11K | 32s | ❌ | Same — redirected to sign-in |

**This is where the story gets real.**

Gmail's floating compose window is a **shadow DOM nightmare**. The browser tool's ARIA snapshots couldn't even *see* the compose dialog — it doesn't appear in the accessibility tree. The agent spent 5+ minutes trying DOM evaluation workarounds before discovering that Gmail's URL-based compose (`?view=cm&to=...&su=...`) renders a full-page form that *is* accessible.

tappi handled it in 8 tool calls because it works at the DOM level, piercing shadow DOM boundaries. Click compose → type recipient → tab → type recipient → fill subject → fill body → click send. Clean, direct, 82 seconds.

Playwright and playwright-cli failed instantly — they launch fresh browsers with no authentication. Google redirected them to sign-in. This is the **persistent session advantage**: tools that integrate with your existing browser profile (tappi, OpenClaw browser tool) can access any service you're already signed into. Fresh-browser tools can't.

---

## The Big Picture

### Total Context Burned (All 3 Tasks)

```
tappi          ██████████████████ 59K tokens
playwright     █████████████ 44K tokens (but 2/3 success)
playwright-cli ████████████████ 52K tokens (but 1/3 success)
browser tool   ████████████████████████████████████████████████████████████████████████████ 252K tokens
```

### Success Rate vs Token Efficiency

| Tool | Success Rate | Total Context | Total Time | Verdict |
|------|-------------|---------------|------------|---------|
| **tappi** | **3/3 (100%)** | **59K** | 4m13s | 🏆 Best overall — highest success, low tokens |
| **Browser tool** | 3/3 (100%) | 252K | 8m38s | ✅ Reliable but **4.3x more tokens** than tappi |
| **Playwright** | 2/3 (67%) | 44K | 3m42s | ⚠️ Cheap but fails on auth + poor quality |
| **playwright-cli** | 1/3 (33%) | 52K | 3m36s | ❌ Blocked by bot detection + no auth |

### What This Means for Agent Builders

**1. Token efficiency is not about the model — it's about the tool.**

Same model, same thinking level, same instructions. The only variable was the browser tool. tappi used 59K tokens total. The browser tool used 252K. That's a **4.3x difference** that compounds with every interaction in a long-running agent session.

**2. Persistent sessions are non-negotiable for real-world tasks.**

Playwright and playwright-cli launch clean browsers. That means no cookies, no auth, no session state. Every Google service, every authenticated SaaS tool, every site that remembers you — inaccessible. In our test, this caused 3 out of 6 Playwright/playwright-cli runs to fail outright.

Tools that piggyback on your existing Chrome profile (tappi via CDP, OpenClaw's browser tool) inherit all your signed-in sessions. This isn't a nice-to-have — it's the difference between an agent that can *actually do things* and one that gets stopped at the login page.

**3. Bot detection kills headless browsers.**

Reddit blocked playwright-cli's headless Chrome with a visual CAPTCHA. Google Maps worked (no bot detection on Maps), but Reddit was ruthless. Interestingly, Playwright *scripting* got through while playwright-cli didn't — suggesting that the CLI's default browser configuration has a more detectable fingerprint.

tappi and the OpenClaw browser tool? They run inside a real, headed Chrome instance. No headless detection. No CAPTCHA walls.

**4. Interactive refinement beats one-shot scripts.**

Playwright's approach is "write a script, run it, hope it works." When it works, it's fast and cheap (14K tokens for Reddit). But it captured bot comments instead of real ones because there was no opportunity to inspect, evaluate, and refine.

tappi and the browser tool operate interactively — the agent sees elements, makes decisions, and adjusts. tappi specifically evaluated comment scores and chose the top *human* comment, producing higher quality output.

**5. DOM complexity is the real battleground.**

Google Maps? All tools performed similarly — the data was right there.  
Reddit? Shadow DOM elements, dynamic loading, complex comment trees — the gap widened.  
Gmail? Shadow DOM compose dialogs, chip-based recipient fields, nested iframes — the browser tool needed 113K tokens and a workaround. tappi handled it in 22K.

The more complex the DOM, the more tool choice matters.

---

## Methodology Notes

- All agents were spawned as isolated [OpenClaw](https://openclaw.ai) sub-agent sessions
- Each agent received identical task instructions with explicit tool restrictions
- Token counts reflect the session's total context usage (including tool responses)
- Time measured from session spawn to result file creation
- All runs used the same Claude Sonnet 4.6 model with `thinking: medium`
- Results files written to `/tmp/benchmark/` as structured JSON
- No manual intervention during any run

---

## Try It Yourself

**tappi** is open source and available on PyPI:

```bash
pip install tappi        # core
pip install tappi[agent] # with AI agent capabilities
```

Or use the CLI directly:

```bash
tappi open "https://example.com"
tappi elements
tappi click 3
tappi text
```

- 📦 [PyPI: tappi](https://pypi.org/project/tappi/)
- 🐙 [GitHub: shaihazher/tappi](https://github.com/shaihazher/tappi)
- 📖 [Documentation](https://github.com/shaihazher/tappi#readme)

---

*Built with [OpenClaw](https://openclaw.ai) and [tappi](https://github.com/shaihazher/tappi). The experiment ran on a MacBook Pro with Chrome 145.*

*Have your own benchmark results? I'd love to see them. Open an [issue](https://github.com/shaihazher/tappi/issues) or find me on [dev.to](https://dev.to).*
