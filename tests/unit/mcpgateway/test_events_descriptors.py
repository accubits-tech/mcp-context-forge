# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_descriptors.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Unit tests for the provider descriptor model and registry used by the
config-driven event ingress layer (``mcpgateway.services.events.descriptors``).
"""

# Standard
import os

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events.descriptors import (
    BUILTIN_DESCRIPTORS,
    ProviderDescriptor,
    get_descriptor,
    load_descriptors_from_dir,
)


# --------------------------------------------------------------------------- #
# Builtins exist and carry the expected verify recipes / extraction config     #
# --------------------------------------------------------------------------- #
def test_builtins_present():
    for ref in ("github", "stripe", "slack"):
        assert ref in BUILTIN_DESCRIPTORS
        desc = BUILTIN_DESCRIPTORS[ref]
        assert isinstance(desc, ProviderDescriptor)
        assert desc.id == ref


def test_github_descriptor_recipe():
    desc = BUILTIN_DESCRIPTORS["github"]
    assert desc.verify["strategy"] == "hmac"
    assert desc.verify["header"] == "X-Hub-Signature-256"
    assert desc.verify["algo"] == "sha256"
    assert desc.verify["encoding"] == "hex"
    assert desc.verify["prefix"] == "sha256="
    # event type comes from a request header
    assert desc.event_type["from"] == "header"
    assert desc.event_type["ref"] == "X-GitHub-Event"
    assert desc.event_type["template"] == "com.github.{event_type}"
    # dedup id from the GitHub delivery GUID header
    assert desc.dedup_id["from"] == "header"
    assert desc.dedup_id["ref"] == "X-GitHub-Delivery"
    # GitHub ping is a no-op (ack, no event)
    assert desc.noop is not None
    assert desc.noop["from"] == "header"
    assert desc.noop["ref"] == "X-GitHub-Event"
    assert "ping" in desc.noop["values"]
    # GitHub has no first-touch handshake
    assert desc.handshake is None


def test_stripe_descriptor_recipe():
    desc = BUILTIN_DESCRIPTORS["stripe"]
    assert desc.verify["strategy"] == "hmac_timestamped"
    assert desc.verify["header"] == "Stripe-Signature"
    assert desc.verify["signature_scheme"] == "stripe"
    assert desc.verify["algo"] == "sha256"
    assert desc.verify["encoding"] == "hex"
    # event type taken from JSON body and namespaced via a template
    assert desc.event_type["from"] == "jsonpath"
    assert desc.event_type["ref"] == "type"
    assert desc.event_type["template"] == "com.stripe.{event_type}"
    # dedup id is the Stripe event id
    assert desc.dedup_id["from"] == "jsonpath"
    assert desc.dedup_id["ref"] == "id"


def test_slack_descriptor_recipe():
    desc = BUILTIN_DESCRIPTORS["slack"]
    assert desc.verify["strategy"] == "hmac_timestamped"
    assert desc.verify["header"] == "X-Slack-Signature"
    assert desc.verify["signature_scheme"] == "slack"
    assert desc.verify["prefix"] == "v0="
    assert desc.verify["timestamp_header"] == "X-Slack-Request-Timestamp"
    # event type from nested JSON body
    assert desc.event_type["from"] == "jsonpath"
    assert desc.event_type["ref"] == "event.type"
    assert desc.event_type["template"] == "com.slack.{event_type}"
    # dedup id is the Slack event_id
    assert desc.dedup_id["from"] == "jsonpath"
    assert desc.dedup_id["ref"] == "event_id"
    # url_verification first-touch handshake
    assert desc.handshake is not None
    assert desc.handshake["match"]["ref"] == "type"
    assert desc.handshake["match"]["equals"] == "url_verification"
    assert desc.handshake["echo"]["ref"] == "challenge"


# --------------------------------------------------------------------------- #
# get_descriptor                                                              #
# --------------------------------------------------------------------------- #
def test_get_descriptor_builtins():
    for ref in ("github", "stripe", "slack"):
        desc = get_descriptor(ref)
        assert desc is not None
        assert desc.id == ref


def test_get_descriptor_unknown_returns_none():
    assert get_descriptor("nope") is None


# --------------------------------------------------------------------------- #
# YAML loader: tolerate missing dir, parse files, overlay builtins            #
# --------------------------------------------------------------------------- #
def test_load_descriptors_missing_dir_returns_empty():
    assert load_descriptors_from_dir("/nonexistent/path/that/should/not/exist") == {}


def test_load_descriptors_from_dir_round_trip(tmp_path):
    yaml_text = """
display_name: ACME Webhooks
verify:
  strategy: hmac
  header: X-Acme-Signature
  algo: sha256
  encoding: hex
event_type:
  from: header
  ref: X-Acme-Event
  template: com.acme.{event_type}
dedup_id:
  from: header
  ref: X-Acme-Delivery
"""
    (tmp_path / "acme.yaml").write_text(yaml_text, encoding="utf-8")
    loaded = load_descriptors_from_dir(str(tmp_path))
    assert "acme" in loaded
    desc = loaded["acme"]
    assert isinstance(desc, ProviderDescriptor)
    # id derives from the filename stem
    assert desc.id == "acme"
    assert desc.display_name == "ACME Webhooks"
    assert desc.verify["header"] == "X-Acme-Signature"
    assert desc.event_type["template"] == "com.acme.{event_type}"


def test_loaded_yaml_overlays_builtins(tmp_path):
    # A YAML descriptor whose stem matches a builtin overrides that builtin.
    yaml_text = """
verify:
  strategy: hmac
  header: X-Custom-Github
  algo: sha256
  encoding: hex
event_type:
  from: header
  ref: X-GitHub-Event
  template: com.github.{event_type}
dedup_id:
  from: header
  ref: X-GitHub-Delivery
"""
    (tmp_path / "github.yml").write_text(yaml_text, encoding="utf-8")

    # The new descriptor extends/overlays the builtin set.
    overlaid = get_descriptor("github", descriptors_dir=str(tmp_path))
    assert overlaid is not None
    assert overlaid.verify["header"] == "X-Custom-Github"

    # Builtins for other refs still resolve through the same overlay call.
    assert get_descriptor("stripe", descriptors_dir=str(tmp_path)) is not None

    # Without the overlay the builtin is unchanged.
    assert get_descriptor("github").verify["header"] == "X-Hub-Signature-256"


def test_provider_descriptor_defaults():
    desc = ProviderDescriptor(id="x", verify={"strategy": "none"}, event_type={"from": "const", "ref": "x"})
    assert desc.display_name == ""
    assert desc.dedup_id is None
    assert desc.subject is None
    assert desc.handshake is None
    assert desc.noop is None
    assert desc.plugin_ref is None
    assert desc.extra_oauth_scopes == []
