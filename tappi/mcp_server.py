"""tappi MCP server — expose browser control as MCP tools.

Usage:
    tappi mcp              # stdio transport (Claude Desktop, Cursor, etc.)
    tappi mcp --sse        # HTTP/SSE transport (port 8377)
    tappi mcp --sse --port 9000

Or run directly:
    python -m tappi.mcp_server
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from mcp.server.fastmcp import FastMCP

from tappi.core import Browser, BrowserNotRunning, CDPError

# ── Server setup ──

mcp = FastMCP(
    "tappi",
    instructions=(
        "Browser control via Chrome DevTools Protocol. "
        "Connects to your existing Chrome — all sessions, cookies, extensions carry over. "
        "Pierces shadow DOM. 3-10x fewer tokens than accessibility tree tools. "
        "If Chrome is not running, tappi will auto-launch it on the first tool call."
    ),
)

# Lazy browser singleton — connects on first tool call, auto-launches Chrome if needed
_browser: Browser | None = None
_cdp_port: int = 9222


def _parse_cdp_port() -> int:
    """Extract port from CDP_URL env var."""
    cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
    try:
        return int(cdp_url.rsplit(":", 1)[-1].rstrip("/"))
    except (ValueError, IndexError):
        return 9222


def _is_chrome_running(port: int) -> bool:
    """Check if Chrome is reachable on the given port."""
    try:
        urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2)
        return True
    except (URLError, OSError):
        return False


def _auto_launch_chrome(port: int) -> None:
    """Launch Chrome with CDP if not already running."""
    if _is_chrome_running(port):
        return

    # Launch Chrome with a persistent profile
    user_data_dir = os.path.join(os.path.expanduser("~"), ".tappi", "profiles", "default")
    try:
        Browser.launch(port=port, user_data_dir=user_data_dir)
        # Wait for Chrome to be ready
        for _ in range(15):
            if _is_chrome_running(port):
                return
            time.sleep(0.5)
    except Exception:
        pass  # Will fail naturally when Browser() tries to connect


def _get_browser() -> Browser:
    global _browser, _cdp_port
    if _browser is None:
        _cdp_port = _parse_cdp_port()
        _auto_launch_chrome(_cdp_port)
        cdp_url = os.environ.get("CDP_URL")
        _browser = Browser(cdp_url)
    return _browser


def _reset_browser() -> None:
    """Reset browser connection (used after launch/relaunch)."""
    global _browser
    _browser = None


def _error(msg: str) -> str:
    return json.dumps({"error": msg})


# ── Tools ──


@mcp.tool()
def tappi_tabs() -> str:
    """List all open browser tabs with their index, title, and URL."""
    try:
        b = _get_browser()
        tabs = b.tabs()
        if not tabs:
            return "No tabs open."
        return "\n".join(str(t) for t in tabs)
    except BrowserNotRunning as e:
        return _error(f"Browser not running: {e}")
    except CDPError as e:
        return _error(str(e))


@mcp.tool()
def tappi_open(url: str) -> str:
    """Navigate the current tab to a URL. Adds https:// if missing."""
    try:
        b = _get_browser()
        return b.open(url)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_tab(index: int) -> str:
    """Switch to a different tab by its index number."""
    try:
        b = _get_browser()
        return b.tab(index)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_newtab(url: str = "") -> str:
    """Open a new browser tab, optionally with a URL."""
    try:
        b = _get_browser()
        return b.newtab(url or None)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_close(index: int = -1) -> str:
    """Close a tab by index. Closes the current tab if index is -1."""
    try:
        b = _get_browser()
        return b.close_tab(index if index >= 0 else None)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_elements(selector: str = "") -> str:
    """List all interactive elements on the page (links, buttons, inputs, etc.).

    Each element gets a numbered index for use with tappi_click and tappi_type.
    Pierces shadow DOM automatically (works on Reddit, Gmail, GitHub, etc.).
    Optionally pass a CSS selector to narrow scope.
    """
    try:
        b = _get_browser()
        elements = b.elements(selector or None)
        if not elements:
            return "No interactive elements found."
        return "\n".join(str(e) for e in elements)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_click(index: int) -> str:
    """Click an element by its index number from tappi_elements output.

    Uses real mouse events (works with React, Vue, Angular SPAs).
    Auto-re-indexes elements if the page has changed.
    """
    try:
        b = _get_browser()
        return b.click(index)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_type(index: int, text: str) -> str:
    """Type text into a DOM input element by index. Clears existing content first.

    Works with inputs, textareas, contenteditable, and ARIA textboxes.

    NOTE: This targets DOM elements only. Canvas-based apps (Google Sheets,
    Docs, Slides, Figma) render content on <canvas> — their cell/content
    areas aren't DOM elements, so tappi_type won't work on them. Use
    tappi_keys() instead for raw CDP keyboard input in canvas apps.
    Navigation elements (name box, menus, toolbars) ARE still DOM — use
    tappi_type for those.
    """
    try:
        b = _get_browser()
        return b.type(index, text)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_text(selector: str = "") -> str:
    """Extract visible text from the page (max 8KB). Pierces shadow DOM.

    Optionally pass a CSS selector to extract text from a specific element.
    """
    try:
        b = _get_browser()
        return b.text(selector or None)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_html(selector: str) -> str:
    """Get the outerHTML of a specific element (max 10KB)."""
    try:
        b = _get_browser()
        return b.html(selector)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_eval(javascript: str) -> str:
    """Run JavaScript in the page context and return the result."""
    try:
        b = _get_browser()
        result = b.eval(javascript)
        if result is None:
            return "(undefined)"
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_screenshot() -> str:
    """Take a screenshot of the current page. Returns the file path."""
    try:
        b = _get_browser()
        path = b.screenshot()
        return f"Screenshot saved: {path}"
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_scroll(direction: str, pixels: int = 600) -> str:
    """Scroll the page. Direction: up, down, top, bottom. Default: 600px."""
    try:
        b = _get_browser()
        return b.scroll(direction, pixels)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_url() -> str:
    """Get the current page URL."""
    try:
        b = _get_browser()
        return b.url()
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_back() -> str:
    """Go back in browser history."""
    try:
        b = _get_browser()
        return b.back()
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_forward() -> str:
    """Go forward in browser history."""
    try:
        b = _get_browser()
        return b.forward()
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_refresh() -> str:
    """Reload the current page."""
    try:
        b = _get_browser()
        return b.refresh()
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_upload(file_path: str, selector: str = 'input[type="file"]') -> str:
    """Upload a file to a file input element. Bypasses the OS file picker dialog."""
    try:
        b = _get_browser()
        return b.upload(file_path, selector)
    except (BrowserNotRunning, CDPError, FileNotFoundError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_click_xy(x: float, y: float, double_click: bool = False, right_click: bool = False) -> str:
    """Click at page coordinates via CDP Input events.

    Bypasses all DOM boundaries — works inside cross-origin iframes
    (captchas, payment forms, OAuth widgets). Use tappi_iframe_rect
    to find coordinates of iframe elements.
    """
    try:
        b = _get_browser()
        return b.click_xy(x, y, double=double_click, right=right_click)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_hover_xy(x: float, y: float) -> str:
    """Hover at page coordinates (triggers hover menus, tooltips)."""
    try:
        b = _get_browser()
        return b.hover_xy(x, y)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_drag_xy(x1: float, y1: float, x2: float, y2: float) -> str:
    """Drag from one coordinate to another (sliders, canvas, drag-and-drop)."""
    try:
        b = _get_browser()
        return b.drag_xy(x1, y1, x2, y2)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_iframe_rect(selector: str) -> str:
    """Get bounding box of an iframe element.

    Returns x, y, width, height, and center coordinates.
    Use with tappi_click_xy to target elements inside cross-origin iframes.
    """
    try:
        b = _get_browser()
        info = b.iframe_rect(selector)
        return f"x={info['x']} y={info['y']} w={info['width']} h={info['height']} center=({info['cx']}, {info['cy']})"
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_wait(ms: int = 1000) -> str:
    """Wait for a duration in milliseconds."""
    try:
        b = _get_browser()
        return b.wait(ms)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_keys(actions: list[str]) -> str:
    """Send raw CDP keyboard events — bypasses DOM, works on canvas apps.

    Use for canvas-based apps like Google Sheets, Docs, Slides, Figma where
    tappi_type() can't target canvas content areas (they render on <canvas>,
    not DOM elements). Navigation elements (name box, menus, toolbars) are
    still regular DOM — use tappi_click/tappi_type for those.

    Actions is a list of strings that get executed in order:
    - Plain text: typed character by character via CDP keyboard events
    - Key flags: --enter, --tab, --escape, --backspace, --delete,
      --up, --down, --left, --right, --home, --end, --space
    - Key combos: --combo followed by combo string (e.g. cmd+b, ctrl+a,
      cmd+shift+z). Supports cmd/ctrl/shift/alt modifiers.
    - Delay: --delay followed by ms (per-character delay, default 10)

    These can be freely chained in a single call:
      ["Revenue", "--tab", "Q1", "--tab", "Q2", "--enter"]
      types "Revenue", presses Tab, types "Q1", presses Tab, types "Q2", presses Enter.

    More examples:
      ["--combo", "cmd+b"]                    Bold (Mac)
      ["--combo", "cmd+a", "--delete"]        Select all + delete
      ["--delay", "50", "slow typing"]        50ms per character

    Google Sheets tip: --tab moves between columns within a row, but
    --enter does NOT reliably advance to the next row. Instead, navigate
    to each row start via the Name Box (click it with tappi_click, type
    the cell ref with tappi_type, press --enter to navigate), then use
    --tab within that row. Pattern: Name Box per row, Tab within rows.
    """
    try:
        b = _get_browser()
        return b.keys(*actions)
    except (BrowserNotRunning, CDPError) as e:
        return _error(str(e))


@mcp.tool()
def tappi_launch(port: int = 0, profile: str = "default", headless: bool = False) -> str:
    """Launch Chrome with remote debugging enabled.

    Starts a Chrome instance with a persistent profile. All logins, cookies,
    and extensions in that profile persist across restarts — log in once,
    automate forever.

    If Chrome is already running on the target port, returns status without
    relaunching. Use this to start Chrome explicitly, switch profiles, or
    restart after closing the browser window.

    Args:
        port: CDP port (default: uses CDP_URL env or 9222).
        profile: Profile name — each profile has its own sessions/cookies.
                 Default: "default". Use different names for different accounts.
        headless: Run without a visible window (for server/CI environments).
    """
    try:
        target_port = port if port > 0 else _parse_cdp_port()
        user_data_dir = os.path.join(
            os.path.expanduser("~"), ".tappi", "profiles", profile
        )

        if _is_chrome_running(target_port):
            return f"Chrome already running on port {target_port} (profile: {profile}). Ready to use."

        Browser.launch(port=target_port, user_data_dir=user_data_dir, headless=headless)

        # Wait for Chrome to be ready
        for _ in range(15):
            if _is_chrome_running(target_port):
                _reset_browser()
                is_first = not os.path.exists(os.path.join(user_data_dir, "Default", "Preferences"))
                msg = f"Chrome launched on port {target_port} (profile: {profile})."
                if is_first:
                    msg += (
                        "\n\nThis is a fresh profile — a Chrome window has opened. "
                        "Log into the sites you want to automate (Gmail, GitHub, etc.). "
                        "Those sessions will persist for all future launches."
                    )
                else:
                    msg += " Your saved sessions are active."
                return msg
            time.sleep(0.5)

        return _error("Chrome launched but didn't become ready within 7 seconds. Check if another instance is blocking the port.")
    except Exception as e:
        return _error(f"Failed to launch Chrome: {e}")


# ── Entry point ──


def run_stdio() -> None:
    """Run the MCP server with stdio transport."""
    mcp.run(transport="stdio")


def run_sse(host: str = "127.0.0.1", port: int = 8377) -> None:
    """Run the MCP server with SSE transport."""
    mcp.run(transport="sse", host=host, port=port)


def main() -> None:
    """CLI entry point for 'tappi mcp'."""
    args = sys.argv[1:]

    if "--sse" in args:
        host = "127.0.0.1"
        port = 8377
        for i, a in enumerate(args):
            if a in ("--host",) and i + 1 < len(args):
                host = args[i + 1]
            if a in ("--port", "-p") and i + 1 < len(args):
                port = int(args[i + 1])
        print(f"tappi MCP server (SSE) listening on {host}:{port}", file=sys.stderr)
        run_sse(host=host, port=port)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
