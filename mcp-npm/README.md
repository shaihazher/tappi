# tappi-mcp

npm wrapper for the [tappi](https://github.com/shaihazher/tappi) MCP server — browser control via Chrome DevTools Protocol for AI agents.

## Quick Start

```bash
npx tappi-mcp
```

Or install globally:

```bash
npm install -g tappi-mcp
tappi-mcp
```

## Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tappi": {
      "command": "npx",
      "args": ["tappi-mcp"],
      "env": {
        "CDP_URL": "http://127.0.0.1:9222"
      }
    }
  }
}
```

## Requirements

- Python 3.10+ with either:
  - `pip install tappi[mcp]` (recommended)
  - `uvx` available (auto-installs tappi on the fly)

## What is tappi?

tappi connects to your **existing Chrome browser** via CDP. All your sessions, cookies, and extensions carry over. It pierces shadow DOM, uses 3-10x fewer tokens than accessibility tree tools, and works with every modern web app.

- 📦 **PyPI:** [tappi](https://pypi.org/project/tappi/)
- 🐙 **GitHub:** [shaihazher/tappi](https://github.com/shaihazher/tappi)
