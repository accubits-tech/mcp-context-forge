# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/skill_service.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0

Skill Service Implementation.

Implements Agent Skills management for MCP Foundry. Skills are served to MCP
clients as resources under the ``skill://`` URI scheme, per the Skills Extension
(``io.modelcontextprotocol/skills``) drafted in SEP-2640. The SKILL.md document
format itself is delegated to the agentskills.io specification.

Responsibilities:
- CRUD on skills with team/visibility scoping (matches Prompt service behavior).
- Parse and serialize SKILL.md text (YAML frontmatter + markdown body).
- Emit skill resource entries for MCP ``resources/list`` responses, including
  the well-known ``skill://index.json`` discovery resource.
- Serve skill content for MCP ``resources/read`` calls on ``skill://`` URIs.
- Validate references to tools and gateways at save time (advisory — runtime
  enforcement is out of scope for v1 per product decision).
"""

# Standard
from datetime import timezone
from typing import Any, Dict, List, Optional, Tuple, Union

# Third-Party
import frontmatter
import yaml
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.db import EmailTeam, Gateway
from mcpgateway.db import Skill as DbSkill
from mcpgateway.db import server_skill_association
from mcpgateway.schemas import SkillCreate, SkillFileExport, SkillRead, SkillUpdate
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.utils.pagination import decode_cursor, encode_cursor

logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


# ---------------------------------------------------------------------------
# SEP-2640 and agentskills.io constants. Keeping these at module scope lets
# callers (the MCP dispatcher, admin routes) share one source of truth.
# ---------------------------------------------------------------------------
SKILLS_EXTENSION_ID = "io.modelcontextprotocol/skills"
# Reverse-DNS namespace for Foundry-only fields we surface via MCP resource `_meta`.
FOUNDRY_META_NAMESPACE = "io.hybrid360.foundry"
# Reserved URI for the per-server discovery index (cannot collide with any skill
# name because skill names cannot contain '.').
SKILL_INDEX_URI = "skill://index.json"
# agentskills.io discovery-index schema — bump here when the spec version changes.
SKILLS_INDEX_SCHEMA_URL = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"
# agentskills.io spec-core frontmatter keys. These (and *only* these) are emitted
# to SKILL.md on serve/export. Foundry extras go to resource `_meta`.
SPEC_FRONTMATTER_KEYS = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
# The order we want SKILL.md frontmatter to appear in when exported, for reader
# ergonomics (name first, body-critical fields before license boilerplate).
FRONTMATTER_EMIT_ORDER = ("name", "description", "allowed-tools", "license", "compatibility", "metadata")


class SkillError(Exception):
    """Base class for skill-related errors."""


class SkillNotFoundError(SkillError):
    """Raised when a requested skill is not found."""


class SkillNameConflictError(SkillError):
    """Raised when a skill name/path conflicts with an existing skill in the same scope."""

    def __init__(self, skill_path: str, *, is_active: bool = True, skill_id: Optional[int] = None, visibility: str = "public") -> None:
        """Capture the fields callers commonly want to display.

        Args:
            skill_path: The skill_path that collided.
            is_active: Whether the existing skill is active.
            skill_id: Existing skill id for UI linking.
            visibility: Visibility of the existing skill.
        """
        self.skill_path = skill_path
        self.is_active = is_active
        self.skill_id = skill_id
        super().__init__(f"{visibility.capitalize()} Skill already exists with path: {skill_path}")


class SkillValidationError(SkillError):
    """Raised when skill validation fails (bad frontmatter, bad references, etc.)."""


def _yaml_ordered_dump(data: Dict[str, Any]) -> str:
    """Serialize a dict to YAML with the Foundry-preferred key order.

    PyYAML sorts keys alphabetically by default, which puts ``allowed-tools``
    ahead of ``description`` in skill files — confusing for readers. We emit
    known keys in ``FRONTMATTER_EMIT_ORDER`` first, then anything else.

    Args:
        data: Dict of already-validated spec frontmatter fields.

    Returns:
        str: YAML document (no leading/trailing ``---`` markers).
    """
    ordered: List[Tuple[str, Any]] = []
    seen = set()
    for k in FRONTMATTER_EMIT_ORDER:
        if k in data:
            ordered.append((k, data[k]))
            seen.add(k)
    for k, v in data.items():
        if k not in seen:
            ordered.append((k, v))
    return yaml.safe_dump(dict(ordered), sort_keys=False, allow_unicode=True).strip() + "\n"


def serialize_skill_md(skill: DbSkill) -> str:
    """Reconstruct the full SKILL.md document (frontmatter + body) from a DbSkill.

    Only spec-core frontmatter fields are emitted; Foundry-specific fields are
    NOT serialized into the file so an exported SKILL.md stays portable.

    Args:
        skill: The ORM skill row.

    Returns:
        str: Full SKILL.md text suitable for download or MCP ``resources/read`` response.
    """
    fm: Dict[str, Any] = {
        "name": skill.name,
        "description": skill.description,
    }
    if skill.license:
        fm["license"] = skill.license
    if skill.compatibility:
        fm["compatibility"] = skill.compatibility
    if skill.metadata_json:
        fm["metadata"] = skill.metadata_json
    if skill.allowed_tools:
        fm["allowed-tools"] = skill.allowed_tools
    # Build the file manually — python-frontmatter's default dumper re-sorts keys,
    # which is the thing we're specifically overriding here.
    body = skill.content_md or ""
    return f"---\n{_yaml_ordered_dump(fm)}---\n\n{body}"


def parse_skill_md(text: str) -> Dict[str, Any]:
    """Parse a SKILL.md text into a dict suitable for populating a SkillCreate/Update.

    Unknown frontmatter keys are silently dropped. The caller decides whether a
    missing ``name`` or ``description`` is fatal (SkillCreate will reject it).

    Args:
        text: Raw SKILL.md file content.

    Returns:
        dict: ``{name, description, license, compatibility, metadata_json,
        allowed_tools, content_md}`` — keys map 1:1 to SkillCreate fields
        (frontmatter ``metadata`` → ``metadata_json``, ``allowed-tools`` →
        ``allowed_tools``).
    """
    post = frontmatter.loads(text)
    fm = dict(post.metadata)
    result: Dict[str, Any] = {
        "name": fm.get("name"),
        "description": fm.get("description"),
        "license": fm.get("license"),
        "compatibility": fm.get("compatibility"),
        "metadata_json": fm.get("metadata") or {},
        "allowed_tools": fm.get("allowed-tools"),
        "content_md": post.content,
    }
    return result


def build_skill_uri(skill_path: str, file_path: str = "SKILL.md") -> str:
    """Construct a ``skill://`` URI for a skill file.

    Args:
        skill_path: The skill's URI locator (e.g. ``code-review`` or ``acme/billing/refunds``).
        file_path: File path relative to the skill root. Defaults to ``SKILL.md``.

    Returns:
        str: Fully-formed ``skill://<skill_path>/<file_path>`` URI.
    """
    return f"skill://{skill_path}/{file_path}"


def parse_skill_uri(uri: str) -> Optional[Tuple[str, str]]:
    """Parse a ``skill://`` URI back into ``(skill_path, file_path)``.

    Per SEP-2640, the authority component carries no special meaning and is
    treated as the first segment of ``skill_path``. The ``skill://index.json``
    well-known URI is handled by the caller before this function is invoked.

    Args:
        uri: A URI of the form ``skill://<skill_path>/<file_path>``.

    Returns:
        tuple | None: ``(skill_path, file_path)`` if the URI parses, else ``None``.
    """
    if not uri.startswith("skill://"):
        return None
    rest = uri[len("skill://") :]
    if not rest or "/" not in rest:
        return None
    skill_path, _, file_path = rest.rpartition("/")
    if not skill_path or not file_path:
        return None
    return skill_path, file_path


class SkillService:
    """Service for managing Agent Skills in MCP Foundry.

    CRUD surface mirrors :class:`PromptService` for consistency. Additional
    methods build MCP-protocol shaped responses (``resources/list`` entries,
    ``resources/read`` payloads, and the ``skill://index.json`` discovery doc).
    """

    # -- lifecycle --------------------------------------------------------

    async def initialize(self) -> None:
        """No-op initializer kept for symmetry with other services."""
        logger.info("Initializing skill service")

    async def shutdown(self) -> None:
        """No-op shutdown kept for symmetry with other services."""
        logger.info("Skill service shutdown complete")

    # -- helpers ---------------------------------------------------------

    def _get_team_name(self, db: Session, team_id: Optional[str]) -> Optional[str]:
        """Look up a team name for inclusion in SkillRead responses.

        Args:
            db: SQLAlchemy session.
            team_id: Team id from the skill row.

        Returns:
            str | None: Team display name, or None if the team id is missing or inactive.
        """
        if not team_id:
            return None
        team = db.query(EmailTeam).filter(EmailTeam.id == team_id, EmailTeam.is_active.is_(True)).first()
        return team.name if team else None

    def _convert_db_skill(self, db_skill: DbSkill, team_name: Optional[str] = None) -> Dict[str, Any]:
        """Project a DbSkill into a dict matching :class:`SkillRead`.

        Args:
            db_skill: ORM row.
            team_name: Optional resolved team name (looked up once by caller).

        Returns:
            dict: Field map consumed by ``SkillRead.model_validate``.
        """
        return {
            "id": db_skill.id,
            "name": db_skill.name,
            "skill_path": db_skill.skill_path,
            "description": db_skill.description,
            "content_md": db_skill.content_md or "",
            "license": db_skill.license,
            "compatibility": db_skill.compatibility,
            "metadata_json": db_skill.metadata_json or {},
            "allowed_tools": db_skill.allowed_tools,
            "allowed_gateway_ids": db_skill.allowed_gateway_ids or [],
            "created_at": db_skill.created_at,
            "updated_at": db_skill.updated_at,
            "is_active": db_skill.is_active,
            "tags": db_skill.tags or [],
            "created_by": db_skill.created_by,
            "created_from_ip": db_skill.created_from_ip,
            "created_via": db_skill.created_via,
            "created_user_agent": db_skill.created_user_agent,
            "modified_by": db_skill.modified_by,
            "modified_from_ip": db_skill.modified_from_ip,
            "modified_via": db_skill.modified_via,
            "modified_user_agent": db_skill.modified_user_agent,
            "import_batch_id": db_skill.import_batch_id,
            "federation_source": db_skill.federation_source,
            "version": db_skill.version,
            "team_id": db_skill.team_id,
            "team": team_name,
            "owner_email": db_skill.owner_email,
            "visibility": db_skill.visibility,
            "skill_uri": build_skill_uri(db_skill.skill_path),
        }

    def _validate_references(self, db: Session, allowed_tools: Optional[str], allowed_gateway_ids: List[str]) -> None:  # pylint: disable=unused-argument
        """Verify that referenced tools and gateways exist.

        Only existence is checked — not visibility to the caller. This is
        advisory: we reject saves that reference absent IDs, but we do not
        enforce that a skill may only be invoked on servers that expose the
        same set (that's the UI's job to warn about).

        Args:
            db: SQLAlchemy session.
            allowed_tools: Space-separated agentskills.io string. IDs are
                whatever substrings remain after stripping argument-list
                parentheses — e.g. ``Read Bash(git:*)`` produces ``{"Read", "Bash"}``.
            allowed_gateway_ids: Foundry gateway IDs (UUIDs).

        Raises:
            SkillValidationError: if any referenced gateway ID does not exist.
        """
        # Gateway IDs must exist.
        if allowed_gateway_ids:
            found = {g.id for g in db.execute(select(Gateway).where(Gateway.id.in_(allowed_gateway_ids))).scalars().all()}
            missing = [g for g in allowed_gateway_ids if g not in found]
            if missing:
                raise SkillValidationError(f"allowed_gateway_ids references unknown gateway(s): {missing}")
        # allowed-tools is advisory — tool names can be vendor shorthand like "Bash(git:*)"
        # that does not map 1:1 to a Foundry Tool row. We do not hard-validate those names
        # in v1; the UI surfaces a warning when a referenced name does not match a known tool.

    # -- CRUD ------------------------------------------------------------

    async def register_skill(
        self,
        db: Session,
        skill: SkillCreate,
        *,
        created_by: Optional[str] = None,
        created_from_ip: Optional[str] = None,
        created_via: Optional[str] = None,
        created_user_agent: Optional[str] = None,
        import_batch_id: Optional[str] = None,
        federation_source: Optional[str] = None,
    ) -> SkillRead:
        """Create a new skill.

        Enforces the SEP-2640 path/name constraint (done in the schema layer) plus
        a uniqueness check on ``(team_id, owner_email, skill_path)`` scoped to the
        target visibility, so a user cannot clobber another user's public skill
        path.

        Args:
            db: Database session.
            skill: Validated create payload.
            created_by: Principal email to record as creator.
            created_from_ip: IP of the requester, for audit.
            created_via: One of ``ui|api|import|federation``.
            created_user_agent: Requester User-Agent.
            import_batch_id: Bulk-import batch UUID.
            federation_source: Source gateway for federated skills.

        Returns:
            SkillRead: The newly created skill.

        Raises:
            SkillNameConflictError: If a skill with the same path already exists in scope.
            SkillValidationError: If referenced gateways do not exist.
            SkillError: For any other unexpected DB error.
        """
        assert skill.skill_path is not None  # populated by SkillCreate.validate_path_ends_with_name
        try:
            self._validate_references(db, skill.allowed_tools, skill.allowed_gateway_ids)

            # Uniqueness check in scope. Public skills clash with any other public skill
            # at the same path; team/private skills clash with other (team_id, owner, path)
            # triples. This mirrors PromptService's pre-insert check — cheaper than
            # catching IntegrityError after the fact.
            visibility = (skill.visibility or "public").lower()
            if visibility == "public":
                existing = db.execute(select(DbSkill).where(DbSkill.skill_path == skill.skill_path, DbSkill.visibility == "public")).scalar_one_or_none()
            elif visibility == "team":
                existing = db.execute(
                    select(DbSkill).where(
                        DbSkill.skill_path == skill.skill_path,
                        DbSkill.visibility == "team",
                        DbSkill.team_id == skill.team_id,
                    )
                ).scalar_one_or_none()
            else:
                existing = db.execute(
                    select(DbSkill).where(
                        DbSkill.skill_path == skill.skill_path,
                        DbSkill.owner_email == (skill.owner_email or created_by),
                        DbSkill.visibility == "private",
                    )
                ).scalar_one_or_none()
            if existing is not None:
                raise SkillNameConflictError(
                    skill.skill_path,
                    is_active=existing.is_active,
                    skill_id=existing.id,
                    visibility=existing.visibility,
                )

            db_skill = DbSkill(
                skill_path=skill.skill_path,
                name=skill.name,
                description=skill.description,
                content_md=skill.content_md or "",
                license=skill.license,
                compatibility=skill.compatibility,
                metadata_json=skill.metadata_json or {},
                allowed_tools=skill.allowed_tools,
                allowed_gateway_ids=skill.allowed_gateway_ids or [],
                tags=skill.tags or [],
                team_id=skill.team_id,
                owner_email=skill.owner_email or created_by,
                visibility=visibility,
                created_by=created_by,
                created_from_ip=created_from_ip,
                created_via=created_via,
                created_user_agent=created_user_agent,
                import_batch_id=import_batch_id,
                federation_source=federation_source,
                version=1,
            )
            db.add(db_skill)
            db.commit()
            db.refresh(db_skill)
            logger.info(f"Registered skill: {db_skill.skill_path} (id={db_skill.id})")
            team_name = self._get_team_name(db, db_skill.team_id)
            return SkillRead.model_validate(self._convert_db_skill(db_skill, team_name))
        except SkillNameConflictError:
            db.rollback()
            raise
        except SkillValidationError:
            db.rollback()
            raise
        except IntegrityError as ie:
            db.rollback()
            logger.error(f"IntegrityError registering skill: {ie}")
            raise SkillNameConflictError(skill.skill_path)
        except Exception as e:
            db.rollback()
            raise SkillError(f"Failed to register skill: {e}") from e

    async def update_skill(
        self,
        db: Session,
        skill_id: Union[int, str],
        update: SkillUpdate,
        *,
        user_email: Optional[str] = None,
        modified_from_ip: Optional[str] = None,
        modified_via: Optional[str] = None,
        modified_user_agent: Optional[str] = None,
    ) -> SkillRead:
        """Update fields on an existing skill.

        Only provided fields are applied. If ``name`` changes, ``skill_path``
        must also change to satisfy the SEP-2640 "last segment == name" rule.
        We enforce that here rather than in the schema layer because we need the
        *existing* skill_path to compare against.

        Args:
            db: DB session.
            skill_id: PK of the skill.
            update: Validated update payload.
            user_email: Principal performing the update (for ownership check and audit).
            modified_from_ip: IP of the requester.
            modified_via: Modification channel.
            modified_user_agent: Requester User-Agent.

        Returns:
            SkillRead: The updated skill.

        Raises:
            SkillNotFoundError: If the skill is not found.
            SkillValidationError: If name changes without a matching path change or references are invalid.
            PermissionError: If user_email is not the owner.
        """
        try:
            db_skill = db.get(DbSkill, int(skill_id)) if isinstance(skill_id, str) and skill_id.isdigit() else db.get(DbSkill, skill_id)
            if not db_skill:
                raise SkillNotFoundError(f"Skill not found: {skill_id}")

            if user_email:
                # First-Party
                from mcpgateway.services.permission_service import PermissionService  # pylint: disable=import-outside-toplevel

                perm = PermissionService(db)
                if not await perm.check_resource_ownership(user_email, db_skill):
                    raise PermissionError("Only the owner can modify this skill")

            # If either name or skill_path is changing, both must end up consistent.
            new_name = update.name if update.name is not None else db_skill.name
            new_path = update.skill_path if update.skill_path is not None else db_skill.skill_path
            if new_path.rsplit("/", 1)[-1] != new_name:
                raise SkillValidationError(f"skill_path final segment '{new_path.rsplit('/', 1)[-1]}' must equal name '{new_name}' (SEP-2640)")

            # Apply changes.
            patch = update.model_dump(exclude_unset=True)
            # Re-validate references if they changed.
            if "allowed_tools" in patch or "allowed_gateway_ids" in patch:
                self._validate_references(
                    db,
                    patch.get("allowed_tools", db_skill.allowed_tools),
                    patch.get("allowed_gateway_ids", db_skill.allowed_gateway_ids) or [],
                )

            for key, value in patch.items():
                setattr(db_skill, key, value)
            # bump version
            db_skill.version = (db_skill.version or 1) + 1
            db_skill.modified_by = user_email
            db_skill.modified_from_ip = modified_from_ip
            db_skill.modified_via = modified_via
            db_skill.modified_user_agent = modified_user_agent

            db.commit()
            db.refresh(db_skill)
            team_name = self._get_team_name(db, db_skill.team_id)
            return SkillRead.model_validate(self._convert_db_skill(db_skill, team_name))
        except (PermissionError, SkillNotFoundError, SkillValidationError):
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise SkillError(f"Failed to update skill: {e}") from e

    async def delete_skill(self, db: Session, skill_id: Union[int, str], user_email: Optional[str] = None) -> None:
        """Delete a skill by id.

        Args:
            db: DB session.
            skill_id: Skill PK.
            user_email: Principal performing the delete (for ownership check).

        Raises:
            SkillNotFoundError: If the skill is not found.
            PermissionError: If user_email is provided and doesn't own the skill.
        """
        try:
            db_skill = db.get(DbSkill, int(skill_id)) if isinstance(skill_id, str) and skill_id.isdigit() else db.get(DbSkill, skill_id)
            if not db_skill:
                raise SkillNotFoundError(f"Skill not found: {skill_id}")
            if user_email:
                # First-Party
                from mcpgateway.services.permission_service import PermissionService  # pylint: disable=import-outside-toplevel

                perm = PermissionService(db)
                if not await perm.check_resource_ownership(user_email, db_skill):
                    raise PermissionError("Only the owner can delete this skill")
            db.delete(db_skill)
            db.commit()
            logger.info(f"Deleted skill: id={skill_id}")
        except (PermissionError, SkillNotFoundError):
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            raise SkillError(f"Failed to delete skill: {e}") from e

    async def get_skill(self, db: Session, skill_id: Union[int, str]) -> SkillRead:
        """Fetch a skill by id.

        Args:
            db: DB session.
            skill_id: Skill PK.

        Returns:
            SkillRead: The skill.

        Raises:
            SkillNotFoundError: If the skill is not found.
        """
        pk = int(skill_id) if isinstance(skill_id, str) and skill_id.isdigit() else skill_id
        db_skill = db.get(DbSkill, pk)
        if not db_skill:
            raise SkillNotFoundError(f"Skill not found: {skill_id}")
        team_name = self._get_team_name(db, db_skill.team_id)
        return SkillRead.model_validate(self._convert_db_skill(db_skill, team_name))

    async def toggle_skill_status(self, db: Session, skill_id: Union[int, str], activate: bool, user_email: Optional[str] = None) -> SkillRead:
        """Activate or deactivate a skill.

        Args:
            db: DB session.
            skill_id: Skill PK.
            activate: Target is_active state.
            user_email: Principal (for ownership check).

        Returns:
            SkillRead: The updated skill.

        Raises:
            SkillNotFoundError: If the skill is not found.
            PermissionError: If user_email doesn't own the skill.
        """
        return await self.update_skill(db, skill_id, SkillUpdate(is_active=activate), user_email=user_email)

    async def list_skills(
        self,
        db: Session,
        *,
        include_inactive: bool = False,
        cursor: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 100,
    ) -> Tuple[List[SkillRead], Optional[str]]:
        """Public list (no auth filter) with cursor pagination.

        Args:
            db: DB session.
            include_inactive: If True, include inactive skills.
            cursor: Opaque pagination cursor.
            tags: Optional tag filter — a skill matches if any of its tags intersect.
            limit: Max rows per page.

        Returns:
            tuple[list[SkillRead], str | None]: Rows plus next-cursor (None when exhausted).
        """
        query = select(DbSkill)
        if not include_inactive:
            query = query.where(DbSkill.is_active.is_(True))
        if cursor:
            decoded = decode_cursor(cursor) or {}
            last_id = decoded.get("id")
            if last_id is not None:
                query = query.where(DbSkill.id > last_id)
        query = query.order_by(DbSkill.id.asc()).limit(limit + 1)
        rows = list(db.execute(query).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        if tags:
            tagset = set(tags)
            rows = [r for r in rows if tagset.intersection(set(r.tags or []))]
        result = [SkillRead.model_validate(self._convert_db_skill(r, self._get_team_name(db, r.team_id))) for r in rows]
        next_cursor = encode_cursor({"id": rows[-1].id}) if has_more and rows else None
        return result, next_cursor

    async def list_skills_for_user(
        self,
        db: Session,
        user_email: str,
        *,
        team_id: Optional[str] = None,
        visibility: Optional[str] = None,
        include_inactive: bool = False,
        skip: int = 0,
        limit: int = 100,
    ) -> List[SkillRead]:
        """List skills visible to a given user under the team/visibility model.

        Mirrors :meth:`PromptService.list_prompts_for_user`:

        1. User's own rows (owner_email match).
        2. Team rows (team_id in user's teams, visibility in {team, public}).
        3. Public rows (visibility == public) across all teams.

        Args:
            db: DB session.
            user_email: Principal.
            team_id: Optional team filter — only rows within this team.
            visibility: Optional visibility filter.
            include_inactive: If True, include inactive skills.
            skip: Offset for pagination.
            limit: Max rows.

        Returns:
            list[SkillRead]: Visible skills.
        """
        # First-Party
        from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

        team_service = TeamManagementService(db)
        user_teams = await team_service.get_user_teams(user_email)
        team_ids = [t.id for t in user_teams]

        query = select(DbSkill)
        if not include_inactive:
            query = query.where(DbSkill.is_active.is_(True))

        if team_id:
            if team_id not in team_ids:
                return []
            conds = [
                and_(DbSkill.team_id == team_id, DbSkill.visibility.in_(["team", "public"])),
                and_(DbSkill.team_id == team_id, DbSkill.owner_email == user_email),
            ]
            query = query.where(or_(*conds))
        else:
            conds = [DbSkill.owner_email == user_email]
            if team_ids:
                conds.append(and_(DbSkill.team_id.in_(team_ids), DbSkill.visibility.in_(["team", "public"])))
            conds.append(DbSkill.visibility == "public")
            query = query.where(or_(*conds))

        if visibility:
            query = query.where(DbSkill.visibility == visibility)

        query = query.offset(skip).limit(limit)
        rows = list(db.execute(query).scalars().all())
        return [SkillRead.model_validate(self._convert_db_skill(r, self._get_team_name(db, r.team_id))) for r in rows]

    async def list_server_skills(self, db: Session, server_id: str, include_inactive: bool = False) -> List[SkillRead]:
        """List skills attached to a virtual server.

        Args:
            db: DB session.
            server_id: Virtual server id.
            include_inactive: If True, include inactive skills.

        Returns:
            list[SkillRead]: Skills composed onto the server.
        """
        query = select(DbSkill).join(server_skill_association, DbSkill.id == server_skill_association.c.skill_id).where(server_skill_association.c.server_id == server_id)
        if not include_inactive:
            query = query.where(DbSkill.is_active.is_(True))
        rows = list(db.execute(query).scalars().all())
        return [SkillRead.model_validate(self._convert_db_skill(r, self._get_team_name(db, r.team_id))) for r in rows]

    # -- MCP protocol projections ---------------------------------------

    def build_resource_list_entries(self, skills: List[DbSkill]) -> List[Dict[str, Any]]:
        """Project skills into MCP ``resources/list`` entries per SEP-2640.

        Each skill contributes one entry — the primary ``SKILL.md`` file. Supporting
        files are not yet exposed (v1 scope). The ``skill://index.json`` discovery
        resource is emitted separately by :meth:`build_index_entry`.

        Args:
            skills: ORM rows to project.

        Returns:
            list[dict]: Resource entries in wire shape.
        """
        entries: List[Dict[str, Any]] = []
        for s in skills:
            # Resource `_meta` carries Foundry-only fields under our reverse-DNS namespace.
            # Per WG guidance, do NOT duplicate skill-level semantics that live in
            # frontmatter (name/description/allowed-tools/etc.) into `_meta`.
            meta: Dict[str, Any] = {}
            ns = FOUNDRY_META_NAMESPACE
            if s.visibility:
                meta[f"{ns}/visibility"] = s.visibility
            if s.team_id:
                meta[f"{ns}/team_id"] = s.team_id
            if s.tags:
                meta[f"{ns}/tags"] = s.tags
            if s.allowed_gateway_ids:
                meta[f"{ns}/allowed_gateways"] = s.allowed_gateway_ids
            if s.owner_email:
                meta[f"{ns}/owner_email"] = s.owner_email

            annotations: Dict[str, Any] = {
                "audience": ["assistant"],
                "priority": 0.8,
            }
            if s.updated_at is not None:
                annotations["lastModified"] = s.updated_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

            entry: Dict[str, Any] = {
                "uri": build_skill_uri(s.skill_path),
                "name": s.name,
                "description": s.description,
                "mimeType": "text/markdown",
                "annotations": annotations,
            }
            if meta:
                entry["_meta"] = meta
            entries.append(entry)
        return entries

    def build_index_entry(self) -> Dict[str, Any]:
        """Return the ``skill://index.json`` well-known resource entry for ``resources/list``.

        This entry tells clients that a discovery index is available; they then
        call ``resources/read`` on it to get the contents.

        Returns:
            dict: Resource listing entry for the index.
        """
        return {
            "uri": SKILL_INDEX_URI,
            "name": "skills-index",
            "description": "SEP-2640 skill discovery index for this MCP endpoint.",
            "mimeType": "application/json",
            "annotations": {"audience": ["assistant"], "priority": 0.5},
        }

    def build_index_json(self, skills: List[DbSkill]) -> Dict[str, Any]:
        """Build the ``skill://index.json`` body for a set of skills.

        Conforms to the agentskills.io discovery-index schema (version 0.2.0):
        a top-level ``$schema`` plus a ``skills`` array. For v1 every entry is
        ``type: "skill-md"`` — the resource-template form will be added when we
        support parameterized skill namespaces.

        Args:
            skills: ORM rows to include.

        Returns:
            dict: JSON-serializable index.
        """
        return {
            "$schema": SKILLS_INDEX_SCHEMA_URL,
            "skills": [
                {
                    "name": s.name,
                    "type": "skill-md",
                    "description": s.description,
                    "url": build_skill_uri(s.skill_path),
                }
                for s in skills
            ],
        }

    async def read_resource(
        self,
        db: Session,
        uri: str,
        *,
        scope_skills: Optional[List[DbSkill]] = None,
    ) -> Dict[str, Any]:
        """Handle ``resources/read`` for a ``skill://`` URI.

        Produces a dict in MCP ``ReadResourceResult`` shape:
        ``{"contents": [{"uri": ..., "mimeType": ..., "text": ...}]}``.

        Args:
            db: DB session.
            uri: A ``skill://`` URI — either the well-known ``skill://index.json``
                or ``skill://<path>/SKILL.md``.
            scope_skills: Optional pre-computed list of skills in scope (e.g. for
                server-scoped reads). When ``None`` we fall back to all active skills.

        Returns:
            dict: ``{"contents": [...]}`` result payload.

        Raises:
            SkillNotFoundError: If the URI names a non-existent skill, or names a
                file other than ``SKILL.md`` (supporting files are v2 scope).
            SkillValidationError: If the URI is malformed.
        """
        if uri == SKILL_INDEX_URI:
            # The well-known index aggregates whatever skills are in scope.
            if scope_skills is None:
                scope_skills = list(db.execute(select(DbSkill).where(DbSkill.is_active.is_(True))).scalars().all())
            # Standard
            import json

            body = json.dumps(self.build_index_json(scope_skills), indent=2)
            return {"contents": [{"uri": uri, "mimeType": "application/json", "text": body}]}

        parsed = parse_skill_uri(uri)
        if parsed is None:
            raise SkillValidationError(f"Malformed skill URI: {uri}")
        skill_path, file_path = parsed
        if file_path != "SKILL.md":
            # v1 only serves SKILL.md. Supporting files (scripts/, references/, assets/)
            # are reserved for v2.
            raise SkillNotFoundError(f"Supporting-file reads are not yet supported: {uri}")

        # Resolve the skill. If a scope is given, search within it (server-scoped);
        # otherwise search globally among active skills.
        if scope_skills is not None:
            match = next((s for s in scope_skills if s.skill_path == skill_path), None)
        else:
            match = db.execute(select(DbSkill).where(DbSkill.skill_path == skill_path, DbSkill.is_active.is_(True))).scalar_one_or_none()
        if match is None:
            raise SkillNotFoundError(f"Skill not found for URI: {uri}")
        return {"contents": [{"uri": uri, "mimeType": "text/markdown", "text": serialize_skill_md(match)}]}

    # -- import / export -------------------------------------------------

    async def import_skill(self, db: Session, file_text: str, **meta: Any) -> SkillRead:
        """Parse a SKILL.md file and register it.

        Args:
            db: DB session.
            file_text: Full SKILL.md content (frontmatter + body).
            **meta: Audit fields (created_by, created_from_ip, etc.) passed through
                to :meth:`register_skill`.

        Returns:
            SkillRead: The newly registered skill.

        Raises:
            SkillValidationError: If frontmatter is missing a required field.
        """
        parsed = parse_skill_md(file_text)
        if not parsed.get("name") or not parsed.get("description"):
            raise SkillValidationError("SKILL.md must contain both `name` and `description` in frontmatter")
        payload = SkillCreate(**parsed)
        return await self.register_skill(db, payload, **meta)

    def export_skill(self, db_skill: DbSkill) -> SkillFileExport:
        """Serialize a skill as a downloadable SKILL.md file.

        Args:
            db_skill: ORM row.

        Returns:
            SkillFileExport: Filename + content pair for the HTTP response.
        """
        filename = f"{db_skill.name}_SKILL.md"
        return SkillFileExport(filename=filename, content=serialize_skill_md(db_skill))
