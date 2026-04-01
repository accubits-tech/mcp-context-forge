# MCP Foundry -- Enterprise Platform Architecture

**Version:** 1.0
**Date:** March 2026
**Classification:** Client-Facing -- Detailed Architecture Reference

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Platform Overview](#2-platform-overview)
3. [Component Architecture](#3-component-architecture)
   - 3.1 [AI Agents](#31-ai-agents)
   - 3.2 [React Web Dashboard](#32-react-web-dashboard)
   - 3.3 [HTTP / SSE Transport Layer](#33-http--sse-transport-layer)
   - 3.4 [API Gateway](#34-api-gateway)
   - 3.5 [Middleware Stack](#35-middleware-stack)
   - 3.6 [Backend Services](#36-backend-services)
   - 3.7 [MCP Tools Generator](#37-mcp-tools-generator)
   - 3.8 [Skills Hub](#38-skills-hub)
   - 3.9 [Bifrost Internal Router](#39-bifrost-internal-router)
   - 3.10 [Data Layer](#310-data-layer)
   - 3.11 [MCP Registry](#311-mcp-registry)
   - 3.12 [Observability](#312-observability)
4. [Layered Architecture](#4-layered-architecture)
5. [End-to-End Request Flows](#5-end-to-end-request-flows)
   - 5.1 [AI Agent Calling a Tool](#51-flow-1-ai-agent-calling-a-tool)
   - 5.2 [Dashboard User Managing MCP Tools or Skills](#52-flow-2-dashboard-user-managing-mcp-tools-or-skills)
   - 5.3 [Request Routing Through Bifrost to Internal or External MCPs](#53-flow-3-request-routing-through-bifrost-to-internal-or-external-mcps)
   - 5.4 [Auto-Generated MCP Creation Flow](#54-flow-4-auto-generated-mcp-creation-flow)
   - 5.5 [Skills Discovery and Execution](#55-flow-5-skills-discovery-and-execution)
6. [Deep Dive: Special Components](#6-deep-dive-special-components)
   - 6.1 [Bifrost Internal Router](#61-bifrost-internal-router)
   - 6.2 [MCP Tools Generator](#62-mcp-tools-generator)
   - 6.3 [Skills Hub](#63-skills-hub)
   - 6.4 [Federation Layer](#64-federation-layer)
   - 6.5 [A2A Service](#65-a2a-service)
   - 6.6 [MCP Server Service](#66-mcp-server-service)
7. [Security and Governance](#7-security-and-governance)
8. [Scalability and Reliability](#8-scalability-and-reliability)
9. [Observability and Operations](#9-observability-and-operations)
10. [Architecture Strengths](#10-architecture-strengths)
11. [Assumptions and Reasonable Inferences](#11-assumptions-and-reasonable-inferences)
12. [Glossary](#12-glossary)

---

## 1. Executive Summary

MCP Foundry is an enterprise-grade platform for orchestrating, governing, and scaling Model Context Protocol (MCP) tool ecosystems. It provides a unified gateway through which AI agents, human operators, and automated systems can discover, invoke, and manage MCP-compliant tools, resources, and prompts across organizational boundaries.

The platform addresses a fundamental challenge facing enterprises that are adopting agentic AI: as the number of MCP servers, tools, and AI agents grows, organizations need a centralized control plane that provides consistent authentication, authorization, observability, and lifecycle management without constraining the diversity of underlying tool backends.

MCP Foundry delivers this through a layered architecture that separates concerns across experience, transport, gateway, service, routing, and data layers. It supports multiple MCP connectivity modes -- externally hosted servers, internally managed servers, and auto-generated stdio-based servers -- all unified behind a single API surface. The platform includes an AI-powered tool generation pipeline that can convert API documentation and OpenAPI specifications into production-ready MCP tools, reducing the integration effort from days to minutes.

For enterprises adopting MCP-based AI tool ecosystems, MCP Foundry provides:

- **Unified governance** over all MCP tools, resources, and prompts with role-based access control, team isolation, and audit logging.
- **Federated connectivity** to multiple MCP backends through a single gateway, eliminating the need for agents to manage individual server connections.
- **Automated tool generation** from existing API documentation using AI-powered ingestion, crawling, enhancement, and evaluation pipelines.
- **Operational visibility** through distributed tracing, structured logging, and Prometheus-compatible metrics across every tool invocation.
- **Multi-tenant team management** that enables different business units to independently manage their MCP tools while sharing a common platform.

---

## 2. Platform Overview

MCP Foundry operates as an enterprise MCP orchestration, tooling, and governance system. It sits between the consumers of MCP capabilities (AI agents, dashboards, programmatic clients) and the providers of those capabilities (MCP servers, REST APIs, agent endpoints), providing a managed intermediary layer that adds security, observability, lifecycle management, and federation.

### Core Capabilities

**Tool and Skill Management.** The platform maintains a centralized registry of MCP tools, resources, and prompts. Each registered capability goes through a managed lifecycle -- registration, validation, activation, invocation, metrics collection, and decommissioning. The Skills Hub enables the creation and management of composable, reusable skills -- self-contained units of capability that AI agents can discover and invoke through MCP. The Skills Registry provides a catalog of predefined skills that agents can use out of the box.

**Multi-Source MCP Connectivity.** MCP Foundry connects to three categories of MCP servers simultaneously: externally hosted MCPs accessed over the network, internally managed MCPs running within the platform's infrastructure, and stdio-based MCPs that are automatically bridged to HTTP/SSE transports. The Bifrost internal router abstracts these differences, presenting a uniform interface to consumers.

**AI-Powered Tool Generation.** The MCP Tools Generator pipeline ingests API documentation from various sources (URLs, OpenAPI specs, uploaded documents), crawls and parses the content, enhances tool definitions using AI agents, evaluates quality and correctness, and registers the resulting tools -- all as a managed background process.

**Federation.** The Federation Layer enables multiple MCP Foundry instances (or compatible MCP gateways) to discover each other, exchange capability catalogs, and forward requests transparently. Federated tools appear as local tools to consumers, with the routing handled internally by Bifrost.

**Agent-to-Agent Communication.** The A2A Service provides a protocol-compliant mechanism for AI agents to communicate with each other through the platform, enabling multi-agent orchestration patterns while maintaining governance controls.

### Design Principles

| Principle | Description |
|---|---|
| Protocol-native | Built from the ground up around the MCP specification, supporting JSON-RPC messaging, SSE streaming, and Streamable HTTP transports. |
| Zero-trust security | Every request is authenticated and authorized. No implicit trust between layers. Token scoping, IP restrictions, and time-based access controls are enforced at the middleware level. |
| Transport-agnostic | Backend MCP servers can communicate via HTTP, SSE, WebSocket, or stdio. The platform normalizes these into a consistent API surface. |
| Vendor-agnostic observability | Supports OpenTelemetry (OTLP, Jaeger, Zipkin), Prometheus, and structured JSON logging without coupling to any specific observability vendor. |
| Plugin-extensible | Every major lifecycle event (authentication, tool invocation, resource fetch, prompt rendering) exposes pre- and post-execution hooks that plugins can intercept, modify, or block. |

---

## 3. Component Architecture

This section provides a detailed explanation of every component and sub-component visible in the architecture diagram.

### 3.1 AI Agents

**Purpose.** AI Agents represent the primary programmatic consumers of the platform. These are autonomous or semi-autonomous AI systems (large language model agents, orchestration frameworks, custom agentic applications) that need to discover and invoke MCP tools at runtime.

**Responsibilities.**
- Establish authenticated sessions with the platform via JWT, OAuth, or SSO tokens.
- Discover available tools, resources, and prompts through MCP protocol methods (`tools/list`, `resources/list`, `prompts/list`).
- Invoke tools via `tools/call` and consume results, including streamed responses over SSE.
- Participate in multi-agent coordination through the A2A Service.

**Interactions.** Agents connect to the platform through the HTTP/SSE transport layer. All agent traffic passes through the API Gateway and the full Middleware Stack before reaching backend services. Agents have no direct access to the Data Layer or individual MCP servers -- all interactions are mediated by the platform.

**Design Considerations.** The platform is client-agnostic: any MCP-compliant client can connect. This includes commercial AI assistants, open-source agent frameworks (LangChain, CrewAI, AutoGen, LangGraph), and custom-built agents. The transport layer supports both stateful (SSE with session management) and stateless (Streamable HTTP) connection modes to accommodate different agent architectures.

### 3.2 React Web Dashboard

**Purpose.** The React Web Dashboard is the human-facing administrative interface for the platform. It provides a graphical environment for managing all platform entities, monitoring system health, and performing operational tasks.

**Responsibilities.**
- Provide CRUD operations for tools, resources, prompts, servers, and gateways.
- Display real-time metrics and observability data (traces, logs, system stats).
- Enable team management operations: create teams, invite members, assign roles, manage permissions.
- Trigger MCP tool generation workflows from API documentation URLs.
- Browse and deploy entries from the Skills Registry catalog.
- Configure platform settings including authentication, federation, and plugin management.

**Interactions.** The Dashboard communicates exclusively with the API Gateway over HTTP. It is served as a standalone React application and does not have direct access to the database or backend services. All data retrieval and mutations flow through the same API Gateway and Middleware Stack that agents use, ensuring consistent authorization enforcement.

**Security Implications.** Dashboard sessions are authenticated via JWT tokens with configurable expiration. Role-based access control restricts which operations a user can perform based on their team membership and assigned roles.

### 3.3 HTTP / SSE Transport Layer

**Purpose.** The transport layer provides the protocol interface between external consumers (AI Agents, Dashboard) and the API Gateway. It supports two primary transport mechanisms as defined by the MCP specification.

**Responsibilities.**
- **HTTP (Request/Response):** Handle standard REST API calls for administrative operations, tool management, and synchronous tool invocations.
- **SSE (Server-Sent Events):** Provide real-time, server-push streaming for long-running tool invocations, event notifications, and MCP protocol sessions.
- **Streamable HTTP:** Support the MCP Streamable HTTP transport (specification version 2025-06-18), which enables bidirectional communication over standard HTTP with optional SSE streaming for responses.
- **WebSocket:** Provide full-duplex bidirectional communication for use cases requiring low-latency, interactive sessions.

**Inputs and Outputs.** Inbound: HTTP requests (JSON-RPC payloads for MCP operations, REST for admin APIs). Outbound: JSON responses, SSE event streams, or WebSocket frames.

**Design Considerations.** The transport layer uses an abstract `Transport` base class, allowing new transport protocols to be added without modifying upstream or downstream components. Session management includes auto-generated UUIDs, keepalive support, and configurable TTLs. The Streamable HTTP transport supports event resumability through an in-memory event store, enabling clients to recover from transient network failures.

### 3.4 API Gateway

**Purpose.** The API Gateway is the single entry point for all traffic into the platform. Built on a high-performance ASGI framework with ORJSON serialization, it routes requests to the appropriate backend services and enforces cross-cutting concerns through the Middleware Stack.

**Responsibilities.**
- Accept and validate inbound HTTP/SSE/WebSocket connections.
- Route requests to the correct service endpoint based on URL path and MCP method.
- Apply the Middleware Stack in the correct order for every request.
- Serve OpenAPI documentation (Swagger UI) for the REST API surface.
- Expose health check (`/health`), readiness (`/ready`), and security status (`/health/security`) endpoints for infrastructure orchestrators.
- Manage application lifecycle: ordered service initialization at startup, graceful shutdown with resource cleanup.

**Interactions.** The API Gateway sits between the Transport Layer and the Middleware Stack. It receives all inbound traffic and delegates to 14 dedicated API routers organized by domain: Protocol, Tools, Tool Generation Jobs, Resources, Prompts, Gateways, Roots, Utilities, Servers, Metrics, Tags, Export/Import, A2A Agents, and Registry.

**Operational Considerations.** The gateway validates security configuration at startup: it checks JWT secret strength, verifies authentication requirements for production environments, and validates federation security settings. A `REQUIRE_STRONG_SECRETS` flag can enforce strict security posture in production deployments.

### 3.5 Middleware Stack

**Purpose.** The Middleware Stack is an ordered pipeline of cross-cutting concerns that every request passes through before reaching a backend service. It enforces security, logging, compression, and protocol validation uniformly across all API endpoints.

The diagram highlights six primary middleware components. The full execution pipeline is described below.

| Middleware | Responsibility | Key Behavior |
|---|---|---|
| **Auth** | Authentication enforcement | Extracts and validates JWT tokens from Authorization headers, cookies, or query parameters. Supports plugin-based custom authentication via hook extensibility. Checks token revocation status. Falls back through multiple authentication strategies (plugin auth, JWT, API token, basic auth). |
| **RBAC** | Role-based access control | Evaluates the authenticated user's roles and permissions against the required permission for the target endpoint. Supports global, team-scoped, and personal permission levels. Logs permission decisions to an audit trail. |
| **CORS** | Cross-origin resource sharing | Configures allowed origins, methods, headers, and credential sharing for browser-based clients. Prevents unauthorized cross-origin access to API endpoints. |
| **RequestLogger** | Request and response logging | Produces structured JSON logs for every request, including method, path, status code, and response time. Automatically masks sensitive data: JWT tokens, passwords, API keys, OAuth tokens, and other secrets are redacted before logging. |
| **SecurityHeaders** | HTTP security hardening | Injects security headers on every response: Content-Security-Policy (CSP), Strict-Transport-Security (HSTS), X-Frame-Options (clickjacking prevention), X-Content-Type-Options (MIME sniffing prevention), Referrer-Policy, and Permissions-Policy. |
| **Compression** | Response compression | Applies negotiated compression (Brotli, Zstandard, or GZip) to response payloads, reducing bandwidth consumption for large tool results and bulk API responses. |

**Additional Middleware.** Beyond the six primary components, the platform includes supplementary middleware for MCP protocol version validation (rejects requests with incompatible protocol versions), token scoping (enforces IP-based, time-based, and server-scoped access restrictions on tokens), MCP path rewriting (normalizes MCP endpoint paths), proxy header handling (trusts X-Forwarded-For/Proto from configured proxy IPs), and API documentation protection (restricts access to Swagger UI in production).

**Execution Order.** Middleware executes in a defined sequence: CORS and compression are applied first (outermost), followed by security headers, protocol validation, token scoping, authentication, request logging, and observability instrumentation (innermost). This ordering ensures that security checks occur before business logic, and that logging captures the full authenticated context of each request.

### 3.6 Backend Services

The Backend Services layer contains the core business logic of the platform. Each service is a self-contained module with its own error hierarchy, event notification system, and database interactions.

#### 3.6.1 Federation Layer

**Purpose.** Enables the platform to participate in a mesh of interconnected MCP gateways, allowing tools and capabilities to be discovered and invoked across organizational boundaries.

**Responsibilities.**
- Discover peer gateways through multiple mechanisms: DNS-SD/mDNS (Zeroconf) for local network discovery, static peer lists for deterministic topologies, and peer exchange for transitive discovery.
- Monitor peer health with configurable check intervals and unhealthy thresholds.
- Forward tool invocation requests to the gateway that owns the requested tool.
- Aggregate responses from multiple peer gateways into unified result sets.
- Synchronize tool, resource, and prompt catalogs across federated peers.

**Inputs.** Peer gateway URLs (manually configured or discovered), inbound tool/resource/prompt requests targeting federated capabilities.
**Outputs.** Federated capability catalogs, forwarded request responses, peer health status.

**Design Considerations.** Federation is transparent to consumers: a federated tool appears identical to a locally registered tool. The platform tracks which gateway owns each tool and routes invocations automatically through Bifrost. Federation supports OAuth-based authentication between peers for secure cross-boundary communication.

#### 3.6.2 Gateway Service

**Purpose.** Manages the lifecycle of gateway peers -- both local and federated -- including registration, health monitoring, capability ingestion, and decommissioning.

**Responsibilities.**
- Register and manage gateway peers with their connection parameters, authentication credentials, and TLS certificates.
- Periodically check gateway health and mark peers as healthy or unhealthy based on configurable thresholds.
- Ingest tools, resources, and prompts from remote gateways upon successful connection.
- Handle gateway-level OAuth token management and refresh flows.
- Support both SSE and Streamable HTTP client connections to remote gateways.

**Interactions.** The Gateway Service works closely with the Federation Layer for discovery and with Bifrost for routing. It maintains the `gateways` table in the database and publishes events when gateways are added, updated, or removed.

#### 3.6.3 Tool Service

**Purpose.** The central registry and invocation engine for MCP tools. Every tool call in the platform ultimately passes through the Tool Service.

**Responsibilities.**
- Register tools with JSON Schema-based input validation.
- Invoke tools with full schema validation, timeout enforcement, and error handling.
- Execute plugin chains: pre-invocation hooks (content filtering, PII detection, rate limiting) and post-invocation hooks (response transformation, caching, logging).
- Track per-tool metrics: invocation count, success/failure rates, average/min/max response times.
- Support JQ-based response filtering for extracting specific data from tool results.
- Manage tool activation/deactivation and soft-delete lifecycle states.
- Publish tool change events to subscribers (SSE event streams).

**Inputs.** Tool definitions (name, description, input schema, endpoint configuration, authentication), tool invocation requests (tool name, arguments).
**Outputs.** Tool registration confirmations, invocation results, metrics snapshots.

#### 3.6.4 Team Management

**Purpose.** Provides multi-tenant team isolation, enabling different business units or project teams to independently manage their MCP tool portfolios within a shared platform.

**Responsibilities.**
- Create and manage teams with configurable visibility (public, private).
- Manage team membership: invite users, assign roles (admin, member, viewer), track membership history.
- Scope tool, resource, prompt, and server visibility to specific teams.
- Enforce team-level permission boundaries on all CRUD operations.
- Maintain audit trails of team membership changes.

**Interactions.** Team Management integrates with the RBAC middleware to enforce team-scoped permissions. All core entities (tools, resources, prompts, servers) carry a `team_id` field that determines visibility and access.

#### 3.6.5 A2A Service

**Purpose.** Implements the Agent-to-Agent (A2A) communication protocol, enabling AI agents registered with the platform to discover and interact with each other through standardized interfaces.

**Responsibilities.**
- Register external AI agents with their endpoint URLs, capabilities, and authentication requirements.
- Enable agent discovery: agents can query the platform to find other agents with specific capabilities.
- Proxy inter-agent communication through the platform, ensuring that all agent-to-agent traffic is authenticated, logged, and metricated.
- Track per-agent metrics: interaction count, response times, failure rates.
- Support team-scoped agent visibility, preventing unauthorized agent discovery across team boundaries.

**Interactions.** A2A agents can be composed into virtual MCP servers alongside tools, resources, and prompts, enabling mixed human-tool-agent server configurations. The A2A Service publishes agent change events and integrates with the federation system for cross-gateway agent discovery.

#### 3.6.6 MCP Server Service

**Purpose.** Manages the lifecycle of virtual MCP servers -- logical groupings of tools, resources, and prompts that are exposed as cohesive MCP endpoints.

**Responsibilities.**
- Compose virtual servers by associating selected tools, resources, prompts, and A2A agents into a single named server.
- Manage server lifecycle: creation, activation, health checking, metrics collection, and decommissioning.
- Track server transport configuration (SSE, WebSocket, or stdio).
- Provide server-level metrics aggregation: total invocations, error rates, top-performing tools within a server.
- Publish servers to the Skills Registry for catalog browsing and one-click redeployment.

**Interactions.** The MCP Server Service uses many-to-many associations between servers and their constituent entities. A single tool can belong to multiple servers, and a server can contain tools from different origins (local, federated, auto-generated). The service integrates with the stdio bridge manager for auto-created stdio servers.

### 3.7 MCP Tools Generator

**Purpose.** The MCP Tools Generator is an AI-powered pipeline that converts existing API documentation into production-ready MCP tools. It reduces the integration effort for onboarding new APIs from manual schema authoring to an automated crawl-analyze-generate workflow.

The generator consists of four stages that operate in sequence, managed as background jobs with concurrency control.

#### 3.7.1 Ingestion Pipeline

**Purpose.** The entry point for new API integrations. Accepts input from multiple sources and normalizes it for downstream processing.

**Responsibilities.**
- Accept API documentation URLs, uploaded OpenAPI specifications (JSON/YAML), Postman collections, and raw documentation files (PDF, HTML, Markdown, plain text).
- Validate and normalize input data.
- Support bulk import with configurable conflict resolution strategies: skip existing, update existing, rename conflicting, or fail on conflict.
- Provide dry-run preview mode: show what would be imported without committing changes.
- Track import operations with granular status reporting (per-tool, per-resource, per-prompt success/failure).

**Inputs.** API documentation URLs, OpenAPI specs, Postman collections, document files.
**Outputs.** Normalized API definitions ready for crawling and enhancement.

#### 3.7.2 Crawler Engine

**Purpose.** Traverses API documentation websites to extract structured information about available endpoints, parameters, authentication methods, and response formats. The Crawler Engine is powered by Firecrawl, a purpose-built web crawling and scraping engine optimized for extracting clean, structured content from documentation sites.

**Responsibilities.**
- Leverage Firecrawl to perform intelligent, multi-page crawling of documentation sites with automatic content extraction and structuring.
- Enforce SSRF (Server-Side Request Forgery) protections: validate every URL before crawling to prevent access to internal network resources.
- Respect `robots.txt` directives and implement politeness controls (rate limiting between requests).
- Auto-detect OpenAPI specification files linked within documentation pages.
- Classify authentication-related pages for separate processing by the auth extraction module.
- Infer base URLs from crawled content for endpoint construction.
- Handle JavaScript-rendered pages and complex documentation sites through Firecrawl's advanced rendering capabilities.

**Inputs.** Documentation URLs, crawl depth limits, domain restrictions.
**Outputs.** Extracted page content, discovered OpenAPI specs, classified page types, inferred base URLs.

#### 3.7.3 Tool Enhancer Agent

**Purpose.** An AI-powered analysis engine that transforms raw API information into well-structured, richly described MCP tool definitions.

**Responsibilities.**
- Analyze OpenAPI specifications to extract endpoint semantics, parameter relationships, and authentication requirements.
- Generate human-readable tool descriptions that accurately convey each endpoint's purpose and usage patterns.
- Map API parameters to MCP tool input schemas with appropriate JSON Schema types, constraints, and descriptions.
- Assess security implications of each endpoint (read-only vs. mutating, authentication requirements, data sensitivity).
- Parse unstructured API documentation (PDF, HTML, Markdown) to extract endpoint definitions when no OpenAPI spec is available.

**Inputs.** Crawled API content, OpenAPI specifications, raw documentation text.
**Outputs.** Draft MCP tool definitions with input schemas, descriptions, and security annotations.

#### 3.7.4 Evaluation Agent

**Purpose.** A quality assurance stage that validates, deduplicates, corrects, and enriches the tool definitions produced by the Tool Enhancer Agent before they are registered in the platform.

**Responsibilities.**
- Validate endpoint definitions for correctness: verify URLs, check parameter consistency, confirm schema compliance.
- Deduplicate tools: identify semantically equivalent endpoints and merge them.
- Correct common issues: fix malformed schemas, normalize naming conventions, resolve parameter type mismatches.
- Enrich tool metadata: add tags, categorization, usage examples, and confidence scores.
- Assign confidence scores to each generated tool, enabling operators to prioritize manual review for low-confidence results.

**Inputs.** Draft tool definitions from the Tool Enhancer Agent.
**Outputs.** Validated, deduplicated, enriched MCP tool definitions ready for registration.

**Background Job Management.** The entire generation pipeline runs as a managed background job with semaphore-controlled concurrency. Job states progress through `pending`, `running`, `completed`, `failed`, or `cancelled`, with automatic cleanup of stale jobs based on configurable TTL. Operators can monitor job progress, retrieve results, or cancel in-progress jobs through the API or Dashboard.

### 3.8 Skills Hub

**Purpose.** The Skills Hub is the platform's system for creating, managing, and exposing composable skills to AI agents through MCP. Conceptually similar to how Claude exposes skills to users, the Skills Hub allows organizations to define self-contained units of capability -- each combining tools, resources, and prompts into a coherent skill -- and make them discoverable and invocable by any MCP-compliant AI agent. The Skills Hub provides two complementary functions: skill authoring and lifecycle management through the Skills Manager, and a predefined skill catalog through the Skills Registry.

#### 3.8.1 Skills Manager

**Purpose.** Provides the authoring and lifecycle management layer for creating, configuring, and exposing skills to AI agents via MCP.

**Responsibilities.**
- Enable skill creation by composing tools, resources, and prompts into cohesive, self-contained skill units that agents can discover and invoke through standard MCP protocol methods.
- Provide CRUD operations for skills and their constituent capabilities with JSON Schema validation.
- Enforce naming uniqueness and version tracking across all registered skills.
- Manage activation and deactivation states, allowing skills to be temporarily disabled without deletion.
- Execute plugin hook chains at every lifecycle stage: pre-registration validation, post-registration notification, pre-invocation filtering (PII detection, content moderation, rate limiting), and post-invocation transformation (response caching, JSON repair, schema validation).
- Publish change events to subscriber queues, enabling real-time notification of skill changes to connected agents.
- Render prompt templates using a sandboxed Jinja2 environment with HTML/XML auto-escaping for XSS prevention.

**Inputs.** Skill definitions (tool schemas, resource URIs, prompt templates), invocation requests from AI agents.
**Outputs.** Registered skills exposed via MCP, invocation results, change events.

#### 3.8.2 Skills Registry

**Purpose.** The Skills Registry is a catalog of predefined, ready-to-use skills that AI agents can leverage immediately. These are curated, validated skill configurations that provide agents with common capabilities without requiring manual setup.

**Responsibilities.**
- Maintain a catalog of predefined skills that agents can discover and use out of the box, covering common enterprise use cases (data retrieval, document processing, API integration, workflow automation).
- Publish validated skill configurations as registry entries, including snapshots of all associated tool definitions.
- Support one-click deployment: activate a predefined skill from the registry and make it immediately available to agents.
- Manage registry entry lifecycle: publish, update, unpublish.
- Support team-scoped visibility: registry entries can be shared within a team or published to the entire platform.
- Track deployment history, usage metrics, and version metadata for each registry entry.

**Interactions.** The Skills Registry integrates with the MCP Server Service for skill exposure and with Team Management for visibility scoping. Agents discover available skills through standard MCP protocol methods and invoke them like any other MCP capability.

### 3.9 Bifrost Internal Router

**Purpose.** Bifrost is the internal routing plane that sits between the Backend Services / Skills Hub and the MCP Registry. It abstracts the complexity of multi-protocol, multi-origin MCP server connectivity into a unified dispatch layer.

**Responsibilities.**
- Route MCP method calls (`tools/call`, `resources/read`, `prompts/get`) to the correct backend regardless of where the target capability resides -- locally registered, on a federated peer, or behind a stdio bridge.
- Translate between transport protocols: normalize stdio, SSE, and Streamable HTTP into a consistent internal request/response format.
- Forward requests to federated gateways when the target tool is owned by a remote peer, and aggregate multi-gateway responses.
- Enforce routing policies: tool-level access controls, team visibility scoping, and rate limiting are evaluated during routing.
- Handle protocol version negotiation between the gateway's internal protocol and each backend MCP server's supported protocol version.

**Inputs.** Routed MCP requests from backend services, target tool/resource/prompt identifiers.
**Outputs.** Responses from the target MCP server, normalized to the platform's internal format.

**Design Considerations.** Bifrost is designed as a protocol-aware dispatch layer, not a simple HTTP proxy. It understands MCP semantics (tool invocation vs. resource read vs. prompt rendering) and applies appropriate routing logic for each operation type. This separation means that adding a new MCP server type (e.g., a gRPC-based MCP backend) requires changes only within Bifrost's transport adapter layer, without affecting upstream services or downstream consumers.

A detailed explanation of Bifrost is provided in [Section 6.1](#61-bifrost-internal-router).

### 3.10 Data Layer

**Purpose.** The Data Layer provides persistent storage and high-speed caching for the entire platform.

#### 3.10.1 Cache Layer -- Redis

**Purpose.** Redis provides a distributed, high-speed caching and session management layer for the platform.

**Responsibilities.**
- Cache frequently accessed data: tool catalogs, gateway capability lists, resource content, and server metadata.
- Manage distributed sessions for multi-worker deployments, ensuring session state is consistent across application instances.
- Provide session locking with configurable retries to prevent race conditions in concurrent environments.
- Support configurable TTLs per cache category (catalog cache, session cache, resource cache).
- Serve as the backing store for rate limiting counters and temporary operational state.

**Configuration.** The platform supports multiple cache backends: Redis (recommended for production multi-worker deployments), in-memory (single-worker development), database-backed (fallback for environments without Redis), or none (caching disabled). All cache keys are prefixed with a configurable namespace (`mcpgw:` by default) to support shared Redis instances.

**Operational Considerations.** Redis is optional. The platform operates without Redis using database-backed caching, but Redis is strongly recommended for production deployments with multiple worker processes to ensure session consistency and cache coherence.

#### 3.10.2 Database -- PostgreSQL

**Purpose.** PostgreSQL serves as the primary persistent data store for all platform entities, configuration, metrics, and audit logs.

**Responsibilities.**
- Store all core MCP entities: tools, resources, prompts, servers, gateways, registry entries, and A2A agents.
- Store authentication and authorization data: users, teams, roles, permissions, API tokens, SSO sessions, OAuth tokens.
- Store observability data: distributed traces, spans, events, and metrics when database-backed observability is enabled.
- Store operational data: tool generation jobs, import/export tracking, session records, and permission audit logs.
- Manage schema evolution through Alembic-based migration framework.

**Data Model.** The platform uses an ORM with 50+ model classes organized into logical domains. Key model groups include:

| Domain | Models | Purpose |
|---|---|---|
| MCP Core | Tool, Resource, Prompt, Server, Gateway | Core MCP entity storage with metrics, schemas, and lifecycle state |
| Authentication | EmailUser, EmailTeam, Role, UserRole, SSOProvider | User identity, team membership, role-based access |
| Tokens | EmailApiToken, OAuthToken, TokenRevocation, TokenUsageLog | API token management, OAuth flows, token lifecycle |
| Observability | ObservabilityTrace, ObservabilitySpan, ObservabilityEvent, ObservabilityMetric | Distributed tracing and metrics storage |
| Metrics | ToolMetric, ResourceMetric, ServerMetric, PromptMetric, A2AAgentMetric | Per-entity performance and usage metrics |
| Operations | ToolGenerationJob, RegistryEntry, SessionRecord | Background jobs, registry, session management |
| Audit | PermissionAuditLog, EmailTeamMemberHistory | Compliance and audit trail |

**Design Considerations.** All entities include comprehensive audit metadata: `created_by`, `created_from_ip`, `created_via`, `created_user_agent`, `modified_by`, `modified_from_ip`, `modified_via`, `modified_user_agent`, and `version`. This enables full provenance tracking for compliance and debugging. The `created_via` field tracks the origin of each entity (manual creation, API import, federation sync, registry deployment, catalog registration, etc.).

Connection pooling is configured for production workloads: pool sizes, overflow limits, timeouts, and recycle intervals are tunable via environment variables. PostgreSQL connections use TCP keepalive settings to maintain long-lived connections through network infrastructure.

### 3.11 MCP Registry

**Purpose.** The MCP Registry is the organizational layer that categorizes and manages all MCP server connections accessible to the platform. It supports three distinct categories of MCP connectivity.

#### 3.11.1 External MCPs

External MCP servers are third-party or remotely hosted MCP endpoints that the platform connects to over the network. These may use ContextForge for external MCP integrations, providing authenticated gateway connections over SSE or Streamable HTTP transports. External MCPs are discovered through federation (DNS-SD, peer exchange, or static configuration) or registered manually. The platform ingests their tool, resource, and prompt catalogs upon connection and monitors their health continuously.

#### 3.11.2 Internal MCPs

Internal MCPs are tools, resources, and prompts that are registered directly within the platform. These capabilities are stored in the platform's database, managed through the Skills Hub, and executed by the platform's own service layer. Internal MCPs do not require external network connectivity -- they represent capabilities that the platform itself provides, whether authored manually, imported via bulk operations, or generated by the MCP Tools Generator.

#### 3.11.3 Stdio Auto Created MCPs

Stdio Auto Created MCPs are local command-line MCP servers that are automatically bridged to HTTP/SSE transports by the platform's built-in translation layer. When a stdio-based MCP server command is configured (e.g., a command-line tool that speaks MCP over standard input/output), the platform spawns a bridge subprocess that wraps the stdio interface in an HTTP/SSE endpoint. This bridged server is then registered as a gateway, and its tools become available to platform consumers like any other MCP tools.

**Routing Unification.** Bifrost serves as the internal router across all three MCP categories. Consumers do not need to know whether a tool originates from an external, internal, or stdio-based server -- Bifrost routes the request to the correct backend transparently.

### 3.12 Observability

**Purpose.** The Observability stack provides comprehensive visibility into the platform's operation across three pillars: logs, traces, and metrics.

#### 3.12.1 Logging Trace Pipeline

**Purpose.** Produces structured, machine-parseable logs for every platform operation.

**Responsibilities.**
- Emit structured JSON logs for all HTTP requests and responses, including method, path, status code, response time, and authenticated user identity.
- Automatically mask sensitive data in log output: JWT tokens, passwords, API keys, OAuth tokens, and other configurable secret patterns are redacted.
- Support dual output: console (human-readable text format) and file (JSON format with configurable rotation).
- Classify log severity using RFC 5424 levels (DEBUG, INFO, WARNING, ERROR, CRITICAL).
- Support file rotation with configurable maximum size and backup count to prevent disk exhaustion.

#### 3.12.2 LogFire

**Purpose.** LogFire is the dedicated logging layer for AI agent activity. It captures, aggregates, and provides analytics over the interactions between AI agents and the platform -- including tool invocations, skill executions, prompt renderings, and agent-to-agent communications.

**Responsibilities.**
- Capture and aggregate logs from all AI agent interactions: tool calls, skill invocations, resource reads, prompt renderings, and A2A communications.
- Provide per-agent activity trails: track which agent invoked which tools, with what arguments, and what results were returned, enabling auditability and debugging of agentic workflows.
- Enable real-time streaming of agent activity logs for operational monitoring and incident response.
- Provide log search and filtering capabilities for troubleshooting agent behavior and diagnosing failed tool invocations.
- Support fire-and-forget log shipping: agent activity logging never blocks the primary request path or degrades agent response times.

#### 3.12.3 Metrics Collector

**Purpose.** Collects, aggregates, and exposes operational and business metrics for external monitoring systems.

**Responsibilities.**
- Instrument all HTTP endpoints with Prometheus-compatible metrics: request counts by method/endpoint/status, request duration histograms, request and response size histograms.
- Expose metrics via a dedicated `/metrics/prometheus` endpoint with GZip compression.
- Support custom application labels for metrics segmentation (environment, version, deployment region).
- Provide configurable endpoint exclusion patterns to suppress metrics for health checks, static assets, and other high-frequency, low-value endpoints.
- Support distributed tracing via OpenTelemetry with W3C Trace Context (`traceparent` header) propagation, enabling end-to-end request tracing across federated gateways.
- Export traces to multiple backends: OTLP (gRPC/HTTP), Jaeger, Zipkin, or console output.
- Store traces, spans, and events in the platform's own database for built-in trace analysis when external tracing infrastructure is not available.

---

## 4. Layered Architecture

The platform is organized into eight logical layers, each with a well-defined responsibility and interface to adjacent layers. Requests flow top-to-bottom from consumers to backends, and responses flow bottom-to-top.

### Layer 1: Experience Layer

**Components:** AI Agents, React Web Dashboard

This is the consumer-facing layer. AI agents interact programmatically through MCP protocol methods, while human operators use the React Dashboard for visual management. Both consumer types connect through the same Transport Layer and API Gateway, ensuring consistent security and observability regardless of the client type.

### Layer 2: Transport / Protocol Layer

**Components:** HTTP / SSE

The transport layer adapts external protocols (HTTP, SSE, WebSocket) into internal request representations. It manages connection lifecycle (session establishment, keepalive, graceful disconnection) and transport-specific concerns (SSE event formatting, WebSocket frame handling, Streamable HTTP event resumability). The transport layer is the first layer that touches raw network traffic and the last layer that touches raw response data.

### Layer 3: Gateway and Middleware Layer

**Components:** API Gateway, Middleware Stack (Auth, RBAC, CORS, RequestLogger, SecurityHeaders, Compression)

The gateway layer is the policy enforcement boundary. Every request, regardless of origin, passes through the full middleware pipeline. Authentication is validated, authorization is checked, security headers are applied, and the request is logged before any business logic executes. This layer is stateless -- it does not persist data, but it reads from the Data Layer to validate tokens and check permissions.

### Layer 4: Core Backend Services Layer

**Components:** Federation Layer, Gateway Service, Tool Service, Team Management, A2A Service, MCP Server Service

The services layer contains the domain logic of the platform. Each service manages a specific entity type (tools, gateways, servers, teams, agents) with consistent patterns: CRUD operations, event notifications, metrics collection, and plugin hook execution. Services interact with each other through direct method calls (e.g., MCP Server Service queries Tool Service when composing a virtual server) and with the Data Layer for persistence.

### Layer 5: Tool and Skills Enablement Layer

**Components:** MCP Tools Generator (Ingestion Pipeline, Crawler Engine, Tool Enhancer Agent, Evaluation Agent), Skills Hub (Skills Manager, Skills Registry)

The enablement layer accelerates MCP adoption by automating tool creation and providing managed catalogs. The MCP Tools Generator feeds into the Skills Hub: generated tools are registered through the Skills Manager and published through the Skills Registry. This layer operates asynchronously -- tool generation runs as background jobs and does not block request processing.

### Layer 6: Routing and MCP Connectivity Layer

**Components:** Bifrost Internal Router, MCP Registry (External MCPs, Internal MCPs, Stdio Auto Created MCPs)

The routing layer decouples the platform's service logic from the specifics of MCP server connectivity. Bifrost determines where each tool/resource/prompt request should be dispatched, translates between protocols as needed, and returns normalized responses. The MCP Registry maintains the catalog of all available backends across all three connectivity modes. This layer is the boundary between the platform and the MCP ecosystem.

### Layer 7: Data Layer

**Components:** Cache Layer (Redis), Database (PostgreSQL)

The data layer provides persistence and caching for all other layers. PostgreSQL stores the authoritative state of all entities, while Redis provides high-speed access to frequently used data and manages distributed session state. The data layer is accessed by services in Layer 4, the Skills Hub in Layer 5, and the observability stack.

### Layer 8: Observability Layer

**Components:** Logging Trace Pipeline, LogFire, Metrics Collector

The observability layer spans all other layers. Logging middleware in Layer 3 captures request/response data. Service instrumentation in Layer 4 records business metrics. Tracing propagation follows requests from Layer 2 through Layer 6. Metrics are exposed for external scraping, and traces are exported to configured backends. This layer is cross-cutting by design -- it observes without participating in request processing.

---

## 5. End-to-End Request Flows

### 5.1 Flow 1: AI Agent Calling a Tool

This flow describes how an AI agent discovers and invokes an MCP tool through the platform.

1. **Agent connects.** The AI agent establishes an SSE session with the platform by sending an HTTP request to the MCP endpoint. The transport layer assigns a session UUID and opens an SSE event stream.

2. **Agent authenticates.** The agent provides a JWT bearer token in the Authorization header. The Auth middleware validates the token signature, checks expiration, verifies audience claims, and confirms the token has not been revoked. The RBAC middleware resolves the user's roles and permissions.

3. **Agent discovers tools.** The agent sends a `tools/list` JSON-RPC request. The request passes through the Middleware Stack (logging, security headers, compression applied). The Tool Service queries the database for all tools visible to the authenticated user's team scope. If federation is enabled, the Federation Layer aggregates tool lists from peer gateways. The combined list is returned to the agent.

4. **Agent invokes a tool.** The agent sends a `tools/call` JSON-RPC request with the tool name and arguments. The request passes through the Middleware Stack. The Tool Service validates the arguments against the tool's JSON Schema.

5. **Bifrost routes the request.** Bifrost determines the tool's origin:
   - **Local tool:** Bifrost dispatches directly to the tool's configured endpoint.
   - **Federated tool:** Bifrost forwards the request through the Federation Layer to the owning gateway.
   - **Stdio tool:** Bifrost routes through the stdio bridge to the local MCP process.

6. **Plugin chain executes.** Before invocation, pre-invoke plugins run (PII filtering, content moderation, rate limiting). After invocation, post-invoke plugins run (response caching, JSON repair, schema validation).

7. **Response returns.** The tool result flows back through Bifrost, through the service layer, and is delivered to the agent via the SSE event stream. Metrics are recorded: invocation count, response time, success/failure status.

### 5.2 Flow 2: Dashboard User Managing MCP Tools or Skills

This flow describes how a platform administrator uses the React Dashboard to manage tools and skills.

1. **User authenticates.** The administrator logs into the Dashboard via username/password or SSO. The Dashboard obtains a JWT token from the platform's auth endpoints.

2. **User navigates to tool management.** The Dashboard fetches the current tool list by sending a `GET /tools` request to the API Gateway. The middleware validates the JWT, checks RBAC permissions (the user must have tool management permission for their team), and returns the list.

3. **User creates a new tool.** The administrator fills in the tool definition form (name, description, input schema, endpoint URL, authentication configuration) and submits. The Dashboard sends a `POST /tools` request. The Tool Service validates the schema, checks for naming conflicts, and persists the tool to the database.

4. **User publishes to Skills Registry.** The administrator selects a virtual server containing the new tool and clicks "Publish to Registry." The Skills Registry creates a snapshot of the server configuration, including all associated tool definitions, and stores it as a registry entry.

5. **Other team members discover and deploy.** Team members browse the Skills Registry, find the published server, and deploy it to their own workspace using one-click deployment. The Registry Service recreates the server configuration with all tools from the snapshot.

### 5.3 Flow 3: Request Routing Through Bifrost to Internal or External MCPs

This flow illustrates how Bifrost routes a single tool invocation to different MCP backend types.

**Scenario A: Internal MCP**

1. A `tools/call` request arrives at the Tool Service for a locally registered tool.
2. Bifrost identifies the tool as internal (no `federation_source`, no `gateway_id`).
3. Bifrost dispatches the request directly to the tool's configured HTTP endpoint.
4. The response is returned without protocol translation.

**Scenario B: External MCP (Federated)**

1. A `tools/call` request arrives for a tool owned by a remote gateway.
2. Bifrost identifies the tool as federated (has `federation_source` and `gateway_id`).
3. Bifrost delegates to the Federation Layer's forwarding service.
4. The forwarding service establishes an SSE or Streamable HTTP connection to the remote gateway, authenticating with the stored credentials.
5. The request is forwarded in MCP JSON-RPC format.
6. The remote gateway processes the request and returns the result.
7. Bifrost normalizes the response format and returns it to the caller.

**Scenario C: Stdio Auto Created MCP**

1. A `tools/call` request arrives for a tool registered through a stdio bridge.
2. Bifrost identifies the tool as stdio-based (associated with a gateway that has a `stdio_command`).
3. Bifrost routes the request to the stdio bridge's HTTP/SSE endpoint.
4. The bridge subprocess translates the HTTP request into stdin/stdout communication with the underlying MCP server process.
5. The MCP server processes the request and writes the response to stdout.
6. The bridge translates the stdout response back to HTTP and returns it through Bifrost.

### 5.4 Flow 4: Auto-Generated MCP Creation Flow

This flow describes the end-to-end process of generating MCP tools from an API documentation URL.

1. **User initiates generation.** An administrator provides an API documentation URL through the Dashboard or API. The Tool Generation Job Service creates a background job in `pending` state with the provided parameters.

2. **Job starts.** The job service picks up the job (subject to concurrency semaphore limits), transitions it to `running` state, and begins the pipeline.

3. **Crawler Engine executes.** The Doc Crawler performs BFS traversal of the documentation site:
   - Each URL is validated for SSRF safety before fetching.
   - `robots.txt` is consulted and respected.
   - Pages are classified (documentation, authentication, API reference).
   - OpenAPI specification links are auto-detected and extracted.
   - Page content is scraped and stored.

4. **Ingestion Pipeline processes results.** The extracted content and any discovered OpenAPI specs are normalized into a structured format. If an OpenAPI spec was found, it is parsed and validated.

5. **Tool Enhancer Agent analyzes.** The AI agent processes the API information:
   - For OpenAPI specs: endpoints are extracted, tool descriptions are generated, input schemas are mapped, authentication requirements are analyzed.
   - For unstructured documentation: the AI agent extracts endpoint definitions, parameter types, and usage patterns from prose.

6. **Evaluation Agent validates.** The generated tool definitions undergo quality assurance:
   - Endpoint URLs are validated for correctness.
   - Duplicate endpoints are identified and merged.
   - Schema issues are corrected automatically where possible.
   - Each tool receives a confidence score.
   - Metadata (tags, categories, examples) is enriched.

7. **Tools are registered.** Validated tools are registered through the Skills Manager with appropriate conflict resolution (skip, update, rename, or fail on duplicates). The tools are associated with a new virtual MCP server.

8. **Job completes.** The job transitions to `completed` state. The administrator can review the generated tools, adjust configurations, and activate them for production use.

### 5.5 Flow 5: Skills Discovery and Execution

This flow describes how a consumer discovers available skills and executes one.

1. **Consumer queries the Skills Registry.** An AI agent or Dashboard user requests the list of available registry entries, optionally filtered by team scope, tags, or categories.

2. **Registry returns catalog.** The Skills Registry returns a list of published server configurations with their associated tool counts, descriptions, and deployment metadata.

3. **Consumer selects and deploys.** The consumer selects a registry entry and triggers deployment. The Registry Service recreates the virtual server from the snapshot: all tools, resources, and prompts are registered with the platform, and a new MCP server endpoint is created.

4. **Consumer discovers deployed tools.** The consumer sends a `tools/list` request scoped to the newly deployed server. The Skills Manager returns the server's tool catalog.

5. **Consumer invokes a tool.** The consumer sends a `tools/call` request. The request is routed through Bifrost to the appropriate backend (the deployed tools may target internal endpoints, external APIs, or federated gateways depending on their original configuration).

6. **Metrics are recorded.** The Tool Service and MCP Server Service record invocation metrics for the deployed tools and server, enabling performance monitoring and usage analytics.

---

## 6. Deep Dive: Special Components

### 6.1 Bifrost Internal Router

Bifrost is the platform's internal routing plane, named after the bridge between realms in Norse mythology. It serves as the single abstraction layer that connects the platform's service and skills layers to the diverse ecosystem of MCP backends.

**Routing Abstraction.** The fundamental problem Bifrost solves is that MCP servers can be reached through different protocols (HTTP, SSE, Streamable HTTP, stdio), reside in different locations (local, remote, federated), and require different authentication mechanisms. Without a routing abstraction, every service in the platform would need to understand these differences. Bifrost centralizes this complexity, presenting a uniform dispatch interface to upstream services.

**Protocol Normalization.** When a request arrives at Bifrost, it determines the target server's transport type and translates the request into the appropriate wire format:
- For HTTP-based servers: the request is forwarded as a standard HTTP POST with JSON-RPC payload.
- For SSE-based servers: Bifrost establishes or reuses an SSE session, sends the request, and waits for the response event.
- For stdio-based servers: Bifrost routes through the stdio bridge, which translates between HTTP and the stdin/stdout protocol of the underlying process.
- For Streamable HTTP servers: Bifrost uses the Streamable HTTP client with optional SSE response mode.

**Request Dispatching.** Bifrost maintains an internal routing table that maps tool identifiers to their owning backends. When a `tools/call` request arrives:
1. Bifrost looks up the tool's origin (local database, federation source, stdio gateway).
2. It selects the appropriate transport client for the target backend.
3. It forwards the request, including authentication context, and awaits the response.
4. It normalizes the response into the platform's internal format.
5. It returns the response to the calling service.

**Policy Enforcement.** Bifrost serves as a policy enforcement point during routing. Before dispatching a request, it can evaluate:
- Team-level visibility: is the requesting user's team allowed to access this tool?
- Tool-level activation: is the target tool currently active?
- Federation trust: is the target gateway currently healthy and trusted?

**Extensibility.** Bifrost's transport adapter architecture allows new MCP server types to be added by implementing a new transport adapter. The adapter provides `connect()`, `send_message()`, `receive_message()`, and `disconnect()` methods. Once registered, Bifrost routes to the new server type automatically. This design has already been used to support gRPC-based MCP servers as an experimental capability.

### 6.2 MCP Tools Generator

The MCP Tools Generator is one of the platform's most distinctive capabilities. It addresses a key pain point in MCP adoption: the manual effort required to author MCP tool definitions for existing APIs.

**Pipeline Architecture.** The generator operates as a four-stage pipeline where each stage's output feeds into the next:

```
[Documentation URL] → Ingestion Pipeline → Crawler Engine → Tool Enhancer Agent → Evaluation Agent → [Registered Tools]
```

**Multi-Format Input.** The ingestion stage accepts a wide range of input formats, reflecting the reality that API documentation exists in many forms: machine-readable OpenAPI specifications, Postman collections, human-readable HTML documentation sites, PDF manuals, and Markdown files. The platform normalizes all of these into a common internal representation.

**Intelligent Crawling.** The Crawler Engine is powered by Firecrawl, which provides intelligent, multi-page crawling with automatic content extraction and structuring. It performs security-aware crawling (SSRF protection on every hop), respects web standards (`robots.txt`), classifies pages by type (API reference, authentication guide, changelog, unrelated content), handles JavaScript-rendered pages, and auto-detects machine-readable API specifications embedded within documentation sites. Firecrawl's advanced rendering capabilities ensure that even complex, dynamically generated documentation sites are crawled accurately.

**AI-Powered Enhancement.** The Tool Enhancer Agent uses large language model capabilities to understand API semantics beyond what structured formats provide. For an OpenAPI spec, this means generating human-readable descriptions that capture the business purpose of each endpoint, not just its technical signature. For unstructured documentation, it means extracting endpoint definitions from prose -- a task that requires natural language understanding of authentication flows, parameter relationships, and error handling patterns.

**Quality Assurance.** The Evaluation Agent applies a multi-step validation pipeline: endpoint validation (are the URLs reachable?), deduplication (are there semantically equivalent tools?), correction (are schemas well-formed?), enrichment (can we add tags or examples?), and confidence scoring (how confident are we in this tool definition?). This pipeline ensures that auto-generated tools meet the same quality bar as manually authored ones.

**Operational Model.** Tool generation runs as managed background jobs with configurable concurrency limits. This prevents resource exhaustion when multiple users trigger generation simultaneously. Each job maintains detailed state tracking, enabling operators to monitor progress, inspect intermediate results, and cancel jobs if needed.

### 6.3 Skills Hub

The Skills Hub is conceptually similar to how Claude exposes skills to its users -- but designed for enterprise AI agent ecosystems and exposed through MCP. Where Claude's skills give individual users access to specialized capabilities (web search, code execution, file analysis), the Skills Hub enables organizations to define, manage, and expose their own composable skills to any MCP-compliant AI agent at scale.

**Skills Manager -- Authoring and Lifecycle.** The Skills Manager is the authoring layer where skills are created by composing tools, resources, and prompts into cohesive units of capability. Each skill represents a self-contained capability that an agent can discover and invoke -- for example, a "Customer Lookup" skill that combines a CRM API tool, a customer data resource, and a response formatting prompt into a single, agent-friendly capability.

The Skills Manager handles the full lifecycle: creation, validation, activation, invocation, and retirement. It enforces consistency rules (unique names, valid schemas, appropriate permissions) and provides a plugin hook system that allows custom logic at every stage. At each lifecycle event (pre-create, post-create, pre-invoke, post-invoke), registered plugins can inspect, modify, or reject operations. This enables enterprise-specific policies: PII detection before skill invocation, content moderation on responses, schema validation against corporate standards, or automated caching of expensive results.

**Skills Registry -- Predefined Skills for Agents.** The Skills Registry provides a catalog of predefined, ready-to-use skills that agents can leverage immediately without manual configuration. These are curated, validated skill configurations covering common enterprise use cases -- data retrieval, document processing, API integration, workflow automation -- that agents can discover through standard MCP protocol methods.

The Registry uses a snapshot-based versioning model. When a skill configuration is published, the Registry captures the complete definition at that point in time. This snapshot is immutable, ensuring that agents always get the exact configuration that was validated and approved. Organizations can build up their Skills Registry over time, creating an internal library of trusted, production-ready capabilities that any authorized agent can use.

### 6.4 Federation Layer

The Federation Layer transforms MCP Foundry from a single-instance gateway into a node in a mesh of interconnected MCP platforms. Federation is critical for enterprises with distributed infrastructure, multi-region deployments, or partnerships that require cross-organizational tool sharing.

**Discovery Mechanisms.** The platform supports four discovery methods, providing flexibility for different network topologies:
- **DNS-SD/mDNS (Zeroconf):** Automatic discovery of peer gateways on the local network segment. Ideal for development environments and co-located data centers.
- **Static peer lists:** Deterministic configuration of known peer gateway URLs. Suitable for production environments with stable infrastructure.
- **Peer exchange:** Transitive peer discovery where gateways share their peer lists with each other. Enables organic growth of the federation mesh without central coordination.
- **Manual registration:** Direct registration of individual peer gateways through the API or Dashboard. Provides precise control for sensitive environments.

**Capability Synchronization.** When a peer gateway is discovered and authenticated, the Federation Layer ingests its tool, resource, and prompt catalogs. These federated capabilities are stored in the local database with their `federation_source` tracked, ensuring that the platform always knows where each capability originates.

**Transparent Routing.** From a consumer's perspective, federated tools are indistinguishable from local tools. The `tools/list` response includes both local and federated tools. When a federated tool is invoked, Bifrost routes the request to the owning gateway automatically. This transparency is what makes federation practical -- agents do not need to be aware of the platform's federation topology.

**Health Monitoring.** The Federation Layer continuously monitors peer health with configurable check intervals and unhealthy thresholds. Unhealthy peers are excluded from routing decisions until they recover. Health status is visible in the Dashboard and through the admin API.

### 6.5 A2A Service

The A2A (Agent-to-Agent) Service extends the platform beyond tool invocation into multi-agent orchestration. While tools are stateless functions that process a request and return a result, agents are stateful entities with their own capabilities, instructions, and interaction patterns.

**Agent Registration.** External AI agents register with the platform by providing their endpoint URL, supported capabilities, agent type, and authentication requirements. The platform stores this registration and makes the agent discoverable to other agents and services.

**Agent Communication.** The A2A Service proxies communication between agents through the platform. This mediated communication model ensures that all inter-agent traffic passes through the platform's security, logging, and metrics infrastructure. Agents do not communicate directly with each other -- the platform is always in the path.

**Composition.** A2A agents can be included in virtual MCP server configurations alongside tools, resources, and prompts. This allows operators to create server endpoints that offer a mix of deterministic tools and intelligent agent capabilities, enabling sophisticated orchestration patterns.

**Metrics and Observability.** Every agent interaction is tracked: invocation count, response time, failure rate. These metrics enable operators to identify poorly performing agents, detect communication patterns, and optimize multi-agent workflows.

### 6.6 MCP Server Service

The MCP Server Service manages the abstraction of virtual MCP servers -- logical groupings of capabilities that are exposed as cohesive endpoints.

**Server Composition.** A virtual server is defined by selecting a set of tools, resources, prompts, and optionally A2A agents. These associations are many-to-many: a single tool can belong to multiple servers, and a server can contain capabilities from different origins (local, federated, auto-generated). This composition model allows operators to create purpose-built server endpoints for specific use cases (e.g., a "Finance Tools" server, a "Customer Data" server) without duplicating underlying capabilities.

**Lifecycle Management.** The MCP Server Service manages server creation, activation, health checking, and decommissioning. Each server tracks its transport type (SSE, WebSocket, stdio) and connection parameters. Health checks verify that the server's constituent tools are operational.

**Stdio Bridge Integration.** For stdio-based MCP servers, the MCP Server Service coordinates with the stdio bridge manager to spawn and manage the bridge subprocesses that translate between the platform's HTTP interface and the MCP server's stdin/stdout protocol. The bridge handles process lifecycle, output parsing, and error recovery.

**Registry Integration.** Servers can be published to the Skills Registry, creating immutable snapshots of their configuration. This enables reproducible deployments and organizational sharing of validated server configurations.

---

## 7. Security and Governance

The platform implements defense-in-depth security with multiple enforcement layers.

### Authentication

The platform supports multiple authentication mechanisms to accommodate diverse enterprise environments:

| Mechanism | Description | Use Case |
|---|---|---|
| **JWT (HS256/RS256/Ed25519)** | JSON Web Tokens with configurable signing algorithms | Programmatic API access, agent authentication |
| **SSO (SAML/OIDC)** | Integration with enterprise identity providers: Keycloak, GitHub, Google, Okta, Microsoft Entra ID | Browser-based dashboard access, corporate identity federation |
| **API Tokens** | Long-lived tokens with configurable scopes and expiration | CI/CD pipelines, service accounts, automated integrations |
| **Basic Auth** | Username/password with Argon2id hashing | Development environments, simple deployments |
| **External JWT Validation** | Accept JWTs issued by external identity providers via JWKS | Zero-trust architectures, proxy-based authentication |

Authentication is enforced at the middleware layer, ensuring that every request is validated before reaching business logic. The platform supports plugin-based custom authentication, allowing organizations to inject proprietary authentication schemes through the hook system.

### Role-Based Access Control (RBAC)

The RBAC system operates at three scope levels:

- **Global scope:** Platform-wide permissions (create teams, manage federation, access admin functions).
- **Team scope:** Permissions within a specific team (manage team tools, view team metrics, invite members).
- **Personal scope:** User-specific permissions (manage own API tokens, view own audit history).

Permission decisions are logged to a `PermissionAuditLog` table for compliance and forensic analysis. The permission model is checked at every API endpoint through dependency injection, ensuring that no endpoint can be accessed without proper authorization.

### Token Security

- **Token scoping:** Tokens can be restricted by server ID (only access specific MCP servers), IP address (CIDR-based allowlists), and time window (valid only during specific hours). This enables least-privilege token issuance for automated systems.
- **Token revocation:** Tokens can be revoked immediately. Revocation is checked on every request through the middleware layer.
- **Usage logging:** Every token use is logged with timestamp, IP address, and endpoint accessed, providing a complete audit trail.

### Gateway and Transport Security

- **CORS configuration:** Allowed origins, methods, and headers are explicitly configured. Credentials sharing is controlled independently.
- **Security headers:** CSP, HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, and Permissions-Policy are applied to all responses.
- **SSRF protection:** The crawler engine validates every URL before fetching to prevent Server-Side Request Forgery attacks against internal network resources.
- **TLS certificate management:** Gateway peers can be configured with CA certificates for mutual TLS verification.
- **Proxy trust:** The platform explicitly configures which reverse proxy IPs are trusted for X-Forwarded-For header processing, preventing IP spoofing.

### Team Isolation and Tenancy

Team boundaries enforce data isolation at the application layer. Each team's tools, resources, prompts, servers, and agents are visible only to team members unless explicitly published to the broader platform through the Skills Registry. Team membership changes are tracked in an audit history table, providing a complete record of access changes.

### Policy Enforcement Points

The architecture provides multiple points where organizational policies can be enforced:

1. **Middleware layer:** Authentication, authorization, rate limiting, and security headers.
2. **Plugin hooks:** Pre- and post-execution hooks for tool invocation, resource fetching, and prompt rendering. Plugins can reject operations that violate organizational policies (PII exposure, unauthorized data access, content policy violations).
3. **Bifrost routing:** Team visibility and tool activation checks during request routing.
4. **Token scoping:** IP, time, and server-scope restrictions on tokens.

### Startup Security Validation

The platform validates its security configuration at startup. In production environments, it checks JWT secret strength, verifies that authentication is enabled, confirms federation security settings, and can enforce a `REQUIRE_STRONG_SECRETS` flag that prevents the application from starting with default or weak credentials.

---

## 8. Scalability and Reliability

### Horizontal Scaling

The platform is designed for horizontal scaling behind a load balancer. Key architectural decisions that support this:

- **Stateless services:** Backend services do not maintain in-process state that would prevent load balancing. Session state is externalized to Redis.
- **Distributed session management:** When multiple worker processes handle requests, Redis-backed session storage ensures that any worker can serve any session. Session locking with configurable retries prevents race conditions.
- **Connection pooling:** Database connections are managed through a configurable connection pool (pool size, overflow limits, timeouts, recycle intervals) that supports high-concurrency workloads.
- **Multi-worker deployment:** The platform supports deployment behind a process manager with multiple worker processes, with Redis providing the shared state layer.

### Why Redis

Redis serves three critical functions in a scaled deployment:

1. **Session coherence:** In a multi-worker environment, session state must be accessible from any worker. Redis provides the shared session store.
2. **Cache performance:** Frequently accessed data (tool catalogs, gateway capability lists, resource content) is cached in Redis with configurable TTLs, reducing database load.
3. **Operational state:** Rate limiting counters, temporary locks, and other operational state that must be shared across workers is stored in Redis.

Redis is optional for single-worker deployments. The platform gracefully falls back to database-backed or in-memory caching when Redis is unavailable.

### Why PostgreSQL

PostgreSQL provides the reliability guarantees required for a production MCP governance platform:

- **ACID transactions:** Tool registrations, permission changes, and configuration updates are atomic. Partial writes cannot leave the system in an inconsistent state.
- **Concurrent access:** PostgreSQL's MVCC (Multi-Version Concurrency Control) supports hundreds of concurrent readers and writers without lock contention.
- **Schema evolution:** Alembic-managed migrations enable zero-downtime schema updates as the platform evolves.
- **JSON support:** PostgreSQL's native JSON/JSONB types are used for flexible schema storage (tool input schemas, agent capabilities, configuration snapshots) without sacrificing query performance.
- **Extensibility:** PostgreSQL extensions (e.g., `pg_cron` for scheduled maintenance tasks) are leveraged for operational automation.

### How Bifrost Enables Independent Evolution

Bifrost's routing abstraction decouples the platform's service layer from the MCP backend ecosystem. This decoupling has concrete scalability benefits:

- **Backend independence:** New MCP server types can be added without modifying service layer code. Only Bifrost's transport adapter layer needs to be extended.
- **Federation scaling:** Adding peer gateways does not increase the complexity of the service layer. Bifrost handles the routing, and the Federation Layer handles the discovery and health monitoring.
- **Gradual migration:** Organizations can migrate from stdio-based servers to HTTP-based servers (or vice versa) without disrupting consumers. Bifrost routes requests to the appropriate transport transparently.

### Reliability Patterns

- **Health checks:** Dedicated `/health` and `/ready` endpoints enable infrastructure orchestrators (Kubernetes, load balancers) to detect and route around unhealthy instances.
- **Graceful shutdown:** The application performs ordered service teardown on shutdown, flushing pending metrics and closing database connections cleanly.
- **Federation health monitoring:** Unhealthy peer gateways are automatically excluded from routing decisions and re-included when they recover.
- **Background job resilience:** Tool generation jobs track their state persistently. If a worker crashes, the job remains in `running` state and can be identified as stale by the automatic cleanup process.
- **Caching layer:** The Nginx caching proxy in front of the gateway provides CDN-like caching for static assets and cacheable API responses, reducing backend load and improving response times.

---

## 9. Observability and Operations

### Why Observability Matters in MCP Systems

MCP-based tool ecosystems are inherently distributed. A single agent request may trigger tool invocations across multiple federated gateways, each involving different transport protocols, authentication mechanisms, and backend services. Without comprehensive observability, diagnosing latency issues, tracing failures, and understanding usage patterns across this distributed landscape becomes intractable.

MCP Foundry addresses this with a three-pillar observability strategy: logs, traces, and metrics.

### Logging Trace Pipeline

The logging pipeline produces structured JSON logs that are machine-parseable while remaining human-readable. Every HTTP request generates a log entry containing: HTTP method, request path, response status code, response time, authenticated user identity, client IP address, and user agent.

Sensitive data is automatically masked before logging. JWT tokens, passwords, API keys, OAuth tokens, and refresh tokens are redacted from log output. The masking patterns are configurable, allowing organizations to add custom sensitive fields.

Dual output is supported: console output uses human-readable text format for development, while file output uses JSON format for machine processing. File logging supports size-based rotation with configurable maximum size and backup count, preventing disk exhaustion in long-running deployments.

### LogFire

LogFire is the dedicated logging layer for AI agent activity. It captures, aggregates, and provides analytics over the interactions between AI agents and the platform -- including tool invocations, skill executions, prompt renderings, and agent-to-agent communications. LogFire provides per-agent activity trails, enabling operators to track which agent invoked which tools, with what arguments, and what results were returned. This auditability is critical for debugging agentic workflows, understanding agent behavior patterns, and ensuring compliance. LogFire uses a fire-and-forget shipping mechanism that ensures agent activity logging never blocks the primary request path or degrades agent response times.

### Metrics Collector

The Metrics Collector provides Prometheus-compatible metrics for integration with standard monitoring infrastructure:

- **HTTP metrics:** Request count, duration histogram, request size histogram, and response size histogram -- segmented by method, endpoint, and status code.
- **Business metrics:** Per-tool invocation counts, success/failure rates, and response time statistics (min, max, average). Per-server aggregate metrics. Per-agent interaction counts.
- **Custom labels:** Application-level labels (environment, version, deployment region) can be attached to all metrics for segmentation in monitoring dashboards.

Metrics are exposed at `/metrics/prometheus` with GZip compression. Endpoint exclusion patterns allow high-frequency, low-value endpoints (health checks, static assets) to be suppressed from metrics collection.

### Distributed Tracing

The platform implements W3C Trace Context propagation using the `traceparent` header format. Traces are generated for every HTTP request and can span multiple service boundaries, including federated gateway hops.

Tracing supports multiple export backends through OpenTelemetry:
- **OTLP (gRPC/HTTP):** For OpenTelemetry-native collectors and observability platforms.
- **Jaeger:** For Jaeger-based distributed tracing infrastructure.
- **Zipkin:** For Zipkin-based tracing deployments.
- **Console:** For development and debugging.

When external tracing infrastructure is not available, the platform stores traces, spans, and events in its own database, providing built-in trace analysis through the Dashboard's observability views.

### Operational Tooling

- **Support bundle generation:** The platform can generate diagnostic bundles containing version information, configuration (with secrets automatically redacted), sanitized logs, platform details, service status, and database/cache information. These bundles accelerate support and troubleshooting workflows.
- **System stats service:** Runtime health metrics including memory usage, CPU utilization, active connections, and background job status.
- **Admin observability views:** The Dashboard provides built-in views for browsing traces, filtering logs, and analyzing metrics without requiring external tools.

---

## 10. Architecture Strengths

**Modularity.** The platform's layered architecture with well-defined service boundaries enables independent development, testing, and deployment of individual components. The middleware pipeline, plugin framework, and transport adapter system all follow the same principle: add new capabilities without modifying existing code.

**Extensibility.** The plugin hook system provides pre- and post-execution interception points across all major operations (authentication, tool invocation, resource fetching, prompt rendering, agent communication). Organizations can implement custom policies, integrations, and transformations without modifying platform source code. Plugins can be internal (Python modules), external (separate MCP-based processes), or managed (containerized).

**Governance.** Enterprise governance is built into the architecture, not bolted on. RBAC operates at global, team, and personal scopes. Token scoping restricts access by IP, time, and server. Permission decisions are logged for audit. Team boundaries enforce data isolation. Startup validation prevents insecure configurations from reaching production.

**Multi-MCP Interoperability.** The Bifrost routing plane and MCP Registry architecture support three distinct MCP connectivity modes (external, internal, stdio auto-created) behind a single API surface. This eliminates the integration complexity that enterprises face when managing diverse MCP server ecosystems.

**Enterprise Readiness.** The platform addresses production requirements that simpler MCP proxies do not: SSO integration with major identity providers (Keycloak, Okta, Microsoft Entra ID, Google, GitHub), PostgreSQL-backed persistence with schema migrations, Prometheus-compatible metrics, OpenTelemetry distributed tracing, support bundle generation, health/readiness probes for Kubernetes, and multi-worker horizontal scaling with Redis-backed sessions.

**Support for Agentic Systems.** The platform is designed for AI-first use cases: MCP protocol-native transport, A2A agent communication, real-time event streaming via SSE, and a tool generation pipeline that uses AI to convert API documentation into agent-ready tools.

**Separation of Concerns.** Each architectural layer has a single, clear responsibility. The transport layer handles protocols. The gateway handles routing and cross-cutting concerns. Services handle business logic. Bifrost handles multi-backend routing. The data layer handles persistence. The observability layer handles monitoring. This separation makes the system understandable, testable, and evolvable.

---

## 11. Assumptions and Reasonable Inferences

The architecture diagram is a high-level representation. The following assumptions and inferences have been made where the diagram leaves ambiguity. Each is labeled explicitly.

**Assumption 1: LogFire Scope.** The diagram shows "LogFire" as a component within the Observability stack. LogFire is the dedicated AI agent activity logging layer, capturing and aggregating logs from all agent interactions with the platform. The logging trace pipeline handles general platform infrastructure logging, while LogFire specifically focuses on agent-centric activity trails and analytics.

**Assumption 2: Bifrost as a Conceptual Layer.** The diagram shows "Bifrost Internal Router" as a single block. In practice, the routing behavior described as Bifrost spans multiple platform components: the API router layer, the transport translation engine, the federation forwarding service, and the gateway service orchestration. This document presents these as a unified routing plane under the Bifrost name, which accurately represents the abstraction that consumers of the platform experience.

**Assumption 3: Skills Hub Composition.** The diagram shows "Skills Hub" with "Skills Manager" and "Skills Registry" sub-components. The Skills Hub is conceptually similar to Claude's skills system, enabling organizations to create and expose composable skills to AI agents through MCP. The Skills Manager handles skill authoring and lifecycle, while the Skills Registry provides a catalog of predefined, ready-to-use skills for agents.

**Assumption 4: React Dashboard as Separate Application.** The React Web Dashboard is shown as a top-level component. Based on the architecture, it is presented as a standalone frontend application that communicates with the platform exclusively through the API Gateway, with no direct database or service access.

**Assumption 5: Evaluation Agent vs. Tool Enhancer Agent Boundary.** The diagram shows these as separate components within the MCP Tools Generator. The platform implements AI-powered tool analysis and a multi-step post-processing pipeline (validation, deduplication, correction, enrichment, confidence scoring). This document maps the AI analysis to the Tool Enhancer Agent and the post-processing validation pipeline to the Evaluation Agent.

**Assumption 6: gRPC Support.** The platform includes experimental gRPC service support (conditional on dependency availability). This is not shown in the architecture diagram and is not presented as a primary capability in this document.

---

## 12. Glossary

| Term | Definition |
|---|---|
| **MCP** | Model Context Protocol. An open protocol that standardizes how AI applications interact with tools, resources, and prompts. MCP defines a JSON-RPC-based messaging format for tool discovery (`tools/list`), tool invocation (`tools/call`), resource access (`resources/read`), and prompt rendering (`prompts/get`). |
| **Bifrost** | The platform's internal routing plane that dispatches MCP requests to the appropriate backend server, handling protocol translation, federation forwarding, and transport normalization across external, internal, and stdio-based MCP servers. |
| **Skills Hub** | The platform's system for creating and exposing composable skills to AI agents through MCP. Conceptually similar to Claude's skills system, but designed for enterprise agent ecosystems. Comprises the Skills Manager (skill authoring and lifecycle) and Skills Registry (predefined skills for agents). |
| **Federation** | The mechanism by which multiple MCP Foundry instances (or compatible gateways) discover each other, exchange capability catalogs, and transparently forward requests. Federated tools appear as local tools to consumers. |
| **A2A** | Agent-to-Agent. A protocol and service for enabling AI agents to discover and communicate with each other through the platform. |
| **SSE** | Server-Sent Events. A unidirectional streaming protocol where the server pushes events to the client over a persistent HTTP connection. Used for real-time MCP protocol sessions and event notifications. |
| **Streamable HTTP** | An MCP transport mode (specification 2025-06-18) that enables bidirectional communication over standard HTTP, with optional SSE streaming for responses. Supports both stateful and stateless operation. |
| **JSON-RPC** | JSON Remote Procedure Call. The messaging format used by the MCP protocol for encoding tool invocations, resource reads, and other operations as structured request/response pairs. |
| **RBAC** | Role-Based Access Control. A security model where permissions are assigned to roles, and roles are assigned to users. The platform supports global, team-scoped, and personal permission levels. |
| **JWT** | JSON Web Token. A compact, URL-safe token format used for authentication. The platform supports HS256, RS256, and Ed25519 signing algorithms. |
| **SSO** | Single Sign-On. Authentication mechanism that allows users to authenticate with the platform using their existing corporate identity provider (Keycloak, Okta, Google, GitHub, Microsoft Entra ID). |
| **OTEL** | OpenTelemetry. A vendor-neutral observability framework for generating, collecting, and exporting telemetry data (traces, metrics, logs). The platform uses OTEL for distributed tracing. |
| **CORS** | Cross-Origin Resource Sharing. A browser security mechanism that controls which web origins can access the platform's API. Configured at the middleware layer. |
| **SSRF** | Server-Side Request Forgery. An attack where a server is tricked into making requests to unintended internal resources. The platform's crawler engine includes SSRF protection on every URL fetch. |
| **Skills Registry** | A catalog of predefined, ready-to-use skills that AI agents can discover and invoke immediately. Uses snapshot-based versioning for reproducibility. |
| **Virtual Server** | A logical MCP server composed by selecting a set of tools, resources, prompts, and A2A agents. Virtual servers are exposed as cohesive MCP endpoints without requiring a separate MCP server process. |
| **Stdio Bridge** | A translation layer that wraps stdio-based MCP servers (processes that communicate over stdin/stdout) in an HTTP/SSE endpoint, making them accessible through the platform's standard API surface. |
| **Plugin Hook** | An extensibility point in the platform's processing pipeline where custom logic (plugins) can be executed. Hooks exist for pre/post authentication, tool invocation, resource fetching, and prompt rendering. |
| **LogFire** | The dedicated AI agent activity logging layer. Captures, aggregates, and provides analytics over agent interactions with the platform, including tool invocations, skill executions, and agent-to-agent communications. |

---

*End of Document*
