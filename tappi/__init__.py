"""tappi — Lightweight CDP browser control for Python.

Connect to any Chrome/Chromium with --remote-debugging-port and control it
programmatically. Reuses existing browser sessions (cookies, logins, extensions).

Quick start:
    from tappi import Browser

    b = Browser()                    # Connect to CDP (default: localhost:9222)
    b.open("https://example.com")    # Navigate
    elements = b.elements()          # List interactive elements
    b.click(3)                       # Click element [3]
    b.type(5, "hello")              # Type into element [5]
    print(b.text())                  # Read page text
"""

from tappi.core import Browser, CDPSession

__version__ = "0.7.2"
__all__ = ["Browser", "CDPSession"]
