import json

import pytest

from postgres_mcp.config import AccessMode
from postgres_mcp.config import load_database_config
from postgres_mcp.config import parse_database_connections_config


def _config(connection_selection=None, connections=None):
    data = {
        "connections": connections
        or {
            "main": {"uri": "postgresql://user:pass@main/db", "mode": "restricted"},
            "analytics": {"uri": "postgresql://user:pass@analytics/db", "mode": "unrestricted"},
        },
        "default_connection": "main",
    }
    if connection_selection is not None:
        data["connection_selection"] = connection_selection
    return json.dumps(data)


def test_parse_database_connections_config_valid():
    config = parse_database_connections_config(_config(connection_selection="strict"))

    assert config.default_connection == "main"
    assert config.connection_selection == "strict"
    assert config.connections["main"].mode == AccessMode.RESTRICTED
    assert config.connections["analytics"].mode == AccessMode.UNRESTRICTED
    assert config.is_legacy is False


def test_parse_database_connections_defaults_to_strict_for_multiple_connections():
    config = parse_database_connections_config(_config())

    assert config.connection_selection == "strict"


def test_parse_database_connections_defaults_to_default_for_single_connection():
    config = parse_database_connections_config(
        _config(
            connections={
                "main": {"uri": "postgresql://user:pass@main/db", "mode": "restricted"},
            },
        )
    )

    assert config.connection_selection == "default"


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, "connections must be a non-empty object"),
        ({"connections": {}, "default_connection": "main"}, "connections must be a non-empty object"),
        ({"connections": {"main": "postgresql://user:pass@main/db"}, "default_connection": "main"}, "must be an object"),
        ({"connections": {"main": {"uri": 123, "mode": "restricted"}}, "default_connection": "main"}, "uri must be a non-empty string"),
        (
            {"connections": {"main": {"uri": "postgresql://user:pass@main/db", "mode": "bad"}}, "default_connection": "main"},
            "must be 'restricted' or 'unrestricted'",
        ),
        (
            {"connections": {"main": {"uri": "postgresql://user:pass@main/db", "mode": "restricted"}}, "default_connection": "missing"},
            "is not configured",
        ),
        (
            {
                "connections": {"main": {"uri": "postgresql://user:pass@main/db", "mode": "restricted"}},
                "default_connection": "main",
                "connection_selection": "bad",
            },
            "connection_selection must be either",
        ),
    ],
)
def test_parse_database_connections_config_validation_errors(payload, expected):
    with pytest.raises(ValueError, match=expected):
        parse_database_connections_config(json.dumps(payload))


def test_parse_database_connections_config_rejects_invalid_json():
    with pytest.raises(ValueError, match="Invalid DATABASE_CONNECTIONS JSON"):
        parse_database_connections_config("{")


def test_load_database_config_uses_database_connections_before_legacy_uri():
    config = load_database_config(
        database_connections=_config(connection_selection="strict"),
        database_uri="postgresql://legacy:pass@legacy/db",
        positional_database_url=None,
        legacy_access_mode=AccessMode.UNRESTRICTED,
    )

    assert set(config.connections) == {"main", "analytics"}
    assert config.is_legacy is False


def test_load_database_config_builds_legacy_config_from_database_uri():
    config = load_database_config(
        database_connections=None,
        database_uri="postgresql://legacy:pass@legacy/db",
        positional_database_url=None,
        legacy_access_mode=AccessMode.RESTRICTED,
    )

    assert list(config.connections) == ["default"]
    assert config.default_connection == "default"
    assert config.connection_selection == "default"
    assert config.connections["default"].uri == "postgresql://legacy:pass@legacy/db"
    assert config.connections["default"].mode == AccessMode.RESTRICTED
    assert config.is_legacy is True


def test_load_database_config_requires_some_database_config():
    with pytest.raises(ValueError, match="No database configuration provided"):
        load_database_config(
            database_connections=None,
            database_uri=None,
            positional_database_url=None,
            legacy_access_mode=AccessMode.UNRESTRICTED,
        )
