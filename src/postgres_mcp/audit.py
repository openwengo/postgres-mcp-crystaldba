from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CallerIdentity:
    """Log-friendly caller identity.

    Token claims identify the authenticated MCP client. The x-airunner headers
    identify the per-call workflow context and are caller-asserted audit fields,
    not authorization inputs.
    """

    actor: str
    issuer: str | None = None
    audience: str | None = None
    runner: str | None = None
    consumer_account: str | None = None
    job: str | None = None

    def as_log_fields(self) -> str:
        parts = [f"actor={self.actor}"]
        if self.runner:
            parts.append(f"runner={self.runner}")
        if self.consumer_account:
            parts.append(f"consumer={self.consumer_account}")
        if self.job:
            parts.append(f"job={self.job}")
        if self.issuer:
            parts.append(f"iss={self.issuer}")
        if self.audience:
            parts.append(f"aud={self.audience}")
        return " ".join(parts)


_AIRUNNER_HEADERS = {
    "runner": "x-airunner-identity",
    "consumer_account": "x-airunner-consumeraccount",
    "job": "x-airunner-job",
}


def log_safe(value: Any) -> str | None:
    """Collapse whitespace so caller-controlled values cannot forge log fields."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def _stringify_audience(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, (list, tuple)):
        joined = ",".join(str(a) for a in raw if a)
        return joined or None
    return str(raw)


def _airunner_headers() -> dict[str, str | None]:
    """Read per-call x-airunner-* headers from the current HTTP request."""
    try:
        from fastmcp.server.dependencies import get_http_headers

        headers = get_http_headers()
    except Exception:
        headers = {}

    return {field: log_safe((headers.get(header) or "").strip() or None) for field, header in _AIRUNNER_HEADERS.items()}


def get_caller_identity() -> CallerIdentity:
    """Return token identity plus x-airunner audit headers for the current call."""
    runner_fields = _airunner_headers()

    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception:
        return CallerIdentity(actor="anonymous", **runner_fields)

    if token is None:
        return CallerIdentity(actor="anonymous", **runner_fields)

    claims = getattr(token, "claims", None) or {}

    def _claim(name: str) -> str | None:
        value = claims.get(name)
        if value is None:
            return None
        return log_safe(value)

    actor = _claim("email") or _claim("name")
    if actor is None:
        sub = _claim("sub")
        if sub is not None:
            actor = sub if not sub.isdigit() else f"google-id:{sub}"
    if actor is None:
        client_id = getattr(token, "client_id", None)
        actor = log_safe(client_id)
    if not actor:
        actor = "unknown"

    return CallerIdentity(
        actor=actor,
        issuer=_claim("iss"),
        audience=_stringify_audience(claims.get("aud")),
        **runner_fields,
    )
