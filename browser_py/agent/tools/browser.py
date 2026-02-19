"""Browser tool — wraps browser-py CDP commands for the LLM agent.

Exposes browser control as a single tool with an `action` parameter,
keeping the tool count low (LLMs work better with fewer, richer tools).
"""

from __future__ import annotations

import json
from typing import Any

from browser_py.core import Browser, CDPError, BrowserNotRunning
from browser_py.profiles import list_profiles, get_profile, create_profile

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
            "the page structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "launch", "tabs", "open", "tab", "newtab", "close_tab",
                        "elements", "click", "type", "text", "html", "eval",
                        "screenshot", "scroll", "url", "back", "forward",
                        "refresh", "upload", "wait", "profiles",
                    ],
                    "description": (
                        "Browser action to perform:\n"
                        "- launch: Start browser (optional: profile name)\n"
                        "- profiles: List available browser profiles\n"
                        "- tabs: List open tabs\n"
                        "- open: Navigate to URL (requires 'url')\n"
                        "- tab: Switch to tab by index (requires 'index')\n"
                        "- newtab: Open new tab (optional: 'url')\n"
                        "- close_tab: Close tab (optional: 'index', default current)\n"
                        "- elements: List interactive elements (optional: 'selector')\n"
                        "- click: Click element by index (requires 'index')\n"
                        "- type: Type into element (requires 'index' and 'text')\n"
                        "- text: Extract visible text (optional: 'selector')\n"
                        "- html: Get element HTML (requires 'selector')\n"
                        "- eval: Run JavaScript (requires 'expression')\n"
                        "- screenshot: Save screenshot (optional: 'path')\n"
                        "- scroll: Scroll page (requires 'direction': up/down/top/bottom, optional 'amount')\n"
                        "- url: Get current URL\n"
                        "- back/forward/refresh: Navigate history\n"
                        "- upload: Upload file (requires 'path', optional 'selector')\n"
                        "- wait: Wait milliseconds (requires 'ms')"
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
                "profile": {"type": "string", "description": "Browser profile name for launch"},
            },
            "required": ["action"],
        },
    },
}


class BrowserTool:
    """Stateful browser tool that maintains a connection."""

    def __init__(self, default_profile: str | None = None, download_dir: str | None = None) -> None:
        self._browser: Browser | None = None
        self._default_profile = default_profile
        self._download_dir = download_dir

    def _get_browser(self) -> Browser:
        """Get or create the browser connection."""
        if self._browser is None:
            # Try to connect to default profile
            try:
                profile = get_profile(self._default_profile)
                self._browser = Browser(f"http://127.0.0.1:{profile['port']}")
                # Test connection
                self._browser.tabs()
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
                return browser.newtab(params.get("url"))

            elif action == "close_tab":
                idx = params.get("index")
                return browser.close_tab(int(idx) if idx is not None else None)

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
        from browser_py.core import Browser as B

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
