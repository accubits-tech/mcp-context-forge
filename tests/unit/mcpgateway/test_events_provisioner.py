# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_provisioner.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for **mcpgateway.services.events.provisioner**.

These tests exercise the refcounted upstream-hook provisioning machinery
against a real (temporary, in-memory) database. The provisioner keeps a
per-``(connection, event_type)`` reference count in ``Gateway.hook_state`` and
calls a pluggable :class:`UpstreamHookProvisioner` only on refcount edge
transitions:

* ``ensure_hooks`` registers via the provisioner **only on 0 -> 1** and reuses
  the live hook on subsequent references (TC-SUB-019: a 2nd ensure of the same
  event_types takes refcount to 2 with exactly ONE provider register call);
* ``release_hooks`` de-registers **only on 1 -> 0** (TC-SUB-020: release to 0
  triggers exactly ONE deregister call);
* with two references, releasing one retains the hook at refcount=1 with zero
  deregister calls (TC-SUB-021);
* a provider ``register`` failure rolls the refcount/hook_state back and
  re-raises with no dangling state (TC-SUB-022);
* a double release is idempotent and never drives the refcount negative
  (TC-SUB-008);
* interleaved concurrent ensure/release keep the refcount consistent with no
  lost updates or orphaned hooks (TC-SUB-010).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_provisioner.py -q
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
from mcpgateway.services.events.provisioner import (
    ensure_hooks,
    NoopProvisioner,
    release_hooks,
    UpstreamHookProvisioner,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


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


def _make_gateway(db) -> Gateway:
    """Persist a minimal Gateway row to hang hook_state on."""
    gw = Gateway(
        id=uuid.uuid4().hex,
        name=f"gw-{uuid.uuid4().hex[:6]}",
        slug=f"gw-{uuid.uuid4().hex[:8]}",
        url="http://example.com",
        capabilities={},
    )
    db.add(gw)
    db.commit()
    db.refresh(gw)
    return gw


# --------------------------------------------------------------------------- #
# Test doubles                                                                 #
# --------------------------------------------------------------------------- #


class MockProvisioner(UpstreamHookProvisioner):
    """A provisioner that COUNTS register/deregister calls for assertions."""

    def __init__(self, *, fail_register: bool = False) -> None:
        """Initialize the mock.

        Args:
            fail_register: When ``True``, :meth:`register` raises a
                :class:`RuntimeError` to simulate a provider API failure.
        """
        self.register_calls: list[tuple[str, tuple[str, ...]]] = []
        self.deregister_calls: list[tuple[str, tuple[str, ...]]] = []
        self.fail_register = fail_register

    async def register(self, gateway, event_types):  # type: ignore[override]
        """Record a register call and return a synthetic hook ref (or fail)."""
        self.register_calls.append((gateway.id, tuple(event_types)))
        if self.fail_register:
            raise RuntimeError("provider 500")
        return {"external_hook_id": f"hook-{uuid.uuid4().hex[:8]}", "scopes_granted": ["admin:repo_hook"]}

    async def deregister(self, gateway, event_types, hook_ref):  # type: ignore[override]
        """Record a deregister call."""
        self.deregister_calls.append((gateway.id, tuple(event_types)))


# --------------------------------------------------------------------------- #
# TC-SUB-019: 2nd ensure same event_types -> refcount=2, ONE register total    #
# --------------------------------------------------------------------------- #


def test_ensure_twice_increments_refcount_one_register(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()

    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))
    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))

    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 2
    assert len(prov.register_calls) == 1  # exactly ONE provider call total
    assert gw.hook_state["com.github.push"]["external_hook_id"]
    assert gw.hook_state["com.github.push"]["registered_at"]
    assert gw.hook_state["com.github.push"]["scopes_granted"] == ["admin:repo_hook"]


# --------------------------------------------------------------------------- #
# TC-SUB-020: release to 0 -> exactly ONE deregister, hook state cleared       #
# --------------------------------------------------------------------------- #


def test_release_to_zero_deregisters_once(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()

    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))
    asyncio.run(release_hooks(session, gw, ["com.github.push"], prov))

    session.refresh(gw)
    assert len(prov.deregister_calls) == 1
    # The hook entry is gone once the refcount reaches 0.
    assert "com.github.push" not in (gw.hook_state or {})


# --------------------------------------------------------------------------- #
# TC-SUB-021: two refs, release one -> hook retained, refcount=1, no deregister #
# --------------------------------------------------------------------------- #


def test_release_one_of_two_retains_hook(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()

    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))
    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))
    asyncio.run(release_hooks(session, gw, ["com.github.push"], prov))

    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 1
    assert len(prov.deregister_calls) == 0  # still referenced -> no deregister
    assert len(prov.register_calls) == 1


# --------------------------------------------------------------------------- #
# TC-SUB-022: provider.register raises -> rolled back, no dangling refcount     #
# --------------------------------------------------------------------------- #


def test_register_failure_rolls_back_and_reraises(session):
    gw = _make_gateway(session)
    prov = MockProvisioner(fail_register=True)

    with pytest.raises(RuntimeError):
        asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))

    session.refresh(gw)
    # No dangling refcount/hook entry after a failed register.
    assert "com.github.push" not in (gw.hook_state or {})
    assert len(prov.register_calls) == 1


def test_register_failure_does_not_corrupt_existing_refcount(session):
    """A failed register for a NEW event type must not disturb a live hook."""
    gw = _make_gateway(session)
    ok = MockProvisioner()
    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], ok))

    bad = MockProvisioner(fail_register=True)
    with pytest.raises(RuntimeError):
        asyncio.run(ensure_hooks(session, gw, ["com.github.pull_request"], bad))

    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 1
    assert "com.github.pull_request" not in gw.hook_state


# --------------------------------------------------------------------------- #
# TC-SUB-008: double release is idempotent, never negative                     #
# --------------------------------------------------------------------------- #


def test_double_release_idempotent_never_negative(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()

    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))
    asyncio.run(release_hooks(session, gw, ["com.github.push"], prov))
    # Second release of an already-zero hook is a no-op.
    asyncio.run(release_hooks(session, gw, ["com.github.push"], prov))

    session.refresh(gw)
    assert "com.github.push" not in (gw.hook_state or {})
    assert len(prov.deregister_calls) == 1  # not called a second time


def test_release_unknown_event_type_is_noop(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()
    # Releasing a hook that was never ensured must not raise or go negative.
    asyncio.run(release_hooks(session, gw, ["com.never.seen"], prov))
    session.refresh(gw)
    assert (gw.hook_state or {}).get("com.never.seen") is None
    assert len(prov.deregister_calls) == 0


# --------------------------------------------------------------------------- #
# Multi event-type ensure/release in one call                                  #
# --------------------------------------------------------------------------- #


def test_ensure_multiple_event_types(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()

    asyncio.run(ensure_hooks(session, gw, ["com.github.push", "com.github.pull_request"], prov))

    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 1
    assert gw.hook_state["com.github.pull_request"]["refcount"] == 1
    assert len(prov.register_calls) == 2


# --------------------------------------------------------------------------- #
# TC-SUB-010: interleaved concurrent ensure/release keep refcount consistent   #
# --------------------------------------------------------------------------- #


def test_concurrent_ensure_release_consistent(session):
    gw = _make_gateway(session)
    prov = MockProvisioner()

    async def driver():
        # Seed a baseline reference so concurrent releases have something to
        # decrement without going negative.
        await ensure_hooks(session, gw, ["com.github.push"], prov)
        # Interleave many ensure/release pairs concurrently.
        tasks = []
        for _ in range(20):
            tasks.append(ensure_hooks(session, gw, ["com.github.push"], prov))
            tasks.append(release_hooks(session, gw, ["com.github.push"], prov))
        await asyncio.gather(*tasks)

    asyncio.run(driver())

    session.refresh(gw)
    # Each ensure is matched by a release, so the baseline reference remains.
    assert gw.hook_state["com.github.push"]["refcount"] == 1
    # The hook was registered exactly once and never torn down (baseline held).
    assert len(prov.register_calls) == 1
    assert len(prov.deregister_calls) == 0


# --------------------------------------------------------------------------- #
# NoopProvisioner default behaviour                                            #
# --------------------------------------------------------------------------- #


def test_noop_provisioner_returns_synthetic_id_no_external_call(session):
    gw = _make_gateway(session)
    prov = NoopProvisioner()

    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], prov))

    session.refresh(gw)
    entry = gw.hook_state["com.github.push"]
    assert entry["refcount"] == 1
    assert entry["external_hook_id"]  # synthetic id present

    # release back to zero must work cleanly with the default provisioner.
    asyncio.run(release_hooks(session, gw, ["com.github.push"], prov))
    session.refresh(gw)
    assert "com.github.push" not in (gw.hook_state or {})


def test_noop_provisioner_is_default_when_none_passed(session):
    """ensure_hooks/release_hooks default to a NoopProvisioner when None."""
    gw = _make_gateway(session)
    asyncio.run(ensure_hooks(session, gw, ["com.github.push"], None))
    session.refresh(gw)
    assert gw.hook_state["com.github.push"]["refcount"] == 1
    asyncio.run(release_hooks(session, gw, ["com.github.push"], None))
    session.refresh(gw)
    assert "com.github.push" not in (gw.hook_state or {})
