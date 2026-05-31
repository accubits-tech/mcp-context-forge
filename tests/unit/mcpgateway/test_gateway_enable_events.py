# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_gateway_enable_events.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for ``GatewayService.enable_events`` — making an existing gateway
events-ready so budapp can auto-provision event triggers WITHOUT a direct DB
write (the phase-1 workaround).

Coverage:

* sets ``events_enabled``, folds the non-secret ``capabilities.events`` block
  (descriptor_ref + webhooksSupported), records the descriptor verify strategy,
  and stores the inbound webhook signing secret encrypted in the dedicated
  column only (never plaintext, never under capabilities).
* idempotent: re-invoking rotates the signing secret and re-affirms capability.
* unknown ``descriptor_ref`` -> ``GatewayError`` (mapped to 422 at the route).
* missing gateway -> ``GatewayNotFoundError``.
* the returned ``GatewayRead`` never exposes the signing secret.

Run with::

    pytest tests/unit/mcpgateway/test_gateway_enable_events.py -q
"""

# Future
from __future__ import annotations

# Standard
import asyncio
import uuid

# Third-Party
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# First-Party
from mcpgateway.db import Base, Gateway
from mcpgateway.services.gateway_service import GatewayError, GatewayNotFoundError, GatewayService
from mcpgateway.utils.services_auth import decode_auth


TEAM_A = "team-a"


@pytest.fixture
def session():
    """Create a fresh in-memory database session with all tables built."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = testing_session_local()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _make_gateway(db, *, team_id=TEAM_A) -> Gateway:
    """Persist a minimal (non-events) Gateway row, as the OAuth/tools register flow would."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        capabilities={},
        team_id=team_id,
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


def _svc() -> GatewayService:
    return GatewayService()


def test_enable_events_sets_row_fields(session):
    gw = _make_gateway(session)
    svc = _svc()

    result = asyncio.run(svc.enable_events(session, gw.id, descriptor_ref="slack", webhook_signing_secret="s3cr3t", user_email=None))

    row = session.get(Gateway, gw.id)
    assert row.events_enabled is True
    # Non-secret capability folded in (camelCase wire shape).
    assert row.capabilities["events"]["ingress"]["descriptor_ref"] == "slack"
    assert row.capabilities["events"]["webhooksSupported"] is True
    # Descriptor verify strategy recorded (slack uses hmac_timestamped).
    assert row.webhook_secret_algo == "hmac_timestamped"
    # Secret stored ENCRYPTED in the dedicated column only; round-trips via decode_auth.
    assert row.webhook_signing_secret is not None
    assert row.webhook_signing_secret != "s3cr3t"
    assert decode_auth(row.webhook_signing_secret) == {"secret": "s3cr3t"}
    # Secret must NOT leak into capabilities anywhere.
    assert "s3cr3t" not in str(row.capabilities)
    # A GatewayRead is returned and never exposes the signing secret.
    dumped = result.model_dump()
    assert "s3cr3t" not in str(dumped)
    assert dumped.get("webhook_signing_secret") in (None, "")


def test_enable_events_idempotent_rotates_secret(session):
    gw = _make_gateway(session)
    svc = _svc()

    asyncio.run(svc.enable_events(session, gw.id, descriptor_ref="slack", webhook_signing_secret="old", user_email=None))
    asyncio.run(svc.enable_events(session, gw.id, descriptor_ref="slack", webhook_signing_secret="new", user_email=None))

    row = session.get(Gateway, gw.id)
    assert decode_auth(row.webhook_signing_secret) == {"secret": "new"}
    assert row.events_enabled is True


def test_enable_events_unknown_descriptor_raises(session):
    gw = _make_gateway(session)
    svc = _svc()

    with pytest.raises(GatewayError):
        asyncio.run(svc.enable_events(session, gw.id, descriptor_ref="not-a-provider", webhook_signing_secret="x", user_email=None))

    # Nothing persisted on the rejected path.
    row = session.get(Gateway, gw.id)
    assert row.events_enabled is False
    assert row.webhook_signing_secret is None


def test_enable_events_missing_gateway_raises(session):
    svc = _svc()
    with pytest.raises(GatewayNotFoundError):
        asyncio.run(svc.enable_events(session, "does-not-exist", descriptor_ref="slack", webhook_signing_secret="x", user_email=None))
