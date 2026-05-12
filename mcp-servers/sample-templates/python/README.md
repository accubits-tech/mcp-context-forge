# Sample MCP Server (Python)

A minimal, ready-to-deploy example MCP server. Use this as a starting point for your own server: zip the directory, upload it through the MCP Foundry **Add Gateway → Deploy** flow, and you'll get a working `ping` tool you can replace with your own.

## What's inside

```
.
├── README.md                   # this file
├── pyproject.toml              # project metadata + entry point
├── requirements.txt            # pinned runtime deps
├── .env.example                # template for env vars (do not commit real secrets)
├── src/
│   └── sample_mcp_server/
│       ├── __init__.py
│       └── server.py           # FastMCP server: one tool, `ping`
└── tests/
    └── test_server.py          # pytest example
```

## Mandatory files (the gateway's container build requires one of these)

The deploy build runs:

```
if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
if [ -f pyproject.toml ] || [ -f setup.py ]; then pip install .; fi
# else: build fails with "no pyproject.toml / setup.py / requirements.txt found"
```

This sample ships **both** `pyproject.toml` and `requirements.txt` — keep at least one.

## Files you must NOT include

- `Dockerfile` / `Containerfile` — the gateway renders its own hardened container build file. Any user-supplied one is renamed to `*.user` and ignored. Submitting one is harmless but unnecessary.
- Anything outside the archive root (no symlinks pointing up, no absolute paths).
- Archives over 50 MiB (gateway default).

## Deploy this sample

1. Zip this directory: `cd sample-templates/python && zip -r ../sample-python.zip .`
2. In MCP Foundry, open **Gateways → Add Gateway → Transport: DEPLOY**.
3. Source: `Upload archive`, attach `sample-python.zip`.
4. **Runtime:** `Python`. **Entry mode:** `stdio`. **Entry command:** `python -m sample_mcp_server`.
5. Submit. The gateway will build the container, scan it, and register the new MCP server.

You can also point the form at a Git URL for the same code — the structure expectations are identical.

## Replace the example tool

Open `src/sample_mcp_server/server.py`. The single tool is:

```python
@mcp.tool()
def ping() -> str:
    """Reply with 'pong'."""
    return "pong"
```

Add your own `@mcp.tool()` functions, push, redeploy.

## Local development (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m sample_mcp_server          # starts stdio server
pytest                                # runs the test
```
