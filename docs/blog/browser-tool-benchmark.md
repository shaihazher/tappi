# I Benchmarked 4 Browser Automation Tools with AI Agents. Here's What Actually Happened.

*tappi, OpenClaw's browser tool, Playwright, and playwright-cli walk into a bar. Only one walks out with a perfect score.*

---

Every AI agent framework eventually needs to browse the web. Send an email. Scrape data. Fill a form. The question isn't *whether* your agent needs a browser — it's *which browser tool burns the fewest tokens, completes the fastest, and actually works*.

I ran a controlled experiment: **4 AI agents, 4 different browser tools, 3 real-world tasks**. Same model, same thinking level, same instructions. The results weren't even close.

## What Are These Tools?

Before we dive in, let's set the stage. These are the four browser automation tools I tested, each representing a fundamentally different approach to letting AI agents control a browser.

### 🔹 tappi

[tappi](https://github.com/shaihazher/tappi) is a lightweight CDP (Chrome DevTools Protocol) browser control tool designed specifically for AI agents. Available as both a Python package (`pip install tappi`) and a CLI (`tappi` / `bjs`), it connects to an already-running Chrome instance via CDP and exposes simple shell commands: `tappi open "url"`, `tappi elements`, `tappi click 5`, `tappi type "hello"`, `tappi text`.

The key design philosophy: **give the agent only what it needs**. Instead of dumping an entire DOM or accessibility tree into the LLM's context, tappi returns compact, indexed element lists. It also pierces shadow DOM boundaries — critical for modern web apps like Reddit, Gmail, and GitHub that use web components extensively.

Because it connects to your existing Chrome via CDP, it inherits all your signed-in sessions, cookies, and extensions. No fresh browser. No login walls.

### 🔸 OpenClaw Browser Tool

[OpenClaw](https://openclaw.ai) is an AI agent orchestration platform. Its built-in browser tool uses Playwright under the hood to capture full ARIA (accessibility) tree snapshots of web pages. The agent calls it as an MCP tool — `browser navigate`, `browser snapshot`, `browser act` — and gets back a structured representation of the page.

Like tappi, it connects to an existing Chrome profile, so the agent has access to signed-in sessions. The tradeoff: ARIA snapshots are comprehensive but *massive*. A single Reddit page can produce 50K+ tokens of snapshot data.

### 🔷 Playwright (scripting)

[Playwright](https://playwright.dev/) is Microsoft's popular browser automation framework. In this benchmark, the agent uses Playwright the traditional way: it writes a complete Node.js script using Playwright's API (`chromium.launch()`, `page.goto()`, `page.locator()`, etc.), then executes it.

The agent has to reason about the entire script upfront, launch a fresh browser instance (no saved cookies or sessions), and hope the script works on the first try. There's no interactive feedback loop — if the page doesn't look like what the agent expected, it finds out only after the script fails.

### 🔶 playwright-cli

[@playwright/cli](https://github.com/microsoft/playwright-mcp) is Microsoft's new command-line tool, released in early 2026 as a companion to Playwright MCP. It's designed specifically for AI coding agents: instead of writing scripts, the agent calls shell commands like `playwright-cli open "url"`, `playwright-cli snapshot`, `playwright-cli click e5`.

The philosophy is similar to tappi — compact commands, YAML-based snapshots — but it launches its own browser instance (no persistent sessions) and runs headless Chrome by default. It was built to reduce token usage compared to Playwright MCP's full accessibility tree dumps.

---

## The Experiment Setup

**Model:** Claude Sonnet 4.6  
**Thinking:** Medium  
**Orchestrator:** OpenClaw (spawned isolated sub-agents for each run)

| Tool | Approach | Session Access |
|------|----------|---------------|
| **tappi** 🔹 | CDP shell commands | ✅ Existing Chrome profile (signed in) |
| **OpenClaw Browser Tool** 🔸 | Built-in ARIA snapshots via MCP | ✅ Existing Chrome profile (signed in) |
| **Playwright** 🔷 | Agent writes & executes Node.js scripts | ❌ Fresh browser, no cookies |
| **playwright-cli** 🔶 | Shell commands + YAML snapshots | ❌ Fresh browser, no cookies |

**Key constraint:** Each agent was *forbidden* from switching tools. If their assigned tool couldn't do the job, they reported failure. No bailouts.

---

## The Results

### Task 1: Reddit Data Extraction

**The task:** Navigate to Reddit's r/LocalLLaMA subreddit, find the top 5 posts from the past week, and for each post extract the title, upvote count, and the text of the top comment. This requires navigating to the subreddit, parsing a dynamic listing page, then clicking into each individual post to read its comments — a multi-step browsing workflow across 6 pages total.

**What actually happened:**

🔹 **tappi** opened the subreddit, ran a JavaScript query to pull all post titles and upvote counts in one shot, then visited each post individually. On every post, it evaluated comment scores via the DOM and deliberately *skipped* the automod bot comment to surface the highest-voted *human* comment — complete with author name and score. 8 tool calls. Done in under 2 minutes.

🔸 **The browser tool** followed a similar strategy but each page navigation produced a full ARIA tree snapshot — tens of thousands of tokens per page. Reddit's deeply nested shadow DOM (`<shreddit-comment>` web components) made these snapshots enormous. Same quality result, but at **5.6x the token cost**.

🔷 **Playwright** wrote a single Node.js script, launched a fresh headless Chromium, and executed it. Clever shortcut: it used `old.reddit.com` (simpler DOM). Fast and cheap — but it blindly grabbed the first comment on each post, which turned out to be an automod bot message on 4 out of 5 posts. No opportunity to inspect and adjust.

🔶 **playwright-cli** never got past the front door. Reddit detected the headless Chrome fingerprint and served a visual reCAPTCHA: *"Select all images with crosswalks."* The agent tried `old.reddit.com` — blocked. Tried the JSON API — blocked. Every endpoint returned the same wall.

<!-- Chart: Reddit Token Usage -->
> **Context tokens burned:**
>
> 🔹 tappi ·········· **21K** ✅
> 🔸 browser tool ·················································· **118K** ✅ *(5.6× more)*
> 🔷 playwright ········ **14K** ⚠️ *(wrong data)*
> 🔶 playwright-cli ·········· **21K** ❌ *(CAPTCHA blocked)*

| Tool | Context | Time | Result |
|------|---------|------|--------|
| 🔹 **tappi** | 21K | 1m52s | ✅ Correct data, real human comments |
| 🔸 **Browser tool** | 118K | 3m00s | ✅ Correct data, massive token cost |
| 🔷 **Playwright** | 14K | 1m02s | ⚠️ Wrong data — captured bot comments on 4/5 posts |
| 🔶 **playwright-cli** | 21K | 2m22s | ❌ Blocked by Reddit's bot detection |

**The insight:** Playwright *scripting* got through Reddit's bot detection but playwright-cli didn't — same underlying browser engine, different fingerprints. *How* you launch Chromium matters. And tappi's interactive approach (inspect → evaluate → decide) produced fundamentally better output than Playwright's one-shot "write script, pray it works" approach.

---

### Task 2: Google Maps Lead Generation

**The task:** Search Google Maps for "plumbers in Houston TX" and extract the top 5 organic results with business name, star rating, phone number, and street address. This is the kind of lead generation task that people pay Zapier and n8n real money for — and it's a single-page extraction, so the playing field should be level.

**What actually happened:**

🔹 **tappi** opened Google Maps, called `elements` to get the listing links, then used `text` to extract all visible business data in one pass. Three commands. Under a minute.

🔸 **The browser tool** took a single ARIA snapshot and — to its credit — had everything it needed in that one snapshot. All business names, ratings, phone numbers, and addresses. Its fastest run of the day: 38 seconds.

🔷 **Playwright** wrote a scraping script that launched Chromium, navigated to Maps, and parsed the page. It worked, but took 2.5 minutes because the agent had to reason about the script, handle page load timing, and deal with Google's dynamic rendering.

🔶 **playwright-cli** did what it was built for — `open` + `snapshot` — and had all the data in 42 seconds. Clean, efficient, and proof that Google Maps doesn't have the same bot detection as Reddit.

> **Context tokens burned:**
>
> 🔹 tappi ·········· **16K** ✅
> 🔸 browser tool ············· **21K** ✅
> 🔷 playwright ············ **18K** ✅
> 🔶 playwright-cli ············ **20K** ✅

| Tool | Context | Time | Result |
|------|---------|------|--------|
| 🔹 **tappi** | 16K | 59s | ✅ Clean extraction, 3 commands |
| 🔸 **Browser tool** | 21K | 38s | ✅ Single snapshot, fastest run |
| 🔷 **Playwright** | 18K | 2m34s | ✅ Works, but slow script execution |
| 🔶 **playwright-cli** | 20K | 42s | ✅ 2 commands, elegant |

**The insight:** When the data is all on one page, tool differences shrink dramatically. Google Maps was the great equalizer. The real differentiation happens on *multi-step, interactive* tasks — which is exactly what most real-world agent work looks like.

---

### Task 3: Gmail — Send an Email

**The task:** Navigate to Gmail (already signed in on the host Chrome), click Compose, add two recipients (`info@houstoncatchmycall.com` and `aria@synthworx.com`), fill in the subject line and body, and click Send. This is the kind of task that separates a demo from a real agent — it requires authentication, a complex interactive UI, and precise multi-step form filling.

**What actually happened:**

🔹 **tappi** navigated to Gmail (already signed in via the shared Chrome session), clicked Compose, typed the first recipient, hit Tab, typed the second, filled in the subject and body, and clicked Send. Gmail confirmed: *"Message sent."* Eight tool calls, 82 seconds. The shadow DOM compose dialog? tappi pierced right through it.

🔸 **The browser tool** ran into a wall immediately. Gmail's floating compose window — a deeply nested shadow DOM dialog — was **invisible to the ARIA tree**. The agent couldn't see it, couldn't click into it, couldn't type in it. After 5 minutes of DOM evaluation workarounds and multiple screenshots, it discovered a creative hack: Gmail's URL-based compose (`?view=cm&to=...&su=...&body=...`) renders a *full-page* form that *is* accessible. It worked — email sent — but it burned **113K tokens** finding the workaround.

🔷 **Playwright** launched a fresh Chromium and navigated to `mail.google.com`. Google immediately redirected to the sign-in page. No cookies. No session. No email sent. Failure reported in 26 seconds.

🔶 **playwright-cli** hit the same wall. Fresh browser, no auth, redirected to sign-in. Failed in 32 seconds.

> **Context tokens burned:**
>
> 🔹 tappi ·············· **22K** ✅
> 🔸 browser tool ·················································· **113K** ✅ *(5.1× more, needed workaround)*
> 🔷 playwright ········ **12K** ❌ *(no auth)*
> 🔶 playwright-cli ······· **11K** ❌ *(no auth)*

| Tool | Context | Time | Result |
|------|---------|------|--------|
| 🔹 **tappi** | 22K | 1m22s | ✅ Email sent — 8 clean tool calls |
| 🔸 **Browser tool** | 113K | 5m35s | ✅ Email sent — but needed URL workaround for shadow DOM |
| 🔷 **Playwright** | 12K | 26s | ❌ No auth — redirected to Google sign-in |
| 🔶 **playwright-cli** | 11K | 32s | ❌ No auth — redirected to Google sign-in |

**The insight:** This task exposed two critical fault lines. First, **persistent sessions are non-negotiable** — without them, you can't access any authenticated service. Second, **shadow DOM piercing matters** — Gmail's compose dialog is invisible to accessibility-tree-based tools, but tappi works at the raw DOM level and handles it natively.

---

## The Big Picture

### Total Context Burned (All 3 Tasks Combined)

> 🔹 tappi ·················· **59K tokens**
> 🔷 playwright ············· **44K tokens** *(but only 1/3 tasks correct)*
> 🔶 playwright-cli ················ **52K tokens** *(but only 1/3 tasks succeeded)*
> 🔸 browser tool ·················································································· **252K tokens**

### The Final Scorecard

| | 🔹 tappi | 🔸 Browser Tool | 🔷 Playwright | 🔶 playwright-cli |
|--|---------|----------------|--------------|-------------------|
| **Success Rate** | 🟢 **3/3** | 🟢 3/3 | 🟡 1/3* | 🔴 1/3 |
| **Total Context** | **59K** | 252K | 44K | 52K |
| **Total Time** | 4m13s | 8m38s | 3m42s | 3m36s |
| **Auth Tasks** | ✅ | ✅ | ❌ | ❌ |
| **Bot Detection** | ✅ | ✅ | ✅ | ❌ |
| **Shadow DOM** | ✅ | ⚠️ Workaround | N/A | N/A |
| **Data Quality** | ⭐ High | ⭐ High | ⚠️ Low | N/A |
| **Verdict** | 🏆 **Best overall** | Reliable but heavy | Cheap but brittle | Too limited |

*\*Playwright's Reddit "success" returned automod bot comments instead of actual top comments on 4 out of 5 posts — functionally incorrect.*

---

## What This Means for Agent Builders

### 1. Token efficiency is not about the model — it's about the tool.

Same model, same thinking level, same instructions. The only variable was the browser tool. tappi used 59K tokens total. The browser tool used 252K. That's a **4.3x difference** — and it compounds with every interaction in a long-running agent session. Over a day of agent work, that's the difference between staying within your context window and hitting compaction.

### 2. Persistent sessions are non-negotiable for real-world tasks.

Playwright and playwright-cli launch clean browsers. No cookies, no auth, no session state. Every Google service, every authenticated SaaS tool, every site that remembers you — inaccessible. In our test, this caused **4 out of 6** Playwright/playwright-cli runs to either fail outright or return garbage data.

Tools that piggyback on your existing Chrome profile (tappi via CDP, OpenClaw's browser tool) inherit all your signed-in sessions. This isn't a nice-to-have — it's the difference between an agent that can *actually do things* and one that gets stopped at the login page.

### 3. Bot detection kills headless browsers.

Reddit blocked playwright-cli's headless Chrome with a visual CAPTCHA. Interestingly, Playwright *scripting* got through while playwright-cli didn't — same engine, different fingerprints. The lesson: default headless configurations get caught.

tappi and the OpenClaw browser tool run inside a real, headed Chrome instance. No headless detection. No CAPTCHA walls. No blocked endpoints.

### 4. Interactive refinement beats one-shot scripts.

Playwright's approach is "write a script, run it, hope it works." When it works, it's fast and cheap. But it captured bot comments instead of real ones on Reddit because there was no opportunity to inspect, evaluate, and refine.

tappi operates interactively — the agent sees elements, makes decisions, and adjusts in real time. On Reddit, it evaluated comment scores and chose the top *human* comment, producing fundamentally better output. **The cheapest tokens are the ones that get you wrong answers.**

### 5. Shadow DOM is the real battleground.

Google Maps? All tools performed similarly — simple page, simple DOM.  
Reddit? Shadow DOM web components, dynamic loading — the gap widened.  
Gmail? Shadow DOM compose dialogs, chip-based recipient fields — the browser tool needed 113K tokens and a URL hack. tappi handled it in 22K, natively.

Modern web apps are built on shadow DOM. Your browser tool either pierces it or it doesn't.

---

## Methodology Notes

- All agents were spawned as isolated [OpenClaw](https://openclaw.ai) sub-agent sessions
- Each agent received identical task instructions with explicit tool restrictions
- Token counts reflect the session's total context usage (including tool responses)
- Time measured from session spawn to result file creation
- All runs used the same Claude Sonnet 4.6 model with `thinking: medium`
- Results files written to `/tmp/benchmark/` as structured JSON
- No manual intervention during any run
- Full result data and agent transcripts available on request

---

## Try It Yourself

**tappi** is open source and available on PyPI:

```bash
pip install tappi        # core CDP browser control
pip install tappi[agent] # with AI agent capabilities
```

Or use the CLI directly:

```bash
tappi open "https://example.com"
tappi elements
tappi click 3
tappi text
```

Connect it to your existing Chrome session and give your AI agent the ability to browse the web the way you do — with all your sessions, cookies, and context intact.

- 📦 **PyPI:** [tappi](https://pypi.org/project/tappi/)
- 🐙 **GitHub:** [shaihazher/tappi](https://github.com/shaihazher/tappi)
- 📖 **Docs:** [README](https://github.com/shaihazher/tappi#readme)

---

*Built with [OpenClaw](https://openclaw.ai) and [tappi](https://github.com/shaihazher/tappi). The experiment ran on a MacBook Pro with Chrome 145.*

*Have your own benchmark results or want to challenge these numbers? Open an [issue](https://github.com/shaihazher/tappi/issues) — I'd love to see them.*
