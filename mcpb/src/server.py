#!/usr/bin/env python3
"""tappi MCP server entry point for MCPB bundle.

Imports from the bundled tappi source (no PyPI dependency).
"""

import os
import sys

# Add the src directory to Python path so bundled tappi is importable
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# Read CDP_URL from user config
cdp_url = os.environ.get("CDP_URL", "http://127.0.0.1:9222")
if not os.environ.get("CDP_URL"):
    config_cdp = os.environ.get("cdp_url", "")
    if config_cdp:
        os.environ["CDP_URL"] = config_cdp

from tappi.mcp_server import run_stdio
run_stdio()
