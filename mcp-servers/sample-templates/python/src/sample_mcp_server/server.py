"""Sample FastMCP server with a single ping tool.

Replace the tool below with your own. Keep `main()` callable so the deploy form's
entry command (`python -m sample_mcp_server`) keeps working.
"""

import logging
import sys

from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger(__name__)

mcp = FastMCP(name="sample-mcp-server", version="0.1.0")


@mcp.tool()
def ping() -> str:
    """Reply with 'pong'. Replace this tool with your own."""
    return "pong"


def main() -> None:
    """Run the server over stdio. The deploy entry command points here."""
    mcp.run()
