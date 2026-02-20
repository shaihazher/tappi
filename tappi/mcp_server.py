"""tappi MCP server — expose browser control as MCP tools.

Usage:
    tappi mcp              # stdio transport (Claude Desktop, Cursor, etc.)
    tappi mcp --sse        # HTTP/SSE transport (port 8377)
    tappi mcp --sse --port 9000

Or run directly:
    python -m tappi.mcp_server
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from tappi.core import Browser, BrowserNotRunning, CDPError

# ── Server setup ──

mcp = FastMCP(
    "tappi",
    instructions=(
        "Browser control via Chrome DevTools Protocol. "
        "Connects to your existing Chrome — all sessions, cookies, extensions carry over. "
        "Pierces shadow DOM. 3-10x fewer tokens than accessibility tree tools."
    ),
)

# Lazy browser singleton — connects on first tool call
_browser: Browser | None = None


def _get_browser() -> Browser:
    global _browser
    if _browser is None:
        cdp_url = os.environ.get("CDP_URL")
        _browser = Browser(cdp_url)
    return _browser


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
    """Type text into an input element by index. Clears existing content first.

    Works with inputs, textareas, contenteditable, and ARIA textboxes.
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
