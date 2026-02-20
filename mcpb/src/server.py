#!/usr/bin/env python3
"""tappi MCP server entry point for MCPB bundle.

This is the thin entry point that Claude Desktop launches.
It reads the user-configured CDP_URL and starts the tappi MCP server.
"""

import os
import sys


def main():
    # Read CDP_URL from user config (set by Claude Desktop from manifest user_config)
    cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:9222")

    # Also check the mcpb user_config pattern
    if not os.environ.get("CDP_URL"):
        config_cdp = os.environ.get("cdp_url", "")
        if config_cdp:
            os.environ["CDP_URL"] = config_cdp

    # Import and run the tappi MCP server
    from tappi.mcp_server import run_stdio
    run_stdio()


if __name__ == "__main__":
    main()
