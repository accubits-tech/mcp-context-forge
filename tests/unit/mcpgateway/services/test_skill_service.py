# -*- coding: utf-8 -*-
"""Unit tests for SkillService.

These tests exercise the service against an in-memory SQLite DB so we verify
real SQLAlchemy behavior (uniqueness constraints, relationships) in addition to
the pure helpers (URI builders, frontmatter round-trip, MCP projections).

SEP-2640 and agentskills.io correctness is the thing we care about. Each
assertion below maps to a clause in the spec or the Skills Extension draft.
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import json

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# First-Party
from mcpgateway.db import Base, Gateway
from mcpgateway.db import Skill as DbSkill
from mcpgateway.schemas import SkillCreate, SkillUpdate
from mcpgateway.services.skill_service import (
    FOUNDRY_META_NAMESPACE,
    SKILL_INDEX_URI,
    SKILLS_EXTENSION_ID,
    SKILLS_INDEX_SCHEMA_URL,
    SkillNameConflictError,
    SkillNotFoundError,
    SkillService,
    SkillValidationError,
    build_skill_uri,
    parse_skill_md,
    parse_skill_uri,
    serialize_skill_md,
)


@pytest.fixture()
def session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


@pytest.fixture()
def service() -> SkillService:
    return SkillService()


# ---------------------------------------------------------------------------
# Pure helpers — URI and frontmatter round-trip
# ---------------------------------------------------------------------------


class TestUriHelpers:
    def test_build_flat_skill_uri(self):
        assert build_skill_uri("code-review") == "skill://code-review/SKILL.md"

    def test_build_nested_skill_uri(self):
        assert build_skill_uri("acme/billing/refunds") == "skill://acme/billing/refunds/SKILL.md"

    def test_build_with_subfile(self):
        assert build_skill_uri("code-review", "references/GUIDE.md") == "skill://code-review/references/GUIDE.md"

    def test_parse_flat(self):
        assert parse_skill_uri("skill://code-review/SKILL.md") == ("code-review", "SKILL.md")

    def test_parse_nested(self):
        assert parse_skill_uri("skill://acme/billing/refunds/SKILL.md") == ("acme/billing/refunds", "SKILL.md")

    def test_parse_subresource(self):
        assert parse_skill_uri("skill://code-review/references/GUIDE.md") == ("code-review/references", "GUIDE.md")

    def test_parse_rejects_non_skill_scheme(self):
        assert parse_skill_uri("http://example.com/file.md") is None

    def test_parse_rejects_missing_file(self):
        # `skill://foo` alone has no file segment.
        assert parse_skill_uri("skill://foo") is None


class TestFrontmatterRoundTrip:
    """Serialization must emit spec-core fields only; parse must round-trip them."""

    def test_serialize_minimal(self):
        skill = DbSkill(
            name="minimal",
            skill_path="minimal",
            description="Short",
            content_md="# Body",
            metadata_json={},
            allowed_gateway_ids=[],
            tags=[],
        )
        text = serialize_skill_md(skill)
        assert text.startswith("---\nname: minimal\n")
        assert "description: Short" in text
        assert "# Body" in text
        # Foundry-only fields MUST NOT leak into the portable SKILL.md.
        assert "allowed_gateway_ids" not in text
        assert "visibility" not in text
        assert "team_id" not in text

    def test_serialize_preserves_order(self):
        """``name`` first, ``description`` next, ``allowed-tools`` before license — reader-friendly order."""
        skill = DbSkill(
            name="ordered",
            skill_path="ordered",
            description="d",
            content_md="",
            allowed_tools="Read",
            license="Apache-2.0",
            compatibility="any",
            metadata_json={"author": "a"},
            allowed_gateway_ids=[],
            tags=[],
        )
        text = serialize_skill_md(skill)
        name_i = text.index("name:")
        desc_i = text.index("description:")
        tools_i = text.index("allowed-tools:")
        license_i = text.index("license:")
        assert name_i < desc_i < tools_i < license_i

    def test_roundtrip(self):
        original = "---\n" "name: rt\n" "description: round-trip\n" "allowed-tools: Read Bash(git:*)\n" "metadata:\n" "  author: alice\n" "---\n\n" "# Body\nContent."
        parsed = parse_skill_md(original)
        assert parsed["name"] == "rt"
        assert parsed["description"] == "round-trip"
        assert parsed["allowed_tools"] == "Read Bash(git:*)"
        assert parsed["metadata_json"] == {"author": "alice"}
        assert parsed["content_md"] == "# Body\nContent."


# ---------------------------------------------------------------------------
# Schema-level validation (agentskills.io naming rules, SEP-2640 path rule)
# ---------------------------------------------------------------------------


class TestSkillCreateValidation:
    def test_valid_flat_name(self):
        sc = SkillCreate(name="code-review", description="d")
        assert sc.skill_path == "code-review"

    def test_valid_nested_path(self):
        sc = SkillCreate(name="refunds", skill_path="acme/billing/refunds", description="d")
        assert sc.skill_path == "acme/billing/refunds"

    @pytest.mark.parametrize(
        "bad_name",
        ["Code-Review", "-leading", "trailing-", "double--hyphen", "Contains Space", "a" * 65],
    )
    def test_rejects_bad_names(self, bad_name: str):
        with pytest.raises(Exception):
            SkillCreate(name=bad_name, description="d")

    def test_rejects_tail_not_matching_name(self):
        # SEP-2640: final segment of skill_path MUST equal name.
        with pytest.raises(Exception) as exc:
            SkillCreate(name="refunds", skill_path="acme/other", description="d")
        assert "final segment" in str(exc.value)

    def test_rejects_empty_path_segment(self):
        with pytest.raises(Exception):
            SkillCreate(name="x", skill_path="a//b", description="d")

    def test_description_length_limit(self):
        with pytest.raises(Exception):
            SkillCreate(name="x", description="x" * 1025)

    def test_compatibility_length_limit(self):
        with pytest.raises(Exception):
            SkillCreate(name="x", description="d", compatibility="c" * 501)


# ---------------------------------------------------------------------------
# CRUD via real DB round-trip
# ---------------------------------------------------------------------------


class TestSkillServiceCrud:
    def test_register_and_get(self, service: SkillService, session):
        sc = SkillCreate(name="demo", description="A demo skill", content_md="# Hi", visibility="public")
        created = asyncio.run(service.register_skill(session, sc, created_by="alice@acme.com"))
        assert created.id is not None
        assert created.skill_uri == "skill://demo/SKILL.md"
        got = asyncio.run(service.get_skill(session, created.id))
        assert got.name == "demo"
        assert got.owner_email == "alice@acme.com"

    def test_duplicate_path_rejected_scoped_to_public(self, service: SkillService, session):
        sc = SkillCreate(name="dup", description="d", visibility="public")
        asyncio.run(service.register_skill(session, sc, created_by="a@x.com"))
        with pytest.raises(SkillNameConflictError):
            asyncio.run(service.register_skill(session, sc, created_by="b@x.com"))

    def test_update_bumps_version(self, service: SkillService, session):
        sc = SkillCreate(name="u1", description="d", visibility="public")
        c = asyncio.run(service.register_skill(session, sc))
        updated = asyncio.run(service.update_skill(session, c.id, SkillUpdate(description="new")))
        assert updated.version == 2
        assert updated.description == "new"

    def test_update_enforces_path_tail_equals_name(self, service: SkillService, session):
        sc = SkillCreate(name="t1", description="d", visibility="public")
        c = asyncio.run(service.register_skill(session, sc))
        # name change without matching path change -> rejected
        with pytest.raises(SkillValidationError):
            asyncio.run(service.update_skill(session, c.id, SkillUpdate(name="t2")))
        # name + path change in lockstep -> ok
        renamed = asyncio.run(service.update_skill(session, c.id, SkillUpdate(name="t2", skill_path="t2")))
        assert renamed.name == "t2"
        assert renamed.skill_path == "t2"

    def test_delete_then_get_raises(self, service: SkillService, session):
        c = asyncio.run(service.register_skill(session, SkillCreate(name="d1", description="d", visibility="public")))
        asyncio.run(service.delete_skill(session, c.id))
        with pytest.raises(SkillNotFoundError):
            asyncio.run(service.get_skill(session, c.id))

    def test_reference_validation_rejects_unknown_gateway(self, service: SkillService, session):
        sc = SkillCreate(name="badref", description="d", allowed_gateway_ids=["does-not-exist"], visibility="public")
        with pytest.raises(SkillValidationError) as exc:
            asyncio.run(service.register_skill(session, sc))
        assert "does-not-exist" in str(exc.value)

    def test_reference_validation_accepts_known_gateway(self, service: SkillService, session):
        # Create a gateway so the reference validates. Populate all NOT NULL columns.
        gw = Gateway(
            id="gw-1",
            name="g1",
            slug="g1",
            url="http://example.com",
            description=None,
            transport="SSE",
            capabilities={},
        )
        session.add(gw)
        session.commit()
        sc = SkillCreate(name="okref", description="d", allowed_gateway_ids=["gw-1"], visibility="public")
        c = asyncio.run(service.register_skill(session, sc))
        assert c.allowed_gateway_ids == ["gw-1"]


# ---------------------------------------------------------------------------
# MCP projections — the SEP-2640 wire-shape contract
# ---------------------------------------------------------------------------


class TestMcpProjections:
    def test_module_constants_stable(self):
        """If any of these drift, every client integration breaks."""
        assert SKILLS_EXTENSION_ID == "io.modelcontextprotocol/skills"
        assert FOUNDRY_META_NAMESPACE == "io.hybrid360.foundry"
        assert SKILL_INDEX_URI == "skill://index.json"
        assert SKILLS_INDEX_SCHEMA_URL.startswith("https://schemas.agentskills.io/discovery/")

    def test_resource_list_entry_shape(self, service: SkillService, session):
        sc = SkillCreate(
            name="proj",
            description="Project description",
            tags=["t1"],
            allowed_gateway_ids=[],
            visibility="team",
            team_id="team-7",
        )
        created = asyncio.run(service.register_skill(session, sc, created_by="u@x.com"))
        rows = [session.get(DbSkill, created.id)]
        entries = service.build_resource_list_entries(rows)
        assert len(entries) == 1
        e = entries[0]
        assert e["uri"] == "skill://proj/SKILL.md"
        assert e["name"] == "proj"
        assert e["description"] == "Project description"
        assert e["mimeType"] == "text/markdown"
        ann = e["annotations"]
        assert ann["audience"] == ["assistant"]
        assert ann["priority"] == 0.8
        assert ann["lastModified"].endswith("Z")
        # Foundry-only metadata is namespaced.
        meta = e["_meta"]
        assert meta["io.hybrid360.foundry/visibility"] == "team"
        assert meta["io.hybrid360.foundry/team_id"] == "team-7"
        assert meta["io.hybrid360.foundry/tags"] == ["t1"]
        assert meta["io.hybrid360.foundry/owner_email"] == "u@x.com"

    def test_index_entry_shape(self, service: SkillService):
        entry = service.build_index_entry()
        assert entry["uri"] == SKILL_INDEX_URI
        assert entry["mimeType"] == "application/json"
        assert entry["annotations"]["audience"] == ["assistant"]

    def test_index_json_shape(self, service: SkillService, session):
        for name in ("a", "b"):
            asyncio.run(service.register_skill(session, SkillCreate(name=name, description="d", visibility="public")))
        rows = session.query(DbSkill).all()
        idx = service.build_index_json(rows)
        assert idx["$schema"] == SKILLS_INDEX_SCHEMA_URL
        assert len(idx["skills"]) == 2
        for entry in idx["skills"]:
            assert entry["type"] == "skill-md"
            assert entry["url"].startswith("skill://")
            assert entry["url"].endswith("/SKILL.md")

    def test_read_resource_skill_md(self, service: SkillService, session):
        sc = SkillCreate(name="rd", description="d", content_md="# Hi", visibility="public")
        asyncio.run(service.register_skill(session, sc))
        result = asyncio.run(service.read_resource(session, "skill://rd/SKILL.md"))
        contents = result["contents"]
        assert len(contents) == 1
        assert contents[0]["mimeType"] == "text/markdown"
        assert "name: rd" in contents[0]["text"]
        assert "# Hi" in contents[0]["text"]

    def test_read_resource_index(self, service: SkillService, session):
        asyncio.run(service.register_skill(session, SkillCreate(name="i1", description="d", visibility="public")))
        result = asyncio.run(service.read_resource(session, "skill://index.json"))
        doc = json.loads(result["contents"][0]["text"])
        assert doc["$schema"] == SKILLS_INDEX_SCHEMA_URL
        assert doc["skills"][0]["name"] == "i1"

    def test_read_resource_rejects_subfile_v1(self, service: SkillService, session):
        """v1 only serves SKILL.md; supporting files are reserved for v2."""
        asyncio.run(service.register_skill(session, SkillCreate(name="sub", description="d", visibility="public")))
        with pytest.raises(SkillNotFoundError):
            asyncio.run(service.read_resource(session, "skill://sub/references/GUIDE.md"))

    def test_read_resource_unknown_skill(self, service: SkillService, session):
        with pytest.raises(SkillNotFoundError):
            asyncio.run(service.read_resource(session, "skill://missing/SKILL.md"))

    def test_read_resource_rejects_bad_uri(self, service: SkillService, session):
        with pytest.raises(SkillValidationError):
            asyncio.run(service.read_resource(session, "skill://no-file"))


# ---------------------------------------------------------------------------
# Scoped listing — user/team visibility, server scoping
# ---------------------------------------------------------------------------


class TestVisibilityScoping:
    def test_public_skills_visible_to_non_owner_user(self, service: SkillService, session, monkeypatch):
        # Pretend the TeamManagementService returns no teams for this user; only public
        # skills should then be visible.
        class _FakeTMS:
            def __init__(self, _db):
                pass

            async def get_user_teams(self, _email):
                return []

        monkeypatch.setattr("mcpgateway.services.team_management_service.TeamManagementService", _FakeTMS)
        asyncio.run(service.register_skill(session, SkillCreate(name="v1", description="d", visibility="public"), created_by="alice@x.com"))
        asyncio.run(service.register_skill(session, SkillCreate(name="v2", description="d", visibility="private"), created_by="alice@x.com"))
        visible = asyncio.run(service.list_skills_for_user(session, "bob@x.com"))
        names = {s.name for s in visible}
        assert "v1" in names
        assert "v2" not in names

    def test_owner_sees_own_private_skill(self, service: SkillService, session, monkeypatch):
        class _FakeTMS:
            def __init__(self, _db):
                pass

            async def get_user_teams(self, _email):
                return []

        monkeypatch.setattr("mcpgateway.services.team_management_service.TeamManagementService", _FakeTMS)
        asyncio.run(service.register_skill(session, SkillCreate(name="priv", description="d", visibility="private"), created_by="alice@x.com"))
        visible = asyncio.run(service.list_skills_for_user(session, "alice@x.com"))
        assert any(s.name == "priv" for s in visible)
