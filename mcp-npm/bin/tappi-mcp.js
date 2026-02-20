#!/usr/bin/env node

/**
 * tappi-mcp — npm wrapper for the tappi MCP server.
 *
 * This is a thin shim that launches the Python tappi MCP server.
 * The actual MCP server lives in the tappi Python package.
 *
 * Install:  npx tappi-mcp
 * Or:       npm install -g tappi-mcp && tappi-mcp
 *
 * Requires Python 3.10+ and either:
 *   - tappi[mcp] installed: pip install tappi[mcp]
 *   - uvx available (auto-installs tappi on the fly)
 */

const { spawn, execFileSync } = require("child_process");
const { argv, env, exit, stderr } = process;

// Pass through all args after the binary name
const userArgs = argv.slice(2);

// Forward CDP_URL and other env vars
const childEnv = { ...env };

/**
 * Try running a command, return true if it exits 0.
 */
function hasCommand(cmd, args = ["--version"]) {
  try {
    execFileSync(cmd, args, { stdio: "ignore", timeout: 5000 });
    return true;
  } catch {
    return false;
  }
}

/**
 * Spawn the MCP server and pipe stdio through.
 */
function runServer(cmd, args) {
  const child = spawn(cmd, args, {
    stdio: ["inherit", "inherit", "inherit"],
    env: childEnv,
  });

  child.on("error", (err) => {
    stderr.write(`tappi-mcp: failed to start: ${err.message}\n`);
    exit(1);
  });

  child.on("exit", (code) => {
    exit(code ?? 0);
  });

  // Forward signals
  for (const sig of ["SIGINT", "SIGTERM"]) {
    process.on(sig, () => child.kill(sig));
  }
}

// Strategy 1: tappi CLI is installed
if (hasCommand("tappi", ["--version"])) {
  runServer("tappi", ["mcp", ...userArgs]);
}
// Strategy 2: uvx is available (auto-installs tappi)
else if (hasCommand("uvx", ["--version"])) {
  runServer("uvx", ["tappi[mcp]", "mcp", ...userArgs]);
}
// Strategy 3: python with tappi installed
else if (hasCommand("python3", ["-c", "import tappi"])) {
  runServer("python3", ["-m", "tappi.mcp_server", ...userArgs]);
}
// Nothing works
else {
  stderr.write(
    `tappi-mcp: Could not find tappi or uvx.

Install tappi:
  pip install tappi[mcp]

Or install uv (which provides uvx):
  curl -LsSf https://astral.sh/uv/install.sh | sh

Then run tappi-mcp again.
`
  );
  exit(1);
}
