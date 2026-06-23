"""Configuration parsing for PostgreSQL MCP database connections."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from .sql import obfuscate_password


class AccessMode(str, Enum):
    """SQL access modes for a configured connection."""

    UNRESTRICTED = "unrestricted"
    RESTRICTED = "restricted"


ConnectionSelection = Literal["strict", "default"]

DATABASE_CONNECTIONS_ENV = "DATABASE_CONNECTIONS"
DATABASE_URI_ENV = "DATABASE_URI"

_CONNECTION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class DatabaseConnectionConfig:
    """Configuration for one named PostgreSQL connection."""

    name: str
    uri: str
    mode: AccessMode


@dataclass(frozen=True)
class DatabasesConfig:
    """Configuration for all PostgreSQL connections exposed by the server."""

    connections: dict[str, DatabaseConnectionConfig]
    default_connection: str
    connection_selection: ConnectionSelection
    is_legacy: bool = False


def _config_error(message: str) -> ValueError:
    return ValueError(obfuscate_password(message) or message)


def _parse_access_mode(value: object, context: str) -> AccessMode:
    if not isinstance(value, str):
        raise _config_error(f"{context} must be a string: 'restricted' or 'unrestricted'")
    try:
        return AccessMode(value)
    except ValueError as exc:
        raise _config_error(f"{context} must be 'restricted' or 'unrestricted', got {value!r}") from exc


def _validate_connection_name(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise _config_error("Connection names must be non-empty strings")
    if not _CONNECTION_NAME_RE.fullmatch(name):
        raise _config_error(
            f"Invalid connection name {name!r}. Use only letters, numbers, underscores, dashes, and dots.",
        )
    return name


def parse_database_connections_config(raw_config: str) -> DatabasesConfig:
    """Parse DATABASE_CONNECTIONS JSON."""
    try:
        data = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise _config_error(f"Invalid {DATABASE_CONNECTIONS_ENV} JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise _config_error(f"{DATABASE_CONNECTIONS_ENV} must be a JSON object")

    raw_connections = data.get("connections")
    if not isinstance(raw_connections, dict) or not raw_connections:
        raise _config_error(f"{DATABASE_CONNECTIONS_ENV}.connections must be a non-empty object")

    connections: dict[str, DatabaseConnectionConfig] = {}
    for raw_name, raw_connection in raw_connections.items():
        name = _validate_connection_name(raw_name)
        if not isinstance(raw_connection, dict):
            raise _config_error(f"Connection {name!r} must be an object with 'uri' and 'mode'")

        uri = raw_connection.get("uri")
        if not isinstance(uri, str) or not uri:
            raise _config_error(f"Connection {name!r} uri must be a non-empty string")

        mode = _parse_access_mode(raw_connection.get("mode"), f"Connection {name!r} mode")
        connections[name] = DatabaseConnectionConfig(name=name, uri=uri, mode=mode)

    default_connection = data.get("default_connection")
    default_connection = _validate_connection_name(default_connection)
    if default_connection not in connections:
        available = ", ".join(sorted(connections))
        raise _config_error(f"default_connection {default_connection!r} is not configured. Available connections: {available}")

    raw_selection = data.get("connection_selection")
    if raw_selection is None:
        connection_selection: ConnectionSelection = "strict" if len(connections) > 1 else "default"
    elif raw_selection in ("strict", "default"):
        connection_selection = raw_selection
    else:
        raise _config_error("connection_selection must be either 'strict' or 'default'")

    return DatabasesConfig(
        connections=connections,
        default_connection=default_connection,
        connection_selection=connection_selection,
    )


def build_legacy_database_config(database_url: str, access_mode: AccessMode) -> DatabasesConfig:
    """Build the internal config used for legacy DATABASE_URI or positional URL mode."""
    return DatabasesConfig(
        connections={
            "default": DatabaseConnectionConfig(
                name="default",
                uri=database_url,
                mode=access_mode,
            )
        },
        default_connection="default",
        connection_selection="default",
        is_legacy=True,
    )


def load_database_config(
    database_connections: str | None,
    database_uri: str | None,
    positional_database_url: str | None,
    legacy_access_mode: AccessMode,
) -> DatabasesConfig:
    """Load database configuration from multi-connection or legacy inputs."""
    if database_connections:
        return parse_database_connections_config(database_connections)

    database_url = database_uri or positional_database_url
    if database_url:
        return build_legacy_database_config(database_url, legacy_access_mode)

    raise _config_error(
        f"Error: No database configuration provided. Set {DATABASE_CONNECTIONS_ENV}, {DATABASE_URI_ENV}, or a database URL argument.",
    )
