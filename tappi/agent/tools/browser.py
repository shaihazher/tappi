"""Browser tool — wraps tappi CDP commands for the LLM agent.

Exposes browser control as a single tool with an `action` parameter,
keeping the tool count low (LLMs work better with fewer, richer tools).
"""

from __future__ import annotations

import json
from typing import Any

from tappi.core import Browser, CDPError, BrowserNotRunning
from tappi.profiles import list_profiles, get_profile, create_profile

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "browser",
        "description": (
            "Control a web browser. Navigate pages, click elements, type text, "
            "read content, take screenshots, and manage tabs. Uses your real "
            "browser with saved logins and cookies.\n\n"
            "Workflow: open a URL → elements (see what's clickable) → click/type → "
            "text (read result). Always call 'elements' after navigation to see "
            "the page structure.\n\n"
            "VERIFY YOUR ACTIONS: After typing into fields (especially compose "
            "areas, rich editors, forms), use 'check' to confirm the text landed "
            "in the right element. If a popup, contact card, or dropdown appeared "
            "and shifted focus, use 'focus' to reclaim it (lighter than click — "
            "won't trigger more popups), or 'keys --escape' to dismiss the overlay. "
            "Then retry your input. One quick verification before Send/Submit/Delete "
            "catches silent failures and saves recovery time. If these steps don't "
            "resolve the issue, use 'eval' for custom JS fixes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "launch", "tabs", "open", "tab", "newtab", "close_tab",
                        "search", "elements", "click", "type", "focus", "check",
                        "paste", "text", "html", "eval",
                        "screenshot", "scroll", "url", "back", "forward",
                        "refresh", "upload", "wait", "profiles",
                        "create_profile", "switch_profile",
                        "click_xy", "hover_xy", "drag_xy", "iframe_rect",
                        "keys",
                    ],
                    "description": (
                        "Browser action to perform:\n"
                        "- launch: Start browser (optional: profile name — creates if needed)\n"
                        "- profiles: List available browser profiles\n"
                        "- create_profile: Create a new browser profile (requires 'profile')\n"
                        "- switch_profile: Switch to a different profile and launch it (requires 'profile')\n"
                        "- tabs: List open tabs\n"
                        "- open: Navigate to URL (requires 'url')\n"
                        "- search: Google search — returns clean result links with index numbers (requires 'query')\n"
                        "- tab: Switch to tab by index (requires 'index')\n"
                        "- newtab: Open new tab (optional: 'url')\n"
                        "- close_tab: Close tab (optional: 'index', default current)\n"
                        "- elements: List interactive elements (optional: 'selector')\n"
                        "- click: Click element by index (requires 'index')\n"
                        "- type: Type into a DOM element (requires 'index' and 'text'). "
                        "NOTE: won't work on canvas-based apps (Google Sheets, Docs, Figma) — use 'keys' instead\n"
                        "- focus: Focus an element by index WITHOUT triggering click events (requires 'index'). "
                        "Use to reclaim input focus after a popup, contact card, or dropdown appeared. "
                        "Lighter than click — calls el.focus() only, so it won't spawn additional popups\n"
                        "- check: Read the current value/text of an element by index (requires 'index'). "
                        "Use after type to verify text landed correctly. Returns the value, length, and "
                        "whether the element has focus. Catches silent failures from focus shifts\n"
                        "- paste: Paste content into an element with auto-verification and fallback "
                        "(requires 'index' + either 'text' or 'path' to a file). "
                        "PREFERRED for long content like email bodies, comments, posts. "
                        "Handles focus, insertion, verification, and JS fallback automatically. "
                        "For short text, 'type' is fine. For canvas apps (Sheets, Docs), use 'keys'. "
                        "Tip: in multi-step tasks, prior steps write content to files — use 'path' "
                        "to reference those files instead of passing the content inline\n"
                        "- text: Extract visible text (optional: 'selector')\n"
                        "- html: Get element HTML (requires 'selector')\n"
                        "- eval: Run JavaScript (requires 'expression')\n"
                        "- screenshot: Save screenshot (optional: 'path')\n"
                        "- scroll: Scroll page (requires 'direction': up/down/top/bottom, optional 'amount')\n"
                        "- url: Get current URL\n"
                        "- back/forward/refresh: Navigate history\n"
                        "- upload: Upload file (requires 'path', optional 'selector')\n"
                        "- wait: Wait milliseconds (requires 'ms')\n"
                        "\nCoordinate commands (for cross-origin iframes, captchas, overlays):\n"
                        "- click_xy: Click at page coordinates (requires 'x', 'y'; optional 'double', 'right')\n"
                        "- hover_xy: Hover at page coordinates (requires 'x', 'y')\n"
                        "- drag_xy: Drag between coordinates (requires 'x', 'y', 'x2', 'y2')\n"
                        "- iframe_rect: Get iframe bounding box for targeting (requires 'selector')\n"
                        "\nRaw keyboard input (for canvas apps: Google Sheets, Docs, Figma):\n"
                        "- keys: Send raw CDP keyboard events that bypass the DOM (requires 'actions' list). "
                        "Use for canvas-based apps where 'type' can't reach content areas. "
                        "Actions list can contain: plain text, key flags (--enter, --tab, --escape, "
                        "--backspace, --delete, --up, --down, --left, --right), or --combo followed by "
                        "a key combo (e.g. cmd+b, ctrl+a). Chain freely: "
                        "[\"Revenue\", \"--tab\", \"Q1\", \"--enter\"]. "
                        "Navigation elements (menus, toolbars, name box) are still DOM — use click/type for those. "
                        "Google Sheets tip: --tab moves between columns, but --enter does NOT "
                        "reliably advance rows. Navigate to each row start via the Name Box "
                        "(click it, type cell ref with 'type', press --enter), then --tab within the row."
                    ),
                },
                "url": {"type": "string", "description": "URL for open/newtab actions"},
                "index": {"type": "integer", "description": "Element or tab index"},
                "text": {"type": "string", "description": "Text to type"},
                "selector": {"type": "string", "description": "CSS selector for elements/text/html/upload"},
                "expression": {"type": "string", "description": "JavaScript expression for eval"},
                "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"], "description": "Scroll direction"},
                "amount": {"type": "integer", "description": "Scroll pixels (default 600)"},
                "path": {"type": "string", "description": "File path for screenshot/upload"},
                "ms": {"type": "integer", "description": "Milliseconds to wait"},
                "query": {"type": "string", "description": "Search query for search action"},
                "profile": {"type": "string", "description": "Browser profile name for launch"},
                "x": {"type": "number", "description": "X page coordinate for click_xy/hover_xy/drag_xy"},
                "y": {"type": "number", "description": "Y page coordinate for click_xy/hover_xy/drag_xy"},
                "x2": {"type": "number", "description": "End X coordinate for drag_xy"},
                "y2": {"type": "number", "description": "End Y coordinate for drag_xy"},
                "double": {"type": "boolean", "description": "Double-click for click_xy"},
                "right": {"type": "boolean", "description": "Right-click for click_xy"},
                "actions": {"type": "array", "items": {"type": "string"}, "description": "List of actions for keys: text strings, --flags (--enter, --tab, etc.), or --combo pairs"},
            },
            "required": ["action"],
        },
    },
}


class BrowserTool:
    """Stateful browser tool that maintains a connection.

    Tracks tabs opened during this session so they can be cleaned up
    when the agent is done (via close_opened_tabs or cleanup).
    """

    def __init__(self, default_profile: str | None = None, download_dir: str | None = None) -> None:
        self._browser: Browser | None = None
        self._default_profile = default_profile
        self._download_dir = download_dir
        self._opened_tabs: list[str] = []  # target IDs of tabs we opened
        self._initial_tabs: set[str] = set()  # tabs that existed before agent started

    def snapshot_tabs(self) -> None:
        """Capture current tab IDs so cleanup knows what was pre-existing."""
        if self._browser:
            try:
                self._initial_tabs = {p["id"] for p in self._browser._get_pages()
                                      if "url" in p and not p["url"].startswith("devtools://")}
            except Exception:
                pass

    def _get_browser(self) -> Browser:
        """Get or create the browser connection."""
        if self._browser is None:
            # CDP_URL env var takes priority (external browser like OpenClaw)
            import os
            cdp_url = os.environ.get("CDP_URL")
            if cdp_url:
                try:
                    self._browser = Browser(cdp_url)
                    self._browser.tabs()
                    if not self._initial_tabs:
                        self.snapshot_tabs()
                    return self._browser
                except BrowserNotRunning:
                    self._browser = None
                    raise BrowserNotRunning(cdp_url)

            # Fall back to profile-based connection
            try:
                profile = get_profile(self._default_profile)
                self._browser = Browser(f"http://127.0.0.1:{profile['port']}")
                # Test connection
                self._browser.tabs()
                # Snapshot existing tabs on first connect
                if not self._initial_tabs:
                    self.snapshot_tabs()
            except (ValueError, BrowserNotRunning):
                self._browser = None
                raise BrowserNotRunning("http://127.0.0.1:9222")
        return self._browser

    def execute(self, **params: Any) -> str:
        """Execute a browser action. Returns a string result."""
        action = params.get("action", "")

        try:
            # Actions that don't need an existing connection
            if action == "launch":
                return self._launch(params.get("profile"))
            if action == "profiles":
                return self._list_profiles()
            if action == "create_profile":
                return self._create_profile(params.get("profile", ""))
            if action == "switch_profile":
                return self._switch_profile(params.get("profile", ""))

            # All other actions need a browser
            browser = self._get_browser()

            if action == "tabs":
                tabs = browser.tabs()
                if not tabs:
                    return "No tabs open."
                return "\n".join(str(t) for t in tabs)

            elif action == "open":
                url = params.get("url", "")
                if not url:
                    return "Error: 'url' parameter required for open action."
                return browser.open(url)

            elif action == "tab":
                idx = params.get("index")
                if idx is None:
                    return "Error: 'index' parameter required for tab action."
                return browser.tab(int(idx))

            elif action == "newtab":
                # Track tabs opened by the agent for cleanup
                before_ids = {p["id"] for p in browser._get_pages()}
                result = browser.newtab(params.get("url"))
                after_ids = {p["id"] for p in browser._get_pages()}
                new_ids = after_ids - before_ids
                self._opened_tabs.extend(new_ids)
                return result

            elif action == "close_tab":
                idx = params.get("index")
                return browser.close_tab(int(idx) if idx is not None else None)

            elif action == "search":
                query = params.get("query", "")
                if not query:
                    return "Error: 'query' parameter required for search action."
                from urllib.parse import quote_plus
                browser.open(f"https://www.google.com/search?q={quote_plus(query)}")
                import time as _time
                _time.sleep(2)
                # Extract search result links via JS for full URLs
                js = """(() => {
                    const results = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const href = a.href;
                        if (!href || href.includes('google.com') || href.includes('youtube.com')
                            || href.startsWith('javascript:') || href.includes('accounts.google')
                            || href.includes('support.google') || href.includes('policies.google'))
                            return;
                        const text = (a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 100);
                        if (!text || text.length < 5) return;
                        results.push({title: text, url: href});
                    });
                    // Dedupe by URL
                    const seen = new Set();
                    return results.filter(r => {
                        const key = r.url.split('#')[0].split('?')[0];
                        if (seen.has(key)) return false;
                        seen.add(key);
                        return true;
                    }).slice(0, 10);
                })()"""
                raw = browser.eval(js)
                if not raw:
                    return "No search results found. Try a different query."
                lines = []
                for i, r in enumerate(raw):
                    lines.append(f"[{i+1}] {r['title']}\n    {r['url']}")
                return f"Search results for: {query}\n\n" + "\n".join(lines)

            elif action == "elements":
                elements = browser.elements(params.get("selector"))
                if not elements:
                    return "No interactive elements found. The page might still be loading — try wait then elements again."
                return "\n".join(str(e) for e in elements)

            elif action == "click":
                idx = params.get("index")
                if idx is None:
                    return "Error: 'index' parameter required for click action."
                return browser.click(int(idx))

            elif action == "type":
                idx = params.get("index")
                text = params.get("text", "")
                if idx is None or not text:
                    return "Error: 'index' and 'text' parameters required for type action."
                return browser.type(int(idx), text)

            elif action == "focus":
                idx = params.get("index")
                if idx is None:
                    return "Error: 'index' parameter required for focus action."
                return browser.focus(int(idx))

            elif action == "check":
                idx = params.get("index")
                if idx is None:
                    return "Error: 'index' parameter required for check action."
                return browser.check(int(idx))

            elif action == "paste":
                idx = params.get("index")
                if idx is None:
                    return "Error: 'index' parameter required for paste action."
                content = params.get("text", "")
                file_path = params.get("path", "")
                if file_path and not content:
                    import os
                    fp = os.path.expanduser(file_path)
                    if not os.path.isfile(fp):
                        return f"Error: File not found: {fp}"
                    with open(fp, "r") as f:
                        content = f.read()
                if not content:
                    return "Error: 'text' or 'path' parameter required for paste action."
                return browser.paste(int(idx), content)

            elif action == "text":
                return browser.text(params.get("selector"))

            elif action == "html":
                sel = params.get("selector", "")
                if not sel:
                    return "Error: 'selector' parameter required for html action."
                return browser.html(sel)

            elif action == "eval":
                expr = params.get("expression", "")
                if not expr:
                    return "Error: 'expression' parameter required for eval action."
                result = browser.eval(expr)
                if isinstance(result, str):
                    return result
                return json.dumps(result, indent=2) if result is not None else "(undefined)"

            elif action == "screenshot":
                path = browser.screenshot(params.get("path"))
                return f"Screenshot saved: {path}"

            elif action == "scroll":
                direction = params.get("direction", "down")
                amount = int(params.get("amount", 600))
                return browser.scroll(direction, amount)

            elif action == "url":
                return browser.url()

            elif action == "back":
                return browser.back()

            elif action == "forward":
                return browser.forward()

            elif action == "refresh":
                return browser.refresh()

            elif action == "upload":
                path = params.get("path", "")
                if not path:
                    return "Error: 'path' parameter required for upload action."
                selector = params.get("selector", 'input[type="file"]')
                return browser.upload(path, selector)

            elif action == "wait":
                ms = int(params.get("ms", 1000))
                return browser.wait(ms)

            # Raw keyboard input (canvas apps)
            elif action == "keys":
                actions_list = params.get("actions", [])
                if not actions_list:
                    return "Error: 'actions' parameter required for keys. Provide a list like [\"text\", \"--tab\", \"more text\", \"--enter\"] or [\"--combo\", \"cmd+b\"]."
                return browser.keys(*actions_list)

            # Coordinate commands
            elif action == "click_xy":
                x = float(params.get("x", 0))
                y = float(params.get("y", 0))
                double = bool(params.get("double", False))
                right = bool(params.get("right", False))
                return browser.click_xy(x, y, double=double, right=right)

            elif action == "hover_xy":
                return browser.hover_xy(float(params.get("x", 0)), float(params.get("y", 0)))

            elif action == "drag_xy":
                return browser.drag_xy(
                    float(params.get("x", 0)), float(params.get("y", 0)),
                    float(params.get("x2", 0)), float(params.get("y2", 0)),
                )

            elif action == "iframe_rect":
                sel = params.get("selector", "iframe")
                info = browser.iframe_rect(sel)
                return f"x={info['x']} y={info['y']} w={info['width']} h={info['height']} center=({info['cx']}, {info['cy']})"

            else:
                return f"Unknown action: {action}"

        except BrowserNotRunning:
            return (
                "Browser is not running. Use action='launch' to start it, "
                "or action='profiles' to see available profiles."
            )
        except CDPError as e:
            return f"Browser error: {e}"
        except Exception as e:
            return f"Error: {e}"

    def _launch(self, profile_name: str | None = None) -> str:
        """Launch a browser profile."""
        from tappi.core import Browser as B

        name = profile_name or self._default_profile
        try:
            profile = get_profile(name)
        except ValueError:
            if name:
                profile = create_profile(name)
            else:
                profile = create_profile("default")

        port = profile["port"]

        # Check if already running
        try:
            b = Browser(f"http://127.0.0.1:{port}")
            b.tabs()
            self._browser = b
            return f"Browser already running — profile: {profile['name']} (port {port})"
        except BrowserNotRunning:
            pass

        B.launch(port=port, user_data_dir=profile["path"], download_dir=self._download_dir)
        self._browser = Browser(f"http://127.0.0.1:{port}")
        return f"Browser launched — profile: {profile['name']} (port {port})"

    def cleanup(self) -> str:
        """Close all tabs opened during this agent session.

        Closes any tab that wasn't present when the agent first connected.
        Falls back to the explicit _opened_tabs list if no initial snapshot.
        Returns a summary of what was cleaned up.
        """
        if not self._browser:
            self._opened_tabs.clear()
            return "No browser connected."

        closed = 0
        try:
            from urllib.request import urlopen
            pages = self._browser._get_pages()

            # Determine which tabs to close: anything not in the initial snapshot
            if self._initial_tabs:
                tabs_to_close = [
                    p["id"] for p in pages
                    if p["id"] not in self._initial_tabs
                    and "url" in p
                    and not p["url"].startswith("devtools://")
                ]
            else:
                # Fallback: use explicit tracking list
                page_ids = {p["id"] for p in pages}
                tabs_to_close = [tid for tid in self._opened_tabs if tid in page_ids]

            for tid in tabs_to_close:
                try:
                    urlopen(f"{self._browser.cdp_url}/json/close/{tid}", timeout=2)
                    closed += 1
                except Exception:
                    pass
        except Exception:
            pass

        self._opened_tabs.clear()
        # Reset initial snapshot for next session
        self.snapshot_tabs()
        return f"Closed {closed} tab(s) opened during this session."

    def _create_profile(self, name: str) -> str:
        """Create a new browser profile."""
        if not name:
            return "Error: 'profile' parameter required. Provide a name like 'personal', 'work', etc."
        try:
            get_profile(name)
            return f"Profile '{name}' already exists. Use action='switch_profile' to switch to it, or action='launch' with profile='{name}' to start it."
        except ValueError:
            pass
        profile = create_profile(name)
        return f"Profile '{name}' created (port {profile['port']}). Use action='launch' with profile='{name}' to start the browser, then navigate and log in."

    def _switch_profile(self, name: str) -> str:
        """Switch to a different profile and launch it."""
        if not name:
            return "Error: 'profile' parameter required."
        # Create if it doesn't exist
        try:
            get_profile(name)
        except ValueError:
            create_profile(name)

        # Disconnect from current browser
        self._browser = None
        self._opened_tabs.clear()
        self._initial_tabs.clear()
        self._default_profile = name

        # Persist to config so the profile picker and cron jobs pick it up
        try:
            from tappi.agent.config import load_config, save_config
            cfg = load_config()
            cfg.setdefault("agent", {})["browser_profile"] = name
            save_config(cfg)
        except Exception:
            pass

        # Launch the new profile
        return self._launch(name)

    def _list_profiles(self) -> str:
        """List available browser profiles."""
        profiles = list_profiles()
        if not profiles:
            return "No profiles. Use action='launch' with profile='name' to create one."
        lines = []
        for p in profiles:
            default = " (default)" if p["is_default"] else ""
            lines.append(f"  {p['name']} — port {p['port']}{default}")
        return "Browser profiles:\n" + "\n".join(lines)
