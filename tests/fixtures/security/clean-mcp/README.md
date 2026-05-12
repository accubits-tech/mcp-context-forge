# clean-mcp fixture

Used by the security-scan gate integration tests as the "passes everything" baseline.
- No hardcoded secrets.
- No vulnerable deps (pinned recent versions).
- No eval/exec on tool args.
- No outbound exfil patterns.

Build for testing:

```
cd tests/fixtures/security/clean-mcp
tar czf /tmp/clean-mcp.tgz .
```
