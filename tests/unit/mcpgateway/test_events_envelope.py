# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_envelope.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the event envelope normalizer (FRD section 6.3 / SC-ING-061).

These tests feed GitHub/Stripe/Slack-shaped parsed bodies and headers to the
dependency-free dotted-path resolver and the envelope builder, asserting:

* ``type`` is the correct reverse-DNS string (``com.github.push``,
  ``com.stripe.payment_intent.succeeded``, ``com.slack.<...>``);
* ``id`` is taken from the configured field when present;
* ``id`` is synthesized when absent (deterministic + differs across distinct
  bodies);
* ``subject`` is resolved from the configured field;
* ``data`` preserves the raw parsed provider body.
"""

# Future
from __future__ import annotations

# Standard
from types import SimpleNamespace

# Third-Party
import pytest

# First-Party
from mcpgateway.schemas import EventEnvelope
from mcpgateway.services.events.envelope import (
    build_envelope,
    resolve,
    synthesize_dedup_id,
)


# ---------------------------------------------------------------------------
# Lightweight descriptor stub
#
# build_envelope only reads the ``event_type`` / ``dedup_id`` / ``subject`` /
# ``time`` attributes of the descriptor, so a SimpleNamespace duck-types the
# contract's ProviderDescriptor without coupling these tests to the (separately
# owned) descriptors module. A parallel block below additionally exercises the
# real BUILTIN_DESCRIPTORS when that module is importable.
# ---------------------------------------------------------------------------
def _descriptor(*, event_type, dedup_id=None, subject=None, time=None, id="prov"):
    """Build a duck-typed ProviderDescriptor-compatible stub.

    Args:
        event_type: ``event_type`` spec dict.
        dedup_id: Optional ``dedup_id`` spec dict.
        subject: Optional ``subject`` spec dict.
        time: Optional ``time`` spec dict.
        id: Provider id string.

    Returns:
        SimpleNamespace: Object exposing the descriptor attributes build_envelope reads.
    """
    return SimpleNamespace(
        id=id,
        event_type=event_type,
        dedup_id=dedup_id,
        subject=subject,
        time=time,
    )


GITHUB_DESCRIPTOR = _descriptor(
    id="github",
    event_type={"from": "header", "ref": "X-GitHub-Event", "template": "com.github.{type}"},
    dedup_id={"from": "header", "ref": "X-GitHub-Delivery"},
    subject={"from": "jsonpath", "ref": "repository.full_name"},
)

STRIPE_DESCRIPTOR = _descriptor(
    id="stripe",
    event_type={"from": "jsonpath", "ref": "type", "template": "com.stripe.{type}"},
    dedup_id={"from": "jsonpath", "ref": "id"},
    subject={"from": "jsonpath", "ref": "data.object.id"},
    time={"from": "jsonpath", "ref": "created"},
)

SLACK_DESCRIPTOR = _descriptor(
    id="slack",
    event_type={"from": "jsonpath", "ref": "event.type", "template": "com.slack.{type}"},
    dedup_id={"from": "jsonpath", "ref": "event_id"},
    subject={"from": "jsonpath", "ref": "event.channel"},
)


GITHUB_BODY = {"repository": {"full_name": "octo/repo"}, "ref": "refs/heads/main"}
GITHUB_HEADERS = {"X-GitHub-Event": "push", "X-GitHub-Delivery": "delivery-guid-123"}

STRIPE_BODY = {
    "id": "evt_1abc",
    "type": "payment_intent.succeeded",
    "created": 1717000000,
    "data": {"object": {"id": "pi_42"}},
}
STRIPE_HEADERS = {"Stripe-Signature": "t=1,v1=x"}

SLACK_BODY = {
    "event_id": "Ev0001",
    "team_id": "T123",
    "event": {"type": "message", "channel": "C42", "text": "hi"},
}
SLACK_HEADERS = {"X-Slack-Signature": "v0=abc"}


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------
def test_resolve_header_is_case_insensitive():
    """A header lookup matches regardless of header-name casing."""
    spec = {"from": "header", "ref": "x-github-event"}
    assert resolve(spec, parsed={}, headers={"X-GitHub-Event": "push"}) == "push"


def test_resolve_jsonpath_dotted_traversal():
    """A dotted jsonpath traverses nested dict keys."""
    spec = {"from": "jsonpath", "ref": "data.object.id"}
    assert resolve(spec, parsed=STRIPE_BODY, headers={}) == "pi_42"


def test_resolve_missing_returns_none():
    """An unresolvable spec returns None rather than raising."""
    assert resolve({"from": "jsonpath", "ref": "nope.here"}, parsed={}, headers={}) is None
    assert resolve({"from": "header", "ref": "X-Absent"}, parsed={}, headers={}) is None
    assert resolve(None, parsed={}, headers={}) is None


def test_resolve_jsonpath_supports_leading_dollar_dot():
    """A jsonpath ref may carry the FRD's leading ``$.`` and still resolve."""
    spec = {"from": "jsonpath", "ref": "$.repository.full_name"}
    assert resolve(spec, parsed=GITHUB_BODY, headers={}) == "octo/repo"


# ---------------------------------------------------------------------------
# synthesize_dedup_id()
# ---------------------------------------------------------------------------
def test_synthesize_dedup_id_is_deterministic():
    """The same source + body + headers yields the same id."""
    a = synthesize_dedup_id("github", b'{"x":1}', {"X-GitHub-Event": "push"})
    b = synthesize_dedup_id("github", b'{"x":1}', {"X-GitHub-Event": "push"})
    assert a == b
    # sha256 hex digest
    assert len(a) == 64
    int(a, 16)  # parses as hex


def test_synthesize_dedup_id_differs_for_distinct_bodies():
    """Distinct bodies yield distinct ids."""
    a = synthesize_dedup_id("github", b'{"x":1}', {})
    b = synthesize_dedup_id("github", b'{"x":2}', {})
    assert a != b


def test_synthesize_dedup_id_differs_for_distinct_sources():
    """Distinct sources yield distinct ids for an identical body."""
    a = synthesize_dedup_id("github", b'{"x":1}', {})
    b = synthesize_dedup_id("stripe", b'{"x":1}', {})
    assert a != b


# ---------------------------------------------------------------------------
# build_envelope() — GitHub (SC-ING-061)
# ---------------------------------------------------------------------------
def test_build_envelope_github_push():
    """GitHub push normalizes to com.github.push with header-sourced id/subject."""
    env = build_envelope(
        descriptor=GITHUB_DESCRIPTOR,
        raw_body=b'{"repository":{"full_name":"octo/repo"}}',
        parsed=GITHUB_BODY,
        headers=GITHUB_HEADERS,
        source="https://github.com/octo/repo",
    )
    assert isinstance(env, EventEnvelope)
    assert env.type == "com.github.push"
    assert env.id == "delivery-guid-123"
    assert env.subject == "octo/repo"
    assert env.source == "https://github.com/octo/repo"
    assert env.data == GITHUB_BODY


# ---------------------------------------------------------------------------
# build_envelope() — Stripe
# ---------------------------------------------------------------------------
def test_build_envelope_stripe_payment_intent():
    """Stripe normalizes to the dotted reverse-DNS type via the template."""
    env = build_envelope(
        descriptor=STRIPE_DESCRIPTOR,
        raw_body=b"{}",
        parsed=STRIPE_BODY,
        headers=STRIPE_HEADERS,
        source="stripe",
    )
    assert env.type == "com.stripe.payment_intent.succeeded"
    assert env.id == "evt_1abc"
    assert env.subject == "pi_42"
    assert env.data == STRIPE_BODY


# ---------------------------------------------------------------------------
# build_envelope() — Slack
# ---------------------------------------------------------------------------
def test_build_envelope_slack_message():
    """Slack normalizes the nested event.type to com.slack.message."""
    env = build_envelope(
        descriptor=SLACK_DESCRIPTOR,
        raw_body=b"{}",
        parsed=SLACK_BODY,
        headers=SLACK_HEADERS,
        source="https://slack.com/T123",
    )
    assert env.type == "com.slack.message"
    assert env.id == "Ev0001"
    assert env.subject == "C42"
    assert env.data == SLACK_BODY


# ---------------------------------------------------------------------------
# build_envelope() — id synthesis when dedup_id is absent / unresolvable
# ---------------------------------------------------------------------------
def test_build_envelope_synthesizes_id_when_dedup_absent():
    """When the dedup id cannot be resolved, a deterministic id is synthesized."""
    desc = _descriptor(
        id="github",
        event_type={"from": "header", "ref": "X-GitHub-Event", "template": "com.github.{type}"},
        dedup_id={"from": "header", "ref": "X-GitHub-Delivery"},  # header absent below
    )
    raw = b'{"repository":{"full_name":"octo/repo"}}'
    env1 = build_envelope(
        descriptor=desc,
        raw_body=raw,
        parsed=GITHUB_BODY,
        headers={"X-GitHub-Event": "push"},  # no delivery header
        source="src",
    )
    env2 = build_envelope(
        descriptor=desc,
        raw_body=raw,
        parsed=GITHUB_BODY,
        headers={"X-GitHub-Event": "push"},
        source="src",
    )
    assert env1.id == env2.id  # deterministic
    assert env1.id == synthesize_dedup_id("src", raw, {"X-GitHub-Event": "push"})


def test_build_envelope_synthesized_ids_differ_for_distinct_bodies():
    """Synthesized ids differ when the raw body differs."""
    desc = _descriptor(
        id="x",
        event_type={"from": "jsonpath", "ref": "type", "template": "com.x.{type}"},
        dedup_id=None,
    )
    env_a = build_envelope(descriptor=desc, raw_body=b'{"type":"a"}', parsed={"type": "a"}, headers={}, source="s")
    env_b = build_envelope(descriptor=desc, raw_body=b'{"type":"b"}', parsed={"type": "b"}, headers={}, source="s")
    assert env_a.id != env_b.id


# ---------------------------------------------------------------------------
# build_envelope() — event_type map overrides take precedence over template
# ---------------------------------------------------------------------------
def test_build_envelope_map_override_wins_over_template():
    """An exact ``map`` entry overrides the template for messy provider types."""
    desc = _descriptor(
        id="github",
        event_type={
            "from": "header",
            "ref": "X-GitHub-Event",
            "template": "com.github.{type}",
            "map": {"pull_request": "com.github.pull_request.opened"},
        },
        dedup_id={"from": "header", "ref": "X-GitHub-Delivery"},
    )
    env = build_envelope(
        descriptor=desc,
        raw_body=b"{}",
        parsed={},
        headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d1"},
        source="s",
    )
    assert env.type == "com.github.pull_request.opened"


def test_build_envelope_time_resolved_when_parseable():
    """An RFC3339 time string is resolved onto the envelope's datetime field."""
    desc = _descriptor(
        id="x",
        event_type={"from": "jsonpath", "ref": "type", "template": "com.x.{type}"},
        dedup_id={"from": "jsonpath", "ref": "id"},
        time={"from": "jsonpath", "ref": "ts"},
    )
    body = {"type": "t", "id": "i", "ts": "2026-05-30T12:00:00Z"}
    env = build_envelope(descriptor=desc, raw_body=b"{}", parsed=body, headers={}, source="s")
    assert env.time is not None
    assert env.time.year == 2026 and env.time.month == 5 and env.time.day == 30


def test_build_envelope_time_none_when_unparseable():
    """A non-datetime time value leaves the envelope time as None (lenient)."""
    desc = _descriptor(
        id="x",
        event_type={"from": "jsonpath", "ref": "type", "template": "com.x.{type}"},
        dedup_id={"from": "jsonpath", "ref": "id"},
        time={"from": "jsonpath", "ref": "ts"},
    )
    body = {"type": "t", "id": "i", "ts": "not-a-timestamp"}
    env = build_envelope(descriptor=desc, raw_body=b"{}", parsed=body, headers={}, source="s")
    assert env.time is None


# ---------------------------------------------------------------------------
# Real BUILTIN_DESCRIPTORS exercise (only if the descriptors module is present)
# ---------------------------------------------------------------------------
def test_build_envelope_with_builtin_descriptors_if_available():
    """When the descriptors module is present, builtins normalize correctly."""
    builtins = pytest.importorskip("mcpgateway.services.events.descriptors").BUILTIN_DESCRIPTORS

    gh = builtins["github"]
    env = build_envelope(
        descriptor=gh,
        raw_body=b'{"repository":{"full_name":"octo/repo"}}',
        parsed=GITHUB_BODY,
        headers=GITHUB_HEADERS,
        source="https://github.com/octo/repo",
    )
    assert env.type == "com.github.push"
    assert env.data == GITHUB_BODY

    stripe = builtins["stripe"]
    env = build_envelope(
        descriptor=stripe,
        raw_body=b"{}",
        parsed=STRIPE_BODY,
        headers=STRIPE_HEADERS,
        source="stripe",
    )
    assert env.type == "com.stripe.payment_intent.succeeded"

    slack = builtins["slack"]
    env = build_envelope(
        descriptor=slack,
        raw_body=b"{}",
        parsed=SLACK_BODY,
        headers=SLACK_HEADERS,
        source="https://slack.com/T123",
    )
    assert env.type == "com.slack.message"
