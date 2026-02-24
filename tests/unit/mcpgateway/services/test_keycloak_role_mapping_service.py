# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/services/test_keycloak_role_mapping_service.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Tests for the Keycloak Role Mapping Service.
"""

# Standard
from unittest.mock import AsyncMock, MagicMock, patch

# Third-Party
import pytest
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import EmailUser
from mcpgateway.services.keycloak_role_mapping_service import KeycloakRoleMappingService

# Patch targets — these are lazy-imported inside service methods
_TMS_TARGET = "mcpgateway.services.team_management_service.TeamManagementService"
_RS_TARGET = "mcpgateway.services.role_service.RoleService"
_SETTINGS_TARGET = "mcpgateway.services.keycloak_role_mapping_service.settings"


def _make_user(email: str = "kc@example.com", is_admin: bool = False) -> MagicMock:
    """Create a mock EmailUser."""
    user = MagicMock(spec=EmailUser)
    user.email = email
    user.is_admin = is_admin
    return user


def _make_team(team_id: str = "team-123", slug: str = "dev") -> MagicMock:
    """Create a mock team object."""
    team = MagicMock()
    team.id = team_id
    team.slug = slug
    return team


def _make_role(role_id: str = "role-abc", name: str = "viewer", scope: str = "team") -> MagicMock:
    """Create a mock role object."""
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.scope = scope
    return role


@pytest.fixture
def mock_db():
    return MagicMock(spec=Session)


@pytest.fixture
def service(mock_db):
    return KeycloakRoleMappingService(mock_db)


# ---------------------------------------------------------------
# Step 1 — Admin flag
# ---------------------------------------------------------------


class TestAdminFlag:
    """Tests for admin flag synchronisation."""

    @pytest.mark.asyncio
    async def test_admin_realm_role_sets_is_admin(self, service, mock_db):
        """'admin' in realm roles sets is_admin=True."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": ["admin", "developer"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            await service.sync_roles_from_jwt(user, payload)

        assert user.is_admin is True
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_no_admin_role_clears_is_admin(self, service, mock_db):
        """Missing 'admin' sets is_admin=False."""
        user = _make_user(is_admin=True)
        payload = {"realm_access": {"roles": ["developer"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            await service.sync_roles_from_jwt(user, payload)

        assert user.is_admin is False
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_super_admin_realm_role_sets_is_admin(self, service, mock_db):
        """'super_admin' in realm roles sets is_admin=True."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": ["super_admin", "developer"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            await service.sync_roles_from_jwt(user, payload)

        assert user.is_admin is True
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_admin_flag_no_change_skips_commit(self, service, mock_db):
        """No commit when admin flag is already correct."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": ["developer"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            await service.sync_roles_from_jwt(user, payload)

        # is_admin was already False, no commit needed
        mock_db.commit.assert_not_called()


# ---------------------------------------------------------------
# Step 2 — Realm roles → teams
# ---------------------------------------------------------------


class TestRealmRolesToTeams:
    """Tests for mapping realm roles to gateway teams."""

    @pytest.mark.asyncio
    async def test_realm_role_creates_team_and_adds_member(self, service):
        """A new realm role creates the team and adds the user."""
        user = _make_user()
        payload = {"realm_access": {"roles": ["developer"]}}
        team = _make_team()

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = True
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock(return_value=None)
                mock_tms.create_team = AsyncMock(return_value=team)
                mock_tms.add_member_to_team = AsyncMock(return_value=True)
                mock_tms_class.return_value = mock_tms

                await service.sync_roles_from_jwt(user, payload)

                mock_tms.create_team.assert_called_once()
                mock_tms.add_member_to_team.assert_called_once_with(
                    team_id=team.id,
                    user_email=user.email,
                    role="member",
                    invited_by=user.email,
                )

    @pytest.mark.asyncio
    async def test_realm_role_existing_team_adds_member(self, service):
        """An existing team is reused — no creation."""
        user = _make_user()
        payload = {"realm_access": {"roles": ["developer"]}}
        team = _make_team()

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = True
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock(return_value=team)
                mock_tms.create_team = AsyncMock()
                mock_tms.add_member_to_team = AsyncMock(return_value=True)
                mock_tms_class.return_value = mock_tms

                await service.sync_roles_from_jwt(user, payload)

                mock_tms.create_team.assert_not_called()
                mock_tms.add_member_to_team.assert_called_once()

    @pytest.mark.asyncio
    async def test_realm_role_skips_internal_roles(self, service):
        """offline_access, uma_authorization, default-roles-*, super_admin are skipped."""
        user = _make_user()
        payload = {"realm_access": {"roles": ["offline_access", "uma_authorization", "default-roles-myrealm", "super_admin"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = True
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock()
                mock_tms.create_team = AsyncMock()
                mock_tms.add_member_to_team = AsyncMock()
                mock_tms_class.return_value = mock_tms

                await service.sync_roles_from_jwt(user, payload)

                mock_tms.get_team_by_slug.assert_not_called()
                mock_tms.create_team.assert_not_called()

    @pytest.mark.asyncio
    async def test_realm_role_skips_admin(self, service):
        """'admin' is not created as a team."""
        user = _make_user()
        payload = {"realm_access": {"roles": ["admin"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = True
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock()
                mock_tms_class.return_value = mock_tms

                await service.sync_roles_from_jwt(user, payload)

                mock_tms.get_team_by_slug.assert_not_called()


# ---------------------------------------------------------------
# Step 3 — Client roles → RBAC roles
# ---------------------------------------------------------------


class TestClientRolesToRBAC:
    """Tests for mapping client roles to gateway RBAC roles."""

    @pytest.mark.asyncio
    async def test_client_role_assigns_matching_gateway_role(self, service):
        """A matching RBAC role is assigned to the user."""
        user = _make_user()
        role = _make_role()
        payload = {
            "realm_access": {"roles": []},
            "resource_access": {"my-client": {"roles": ["viewer"]}},
        }

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = True
            mock_settings.sso_keycloak_client_id = "my-client"
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()

                async def _get_role(name, scope):
                    if name == "viewer" and scope == "team":
                        return role
                    return None

                mock_rs.get_role_by_name = AsyncMock(side_effect=_get_role)
                mock_rs.assign_role_to_user = AsyncMock()
                mock_rs_class.return_value = mock_rs

                await service.sync_roles_from_jwt(user, payload)

                mock_rs.assign_role_to_user.assert_called_once_with(
                    user_email=user.email,
                    role_id=role.id,
                    scope=role.scope,
                    scope_id=None,
                    granted_by=user.email,
                )

    @pytest.mark.asyncio
    async def test_client_role_skips_unknown(self, service):
        """Unrecognised client role is skipped gracefully."""
        user = _make_user()
        payload = {
            "realm_access": {"roles": []},
            "resource_access": {"my-client": {"roles": ["nonexistent"]}},
        }

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = True
            mock_settings.sso_keycloak_client_id = "my-client"
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs.get_role_by_name = AsyncMock(return_value=None)
                mock_rs.assign_role_to_user = AsyncMock()
                mock_rs_class.return_value = mock_rs

                await service.sync_roles_from_jwt(user, payload)

                mock_rs.assign_role_to_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_role_duplicate_assignment_handled(self, service):
        """ValueError from duplicate assignment is caught."""
        user = _make_user()
        role = _make_role()
        payload = {
            "realm_access": {"roles": []},
            "resource_access": {"my-client": {"roles": ["viewer"]}},
        }

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = True
            mock_settings.sso_keycloak_client_id = "my-client"
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs.get_role_by_name = AsyncMock(return_value=role)
                mock_rs.assign_role_to_user = AsyncMock(side_effect=ValueError("already assigned"))
                mock_rs_class.return_value = mock_rs

                # Should not raise
                await service.sync_roles_from_jwt(user, payload)


# ---------------------------------------------------------------
# Step 4 — Groups → teams
# ---------------------------------------------------------------


class TestGroupsToTeams:
    """Tests for mapping Keycloak groups to gateway teams."""

    @pytest.mark.asyncio
    async def test_groups_creates_teams(self, service):
        """Group paths create teams using the last path segment."""
        user = _make_user()
        payload = {
            "realm_access": {"roles": []},
            "groups": ["/org/team-alpha"],
        }
        team = _make_team(slug="team-alpha")

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock(return_value=None)
                mock_tms.create_team = AsyncMock(return_value=team)
                mock_tms.add_member_to_team = AsyncMock(return_value=True)
                mock_tms_class.return_value = mock_tms

                await service.sync_roles_from_jwt(user, payload)

                mock_tms.create_team.assert_called_once()
                call_kwargs = mock_tms.create_team.call_args.kwargs
                assert call_kwargs["name"] == "team-alpha"
                mock_tms.add_member_to_team.assert_called_once()

    @pytest.mark.asyncio
    async def test_groups_existing_team_adds_member(self, service):
        """Existing team is reused when group maps to it."""
        user = _make_user()
        payload = {
            "realm_access": {"roles": []},
            "groups": ["/team-beta"],
        }
        team = _make_team(slug="team-beta")

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock(return_value=team)
                mock_tms.create_team = AsyncMock()
                mock_tms.add_member_to_team = AsyncMock(return_value=True)
                mock_tms_class.return_value = mock_tms

                await service.sync_roles_from_jwt(user, payload)

                mock_tms.create_team.assert_not_called()
                mock_tms.add_member_to_team.assert_called_once()


# ---------------------------------------------------------------
# Config toggles & error handling
# ---------------------------------------------------------------


class TestConfigToggles:
    """Tests for configuration toggles."""

    @pytest.mark.asyncio
    async def test_disabled_realm_roles_skips_team_sync(self, service):
        """Disabled sso_keycloak_map_realm_roles skips team mapping."""
        user = _make_user()
        payload = {"realm_access": {"roles": ["developer"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                await service.sync_roles_from_jwt(user, payload)
                mock_tms_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_disabled_client_roles_skips_rbac_sync(self, service):
        """Disabled sso_keycloak_map_client_roles skips RBAC mapping."""
        user = _make_user()
        payload = {
            "realm_access": {"roles": []},
            "resource_access": {"my-client": {"roles": ["viewer"]}},
        }

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs.get_role_by_name = AsyncMock(return_value=None)
                mock_rs_class.return_value = mock_rs

                await service.sync_roles_from_jwt(user, payload)

                # Step 3 skipped; Step 5 ran but found no client role
                mock_rs.get_role_by_name.assert_called_once_with("client", "global")
                mock_rs.assign_role_to_user.assert_not_called()


class TestErrorHandling:
    """Tests for error resilience."""

    @pytest.mark.asyncio
    async def test_all_errors_fail_soft(self, service):
        """Exceptions in individual steps don't propagate."""
        user = _make_user()
        payload = {
            "realm_access": {"roles": ["developer"]},
            "resource_access": {"my-client": {"roles": ["viewer"]}},
            "groups": ["/team-gamma"],
        }

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = True
            mock_settings.sso_keycloak_map_client_roles = True
            mock_settings.sso_keycloak_client_id = "my-client"
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_TMS_TARGET) as mock_tms_class:
                mock_tms = MagicMock()
                mock_tms.get_team_by_slug = AsyncMock(side_effect=RuntimeError("db error"))
                mock_tms_class.return_value = mock_tms

                with patch(_RS_TARGET) as mock_rs_class:
                    mock_rs = MagicMock()
                    mock_rs.get_role_by_name = AsyncMock(side_effect=RuntimeError("db error"))
                    mock_rs_class.return_value = mock_rs

                    # Should not raise despite all steps failing
                    await service.sync_roles_from_jwt(user, payload)

    @pytest.mark.asyncio
    async def test_empty_payload_succeeds(self, service):
        """An empty JWT payload completes without error."""
        user = _make_user()
        payload = {}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            await service.sync_roles_from_jwt(user, payload)

    @pytest.mark.asyncio
    async def test_malformed_realm_access_handled(self, service):
        """Malformed realm_access doesn't crash."""
        user = _make_user()
        payload = {"realm_access": "not-a-dict"}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            # Should not raise
            await service.sync_roles_from_jwt(user, payload)


# ---------------------------------------------------------------
# Step 5 — Default client role assignment
# ---------------------------------------------------------------


class TestDefaultClientRole:
    """Tests for automatic 'client' role assignment to non-admin users."""

    @pytest.mark.asyncio
    async def test_non_admin_gets_client_role_assigned(self, service):
        """Non-admin Keycloak user gets the 'client' role assigned."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": []}}
        client_role = _make_role(role_id="client-role-id", name="client", scope="global")

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs.get_role_by_name = AsyncMock(return_value=client_role)
                mock_rs.assign_role_to_user = AsyncMock()
                mock_rs_class.return_value = mock_rs

                await service.sync_roles_from_jwt(user, payload)

                mock_rs.get_role_by_name.assert_called_once_with("client", "global")
                mock_rs.assign_role_to_user.assert_called_once_with(
                    user_email=user.email,
                    role_id=client_role.id,
                    scope="global",
                    scope_id=None,
                    granted_by=user.email,
                )

    @pytest.mark.asyncio
    async def test_admin_skips_client_role_assignment(self, service):
        """Admin users don't get the default client role."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": ["admin"]}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs_class.return_value = mock_rs

                await service.sync_roles_from_jwt(user, payload)

                # User became admin in Step 1, so Step 5 is skipped
                mock_rs.get_role_by_name.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_role_not_found_skips_gracefully(self, service):
        """Missing client role (not bootstrapped) doesn't cause errors."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": []}}

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs.get_role_by_name = AsyncMock(return_value=None)
                mock_rs_class.return_value = mock_rs

                await service.sync_roles_from_jwt(user, payload)

                mock_rs.assign_role_to_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_client_role_already_assigned(self, service):
        """Duplicate client role assignment is handled gracefully."""
        user = _make_user(is_admin=False)
        payload = {"realm_access": {"roles": []}}
        client_role = _make_role(role_id="client-role-id", name="client", scope="global")

        with patch(_SETTINGS_TARGET) as mock_settings:
            mock_settings.sso_keycloak_map_realm_roles = False
            mock_settings.sso_keycloak_map_client_roles = False
            mock_settings.sso_keycloak_groups_claim = "groups"

            with patch(_RS_TARGET) as mock_rs_class:
                mock_rs = MagicMock()
                mock_rs.get_role_by_name = AsyncMock(return_value=client_role)
                mock_rs.assign_role_to_user = AsyncMock(side_effect=ValueError("already assigned"))
                mock_rs_class.return_value = mock_rs

                # Should not raise
                await service.sync_roles_from_jwt(user, payload)
