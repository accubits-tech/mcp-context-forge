# Sample MCP Server (Node.js)

A minimal, ready-to-deploy example MCP server built on the official MCP TypeScript/JavaScript SDK. Use this as a starting point: zip the directory, upload it through the MCP Foundry **Add Gateway → Deploy** flow, and you'll have a working `ping` tool you can replace with your own.

## What's inside

```
.
├── README.md          # this file
├── package.json       # name, deps, "type": "module"
├── .env.example       # template for env vars (do not commit real secrets)
└── src/
    └── server.js      # MCP server: one tool, `ping`
```

## Mandatory file (the gateway's container build requires it)

The deploy build runs:

```
if [ -f package-lock.json ]; then npm ci --ignore-scripts --omit=dev
elif [ -f yarn.lock ];        then yarn install --frozen-lockfile --ignore-scripts --production
elif [ -f package.json ];     then npm install --ignore-scripts --omit=dev
else                                echo "no package.json found" && exit 1
```

This sample ships a `package.json` only. **Strongly recommended:** run `npm install` locally and commit the resulting `package-lock.json` before deploying — it gives you a reproducible build.

## Files you must NOT include

- `Dockerfile` / `Containerfile` — the gateway renders its own hardened container build file. Any user-supplied one is renamed to `*.user` and ignored.
- Anything outside the archive root (no symlinks pointing up, no absolute paths).
- Archives over 50 MiB (gateway default).
- `node_modules/` — the gateway builds it inside the container; shipping it bloats the archive and may exceed the size cap.

## Deploy this sample

1. (optional but recommended) `npm install` then commit the lockfile.
2. Zip the directory: `zip -r ../sample-node.zip . -x 'node_modules/*'`.
3. In MCP Foundry, open **Gateways → Add Gateway → Transport: DEPLOY**.
4. Source: `Upload archive`, attach `sample-node.zip`.
5. **Runtime:** `Node.js`. **Entry mode:** `stdio`. **Entry command:** `node src/server.js`.
6. Submit. The gateway will build the container, scan it, and register the new MCP server.

You can also point the form at a Git URL — the structure expectations are identical.

## Replace the example tool

Open `src/server.js`. The single tool is wired through the SDK's request handlers:

```js
server.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name === "ping") {
    return { content: [{ type: "text", text: "pong" }] };
  }
  throw new Error(`unknown tool: ${req.params.name}`);
});
```

Add cases for your own tools and update the `ListToolsRequestSchema` handler to advertise them.

## Local development (optional)

```bash
npm install
node src/server.js     # starts stdio server (it waits for MCP framing on stdin)
```
