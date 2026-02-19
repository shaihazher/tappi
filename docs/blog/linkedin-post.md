# LinkedIn Post — Tappi

---

Every AI browser agent today does one of two things:

Send a full screenshot → LLM squints at pixels → guesses coordinates.
Or dump the entire DOM → LLM reads 30,000 tokens of nested divs → reasons about what to click.

Both burn tokens. Both force the LLM to do heavy reasoning just to figure out what's on the page — before it even starts your task.

I built something different.

Tappi is a local AI agent that controls your real browser — but instead of screenshots or DOM dumps, it indexes every interactive element into a compact numbered list:

[0] (link) Homepage
[1] (button) Sign In
[2] (input) Search...

The LLM says "click 1." Done. 10x fewer tokens. Visibly faster decisions.

Here's why it matters:

🔒 It's local. Runs on your machine, in your browser, with your saved sessions. Data never leaves.

🚫 Zero ban risk. No scraping APIs. No proxies. Just normal browsing — done by an AI instead of you.

📦 Sandboxed. One browser. One folder. Your system stays untouched. Safe for work.

🛠️ Full toolkit. PDFs, spreadsheets, cron jobs, file management — all within the sandbox.

🤖 Any LLM. Claude, GPT-4, open-source via OpenRouter. Bring your own key — or your existing Claude Max subscription.

Three commands to get started:

pip install tappi[agent]
bpy setup
bpy agent "Check my Gmail for unread emails and summarize them"

It opens your Chrome, uses your saved login, reads your inbox, and reports back. No re-auth. No CAPTCHAs.

Who's this for? Anyone who uses a browser and is tired of repetitive manual work. Social media, email, research, data extraction — hand it off.

Open source. Python 3.10+. Linux, macOS, Windows.

→ GitHub: github.com/shaihazher/tappi
→ PyPI: pypi.org/project/tappi

#OpenSource #AI #BrowserAutomation #Python #LLM #Automation #DevTools #BuildInPublic
