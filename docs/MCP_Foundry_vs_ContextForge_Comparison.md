# MCP Foundry vs IBM ContextForge -- Feature Comparison Matrix

**Version:** 1.2 | **Date:** April 2026 | **Classification:** Internal Assessment

This document compares MCP Foundry and IBM ContextForge across the capabilities that define a complete MCP orchestration platform. The comparison is organized around functional areas from automated tool creation through agent-centric observability.

**Legend:** Full = production-ready | Partial = limited or experimental | None = not available

---

## 1. Core MCP Capabilities

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| Support for 3rd party MCPs (connect to remote, externally hosted MCP servers) | Full | Full |
| Support for non-hosted, self-hostable MCPs (auto-bridge stdio MCP servers to HTTP/SSE) | Full | None |
| Automated MCP creation (AI-powered generation of MCP tools from API documentation) | Full | None |
| Self-hosted new MCPs (create, host, and manage new MCP tools internally on the platform) | Full | None |

---

## 2. Automated MCP Creation & Tool Generation

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| Firecrawl-powered API documentation crawling | Full | None |
| AI-powered tool enhancement (LLM-based description generation, schema mapping) | Full | None |
| AI-powered evaluation and quality assurance (validation, deduplication, correction) | Full | None |
| Confidence scoring for generated tools | Full | None |
| Multi-format ingestion (OpenAPI, Postman, PDF, HTML, Markdown) | Full | None |
| Background job management with concurrency control | Full | None |
| Dry-run preview mode (preview imports without committing) | Full | None |
| Bulk import with conflict resolution (skip, update, rename, fail) | Full | None |

---

## 3. Skills Management & Agent Enablement

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| Predefined Skills Registry (curated, ready-to-use skill catalog for agents) | Full | None |
| One-click skill deployment from registry | Full | None |
| Snapshot-based skill versioning (immutable, reproducible configurations) | Full | None |
| Team-scoped skill visibility and access control | Full | None |
| Skill lifecycle management (activate, deactivate, retire, version) | Full | None |

---

## 4. MCP Orchestration & Routing

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| Bifrost unified routing plane (internal, external, and stdio MCPs) | Full | None |
| Virtual server composition (tools + resources + prompts + A2A agents) | Full | Partial |
| Multi-source MCP connectivity (internal, external, auto-created stdio) | Full | Partial |
| Routing-level policy enforcement (team visibility, tool activation, federation trust) | Full | None |
| Protocol normalization across transports (HTTP, SSE, stdio, Streamable HTTP) | Full | Partial |

---

## 5. Security & Governance

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| JWT authentication (HS256, RS256, Ed25519) | Full | Full |
| OAuth 2.0 + SSO (GitHub, Google, Okta, Entra ID, Keycloak) | Full | Full |
| RBAC with three scope levels (global, team, personal) | Full | Partial |
| Token scoping (IP-based CIDR, time-window, server-scope restrictions) | Full | None |
| Multi-tenant team management with data isolation | Full | Full |
| Permission audit logging (compliance trail) | Full | Partial |
| Plugin-based policy enforcement hooks (pre/post invocation filtering) | Full | Full |
| Startup security validation (enforce strong secrets in production) | Full | None |

---

## 6. MCP Scaling & Operations

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| Horizontal scaling of self-hosted MCPs across multiple worker instances | Full | None |
| Redis-backed distributed session management for MCP server state | Full | None |
| Background MCP generation jobs with concurrency control and auto-recovery | Full | None |
| Per-MCP health monitoring with configurable thresholds and auto-exclusion | Full | Partial |
| MCP-level rate limiting and concurrency controls per tool | Full | Partial |
| Nginx caching layer for MCP response caching and load reduction | Full | None |
| Kubernetes-native MCP deployment with health and readiness probes | Full | Partial |
| PostgreSQL-backed persistent state for MCP lifecycle, metrics, and audit | Full | Partial |

---

## 7. AI Agent Observability

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| LogFire -- dedicated AI agent activity logging | Full | None |
| Per-agent interaction trails and auditability | Full | None |
| OpenTelemetry distributed tracing (OTLP, Jaeger, Zipkin) | Full | Full |

---

## 8. Admin Experience

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| React Web Dashboard | Full | None |
| Real-time log viewer with search and filtering | Full | Partial |
| Tool, server, and agent management UI | Full | Partial |
| Configuration export and import | Full | Full |
| Skills Registry browsing and one-click deployment UI | Full | None |

---

## 9. MCP Gateway Fundamentals

| Capability | MCP Foundry | IBM ContextForge |
|---|---|---|
| MCP protocol support (JSON-RPC, HTTP, SSE, WebSocket, Streamable HTTP) | Full | Full |
| Federation -- multi-gateway mesh with DNS-SD auto-discovery | Full | Full |
| Tool registry with JSON Schema validation and rate limiting | Full | Full |
| External MCP server connectivity (ContextForge for remote MCP integrations) | Full | Full |
| Plugin extension system (pre/post hooks, native and external plugins) | Full | Full |

---

*End of Document*
