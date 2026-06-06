"""Standalone check: start mcp_server.py in a subprocess and call it via MCP client.

Run from the project root:
    python tests/scripts/check_mcp_call.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SERVER_SCRIPT = PROJECT_ROOT / "mcp_server.py"


def main() -> None:
    print(f"Starting MCP server: {SERVER_SCRIPT}")
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    time.sleep(2)

    if proc.poll() is not None:
        _, err = proc.communicate()
        print(f"Server exited early:\n{err.decode()}")
        sys.exit(1)

    print("MCP server is running (pid={})".format(proc.pid))
    print()
    print("To test manually, configure Claude Desktop with:")
    print(json.dumps({
        "mcpServers": {
            "squadone-scraper": {
                "command": sys.executable,
                "args": [str(SERVER_SCRIPT)],
                "cwd": str(PROJECT_ROOT),
            }
        }
    }, indent=2))
    print()
    print("Then call the 'scrape' tool with url and objective parameters.")
    print()
    print("Stopping server...")
    proc.terminate()
    proc.wait(timeout=5)
    print("Done.")


if __name__ == "__main__":
    main()
