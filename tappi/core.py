"""Core CDP session and Browser class.

This is the main module. Use Browser for high-level control, CDPSession
for raw CDP protocol access.

    from tappi import Browser

    b = Browser("http://127.0.0.1:9222")
    b.open("https://example.com")
    for el in b.elements():
        print(el)
    b.click(0)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen
from urllib.error import URLError

from tappi.js_expressions import (
    check_indexed_js,
    clear_contenteditable_js,
    clear_input_js,
    click_info_js,
    elements_js,
    extract_text_js,
    get_html_js,
    set_input_value_js,
    type_info_js,
)

try:
    import websockets
    from websockets.sync.client import connect as ws_connect
except ImportError:
    websockets = None  # type: ignore[assignment]

# ── Data classes ──


@dataclass
class Tab:
    """Represents a browser tab."""

    index: int
    id: str
    title: str
    url: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.title or '(untitled)'} — {self.url}"


@dataclass
class Element:
    """An interactive element on the page."""

    index: int
    label: str
    desc: str

    def __str__(self) -> str:
        return f"[{self.index}] ({self.label}) {self.desc}"


# ── CDP Session (sync) ──


class CDPError(Exception):
    """Error from the Chrome DevTools Protocol."""

    pass


class BrowserNotRunning(Exception):
    """Raised when CDP endpoint is unreachable."""

    def __init__(self, cdp_url: str) -> None:
        self.cdp_url = cdp_url
        port = cdp_url.rsplit(":", 1)[-1].split("/")[0]
        super().__init__(
            f"Cannot connect to browser at {cdp_url}\n\n"
            f"Make sure Chrome/Chromium is running with remote debugging enabled:\n"
            f"  chrome --remote-debugging-port={port}\n\n"
            f"Or start it with a persistent profile (keeps your logins):\n"
            f"  chrome --remote-debugging-port={port} --user-data-dir=~/.tappi-data"
        )


class CDPSession:
    """Low-level synchronous CDP WebSocket session.

    Use this directly only if you need raw CDP protocol access.
    For normal use, prefer the Browser class.

    Example:
        cdp = CDPSession.connect_to_page(target_id, port=9222)
        result = cdp.send("Runtime.evaluate", expression="1+1")
        cdp.close()
    """

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._id = 0

    @classmethod
    def connect_to_page(cls, target_id: str, port: int = 9222) -> CDPSession:
        """Connect to a specific page target by its ID."""
        if websockets is None:
            raise ImportError(
                "websockets is required: pip install tappi\n"
                "Or: pip install websockets"
            )
        ws_url = f"ws://127.0.0.1:{port}/devtools/page/{target_id}"
        ws = ws_connect(ws_url)
        return cls(ws)

    @classmethod
    def connect_to_browser(cls, cdp_url: str) -> CDPSession:
        """Connect to the browser-level CDP endpoint."""
        if websockets is None:
            raise ImportError("websockets is required: pip install tappi")
        try:
            data = json.loads(urlopen(f"{cdp_url}/json/version").read())
        except (URLError, OSError):
            raise BrowserNotRunning(cdp_url)
        ws_url = data.get("webSocketDebuggerUrl", "")
        port = cdp_url.rsplit(":", 1)[-1].split("/")[0]
        ws_url = re.sub(r"^ws://[^/]+", f"ws://127.0.0.1:{port}", ws_url)
        if not ws_url:
            raise CDPError("Browser did not expose webSocketDebuggerUrl")
        ws = ws_connect(ws_url)
        return cls(ws)

    def send(self, method: str, **params: Any) -> dict:
        """Send a CDP command and wait for the response."""
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))

        while True:
            raw = self._ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CDPError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result", {})
            # Skip events, keep reading

    def send_and_wait_event(
        self, method: str, event_name: str, timeout: float = 10.0, **params: Any
    ) -> dict:
        """Send a CDP command and wait for a specific event."""
        self._id += 1
        msg_id = self._id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))

        result = None
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                raw = self._ws.recv(timeout=remaining)
            except TimeoutError:
                break
            msg = json.loads(raw)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise CDPError(msg["error"].get("message", str(msg["error"])))
                result = msg.get("result", {})
            if msg.get("method") == event_name:
                return result or {}

        return result or {}

    def close(self) -> None:
        """Close the WebSocket connection."""
        try:
            self._ws.close()
        except Exception:
            pass


# ── Browser (high-level API) ──


class Browser:
    """High-level browser control via CDP.

    Connects to a running Chrome/Chromium instance and provides simple
    methods to navigate, interact with elements, and extract content.

    The killer feature: it connects to your EXISTING browser. All your
    logged-in sessions, cookies, and extensions are available.

    Args:
        cdp_url: CDP endpoint URL (default: http://127.0.0.1:9222).
                 Set CDP_URL env var to override.

    Example:
        b = Browser()
        b.open("https://github.com")
        elements = b.elements()       # See what's on the page
        b.click(0)                    # Click the first element
        print(b.text())               # Read the page
    """

    def __init__(self, cdp_url: str | None = None) -> None:
        self.cdp_url = cdp_url or os.environ.get("CDP_URL", "http://127.0.0.1:9222")
        self._port = int(self.cdp_url.rsplit(":", 1)[-1].split("/")[0])

    # ── Private helpers ──

    def _fetch_json(self, path: str) -> Any:
        """Fetch JSON from the CDP HTTP endpoint."""
        try:
            return json.loads(urlopen(f"{self.cdp_url}{path}").read())
        except (URLError, OSError):
            raise BrowserNotRunning(self.cdp_url)

    def _get_pages(self) -> list[dict]:
        """Get all page-type targets."""
        targets = self._fetch_json("/json/list")
        return [t for t in targets if t.get("type") == "page"]

    def _current_target(self) -> dict:
        """Get the first visible page target."""
        pages = self._get_pages()
        if not pages:
            raise CDPError(
                "No browser tabs open.\n"
                "Hint: Open a tab in your browser, or use: browser.open('https://example.com')"
            )
        return pages[0]

    def _target_by_index(self, index: int) -> dict:
        """Get a page target by tab index."""
        pages = self._get_pages()
        if index < 0 or index >= len(pages):
            raise CDPError(
                f"Tab index {index} out of range (0–{len(pages) - 1}).\n"
                f"Hint: Run tabs() to see available tabs."
            )
        return pages[index]

    def _connect_page(self, target_id: str | None = None) -> CDPSession:
        """Connect to a page target. Uses current tab if no ID given."""
        tid = target_id or self._current_target()["id"]
        return CDPSession.connect_to_page(tid, self._port)

    def _connect_browser(self) -> CDPSession:
        """Connect to the browser-level CDP endpoint."""
        return CDPSession.connect_to_browser(self.cdp_url)

    def _eval(self, js: str, target_id: str | None = None) -> Any:
        """Evaluate JS in the page and return the result value."""
        cdp = self._connect_page(target_id)
        try:
            result = cdp.send("Runtime.evaluate", expression=js, returnByValue=True)
            r = result.get("result", {})
            return r.get("value")
        finally:
            cdp.close()

    def _ensure_indexed(self, cdp: CDPSession) -> bool:
        """Make sure elements are indexed. Returns True if re-indexed."""
        result = cdp.send(
            "Runtime.evaluate", expression=check_indexed_js(), returnByValue=True
        )
        if not result.get("result", {}).get("value"):
            cdp.send(
                "Runtime.evaluate", expression=elements_js(None), returnByValue=True
            )
            return True
        return False

    # ── Tab management ──

    def tabs(self) -> list[Tab]:
        """List all open browser tabs.

        Returns:
            List of Tab objects with index, id, title, and url.

        Example:
            >>> b.tabs()
            [Tab(index=0, id='...', title='Google', url='https://google.com')]
        """
        pages = self._get_pages()
        return [
            Tab(index=i, id=t["id"], title=t.get("title", ""), url=t.get("url", ""))
            for i, t in enumerate(pages)
        ]

    def tab(self, index: int) -> str:
        """Switch to a tab by its index number.

        Args:
            index: Tab number from tabs() output.

        Returns:
            Confirmation message with tab title and URL.
        """
        target = self._target_by_index(index)
        cdp = self._connect_page(target["id"])
        try:
            cdp.send("Page.bringToFront")
            return f"Switched to tab [{index}]: {target.get('title', '')} — {target.get('url', '')}"
        finally:
            cdp.close()

    def newtab(self, url: str | None = None) -> str:
        """Open a new browser tab.

        Args:
            url: URL to open (default: blank tab).

        Returns:
            The new tab's target ID.
        """
        cdp = self._connect_browser()
        try:
            result = cdp.send("Target.createTarget", url=url or "about:blank")
            return f"Opened new tab: {result.get('targetId', '')}"
        finally:
            cdp.close()

    def close_tab(self, index: int | None = None) -> str:
        """Close a browser tab.

        Args:
            index: Tab index to close (default: current tab).

        Returns:
            Confirmation message.
        """
        target = (
            self._target_by_index(index) if index is not None else self._current_target()
        )
        cdp = self._connect_browser()
        try:
            cdp.send("Target.closeTarget", targetId=target["id"])
            return f"Closed tab: {target.get('title', '')}"
        finally:
            cdp.close()

    # ── Navigation ──

    def open(self, url: str) -> str:
        """Navigate the current tab to a URL.

        Waits for the page to finish loading (up to 10s).

        Args:
            url: The URL to navigate to. 'http(s)://' is added if missing.

        Returns:
            Confirmation message.

        Example:
            >>> b.open("github.com")
            'Navigated to https://github.com'
        """
        if not url.startswith("http"):
            url = "https://" + url
        target = self._current_target()
        cdp = self._connect_page(target["id"])
        try:
            cdp.send("Page.enable")
            cdp.send_and_wait_event(
                "Page.navigate", "Page.loadEventFired", timeout=10.0, url=url
            )
            return f"Navigated to {url}"
        finally:
            cdp.close()

    def url(self) -> str:
        """Get the current page URL.

        Returns:
            The URL of the active tab.
        """
        return self._current_target().get("url", "")

    def back(self) -> str:
        """Go back in browser history.

        Returns:
            The URL navigated to, or a message if already at the start.
        """
        cdp = self._connect_page()
        try:
            hist = cdp.send("Page.getNavigationHistory")
            idx = hist.get("currentIndex", 0)
            entries = hist.get("entries", [])
            if idx > 0:
                cdp.send(
                    "Page.navigateToHistoryEntry", entryId=entries[idx - 1]["id"]
                )
                return f"Back to: {entries[idx - 1]['url']}"
            return "Already at first page in history."
        finally:
            cdp.close()

    def forward(self) -> str:
        """Go forward in browser history.

        Returns:
            The URL navigated to, or a message if already at the end.
        """
        cdp = self._connect_page()
        try:
            hist = cdp.send("Page.getNavigationHistory")
            idx = hist.get("currentIndex", 0)
            entries = hist.get("entries", [])
            if idx < len(entries) - 1:
                cdp.send(
                    "Page.navigateToHistoryEntry", entryId=entries[idx + 1]["id"]
                )
                return f"Forward to: {entries[idx + 1]['url']}"
            return "Already at last page in history."
        finally:
            cdp.close()

    def refresh(self) -> str:
        """Reload the current page.

        Returns:
            Confirmation message.
        """
        cdp = self._connect_page()
        try:
            cdp.send("Page.reload")
            return "Refreshed."
        finally:
            cdp.close()

    # ── Element interaction ──

    def elements(self, selector: str | None = None) -> list[Element]:
        """List all interactive elements on the page.

        Scans for links, buttons, inputs, selects, textareas, and ARIA
        roles. Pierces shadow DOM boundaries automatically.

        Each element gets an index number you can use with click() and type().

        Args:
            selector: Optional CSS selector to narrow scope (e.g., ".modal").

        Returns:
            List of Element objects with index, label, and description.

        Example:
            >>> b.elements()
            [Element(index=0, label='link', desc='Home → /'),
             Element(index=1, label='button', desc='Sign In'),
             Element(index=2, label='input:text', desc='Search')]

            >>> b.elements(".sidebar")  # Only elements inside .sidebar
        """
        cdp = self._connect_page()
        try:
            cdp.send("DOM.enable")
            cdp.send("Runtime.enable")
            result = cdp.send(
                "Runtime.evaluate",
                expression=elements_js(selector),
                returnByValue=True,
            )
            raw = json.loads(result.get("result", {}).get("value", "[]"))
            if isinstance(raw, dict) and "error" in raw:
                raise CDPError(raw["error"])
            return [Element(index=i, label=e["label"], desc=e["desc"]) for i, e in enumerate(raw)]
        finally:
            cdp.close()

    def click(self, index: int) -> str:
        """Click an element by its index number.

        Uses real mouse events (mousePressed + mouseReleased) via CDP,
        which triggers React/Vue/Angular event handlers properly.

        Elements are auto-indexed if not already — you can call click()
        right after open() without calling elements() first.

        Args:
            index: Element index from elements() output.

        Returns:
            Description of what was clicked.

        Example:
            >>> b.click(3)
            'Clicked: (button) Sign In'
        """
        cdp = self._connect_page()
        try:
            self._ensure_indexed(cdp)
            result = cdp.send(
                "Runtime.evaluate",
                expression=click_info_js(index),
                returnByValue=True,
            )
            info = json.loads(result.get("result", {}).get("value", "{}"))
            if "error" in info:
                raise CDPError(info["error"])

            opts = {
                "x": info["x"],
                "y": info["y"],
                "button": "left",
                "clickCount": 1,
            }
            cdp.send("Input.dispatchMouseEvent", type="mousePressed", **opts)
            cdp.send("Input.dispatchMouseEvent", type="mouseReleased", **opts)
            return f"Clicked: ({info['label']}) {info['desc']}"
        finally:
            cdp.close()

    def type(self, index: int, text: str) -> str:
        """Type text into an element by its index number.

        Clears existing content first, then types. Works with:
        - Regular <input> and <textarea> elements
        - contenteditable elements (rich text editors)
        - Elements with role="textbox"

        Dispatches proper input/change events for React/Vue/Angular.

        Args:
            index: Element index from elements() output.
            text: Text to type.

        Returns:
            Confirmation message.

        Example:
            >>> b.type(2, "hello world")
            'Typed into [2] (input)'
        """
        cdp = self._connect_page()
        try:
            self._ensure_indexed(cdp)

            # Verify element is typeable
            result = cdp.send(
                "Runtime.evaluate",
                expression=type_info_js(index),
                returnByValue=True,
            )
            info = json.loads(result.get("result", {}).get("value", "{}"))
            if "error" in info:
                raise CDPError(info["error"])

            # Click to focus (real mouse events)
            click_opts = {
                "x": info["x"],
                "y": info["y"],
                "button": "left",
                "clickCount": 1,
            }
            cdp.send("Input.dispatchMouseEvent", type="mousePressed", **click_opts)
            cdp.send("Input.dispatchMouseEvent", type="mouseReleased", **click_opts)
            time.sleep(0.1)

            # Clear existing content
            if info.get("ce"):
                cdp.send(
                    "Runtime.evaluate",
                    expression=clear_contenteditable_js(index),
                )
                cdp.send(
                    "Input.dispatchKeyEvent",
                    type="keyDown",
                    key="Backspace",
                    code="Backspace",
                )
                cdp.send(
                    "Input.dispatchKeyEvent",
                    type="keyUp",
                    key="Backspace",
                    code="Backspace",
                )
            else:
                cdp.send(
                    "Runtime.evaluate", expression=clear_input_js(index)
                )

            # Insert text
            try:
                cdp.send("Input.insertText", text=text)
            except CDPError:
                # Fallback: character-by-character
                for char in text:
                    cdp.send(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        text=char,
                        key=char,
                        unmodifiedText=char,
                    )
                    cdp.send("Input.dispatchKeyEvent", type="keyUp", key=char)

            # Sync value for React/Vue
            if not info.get("ce"):
                cdp.send(
                    "Runtime.evaluate",
                    expression=set_input_value_js(index, text),
                )

            tag = info.get("tag", "element")
            ce = ", contenteditable" if info.get("ce") else ""
            return f"Typed into [{index}] ({tag}{ce})"
        finally:
            cdp.close()

    # ── Content extraction ──

    def text(self, selector: str | None = None) -> str:
        """Extract visible text from the page.

        Pierces shadow DOM boundaries. Caps at 8KB to avoid overwhelming
        output.

        Args:
            selector: Optional CSS selector to scope extraction.

        Returns:
            Visible text content as a string.

        Example:
            >>> print(b.text())
            'Welcome to GitHub ...'

            >>> print(b.text(".main-content"))  # Just the main area
        """
        return self._eval(extract_text_js(selector)) or "(empty page)"

    def html(self, selector: str) -> str:
        """Get the outerHTML of an element.

        Caps at 10KB. Useful for inspecting element structure.

        Args:
            selector: CSS selector for the element.

        Returns:
            The element's outerHTML.
        """
        return self._eval(get_html_js(selector)) or ""

    def eval(self, js: str) -> Any:
        """Execute JavaScript in the page context.

        Supports async expressions (awaits promises automatically).

        Args:
            js: JavaScript expression or statement to evaluate.

        Returns:
            The result value.

        Example:
            >>> b.eval("document.title")
            'GitHub'
            >>> b.eval("window.location.href")
            'https://github.com'
        """
        cdp = self._connect_page()
        try:
            result = cdp.send(
                "Runtime.evaluate",
                expression=js,
                returnByValue=True,
                awaitPromise=True,
            )
            exc = result.get("exceptionDetails")
            if exc:
                desc = exc.get("exception", {}).get("description", exc.get("text", ""))
                raise CDPError(f"JS Error: {desc}")
            r = result.get("result", {})
            val = r.get("value")
            if val is not None:
                return val
            if r.get("description"):
                return r["description"]
            return None if r.get("type") == "undefined" else r
        finally:
            cdp.close()

    # ── Screenshot ──

    def screenshot(self, path: str | None = None, *, format: str = "png") -> str:
        """Take a screenshot of the current page.

        Args:
            path: File path to save to (default: /tmp/tappi_screenshot_<timestamp>.png).
            format: Image format — "png" or "jpeg".

        Returns:
            The path where the screenshot was saved.
        """
        cdp = self._connect_page()
        try:
            import base64

            result = cdp.send("Page.captureScreenshot", format=format)
            data = base64.b64decode(result.get("data", ""))
            ext = "jpg" if format == "jpeg" else format
            out_path = path or f"/tmp/tappi_screenshot_{int(time.time())}.{ext}"
            Path(out_path).write_bytes(data)
            return out_path
        finally:
            cdp.close()

    # ── Scrolling ──

    def scroll(self, direction: str, amount: int = 600) -> str:
        """Scroll the page.

        Args:
            direction: One of "up", "down", "top", "bottom".
            amount: Pixels to scroll (for up/down). Default: 600.

        Returns:
            Confirmation message.
        """
        js_map = {
            "up": f"window.scrollBy(0, -{amount})",
            "down": f"window.scrollBy(0, {amount})",
            "top": "window.scrollTo(0, 0)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
        }
        if direction not in js_map:
            raise ValueError(
                f"Invalid direction '{direction}'. Use: up, down, top, bottom"
            )
        self._eval(js_map[direction])
        suffix = f" {amount}px" if direction in ("up", "down") else ""
        return f"Scrolled {direction}{suffix}"

    # ── Coordinate-based input (cross-origin iframes, captchas, overlays) ──

    def click_xy(
        self, x: float, y: float, *, double: bool = False, right: bool = False
    ) -> str:
        """Click at page coordinates via CDP Input events.

        Bypasses all DOM boundaries — works inside cross-origin iframes
        (captchas, payment forms, OAuth widgets). Uses real mouse events.

        Args:
            x: X coordinate on the page.
            y: Y coordinate on the page.
            double: If True, double-click.
            right: If True, right-click.

        Returns:
            Confirmation message.

        Example:
            >>> b.click_xy(125, 458)            # Click inside captcha iframe
            'Clicked at (125, 458)'
            >>> b.click_xy(300, 200, double=True)
            'Double-clicked at (300, 200)'
        """
        cdp = self._connect_page()
        try:
            button = "right" if right else "left"
            click_count = 2 if double else 1

            # Move mouse first (triggers hover states)
            cdp.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            time.sleep(0.05)

            cdp.send(
                "Input.dispatchMouseEvent",
                type="mousePressed", x=x, y=y, button=button, clickCount=click_count,
            )
            cdp.send(
                "Input.dispatchMouseEvent",
                type="mouseReleased", x=x, y=y, button=button, clickCount=click_count,
            )

            if double:
                cdp.send(
                    "Input.dispatchMouseEvent",
                    type="mousePressed", x=x, y=y, button=button, clickCount=2,
                )
                cdp.send(
                    "Input.dispatchMouseEvent",
                    type="mouseReleased", x=x, y=y, button=button, clickCount=2,
                )

            label = "Double-clicked" if double else ("Right-clicked" if right else "Clicked")
            return f"{label} at ({x}, {y})"
        finally:
            cdp.close()

    def hover_xy(self, x: float, y: float) -> str:
        """Hover at page coordinates.

        Args:
            x: X coordinate.
            y: Y coordinate.

        Returns:
            Confirmation message.
        """
        cdp = self._connect_page()
        try:
            cdp.send("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
            return f"Hovered at ({x}, {y})"
        finally:
            cdp.close()

    def drag_xy(
        self, from_x: float, from_y: float, to_x: float, to_y: float, *, steps: int = 10
    ) -> str:
        """Drag from one coordinate to another.

        Args:
            from_x: Start X.
            from_y: Start Y.
            to_x: End X.
            to_y: End Y.
            steps: Number of intermediate move events (default: 10).

        Returns:
            Confirmation message.
        """
        cdp = self._connect_page()
        try:
            cdp.send("Input.dispatchMouseEvent", type="mouseMoved", x=from_x, y=from_y)
            time.sleep(0.05)
            cdp.send(
                "Input.dispatchMouseEvent",
                type="mousePressed", x=from_x, y=from_y, button="left", clickCount=1,
            )
            time.sleep(0.05)

            for i in range(1, steps + 1):
                mx = from_x + (to_x - from_x) * (i / steps)
                my = from_y + (to_y - from_y) * (i / steps)
                cdp.send(
                    "Input.dispatchMouseEvent",
                    type="mouseMoved", x=mx, y=my, button="left",
                )
                time.sleep(0.02)

            cdp.send(
                "Input.dispatchMouseEvent",
                type="mouseReleased", x=to_x, y=to_y, button="left", clickCount=1,
            )
            return f"Dragged from ({from_x}, {from_y}) to ({to_x}, {to_y})"
        finally:
            cdp.close()

    def iframe_rect(self, selector: str) -> dict:
        """Get the bounding box of an iframe element.

        Useful for calculating coordinates for click_xy when targeting
        elements inside cross-origin iframes.

        Args:
            selector: CSS selector for the iframe.

        Returns:
            Dict with x, y, width, height, cx (center x), cy (center y).

        Example:
            >>> b.iframe_rect('iframe[title*="hCaptcha"]')
            {'x': 95, 'y': 440, 'width': 302, 'height': 76, 'cx': 246, 'cy': 478}
        """
        js = f"""
        (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return JSON.stringify({{ error: "Selector not found: " + {json.dumps(selector)} }});
            el.scrollIntoView({{ block: 'center' }});
            const r = el.getBoundingClientRect();
            return JSON.stringify({{ x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height), cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2) }});
        }})()
        """
        result = self._eval(js)
        info = json.loads(result)
        if "error" in info:
            raise CDPError(info["error"])
        return info

    # ── File upload ──

    def upload(self, file_path: str, selector: str = 'input[type="file"]') -> str:
        """Upload a file to a file input element.

        Bypasses the OS file picker dialog by injecting the file directly
        via CDP. Works with hidden file inputs too.

        Args:
            file_path: Path to the file to upload.
            selector: CSS selector for the file input (default: input[type="file"]).

        Returns:
            Confirmation message.

        Example:
            >>> b.upload("~/photos/avatar.jpg")
            'Uploaded: avatar.jpg → input[type="file"]'
        """
        abs_path = str(Path(file_path).expanduser().resolve())
        if not Path(abs_path).exists():
            raise FileNotFoundError(f"File not found: {abs_path}")

        cdp = self._connect_page()
        try:
            cdp.send("DOM.enable")
            root = cdp.send("DOM.getDocument")
            node = cdp.send(
                "DOM.querySelector",
                nodeId=root["root"]["nodeId"],
                selector=selector,
            )
            node_id = node.get("nodeId", 0)
            if not node_id:
                raise CDPError(
                    f"No file input found matching: {selector}\n"
                    f"Hint: Check the page with elements() or html('form')"
                )
            cdp.send("DOM.setFileInputFiles", files=[abs_path], nodeId=node_id)
            return f"Uploaded: {Path(abs_path).name} → {selector}"
        finally:
            cdp.close()

    # ── Utility ──

    def wait(self, ms: int = 1000) -> str:
        """Wait for a specified duration.

        Args:
            ms: Milliseconds to wait. Default: 1000.

        Returns:
            Confirmation message.
        """
        time.sleep(ms / 1000)
        return f"Waited {ms}ms"

    # ── Launch Chrome ──

    @staticmethod
    def launch(
        port: int = 9222,
        user_data_dir: str | None = None,
        headless: bool = False,
        chrome_path: str | None = None,
        download_dir: str | None = None,
    ) -> subprocess.Popen:
        """Launch Chrome/Chromium with remote debugging enabled.

        Creates a separate browser instance with its own profile directory.
        Your logins, cookies, and extensions in that profile persist across
        restarts — log in once, automate forever.

        Args:
            port: CDP port (default: 9222).
            user_data_dir: Where to store the browser profile. Default:
                           ~/.tappi/profile
            headless: Run without a visible window (default: False).
                      Set True for server/CI environments.
            chrome_path: Path to Chrome/Chromium binary. Auto-detected if
                         not provided.

        Returns:
            The subprocess.Popen object for the browser process.

        Example:
            >>> Browser.launch()           # Start Chrome, default profile
            >>> Browser.launch(port=9333)  # Different port
            >>> b = Browser("http://127.0.0.1:9333")
        """
        chrome = chrome_path or _find_chrome()
        if not chrome:
            raise FileNotFoundError(
                "Chrome/Chromium not found. Install it or pass chrome_path=...\n\n"
                "Install options:\n"
                "  macOS:   brew install --cask google-chrome\n"
                "  Ubuntu:  sudo apt install chromium-browser\n"
                "  Fedora:  sudo dnf install chromium"
            )

        data_dir = user_data_dir or os.path.join(
            Path.home(), ".tappi", "profile"
        )
        os.makedirs(data_dir, exist_ok=True)

        cmd = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={data_dir}",
        ]
        if headless:
            cmd.append("--headless=new")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Wait for CDP to be ready
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                json.loads(urlopen(f"http://127.0.0.1:{port}/json/version").read())
                # Set download directory if specified
                if download_dir:
                    dl_path = str(Path(download_dir).expanduser().resolve())
                    os.makedirs(dl_path, exist_ok=True)
                    try:
                        browser_cdp = CDPSession.connect_to_browser(
                            f"http://127.0.0.1:{port}"
                        )
                        # Get the first page target to set download behavior
                        targets = json.loads(
                            urlopen(f"http://127.0.0.1:{port}/json/list").read()
                        )
                        pages = [t for t in targets if t.get("type") == "page"]
                        if pages:
                            page_cdp = CDPSession.connect_to_page(
                                pages[0]["id"], port=port
                            )
                            page_cdp.send(
                                "Browser.setDownloadBehavior",
                                behavior="allow",
                                downloadPath=dl_path,
                            )
                            page_cdp.close()
                        browser_cdp.close()
                    except Exception:
                        pass  # Non-fatal — downloads still work, just default location
                return proc
            except (URLError, OSError):
                time.sleep(0.3)

        proc.kill()
        raise TimeoutError(
            f"Chrome started but CDP not ready on port {port} after 10s.\n"
            f"Check if another process is using port {port}."
        )

    def __repr__(self) -> str:
        return f"Browser(cdp_url={self.cdp_url!r})"


def _find_chrome() -> str | None:
    """Auto-detect Chrome/Chromium binary path."""
    candidates = []

    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    elif sys.platform == "linux":
        candidates = [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
            "brave-browser",
            "microsoft-edge",
        ]
    elif sys.platform == "win32":
        import glob
        for pattern in [
            os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
            os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        ]:
            candidates.extend(glob.glob(pattern))

    for c in candidates:
        if os.path.isfile(c):
            return c
        # For linux — check PATH
        if not os.path.sep in c:
            import shutil
            found = shutil.which(c)
            if found:
                return found

    return None
