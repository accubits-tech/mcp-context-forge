# -*- coding: utf-8 -*-
"""Fixture: tool body evaluates user-supplied input.

Used by the security-scan gate tests to verify the MCP-rules Semgrep ruleset
catches eval-on-tool-input as a tool-poisoning pattern.
"""

# Standard
import os

# Third-Party
from fastapi import FastAPI

app = FastAPI(title="with-eval")


class _FakeMcp:
    @staticmethod
    def tool(*_args, **_kw):
        def _inner(fn):
            return fn

        return _inner


mcp = _FakeMcp()


@mcp.tool()
def calculate(expression: str) -> int:
    """Naive calculator that evaluates the user-supplied expression directly."""
    # WARNING - the security gate should flag this line via mcp-tool-eval-on-input.
    return eval(expression)  # noqa: S307


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def main() -> None:
    # Third-Party
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
