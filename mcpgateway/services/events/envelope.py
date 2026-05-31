# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/envelope.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Dependency-free event-envelope normalizer for config-driven ingress.

This module turns a verified, parsed provider POST into the gateway's
proprietary ``event`` block (FRD section 6.3 / SC-ING-061). It carries **no**
third-party dependencies: JSONPath lookups are a restricted dotted-path
traversal of the already-parsed body (e.g. ``object.id`` or ``event.type``),
not a full JSONPath engine.

The public surface is:

* :func:`resolve` - pull a single string value out of headers or the parsed
  body per a ``{"from": ..., "ref": ...}`` spec.
* :func:`synthesize_dedup_id` - a deterministic sha256 hex id for events that
  do not carry a provider-supplied id (the MCP-native synthesis rule,
  FRD section 6.3.2).
* :func:`build_envelope` - assemble a :class:`mcpgateway.schemas.EventEnvelope`
  with a reverse-DNS ``type``, a resolved/synthesized ``id``, a resolved
  ``subject``/``time``, and ``data`` set to the raw parsed provider body.

The descriptor argument is duck-typed: only its ``event_type`` / ``dedup_id`` /
``subject`` / ``time`` attributes are read, so any object matching the
``ProviderDescriptor`` contract works.
"""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timezone
import hashlib
from typing import Any, Mapping, Optional

# First-Party
from mcpgateway.schemas import EventEnvelope

__all__ = ["resolve", "synthesize_dedup_id", "build_envelope"]


def _lower_keyed(headers: Mapping[str, str]) -> dict:
    """Return a copy of ``headers`` keyed by lower-cased name.

    Args:
        headers: Request headers.

    Returns:
        dict: Mapping of lower-cased header name to value.
    """
    return {str(k).lower(): v for k, v in headers.items()}


def _traverse(parsed: Any, ref: str) -> Optional[str]:
    """Walk a dotted path through the parsed JSON body.

    The path is a restricted JSONPath: a sequence of dotted keys (e.g.
    ``data.object.id``). A leading ``$.`` or ``$`` from the FRD's JSONPath
    spelling is tolerated and stripped. Only mapping traversal is supported;
    a missing key, a non-mapping intermediate, or a non-scalar leaf yields
    ``None``.

    Args:
        parsed: The parsed JSON body (typically a dict).
        ref: Dotted path string.

    Returns:
        Optional[str]: The leaf value rendered as a string, or None.
    """
    if ref is None:
        return None
    path = ref
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:]
    path = path.strip(".")
    if not path:
        return None

    current: Any = parsed
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    if current is None or isinstance(current, (Mapping, list)):
        return None
    if isinstance(current, bool):
        # Avoid surprising "True"/"False" rendering; treat booleans as unresolvable scalars.
        return str(current)
    return str(current)


def resolve(spec: Optional[Mapping[str, Any]], *, parsed: Any, headers: Mapping[str, str]) -> Optional[str]:
    """Resolve a single value from headers or the parsed body.

    Args:
        spec: A ``{"from": "header"|"jsonpath", "ref": str}`` spec, or None.
        parsed: The parsed JSON body.
        headers: Request headers (case-insensitive for ``from="header"``).

    Returns:
        Optional[str]: The resolved value as a string, or None when unresolvable.

    Examples:
        >>> resolve({"from": "header", "ref": "x-evt"}, parsed={}, headers={"X-Evt": "push"})
        'push'
        >>> resolve({"from": "jsonpath", "ref": "a.b"}, parsed={"a": {"b": "v"}}, headers={})
        'v'
        >>> resolve(None, parsed={}, headers={}) is None
        True
    """
    if not spec:
        return None
    source = spec.get("from")
    ref = spec.get("ref")
    if ref is None:
        return None
    if source == "header":
        value = _lower_keyed(headers).get(str(ref).lower())
        return None if value is None else str(value)
    if source == "jsonpath":
        return _traverse(parsed, str(ref))
    return None


def synthesize_dedup_id(source: str, raw_body: bytes, headers: Mapping[str, str]) -> str:
    """Synthesize a deterministic dedup id for events lacking a provider id.

    The digest is a stable sha256 over the source, the exact raw body bytes,
    and the request headers (sorted, case-insensitive). Identical re-delivered
    events collapse to the same id; genuinely distinct events differ.

    Args:
        source: The event source (connection-scoped).
        raw_body: The exact raw request body bytes.
        headers: Request headers folded into the digest.

    Returns:
        str: A 64-character sha256 hex digest.

    Examples:
        >>> a = synthesize_dedup_id("s", b'{"x":1}', {})
        >>> b = synthesize_dedup_id("s", b'{"x":1}', {})
        >>> a == b and len(a) == 64
        True
        >>> synthesize_dedup_id("s", b'{"x":1}', {}) != synthesize_dedup_id("s", b'{"x":2}', {})
        True
    """
    hasher = hashlib.sha256()
    hasher.update(str(source).encode("utf-8"))
    hasher.update(b"\x00")
    hasher.update(raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body).encode("utf-8"))
    hasher.update(b"\x00")
    for name in sorted(headers, key=lambda k: str(k).lower()):
        hasher.update(str(name).lower().encode("utf-8"))
        hasher.update(b"=")
        hasher.update(str(headers[name]).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _build_type(event_type_spec: Mapping[str, Any], *, parsed: Any, headers: Mapping[str, str], provider: str) -> str:
    """Build the reverse-DNS ``event.type`` string from the event_type spec.

    Resolution order: resolve the raw provider type, then prefer an exact
    ``map`` override, then a ``template`` (``{type}`` substituted), then the
    raw type, then a provider-only fallback (``com.<provider>.unknown``).

    Args:
        event_type_spec: The descriptor ``event_type`` spec.
        parsed: The parsed JSON body.
        headers: Request headers.
        provider: Provider id (fallback prefix component).

    Returns:
        str: The reverse-DNS event type.
    """
    raw_type = resolve(event_type_spec, parsed=parsed, headers=headers)

    mapping = event_type_spec.get("map") if event_type_spec else None
    if mapping and raw_type is not None and raw_type in mapping:
        return str(mapping[raw_type])

    template = event_type_spec.get("template") if event_type_spec else None
    if template and raw_type is not None:
        return template.format(type=raw_type, event_type=raw_type)

    if raw_type is not None:
        return raw_type

    if template:
        # Template with no resolvable type still yields a stable, non-empty type.
        return template.format(type="unknown", event_type="unknown")
    return f"com.{provider}.unknown"


def _coerce_time(value: Optional[str]) -> Optional[datetime]:
    """Leniently coerce a resolved time value to a datetime, else None.

    Accepts RFC3339 strings (``Z`` normalized to ``+00:00``) and integer or
    float epoch seconds. Anything that cannot be parsed yields None rather than
    raising into the build path.

    Args:
        value: The resolved time value as a string, or None.

    Returns:
        Optional[datetime]: A parsed datetime, or None.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Epoch seconds (e.g. Stripe ``created``).
    try:
        if text.isdigit() or (text.lstrip("-").replace(".", "", 1).isdigit()):
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    # RFC3339 / ISO-8601.
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def build_envelope(*, descriptor: Any, raw_body: bytes, parsed: Any, headers: Mapping[str, str], source: str) -> EventEnvelope:
    """Normalize a verified, parsed provider POST into an EventEnvelope.

    Args:
        descriptor: A ProviderDescriptor-compatible object exposing
            ``event_type`` / ``dedup_id`` / ``subject`` / ``time`` specs.
        raw_body: The exact raw request body bytes (used for id synthesis).
        parsed: The parsed JSON body (becomes ``data`` and feeds jsonpath).
        headers: Request headers.
        source: The resolved ``event.source`` (connection-scoped, set by ingress).

    Returns:
        EventEnvelope: The normalized event block with a reverse-DNS ``type``,
        a resolved/synthesized ``id``, resolved ``subject``/``time``, and
        ``data`` set to the raw parsed provider body.
    """
    provider = getattr(descriptor, "id", "") or ""

    event_type_spec = getattr(descriptor, "event_type", None) or {}
    ce_type = _build_type(event_type_spec, parsed=parsed, headers=headers, provider=provider)

    ce_id = resolve(getattr(descriptor, "dedup_id", None), parsed=parsed, headers=headers)
    if ce_id is None or ce_id == "__synthesize__":
        ce_id = synthesize_dedup_id(source, raw_body, headers)

    subject = resolve(getattr(descriptor, "subject", None), parsed=parsed, headers=headers)
    occurred = _coerce_time(resolve(getattr(descriptor, "time", None), parsed=parsed, headers=headers))

    return EventEnvelope(
        id=ce_id,
        source=source,
        type=ce_type,
        subject=subject,
        time=occurred,
        data=parsed,
    )
