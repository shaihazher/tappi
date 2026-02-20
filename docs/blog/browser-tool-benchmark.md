# I Benchmarked 4 Browser Automation Tools with AI Agents. Here's What Actually Happened.

*How bjs, OpenClaw's browser tool, Playwright, and playwright-cli compare when AI agents drive the browser.*

---

Every AI agent framework eventually needs to browse the web. Send an email. Scrape data. Fill a form. The question isn't *whether* your agent needs a browser — it's *which browser tool burns the fewest tokens, completes the fastest, and actually works*.

I ran a controlled experiment: **4 AI agents, 4 different browser tools, 3 real-world tasks**. Same model, same thinking level, same instructions. The results weren't even close.

## The Experiment Setup

**Model:** Claude Sonnet 4.6  
**Thinking:** Medium  
**Orchestrator:** OpenClaw (spawned isolated sub-agents for each run)

### The Tools

| Tool | Approach | Session Access |
|------|----------|---------------|
| **bjs** ([browser.js](https://github.com/shaihazher/browser-js)) | CDP shell commands (`bjs open`, `bjs elements`, `bjs click`) | ✅ Existing Chrome profile (signed in) |
| **OpenClaw Browser Tool** | Built-in DOM/aria snapshots via MCP | ✅ Existing Chrome profile (signed in) |
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

bjs           ████████████ 21K
browser tool  ████████████████████████████████████████████████████████████ 118K  ← 5.6x more
playwright    ████████ 14K
playwright-cli████████████ 21K (FAILED)
```

| Tool | Context | Time | Success | Notes |
|------|---------|------|---------|-------|
| **bjs** | 21K | 1m52s | ✅ | Skipped bot comments, got real top comments with authors & upvotes |
| **Browser tool** | **118K** | 3m00s | ✅ | Same quality, but each page snapshot was enormous |
| **Playwright** | 14K | 1m02s | ✅* | Captured automod bot comments instead of real top comments (4/5 posts) |
| **playwright-cli** | 21K | 2m22s | ❌ | Reddit's reCAPTCHA blocked headless Chrome entirely |

**What happened:** The browser tool returned the same quality data as bjs but burned **5.6x more tokens** doing it. Every Reddit page snapshot injected tens of thousands of tokens of DOM data into the model's context. bjs returned compact, actionable element lists.

Playwright scripting technically succeeded but with poor quality — it grabbed whatever was first in the DOM (bot comments), while bjs was able to interactively evaluate comment scores and pick the top *human* comment.

playwright-cli was blocked cold. Reddit detected headless Chrome and served a visual CAPTCHA ("Select all images with crosswalks"). The agent tried `old.reddit.com`, the JSON API, everything — all blocked.

**Interesting:** Playwright *scripting* got through Reddit's bot detection, but playwright-cli didn't. Same underlying browser engine, different fingerprints. The lesson: *how* you launch Chromium matters.

### Task 2: Google Maps (Lead Generation)

```
Context Tokens Used

bjs           ██████████ 16K
browser tool  █████████████ 21K
playwright    ████████████ 18K
playwright-cli████████████ 20K
```

| Tool | Context | Time | Success | Notes |
|------|---------|------|---------|-------|
| **bjs** | 16K | 59s | ✅ | 3 commands total |
| **Browser tool** | 21K | **38s** | ✅ | Single ARIA snapshot captured everything |
| **Playwright** | 18K | 2m34s | ✅ | Script worked but slower to execute |
| **playwright-cli** | 20K | 42s | ✅ | 2 commands: `open` + `snapshot` |

**The equalizer.** Google Maps was the one task where all four tools performed comparably. The page renders all business data in a single view — no pagination, no clicking into individual results. A single snapshot (whether ARIA tree or DOM) captured everything.

The browser tool actually had its best showing here — one snapshot, all data extracted, 38 seconds. playwright-cli was equally elegant: just `open` + `snapshot`, done in 42 seconds.

bjs was slightly more token-efficient (16K vs 20K) because its element output is more compact than a full ARIA tree, but the gap was narrow.

**The takeaway:** When the data is all on one page, the tool differences shrink dramatically. The real differentiation happens on *multi-step, interactive* tasks.

### Task 3: Gmail (Send Email)

```
Context Tokens Used

bjs           ██████████████ 22K
browser tool  ████████████████████████████████████████████████████████████ 113K  ← 5.1x more
playwright    ████████ 12K (FAILED - no auth)
playwright-cli███████ 11K (FAILED - no auth)
```

| Tool | Context | Time | Success | Notes |
|------|---------|------|---------|-------|
| **bjs** | 22K | 1m22s | ✅ | 8 tool calls: open → compose → fill → send |
| **Browser tool** | **113K** | **5m+** | ✅* | Gmail's compose dialog invisible to ARIA tree. Required URL-based workaround |
| **Playwright** | 12K | 26s | ❌ | Redirected to Google sign-in. No auth, no access |
| **playwright-cli** | 11K | 32s | ❌ | Same — redirected to sign-in |

**This is where the story gets real.**

Gmail's floating compose window is a **shadow DOM nightmare**. The browser tool's ARIA snapshots couldn't even *see* the compose dialog — it doesn't appear in the accessibility tree. The agent spent 5+ minutes trying DOM evaluation workarounds before discovering that Gmail's URL-based compose (`?view=cm&to=...&su=...`) renders a full-page form that *is* accessible.

bjs handled it in 8 tool calls because it works at the DOM level, piercing shadow DOM boundaries. Click compose → type recipient → tab → type recipient → fill subject → fill body → click send. Clean, direct, 82 seconds.

Playwright and playwright-cli failed instantly — they launch fresh browsers with no authentication. Google redirected them to sign-in. This is the **persistent session advantage**: tools that integrate with your existing browser profile (bjs, OpenClaw browser tool) can access any service you're already signed into. Fresh-browser tools can't.

---

## The Big Picture

### Total Context Burned (All 3 Tasks)

```
bjs            ██████████████████ 59K tokens
playwright     █████████████ 44K tokens (but 2/3 success)
playwright-cli ████████████████ 52K tokens (but 1/3 success)
browser tool   ████████████████████████████████████████████████████████████████████████████ 252K tokens
```

### Success Rate vs Token Efficiency

| Tool | Success Rate | Total Context | Total Time | Verdict |
|------|-------------|---------------|------------|---------|
| **bjs** | **3/3 (100%)** | **59K** | 4m13s | 🏆 Best overall — highest success, low tokens |
| **Browser tool** | 3/3 (100%) | 252K | 8m38s | ✅ Reliable but **4.3x more tokens** than bjs |
| **Playwright** | 2/3 (67%) | 44K | 3m42s | ⚠️ Cheap but fails on auth + poor quality |
| **playwright-cli** | 1/3 (33%) | 52K | 3m36s | ❌ Blocked by bot detection + no auth |

### What This Means for Agent Builders

**1. Token efficiency is not about the model — it's about the tool.**

Same model, same thinking level, same instructions. The only variable was the browser tool. bjs used 59K tokens total. The browser tool used 252K. That's a **4.3x difference** that compounds with every interaction in a long-running agent session.

**2. Persistent sessions are non-negotiable for real-world tasks.**

Playwright and playwright-cli launch clean browsers. That means no cookies, no auth, no session state. Every Google service, every authenticated SaaS tool, every site that remembers you — inaccessible. In our test, this caused 3 out of 6 Playwright/playwright-cli runs to fail outright.

Tools that piggyback on your existing Chrome profile (bjs via CDP, OpenClaw's browser tool) inherit all your signed-in sessions. This isn't a nice-to-have — it's the difference between an agent that can *actually do things* and one that gets stopped at the login page.

**3. Bot detection kills headless browsers.**

Reddit blocked playwright-cli's headless Chrome with a visual CAPTCHA. Google Maps worked (no bot detection on Maps), but Reddit was ruthless. Interestingly, Playwright *scripting* got through while playwright-cli didn't — suggesting that the CLI's default browser configuration has a more detectable fingerprint.

bjs and the OpenClaw browser tool? They run inside a real, headed Chrome instance. No headless detection. No CAPTCHA walls.

**4. Interactive refinement beats one-shot scripts.**

Playwright's approach is "write a script, run it, hope it works." When it works, it's fast and cheap (14K tokens for Reddit). But it captured bot comments instead of real ones because there was no opportunity to inspect, evaluate, and refine.

bjs and the browser tool operate interactively — the agent sees elements, makes decisions, and adjusts. bjs specifically evaluated comment scores and chose the top *human* comment, producing higher quality output.

**5. DOM complexity is the real battleground.**

Google Maps? All tools performed similarly — the data was right there.  
Reddit? Shadow DOM elements, dynamic loading, complex comment trees — the gap widened.  
Gmail? Shadow DOM compose dialogs, chip-based recipient fields, nested iframes — the browser tool needed 113K tokens and a workaround. bjs handled it in 22K.

The more complex the DOM, the more tool choice matters.

---

## Methodology Notes

- All agents were spawned as isolated OpenClaw sub-agent sessions
- Each agent received identical task instructions with explicit tool restrictions
- Token counts reflect the session's total context usage (including tool responses)
- Time measured from session spawn to result file creation
- All runs used the same Claude Sonnet 4.6 model with `thinking: medium`
- Results files written to `/tmp/benchmark/` as structured JSON
- No manual intervention during any run

## The Tools

- **bjs (browser.js)**: Open source CDP-based browser control CLI. [GitHub](https://github.com/shaihazher/browser-js) | [ClawHub](https://clawhub.com)
- **tappi**: Python equivalent with AI agent capabilities. [PyPI](https://pypi.org/project/tappi/) | [GitHub](https://github.com/shaihazher/tappi)
- **OpenClaw Browser Tool**: Built-in MCP tool in OpenClaw, uses Playwright under the hood for DOM snapshots
- **Playwright**: Microsoft's browser automation framework. Full scripting API.
- **playwright-cli** (@playwright/cli): Microsoft's new CLI wrapper, designed for token efficiency with AI coding agents

---

*Built with [OpenClaw](https://openclaw.ai) and [tappi](https://github.com/shaihazher/tappi). The experiment ran on a MacBook Pro with Chrome 145.*

*Have your own benchmark results? I'd love to see them. [GitHub Issues](https://github.com/shaihazher/tappi/issues) or find me on [dev.to](https://dev.to/shaihazher).*
