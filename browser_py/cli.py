"""browser-py CLI — control your browser from the terminal.

Usage:
    browser-py <command> [args...]
    browser-py --help

Examples:
    browser-py tabs                     # See your open tabs
    browser-py open github.com          # Go to a URL
    browser-py elements                 # What can I click?
    browser-py click 3                  # Click element [3]
    browser-py type 5 "hello world"     # Type into element [5]
    browser-py text                     # Read the page
"""

from __future__ import annotations

import sys
import os
import textwrap

from browser_py.core import Browser, CDPError, BrowserNotRunning


# ── Colors (disable with NO_COLOR env var) ──

_NO_COLOR = os.environ.get("NO_COLOR") or not sys.stdout.isatty()


def _dim(s: str) -> str:
    return s if _NO_COLOR else f"\033[2m{s}\033[0m"


def _bold(s: str) -> str:
    return s if _NO_COLOR else f"\033[1m{s}\033[0m"


def _cyan(s: str) -> str:
    return s if _NO_COLOR else f"\033[36m{s}\033[0m"


def _green(s: str) -> str:
    return s if _NO_COLOR else f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    return s if _NO_COLOR else f"\033[33m{s}\033[0m"


def _red(s: str) -> str:
    return s if _NO_COLOR else f"\033[31m{s}\033[0m"


# ── Help text ──

COMMANDS_HELP = {
    "tabs": {
        "usage": "browser-py tabs",
        "desc": "List all open browser tabs with their index, title, and URL.",
        "example": (
            "  $ browser-py tabs\n"
            "  [0] Google — https://google.com\n"
            "  [1] GitHub — https://github.com"
        ),
        "hint": "Use the [index] number with 'tab' to switch tabs.",
    },
    "open": {
        "usage": "browser-py open <url>",
        "desc": "Navigate the current tab to a URL. Adds https:// if missing.",
        "example": "  $ browser-py open github.com\n  Navigated to https://github.com",
        "hint": "After navigating, run 'elements' to see what you can interact with.",
    },
    "tab": {
        "usage": "browser-py tab <index>",
        "desc": "Switch to a different tab by its index number.",
        "example": "  $ browser-py tab 2\n  Switched to tab [2]: Reddit — https://reddit.com",
        "hint": "Run 'tabs' first to see available tabs and their indices.",
    },
    "newtab": {
        "usage": "browser-py newtab [url]",
        "desc": "Open a new browser tab, optionally with a URL.",
        "example": "  $ browser-py newtab https://example.com",
    },
    "close": {
        "usage": "browser-py close [index]",
        "desc": "Close a tab. Closes the current tab if no index given.",
        "example": "  $ browser-py close 3",
    },
    "elements": {
        "usage": "browser-py elements [css-selector]",
        "desc": (
            "List all interactive elements on the page — links, buttons, inputs, etc.\n"
            "Each element gets a number you can use with 'click' and 'type'.\n"
            "Pierces shadow DOM automatically (works on Reddit, GitHub, etc.)."
        ),
        "example": (
            "  $ browser-py elements\n"
            "  [0] (link) Home → /\n"
            "  [1] (button) Sign In\n"
            "  [2] (input:text) Search\n"
            "  [3] (link) About → /about\n\n"
            "  $ browser-py elements \".sidebar\"   # Only sidebar elements"
        ),
        "hint": (
            "Elements are numbered — use 'click 1' or 'type 2 hello' to interact.\n"
            "Disabled elements show as (button:disabled)."
        ),
    },
    "click": {
        "usage": "browser-py click <index>",
        "desc": (
            "Click an element by its index number from 'elements' output.\n"
            "Uses real mouse events (works with React, Vue, Angular, etc.)."
        ),
        "example": "  $ browser-py click 1\n  Clicked: (button) Sign In",
        "hint": (
            "If the page changed since 'elements', indices may be stale.\n"
            "Run 'elements' again to re-index."
        ),
    },
    "type": {
        "usage": "browser-py type <index> <text>",
        "desc": (
            "Type text into an input element. Clears existing content first.\n"
            "Works with inputs, textareas, contenteditable, and ARIA textboxes."
        ),
        "example": (
            "  $ browser-py type 2 \"hello world\"\n"
            "  Typed into [2] (input)"
        ),
        "hint": "The element must be a text input. If it's a button or link, use 'click' instead.",
    },
    "text": {
        "usage": "browser-py text [css-selector]",
        "desc": "Extract visible text from the page (max 8KB). Pierces shadow DOM.",
        "example": (
            "  $ browser-py text\n"
            "  Welcome to GitHub. Let's build from here ...\n\n"
            "  $ browser-py text \".main-content\"   # Just the main area"
        ),
    },
    "html": {
        "usage": "browser-py html <css-selector>",
        "desc": "Get the outerHTML of a specific element (max 10KB).",
        "example": "  $ browser-py html \"nav.header\"",
    },
    "eval": {
        "usage": "browser-py eval <javascript>",
        "desc": "Run JavaScript in the page context and print the result.",
        "example": (
            "  $ browser-py eval \"document.title\"\n"
            "  GitHub\n\n"
            "  $ browser-py eval \"document.querySelectorAll('img').length\"\n"
            "  42"
        ),
    },
    "screenshot": {
        "usage": "browser-py screenshot [path]",
        "desc": "Save a screenshot of the current page.",
        "example": (
            "  $ browser-py screenshot\n"
            "  /tmp/browser_py_screenshot_1708300000.png\n\n"
            "  $ browser-py screenshot ~/Desktop/page.png"
        ),
    },
    "scroll": {
        "usage": "browser-py scroll <up|down|top|bottom> [pixels]",
        "desc": "Scroll the page in a direction. Default: 600px.",
        "example": "  $ browser-py scroll down 1000",
    },
    "url": {
        "usage": "browser-py url",
        "desc": "Print the current page URL.",
        "example": "  $ browser-py url\n  https://github.com",
    },
    "back": {
        "usage": "browser-py back",
        "desc": "Go back in browser history.",
    },
    "forward": {
        "usage": "browser-py forward",
        "desc": "Go forward in browser history.",
    },
    "refresh": {
        "usage": "browser-py refresh",
        "desc": "Reload the current page.",
    },
    "upload": {
        "usage": "browser-py upload <file-path> [css-selector]",
        "desc": (
            "Upload a file to a file input. Bypasses the OS file picker dialog.\n"
            "Default selector: input[type=\"file\"]"
        ),
        "example": (
            "  $ browser-py upload ~/photos/avatar.jpg\n"
            "  Uploaded: avatar.jpg → input[type=\"file\"]\n\n"
            "  $ browser-py upload ~/doc.pdf \"input.file-drop\""
        ),
    },
    "wait": {
        "usage": "browser-py wait <ms>",
        "desc": "Wait for a duration (useful in scripts).",
        "example": "  $ browser-py wait 2000\n  Waited 2000ms",
    },
}


def print_main_help() -> None:
    """Print the main help screen."""
    print(_bold("browser-py") + " — Control your browser from the terminal\n")
    print(_dim("Connects to Chrome/Chromium via CDP (Chrome DevTools Protocol)."))
    print(_dim("Your logged-in sessions, cookies, and extensions all carry over.\n"))

    print(_bold("Usage:") + " browser-py <command> [args...]\n")

    # Group commands
    groups = [
        (
            "Navigation",
            [
                ("open <url>", "Go to a URL"),
                ("url", "Print current URL"),
                ("back", "Go back"),
                ("forward", "Go forward"),
                ("refresh", "Reload page"),
            ],
        ),
        (
            "Tabs",
            [
                ("tabs", "List open tabs"),
                ("tab <index>", "Switch to tab"),
                ("newtab [url]", "Open new tab"),
                ("close [index]", "Close tab"),
            ],
        ),
        (
            "Interact",
            [
                ("elements [selector]", "List clickable elements (numbered)"),
                ("click <index>", "Click element by number"),
                ("type <index> <text>", "Type into element"),
                ("upload <path> [sel]", "Upload file"),
            ],
        ),
        (
            "Read",
            [
                ("text [selector]", "Extract visible text"),
                ("html <selector>", "Get element HTML"),
                ("eval <js>", "Run JavaScript"),
                ("screenshot [path]", "Save screenshot"),
            ],
        ),
        (
            "Other",
            [
                ("scroll <dir> [px]", "Scroll up/down/top/bottom"),
                ("wait <ms>", "Wait (for scripts)"),
            ],
        ),
    ]

    for group_name, cmds in groups:
        print(f"  {_cyan(group_name)}")
        for cmd, desc in cmds:
            print(f"    {cmd:<24} {_dim(desc)}")
        print()

    print(_bold("Quick start:"))
    print(_dim("  browser-py open example.com    # Navigate"))
    print(_dim("  browser-py elements            # See what's clickable"))
    print(_dim("  browser-py click 3             # Click element [3]"))
    print(_dim("  browser-py type 5 hello        # Type into element [5]"))
    print()
    print(_dim("Env: CDP_URL — override CDP endpoint (default: http://127.0.0.1:9222)"))
    print(_dim("     NO_COLOR — disable colored output"))
    print()
    print(_dim("Run 'browser-py <command> --help' for detailed help on any command."))


def print_command_help(cmd: str) -> None:
    """Print help for a specific command."""
    info = COMMANDS_HELP.get(cmd)
    if not info:
        print(f"Unknown command: {cmd}")
        print("Run 'browser-py --help' to see all commands.")
        return

    print(_bold(info["usage"]))
    print()
    print(info["desc"])

    if "example" in info:
        print(f"\n{_cyan('Example:')}")
        print(info["example"])

    if "hint" in info:
        print(f"\n{_yellow('💡 Tip:')} {info['hint']}")


# ── Command dispatch ──


def run_command(browser: Browser, cmd: str, args: list[str]) -> str | None:
    """Execute a command and return the output string."""
    if cmd == "tabs":
        tabs = browser.tabs()
        if not tabs:
            return "No tabs open."
        return "\n".join(str(t) for t in tabs)

    elif cmd == "open":
        if not args:
            print_command_help("open")
            return None
        result = browser.open(args[0])
        return result + "\n" + _dim("💡 Run 'elements' to see interactive elements on this page.")

    elif cmd == "tab":
        if not args:
            print_command_help("tab")
            return None
        return browser.tab(int(args[0]))

    elif cmd == "newtab":
        return browser.newtab(args[0] if args else None)

    elif cmd == "close":
        return browser.close_tab(int(args[0]) if args else None)

    elif cmd == "elements":
        elements = browser.elements(args[0] if args else None)
        if not elements:
            return (
                "No interactive elements found.\n"
                + _dim("💡 The page might still be loading. Try: wait 1000, then elements again.\n")
                + _dim("   Or narrow down with a selector: elements \".content\"")
            )
        lines = [str(e) for e in elements]
        lines.append("")
        lines.append(
            _dim(f"💡 {len(elements)} elements found. Use 'click <number>' or 'type <number> <text>' to interact.")
        )
        return "\n".join(lines)

    elif cmd == "click":
        if not args:
            print_command_help("click")
            return None
        return browser.click(int(args[0]))

    elif cmd == "type":
        if len(args) < 2:
            print_command_help("type")
            return None
        index = int(args[0])
        text = " ".join(args[1:])
        return browser.type(index, text)

    elif cmd == "text":
        return browser.text(args[0] if args else None)

    elif cmd == "html":
        if not args:
            print_command_help("html")
            return None
        return browser.html(args[0])

    elif cmd == "eval":
        if not args:
            print_command_help("eval")
            return None
        result = browser.eval(" ".join(args))
        if isinstance(result, str):
            return result
        if result is None:
            return "(undefined)"
        import json
        return json.dumps(result, indent=2)

    elif cmd == "screenshot":
        path = browser.screenshot(args[0] if args else None)
        return f"Screenshot saved: {path}"

    elif cmd == "scroll":
        if not args:
            print_command_help("scroll")
            return None
        amount = int(args[1]) if len(args) > 1 else 600
        return browser.scroll(args[0], amount)

    elif cmd == "url":
        return browser.url()

    elif cmd == "back":
        return browser.back()

    elif cmd == "forward":
        return browser.forward()

    elif cmd == "refresh":
        return browser.refresh()

    elif cmd == "upload":
        if not args:
            print_command_help("upload")
            return None
        selector = args[1] if len(args) > 1 else 'input[type="file"]'
        return browser.upload(args[0], selector)

    elif cmd == "wait":
        ms = int(args[0]) if args else 1000
        return browser.wait(ms)

    else:
        print(_red(f"Unknown command: {cmd}"))
        print("Run 'browser-py --help' to see all commands.")
        sys.exit(1)


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]

    # No args or help flag
    if not args or args[0] in ("--help", "-h", "help"):
        print_main_help()
        return

    cmd = args[0].lower()
    cmd_args = args[1:]

    # Per-command help
    if cmd_args and cmd_args[0] in ("--help", "-h"):
        print_command_help(cmd)
        return

    # Version
    if cmd in ("--version", "-V", "version"):
        from browser_py import __version__
        print(f"browser-py {__version__}")
        return

    browser = Browser()

    try:
        result = run_command(browser, cmd, cmd_args)
        if result is not None:
            print(result)
    except BrowserNotRunning as e:
        print(_red("✗ Browser not running\n"))
        print(str(e))
        sys.exit(1)
    except CDPError as e:
        print(_red(f"✗ {e}"))
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(_red(f"✗ Error: {e}"))
        sys.exit(1)


if __name__ == "__main__":
    main()
