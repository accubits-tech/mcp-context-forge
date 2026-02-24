# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/keycloak_role_mapping_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Keycloak Role Mapping Service.
Maps Keycloak JWT claims (realm roles, client roles, groups) to MCP Gateway
roles and teams on every login so that role changes in Keycloak propagate
automatically.

Examples:
    >>> from unittest.mock import Mock
    >>> service = KeycloakRoleMappingService(Mock())
    >>> isinstance(service, KeycloakRoleMappingService)
    True
"""

# Standard
import logging
from typing import List, Set

# Third-Party
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import EmailUser
from mcpgateway.utils.create_slug import slugify

logger = logging.getLogger(__name__)

# Realm roles that grant gateway admin (is_admin=True)
_ADMIN_REALM_ROLES: Set[str] = {
    "admin",
    "super_admin",
    "realm-admin",
    "gateway-admin",
}

# Keycloak internal roles that should not be mapped to teams
_SKIP_REALM_ROLES: Set[str] = _ADMIN_REALM_ROLES | {
    "offline_access",
    "uma_authorization",
}


class KeycloakRoleMappingService:
    """Maps Keycloak JWT claims to MCP Gateway roles and teams.

    Called on every Keycloak login (both new and returning users) so that
    role changes in Keycloak propagate to the gateway.

    Examples:
        >>> from unittest.mock import Mock
        >>> svc = KeycloakRoleMappingService(Mock())
        >>> isinstance(svc, KeycloakRoleMappingService)
        True
    """

    def __init__(self, db: Session):
        """Initialize with a database session.

        Args:
            db: SQLAlchemy database session
        """
        self.db = db

    async def sync_roles_from_jwt(self, user: EmailUser, jwt_payload: dict) -> None:
        """Synchronise MCP Gateway roles/teams from a Keycloak JWT.

        Steps:
        1. Set ``user.is_admin`` based on ``realm_access.roles`` containing ``"admin"``.
        2. Map realm roles to teams (if ``sso_keycloak_map_realm_roles`` is enabled).
        3. Map client roles to RBAC roles (if ``sso_keycloak_map_client_roles`` is enabled).
        4. Map groups to teams (if the groups claim is present).

        Args:
            user: The authenticated EmailUser record.
            jwt_payload: The decoded Keycloak JWT payload.
        """
        email = user.email
        realm_roles = self._extract_realm_roles(jwt_payload)

        # Step 1 — admin flag (always runs)
        await self._sync_admin_flag(user, realm_roles)

        # Step 2 — realm roles → teams
        if getattr(settings, "sso_keycloak_map_realm_roles", False):
            await self._sync_realm_roles_to_teams(email, realm_roles)

        # Step 3 — client roles → RBAC roles
        if getattr(settings, "sso_keycloak_map_client_roles", False):
            client_id = getattr(settings, "sso_keycloak_client_id", None)
            if client_id:
                client_roles = self._extract_client_roles(jwt_payload, client_id)
                await self._sync_client_roles(email, client_roles)

        # Step 4 — groups → teams
        groups_claim = getattr(settings, "sso_keycloak_groups_claim", "groups")
        groups: List[str] = jwt_payload.get(groups_claim, [])
        if groups and isinstance(groups, list):
            await self._sync_groups_to_teams(email, groups)

        # Step 5 — assign default "client" role to non-admin users
        if not user.is_admin:
            await self._assign_default_client_role(email)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_realm_roles(jwt_payload: dict) -> List[str]:
        """Extract realm roles from the JWT payload.

        Args:
            jwt_payload: Decoded JWT payload.

        Returns:
            List of realm role names.
        """
        realm_access = jwt_payload.get("realm_access", {})
        if isinstance(realm_access, dict):
            roles = realm_access.get("roles", [])
            if isinstance(roles, list):
                return roles
        return []

    @staticmethod
    def _extract_client_roles(jwt_payload: dict, client_id: str) -> List[str]:
        """Extract client roles for a specific client from the JWT.

        Args:
            jwt_payload: Decoded JWT payload.
            client_id: The Keycloak client ID.

        Returns:
            List of client role names.
        """
        resource_access = jwt_payload.get("resource_access", {})
        if isinstance(resource_access, dict):
            client_section = resource_access.get(client_id, {})
            if isinstance(client_section, dict):
                roles = client_section.get("roles", [])
                if isinstance(roles, list):
                    return roles
        return []

    async def _sync_admin_flag(self, user: EmailUser, realm_roles: List[str]) -> None:
        """Set or clear the admin flag based on realm roles.

        Checks for any role in ``_ADMIN_REALM_ROLES`` (admin, super_admin,
        realm-admin, gateway-admin).

        Args:
            user: The EmailUser to update.
            realm_roles: List of realm role names.
        """
        try:
            is_admin = bool(_ADMIN_REALM_ROLES.intersection(realm_roles))
            if user.is_admin != is_admin:
                user.is_admin = is_admin
                self.db.commit()
                logger.info("Keycloak role sync: set is_admin=%s for %s", is_admin, user.email)
        except Exception as exc:
            logger.warning("Keycloak role sync: failed to update admin flag for %s: %s", user.email, exc)

    async def _sync_realm_roles_to_teams(self, email: str, realm_roles: List[str]) -> None:
        """Map realm roles to gateway teams (find-or-create + add member).

        Args:
            email: User email address.
            realm_roles: List of realm role names.
        """
        # First-Party
        from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

        team_service = TeamManagementService(self.db)

        for role_name in realm_roles:
            if role_name in _SKIP_REALM_ROLES or role_name.startswith("default-roles-"):
                continue
            try:
                slug = slugify(role_name)
                team = await team_service.get_team_by_slug(slug)
                if not team:
                    team = await team_service.create_team(
                        name=role_name,
                        description=f"Auto-created from Keycloak realm role: {role_name}",
                        created_by=email,
                        visibility="public",
                    )
                    logger.info("Keycloak role sync: created team '%s' (slug=%s) for %s", role_name, slug, email)
                await team_service.add_member_to_team(team_id=team.id, user_email=email, role="member", invited_by=email)
            except Exception as exc:
                logger.warning("Keycloak role sync: failed to map realm role '%s' for %s: %s", role_name, email, exc)

    async def _sync_client_roles(self, email: str, client_roles: List[str]) -> None:
        """Map client roles to pre-existing gateway RBAC roles.

        Args:
            email: User email address.
            client_roles: List of client role names.
        """
        # First-Party
        from mcpgateway.services.role_service import RoleService  # pylint: disable=import-outside-toplevel

        role_service = RoleService(self.db)

        for role_name in client_roles:
            try:
                # Try team scope first, then global
                role = await role_service.get_role_by_name(role_name, "team")
                if not role:
                    role = await role_service.get_role_by_name(role_name, "global")
                if not role:
                    logger.debug("Keycloak role sync: no matching RBAC role '%s' — skipping", role_name)
                    continue
                try:
                    await role_service.assign_role_to_user(
                        user_email=email,
                        role_id=role.id,
                        scope=role.scope,
                        scope_id=None,
                        granted_by=email,
                    )
                    logger.info("Keycloak role sync: assigned RBAC role '%s' to %s", role_name, email)
                except ValueError:
                    # Already assigned — this is fine
                    logger.debug("Keycloak role sync: RBAC role '%s' already assigned to %s", role_name, email)
            except Exception as exc:
                logger.warning("Keycloak role sync: failed to map client role '%s' for %s: %s", role_name, email, exc)

    async def _sync_groups_to_teams(self, email: str, groups: List[str]) -> None:
        """Map Keycloak group paths to gateway teams.

        Group paths like ``"/org/team-beta"`` are mapped using the last
        path segment as the team name.

        Args:
            email: User email address.
            groups: List of group path strings.
        """
        # First-Party
        from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

        team_service = TeamManagementService(self.db)

        for group_path in groups:
            try:
                if not isinstance(group_path, str) or not group_path.strip():
                    continue
                # Extract last path segment: "/org/team-beta" → "team-beta"
                team_name = group_path.rstrip("/").rsplit("/", 1)[-1]
                if not team_name:
                    continue
                slug = slugify(team_name)
                team = await team_service.get_team_by_slug(slug)
                if not team:
                    team = await team_service.create_team(
                        name=team_name,
                        description=f"Auto-created from Keycloak group: {group_path}",
                        created_by=email,
                        visibility="public",
                    )
                    logger.info("Keycloak role sync: created team '%s' from group '%s' for %s", team_name, group_path, email)
                await team_service.add_member_to_team(team_id=team.id, user_email=email, role="member", invited_by=email)
            except Exception as exc:
                logger.warning("Keycloak role sync: failed to map group '%s' for %s: %s", group_path, email, exc)

    async def _assign_default_client_role(self, email: str) -> None:
        """Assign the global ``client`` RBAC role to non-admin Keycloak users.

        The role must already exist (created by ``bootstrap_default_roles``).
        If the user already has the assignment, the duplicate is silently
        ignored.

        Args:
            email: User email address.
        """
        # First-Party
        from mcpgateway.services.role_service import RoleService  # pylint: disable=import-outside-toplevel

        role_service = RoleService(self.db)

        try:
            role = await role_service.get_role_by_name("client", "global")
            if not role:
                logger.debug("Keycloak role sync: 'client' role not found — skipping default assignment for %s", email)
                return
            try:
                await role_service.assign_role_to_user(
                    user_email=email,
                    role_id=role.id,
                    scope="global",
                    scope_id=None,
                    granted_by=email,
                )
                logger.info("Keycloak role sync: assigned default 'client' role to %s", email)
            except ValueError:
                # Already assigned — this is fine
                logger.debug("Keycloak role sync: 'client' role already assigned to %s", email)
        except Exception as exc:
            logger.warning("Keycloak role sync: failed to assign default 'client' role for %s: %s", email, exc)
