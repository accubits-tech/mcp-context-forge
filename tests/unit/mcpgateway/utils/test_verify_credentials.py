# -*- coding: utf-8 -*-
"""Location: ./tests/unit/mcpgateway/utils/test_verify_credentials.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

Unit tests for **mcpgateway.utils.verify_credentials**
Author: Mihai Criveti

Paths covered
-------------
* verify_jwt_token  - success, expired, invalid-signature branches
* verify_credentials - payload enrichment
* require_auth      - happy path, missing-token failure
* verify_basic_credentials - success & failure
* require_basic_auth - required & optional modes
* require_auth_override - header vs cookie precedence

Only dependencies needed are ``pytest`` and ``PyJWT`` (already required by the
target module).  FastAPI `HTTPException` objects are asserted for status code
and detail.
"""

# Future
from __future__ import annotations

# Standard
import base64
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

# Third-Party
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasicCredentials
from fastapi.testclient import TestClient
import jwt
from pydantic import SecretStr
import pytest

# First-Party
from mcpgateway.utils import verify_credentials as vc  # module under test

try:
    # First-Party
    from mcpgateway.main import app
except ImportError:
    app = None

# ---------------------------------------------------------------------------
# Shared constants / helpers
# ---------------------------------------------------------------------------
SECRET = "unit-secret"
ALGO = "HS256"


def _token(payload: dict, *, exp_delta: int | None = 60, secret: str = SECRET) -> str:
    """Return a signed JWT with optional expiry offset (minutes)."""
    # Add required audience and issuer claims for compatibility with RBAC system
    token_payload = payload.copy()
    token_payload.update({"iss": "mcpgateway", "aud": "mcpgateway-api"})

    if exp_delta is not None:
        expire = datetime.now(timezone.utc) + timedelta(minutes=exp_delta)
        token_payload["exp"] = int(expire.timestamp())

    return jwt.encode(token_payload, secret, algorithm=ALGO)


# ---------------------------------------------------------------------------
# verify_jwt_token + verify_credentials
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_jwt_token_success(monkeypatch):
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "require_token_expiration", False, raising=False)

    token = _token({"sub": "abc"})
    data = await vc.verify_jwt_token(token)

    assert data["sub"] == "abc"


@pytest.mark.asyncio
async def test_verify_jwt_token_expired(monkeypatch):
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)

    expired_token = _token({"x": 1}, exp_delta=-1)  # already expired
    with pytest.raises(HTTPException) as exc:
        await vc.verify_jwt_token(expired_token)

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Token has expired"


@pytest.mark.asyncio
async def test_verify_jwt_token_invalid_signature(monkeypatch):
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)

    bad_token = _token({"x": 1}, secret="other-secret")
    with pytest.raises(HTTPException) as exc:
        await vc.verify_jwt_token(bad_token)

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Invalid token"


@pytest.mark.asyncio
async def test_verify_credentials_enriches(monkeypatch):
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)

    tok = _token({"foo": "bar"})
    enriched = await vc.verify_credentials(tok)

    assert enriched["foo"] == "bar"
    assert enriched["token"] == tok


# ---------------------------------------------------------------------------
# require_auth
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_require_auth_header(monkeypatch):
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)

    tok = _token({"uid": 7})
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=tok)
    mock_request = Mock(spec=Request)
    mock_request.headers = {}
    mock_request.cookies = {}  # Empty cookies dict, not Mock

    payload = await vc.require_auth(request=mock_request, credentials=creds, jwt_token=None)
    assert payload["uid"] == 7


@pytest.mark.asyncio
async def test_require_auth_missing_token(monkeypatch):
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    mock_request = Mock(spec=Request)
    mock_request.headers = {}
    mock_request.cookies = {}  # Empty cookies dict, not Mock

    with pytest.raises(HTTPException) as exc:
        await vc.require_auth(request=mock_request, credentials=None, jwt_token=None)

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Not authenticated"


# ---------------------------------------------------------------------------
# Basic-auth helpers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verify_basic_credentials_success(monkeypatch):
    monkeypatch.setattr(vc.settings, "basic_auth_user", "alice", raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_password", SecretStr("secret"), raising=False)

    creds = HTTPBasicCredentials(username="alice", password="secret")
    assert await vc.verify_basic_credentials(creds) == "alice"


@pytest.mark.asyncio
async def test_verify_basic_credentials_failure(monkeypatch):
    monkeypatch.setattr(vc.settings, "basic_auth_user", "alice", raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_password", SecretStr("secret"), raising=False)

    creds = HTTPBasicCredentials(username="bob", password="wrong")
    with pytest.raises(HTTPException) as exc:
        await vc.verify_basic_credentials(creds)

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Invalid credentials"


@pytest.mark.asyncio
async def test_require_basic_auth_optional(monkeypatch):
    monkeypatch.setattr(vc.settings, "auth_required", False, raising=False)
    result = await vc.require_basic_auth(credentials=None)
    assert result == "anonymous"


@pytest.mark.asyncio
async def test_require_basic_auth_raises_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    with pytest.raises(HTTPException) as exc:
        await vc.require_basic_auth(None)

    err = exc.value
    assert err.status_code == status.HTTP_401_UNAUTHORIZED
    assert err.detail == "Not authenticated"
    assert err.headers["WWW-Authenticate"] == "Basic"


# ---------------------------------------------------------------------------
# require_auth_override
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_require_auth_override(monkeypatch):
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)

    header_token = _token({"h": 1})
    cookie_token = _token({"c": 2})

    # Header wins over cookie
    res1 = await vc.require_auth_override(auth_header=f"Bearer {header_token}", jwt_token=cookie_token)
    assert res1["h"] == 1

    # Only cookie present
    res2 = await vc.require_auth_override(auth_header=None, jwt_token=cookie_token)
    assert res2["c"] == 2


@pytest.mark.asyncio
async def test_require_auth_override_non_bearer(monkeypatch):
    # Arrange
    header = "Basic Zm9vOmJhcg=="  # non-Bearer scheme
    monkeypatch.setattr(vc.settings, "auth_required", False, raising=False)
    mock_request = Mock(spec=Request)
    mock_request.headers = {}
    mock_request.cookies = {}  # Empty cookies dict, not Mock

    # Act
    result = await vc.require_auth_override(auth_header=header)

    # Assert
    assert result == await vc.require_auth(request=mock_request, credentials=None, jwt_token=None)


@pytest.mark.asyncio
async def test_require_auth_override_basic_auth_enabled_success(monkeypatch):
    monkeypatch.setattr(vc.settings, "docs_allow_basic_auth", True, raising=False)
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_user", "alice", raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_password", SecretStr("secret"), raising=False)
    basic_auth_header = f"Basic {base64.b64encode('alice:secret'.encode()).decode()}"
    result = await vc.require_auth_override(auth_header=basic_auth_header)
    assert result == vc.settings.basic_auth_user
    assert result == "alice"


@pytest.mark.asyncio
async def test_require_auth_override_basic_auth_enabled_failure(monkeypatch):
    monkeypatch.setattr(vc.settings, "docs_allow_basic_auth", True, raising=False)
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_user", "alice", raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_password", SecretStr("secret"), raising=False)

    # case1. format is wrong
    header = "Basic fakeAuth"
    with pytest.raises(HTTPException) as exc:
        await vc.require_auth_override(auth_header=header)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Invalid basic auth credentials"

    # case2. username or password is wrong
    header = "Basic dGVzdDp0ZXN0"
    with pytest.raises(HTTPException) as exc:
        await vc.require_auth_override(auth_header=header)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Invalid credentials"


@pytest.mark.asyncio
async def test_require_auth_override_basic_auth_disabled(monkeypatch):
    monkeypatch.setattr(vc.settings, "docs_allow_basic_auth", False, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    header = "Basic dGVzdDp0ZXN0"
    with pytest.raises(HTTPException) as exc:
        await vc.require_auth_override(auth_header=header)
    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail == "Not authenticated"


@pytest.fixture
def test_client():
    if app is None:
        pytest.skip("FastAPI app not importable")
    return TestClient(app)


def create_test_jwt_token():
    """Create a valid JWT token for integration tests."""
    return _token({"sub": "integration-user"})


@pytest.mark.asyncio
async def test_docs_auth_with_basic_auth_enabled_bearer_still_works(monkeypatch):
    """CRITICAL: Verify Bearer auth still works when Basic Auth is enabled."""
    monkeypatch.setattr(vc.settings, "docs_allow_basic_auth", True, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_audience", "mcpgateway-api", raising=False)
    monkeypatch.setattr(vc.settings, "jwt_issuer", "mcpgateway", raising=False)
    # Create a valid JWT token
    token = _token({"sub": "testuser"})
    bearer_header = f"Bearer {token}"
    # Bearer auth should STILL work
    result = await vc.require_auth_override(auth_header=bearer_header)
    assert result["sub"] == "testuser"


@pytest.mark.asyncio
async def test_docs_both_auth_methods_work_simultaneously(monkeypatch):
    """Test that both auth methods work when Basic Auth is enabled."""
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    monkeypatch.setattr(vc.settings, "docs_allow_basic_auth", True, raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_user", "admin", raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_password", SecretStr("secret"), raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_audience", "mcpgateway-api", raising=False)
    monkeypatch.setattr(vc.settings, "jwt_issuer", "mcpgateway", raising=False)
    # Test 1: Basic Auth works
    basic_header = f"Basic {base64.b64encode(b'admin:secret').decode()}"
    result1 = await vc.require_auth_override(auth_header=basic_header)
    assert result1 == "admin"
    # Test 2: Bearer Auth still works
    token = _token({"sub": "jwtuser"})
    bearer_header = f"Bearer {token}"
    result2 = await vc.require_auth_override(auth_header=bearer_header)
    assert result2["sub"] == "jwtuser"


@pytest.mark.asyncio
async def test_docs_invalid_basic_auth_fails(monkeypatch):
    """Test that invalid Basic Auth returns 401 and does not fall back to Bearer."""
    monkeypatch.setattr(vc.settings, "auth_required", True, raising=False)
    monkeypatch.setattr(vc.settings, "docs_allow_basic_auth", True, raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_user", "admin", raising=False)
    monkeypatch.setattr(vc.settings, "basic_auth_password", SecretStr("correct"), raising=False)
    # Send wrong Basic Auth
    wrong_basic = f"Basic {base64.b64encode(b'admin:wrong').decode()}"
    with pytest.raises(HTTPException) as exc:
        await vc.require_auth_override(auth_header=wrong_basic)
    assert exc.value.status_code == 401


# Integration test for /docs endpoint (requires test_client fixture and create_test_jwt_token helper)
@pytest.mark.asyncio
async def test_integration_docs_endpoint_both_auth_methods(test_client, monkeypatch):
    """Integration test: /docs accepts both auth methods when enabled."""
    monkeypatch.setattr("mcpgateway.config.settings.docs_allow_basic_auth", True)
    monkeypatch.setattr("mcpgateway.config.settings.basic_auth_user", "admin")
    monkeypatch.setattr("mcpgateway.config.settings.basic_auth_password", SecretStr("changeme"))
    monkeypatch.setattr("mcpgateway.config.settings.jwt_secret_key", SECRET)
    monkeypatch.setattr("mcpgateway.config.settings.jwt_algorithm", ALGO)
    monkeypatch.setattr("mcpgateway.config.settings.jwt_audience", "mcpgateway-api")
    monkeypatch.setattr("mcpgateway.config.settings.jwt_issuer", "mcpgateway")
    # Test with Basic Auth
    basic_creds = base64.b64encode(b"admin:changeme").decode()
    response1 = test_client.get("/docs", headers={"Authorization": f"Basic {basic_creds}"})
    assert response1.status_code == 200
    # Test with Bearer token
    token = create_test_jwt_token()
    response2 = test_client.get("/docs", headers={"Authorization": f"Bearer {token}"})
    assert response2.status_code == 200


# ---------------------------------------------------------------------------
# External (Keycloak) JWT validation
# ---------------------------------------------------------------------------


def test_get_keycloak_issuer(monkeypatch):
    """Test Keycloak issuer URL construction from settings."""
    monkeypatch.setattr(vc.settings, "sso_keycloak_base_url", "https://keycloak.example.com", raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_realm", "myrealm", raising=False)
    assert vc._get_keycloak_issuer() == "https://keycloak.example.com/realms/myrealm"


def test_get_keycloak_issuer_strips_trailing_slash(monkeypatch):
    """Test that trailing slashes on the base URL are handled."""
    monkeypatch.setattr(vc.settings, "sso_keycloak_base_url", "https://keycloak.example.com/", raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_realm", "test", raising=False)
    assert vc._get_keycloak_issuer() == "https://keycloak.example.com/realms/test"


def test_get_keycloak_issuer_returns_none_when_unconfigured(monkeypatch):
    """Test that None is returned when no base URL is configured."""
    monkeypatch.setattr(vc.settings, "sso_keycloak_base_url", None, raising=False)
    assert vc._get_keycloak_issuer() is None


@pytest.mark.asyncio
async def test_external_jwt_disabled_by_default(monkeypatch):
    """Master switch: Keycloak tokens are rejected when external JWT validation is off."""
    monkeypatch.setattr(vc.settings, "external_jwt_validation_enabled", False, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)

    # Create a token that looks like Keycloak (different issuer)
    payload = {"sub": "user@example.com", "iss": "https://kc.example.com/realms/test", "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())}
    token = jwt.encode(payload, SECRET, algorithm=ALGO)

    # Should fail because issuer doesn't match internal "mcpgateway" issuer
    with pytest.raises(HTTPException) as exc:
        await vc.verify_jwt_token(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_internal_jwt_still_works_with_external_enabled(monkeypatch):
    """Backward compat: internal tokens validate normally even when external JWT validation is on."""
    monkeypatch.setattr(vc.settings, "external_jwt_validation_enabled", True, raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_base_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_realm", "test", raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)
    monkeypatch.setattr(vc.settings, "require_token_expiration", False, raising=False)

    # Internal token with iss=mcpgateway
    tok = _token({"sub": "internal-user"})
    result = await vc.verify_jwt_token(tok)
    assert result["sub"] == "internal-user"
    assert "_auth_source" not in result


@pytest.mark.asyncio
async def test_keycloak_issuer_rejects_hs256(monkeypatch):
    """Keycloak path must only accept RS256 — HS256 algorithm confusion must be prevented."""
    monkeypatch.setattr(vc.settings, "external_jwt_validation_enabled", True, raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_base_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_realm", "test", raising=False)
    monkeypatch.setattr(vc.settings, "external_jwt_required_audience", None, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)

    keycloak_issuer = "https://kc.example.com/realms/test"
    # Create an HS256 token with Keycloak issuer (algorithm confusion attack)
    payload = {
        "sub": "attacker@example.com",
        "iss": keycloak_issuer,
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    malicious_token = jwt.encode(payload, "any-secret", algorithm="HS256")

    # Mock the JWKS client — get_signing_key_from_jwt will fail because the token isn't RS256-signed
    mock_jwks_client = MagicMock()
    mock_jwks_client.get_signing_key_from_jwt.side_effect = Exception("No matching key found")
    with patch.object(vc, "_get_keycloak_jwks_client", return_value=mock_jwks_client):
        with pytest.raises(HTTPException) as exc:
            await vc.verify_jwt_token(malicious_token)
        assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_keycloak_jwt_validation_success(monkeypatch):
    """Full success path: RS256 Keycloak token validated via mocked JWKS client."""
    monkeypatch.setattr(vc.settings, "external_jwt_validation_enabled", True, raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_base_url", "https://kc.example.com", raising=False)
    monkeypatch.setattr(vc.settings, "sso_keycloak_realm", "test", raising=False)
    monkeypatch.setattr(vc.settings, "external_jwt_required_audience", None, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_secret_key", SECRET, raising=False)
    monkeypatch.setattr(vc.settings, "jwt_algorithm", ALGO, raising=False)

    keycloak_issuer = "https://kc.example.com/realms/test"

    # Generate an RSA key pair for signing
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()

    payload = {
        "sub": "keycloak-user-id",
        "email": "kc_user@example.com",
        "name": "KC User",
        "iss": keycloak_issuer,
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    private_pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    token = jwt.encode(payload, private_pem, algorithm="RS256")

    # Mock JWKS client to return the matching public key
    mock_signing_key = MagicMock()
    mock_signing_key.key = public_key
    mock_jwks_client = MagicMock()
    mock_jwks_client.get_signing_key_from_jwt.return_value = mock_signing_key

    with patch.object(vc, "_get_keycloak_jwks_client", return_value=mock_jwks_client):
        result = await vc.verify_jwt_token(token)

    assert result["_auth_source"] == "keycloak"
    assert result["email"] == "kc_user@example.com"
    assert result["sub"] == "keycloak-user-id"
