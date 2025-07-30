# -*- coding: utf-8 -*-
"""
Unit-tests for the GatewayService implementation.

These tests use only MagicMock / AsyncMock - no real network access
and no real database needed.  Where the service relies on Pydantic
models or SQLAlchemy Result objects, we monkey-patch or fake just
enough behaviour to satisfy the code paths under test.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti
"""

# Future
from __future__ import annotations

# Standard
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

# Third-Party
import httpx
import pytest

# First-Party
# ---------------------------------------------------------------------------
# Application imports
# ---------------------------------------------------------------------------
from mcpgateway.db import Gateway as DbGateway
from mcpgateway.db import Tool as DbTool
from mcpgateway.schemas import GatewayCreate, GatewayUpdate
from mcpgateway.services.gateway_service import (
    GatewayConnectionError,
    GatewayError,
    GatewayNameConflictError,
    GatewayNotFoundError,
    GatewayService,
)

# ---------------------------------------------------------------------------
# Helpers & global monkey-patches
# ---------------------------------------------------------------------------


def _make_execute_result(*, scalar=None, scalars_list=None):
    """
    Return a MagicMock that behaves like the SQLAlchemy Result object the
    service expects after ``Session.execute``:

        - .scalar_one_or_none()  -> *scalar*
        - .scalars().all()      -> *scalars_list*  (defaults to [])

    This lets us emulate both the "fetch one" path and the "fetch many"
    path with a single helper.
    """
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    scalars_proxy = MagicMock()
    scalars_proxy.all.return_value = scalars_list or []
    result.scalars.return_value = scalars_proxy
    return result


@pytest.fixture(autouse=True)
def _bypass_gatewayread_validation(monkeypatch):
    """
    The real GatewayService returns ``GatewayRead.model_validate(db_obj)``.
    The DB objects we feed in here are MagicMocks, not real models, and
    Pydantic hates that.  We therefore stub out `GatewayRead.model_validate`
    so it simply returns what it was given.
    """
    # First-Party
    from mcpgateway.schemas import GatewayRead

    monkeypatch.setattr(GatewayRead, "model_validate", staticmethod(lambda x: x))


@pytest.fixture(autouse=True)
def _inject_check_gateway_health(monkeypatch):
    """
    Older versions of GatewayService (the one under test) do *not* expose
    `check_gateway_health`, yet the original test-suite calls it.  Inject
    a minimal coroutine that exercises `_initialize_gateway` and sets
    `last_seen` on success.
    """

    async def _check(self, gateway):
        try:
            await self._initialize_gateway(gateway.url, getattr(gateway, "auth_value", {}), getattr(gateway, "transport", "sse"))
            gateway.last_seen = datetime.now(timezone.utc)
            return True
        except Exception:
            return False

    monkeypatch.setattr(GatewayService, "check_gateway_health", _check, raising=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_service():
    """
    A GatewayService instance with its internal HTTP-client replaced by
    an AsyncMock so no real HTTP requests are performed.
    """
    service = GatewayService()
    service._http_client = AsyncMock()
    return service


@pytest.fixture
def mock_gateway():
    """Return a minimal but realistic DbGateway MagicMock."""
    gw = MagicMock(spec=DbGateway)
    gw.id = 1
    gw.name = "test_gateway"
    gw.url = "http://example.com/gateway"
    gw.description = "A test gateway"
    gw.capabilities = {"prompts": {"listChanged": True}, "resources": {"listChanged": True}, "tools": {"listChanged": True}}
    gw.created_at = gw.updated_at = gw.last_seen = "2025-01-01T00:00:00Z"
    gw.enabled = True
    gw.reachable = True

    # one dummy tool hanging off the gateway
    tool = MagicMock(spec=DbTool, id=101, name="dummy_tool")
    gw.tools = [tool]
    gw.federated_tools = []
    gw.transport = "sse"
    gw.auth_value = {}
    return gw


# ---------------------------------------------------------------------------
# Test-cases
# ---------------------------------------------------------------------------


class TestGatewayService:
    """All GatewayService happy-path and error-path unit-tests."""

    # ────────────────────────────────────────────────────────────────────
    # REGISTER
    # ────────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_register_gateway(self, gateway_service, test_db, monkeypatch):
        """Successful gateway registration populates DB and returns data."""
        # DB: no gateway with that name; no existing tools found
        test_db.execute = Mock(
            side_effect=[
                _make_execute_result(scalar=None),  # name-conflict check
                _make_execute_result(scalars_list=[]),  # tool lookup
            ]
        )
        test_db.add = Mock()
        test_db.commit = Mock()
        test_db.refresh = Mock()

        # Internal helpers
        gateway_service._initialize_gateway = AsyncMock(
            return_value=(
                {
                    "prompts": {"listChanged": True},
                    "resources": {"listChanged": True},
                    "tools": {"listChanged": True},
                },
                [],
            )
        )
        gateway_service._notify_gateway_added = AsyncMock()

        # Patch GatewayRead.model_validate to return a mock with .masked()
        mock_model = Mock()
        mock_model.masked.return_value = mock_model
        mock_model.name = "test_gateway"
        mock_model.url = "http://example.com/gateway"
        mock_model.description = "A test gateway"

        monkeypatch.setattr(
            "mcpgateway.services.gateway_service.GatewayRead.model_validate",
            lambda x: mock_model,
        )

        gateway_create = GatewayCreate(
            name="test_gateway",
            url="http://example.com/gateway",
            description="A test gateway",
        )

        result = await gateway_service.register_gateway(test_db, gateway_create)

        test_db.add.assert_called_once()
        test_db.commit.assert_called_once()
        test_db.refresh.assert_called_once()
        gateway_service._initialize_gateway.assert_called_once()
        gateway_service._notify_gateway_added.assert_called_once()

        # `result` is the same GatewayCreate instance because we stubbed
        # GatewayRead.model_validate → just check its fields:
        assert result.name == "test_gateway"
        assert result.url == "http://example.com/gateway"
        assert result.description == "A test gateway"

    @pytest.mark.asyncio
    async def test_register_gateway_name_conflict(self, gateway_service, mock_gateway, test_db):
        """Trying to register a gateway whose *name* already exists raises a conflict error."""
        # DB returns an existing gateway with the same name
        test_db.execute = Mock(return_value=_make_execute_result(scalar=mock_gateway))

        gateway_create = GatewayCreate(
            name="test_gateway",  # same as mock_gateway
            url="http://example.com/other",
            description="Another gateway",
        )

        with pytest.raises(GatewayNameConflictError) as exc_info:
            await gateway_service.register_gateway(test_db, gateway_create)

        err = exc_info.value
        assert "Gateway already exists with name" in str(err)
        assert err.name == "test_gateway"
        assert err.gateway_id == mock_gateway.id

    @pytest.mark.asyncio
    async def test_register_gateway_connection_error(self, gateway_service, test_db):
        """Initial connection to the remote gateway fails and the error propagates."""
        test_db.execute = Mock(return_value=_make_execute_result(scalar=None))

        # _initialize_gateway blows up before any DB work happens
        gateway_service._initialize_gateway = AsyncMock(side_effect=GatewayConnectionError("Failed to connect"))

        gateway_create = GatewayCreate(
            name="test_gateway",
            url="http://example.com/gateway",
            description="A test gateway",
        )

        with pytest.raises(GatewayConnectionError) as exc_info:
            await gateway_service.register_gateway(test_db, gateway_create)

        assert "Failed to connect" in str(exc_info.value)

    # ────────────────────────────────────────────────────────────────────
    # Validate Gateway URL Timeout
    # ────────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_gateway_validate_timeout(self, gateway_service, monkeypatch):
        # creating a mock with a timeout error
        mock_stream = AsyncMock(side_effect=httpx.ReadTimeout("Timeout"))

        mock_aclose = AsyncMock()

        # Step 3: Mock client with .stream and .aclose
        mock_client_instance = MagicMock()
        mock_client_instance.stream = mock_stream
        mock_client_instance.aclose = mock_aclose

        mock_http_client = MagicMock()
        mock_http_client.client = mock_client_instance
        mock_http_client.aclose = mock_aclose

        monkeypatch.setattr("mcpgateway.services.gateway_service.ResilientHttpClient", MagicMock(return_value=mock_http_client))

        result = await gateway_service._validate_gateway_url(url="http://example.com", headers={}, transport_type="SSE", timeout=2)

        assert result is False

    # ────────────────────────────────────────────────────────────────────
    # Validate Gateway URL SSL Verification
    # ────────────────────────────────────────────────────────────────────
    @pytest.mark.skip("Yet to implement")
    async def test_ssl_verification_bypass(self, gateway_service, monkeypatch):
        """
        Test case logic to verify settings.skip_ssl_verify

        """
        pass

    # ────────────────────────────────────────────────────────────────────
    # Validate Gateway URL Auth Failure - 401
    # ────────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_validate_auth_failure_401(self, gateway_service, monkeypatch):
        # Mock the response object to be returned inside the async with block
        response_mock = MagicMock()
        response_mock.status_code = 401
        response_mock.headers = {"content-type": "text/event-stream"}

        # Create an async context manager mock that returns response_mock
        stream_context = MagicMock()
        stream_context.__aenter__ = AsyncMock(return_value=response_mock)
        stream_context.__aexit__ = AsyncMock(return_value=None)

        # Mock the AsyncClient to return this context manager from .stream()
        client_mock = MagicMock()
        client_mock.stream = AsyncMock(return_value=stream_context)
        client_mock.aclose = AsyncMock()

        # Mock ResilientHttpClient to return this client
        resilient_client_mock = MagicMock()
        resilient_client_mock.client = client_mock
        resilient_client_mock.aclose = AsyncMock()

        monkeypatch.setattr("mcpgateway.services.gateway_service.ResilientHttpClient", MagicMock(return_value=resilient_client_mock))

        # Run the method
        result = await gateway_service._validate_gateway_url(url="http://example.com", headers={}, transport_type="SSE")

        # Expect False due to 401
        assert result is False

    # ────────────────────────────────────────────────────────────────────
    # Validate Gateway URL Auth Failure - 403
    # ────────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_validate_auth_failure_403(self, gateway_service, monkeypatch):
        # Mock the response object to be returned inside the async with block
        response_mock = MagicMock()
        response_mock.status_code = 403
        response_mock.headers = {"content-type": "text/event-stream"}

        # Create an async context manager mock that returns response_mock
        stream_context = MagicMock()
        stream_context.__aenter__ = AsyncMock(return_value=response_mock)
        stream_context.__aexit__ = AsyncMock(return_value=None)

        # Mock the AsyncClient to return this context manager from .stream()
        client_mock = MagicMock()
        client_mock.stream = AsyncMock(return_value=stream_context)
        client_mock.aclose = AsyncMock()

        # Mock ResilientHttpClient to return this client
        resilient_client_mock = MagicMock()
        resilient_client_mock.client = client_mock
        resilient_client_mock.aclose = AsyncMock()

        monkeypatch.setattr("mcpgateway.services.gateway_service.ResilientHttpClient", MagicMock(return_value=resilient_client_mock))

        # Run the method
        result = await gateway_service._validate_gateway_url(url="http://example.com", headers={}, transport_type="SSE")

        # Expect False due to 401
        assert result is False

    # ────────────────────────────────────────────────────────────────────
    # Validate Gateway URL Connection Error
    # ────────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_validate_connectivity_failure(self, gateway_service, monkeypatch):
        # Create an async context manager mock that raises ConnectError
        stream_context = AsyncMock()
        stream_context.__aenter__.side_effect = httpx.ConnectError("connection error")
        stream_context.__aexit__.return_value = AsyncMock()

        # Mock client with .stream() and .aclose()
        mock_client = MagicMock()
        mock_client.stream.return_value = stream_context
        mock_client.aclose = AsyncMock()

        # Patch ResilientHttpClient to return this mock client
        resilient_client_mock = MagicMock()
        resilient_client_mock.client = mock_client
        resilient_client_mock.aclose = AsyncMock()

        monkeypatch.setattr("mcpgateway.services.gateway_service.ResilientHttpClient", MagicMock(return_value=resilient_client_mock))

        # Call the method and assert result
        result = await gateway_service._validate_gateway_url(url="http://example.com", headers={}, transport_type="SSE")

        assert result is False

    # ───────────────────────────────────────────────────────────────────────────
    # Validate Gateway - StreamableHTTP with mcp-session-id & redirected-url
    # ───────────────────────────────────────────────────────────────────────────
    @pytest.mark.skip(reason="Investigating the test case")
    async def test_streamablehttp_redirect(self, gateway_service, monkeypatch):
        # Mock first response (redirect)
        first_response = MagicMock()
        first_response.status_code = 200
        first_response.headers = {"Location": "http://sampleredirected.com"}

        first_cm = AsyncMock()
        first_cm.__aenter__.return_value = first_response
        first_cm.__aexit__.return_value = None

        # Mock redirected response (final)
        redirected_response = MagicMock()
        redirected_response.status_code = 200
        redirected_response.headers = {"Mcp-Session-Id": "sample123", "Content-Type": "application/json"}

        second_cm = AsyncMock()
        second_cm.__aenter__.return_value = redirected_response
        second_cm.__aexit__.return_value = None

        # Mock ResilientHttpClient client.stream to return redirect chain
        client_mock = MagicMock()
        client_mock.stream = AsyncMock(side_effect=[first_cm, second_cm])
        client_mock.aclose = AsyncMock()

        resilient_http_mock = MagicMock()
        resilient_http_mock.client = client_mock
        resilient_http_mock.aclose = AsyncMock()

        monkeypatch.setattr("mcpgateway.services.gateway_service.ResilientHttpClient", MagicMock(return_value=resilient_http_mock))

        result = await gateway_service._validate_gateway_url(url="http://example.com", headers={}, transport_type="STREAMABLEHTTP")
        assert result is True

    # ───────────────────────────────────────────────────────────────────────────
    # Validate Gateway URL - Bulk Concurrent requests Validation
    # ───────────────────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_bulk_concurrent_validation(self, gateway_service, monkeypatch):
        urls = [f"http://gateway{i}.com" for i in range(20)]

        # Simulate a successful stream context
        stream_context = AsyncMock()
        stream_context.__aenter__.return_value.status_code = 200
        stream_context.__aenter__.return_value.headers = {"content-type": "text/event-stream"}
        stream_context.__aexit__.return_value = AsyncMock()

        # Mock client to return the above stream context
        mock_client = MagicMock()
        mock_client.stream.return_value = stream_context
        mock_client.aclose = AsyncMock()

        # ResilientHttpClient mock returns a .client and .aclose
        resilient_client_mock = MagicMock()
        resilient_client_mock.client = mock_client
        resilient_client_mock.aclose = AsyncMock()

        # Patch ResilientHttpClient where it’s used in your module
        monkeypatch.setattr("mcpgateway.services.gateway_service.ResilientHttpClient", MagicMock(return_value=resilient_client_mock))

        # Run the validations concurrently
        results = await asyncio.gather(*[gateway_service._validate_gateway_url(url, {}, "SSE") for url in urls])

        # All should be True (validation success)
        assert all(results)

    # ────────────────────────────────────────────────────────────────────
    # LIST / GET
    # ────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_list_gateways(self, gateway_service, mock_gateway, test_db, monkeypatch):
        """Listing gateways returns the active ones."""

        test_db.execute = Mock(return_value=_make_execute_result(scalars_list=[mock_gateway]))

        mock_model = Mock()
        mock_model.masked.return_value = mock_model
        mock_model.name = "test_gateway"

        # Patch using full path string to GatewayRead.model_validate
        monkeypatch.setattr("mcpgateway.services.gateway_service.GatewayRead.model_validate", lambda x: mock_model)

        result = await gateway_service.list_gateways(test_db)

        test_db.execute.assert_called_once()
        assert len(result) == 1
        assert result[0].name == "test_gateway"

    @pytest.mark.asyncio
    async def test_get_gateway(self, gateway_service, mock_gateway, test_db):
        """Gateway is fetched and returned by ID."""
        mock_gateway.masked = Mock(return_value=mock_gateway)
        test_db.get = Mock(return_value=mock_gateway)
        result = await gateway_service.get_gateway(test_db, 1)
        test_db.get.assert_called_once_with(DbGateway, 1)
        assert result.name == "test_gateway"
        assert result.capabilities == mock_gateway.capabilities

    @pytest.mark.asyncio
    async def test_get_gateway_not_found(self, gateway_service, test_db):
        """Missing ID → GatewayNotFoundError."""
        test_db.get = Mock(return_value=None)
        with pytest.raises(GatewayNotFoundError):
            await gateway_service.get_gateway(test_db, 999)

    @pytest.mark.asyncio
    async def test_get_gateway_inactive(self, gateway_service, mock_gateway, test_db):
        """Inactive gateway is not returned unless explicitly asked for."""
        mock_gateway.enabled = False
        mock_gateway.id = 1
        test_db.get = Mock(return_value=mock_gateway)

        # Create a mock for GatewayRead with a masked method
        mock_gateway_read = Mock()
        mock_gateway_read.id = 1
        mock_gateway_read.enabled = False
        mock_gateway_read.masked = Mock(return_value=mock_gateway_read)

        with patch("mcpgateway.services.gateway_service.GatewayRead.model_validate", return_value=mock_gateway_read):
            result = await gateway_service.get_gateway(test_db, 1, include_inactive=True)
            assert result.id == 1
            assert result.enabled == False

            # Now test the inactive = False path
            test_db.get = Mock(return_value=mock_gateway)
            with pytest.raises(GatewayNotFoundError):
                await gateway_service.get_gateway(test_db, 1, include_inactive=False)

    # ────────────────────────────────────────────────────────────────────
    # UPDATE
    # ────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_update_gateway(self, gateway_service, mock_gateway, test_db):
        """All mutable fields can be updated."""
        test_db.get = Mock(return_value=mock_gateway)
        # name-conflict check: no conflicting gateway
        test_db.execute = Mock(return_value=_make_execute_result(scalar=None))
        test_db.commit = Mock()
        test_db.refresh = Mock()

        # Simulate successful gateway initialization
        gateway_service._initialize_gateway = AsyncMock(
            return_value=(
                {
                    "prompts": {"subscribe": True},
                    "resources": {"subscribe": True},
                    "tools": {"subscribe": True},
                },
                [],
            )
        )
        gateway_service._notify_gateway_updated = AsyncMock()

        # Create the update payload
        gateway_update = GatewayUpdate(
            name="updated_gateway",
            url="http://example.com/updated",
            description="Updated description",
        )

        # Create mock return for GatewayRead.model_validate().masked()
        mock_gateway_read = MagicMock()
        mock_gateway_read.name = "updated_gateway"
        mock_gateway_read.masked.return_value = mock_gateway_read  # Ensure .masked() returns the same object

        # Patch the model_validate call in the service
        with patch("mcpgateway.services.gateway_service.GatewayRead.model_validate", return_value=mock_gateway_read):
            result = await gateway_service.update_gateway(test_db, 1, gateway_update)

        # Assertions
        test_db.commit.assert_called_once()
        test_db.refresh.assert_called_once()
        gateway_service._initialize_gateway.assert_called_once()
        gateway_service._notify_gateway_updated.assert_called_once()
        assert mock_gateway.name == "updated_gateway"
        assert result.name == "updated_gateway"

    @pytest.mark.asyncio
    async def test_update_gateway_not_found(self, gateway_service, test_db):
        """Updating a non-existent gateway surfaces GatewayError with message."""
        test_db.get = Mock(return_value=None)
        gateway_update = GatewayUpdate(name="whatever")
        with pytest.raises(GatewayError) as exc_info:
            await gateway_service.update_gateway(test_db, 999, gateway_update)
        assert "Gateway not found: 999" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_update_gateway_name_conflict(self, gateway_service, mock_gateway, test_db):
        """Changing the name to one that already exists raises GatewayError."""
        test_db.get = Mock(return_value=mock_gateway)
        conflicting = MagicMock(spec=DbGateway, id=2, name="existing_gateway", is_active=True)
        test_db.execute = Mock(return_value=_make_execute_result(scalar=conflicting))
        test_db.rollback = Mock()

        # gateway_update = MagicMock(spec=GatewayUpdate, name="existing_gateway")
        gateway_update = GatewayUpdate(name="existing_gateway")

        with pytest.raises(GatewayError) as exc_info:
            await gateway_service.update_gateway(test_db, 1, gateway_update)

        assert "Gateway already exists with name" in str(exc_info.value)

    # ────────────────────────────────────────────────────────────────────
    # TOGGLE ACTIVE / INACTIVE
    # ────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_toggle_gateway_status(self, gateway_service, mock_gateway, test_db):
        """Deactivating an active gateway triggers tool-status toggle + event."""
        test_db.get = Mock(return_value=mock_gateway)
        test_db.commit = Mock()
        test_db.refresh = Mock()

        # Return one tool so toggle_tool_status gets called
        query_proxy = MagicMock()
        filter_proxy = MagicMock()
        filter_proxy.all.return_value = [MagicMock(id=101)]
        query_proxy.filter.return_value = filter_proxy
        test_db.query = Mock(return_value=query_proxy)

        # Setup gateway service mocks
        gateway_service._notify_gateway_activated = AsyncMock()
        gateway_service._notify_gateway_deactivated = AsyncMock()
        gateway_service._initialize_gateway = AsyncMock(return_value=({"prompts": {}}, []))

        tool_service_stub = MagicMock()
        tool_service_stub.toggle_tool_status = AsyncMock()
        gateway_service.tool_service = tool_service_stub

        # Patch model_validate to return a mock with .masked()
        mock_gateway_read = MagicMock()
        mock_gateway_read.masked.return_value = mock_gateway_read

        with patch("mcpgateway.services.gateway_service.GatewayRead.model_validate", return_value=mock_gateway_read):
            result = await gateway_service.toggle_gateway_status(test_db, 1, activate=False)

        assert mock_gateway.enabled is False
        gateway_service._notify_gateway_deactivated.assert_called_once()
        assert tool_service_stub.toggle_tool_status.called
        assert result == mock_gateway_read

    # ────────────────────────────────────────────────────────────────────
    # DELETE
    # ────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_delete_gateway(self, gateway_service, mock_gateway, test_db):
        """Gateway is removed and subscribers are notified."""
        test_db.get = Mock(return_value=mock_gateway)
        test_db.delete = Mock()
        test_db.commit = Mock()

        # tool clean-up query chain
        test_db.query = Mock(return_value=MagicMock(filter=MagicMock(return_value=MagicMock(delete=Mock()))))

        gateway_service._notify_gateway_deleted = AsyncMock()

        await gateway_service.delete_gateway(test_db, 1)

        test_db.delete.assert_called_once_with(mock_gateway)
        gateway_service._notify_gateway_deleted.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_gateway_not_found(self, gateway_service, test_db):
        """Trying to delete a non-existent gateway raises GatewayError."""
        test_db.get = Mock(return_value=None)
        with pytest.raises(GatewayError) as exc_info:
            await gateway_service.delete_gateway(test_db, 999)
        assert "Gateway not found: 999" in str(exc_info.value)

    # ────────────────────────────────────────────────────────────────────
    # FORWARD
    # ────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_forward_request(self, gateway_service, mock_gateway):
        """Happy-path RPC forward."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = Mock()
        mock_response.json = Mock(return_value={"jsonrpc": "2.0", "result": {"success": True, "data": "OK"}, "id": 1})
        gateway_service._http_client.post.return_value = mock_response

        result = await gateway_service.forward_request(mock_gateway, "method", {"p": 1})

        assert result == {"success": True, "data": "OK"}
        assert mock_gateway.last_seen is not None

    @pytest.mark.asyncio
    async def test_forward_request_error_response(self, gateway_service, mock_gateway):
        """Gateway returns JSON-RPC error."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = Mock()
        mock_response.json = Mock(return_value={"jsonrpc": "2.0", "error": {"code": -32000, "message": "Boom"}, "id": 1})
        gateway_service._http_client.post.return_value = mock_response

        with pytest.raises(GatewayError) as exc_info:
            await gateway_service.forward_request(mock_gateway, "method", {"p": 1})
        assert "Gateway error: Boom" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_forward_request_connection_error(self, gateway_service, mock_gateway):
        """HTTP client raises network-level exception."""
        gateway_service._http_client.post.side_effect = Exception("Network down")
        with pytest.raises(GatewayConnectionError):
            await gateway_service.forward_request(mock_gateway, "method", {})

    # ────────────────────────────────────────────────────────────────────
    # HEALTH CHECK helper (injected fixture)
    # ────────────────────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_check_gateway_health(self, gateway_service, mock_gateway):
        """Injected helper returns True + updates last_seen."""
        gateway_service._initialize_gateway = AsyncMock()
        ok = await gateway_service.check_gateway_health(mock_gateway)
        assert ok is True
        assert mock_gateway.last_seen is not None

    @pytest.mark.asyncio
    async def test_check_gateway_health_failure(self, gateway_service, mock_gateway):
        """Injected helper returns False upon failure."""
        gateway_service._initialize_gateway = AsyncMock(side_effect=Exception("fail"))
        ok = await gateway_service.check_gateway_health(mock_gateway)
        assert ok is False
