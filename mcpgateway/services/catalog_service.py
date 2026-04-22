# -*- coding: utf-8 -*-
"""MCP Server Catalog Service.

This service manages the catalog of available MCP servers that can be
easily registered with one-click from the admin UI.
"""

# Standard
from datetime import datetime, timezone
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

# Third-Party
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
import yaml

# First-Party
from mcpgateway.config import settings
from mcpgateway.schemas import (
    CatalogBulkRegisterRequest,
    CatalogBulkRegisterResponse,
    CatalogListRequest,
    CatalogListResponse,
    CatalogServer,
    CatalogServerRegisterRequest,
    CatalogServerRegisterResponse,
    CatalogServerStatusResponse,
)
from mcpgateway.services.gateway_service import GatewayService
from mcpgateway.utils.create_slug import slugify

logger = logging.getLogger(__name__)


class CatalogService:
    """Service for managing MCP server catalog."""

    def __init__(self):
        """Initialize the catalog service."""
        self._catalog_cache: Optional[Dict[str, Any]] = None
        self._cache_timestamp: float = 0
        self._gateway_service = GatewayService()

    async def load_catalog(self, force_reload: bool = False) -> Dict[str, Any]:
        """Load catalog from YAML file.

        Args:
            force_reload: Force reload even if cache is valid

        Returns:
            Catalog data dictionary
        """
        # Check cache validity
        cache_age = time.time() - self._cache_timestamp
        if not force_reload and self._catalog_cache and cache_age < settings.mcpgateway_catalog_cache_ttl:
            return self._catalog_cache

        try:
            catalog_path = Path(settings.mcpgateway_catalog_file)

            # Try multiple locations for the catalog file
            if not catalog_path.is_absolute():
                # Try current directory first
                if not catalog_path.exists():
                    # Try project root
                    catalog_path = Path(__file__).parent.parent.parent / settings.mcpgateway_catalog_file

            if not catalog_path.exists():
                logger.warning(f"Catalog file not found: {catalog_path}")
                return {"catalog_servers": [], "categories": [], "auth_types": []}

            with open(catalog_path, "r", encoding="utf-8") as f:
                catalog_data = yaml.safe_load(f)

            # Update cache
            self._catalog_cache = catalog_data
            self._cache_timestamp = time.time()

            logger.info(f"Loaded {len(catalog_data.get('catalog_servers', []))} servers from catalog")
            return catalog_data

        except Exception as e:
            logger.error(f"Failed to load catalog: {e}")
            return {"catalog_servers": [], "categories": [], "auth_types": []}

    async def get_catalog_servers(
        self,
        request: CatalogListRequest,
        db,
        user: Optional[Dict[str, Any]] = None,
    ) -> CatalogListResponse:
        """Get filtered list of catalog servers.

        Args:
            request: Filter criteria
            db: Database session
            user: Authenticated user context (dict with at least ``email``); used to scope
                the ``registered_instance_count`` to what this user can actually see.

        Returns:
            Filtered catalog servers response
        """
        catalog_data = await self.load_catalog()
        servers = catalog_data.get("catalog_servers", [])

        # Count registered instances per URL, scoped to what this user can see.
        # A gateway is "visible" to the user if it is public, OR private and owned by them,
        # OR team-scoped to a team they belong to. Public+private alone cover the common
        # cases; team membership is resolved when a user is provided.
        registered_counts: Dict[str, int] = {}
        if servers:
            try:
                # First-Party
                from mcpgateway.db import EmailTeamMember as DbEmailTeamMember  # pylint: disable=import-outside-toplevel
                from mcpgateway.db import Gateway as DbGateway  # pylint: disable=import-outside-toplevel

                # Third-Party
                from sqlalchemy import or_  # pylint: disable=import-outside-toplevel

                user_email = (user or {}).get("email") if user else None
                team_ids: List[str] = []
                if user_email:
                    try:
                        team_rows = db.execute(
                            select(DbEmailTeamMember.team_id).where(
                                DbEmailTeamMember.user_email == user_email,
                                DbEmailTeamMember.is_active.is_(True),
                            )
                        ).all()
                        team_ids = [row[0] for row in team_rows if row[0]]
                    except Exception as te:  # pragma: no cover - defensive
                        logger.warning(f"Failed to resolve team memberships for {user_email}: {te}")
                        team_ids = []

                visibility_clauses = [DbGateway.visibility == "public"]
                if user_email:
                    visibility_clauses.append((DbGateway.visibility == "private") & (DbGateway.owner_email == user_email))
                if team_ids:
                    visibility_clauses.append((DbGateway.visibility == "team") & (DbGateway.team_id.in_(team_ids)))

                stmt = select(DbGateway.url).where(DbGateway.url.is_not(None), or_(*visibility_clauses))
                for row in db.execute(stmt):
                    url = row[0]
                    if not url:
                        continue
                    registered_counts[url] = registered_counts.get(url, 0) + 1
            except Exception as e:
                logger.warning(f"Failed to check registered servers: {e}")
                registered_counts = {}

        # Convert to CatalogServer objects and mark registered ones
        catalog_servers = []
        for server_data in servers:
            server = CatalogServer(**server_data)
            count = registered_counts.get(server.url, 0)
            server.registered_instance_count = count
            server.is_registered = count > 0
            server.source = "catalog"
            # Set availability based on registration status (registered servers are assumed available)
            # Individual health checks can be done via the /status endpoint
            server.is_available = server.is_registered or server_data.get("is_available", True)
            catalog_servers.append(server)

        # Load user-published entries from DB
        try:
            # First-Party
            from mcpgateway.db import RegistryEntry as DbRegistryEntry  # pylint: disable=import-outside-toplevel

            db_stmt = select(DbRegistryEntry).where(DbRegistryEntry.is_active.is_(True))
            db_entries = db.execute(db_stmt).scalars().all()

            for entry in db_entries:
                catalog_server = CatalogServer(
                    id=entry.id,
                    name=entry.name,
                    category=entry.category or "Virtual Server",
                    url="",
                    auth_type="Open",
                    provider=entry.published_by or "User",
                    description=entry.description or "",
                    tags=entry.tags or [],
                    source="user_published",
                    registry_entry_id=entry.id,
                    published_by=entry.published_by,
                    capabilities={"tools": entry.tool_count},
                    is_registered=False,
                    is_available=True,
                )
                catalog_servers.append(catalog_server)
        except Exception as e:
            logger.warning(f"Failed to load user-published registry entries: {e}")

        # Apply filters
        filtered = catalog_servers

        # Source filter
        if request.source:
            filtered = [s for s in filtered if s.source == request.source]

        if request.id:
            filtered = [s for s in filtered if s.id == request.id]

        if request.name:
            name_lower = request.name.lower()
            filtered = [s for s in filtered if name_lower in s.name.lower()]

        if request.category:
            filtered = [s for s in filtered if s.category == request.category]

        if request.auth_type:
            filtered = [s for s in filtered if s.auth_type == request.auth_type]

        if request.provider:
            filtered = [s for s in filtered if s.provider == request.provider]

        if request.search:
            search_lower = request.search.lower()
            filtered = [
                s
                for s in filtered
                if search_lower in s.name.lower() or search_lower in s.description.lower() or search_lower in s.provider.lower() or any(search_lower in tag.lower() for tag in s.tags)
            ]

        if request.tags:
            filtered = [s for s in filtered if any(tag in s.tags for tag in request.tags)]

        if request.show_registered_only:
            filtered = [s for s in filtered if s.is_registered]

        if request.show_available_only:
            filtered = [s for s in filtered if s.is_available]

        # Pagination
        total = len(filtered)
        start = request.offset
        end = start + request.limit
        paginated = filtered[start:end]

        # Collect unique values for filters
        all_categories = sorted(set(s.category for s in catalog_servers))
        all_auth_types = sorted(set(s.auth_type for s in catalog_servers))
        all_providers = sorted(set(s.provider for s in catalog_servers))
        all_tags = sorted(set(tag for s in catalog_servers for tag in s.tags))

        return CatalogListResponse(servers=paginated, total=total, categories=all_categories, auth_types=all_auth_types, providers=all_providers, all_tags=all_tags)

    async def register_catalog_server(
        self,
        catalog_id: str,
        request: Optional[CatalogServerRegisterRequest],
        db: Session,
        user: Optional[Dict[str, Any]] = None,
    ) -> CatalogServerRegisterResponse:
        """Register a catalog server as a gateway.

        Args:
            catalog_id: Catalog server ID
            request: Registration request with optional overrides
            db: Database session
            user: Authenticated user context (dict with at least ``email``)

        Returns:
            Registration response
        """
        # Resolve instance visibility, team, and owner before the big try so the
        # name-conflict handler below can reference the resolved name for a
        # friendly error message.
        owner_email = (user or {}).get("email") if user else None
        requested_visibility = (request.visibility if request and request.visibility else "private").lower()
        if requested_visibility not in ("private", "team", "public"):
            requested_visibility = "private"
        instance_team_id = request.team_id if request and requested_visibility == "team" else None

        try:
            # Load catalog to find the server
            catalog_data = await self.load_catalog()
            servers = catalog_data.get("catalog_servers", [])

            # Find the server in catalog
            server_data = None
            for s in servers:
                if s.get("id") == catalog_id:
                    server_data = s
                    break

            if not server_data:
                return CatalogServerRegisterResponse(success=False, server_id="", message="Server not found in catalog", error="Invalid catalog server ID")

            # Prepare gateway creation request using proper schema
            # First-Party
            from mcpgateway.schemas import GatewayCreate  # pylint: disable=import-outside-toplevel

            # Use explicit transport if provided, otherwise auto-detect from URL
            transport = server_data.get("transport")
            if not transport:
                # Detect transport type from URL or use SSE as default
                url = server_data["url"].lower()
                # Check for WebSocket patterns (highest priority)
                if url.startswith("ws://") or url.startswith("wss://"):
                    transport = "WEBSOCKET"  # WebSocket transport for ws:// and wss:// URLs
                # Check for SSE patterns
                elif url.endswith("/sse") or "/sse/" in url:
                    transport = "SSE"  # SSE endpoints or paths containing /sse/
                # Then check for HTTP patterns
                elif "/mcp" in url or url.endswith("/"):
                    transport = "STREAMABLEHTTP"  # Generic MCP endpoints typically use HTTP
                else:
                    transport = "SSE"  # Default to SSE for most catalog servers

            # Check for IPv6 URLs early to provide a clear error message
            url = server_data["url"]
            if "[" in url or "]" in url:
                return CatalogServerRegisterResponse(
                    success=False, server_id="", message="Registration failed", error="IPv6 URLs are not currently supported for security reasons. Please use IPv4 or domain names."
                )

            # Prepare the gateway creation data
            gateway_data = {
                "name": request.name if request and request.name else server_data["name"],
                "url": server_data["url"],
                "description": request.description if request and request.description else server_data["description"],
                "transport": transport,
                "tags": server_data.get("tags", []),
            }

            # Set authentication based on server requirements
            auth_type = server_data.get("auth_type", "Open")
            skip_initialization = False  # Flag to skip connection test for OAuth servers without creds
            has_oauth_creds = request and request.oauth_credentials and request.oauth_credentials.get("client_id") and request.oauth_credentials.get("client_secret")

            # Build OAuth redirect URI from the public gateway domain
            # so DCR/manual registration works correctly behind proxies.
            oauth_redirect_uri = f"{str(settings.app_domain).rstrip('/')}/oauth/callback"

            if has_oauth_creds and auth_type in ["OAuth2.1", "OAuth", "OAuth2.1 & API Key"]:
                # OAuth credentials provided - configure OAuth auth on the gateway
                catalog_oauth_config = server_data.get("oauth_config", {})
                gateway_data["auth_type"] = "oauth"
                gateway_data["oauth_config"] = {
                    "grant_type": "authorization_code",
                    "client_id": request.oauth_credentials["client_id"],
                    "client_secret": request.oauth_credentials["client_secret"],
                    "authorization_url": catalog_oauth_config.get("authorize_url", ""),
                    "token_url": catalog_oauth_config.get("token_url", ""),
                    "scope": " ".join(catalog_oauth_config.get("scopes", [])),
                    "redirect_uri": oauth_redirect_uri,
                    "store_tokens": True,
                    "auto_refresh": True,
                }
                # Also set API key as bearer if provided alongside OAuth creds
                if request.api_key and auth_type == "OAuth2.1 & API Key":
                    gateway_data["auth_token"] = request.api_key
                logger.info(f"Registering OAuth server {server_data['name']} with client credentials")
            elif request and request.api_key and auth_type != "Open":
                # Handle all possible auth types from the catalog
                if auth_type in ["API Key", "API"]:
                    # Use bearer token for API key authentication
                    gateway_data["auth_type"] = "bearer"
                    gateway_data["auth_token"] = request.api_key
                elif auth_type in ["OAuth2.1", "OAuth", "OAuth2.1 & API Key"]:
                    # OAuth servers and mixed auth may need API key as a bearer token
                    gateway_data["auth_type"] = "bearer"
                    gateway_data["auth_token"] = request.api_key
                else:
                    # For any other auth types, use custom headers (as list of dicts)
                    gateway_data["auth_type"] = "authheaders"
                    gateway_data["auth_headers"] = [{"key": "X-API-Key", "value": request.api_key}]
            elif auth_type in ["OAuth2.1", "OAuth"]:
                # OAuth server without credentials - register but skip initialization
                # User will need to complete OAuth flow later
                skip_initialization = True
                logger.info(f"Registering OAuth server {server_data['name']} without credentials - OAuth flow required later")

            # For OAuth servers without credentials, register directly without connection test
            if skip_initialization:
                # Create minimal gateway entry without tool discovery
                # Store OAuth URLs from catalog so the gateway is ready for OAuth setup later
                # First-Party
                from mcpgateway.db import Gateway as DbGateway  # pylint: disable=import-outside-toplevel

                catalog_oauth_config = server_data.get("oauth_config", {})
                oauth_config_for_gateway = None
                if catalog_oauth_config:
                    oauth_config_for_gateway = {
                        "grant_type": "authorization_code",
                        "authorization_url": catalog_oauth_config.get("authorize_url", ""),
                        "token_url": catalog_oauth_config.get("token_url", ""),
                        "scope": " ".join(catalog_oauth_config.get("scopes", [])),
                        "redirect_uri": oauth_redirect_uri,
                        "store_tokens": True,
                        "auto_refresh": True,
                    }
                    # Include DCR registration URL if available
                    if catalog_oauth_config.get("supports_dcr") and catalog_oauth_config.get("registration_url"):
                        oauth_config_for_gateway["registration_url"] = catalog_oauth_config["registration_url"]
                        oauth_config_for_gateway["supports_dcr"] = True

                slug_name = slugify(gateway_data["name"])

                # Pre-check slug uniqueness in this instance's scope so we can return
                # a friendly conflict response instead of letting IntegrityError surface.
                slug_conflict_stmt = select(DbGateway).where(DbGateway.slug == slug_name)
                if requested_visibility == "public":
                    slug_conflict_stmt = slug_conflict_stmt.where(DbGateway.visibility == "public")
                elif requested_visibility == "team":
                    slug_conflict_stmt = slug_conflict_stmt.where(DbGateway.visibility == "team", DbGateway.team_id == instance_team_id)
                else:
                    slug_conflict_stmt = slug_conflict_stmt.where(DbGateway.visibility == "private", DbGateway.owner_email == owner_email)
                if db.execute(slug_conflict_stmt).scalar_one_or_none():
                    suggestion = f"{gateway_data['name']} Work"
                    return CatalogServerRegisterResponse(
                        success=False,
                        server_id="",
                        message=f"A server named '{gateway_data['name']}' already exists in your scope. Try a different name, e.g. '{suggestion}' or '{gateway_data['name']} 2'. Names may contain letters, numbers, spaces, underscores, and hyphens.",
                        error="name_conflict",
                    )

                db_gateway = DbGateway(
                    name=gateway_data["name"],
                    slug=slug_name,
                    url=gateway_data["url"],
                    description=gateway_data["description"],
                    tags=gateway_data.get("tags", []),
                    transport=gateway_data["transport"],
                    capabilities={},
                    auth_type="oauth" if oauth_config_for_gateway else None,
                    oauth_config=oauth_config_for_gateway,
                    enabled=False,  # Disabled until OAuth credentials are configured
                    created_via="catalog",
                    visibility=requested_visibility,
                    team_id=instance_team_id,
                    owner_email=owner_email,
                    version=1,
                )

                db.add(db_gateway)
                db.commit()
                db.refresh(db_gateway)

                # First-Party
                from mcpgateway.schemas import GatewayRead  # pylint: disable=import-outside-toplevel

                gateway_read = GatewayRead.model_validate(db_gateway)

                return CatalogServerRegisterResponse(
                    success=True,
                    server_id=str(gateway_read.id),
                    message=f"Successfully registered {gateway_read.name} - OAuth configuration required before activation",
                    error=None,
                )

            gateway_create = GatewayCreate(**gateway_data)

            # Use the proper gateway registration method which will discover tools.
            # Visibility defaults to private so multi-instance registrations are
            # scoped per-owner and don't collide in the global public slug namespace.
            gateway_read = await self._gateway_service.register_gateway(
                db=db,
                gateway=gateway_create,
                created_via="catalog",
                created_by=owner_email,
                team_id=instance_team_id,
                owner_email=owner_email,
                visibility=requested_visibility,
            )

            logger.info(f"Registered catalog server: {gateway_read.name} ({catalog_id})")

            # Query for tools discovered from this gateway
            # First-Party
            from mcpgateway.db import Tool as DbTool  # pylint: disable=import-outside-toplevel

            tool_count = 0
            if gateway_read.id:
                stmt = select(DbTool).where(DbTool.gateway_id == gateway_read.id)
                result = db.execute(stmt)
                tools = result.scalars().all()
                tool_count = len(tools)

            message = f"Successfully registered {gateway_read.name}"
            if tool_count > 0:
                message += f" with {tool_count} tools discovered"

            return CatalogServerRegisterResponse(success=True, server_id=str(gateway_read.id), message=message, error=None)

        except Exception as e:
            # First-Party
            from mcpgateway.services.gateway_service import GatewayDuplicateConflictError, GatewayNameConflictError  # pylint: disable=import-outside-toplevel

            # Handle user-recoverable conflicts explicitly so the generic mapper below
            # doesn't swallow them into a useless "Registration failed" message.
            if isinstance(e, GatewayNameConflictError):
                logger.info(f"Name conflict registering catalog server {catalog_id}: {e}")
                attempted_name = (request.name if request and request.name else None) or getattr(e, "name", "") or ""
                suggestion = f"{attempted_name} Work" if attempted_name else ""
                msg = (
                    (
                        f"A server named '{attempted_name}' already exists in your scope. "
                        f"Try a different name, e.g. '{suggestion}' or '{attempted_name} 2'. "
                        f"Names may contain letters, numbers, spaces, underscores, and hyphens."
                    )
                    if attempted_name
                    else ("A server with that name already exists in your scope. Try a different name.")
                )
                return CatalogServerRegisterResponse(success=False, server_id="", message=msg, error="name_conflict")

            if isinstance(e, GatewayDuplicateConflictError):
                logger.info(f"Duplicate gateway (same URL + credentials) for catalog server {catalog_id}: {e}")
                return CatalogServerRegisterResponse(
                    success=False,
                    server_id="",
                    message="A gateway with the same URL and credentials already exists in this scope. Use different credentials or choose a different visibility.",
                    error="duplicate_conflict",
                )

            logger.error(f"Failed to register catalog server {catalog_id}: {e}")

            # Map common exceptions to user-friendly messages
            error_str = str(e)
            user_message = "Registration failed"

            if "Connection refused" in error_str or "connect" in error_str.lower():
                user_message = "Server is offline or unreachable"
            elif "SSL" in error_str or "certificate" in error_str.lower():
                user_message = "SSL certificate verification failed - check server security settings"
            elif "timeout" in error_str.lower() or "timed out" in error_str.lower():
                user_message = "Server took too long to respond - it may be slow or unavailable"
            elif "401" in error_str or "Unauthorized" in error_str:
                user_message = "Authentication failed - check API key or OAuth credentials"
            elif "403" in error_str or "Forbidden" in error_str:
                user_message = "Access forbidden - check permissions and API key"
            elif "404" in error_str or "Not Found" in error_str:
                user_message = "Server endpoint not found - check URL is correct"
            elif "500" in error_str or "Internal Server Error" in error_str:
                user_message = "Remote server error - the MCP server is experiencing issues"
            elif "IPv6" in error_str:
                user_message = "IPv6 URLs are not supported - please use IPv4 or domain names"

            # Don't rollback here - let FastAPI handle it
            # db.rollback()
            return CatalogServerRegisterResponse(success=False, server_id="", message=user_message, error=error_str)

    async def check_server_availability(self, catalog_id: str) -> CatalogServerStatusResponse:
        """Check if a catalog server is available.

        Args:
            catalog_id: Catalog server ID

        Returns:
            Server status response
        """
        try:
            # Load catalog to find the server
            catalog_data = await self.load_catalog()
            servers = catalog_data.get("catalog_servers", [])

            # Find the server in catalog
            server_data = None
            for s in servers:
                if s.get("id") == catalog_id:
                    server_data = s
                    break

            if not server_data:
                return CatalogServerStatusResponse(server_id=catalog_id, is_available=False, is_registered=False, error="Server not found in catalog")

            # Check if registered (we'll need db passed in for this)
            is_registered = False

            # Perform health check
            start_time = time.time()
            is_available = False
            error = None

            try:
                async with httpx.AsyncClient(verify=not settings.skip_ssl_verify) as client:
                    # Try a simple GET request with short timeout
                    response = await client.get(server_data["url"], timeout=5.0, follow_redirects=True)
                    is_available = response.status_code < 500
            except Exception as e:
                error = str(e)
                is_available = False

            response_time_ms = (time.time() - start_time) * 1000

            return CatalogServerStatusResponse(
                server_id=catalog_id, is_available=is_available, is_registered=is_registered, last_checked=datetime.now(timezone.utc), response_time_ms=response_time_ms, error=error
            )

        except Exception as e:
            logger.error(f"Failed to check server status for {catalog_id}: {e}")
            return CatalogServerStatusResponse(server_id=catalog_id, is_available=False, is_registered=False, error=str(e))

    async def bulk_register_servers(self, request: CatalogBulkRegisterRequest, db: Session) -> CatalogBulkRegisterResponse:
        """Register multiple catalog servers.

        Args:
            request: Bulk registration request
            db: Database session

        Returns:
            Bulk registration response
        """
        successful = []
        failed = []

        for server_id in request.server_ids:
            try:
                response = await self.register_catalog_server(catalog_id=server_id, request=None, db=db)

                if response.success:
                    successful.append(server_id)
                else:
                    failed.append({"server_id": server_id, "error": response.error or "Registration failed"})

                    if not request.skip_errors:
                        break

            except Exception as e:
                failed.append({"server_id": server_id, "error": str(e)})

                if not request.skip_errors:
                    break

        return CatalogBulkRegisterResponse(successful=successful, failed=failed, total_attempted=len(request.server_ids), total_successful=len(successful))


# Global instance
catalog_service = CatalogService()
