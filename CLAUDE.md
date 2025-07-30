# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP Context Forge is a production-grade gateway, registry, and proxy for the Model Context Protocol (MCP). It unifies REST, MCP, and A2A services with federation, virtual servers, retries, security, and an optional admin UI. The project is built with FastAPI and supports deployment via PyPI or Docker.

## Key Commands

### Development Setup
```bash
# Create virtual environment and install dependencies
make venv
make install-dev    # Install with dev dependencies
make install-db     # Install with Postgres and Redis support

# Activate virtual environment
. ~/.venv/mcpgateway/bin/activate
```

### Running the Application
```bash
# Development server with auto-reload
make dev            # Runs on http://localhost:8000

# Production server
make serve          # Gunicorn on :4444
make serve-ssl      # HTTPS with self-signed certs
```

### Testing
```bash
# Run unit tests
make test

# Run tests with coverage
make coverage

# Run smoke test
make smoketest

# Run specific test types
pytest tests/unit/          # Unit tests only
pytest tests/integration/   # Integration tests
pytest tests/e2e/          # End-to-end tests
```

### Code Quality
```bash
# Run all linters
make lint

# Individual linters
make black          # Format code
make isort          # Sort imports
make ruff           # Fast Python linter
make mypy           # Type checking
make bandit         # Security scanning
make flake8         # Style guide enforcement
make pylint         # Code analysis
```

### Database Management
```bash
# Database migrations (uses Alembic)
alembic upgrade head    # Apply migrations
alembic revision --autogenerate -m "description"  # Create new migration
```

## Architecture Overview

### Core Components

1. **FastAPI Application** (`mcpgateway/main.py`)
   - Entry point for all HTTP/WebSocket traffic
   - Handles MCP protocol operations
   - Manages authentication, CORS, caching

2. **Services** (`mcpgateway/services/`)
   - `gateway_service.py`: Gateway registration and federation
   - `server_service.py`: MCP server connections and management
   - `tool_service.py`: Tool discovery and invocation
   - `resource_service.py`: Resource management
   - `prompt_service.py`: Prompt template handling
   - `completion_service.py`: AI completion operations

3. **Transports** (`mcpgateway/transports/`)
   - WebSocket, SSE, stdio, and streamable HTTP support
   - Protocol translation between different transport types

4. **Models** (`mcpgateway/models.py`)
   - SQLAlchemy ORM models for database entities
   - Support for SQLite (default) and PostgreSQL

5. **Federation** (`mcpgateway/federation/`)
   - Discovery of federated gateways
   - Forward requests to remote gateways

### Key Design Patterns

- **Async-first**: All I/O operations use asyncio
- **Dependency Injection**: FastAPI's DI system for services
- **Pluggable Backends**: Redis/in-memory caching, SQLite/PostgreSQL
- **Transport Agnostic**: Unified handling of different MCP transports

### Configuration

Environment variables (see `.env.example`):
- `MCPGATEWAY_DATABASE_URL`: Database connection string
- `MCPGATEWAY_REDIS_URL`: Redis connection (optional)
- `MCPGATEWAY_AUTH_REQUIRED`: Enable authentication
- `MCPGATEWAY_UI_ENABLED`: Enable admin UI
- `MCPGATEWAY_LOG_LEVEL`: Logging verbosity

### Testing Strategy

- **Unit Tests**: Test individual components in isolation
- **Integration Tests**: Test service interactions
- **E2E Tests**: Full workflow testing with real servers
- **Playwright Tests**: UI automation testing

### Common Development Workflows

1. **Adding a New MCP Server**
   - Register via Admin UI or API POST to `/servers`
   - Test with `GET /tools` to verify discovery

2. **Debugging Transport Issues**
   - Check logs with `MCPGATEWAY_LOG_LEVEL=DEBUG`
   - Use transport-specific test endpoints

3. **Database Schema Changes**
   - Modify models in `models.py`
   - Generate migration with Alembic
   - Test migration on dev database first

## Important Notes

- Current version (0.3.1) is alpha/beta - not production-ready
- Always run linters before committing: `make lint`
- Database files (*.db, *.sqlite) are gitignored
- Use type hints throughout the codebase
- Follow existing code style and patterns



# GitHub Issue Creation

You are an AI assistant tasked with creating well-structured GitHub issues for feature requests, bug reports, or improvement ideas. Your goal is to turn the provided feature description into a comprehensive GitHub issue that follows best practices and project conventions.

First, you will be given a feature description and a repository URL. Here they are:

<feature_description> #$ARGUMENTS </feature_description>

Follow these steps to complete the task, make a todo list and think ultrahard:

### 1. Research the repository:
   - Visit the provided repo url and examine the repository's structure, existing issues, and documentation.
   - Look for any CONTRIBUTING.md, ISSUE_TEMPLATE.md, or similar files that contain guidelines for creating issues.
   - Note the project's coding style, naming conventions, and any specific requirements for submitting issues.

### 2. Research best practices:
   - Search for current best practices in writing GitHub issues, focusing on clarity, completeness, and actionability.
   - Look for examples of well-written issues in popular open-source projects for inspiration.

### 3. Present a plan:
   - Based on your research, outline a plan for creating the GitHub issue.
   - Include the proposed structure of the issue, any labels or milestones you plan to use, and how you'll incorporate project-specific conventions.
   - Present this plan in <plan> tags.
   - Include the reference link to featurebase or any other link that has the source of the user request
   *K for Command, *L for Cascade

### 4. Create the GitHub issue:
   - Once the plan is approved, draft the GitHub issue content.
   - Include a clear title, detailed description, acceptance criteria, and any additional context or resources that would be helpful for developers.
   - Use appropriate formatting (e.g., Markdown) to enhance readability.
   - Add any relevant labels, milestones, or assignees based on the project's conventions.

### 5. Final output:
   - Present the complete GitHub issue content in <github_issue> tags.
   - Do not include any explanations or notes outside of these tags in your final output.

Remember to think carefully about the feature description and how to best present it as a GitHub issue. Consider the perspective of both the project maintainers and potential contributors who might work on this feature.

Your final output should consist of only the content within the <github_issue> tags, ready to be copied and pasted directly into GitHub. Make sure to use the GitHub CLI `gh issue create` to create the actual issue after you generate. Assign either the label `bug` or `enhancement` based on the nature of the issue.