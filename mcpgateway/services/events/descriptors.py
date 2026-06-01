# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/descriptors.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Provider descriptors for config-driven event ingress.

A provider descriptor is a declarative recipe that tells the generic webhook
ingress route how to *verify*, *classify*, and *normalize* an inbound provider
POST. Adding a new provider is therefore configuration (a YAML recipe plus a
per-connection stored secret), not code. The descriptor grammar collapses the
finite set of axes providers vary along:

* how the payload is signed (``verify``),
* where the event type lives and how it is namespaced (``event_type``),
* how to deduplicate (``dedup_id``),
* optional ``subject`` / ``time`` extraction,
* an optional first-touch challenge (``handshake``, e.g. Slack
  ``url_verification``),
* an optional no-op acknowledgement (``noop``, e.g. GitHub ``ping``),
* and an escape hatch (``plugin_ref``) for providers outside the grammar.

This module ships built-in descriptors for GitHub, Stripe, and Slack (the
recipes encoded directly from the worked examples in the triggers/events FRD),
a YAML loader so deployments can add or override descriptors without code
changes, and ``get_descriptor`` which resolves a reference against the built-in
set overlaid by any loaded YAML.

Examples:
    >>> get_descriptor("github").verify["header"]
    'X-Hub-Signature-256'
    >>> get_descriptor("nope") is None
    True
"""

# Future
from __future__ import annotations

# Standard
import os
from typing import Any, Dict, List, Optional

# Third-Party
from pydantic import BaseModel, Field
import yaml

__all__ = [
    "ProviderDescriptor",
    "BUILTIN_DESCRIPTORS",
    "load_descriptors_from_dir",
    "get_descriptor",
]


class ProviderDescriptor(BaseModel):
    """Declarative recipe describing how to verify and normalize a provider webhook.

    Attributes:
        id: Short provider id (lowercase). Doubles as the default ``com.<id>.*``
            type-namespace component and, for YAML descriptors, the filename stem.
        display_name: Human-friendly name for admin surfaces.
        verify: Signature-verification recipe. Keys: ``strategy``
            (``hmac``/``hmac_timestamped``/``none``/``plugin``), ``header``,
            ``algo`` (default ``sha256``), ``encoding`` (``hex``/``base64``),
            ``prefix``, ``signed_payload``, ``timestamp_header``,
            ``signature_scheme`` (``simple``/``stripe``/``slack``),
            ``allow_unsigned`` (default ``False``), ``plugin_ref``.
        event_type: How to derive the raw event type and namespace it. Keys:
            ``from`` (``header``/``jsonpath``/``const``), ``ref`` (header name or
            dotted JSON path), and either ``map`` (exact raw->reverse-DNS remap)
            or ``template`` (e.g. ``com.stripe.{event_type}``).
        dedup_id: How to derive the dedup key (``event.id``). Keys: ``from``,
            ``ref``. ``None`` means synthesize a deterministic id.
        subject: Optional ``event.subject`` extraction (``from``/``ref``).
        time: Optional ``event.time`` extraction (``from``/``ref``).
        handshake: Optional first-touch challenge (e.g. Slack
            ``url_verification``). Keys: ``match`` (``{ref, equals}``) and
            ``echo`` (``{ref}``).
        noop: Optional no-event acknowledgement (e.g. GitHub ``ping``). Keys:
            ``from``, ``ref``, ``values``.
        plugin_ref: Named plugin handling verification for the ``plugin`` escape
            hatch.
        extra_oauth_scopes: Extra OAuth scopes required to enable provider-side
            event delivery.

    Examples:
        >>> d = ProviderDescriptor(id="x", verify={"strategy": "none"},
        ...                        event_type={"from": "const", "ref": "x"})
        >>> d.display_name
        ''
        >>> d.dedup_id is None
        True
        >>> d.extra_oauth_scopes
        []
    """

    id: str
    display_name: str = ""
    verify: Dict[str, Any]
    event_type: Dict[str, Any]
    dedup_id: Optional[Dict[str, Any]] = None
    subject: Optional[Dict[str, Any]] = None
    time: Optional[Dict[str, Any]] = None
    handshake: Optional[Dict[str, Any]] = None
    noop: Optional[Dict[str, Any]] = None
    plugin_ref: Optional[str] = None
    extra_oauth_scopes: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Built-in descriptors (verify recipes encoded from FRD §6.4/§6.5)            #
# --------------------------------------------------------------------------- #
#
# GitHub (FRD §6.4): signs the raw body with the per-repo secret and sends
# ``sha256=<hex>`` in ``X-Hub-Signature-256``. The event type is in
# ``X-GitHub-Event``; the delivery GUID in ``X-GitHub-Delivery`` is a perfect
# dedup id. A ``ping`` event type is a provider liveness probe and carries no
# domain event, so it is acknowledged as a no-op.
_GITHUB = ProviderDescriptor(
    id="github",
    display_name="GitHub",
    verify={
        "strategy": "hmac",
        "header": "X-Hub-Signature-256",
        "algo": "sha256",
        "encoding": "hex",
        "prefix": "sha256=",
        "signed_payload": "{body}",
    },
    event_type={
        "from": "header",
        "ref": "X-GitHub-Event",
        "template": "com.github.{event_type}",
    },
    dedup_id={"from": "header", "ref": "X-GitHub-Delivery"},
    subject={"from": "jsonpath", "ref": "repository.full_name"},
    noop={"from": "header", "ref": "X-GitHub-Event", "values": ["ping"]},
    extra_oauth_scopes=["admin:repo_hook"],
)

# Stripe: signs ``{timestamp}.{body}`` and sends the timestamp + signatures in a
# single ``Stripe-Signature`` header shaped ``t=NNN,v1=aaa,v1=bbb`` (multiple
# ``v1`` candidates support secret rotation). The event type lives at ``$.type``
# and the dedup id at ``$.id`` in the JSON body.
_STRIPE = ProviderDescriptor(
    id="stripe",
    display_name="Stripe",
    verify={
        "strategy": "hmac_timestamped",
        "header": "Stripe-Signature",
        "algo": "sha256",
        "encoding": "hex",
        "signature_scheme": "stripe",
        "signed_payload": "{timestamp}.{body}",
    },
    event_type={
        "from": "jsonpath",
        "ref": "type",
        "template": "com.stripe.{event_type}",
    },
    dedup_id={"from": "jsonpath", "ref": "id"},
    subject={"from": "jsonpath", "ref": "data.object.id"},
    time={"from": "jsonpath", "ref": "created"},
)

# Slack (FRD §6.5): signs ``v0:{timestamp}:{body}`` with the signing secret,
# sends ``v0=<hex>`` in ``X-Slack-Signature`` and the timestamp in
# ``X-Slack-Request-Timestamp`` (replay window). On endpoint setup Slack POSTs
# ``{"type":"url_verification","challenge":"..."}`` and expects the
# ``challenge`` echoed back (handled only inside the authed enable flow).
_SLACK = ProviderDescriptor(
    id="slack",
    display_name="Slack",
    verify={
        "strategy": "hmac_timestamped",
        "header": "X-Slack-Signature",
        "algo": "sha256",
        "encoding": "hex",
        "prefix": "v0=",
        "signature_scheme": "slack",
        "timestamp_header": "X-Slack-Request-Timestamp",
        "signed_payload": "v0:{timestamp}:{body}",
    },
    event_type={
        "from": "jsonpath",
        "ref": "event.type",
        "template": "com.slack.{event_type}",
    },
    dedup_id={"from": "jsonpath", "ref": "event_id"},
    subject={"from": "jsonpath", "ref": "event.channel"},
    time={"from": "jsonpath", "ref": "event_time"},
    handshake={
        "match": {"ref": "type", "equals": "url_verification"},
        "echo": {"ref": "challenge"},
    },
)

BUILTIN_DESCRIPTORS: Dict[str, ProviderDescriptor] = {
    "github": _GITHUB,
    "stripe": _STRIPE,
    "slack": _SLACK,
}


def load_descriptors_from_dir(path: str) -> Dict[str, ProviderDescriptor]:
    """Load provider descriptors from a directory of YAML files.

    Each ``*.yaml`` / ``*.yml`` file is parsed into a :class:`ProviderDescriptor`
    whose ``id`` is the filename stem (an explicit ``id`` key in the document is
    ignored in favour of the stem so the on-disk name is authoritative). A
    missing directory yields an empty mapping; the loader never raises into the
    request path.

    Args:
        path: Directory containing descriptor YAML files.

    Returns:
        Mapping of descriptor id (filename stem) to :class:`ProviderDescriptor`.

    Examples:
        >>> load_descriptors_from_dir("/nonexistent/dir") == {}
        True
    """
    result: Dict[str, ProviderDescriptor] = {}
    if not path or not os.path.isdir(path):
        return result

    for entry in sorted(os.listdir(path)):
        stem, ext = os.path.splitext(entry)
        if ext.lower() not in (".yaml", ".yml"):
            continue
        file_path = os.path.join(path, entry)
        if not os.path.isfile(file_path):
            continue
        with open(file_path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        if not isinstance(data, dict):
            continue
        # The filename stem is authoritative for the descriptor id.
        data = {k: v for k, v in data.items() if k != "id"}
        result[stem] = ProviderDescriptor(id=stem, **data)
    return result


def get_descriptor(ref: str, descriptors_dir: Optional[str] = None) -> Optional[ProviderDescriptor]:
    """Resolve a descriptor reference against built-ins overlaid by loaded YAML.

    The built-in descriptors form the base set; any descriptor loaded from
    ``descriptors_dir`` overrides or extends a built-in with the same id.

    Args:
        ref: Descriptor reference (e.g. ``"github"``).
        descriptors_dir: Optional directory of YAML descriptors that overlay the
            built-in set. Defaults to ``MCPGATEWAY_EVENTS_DESCRIPTORS_DIR`` when
            set, otherwise no overlay is applied.

    Returns:
        The resolved :class:`ProviderDescriptor`, or ``None`` if unknown.

    Examples:
        >>> get_descriptor("slack").id
        'slack'
        >>> get_descriptor("nope") is None
        True
    """
    registry: Dict[str, ProviderDescriptor] = dict(BUILTIN_DESCRIPTORS)

    overlay_dir = descriptors_dir if descriptors_dir is not None else os.environ.get("MCPGATEWAY_EVENTS_DESCRIPTORS_DIR")
    if overlay_dir:
        registry.update(load_descriptors_from_dir(overlay_dir))

    return registry.get(ref)
