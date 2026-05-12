# -*- coding: utf-8 -*-
"""Minimal MCP-style HTTP server fixture used by the vulnerability-scan integration tests.

Should pass every scanner in the gate.
"""

# Standard
import os

# Third-Party
from fastapi import FastAPI

app = FastAPI(title="clean-mcp")


@app.get("/health")
def health() -> dict:
    """Liveness probe used by the deploy pipeline."""
    return {"status": "ok"}


@app.get("/mcp")
def mcp_endpoint() -> dict:
    """A trivial MCP-shaped tools/list response."""
    return {
        "tools": [
            {"name": "echo", "description": "Echo back the input string", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}
        ]
    }


def main() -> None:
    """Run the server when invoked by the container entrypoint."""
    # Third-Party
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
