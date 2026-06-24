from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Literal
from typing import SupportsFloat

from fastmcp.server.auth import AuthProvider
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from key_value.aio.protocols.key_value import AsyncKeyValue

logger = logging.getLogger(__name__)

StorageBackend = Literal["memory", "disk", "valkey"]

_DEFAULT_KEY_PREFIX = "postgres-mcp"
_DEFAULT_GOOGLE_SCOPES = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)


class _CollectionPrefixWrapper(AsyncKeyValue):
    """Prefix collection names so several MCP servers can share one Valkey."""

    def __init__(self, key_value: AsyncKeyValue, prefix: str) -> None:
        self._kv = key_value
        self._prefix = prefix

    def _prefixed(self, collection: str | None) -> str | None:
        if collection is None:
            return self._prefix
        return f"{self._prefix}__{collection}"

    async def get(self, key: str, *, collection: str | None = None) -> dict[str, Any] | None:
        return await self._kv.get(key=key, collection=self._prefixed(collection))

    async def get_many(self, keys: Sequence[str], *, collection: str | None = None) -> list[dict[str, Any] | None]:
        return await self._kv.get_many(keys=keys, collection=self._prefixed(collection))

    async def ttl(self, key: str, *, collection: str | None = None) -> tuple[dict[str, Any] | None, float | None]:
        return await self._kv.ttl(key=key, collection=self._prefixed(collection))

    async def ttl_many(self, keys: Sequence[str], *, collection: str | None = None) -> list[tuple[dict[str, Any] | None, float | None]]:
        return await self._kv.ttl_many(keys=keys, collection=self._prefixed(collection))

    async def put(self, key: str, value: Mapping[str, Any], *, collection: str | None = None, ttl: SupportsFloat | None = None) -> None:
        return await self._kv.put(key=key, value=value, collection=self._prefixed(collection), ttl=ttl)

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        return await self._kv.put_many(keys=keys, values=values, collection=self._prefixed(collection), ttl=ttl)

    async def delete(self, key: str, *, collection: str | None = None) -> bool:
        return await self._kv.delete(key=key, collection=self._prefixed(collection))

    async def delete_many(self, keys: Sequence[str], *, collection: str | None = None) -> int:
        return await self._kv.delete_many(keys=keys, collection=self._prefixed(collection))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _parse_google_scopes(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        return list(_DEFAULT_GOOGLE_SCOPES)
    parts = [s.strip() for s in raw.replace(",", " ").split()]
    return [s for s in parts if s]


def _parse_storage_backend(raw: str) -> StorageBackend:
    value = raw.strip().lower()
    if not value or value == "memory":
        return "memory"
    if value == "disk":
        return "disk"
    if value in {"valkey", "redis"}:
        return "valkey"
    raise ValueError(f"Unsupported POSTGRES_MCP_OAUTH_PROXY_STORAGE_BACKEND={raw!r}")


def _get_base_url(*, host: str, port: int) -> str:
    external_url = os.getenv("POSTGRES_MCP_EXTERNAL_URL", "").strip()
    if external_url:
        return external_url

    explicit = os.getenv("POSTGRES_MCP_BASE_URL", "").strip()
    if explicit:
        return explicit

    fastmcp_explicit = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_BASE_URL", "").strip()
    if fastmcp_explicit:
        return fastmcp_explicit

    if host in {"0.0.0.0", "127.0.0.1", "localhost"}:
        return f"http://localhost:{port}"

    return f"http://{host}:{port}"


def _derive_storage_fernet_key(*, google_client_secret: str) -> bytes:
    from fastmcp.server.auth.jwt_issuer import derive_jwt_key

    jwt_signing_key_override = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "").strip() or None
    if jwt_signing_key_override:
        if len(jwt_signing_key_override) < 12:
            logger.warning(
                "OAuth 2.1: FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY is less than 12 characters; "
                "use a longer secret to improve key derivation strength."
            )
        jwt_signing_key = derive_jwt_key(
            low_entropy_material=jwt_signing_key_override,
            salt="fastmcp-jwt-signing-key",
        )
    else:
        jwt_signing_key = derive_jwt_key(
            high_entropy_material=google_client_secret,
            salt="fastmcp-jwt-signing-key",
        )

    return derive_jwt_key(
        high_entropy_material=jwt_signing_key.decode(),
        salt="fastmcp-storage-encryption-key",
    )


def _create_oauth_proxy_client_storage(*, backend: StorageBackend, google_client_secret: str):
    from cryptography.fernet import Fernet
    from key_value.aio.stores.simple import SimpleStore

    key_prefix = _env_str("POSTGRES_MCP_OAUTH_PROXY_VALKEY_KEY_PREFIX", _DEFAULT_KEY_PREFIX)

    if backend == "memory":
        logger.info("OAuth 2.1: Using in-memory OAuth proxy client_storage")
        return SimpleStore()

    fernet = Fernet(key=_derive_storage_fernet_key(google_client_secret=google_client_secret))

    if backend == "disk":
        from key_value.aio.stores.disk import DiskStore
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        directory_raw = _env_str(
            "POSTGRES_MCP_OAUTH_PROXY_DISK_DIRECTORY",
            "~/.fastmcp/postgres-mcp/oauth-proxy",
        )
        directory = Path(os.path.expandvars(os.path.expanduser(directory_raw)))
        store = DiskStore(directory=directory)
        logger.info("OAuth 2.1: Using DiskStore for OAuth proxy client_storage (dir=%s)", directory)
        encrypted_store = FernetEncryptionWrapper(key_value=store, fernet=fernet)
        if key_prefix:
            return _CollectionPrefixWrapper(key_value=encrypted_store, prefix=key_prefix)
        return encrypted_store

    if backend == "valkey":
        from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

        try:
            from key_value.aio.stores.valkey import ValkeyStore
        except ImportError as exc:
            raise RuntimeError(
                "Valkey client_storage requested but Valkey dependencies are not installed. Install 'py-key-value-aio[valkey]'."
            ) from exc

        valkey_host = _env_str("POSTGRES_MCP_OAUTH_PROXY_VALKEY_HOST", "localhost")
        valkey_port = int(_env_str("POSTGRES_MCP_OAUTH_PROXY_VALKEY_PORT", "6379"))
        valkey_db = int(_env_str("POSTGRES_MCP_OAUTH_PROXY_VALKEY_DB", "0"))
        valkey_use_tls_raw = os.getenv("POSTGRES_MCP_OAUTH_PROXY_VALKEY_USE_TLS", "").strip().lower()
        if not valkey_use_tls_raw or valkey_use_tls_raw == "auto":
            valkey_use_tls = valkey_port == 6380
        else:
            valkey_use_tls = valkey_use_tls_raw in {"1", "true", "yes", "y", "on"}

        valkey_username = os.getenv("POSTGRES_MCP_OAUTH_PROXY_VALKEY_USERNAME", "").strip() or None
        valkey_password = os.getenv("POSTGRES_MCP_OAUTH_PROXY_VALKEY_PASSWORD", "").strip() or None
        valkey_request_timeout_ms = int(_env_str("POSTGRES_MCP_OAUTH_PROXY_VALKEY_REQUEST_TIMEOUT_MS", "5000"))
        valkey_connection_timeout_ms = int(_env_str("POSTGRES_MCP_OAUTH_PROXY_VALKEY_CONNECTION_TIMEOUT_MS", "10000"))

        store = ValkeyStore(
            host=valkey_host,
            port=valkey_port,
            db=valkey_db,
            username=valkey_username,
            password=valkey_password,
        )

        glide_config = getattr(store, "_client_config", None)
        if glide_config is not None:
            glide_config.use_tls = valkey_use_tls
            glide_config.request_timeout = valkey_request_timeout_ms
            try:
                from glide_shared.config import AdvancedGlideClientConfiguration

                glide_config.advanced_config = AdvancedGlideClientConfiguration(connection_timeout=valkey_connection_timeout_ms)
            except Exception:
                logger.debug("OAuth 2.1: Unable to configure Valkey connection timeout via glide_shared")

        logger.info(
            "OAuth 2.1: Using ValkeyStore for OAuth proxy client_storage (host=%s, port=%s, db=%s, tls=%s)",
            valkey_host,
            valkey_port,
            valkey_db,
            valkey_use_tls,
        )
        encrypted_store = FernetEncryptionWrapper(key_value=store, fernet=fernet)
        if key_prefix:
            logger.info("OAuth 2.1: Using collection key prefix %r to namespace Valkey keys", key_prefix)
            return _CollectionPrefixWrapper(key_value=encrypted_store, prefix=key_prefix)
        return encrypted_store

    raise ValueError(f"Unsupported POSTGRES_MCP_OAUTH_PROXY_STORAGE_BACKEND={backend!r}")


def _split_list(raw: str | None) -> list[str] | None:
    if not raw or not raw.strip():
        return None
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    return [p for p in parts if p] or None


def _split_one_or_list(raw: str | None) -> str | list[str] | None:
    items = _split_list(raw)
    if not items:
        return None
    return items[0] if len(items) == 1 else items


def _build_jwt_verifier(*, base_url: str) -> JWTVerifier:
    public_key = os.getenv("FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY", "").strip() or None
    jwks_uri = os.getenv("FASTMCP_SERVER_AUTH_JWT_JWKS_URI", "").strip() or None

    if not public_key and not jwks_uri:
        raise RuntimeError(
            "EXTERNAL_OAUTH21_PROVIDER=true or MCP_UNIFIED_AUTH=true requires JWT verification. "
            "Set FASTMCP_SERVER_AUTH_JWT_JWKS_URI or FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY, "
            "and typically FASTMCP_SERVER_AUTH_JWT_ISSUER and FASTMCP_SERVER_AUTH_JWT_AUDIENCE."
        )
    if public_key and jwks_uri:
        raise RuntimeError("Configure exactly one of FASTMCP_SERVER_AUTH_JWT_PUBLIC_KEY or FASTMCP_SERVER_AUTH_JWT_JWKS_URI, not both.")

    logger.info("OAuth 2.1: JWT verification via %s", "JWKS URI" if jwks_uri else "static public key")
    return JWTVerifier(
        public_key=public_key,
        jwks_uri=jwks_uri,
        issuer=_split_one_or_list(os.getenv("FASTMCP_SERVER_AUTH_JWT_ISSUER")),
        audience=_split_one_or_list(os.getenv("FASTMCP_SERVER_AUTH_JWT_AUDIENCE")),
        algorithm=os.getenv("FASTMCP_SERVER_AUTH_JWT_ALGORITHM", "").strip() or None,
        required_scopes=_split_list(os.getenv("FASTMCP_SERVER_AUTH_JWT_REQUIRED_SCOPES")),
        base_url=base_url,
    )


def _jwt_verifier_from_entry(entry: Mapping[str, Any], *, base_url: str) -> JWTVerifier:
    name = entry.get("name") or entry.get("issuer") or "<unnamed>"
    public_key = (entry.get("public_key") or "").strip() or None
    jwks_uri = (entry.get("jwks_uri") or "").strip() or None
    if bool(public_key) == bool(jwks_uri):
        raise RuntimeError(f"MCP_JWT_ISSUERS entry {name!r} must set exactly one of 'jwks_uri' or 'public_key'.")

    required = entry.get("required_scopes")
    if isinstance(required, str):
        required = _split_list(required)

    return JWTVerifier(
        public_key=public_key,
        jwks_uri=jwks_uri,
        issuer=entry.get("issuer") or None,
        audience=entry.get("audience") or None,
        algorithm=(entry.get("algorithm") or "").strip() or None,
        required_scopes=list(required) if required else None,
        base_url=base_url,
    )


def _parse_jwt_issuers(raw: str | None) -> list[Mapping[str, Any]] | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MCP_JWT_ISSUERS is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list) or not parsed:
        raise RuntimeError("MCP_JWT_ISSUERS must be a non-empty JSON list of issuer configs.")
    if not all(isinstance(e, Mapping) for e in parsed):
        raise RuntimeError("MCP_JWT_ISSUERS entries must each be a JSON object.")
    return parsed


def _build_jwt_verifiers(*, base_url: str) -> list[TokenVerifier]:
    issuers = _parse_jwt_issuers(os.getenv("MCP_JWT_ISSUERS"))
    if issuers is None:
        return [_build_jwt_verifier(base_url=base_url)]
    return [_jwt_verifier_from_entry(e, base_url=base_url) for e in issuers]


def _build_external_provider(*, base_url: str) -> AuthProvider:
    verifiers = _build_jwt_verifiers(base_url=base_url)
    if len(verifiers) == 1:
        return verifiers[0]
    logger.info("OAuth 2.1: External provider mode with %d JWT issuer(s) via MultiAuth", len(verifiers))
    return MultiAuth(verifiers=verifiers, base_url=base_url)


def _build_google_provider(*, base_url: str) -> GoogleProvider:
    client_id = (os.getenv("GOOGLE_OAUTH_CLIENT_ID") or os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("GOOGLE_OAUTH_CLIENT_SECRET") or os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_CLIENT_SECRET") or "").strip()

    if not client_id or not client_secret:
        raise RuntimeError(
            "OAuth 2.1 enabled but Google OAuth credentials are not configured. Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET."
        )

    storage_backend = _parse_storage_backend(os.getenv("POSTGRES_MCP_OAUTH_PROXY_STORAGE_BACKEND", "memory"))
    client_storage = _create_oauth_proxy_client_storage(
        backend=storage_backend,
        google_client_secret=client_secret,
    )
    required_scopes = _parse_google_scopes(os.getenv("GOOGLE_OAUTH_SCOPES"))
    jwt_signing_key_override = os.getenv("FASTMCP_SERVER_AUTH_GOOGLE_JWT_SIGNING_KEY", "").strip() or None

    logger.info(
        "OAuth 2.1: Google provider (base_url=%s, storage=%s, scopes=%s)",
        base_url,
        storage_backend,
        ",".join(required_scopes),
    )
    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=base_url,
        client_storage=client_storage,
        jwt_signing_key=jwt_signing_key_override,
        required_scopes=required_scopes,
    )


def build_auth_provider(*, host: str, port: int) -> AuthProvider | None:
    """Build the FastMCP auth provider.

    Authentication is used for audit identity only. Connection access remains
    controlled by the static database configuration.
    """
    if not _env_flag("MCP_ENABLE_OAUTH21", default=False):
        return None

    base_url = _get_base_url(host=host, port=port)

    if _env_flag("MCP_UNIFIED_AUTH", default=False):
        logger.info("OAuth 2.1: Unified mode (Google OAuth + bearer JWT) on one endpoint")
        return MultiAuth(
            server=_build_google_provider(base_url=base_url),
            verifiers=_build_jwt_verifiers(base_url=base_url),
            base_url=base_url,
            required_scopes=[],
        )

    if _env_flag("EXTERNAL_OAUTH21_PROVIDER", default=False):
        logger.info("OAuth 2.1: External provider mode enabled; validating bearer JWTs")
        return _build_external_provider(base_url=base_url)

    return _build_google_provider(base_url=base_url)
