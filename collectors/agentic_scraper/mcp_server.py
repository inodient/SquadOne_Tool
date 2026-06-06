"""MCP server: exposes the ReAct scraping agent as a single 'scrape' tool.

Start the server:
    python mcp_server.py

Configure in Claude Desktop (~/.claude/claude_desktop_config.json):
    {
      "mcpServers": {
        "squadone-scraper": {
          "command": "/path/to/.venv/bin/python",
          "args": ["/path/to/SquadOne_Agentic_Scrapper/mcp_server.py"],
          "cwd": "/path/to/SquadOne_Agentic_Scrapper"
        }
      }
    }
"""

from __future__ import annotations

import json
import logging

from mcp.server.fastmcp import FastMCP

from core.config import get_config
from react_agent.runner import ReactAgent

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

mcp = FastMCP("squadone-scraper")


@mcp.tool()
def scrape(url: str, objective: str) -> str:
    """Scrape a web page and extract structured data based on the objective.

    Uses a Playwright-powered ReAct agent that navigates, interacts with,
    and extracts data from the page autonomously.

    Args:
        url: Full URL of the page to scrape (must start with http:// or https://).
        objective: Natural language description of what to extract,
                   e.g. "Get all product names and prices"
                        "Extract the top 10 news headline titles and URLs".

    Returns:
        JSON string with keys: run_id, success, step_count, result.
        'result' contains the extracted data in a structure decided by the agent.
    """
    config = get_config()
    agent = ReactAgent(config)
    output = agent.run({"url": url, "objective": objective})
    return json.dumps(output, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
