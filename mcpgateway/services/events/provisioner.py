# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/provisioner.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Refcounted provider-side webhook (upstream-hook) provisioning.

When a subscription needs events from an *external* provider, the gateway must
register the provider's webhook once and share it across every subscription
that targets the same ``(connection, event_type)`` (FRD §6.10 / §7.5.1). This
module owns that refcounting machinery over the connection's ``hook_state``
JSON column:

    Gateway.hook_state = {
        "<event_type>": {
            "external_hook_id": "<provider hook id>",
            "refcount": <int>,
            "registered_at": "<iso-8601 utc>",
            "scopes_granted": ["..."],
        },
        ...
    }

The actual upstream registration is delegated to a pluggable
:class:`UpstreamHookProvisioner`. The default :class:`NoopProvisioner` performs
no external call and hands back a synthetic ``external_hook_id`` so the
subsystem works end-to-end before the real OAuth-backed provider clients land.

The two entry points implement the edge-triggered semantics the FRD mandates:

* :func:`ensure_hooks` increments the refcount and calls
  ``provisioner.register`` **only on the ``0 -> 1`` transition**; a register
  failure rolls back the in-memory bookkeeping and re-raises (no dangling
  refcount).
* :func:`release_hooks` decrements the refcount and calls
  ``provisioner.deregister`` **only on the ``1 -> 0`` transition**, clearing the
  hook entry. It is idempotent and never drives the refcount negative.

Both are concurrency-safe: a per-``(gateway_id, event_type)`` :class:`asyncio.Lock`
serializes the read-modify-write of the refcount so interleaved ensure/release
coroutines cannot lose updates or leave an orphaned upstream hook (FRD NFR-5 /
TC-SUB-010).
"""

# Future
from __future__ import annotations

# Standard
from abc import ABC, abstractmethod
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uuid

__all__ = [
    "UpstreamHookProvisioner",
    "NoopProvisioner",
    "ensure_hooks",
    "release_hooks",
]


class UpstreamHookProvisioner(ABC):
    """Strategy for registering/de-registering a provider-side webhook.

    Concrete implementations talk to a specific provider's API (e.g. GitHub
    repo hooks via the connection's stored OAuth token, or a federated MCP
    ``resources/subscribe``). The refcounting in :func:`ensure_hooks` /
    :func:`release_hooks` guarantees these methods are invoked only on the
    ``0 -> 1`` and ``1 -> 0`` edges, so an implementation need not do its own
    refcounting.
    """

    @abstractmethod
    async def register(self, gateway: Any, event_types: List[str]) -> Dict[str, Any]:
        """Register the upstream webhook for *event_types* on *gateway*.

        Args:
            gateway: The connection (:class:`~mcpgateway.db.Gateway`) row.
            event_types: The provider event type(s) this hook covers.

        Returns:
            Dict[str, Any]: Hook metadata. SHOULD include ``external_hook_id``
            (the provider's hook identifier) and MAY include ``scopes_granted``.

        Raises:
            Exception: Any provider/transport error; the caller rolls back and
                re-raises so the create path fails atomically (TC-SUB-022).
        """

    @abstractmethod
    async def deregister(self, gateway: Any, event_types: List[str], hook_ref: Dict[str, Any]) -> None:
        """De-register the upstream webhook previously created by :meth:`register`.

        Args:
            gateway: The connection (:class:`~mcpgateway.db.Gateway`) row.
            event_types: The provider event type(s) the hook covered.
            hook_ref: The stored hook entry (carrying ``external_hook_id`` etc.).
        """


class NoopProvisioner(UpstreamHookProvisioner):
    """Default provisioner that performs no external call.

    It returns a synthetic ``external_hook_id`` so the refcount bookkeeping and
    the rest of the events pipeline can be exercised before real provider
    clients exist. De-registration is a no-op.
    """

    async def register(self, gateway: Any, event_types: List[str]) -> Dict[str, Any]:  # noqa: D401
        """Return a synthetic hook ref without contacting any provider.

        Args:
            gateway: The connection row (unused).
            event_types: The provider event type(s) (unused beyond the id seed).

        Returns:
            Dict[str, Any]: A dict carrying a synthetic ``external_hook_id``.
        """
        return {"external_hook_id": f"noop-{uuid.uuid4().hex}"}

    async def deregister(self, gateway: Any, event_types: List[str], hook_ref: Dict[str, Any]) -> None:
        """Do nothing (the synthetic hook has no external state).

        Args:
            gateway: The connection row (unused).
            event_types: The provider event type(s) (unused).
            hook_ref: The stored hook entry (unused).
        """


# Per-(gateway_id, event_type) locks serialize the refcount read-modify-write so
# interleaved ensure/release coroutines cannot lose updates (TC-SUB-010). Kept
# process-wide and created lazily; cross-instance safety is the worker's job via
# the L2/leader-election pattern (NFR-5).
_LOCKS: Dict[str, asyncio.Lock] = {}


def _lock_for(gateway_id: str, event_type: str) -> asyncio.Lock:
    """Return the shared lock guarding one ``(gateway_id, event_type)`` refcount.

    Args:
        gateway_id: The connection id.
        event_type: The provider event type.

    Returns:
        asyncio.Lock: The lazily-created lock for this key.
    """
    key = f"{gateway_id}\x00{event_type}"
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _hook_state(gateway: Any) -> Dict[str, Any]:
    """Return a fresh mutable copy of the gateway's ``hook_state`` map.

    A new dict is returned (rather than the live attribute) so callers can
    mutate freely and only assign back on success - and so SQLAlchemy reliably
    detects the change to the JSON column (in-place mutation of a JSON dict is
    not tracked without a mutable type).

    Args:
        gateway: The connection row.

    Returns:
        Dict[str, Any]: A shallow-but-deep-enough copy of ``hook_state``.
    """
    current = getattr(gateway, "hook_state", None) or {}
    # Copy each per-event entry too so a rollback cannot leak a half-mutation.
    return {event_type: dict(entry) for event_type, entry in current.items()}


async def ensure_hooks(db: Any, gateway: Any, event_types: List[str], provisioner: Optional[UpstreamHookProvisioner] = None) -> None:
    """Ensure an upstream hook exists for each event type, refcounted.

    For every event type, the refcount is incremented; the provider's
    ``register`` is invoked **only on the ``0 -> 1`` transition** (the first
    reference). Subsequent references reuse the live hook (TC-SUB-019). On a
    provider ``register`` failure the in-memory bookkeeping for that event type
    is rolled back and the exception is re-raised so the caller's create path
    fails atomically with no dangling refcount (TC-SUB-022).

    Args:
        db: An active SQLAlchemy session (synchronous).
        gateway: The connection (:class:`~mcpgateway.db.Gateway`) row.
        event_types: Provider event types to reference.
        provisioner: The upstream provisioner; defaults to
            :class:`NoopProvisioner` when ``None``.

    Raises:
        Exception: Re-raises any provider ``register`` error after rollback.
    """
    prov = provisioner or NoopProvisioner()
    for event_type in event_types:
        async with _lock_for(gateway.id, event_type):
            state = _hook_state(gateway)
            entry = state.get(event_type)

            if entry and int(entry.get("refcount", 0)) > 0:
                # Reuse the live hook - just bump the refcount (no provider call).
                entry["refcount"] = int(entry["refcount"]) + 1
                state[event_type] = entry
                gateway.hook_state = state
                db.add(gateway)
                db.commit()
                continue

            # 0 -> 1 transition: register upstream, then persist on success.
            hook_ref = await prov.register(gateway, [event_type])
            new_entry = {
                "external_hook_id": hook_ref.get("external_hook_id"),
                "refcount": 1,
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "scopes_granted": hook_ref.get("scopes_granted", []),
            }
            state[event_type] = new_entry
            gateway.hook_state = state
            db.add(gateway)
            try:
                db.commit()
            except Exception:
                # Persistence failed after a successful provider register: undo
                # the in-memory mutation so no dangling refcount survives. The
                # upstream hook teardown is best-effort here.
                db.rollback()
                gateway.hook_state = _hook_state_without(gateway, event_type, state)
                raise


def _hook_state_without(gateway: Any, event_type: str, attempted_state: Dict[str, Any]) -> Dict[str, Any]:
    """Return *attempted_state* minus *event_type* for rollback on commit failure.

    Args:
        gateway: The connection row (unused; kept for symmetry/readability).
        event_type: The event type whose half-applied entry must be dropped.
        attempted_state: The state map that was assigned before the failed commit.

    Returns:
        Dict[str, Any]: A copy of the state without the failed event entry.
    """
    rolled = {et: dict(entry) for et, entry in attempted_state.items() if et != event_type}
    return rolled


async def release_hooks(db: Any, gateway: Any, event_types: List[str], provisioner: Optional[UpstreamHookProvisioner] = None) -> None:
    """Release a reference to each event type's upstream hook, refcounted.

    For every event type, the refcount is decremented; the provider's
    ``deregister`` is invoked **only on the ``1 -> 0`` transition** and the hook
    entry is then removed (TC-SUB-020). With more than one reference, releasing
    one keeps the hook live at the lower refcount with no provider call
    (TC-SUB-021). The operation is idempotent: releasing an unknown or
    already-zero hook is a no-op and the refcount never goes negative
    (TC-SUB-008).

    Args:
        db: An active SQLAlchemy session (synchronous).
        gateway: The connection (:class:`~mcpgateway.db.Gateway`) row.
        event_types: Provider event types to release.
        provisioner: The upstream provisioner; defaults to
            :class:`NoopProvisioner` when ``None``.
    """
    prov = provisioner or NoopProvisioner()
    for event_type in event_types:
        async with _lock_for(gateway.id, event_type):
            state = _hook_state(gateway)
            entry = state.get(event_type)
            if not entry:
                # Never referenced (or already torn down): idempotent no-op.
                continue

            refcount = int(entry.get("refcount", 0))
            if refcount <= 1:
                # 1 -> 0 (or salvage a stuck 0): de-register and drop the entry.
                hook_ref = dict(entry)
                del state[event_type]
                gateway.hook_state = state
                db.add(gateway)
                db.commit()
                await prov.deregister(gateway, [event_type], hook_ref)
            else:
                entry["refcount"] = refcount - 1
                state[event_type] = entry
                gateway.hook_state = state
                db.add(gateway)
                db.commit()
