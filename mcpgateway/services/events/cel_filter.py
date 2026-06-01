# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/cel_filter.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

CEL subscription-filter compilation, evaluation, and reverse-DNS glob matching.

A subscription's ``filter`` is a [CEL](https://cel.dev) boolean expression
evaluated per event against the canonical envelope context (FRD section 7.4).
CEL is chosen because it is sandboxed, side-effect-free, and bounded — safe to
run per event in the delivery worker. The ``event_types`` list is matched first
via a cheap reverse-DNS *segment* glob; the (optional) CEL filter is only
compiled/evaluated for the survivors.

Two layers of safety frame this module (FRD PG-FR-O):

* **Validating admission (compile time).** :func:`compile_filter` rejects, with
  :class:`FilterCompileError` (mapped to ``422`` at the API), expressions that
  fail to parse, that contain a *statically detectable* type mismatch (a
  comparison of a bound envelope attr or a literal to an incompatible type), or
  that exceed a static complexity guard (node-count / nesting-depth budget).
  The complexity guard is a ReDoS / cost-amplification defense applied *before*
  any expression reaches the per-event hot path.
* **Fail-closed evaluation (runtime).** :func:`evaluate` never raises: any
  runtime error (missing field, dynamic overload mismatch) is absorbed as a
  ``False`` no-match (FR-18 / FR-19), while CEL short-circuit semantics
  (``&&`` / ``||`` / ternary) are honored by the underlying VM.

``data`` is an untyped dynamic map, so a type mismatch buried in ``data`` (e.g.
``data.s > 1`` when ``data.s`` is a string) cannot be known at compile time and
is correctly absorbed at runtime as fail-closed; only mismatches over the bound
scalar attrs (``type`` / ``source`` / ``subject`` / ``id``) or pure literals are
rejected at compile time.

The CEL activation (``ctx``) shape is::

    {
        "event":   {<envelope dict>},   # full envelope, e.g. event.data.ref
        "data":    <raw provider body>,  # map; e.g. data.amount
        "type":    <reverse-DNS type>,   # str
        "source":  <event source>,       # str
        "subject": <event subject>,      # str | None
    }

The :mod:`celpy` (``cel-python``) library is an optional dependency (the
``events`` extra). It is imported lazily so that core gateway functionality does
not require it; a missing import raises a clear :class:`RuntimeError`.

Examples:
    >>> match_event_type(["com.stripe.*"], "com.stripe.payment_intent.succeeded")
    True
    >>> match_event_type(["com.stripe"], "com.stripe.payment_intent.succeeded")
    False
    >>> match_event_type(["com.stripe"], "com.stripe")
    True
"""

# Future
from __future__ import annotations

# Standard
from typing import Any, List, Tuple

__all__ = [
    "FilterCompileError",
    "compile_filter",
    "evaluate",
    "match_event_type",
]

# --------------------------------------------------------------------------- #
# Static complexity guard (ReDoS / cost-amplification defense, SC-SUB-038/041)#
# --------------------------------------------------------------------------- #
#
# A subscription filter runs once per matched event in the delivery worker, so a
# pathological expression is a multiplier on every event. These caps bound the
# parse-tree size and nesting depth an admitted expression may have; they are
# generous enough for realistic fan-out filters (a handful of conjoined
# equality / comparison predicates) yet reject obvious amplification attempts.
_MAX_EXPR_CHARS = 4096
_MAX_AST_NODES = 2000
_MAX_AST_DEPTH = 48

# Lazily-resolved module handles (populated by :func:`_load_celpy`). Held at
# module scope so tests can monkeypatch them to exercise the missing-library
# path without uninstalling the dependency.
_celpy: Any = None
_celtypes: Any = None

# Bound scalar envelope attrs probed for type at compile time. ``data`` is left
# as a dynamic untyped map (its leaf types are unknown until an event arrives).
_SCALAR_ATTRS: Tuple[str, ...] = ("type", "source", "subject", "id")


class FilterCompileError(Exception):
    """Raised when a CEL filter fails validating admission.

    Covers syntax errors, statically detectable type mismatches, and
    over-budget / over-deep expressions. The API layer maps this to ``422``.
    """


def _load_celpy() -> Tuple[Any, Any]:
    """Lazily import :mod:`celpy` and return ``(celpy, celtypes)``.

    The import is deferred so that core gateway functionality does not depend on
    the optional ``events`` extra. The resolved modules are cached at module
    scope.

    Returns:
        Tuple[Any, Any]: The ``celpy`` module and its ``celtypes`` submodule.

    Raises:
        RuntimeError: If ``cel-python`` is not installed.
    """
    global _celpy, _celtypes  # pylint: disable=global-statement
    if _celpy is not None and _celtypes is not None:
        return _celpy, _celtypes
    try:
        # Third-Party
        import celpy  # pylint: disable=import-outside-toplevel
        from celpy import celtypes  # pylint: disable=import-outside-toplevel
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise RuntimeError("CEL subscription filters require the optional 'cel-python' dependency; install the gateway 'events' extra (pip install mcp-contextforge-gateway[events]).") from exc
    _celpy, _celtypes = celpy, celtypes
    return celpy, celtypes


class _CompiledFilter:
    """Opaque handle wrapping a compiled CEL program plus its source.

    Attributes:
        source: The original CEL expression text (for logging / diagnostics).
        program: The compiled :class:`celpy.Runner` ready for evaluation.
    """

    __slots__ = ("source", "program")

    def __init__(self, source: str, program: Any) -> None:
        """Store the compiled program and its source text.

        Args:
            source: Original CEL expression text.
            program: Compiled celpy runner.
        """
        self.source = source
        self.program = program


def _ast_size_and_depth(node: Any) -> Tuple[int, int]:
    """Return ``(node_count, max_depth)`` of a parsed CEL (lark) tree.

    Walks the parse tree iteratively-by-recursion counting every node and
    tracking the deepest path. Used by the static complexity guard.

    Args:
        node: The root parse-tree node (a ``lark.Tree`` or leaf token).

    Returns:
        Tuple[int, int]: Total node count and maximum nesting depth.
    """
    children = getattr(node, "children", None)
    if not children:
        return 1, 1
    count = 1
    deepest = 0
    for child in children:
        child_count, child_depth = _ast_size_and_depth(child)
        count += child_count
        if child_depth > deepest:
            deepest = child_depth
    return count, deepest + 1


def _probe_activation(celtypes: Any) -> dict:
    """Build a typed probe activation for compile-time type checking.

    The scalar envelope attrs are bound to string probes so a comparison such as
    ``type > 1`` surfaces a *no matching overload* type error at compile time.
    ``data`` (and ``event``) are bound to empty maps: accessing a ``data`` field
    raises a *missing member* (``KeyError``-backed) error, which is data-
    dependent and therefore NOT treated as a compile-time type error.

    Args:
        celtypes: The ``celpy.celtypes`` module.

    Returns:
        dict: The probe activation mapping for a dry-run evaluation.
    """
    empty_map = celtypes.MapType({})
    probe: dict = {
        "data": empty_map,
        "event": empty_map,
        "type": celtypes.StringType(""),
        "source": celtypes.StringType(""),
        "subject": celtypes.StringType(""),
        "id": celtypes.StringType(""),
    }
    return probe


def _is_static_type_error(eval_error: Any) -> bool:
    """Decide whether a probe :class:`celpy.CELEvalError` is a static type error.

    ``celpy`` carries the originating Python exception class as ``args[1]``. A
    :class:`TypeError` denotes a *no matching overload* mismatch over the typed
    probe values (a genuine, data-independent type error -> reject at compile).
    A :class:`KeyError` denotes a missing map member (data-dependent -> allowed
    at compile, fails closed at runtime). Anything else is treated as
    non-static (allowed at compile) to avoid false rejections.

    Args:
        eval_error: A ``celpy.CELEvalError`` raised by the probe evaluation.

    Returns:
        bool: ``True`` if this is a statically detectable type error.
    """
    args = getattr(eval_error, "args", ())
    cause = args[1] if len(args) > 1 else None
    return isinstance(cause, type) and issubclass(cause, TypeError)


def compile_filter(expr: str) -> _CompiledFilter:
    """Compile and statically validate a CEL filter expression.

    Performs validating admission (FRD PG-FR-O): the expression must parse, pass
    the static complexity guard (length / node-count / nesting-depth budget),
    and survive a typed probe evaluation that rejects statically-detectable type
    mismatches over bound scalar attrs or literals.

    Args:
        expr: The CEL boolean expression text.

    Returns:
        _CompiledFilter: An opaque compiled handle for :func:`evaluate`.

    Raises:
        FilterCompileError: On a syntax error, a statically detectable type
            mismatch, or an over-budget / over-deep expression.
        RuntimeError: If ``cel-python`` is not installed.

    Examples:
        >>> compiled = compile_filter('type == "com.github.push"')
        >>> evaluate(compiled, {"type": "com.github.push", "data": {}})
        True
        >>> compile_filter("data.amount >")  # doctest: +IGNORE_EXCEPTION_DETAIL
        Traceback (most recent call last):
        FilterCompileError: ...
    """
    if expr is None or not str(expr).strip():
        raise FilterCompileError("empty filter expression")

    text = str(expr)
    if len(text) > _MAX_EXPR_CHARS:
        raise FilterCompileError(f"filter expression too long: {len(text)} > {_MAX_EXPR_CHARS} chars")

    celpy, celtypes = _load_celpy()

    env = celpy.Environment(
        annotations={
            "data": celtypes.MapType,
            "event": celtypes.MapType,
            "type": celtypes.StringType,
            "source": celtypes.StringType,
            "subject": celtypes.StringType,
            "id": celtypes.StringType,
        }
    )

    # Parse (syntax). celpy raises CELParseError on a malformed expression.
    try:
        ast = env.compile(text)
    except Exception as exc:  # CELParseError (and any parser-internal error).
        raise FilterCompileError(f"invalid CEL syntax: {exc}") from exc

    # Static complexity guard (cost / depth budget) BEFORE building the program.
    nodes, depth = _ast_size_and_depth(ast)
    if nodes > _MAX_AST_NODES:
        raise FilterCompileError(f"filter expression too complex: {nodes} nodes > {_MAX_AST_NODES}")
    if depth > _MAX_AST_DEPTH:
        raise FilterCompileError(f"filter expression too deeply nested: depth {depth} > {_MAX_AST_DEPTH}")

    try:
        program = env.program(ast)
    except Exception as exc:  # pragma: no cover - parse already validated above.
        raise FilterCompileError(f"could not build CEL program: {exc}") from exc

    # Typed probe evaluation: surface statically-detectable type mismatches.
    try:
        program.evaluate(_probe_activation(celtypes))
    except celpy.CELEvalError as exc:
        if _is_static_type_error(exc):
            raise FilterCompileError(f"CEL type error: {exc}") from exc
        # Data-dependent (missing member, etc.) -> allowed; fails closed at eval.
    except Exception:  # noqa: BLE001 - any other probe failure is data-dependent.
        # Be conservative: do not reject on non-type probe failures.
        pass

    return _CompiledFilter(text, program)


def evaluate(compiled: _CompiledFilter, ctx: dict) -> bool:
    """Evaluate a compiled CEL filter against an activation, fail-closed.

    Any runtime error (missing field, dynamic overload mismatch, malformed
    activation) is absorbed as a ``False`` no-match (FR-18 / FR-19); the function
    never raises into the delivery loop. CEL short-circuit semantics are honored
    by the underlying VM (``false && bad`` -> ``False`` without touching
    ``bad``).

    Args:
        compiled: A handle returned by :func:`compile_filter`.
        ctx: The CEL activation dict (``{"event", "data", "type", "source",
            "subject"}``).

    Returns:
        bool: ``True`` only when the expression evaluates to a true boolean;
        ``False`` on a false result OR any runtime error.

    Examples:
        >>> compiled = compile_filter("data.amount > 100")
        >>> evaluate(compiled, {"data": {"amount": 150}})
        True
        >>> evaluate(compiled, {"data": {"amount": 50}})
        False
        >>> evaluate(compiled, {"data": {}})  # missing field -> fail-closed
        False
    """
    if compiled is None:
        return False
    try:
        celpy, _ = _load_celpy()
        activation = celpy.json_to_cel(ctx)
        result = compiled.program.evaluate(activation)
    except Exception:  # noqa: BLE001 - fail-closed: never crash the delivery loop.
        return False
    # CEL returns a BoolType (a bool subclass); coerce defensively. A CELEvalError
    # value returned (rather than raised) is also a non-true result -> False.
    try:
        return bool(result) is True and not isinstance(result, BaseException)
    except Exception:  # noqa: BLE001 - defensive: any odd result -> no match.
        return False


def _segment_glob_match(glob_segments: List[str], type_segments: List[str]) -> bool:
    """Match reverse-DNS segments against a glob's segments, anchored.

    Matching rules (ReDoS-safe by construction — pure segment comparison, never
    a quantified regex):

    * A trailing ``*`` segment is a wildcard: the glob's leading segments must
      match the type's leading segments exactly, and the type must carry **at
      least one** further segment (``com.stripe.*`` matches
      ``com.stripe.charge`` but NOT bare ``com.stripe``).
    * Without a trailing ``*`` the match is exact, full-length, and
      case-sensitive.

    Args:
        glob_segments: The glob split on ``.``.
        type_segments: The event type split on ``.``.

    Returns:
        bool: ``True`` if the type matches the glob.
    """
    if glob_segments and glob_segments[-1] == "*":
        prefix = glob_segments[:-1]
        # Require strictly more type segments than the fixed prefix so the "*"
        # consumes at least one segment (anchored prefix, not bare-prefix match).
        if len(type_segments) <= len(prefix):
            return False
        return type_segments[: len(prefix)] == prefix
    # Exact, full-length, case-sensitive match.
    return glob_segments == type_segments


def match_event_type(globs: List[str], event_type: str) -> bool:
    """Return whether ``event_type`` matches ANY reverse-DNS glob in ``globs``.

    The match is over reverse-DNS *segments* (split on ``.``): anchored,
    case-sensitive, and ReDoS-safe by construction (segment comparison rather
    than a regex with quantifiers). A trailing ``*`` segment matches one or more
    further segments; a bare prefix (no trailing ``*``) matches only the exact
    type.

    Args:
        globs: List of reverse-DNS glob patterns (e.g. ``["com.stripe.*"]``).
        event_type: The reverse-DNS event type to test.

    Returns:
        bool: ``True`` if ``event_type`` matches at least one glob.

    Examples:
        >>> match_event_type(["com.stripe.*"], "com.stripe.charge.succeeded")
        True
        >>> match_event_type(["com.stripe.payment_intent.*"], "com.stripe.payment_intent.succeeded")
        True
        >>> match_event_type(["com.stripe"], "com.stripe.charge")
        False
        >>> match_event_type(["com.stripe"], "com.stripe")
        True
        >>> match_event_type(["com.stripe.*"], "COM.STRIPE.charge")
        False
    """
    if not globs or event_type is None:
        return False
    type_segments = str(event_type).split(".")
    for glob in globs:
        if glob is None:
            continue
        if _segment_glob_match(str(glob).split("."), type_segments):
            return True
    return False
