# browser-py

Lightweight Python library and CLI to control Chrome/Chromium via the [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/) (CDP).

**The key feature:** connects to your **existing browser** — all your logged-in sessions, cookies, and extensions carry over. No re-authentication needed.

## Install

```bash
pip install browser-py
```

## Setup (One-Time)

```bash
# Step 1: Launch a browser with remote debugging
browser-py launch
```

That's it. A Chrome window opens with its own profile at `~/.browser-py/profile`.

**First time?** Log into the sites you want to automate — Gmail, GitHub, Reddit, whatever. Those sessions persist. Next time you run `browser-py launch`, everything is still logged in.

**Why a separate profile?** Chrome locks its profile — only one instance can use it. So browser-py creates its own. Your main Chrome stays untouched.

**Options:**
```bash
browser-py launch                          # Default: port 9222
browser-py launch --port 9333              # Custom port
browser-py launch --user-data-dir ~/my-profile  # Custom profile location
browser-py launch --headless               # No visible window (servers/CI)
```

## Quick Start

### CLI

```bash
# Terminal 1: Launch the browser (keep it running)
browser-py launch

# Terminal 2: Control it
browser-py open github.com          # Navigate
browser-py elements                 # See what's clickable
browser-py click 3                  # Click element [3]
browser-py type 5 "hello world"     # Type into element [5]
browser-py text                     # Read the page
browser-py screenshot               # Save a screenshot
```

### Python

```python
from browser_py import Browser

b = Browser()                        # Connect (default: localhost:9222)
b.open("https://github.com")         # Navigate
elements = b.elements()              # List interactive elements
print(elements)                       # [Element(0, 'link', 'Home → /'), ...]
b.click(1)                           # Click element [1]
b.type(2, "search query")            # Type into element [2]
print(b.text())                      # Read page text
b.screenshot("~/Desktop/page.png")   # Save screenshot
```

## How It Works

1. **`elements`** scans the page for all interactive elements — links, buttons, inputs, selects, textareas, and ARIA roles. Returns a compact numbered list:

```
[0] (link) Home → /
[1] (button) Sign In
[2] (input:text) Search
[3] (link) About → /about
```

2. **`click 1`** or **`type 2 hello`** — interact by number. That's it.

### Shadow DOM

browser-py automatically **pierces shadow DOM boundaries**. Sites using web components (Reddit, GitHub, etc.) work out of the box. No special flags needed.

### Real Mouse Events

`click` uses CDP `Input.dispatchMouseEvent` — real mouse events that trigger React/Vue/Angular handlers properly. Not just `.click()` in JavaScript.

### Framework-Aware Typing

`type` dispatches proper `input` and `change` events with React's native value setter. Works with SPAs that use synthetic event systems.

## Commands

| Command | Description |
|---------|-------------|
| `launch` | Start Chrome with remote debugging |
| `tabs` | List open tabs |
| `open <url>` | Navigate to URL |
| `tab <index>` | Switch to tab |
| `newtab [url]` | Open new tab |
| `close [index]` | Close tab |
| `elements [selector]` | List interactive elements (numbered) |
| `click <index>` | Click element by number |
| `type <index> <text>` | Type into element |
| `upload <path> [sel]` | Upload file (bypasses OS dialog) |
| `text [selector]` | Extract visible text |
| `html <selector>` | Get element HTML |
| `eval <js>` | Run JavaScript |
| `screenshot [path]` | Save screenshot |
| `scroll <dir> [px]` | Scroll up/down/top/bottom |
| `url` | Current URL |
| `back / forward / refresh` | Navigation history |
| `wait <ms>` | Wait (for scripts) |

Every command has `--help`:

```bash
browser-py click --help
```

## Python API

```python
from browser_py import Browser

# Connect to a specific port
b = Browser("http://127.0.0.1:18800")

# Or use CDP_URL environment variable
# CDP_URL=http://127.0.0.1:18800 python script.py

# Tab management
tabs = b.tabs()              # List[Tab]
b.tab(2)                     # Switch to tab
b.newtab("https://...")      # Open new tab
b.close_tab(0)               # Close tab

# Navigation
b.open("https://example.com")
print(b.url())
b.back()
b.forward()
b.refresh()

# Element interaction
elements = b.elements()      # List[Element]
elements = b.elements(".modal")  # Scoped to selector
b.click(3)                   # Click by index
b.type(5, "hello")           # Type by index

# Content
text = b.text()              # Visible text (max 8KB)
text = b.text(".article")    # Scoped
html = b.html("nav")         # outerHTML (max 10KB)
result = b.eval("document.title")  # Run JS

# Screenshot
path = b.screenshot()        # Returns saved path
b.screenshot("~/page.png")

# File upload
b.upload("~/photo.jpg")      # Auto-finds input[type=file]
b.upload("~/doc.pdf", ".custom-input")

# Scroll
b.scroll("down", 1000)
b.scroll("top")

# Wait
b.wait(2000)
```

## Low-Level CDP Access

For advanced use, access the CDP session directly:

```python
from browser_py import Browser, CDPSession

b = Browser()
target = b._current_target()

# Raw CDP commands
cdp = CDPSession.connect_to_page(target["id"], port=9222)
result = cdp.send("Runtime.evaluate", expression="1+1")
print(result)  # {'result': {'type': 'number', 'value': 2, ...}}
cdp.close()
```

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CDP_URL` | CDP endpoint URL | `http://127.0.0.1:9222` |
| `NO_COLOR` | Disable colored CLI output | (unset) |

## Comparison

| Tool | Session reuse | Deps | Shadow DOM | Token efficiency |
|------|:---:|------|:---:|:---:|
| **browser-py** | ✅ | websockets only | ✅ | ~50-200 tokens |
| Selenium | ❌ | Heavy (WebDriver) | ❌ | N/A |
| Playwright | Partial | Heavy (browsers) | ❌ | ~2,000-5,000 |
| pyppeteer | ❌ | Abandoned | ❌ | N/A |

## License

MIT
