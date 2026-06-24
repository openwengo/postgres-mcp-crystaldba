from __future__ import annotations

import json

import pytest

from postgres_mcp.oauth import build_auth_provider

_AUTH_VARS = [
    "MCP_ENABLE_OAUTH21",
    "MCP_UNIFIED_AUTH",
    "EXTERNAL_OAUTH21_PROVIDER",
    "MCP_JWT_ISSUERS",
    "FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY",
    "FASTMCP_SERVER_AUTH_JWT_JWKS_URI",
    "FASTMCP_SERVER_AUTH_JWT_ISSUER",
    "FASTMCP_SERVER_AUTH_JWT_AUDIENCE",
    "FASTMCP_SERVER_AUTH_JWT_ALGORITHM",
    "FASTMCP_SERVER_AUTH_JWT_REQUIRED_SCOPES",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "GOOGLE_OAUTH_SCOPES",
    "POSTGRES_MCP_BASE_URL",
    "POSTGRES_MCP_EXTERNAL_URL",
    "POSTGRES_MCP_OAUTH_PROXY_STORAGE_BACKEND",
]


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch):
    for var in _AUTH_VARS:
        monkeypatch.setenv(var, "")


def test_no_auth_when_disabled(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "false")
    assert build_auth_provider(host="0.0.0.0", port=8000) is None


def test_external_provider_without_jwt_config_fails_fast(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "true")

    with pytest.raises(RuntimeError) as excinfo:
        build_auth_provider(host="0.0.0.0", port=8000)

    assert "requires JWT verification" in str(excinfo.value)


def test_external_provider_with_jwks_builds_verifier(monkeypatch):
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "true")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_JWKS_URI", "https://www.googleapis.com/oauth2/v3/certs")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_ISSUER", "https://accounts.google.com")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_AUDIENCE", "postgres-mcp-bots")

    provider = build_auth_provider(host="0.0.0.0", port=8000)

    assert isinstance(provider, JWTVerifier)


def test_external_provider_rejects_both_key_and_jwks(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "true")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY", "-----BEGIN PUBLIC KEY-----")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_JWKS_URI", "https://example.com/certs")

    with pytest.raises(RuntimeError, match="exactly one"):
        build_auth_provider(host="0.0.0.0", port=8000)


def test_multi_issuer_builds_multiauth(monkeypatch):
    from fastmcp.server.auth import MultiAuth

    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "true")
    monkeypatch.setenv(
        "MCP_JWT_ISSUERS",
        json.dumps(
            [
                {
                    "name": "eks-prod",
                    "jwks_uri": "https://oidc.eks.eu-west-3.amazonaws.com/id/PROD/keys",
                    "issuer": "https://oidc.eks.eu-west-3.amazonaws.com/id/PROD",
                    "audience": "postgres-mcp-bots",
                },
                {
                    "name": "google-wif",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "issuer": "https://accounts.google.com",
                    "audience": "postgres-mcp-bots",
                },
            ]
        ),
    )

    provider = build_auth_provider(host="0.0.0.0", port=8000)

    assert isinstance(provider, MultiAuth)


def test_multi_issuer_invalid_json_fails_fast(monkeypatch):
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("EXTERNAL_OAUTH21_PROVIDER", "true")
    monkeypatch.setenv("MCP_JWT_ISSUERS", "{not-json")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        build_auth_provider(host="0.0.0.0", port=8000)


def test_google_provider_uses_valkey_backend(monkeypatch):
    from fastmcp.server.auth.providers.google import GoogleProvider
    from key_value.aio.stores.simple import SimpleStore

    captured = {}

    def fake_storage(*, backend, google_client_secret):
        captured["backend"] = backend
        captured["secret"] = google_client_secret
        return SimpleStore()

    monkeypatch.setattr("postgres_mcp.oauth._create_oauth_proxy_client_storage", fake_storage)
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("POSTGRES_MCP_OAUTH_PROXY_STORAGE_BACKEND", "valkey")

    provider = build_auth_provider(host="0.0.0.0", port=8000)

    assert isinstance(provider, GoogleProvider)
    assert captured == {"backend": "valkey", "secret": "client-secret"}


def test_unified_auth_builds_multiauth_without_global_scope_gate(monkeypatch):
    from fastmcp.server.auth import MultiAuth
    from key_value.aio.stores.simple import SimpleStore

    monkeypatch.setattr(
        "postgres_mcp.oauth._create_oauth_proxy_client_storage",
        lambda **_kwargs: SimpleStore(),
    )
    monkeypatch.setenv("MCP_ENABLE_OAUTH21", "true")
    monkeypatch.setenv("MCP_UNIFIED_AUTH", "true")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_JWKS_URI", "https://www.googleapis.com/oauth2/v3/certs")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_ISSUER", "https://accounts.google.com")
    monkeypatch.setenv("FASTMCP_SERVER_AUTH_JWT_AUDIENCE", "postgres-mcp-bots")

    provider = build_auth_provider(host="0.0.0.0", port=8000)

    assert isinstance(provider, MultiAuth)
    assert list(provider.required_scopes or []) == []
    assert provider.server is not None
    assert any("userinfo.email" in s for s in (provider.server.required_scopes or []))
