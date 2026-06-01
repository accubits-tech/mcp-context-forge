# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/routers/webhooks.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

Generic webhook ingress route for MCP server-initiated events (FRD §6.2).

This module exposes the single, config-driven ingress endpoint that every
provider posts to: ``POST /webhooks/{conn-id}``. There is exactly one route for
all providers - GitHub, Stripe, Slack, and any descriptor-defined provider -
because the per-connection :class:`~mcpgateway.db.Gateway` row carries the
provider descriptor reference and the signing secret; the route itself stays
provider-agnostic and delegates the security-load-bearing pipeline (verify ->
parse -> handshake -> no-op -> normalize -> dedup -> persist -> publish) to
:class:`~mcpgateway.services.events.ingress_service.IngressService`.

The route is **unauthenticated at the transport layer** by design: an inbound
provider POST carries no bearer token, only a provider HMAC signature over the
raw body. Authentication is therefore the signature verification performed
inside the ingress service, not a gateway-level auth dependency. Adding a
bearer/RBAC dependency here would break every provider integration.

Two responsibilities live in the route (everything else is in the service):

* **Body-size guard** - there is no global request-body-size middleware on the
  ingress path, so this route self-enforces
  :data:`settings.mcpgateway_events_max_body_bytes` and returns ``413`` for an
  oversized body before reading or processing it (FRD §6.7 / DoS guard).
* **Result-to-HTTP mapping** - :class:`IngressResult` carries a status and an
  optional body; the route renders it. A handshake echo (status ``200``) is
  returned as the raw challenge string (``text/plain``) so a provider's
  url-verification check passes byte-for-byte; an accepted event (``202``)
  returns ``{"status": "accepted"}``; everything else returns an empty body with
  the mapped status so no information leaks (no existence oracle).

When the events master switch
(:data:`settings.mcpgateway_events_enabled`) is off, the route is still mounted
but every method returns ``404`` so it is indistinguishable from an unmounted
path.
"""

# Future
from __future__ import annotations

# Standard
from typing import Any, Mapping

# Third-Party
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy.orm import Session

# First-Party
from mcpgateway.config import settings
from mcpgateway.db import get_db
from mcpgateway.services.events.ingress_service import IngressResult, IngressService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _result_to_response(result: IngressResult) -> Response:
    """Render an :class:`IngressResult` as an HTTP response.

    Args:
        result: The outcome of the ingress pipeline.

    Returns:
        Response: The HTTP response. A ``200`` handshake echo is returned as the
        raw challenge string (``text/plain``); a ``202`` accept returns
        ``{"status": "accepted"}``; every other status returns an empty body so
        no differential information leaks to a caller (no existence oracle).
    """
    if result.status == 200:
        # Handshake echo (e.g. Slack url_verification challenge): the provider
        # expects the challenge value returned verbatim.
        return PlainTextResponse(content="" if result.body is None else str(result.body), status_code=200)
    if result.status == 202:
        return JSONResponse(content={"status": "accepted"}, status_code=202)
    # 400 / 401 / 404 (and any other) -> empty body, mapped status only.
    return Response(status_code=result.status)


@router.post("/{conn_id}")
async def ingest_webhook(conn_id: str, request: Request, db: Session = Depends(get_db)) -> Response:
    """Ingest one inbound provider webhook POST.

    This endpoint is unauthenticated at the transport layer: it is authenticated
    by the provider HMAC signature verified inside the ingress service, not by a
    bearer token. The raw request bytes are read exactly once (verification runs
    over those exact bytes) after a body-size guard.

    Args:
        conn_id: The opaque connection id from the path.
        request: The inbound request (raw body, headers, and query params).
        db: An active database session (canonical ``get_db`` dependency).

    Returns:
        Response: The HTTP response mapped from the ingress outcome - ``202``
        accepted, ``200`` handshake echo, or ``400``/``401``/``404``/``413``.
    """
    # Master flag gate: opaque 404 when events are disabled (indistinguishable
    # from an unmounted route).
    if not settings.mcpgateway_events_enabled:
        return Response(status_code=404)

    # Body-size guard: reject an oversized body before reading/processing it.
    # The Content-Length header (when present and trustworthy) lets us reject
    # without buffering; we still re-check the actual length after reading.
    max_bytes = settings.mcpgateway_events_max_body_bytes
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                return Response(status_code=413)
        except ValueError:
            pass  # Malformed header: fall through to the post-read length check.

    raw_body: bytes = await request.body()
    if len(raw_body) > max_bytes:
        return Response(status_code=413)

    headers: Mapping[str, str] = dict(request.headers)
    query_params: Mapping[str, str] = dict(request.query_params)

    result: IngressResult = await IngressService().ingest(
        conn_id=conn_id,
        raw_body=raw_body,
        headers=headers,
        query_params=query_params,
        db=db,
    )
    return _result_to_response(result)


@router.get("/{conn_id}")
@router.head("/{conn_id}")
async def webhook_liveness(conn_id: str) -> Response:  # pylint: disable=unused-argument
    """Answer a provider liveness/health probe with ``200`` and no side effect.

    Some providers probe the endpoint with ``GET``/``HEAD`` before enabling
    delivery. These methods never emit a domain event.

    Args:
        conn_id: The opaque connection id from the path (unused; accepted so the
            probe targets the same URL the provider will POST to).

    Returns:
        Response: ``200`` when events are enabled, else an opaque ``404``.
    """
    if not settings.mcpgateway_events_enabled:
        return Response(status_code=404)
    return Response(status_code=200)


__all__: list[Any] = ["router"]
