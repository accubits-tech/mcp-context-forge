# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/events/egress/__init__.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0

mcpgateway.services.events.egress - L3 egress delivery adapters package.

This package houses the egress (L3) layer that performs the final hop out of
the gateway to each subscriber: the HTTP-callback adapter (signed outbound
``POST callback_url``) and the SSE/WS stream adapter. Adapters implement a
single async interface and are driven by the delivery worker that consumes the
L2 Redis Stream. Concrete adapters and the shared interface are added by
subsequent milestones; no symbols are exported yet.
"""
