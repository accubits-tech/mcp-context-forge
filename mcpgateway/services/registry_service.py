# -*- coding: utf-8 -*-
"""Registry Service for publishing, deploying, and managing registry entries.

This module provides the RegistryService class that handles:
- Publishing servers to the registry (snapshotting tool definitions)
- Deploying servers from registry entries (re-creating server + tools)
- Unpublishing (soft-deleting) registry entries
- Listing active registry entries
"""

# Standard
import logging
from typing import List, Optional

# Third-Party
from sqlalchemy import select
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import RegistryEntry, Server, server_tool_association, Tool
from mcpgateway.schemas import (
    RegistryEntryDeploy,
    RegistryEntryPublish,
    RegistryEntryRead,
    ServerCreate,
    ServerRead,
    ToolCreate,
)
from mcpgateway.services.server_service import ServerService
from mcpgateway.services.tool_service import ToolService

logger = logging.getLogger(__name__)


class RegistryService:
    """Service for managing registry entries."""

    def __init__(self):
        self.tool_service = ToolService()
        self.server_service = ServerService()

    def _snapshot_tool(self, tool: Tool) -> dict:
        """Create a serializable snapshot of a tool definition."""
        return {
            "name": tool.original_name,
            "url": tool.url,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "output_schema": tool.output_schema,
            "request_type": tool.request_type,
            "integration_type": tool.integration_type,
            "headers": tool.headers,
            "auth_type": tool.auth_type,
            "tags": tool.tags or [],
            "annotations": tool.annotations,
            "jsonpath_filter": tool.jsonpath_filter,
        }

    async def publish_server(
        self,
        request: RegistryEntryPublish,
        user_email: str,
        db: Session,
    ) -> RegistryEntryRead:
        """Publish a server to the registry.

        Args:
            request: Publish request containing server_id and optional category.
            user_email: Email of the publishing user.
            db: Database session.

        Returns:
            The created registry entry.
        """
        # Load server
        server = db.get(Server, request.server_id)
        if not server:
            raise ValueError(f"Server {request.server_id} not found")

        # Load associated tools via the association table
        stmt = (
            select(Tool)
            .join(server_tool_association, Tool.id == server_tool_association.c.tool_id)
            .where(server_tool_association.c.server_id == server.id)
        )
        tools = db.execute(stmt).scalars().all()

        # Snapshot tool definitions
        tool_defs = [self._snapshot_tool(t) for t in tools]

        # Create registry entry
        entry = RegistryEntry(
            name=server.name,
            description=server.description,
            category=request.category or "Virtual Server",
            tags=server.tags or [],
            icon=server.icon,
            tool_definitions=tool_defs,
            tool_count=len(tool_defs),
            server_transport=server.transport or "sse",
            published_by=user_email,
            source_server_id=server.id,
            source_type=server.created_via,
            team_id=server.team_id,
            visibility=server.visibility or "public",
        )
        db.add(entry)

        # Link server to registry entry
        server.registry_entry_id = entry.id
        db.commit()
        db.refresh(entry)

        return RegistryEntryRead(
            id=entry.id,
            name=entry.name,
            description=entry.description,
            category=entry.category,
            tags=entry.tags or [],
            tool_count=entry.tool_count,
            published_by=entry.published_by,
            published_at=entry.published_at,
            source_server_id=entry.source_server_id,
            source_type=entry.source_type,
            is_active=entry.is_active,
            visibility=entry.visibility,
        )

    async def deploy_entry(
        self,
        entry_id: str,
        request: RegistryEntryDeploy,
        user_email: str,
        db: Session,
    ) -> ServerRead:
        """Deploy a server from a registry entry.

        Creates new tools from the snapshot and a new server composing them.

        Args:
            entry_id: Registry entry ID to deploy.
            request: Deploy options (name, description, visibility, team_id).
            user_email: Email of the deploying user.
            db: Database session.

        Returns:
            The newly created server.
        """
        entry = db.get(RegistryEntry, entry_id)
        if not entry or not entry.is_active:
            raise ValueError(f"Registry entry {entry_id} not found or inactive")

        # Create tools from snapshot
        tool_ids: List[str] = []
        for tool_def in entry.tool_definitions:
            try:
                tool_create = ToolCreate(**tool_def)
                db_tool = await self.tool_service.register_tool(
                    db,
                    tool_create,
                    created_by=user_email,
                    created_via="registry_deploy",
                )
                tool_ids.append(db_tool.id)
            except Exception as ex:
                logger.warning(f"Failed to create tool from registry snapshot: {ex}")
                continue

        if not tool_ids:
            raise ValueError("No tools could be created from the registry entry")

        # Create server
        server_name = request.name or entry.name
        server_desc = request.description or entry.description or ""
        server_create = ServerCreate(
            name=server_name,
            description=server_desc,
            associated_tools=tool_ids,
            tags=entry.tags or [],
            transport=entry.server_transport or "sse",
        )

        server_read = await self.server_service.register_server(
            db,
            server_create,
            created_by=user_email,
            created_via="registry_deploy",
            team_id=request.team_id,
            owner_email=user_email,
            visibility=request.visibility or "private",
        )

        return server_read

    async def unpublish_entry(
        self,
        entry_id: str,
        user_email: str,
        db: Session,
    ) -> dict:
        """Unpublish a registry entry (soft delete).

        Args:
            entry_id: Registry entry ID to unpublish.
            user_email: Email of the requesting user.
            db: Database session.

        Returns:
            Success status dict.
        """
        entry = db.get(RegistryEntry, entry_id)
        if not entry:
            raise ValueError(f"Registry entry {entry_id} not found")

        if entry.published_by != user_email:
            raise PermissionError("Only the publisher can unpublish a registry entry")

        entry.is_active = False

        # Clear registry link on the source server if it still exists
        if entry.source_server_id:
            server = db.get(Server, entry.source_server_id)
            if server and server.registry_entry_id == entry.id:
                server.registry_entry_id = None

        db.commit()

        return {"success": True, "message": f"Registry entry '{entry.name}' has been unpublished"}

    async def list_entries(
        self,
        db: Session,
        search: Optional[str] = None,
        category: Optional[str] = None,
        visibility: Optional[str] = None,
    ) -> List[RegistryEntryRead]:
        """List active registry entries with optional filters.

        Args:
            db: Database session.
            search: Search term for name/description.
            category: Filter by category.
            visibility: Filter by visibility.

        Returns:
            List of active registry entries.
        """
        stmt = select(RegistryEntry).where(RegistryEntry.is_active.is_(True))

        entries = db.execute(stmt).scalars().all()

        results = []
        for entry in entries:
            # Apply filters in Python (small dataset, mirrors catalog_service pattern)
            if search:
                search_lower = search.lower()
                if search_lower not in (entry.name or "").lower() and search_lower not in (entry.description or "").lower():
                    continue
            if category and entry.category != category:
                continue
            if visibility and entry.visibility != visibility:
                continue

            results.append(
                RegistryEntryRead(
                    id=entry.id,
                    name=entry.name,
                    description=entry.description,
                    category=entry.category,
                    tags=entry.tags or [],
                    tool_count=entry.tool_count,
                    published_by=entry.published_by,
                    published_at=entry.published_at,
                    source_server_id=entry.source_server_id,
                    source_type=entry.source_type,
                    is_active=entry.is_active,
                    visibility=entry.visibility,
                )
            )

        return results
