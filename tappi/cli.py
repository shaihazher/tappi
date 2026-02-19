"""tappi CLI — control your browser from the terminal.

Usage:
    tappi <command> [args...]
    tappi --help

Examples:
    tappi tabs                     # See your open tabs
    tappi open github.com          # Go to a URL
    tappi elements                 # What can I click?
    tappi click 3                  # Click element [3]
    tappi type 5 "hello world"     # Type into element [5]
    tappi text                     # Read the page
"""

from __future__ import annotations

import sys
import os
import textwrap

from tappi.core import Browser, CDPError, BrowserNotRunning, _find_chrome
from tappi.profiles import (
    list_profiles,
    get_profile,
    create_profile,
    set_default,
    delete_profile,
)


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
    "launch": {
        "usage": "tappi launch [name] [--headless] [--port PORT]",
        "desc": (
            "Start Chrome with a named profile.\n\n"
            "Each profile has its own browser sessions (cookies, logins) and\n"
            "its own CDP port. Profiles live in ~/.tappi/profiles/<name>/.\n\n"
            "Subcommands:\n"
            "  launch              Launch the default profile\n"
            "  launch <name>       Launch a specific profile\n"
            "  launch new [name]   Create a new profile\n"
            "  launch list         List all profiles\n"
            "  launch --default <name>   Set the default profile\n"
            "  launch delete <name>      Delete a profile"
        ),
        "example": (
            "  $ tappi launch\n"
            "  ✓ Chrome launched — profile: default (port 9222)\n\n"
            "  $ tappi launch new work\n"
            "  ✓ Created profile 'work' (port 9223)\n\n"
            "  $ tappi launch work\n"
            "  ✓ Chrome launched — profile: work (port 9223)\n\n"
            "  $ tappi launch list\n"
            "  default  port 9222  ★ default\n"
            "  work     port 9223\n\n"
            "  $ tappi launch --default work\n"
            "  ✓ Default profile set to 'work'"
        ),
        "hint": (
            "First launch of a profile? A fresh Chrome window opens.\n"
            "Log into your sites — sessions persist for all future launches.\n"
            "Each profile gets its own port, so you can run multiple simultaneously."
        ),
    },
    "tabs": {
        "usage": "tappi tabs",
        "desc": "List all open browser tabs with their index, title, and URL.",
        "example": (
            "  $ tappi tabs\n"
            "  [0] Google — https://google.com\n"
            "  [1] GitHub — https://github.com"
        ),
        "hint": "Use the [index] number with 'tab' to switch tabs.",
    },
    "open": {
        "usage": "tappi open <url>",
        "desc": "Navigate the current tab to a URL. Adds https:// if missing.",
        "example": "  $ tappi open github.com\n  Navigated to https://github.com",
        "hint": "After navigating, run 'elements' to see what you can interact with.",
    },
    "tab": {
        "usage": "tappi tab <index>",
        "desc": "Switch to a different tab by its index number.",
        "example": "  $ tappi tab 2\n  Switched to tab [2]: Reddit — https://reddit.com",
        "hint": "Run 'tabs' first to see available tabs and their indices.",
    },
    "newtab": {
        "usage": "tappi newtab [url]",
        "desc": "Open a new browser tab, optionally with a URL.",
        "example": "  $ tappi newtab https://example.com",
    },
    "close": {
        "usage": "tappi close [index]",
        "desc": "Close a tab. Closes the current tab if no index given.",
        "example": "  $ tappi close 3",
    },
    "elements": {
        "usage": "tappi elements [css-selector]",
        "desc": (
            "List all interactive elements on the page — links, buttons, inputs, etc.\n"
            "Each element gets a number you can use with 'click' and 'type'.\n"
            "Pierces shadow DOM automatically (works on Reddit, GitHub, etc.)."
        ),
        "example": (
            "  $ tappi elements\n"
            "  [0] (link) Home → /\n"
            "  [1] (button) Sign In\n"
            "  [2] (input:text) Search\n"
            "  [3] (link) About → /about\n\n"
            "  $ tappi elements \".sidebar\"   # Only sidebar elements"
        ),
        "hint": (
            "Elements are numbered — use 'click 1' or 'type 2 hello' to interact.\n"
            "Disabled elements show as (button:disabled)."
        ),
    },
    "click": {
        "usage": "tappi click <index>",
        "desc": (
            "Click an element by its index number from 'elements' output.\n"
            "Uses real mouse events (works with React, Vue, Angular, etc.)."
        ),
        "example": "  $ tappi click 1\n  Clicked: (button) Sign In",
        "hint": (
            "If the page changed since 'elements', indices may be stale.\n"
            "Run 'elements' again to re-index."
        ),
    },
    "type": {
        "usage": "tappi type <index> <text>",
        "desc": (
            "Type text into an input element. Clears existing content first.\n"
            "Works with inputs, textareas, contenteditable, and ARIA textboxes."
        ),
        "example": (
            "  $ tappi type 2 \"hello world\"\n"
            "  Typed into [2] (input)"
        ),
        "hint": "The element must be a text input. If it's a button or link, use 'click' instead.",
    },
    "text": {
        "usage": "tappi text [css-selector]",
        "desc": "Extract visible text from the page (max 8KB). Pierces shadow DOM.",
        "example": (
            "  $ tappi text\n"
            "  Welcome to GitHub. Let's build from here ...\n\n"
            "  $ tappi text \".main-content\"   # Just the main area"
        ),
    },
    "html": {
        "usage": "tappi html <css-selector>",
        "desc": "Get the outerHTML of a specific element (max 10KB).",
        "example": "  $ tappi html \"nav.header\"",
    },
    "eval": {
        "usage": "tappi eval <javascript>",
        "desc": "Run JavaScript in the page context and print the result.",
        "example": (
            "  $ tappi eval \"document.title\"\n"
            "  GitHub\n\n"
            "  $ tappi eval \"document.querySelectorAll('img').length\"\n"
            "  42"
        ),
    },
    "screenshot": {
        "usage": "tappi screenshot [path]",
        "desc": "Save a screenshot of the current page.",
        "example": (
            "  $ tappi screenshot\n"
            "  /tmp/tappi_screenshot_1708300000.png\n\n"
            "  $ tappi screenshot ~/Desktop/page.png"
        ),
    },
    "scroll": {
        "usage": "tappi scroll <up|down|top|bottom> [pixels]",
        "desc": "Scroll the page in a direction. Default: 600px.",
        "example": "  $ tappi scroll down 1000",
    },
    "url": {
        "usage": "tappi url",
        "desc": "Print the current page URL.",
        "example": "  $ tappi url\n  https://github.com",
    },
    "back": {
        "usage": "tappi back",
        "desc": "Go back in browser history.",
    },
    "forward": {
        "usage": "tappi forward",
        "desc": "Go forward in browser history.",
    },
    "refresh": {
        "usage": "tappi refresh",
        "desc": "Reload the current page.",
    },
    "upload": {
        "usage": "tappi upload <file-path> [css-selector]",
        "desc": (
            "Upload a file to a file input. Bypasses the OS file picker dialog.\n"
            "Default selector: input[type=\"file\"]"
        ),
        "example": (
            "  $ tappi upload ~/photos/avatar.jpg\n"
            "  Uploaded: avatar.jpg → input[type=\"file\"]\n\n"
            "  $ tappi upload ~/doc.pdf \"input.file-drop\""
        ),
    },
    "wait": {
        "usage": "tappi wait <ms>",
        "desc": "Wait for a duration (useful in scripts).",
        "example": "  $ tappi wait 2000\n  Waited 2000ms",
    },
    "click-xy": {
        "usage": "tappi click-xy <x> <y> [--double] [--right]",
        "desc": (
            "Click at page coordinates via CDP Input events.\n\n"
            "Bypasses all DOM boundaries — works inside cross-origin iframes\n"
            "(captchas, payment forms, OAuth widgets, embedded content)."
        ),
        "example": (
            "  $ tappi click-xy 125 458\n"
            "  Clicked at (125, 458)\n\n"
            "  $ tappi click-xy 300 200 --double\n"
            "  Double-clicked at (300, 200)"
        ),
    },
    "hover-xy": {
        "usage": "tappi hover-xy <x> <y>",
        "desc": "Hover at page coordinates (triggers hover menus, tooltips).",
        "example": "  $ tappi hover-xy 400 300\n  Hovered at (400, 300)",
    },
    "drag-xy": {
        "usage": "tappi drag-xy <x1> <y1> <x2> <y2>",
        "desc": "Drag from one coordinate to another (sliders, canvas, drag-and-drop).",
        "example": "  $ tappi drag-xy 100 200 400 200\n  Dragged from (100, 200) to (400, 200)",
    },
    "iframe-rect": {
        "usage": "tappi iframe-rect <css-selector>",
        "desc": (
            "Get bounding box of an iframe element.\n\n"
            "Returns x, y, width, height, and center coordinates.\n"
            "Use with click-xy to target elements inside cross-origin iframes."
        ),
        "example": (
            "  $ tappi iframe-rect 'iframe[title*=\"hCaptcha\"]'\n"
            "  x=95 y=440 w=302 h=76 center=(246, 478)"
        ),
    },
}


def print_main_help() -> None:
    """Print the main help screen."""
    print(_bold("tappi") + " — Control your browser from the terminal\n")
    print(_dim("Connects to Chrome/Chromium via CDP (Chrome DevTools Protocol)."))
    print(_dim("Your logged-in sessions, cookies, and extensions all carry over.\n"))

    print(_bold("Usage:") + " tappi <command> [args...]\n")

    # Group commands
    groups = [
        (
            "Agent",
            [
                ("setup", "Configure LLM provider, workspace, browser"),
                ("agent [message]", "Chat with the agent (interactive or one-shot)"),
                ("research <query>", "Deep research with 5 sub-agents"),
                ("serve [--port 8321]", "Start the web UI"),
            ],
        ),
        (
            "Browser",
            [
                ("launch [name]", "Start Chrome (default or named profile)"),
                ("launch new [name]", "Create a new profile"),
                ("launch list", "List all profiles"),
                ("launch --default <name>", "Set the default profile"),
            ],
        ),
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
    print(_dim("  tappi open example.com    # Navigate"))
    print(_dim("  tappi elements            # See what's clickable"))
    print(_dim("  tappi click 3             # Click element [3]"))
    print(_dim("  tappi type 5 hello        # Type into element [5]"))
    print()
    print(_dim("Env: CDP_URL — override CDP endpoint (default: http://127.0.0.1:9222)"))
    print(_dim("     NO_COLOR — disable colored output"))
    print()
    print(_dim("Run 'tappi <command> --help' for detailed help on any command."))


def print_command_help(cmd: str) -> None:
    """Print help for a specific command."""
    info = COMMANDS_HELP.get(cmd)
    if not info:
        print(f"Unknown command: {cmd}")
        print("Run 'tappi --help' to see all commands.")
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


def run_launch(args: list[str]) -> str:
    """Handle the launch command with profile management."""
    import json as _json
    from urllib.request import urlopen
    from urllib.error import URLError

    # Parse flags
    headless = False
    chrome_path = None
    port_override = None
    default_name = None
    positional = []

    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--headless":
            headless = True
            i += 1
        elif arg in ("--chrome", "--browser") and i + 1 < len(args):
            chrome_path = args[i + 1]
            i += 2
        elif arg in ("--port", "-p") and i + 1 < len(args):
            port_override = int(args[i + 1])
            i += 2
        elif arg == "--default" and i + 1 < len(args):
            default_name = args[i + 1]
            i += 2
        else:
            positional.append(arg)
            i += 1

    # Handle --default flag
    if default_name is not None:
        set_default(default_name)
        return f"✓ Default profile set to {_bold(default_name)}"

    subcmd = positional[0] if positional else None

    # ── launch list ──
    if subcmd == "list":
        profiles = list_profiles()
        if not profiles:
            return (
                "No profiles yet.\n"
                + _dim("Create one with: tappi launch new <name>")
            )
        lines = [_bold("Profiles:"), ""]
        max_name = max(len(p["name"]) for p in profiles)
        for p in profiles:
            default_marker = _yellow(" ★ default") if p["is_default"] else ""
            lines.append(
                f"  {p['name']:<{max_name}}  port {p['port']}{default_marker}"
            )
        lines.append("")
        lines.append(_dim("Launch with: tappi launch <name>"))
        return "\n".join(lines)

    # ── launch new [name] ──
    if subcmd == "new":
        name = positional[1] if len(positional) > 1 else None
        if not name:
            # Interactive: ask for name
            try:
                name = input("Profile name: ").strip()
            except (EOFError, KeyboardInterrupt):
                return "Cancelled."
        if not name:
            return _red("Profile name cannot be empty.")

        profile = create_profile(name, port=port_override)
        lines = [
            f"✓ Created profile {_bold(profile['name'])} (port {profile['port']})",
            f"  Path: {_dim(profile['path'])}",
        ]
        if profile["is_default"]:
            lines.append(f"  {_yellow('★ Set as default')}")
        lines.append("")

        # Auto-launch it
        return "\n".join(lines) + "\n" + _launch_profile(
            profile, headless=headless, chrome_path=chrome_path
        )

    # ── launch delete <name> ──
    if subcmd == "delete":
        if len(positional) < 2:
            return _red("Usage: tappi launch delete <name>")
        name = positional[1]
        msg = delete_profile(name)
        return f"✓ {msg}"

    # ── launch [name] ──
    profile_name = subcmd  # None means default

    try:
        profile = get_profile(profile_name)
    except ValueError:
        # Profile doesn't exist — offer to create it
        if profile_name:
            return (
                _red(f"Profile '{profile_name}' not found.\n")
                + f"\nCreate it with: {_bold(f'tappi launch new {profile_name}')}\n"
                + f"Or list existing: {_bold('tappi launch list')}"
            )
        # No profiles at all — create "default"
        profile = create_profile("default", port=port_override or 9222)

    if port_override:
        profile["port"] = port_override

    return _launch_profile(profile, headless=headless, chrome_path=chrome_path)


def _launch_profile(
    profile: dict, *, headless: bool = False, chrome_path: str | None = None
) -> str:
    """Launch Chrome for a specific profile."""
    import json as _json
    from urllib.request import urlopen
    from urllib.error import URLError

    port = profile["port"]
    data_dir = profile["path"]
    name = profile["name"]

    # Check if already running on this port
    try:
        _json.loads(urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read())
        return (
            f"✓ Profile {_bold(name)} already running (port {port})\n"
            + _dim("  Ready to use — try: tappi tabs")
        )
    except (URLError, OSError):
        pass

    is_first = not os.path.exists(os.path.join(data_dir, "Default"))

    Browser.launch(
        port=port,
        user_data_dir=data_dir,
        headless=headless,
        chrome_path=chrome_path,
    )

    lines = [
        f"✓ Chrome launched — profile: {_bold(name)} (port {port})",
        f"  Path: {_dim(data_dir)}",
    ]

    if is_first:
        lines.append("")
        lines.append(_yellow("⚡ First launch — a fresh Chrome window opened."))
        lines.append("   Log into the sites you want to automate (Gmail, GitHub, etc.).")
        lines.append("   Those sessions will persist for all future launches.")
        lines.append("")
        lines.append(_dim("   When ready, open another terminal and run:"))
        if port != 9222:
            lines.append(_dim(f"   CDP_URL=http://127.0.0.1:{port} tappi tabs"))
        else:
            lines.append(_dim("   tappi tabs"))
    else:
        lines.append("")
        lines.append(_dim("Ready — your saved sessions are active."))
        if port != 9222:
            lines.append(_dim(f"Connect with: CDP_URL=http://127.0.0.1:{port} tappi <command>"))
        else:
            lines.append(_dim("Try: tappi tabs"))

    return "\n".join(lines)


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

    elif cmd == "click-xy":
        if len(args) < 2:
            print_command_help("click-xy")
            return None
        coords = [a for a in args if not a.startswith("--")]
        double = "--double" in args
        right = "--right" in args
        return browser.click_xy(float(coords[0]), float(coords[1]), double=double, right=right)

    elif cmd == "hover-xy":
        if len(args) < 2:
            print_command_help("hover-xy")
            return None
        return browser.hover_xy(float(args[0]), float(args[1]))

    elif cmd == "drag-xy":
        if len(args) < 4:
            print_command_help("drag-xy")
            return None
        return browser.drag_xy(float(args[0]), float(args[1]), float(args[2]), float(args[3]))

    elif cmd == "iframe-rect":
        if not args:
            print_command_help("iframe-rect")
            return None
        info = browser.iframe_rect(" ".join(args))
        return f"x={info['x']} y={info['y']} w={info['width']} h={info['height']} center=({info['cx']}, {info['cy']})"

    else:
        print(_red(f"Unknown command: {cmd}"))
        print("Run 'tappi --help' to see all commands.")
        sys.exit(1)


def run_agent(args: list[str]) -> None:
    """Run the agent with a one-shot message or interactive mode."""
    from tappi.agent.config import is_configured

    if not is_configured():
        print(_yellow("Agent not configured. Running setup first...\n"))
        from tappi.agent.setup import run_setup
        run_setup()
        return

    if not args:
        # Interactive mode
        from tappi.agent.loop import Agent
        from tappi.agent.config import get_agent_config

        cfg = get_agent_config()
        agent = Agent(
            browser_profile=cfg.get("browser_profile"),
            on_tool_call=lambda name, params, result: print(
                _dim(f"  🔧 {name} → {params.get('action', '')}") +
                (f"\n{_dim('     ' + result[:200])}" if result else "")
            ),
        )
        if not cfg.get("shell_enabled", True):
            agent._shell.enabled = False

        print(_bold("tappi agent") + _dim(" (type 'quit' to exit, 'reset' to clear)\n"))

        while True:
            try:
                user_input = input(_cyan("You: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if user_input.lower() == "reset":
                agent.reset()
                print(_dim("Chat cleared.\n"))
                continue

            print()
            response = agent.chat(user_input)
            print(f"\n{_green('Agent:')} {response}\n")
    else:
        # One-shot mode
        message = " ".join(args)
        from tappi.agent.loop import Agent
        from tappi.agent.config import get_agent_config

        cfg = get_agent_config()
        agent = Agent(
            browser_profile=cfg.get("browser_profile"),
            on_tool_call=lambda name, params, result: print(
                _dim(f"  🔧 {name} → {params.get('action', '')}"),
            ),
        )
        if not cfg.get("shell_enabled", True):
            agent._shell.enabled = False

        response = agent.chat(message)
        print(response)


def run_research_cli(args: list[str]) -> None:
    """Run deep research from the CLI."""
    from tappi.agent.config import is_configured

    if not is_configured():
        print(_yellow("Agent not configured. Run 'bpy setup' first."))
        return

    if not args:
        print(_bold("Usage:") + " bpy research <query>")
        print(_dim("Example: bpy research 'What are the best Python web frameworks in 2025?'"))
        return

    query = " ".join(args)
    print(_bold(f"🔬 Deep Research: {query}\n"))
    print(_dim(f"Deploying 5 sub-agents to research this topic...\n"))

    def on_progress(stage: str, message: str) -> None:
        icon = "✅" if stage in ("planned", "researched", "complete", "done") else "⏳"
        print(f"  {icon} {message}")

    from tappi.agent.research import run_research
    from tappi.agent.config import get_agent_config

    cfg = get_agent_config()
    result = run_research(
        query=query,
        on_progress=on_progress,
        browser_profile=cfg.get("browser_profile"),
    )

    print(f"\n{_green('Report saved to:')} {result['report_path']}")
    duration = result["duration_seconds"]
    print(f"{_dim(f'Duration: {duration:.0f}s')}")
    print(f"\n{_bold('--- Report Preview ---')}\n")
    print(result["report"][:3000])
    if len(result["report"]) > 3000:
        print(_dim(f"\n... ({len(result['report'])} chars total — see full report at path above)"))


def run_serve(args: list[str]) -> None:
    """Start the web UI server."""
    # Don't require CLI setup — the web UI has its own setup flow
    host = "127.0.0.1"
    port = 8321

    i = 0
    while i < len(args):
        if args[i] in ("--port", "-p") and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] in ("--host",) and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        else:
            i += 1

    from tappi.server.app import start_server
    start_server(host=host, port=port)


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
        from tappi import __version__
        print(f"tappi {__version__}")
        return

    try:
        # Agent commands
        if cmd == "setup":
            from tappi.agent.setup import run_setup
            run_setup()
            return

        if cmd == "agent":
            run_agent(cmd_args)
            return

        if cmd == "research":
            run_research_cli(cmd_args)
            return

        if cmd == "serve":
            run_serve(cmd_args)
            return

        # Launch doesn't need an existing browser connection
        if cmd == "launch":
            result = run_launch(cmd_args)
            if result:
                print(result)
            return

        browser = Browser()
        result = run_command(browser, cmd, cmd_args)
        if result is not None:
            print(result)
    except BrowserNotRunning as e:
        print(_red("✗ Browser not running\n"))
        print(str(e))
        print()
        print(_yellow("💡 Quick fix:") + " run " + _bold("tappi launch") + " to start Chrome with remote debugging.")
        sys.exit(1)
    except CDPError as e:
        print(_red(f"✗ {e}"))
        sys.exit(1)
    except FileNotFoundError as e:
        print(_red(f"✗ {e}"))
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(_red(f"✗ Error: {e}"))
        sys.exit(1)


if __name__ == "__main__":
    main()
