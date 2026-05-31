# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/test_events_cel_filter.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Test-suite for **mcpgateway.services.events.cel_filter**.

CEL is the gateway-side subscription filter dialect (FRD section 7.4). These
tests pin the security/correctness contract the delivery worker depends on:

* ``compile_filter`` is *validating admission* (FRD PG-FR-O): it raises
  ``FilterCompileError`` on syntax errors, on statically-detectable type
  mismatches, and on over-budget / over-deep expressions (a static complexity
  guard, ReDoS / cost-amplification defense).
* ``evaluate`` is *fail-closed* (FRD FR-18/FR-19): any runtime error (missing
  field, bad overload) yields ``False`` rather than crashing the delivery loop,
  while honoring CEL short-circuit semantics.
* ``match_event_type`` is a reverse-DNS *segment* glob: anchored,
  case-sensitive, and ReDoS-safe by construction (segment comparison, never a
  quantified regex).

The CEL activation (``ctx``) shape is
``{"event": {...}, "data": <body>, "type", "source", "subject"}``.

These cover the M2-gating SUB CEL scenarios SC-SUB-032..046 / SC-SUB-043
(test cases TC-SUB-027..039).

Run with::

    uv run pytest tests/unit/mcpgateway/test_events_cel_filter.py -q
"""

# Future
from __future__ import annotations

# Third-Party
import pytest

# First-Party
from mcpgateway.services.events import cel_filter as cel
from mcpgateway.services.events.cel_filter import (
    compile_filter,
    evaluate,
    FilterCompileError,
    match_event_type,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #
def _ctx(data: dict | None = None, *, type_: str = "com.stripe.charge.succeeded", source: str = "https://stripe.example", subject: str = "ch_1") -> dict:
    """Build a minimal CEL activation dict in the documented ctx shape."""
    body = {} if data is None else data
    return {
        "event": {"id": "evt_1", "type": type_, "source": source, "subject": subject, "data": body},
        "data": body,
        "type": type_,
        "source": source,
        "subject": subject,
    }


def _compiled_eval(expr: str, ctx: dict) -> bool:
    """Compile *expr* then evaluate it against *ctx* (the worker's two-step flow)."""
    return evaluate(compile_filter(expr), ctx)


# --------------------------------------------------------------------------- #
# TC-SUB-027 — CEL valid match true/false (SC-SUB-032/033)                    #
# --------------------------------------------------------------------------- #
def test_tc_sub_027_amount_gt_100_true_then_false():
    """``data.amount > 100`` delivers on 150, does not on 50."""
    compiled = compile_filter("data.amount > 100")
    assert evaluate(compiled, _ctx({"amount": 150})) is True
    assert evaluate(compiled, _ctx({"amount": 50})) is False


# --------------------------------------------------------------------------- #
# TC-SUB-028 — malformed expr rejected at compile/create (SC-SUB-034)         #
# --------------------------------------------------------------------------- #
def test_tc_sub_028_malformed_expr_raises_filter_compile_error():
    """A syntactically invalid expression is rejected at compile time (422)."""
    with pytest.raises(FilterCompileError):
        compile_filter("data.amount >")


def test_tc_sub_028_empty_and_nonbool_handling():
    """An empty/blank expression is a compile error (no implicit catch-all)."""
    with pytest.raises(FilterCompileError):
        compile_filter("   ")


# --------------------------------------------------------------------------- #
# TC-SUB-029 — valid-syntax runtime-error -> fail-closed False (SC-SUB-035)   #
# --------------------------------------------------------------------------- #
def test_tc_sub_029_runtime_error_fails_closed():
    """A valid expr that errors at eval (missing field) yields False, no raise."""
    compiled = compile_filter('data.missing == "y"')
    # data has no "missing" key -> CEL runtime error -> fail-closed no-match.
    assert evaluate(compiled, _ctx({"present": 1})) is False


def test_tc_sub_029_eval_never_raises_on_garbage_ctx():
    """evaluate must never propagate; a wholly unusable ctx still returns False."""
    compiled = compile_filter("data.amount > 100")
    assert evaluate(compiled, {"data": object()}) is False


# --------------------------------------------------------------------------- #
# TC-SUB-030 — has() guard, absent field -> no-match, no exception (SC-036)   #
# --------------------------------------------------------------------------- #
def test_tc_sub_030_has_guard_absent_field_no_match_no_exception():
    """``has(data.x) && data.x == "y"`` with x absent is False, not an error."""
    compiled = compile_filter('has(data.x) && data.x == "y"')
    assert evaluate(compiled, _ctx({"other": 1})) is False


def test_tc_sub_030_has_guard_present_matches():
    """The same guarded expr matches when the field is present and equal."""
    compiled = compile_filter('has(data.x) && data.x == "y"')
    assert evaluate(compiled, _ctx({"x": "y"})) is True


# --------------------------------------------------------------------------- #
# TC-SUB-031 — no_matching_overload at runtime -> no crash (SC-SUB-037)       #
# --------------------------------------------------------------------------- #
def test_tc_sub_031_runtime_no_matching_overload_no_crash():
    """A data-dependent overload error at eval is absorbed as fail-closed."""
    # data.s is a string at runtime; comparing to an int has no overload.
    compiled = compile_filter("data.s > 1")
    assert evaluate(compiled, _ctx({"s": "hello"})) is False


# --------------------------------------------------------------------------- #
# TC-SUB-032 — over-budget expr rejected at compile (SC-SUB-038)              #
# --------------------------------------------------------------------------- #
def test_tc_sub_032_over_budget_expression_rejected_at_compile():
    """A pathologically large conjunction is rejected by the static cost guard."""
    big = " && ".join(f"data.k{i} == {i}" for i in range(400))
    with pytest.raises(FilterCompileError):
        compile_filter(big)


def test_tc_sub_032_modest_expression_compiles():
    """A modest expression well under budget compiles without error."""
    compiled = compile_filter('type == "com.stripe.charge.succeeded" && data.amount > 100')
    assert evaluate(compiled, _ctx({"amount": 150})) is True


# --------------------------------------------------------------------------- #
# TC-SUB-033 — statically-detectable type mismatch -> compile reject (SC-039) #
# --------------------------------------------------------------------------- #
def test_tc_sub_033_type_mismatch_on_bound_attr_rejected_at_compile():
    """Comparing a known-string envelope attr to an int is a compile-time type error."""
    with pytest.raises(FilterCompileError):
        compile_filter("type > 1")


def test_tc_sub_033_literal_type_mismatch_rejected_at_compile():
    """A literal-only type mismatch (no data dependency) is rejected at compile."""
    with pytest.raises(FilterCompileError):
        compile_filter('1 > "x"')


def test_tc_sub_033_data_typed_mismatch_fails_closed_at_runtime():
    """A data-typed mismatch can only be known at runtime; it fails closed there."""
    # data.s typed-mismatch is NOT statically knowable (data is dynamic), so it
    # must compile and then fail-closed at eval (the documented contract).
    compiled = compile_filter("data.s > 1")
    assert evaluate(compiled, _ctx({"s": "str"})) is False


# --------------------------------------------------------------------------- #
# TC-SUB-034 — present-null vs absent via has() (SC-SUB-040)                  #
# --------------------------------------------------------------------------- #
def test_tc_sub_034_present_null_vs_absent_distinguished():
    """``has(data.x)`` is True for present-null, False for absent; no crash."""
    has_x = compile_filter("has(data.x)")
    assert evaluate(has_x, _ctx({"x": None})) is True
    assert evaluate(has_x, _ctx({"y": 1})) is False


# --------------------------------------------------------------------------- #
# TC-SUB-035 — deep nesting guarded vs unguarded (SC-SUB-041)                 #
# --------------------------------------------------------------------------- #
def test_tc_sub_035_guarded_deep_access_no_match_when_missing():
    """A presence-guarded deep access is a clean no-match when a level is absent."""
    compiled = compile_filter("has(data.a) && has(data.a.b) && data.a.b == 1")
    assert evaluate(compiled, _ctx({"a": {"c": 2}})) is False
    assert evaluate(compiled, _ctx({"a": {"b": 1}})) is True


def test_tc_sub_035_over_deep_access_rejected_at_compile():
    """An unguarded over-deep access exceeds the depth cap and is rejected at compile."""
    deep = "data." + ".".join(f"a{i}" for i in range(60)) + " == 1"
    with pytest.raises(FilterCompileError):
        compile_filter(deep)


# --------------------------------------------------------------------------- #
# TC-SUB-036 — short-circuit absorbs errors (SC-SUB-042)                      #
# --------------------------------------------------------------------------- #
def test_tc_sub_036_short_circuit_absorbs_errors():
    """false&&bad -> False; true||bad -> True; cond?a:bad -> a; no propagation."""
    ctx = _ctx({"present": 1})
    assert _compiled_eval("false && data.bad == 1", ctx) is False
    assert _compiled_eval("true || data.bad == 1", ctx) is True
    assert _compiled_eval("true ? true : data.bad == 1", ctx) is True


# --------------------------------------------------------------------------- #
# TC-SUB-037 — reverse-DNS glob anchoring, case-sensitive, ReDoS-safe (SC-043)#
# --------------------------------------------------------------------------- #
def test_tc_sub_037_glob_prefix_matches_anchored():
    """``com.stripe.*`` and ``com.stripe.payment_intent.*`` match the full type."""
    et = "com.stripe.payment_intent.succeeded"
    assert match_event_type(["com.stripe.*"], et) is True
    assert match_event_type(["com.stripe.payment_intent.*"], et) is True


def test_tc_sub_037_bare_prefix_is_exact_only():
    """A bare ``com.stripe`` (no trailing .*) matches ONLY the exact type."""
    assert match_event_type(["com.stripe"], "com.stripe.payment_intent.succeeded") is False
    assert match_event_type(["com.stripe"], "com.stripe") is True


def test_tc_sub_037_anchored_no_substring_match():
    """Matching is anchored: a prefix glob does not match an unrelated namespace."""
    assert match_event_type(["com.stripe.*"], "com.stripeyx.payment") is False
    assert match_event_type(["com.github.*"], "com.stripe.payment_intent.succeeded") is False


def test_tc_sub_037_case_sensitive():
    """Segment comparison is case-sensitive."""
    assert match_event_type(["com.stripe.*"], "COM.STRIPE.charge") is False
    assert match_event_type(["com.Stripe.*"], "com.stripe.charge") is False


def test_tc_sub_037_any_glob_in_list_matches():
    """A type matches if ANY glob in the list matches."""
    assert match_event_type(["com.github.*", "com.stripe.*"], "com.stripe.charge.succeeded") is True
    assert match_event_type([], "com.stripe.charge") is False


def test_tc_sub_037_redos_safe_no_pathological_blowup():
    """A long type vs a long glob completes promptly (segment compare, not regex)."""
    # Construct an input that would blow up a naive backtracking regex; segment
    # comparison is linear so this returns quickly.
    long_glob = "a." * 5000 + "*"
    long_type = "a." * 5000 + "b"
    assert match_event_type([long_glob], long_type) is True


def test_tc_sub_037_trailing_star_requires_at_least_one_more_segment():
    """``com.stripe.*`` requires at least one segment after the prefix."""
    assert match_event_type(["com.stripe.*"], "com.stripe") is False


# --------------------------------------------------------------------------- #
# TC-SUB-039 — type equality fast-path (SC-SUB-046)                           #
# --------------------------------------------------------------------------- #
def test_tc_sub_039_type_equality_fast_path():
    """Attribute equality on ``type`` short-circuits to the right boolean."""
    compiled = compile_filter('type == "com.stripe.charge.succeeded"')
    assert evaluate(compiled, _ctx(type_="com.stripe.charge.succeeded")) is True
    assert evaluate(compiled, _ctx(type_="com.github.push")) is False


# --------------------------------------------------------------------------- #
# Library-availability contract                                               #
# --------------------------------------------------------------------------- #
def test_missing_celpy_raises_clear_runtime_error(monkeypatch):
    """If celpy is not importable, compile_filter raises a clear RuntimeError."""
    # Standard
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "celpy" or name.startswith("celpy."):
            raise ImportError("no celpy")
        return real_import(name, *args, **kwargs)

    # Force the lazy loader to re-resolve celpy.
    monkeypatch.setattr(cel, "_celpy", None, raising=False)
    monkeypatch.setattr(cel, "_celtypes", None, raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(RuntimeError):
        compile_filter('type == "x"')
