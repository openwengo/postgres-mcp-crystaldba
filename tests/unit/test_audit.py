from __future__ import annotations

from types import SimpleNamespace

from postgres_mcp.audit import get_caller_identity
from postgres_mcp.audit import log_safe


def test_get_caller_identity_returns_anonymous_without_token(monkeypatch):
    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", lambda: {})
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: None)

    identity = get_caller_identity()

    assert identity.actor == "anonymous"
    assert identity.as_log_fields() == "actor=anonymous"


def test_get_caller_identity_reads_token_claims(monkeypatch):
    token = SimpleNamespace(
        claims={
            "email": "alice@example.com",
            "iss": "https://accounts.google.com",
            "aud": ["postgres-mcp", "postgres-mcp-bots"],
        },
        client_id="ignored-client",
    )
    monkeypatch.setattr("fastmcp.server.dependencies.get_http_headers", lambda: {})
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: token)

    identity = get_caller_identity()

    assert identity.actor == "alice@example.com"
    assert identity.issuer == "https://accounts.google.com"
    assert identity.audience == "postgres-mcp,postgres-mcp-bots"


def test_get_caller_identity_uses_airunner_headers_with_token(monkeypatch):
    token = SimpleNamespace(
        claims={
            "sub": "system:serviceaccount:workers:runner",
            "iss": "https://oidc.eks.eu-west-3.amazonaws.com/id/PROD",
            "aud": "postgres-mcp-bots",
        },
        client_id=None,
    )
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda: {
            "x-airunner-identity": "sea-analysis@12",
            "x-airunner-consumeraccount": "acme-portal",
            "x-airunner-job": "job-123",
        },
    )
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: token)

    identity = get_caller_identity()

    assert identity.as_log_fields() == (
        "actor=system:serviceaccount:workers:runner "
        "runner=sea-analysis@12 consumer=acme-portal job=job-123 "
        "iss=https://oidc.eks.eu-west-3.amazonaws.com/id/PROD aud=postgres-mcp-bots"
    )


def test_get_caller_identity_logs_airunner_headers_without_token(monkeypatch):
    monkeypatch.setattr(
        "fastmcp.server.dependencies.get_http_headers",
        lambda: {
            "x-airunner-identity": "runner-a",
            "x-airunner-consumeraccount": "consumer-a",
            "x-airunner-job": "job-a",
        },
    )
    monkeypatch.setattr("fastmcp.server.dependencies.get_access_token", lambda: None)

    identity = get_caller_identity()

    assert identity.as_log_fields() == "actor=anonymous runner=runner-a consumer=consumer-a job=job-a"


def test_log_safe_collapses_log_injection_values():
    assert log_safe("runner\nactor=root\t job=x") == "runner actor=root job=x"
