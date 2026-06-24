import ast
from typing import Any
from typing import cast

import pytest

from postgres_mcp.config import AccessMode
from postgres_mcp.config import ConnectionSelection
from postgres_mcp.config import DatabaseConnectionConfig
from postgres_mcp.config import DatabasesConfig
from postgres_mcp.server import configure_database_connections
from postgres_mcp.server import execute_sql_ro
from postgres_mcp.server import get_registered_tool
from postgres_mcp.server import get_sql_driver
from postgres_mcp.server import list_connections
from postgres_mcp.server import register_execute_sql_tool
from postgres_mcp.sql import SafeSqlDriver
from postgres_mcp.sql import SqlDriver


def _server_config(
    connection_selection: ConnectionSelection = "strict",
    main_mode: AccessMode = AccessMode.RESTRICTED,
    analytics_mode: AccessMode = AccessMode.UNRESTRICTED,
):
    return DatabasesConfig(
        connections={
            "main": DatabaseConnectionConfig(
                name="main",
                uri="postgresql://user:mainpass@main/db",
                mode=main_mode,
            ),
            "analytics": DatabaseConnectionConfig(
                name="analytics",
                uri="postgresql://user:analyticspass@analytics/db",
                mode=analytics_mode,
            ),
        },
        default_connection="main",
        connection_selection=connection_selection,
    )


@pytest.mark.asyncio
async def test_get_sql_driver_requires_connection_in_strict_mode():
    configure_database_connections(_server_config(connection_selection="strict"))

    with pytest.raises(ValueError, match="connection parameter is required"):
        await get_sql_driver()


@pytest.mark.asyncio
async def test_get_sql_driver_uses_default_in_default_selection_mode():
    configure_database_connections(_server_config(connection_selection="default"))

    driver = await get_sql_driver()

    assert isinstance(driver, SafeSqlDriver)
    assert driver.connection_name == "main"


@pytest.mark.asyncio
async def test_get_sql_driver_selects_connection_mode():
    configure_database_connections(_server_config(connection_selection="strict"))

    main_driver = await get_sql_driver("main")
    analytics_driver = await get_sql_driver("analytics")

    assert isinstance(main_driver, SafeSqlDriver)
    assert main_driver.connection_name == "main"
    assert isinstance(analytics_driver, SqlDriver)
    assert not isinstance(analytics_driver, SafeSqlDriver)
    assert analytics_driver.connection_name == "analytics"


@pytest.mark.asyncio
async def test_get_sql_driver_unknown_connection_lists_available_connections():
    configure_database_connections(_server_config(connection_selection="strict"))

    with pytest.raises(ValueError, match="Available connections: analytics, main"):
        await get_sql_driver("missing")


@pytest.mark.asyncio
async def test_get_sql_driver_force_restricted_overrides_unrestricted_connection():
    configure_database_connections(_server_config(connection_selection="strict"))

    driver = await get_sql_driver("analytics", force_restricted=True)

    assert isinstance(driver, SafeSqlDriver)
    assert driver.connection_name == "analytics"


@pytest.mark.asyncio
async def test_execute_sql_ro_uses_force_restricted_driver(monkeypatch):
    called = {}

    async def fake_get_sql_driver(connection=None, force_restricted=False):
        called["connection"] = connection
        called["force_restricted"] = force_restricted

        class FakeDriver:
            async def execute_query(self, sql):
                return [SqlDriver.RowResult(cells={"value": 1})]

        return FakeDriver()

    monkeypatch.setattr("postgres_mcp.server.get_sql_driver", fake_get_sql_driver)

    result = await execute_sql_ro(sql="SELECT 1", connection="analytics")

    assert called == {"connection": "analytics", "force_restricted": True}
    assert result[0].text == "[{'value': 1}]"


@pytest.mark.asyncio
async def test_list_connections_hides_uris_and_obfuscates_errors():
    configure_database_connections(_server_config(connection_selection="strict"))

    import postgres_mcp.server as server

    cast(Any, server.db_connections["main"])._is_valid = True
    cast(Any, server.db_connections["analytics"])._last_error = "could not connect to postgresql://user:analyticspass@analytics/db"

    result = await list_connections()
    payload = ast.literal_eval(result[0].text)

    assert payload[0]["name"] == "main"
    assert payload[0]["is_default"] is True
    assert payload[0]["status"] == "connected"
    assert payload[1]["name"] == "analytics"
    assert payload[1]["status"] == "error"
    assert "analyticspass" not in result[0].text
    assert "postgresql://user:****@analytics" in result[0].text


def test_register_execute_sql_tool_is_destructive_when_any_connection_is_unrestricted():
    configure_database_connections(_server_config(connection_selection="strict"))

    register_execute_sql_tool()

    tool = get_registered_tool("execute_sql")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.destructiveHint is True
    assert tool.annotations.readOnlyHint is None


def test_register_execute_sql_tool_is_read_only_when_all_connections_are_restricted():
    configure_database_connections(
        _server_config(
            connection_selection="strict",
            main_mode=AccessMode.RESTRICTED,
            analytics_mode=AccessMode.RESTRICTED,
        )
    )

    register_execute_sql_tool()

    tool = get_registered_tool("execute_sql")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is None


def test_execute_sql_ro_tool_is_always_read_only():
    tool = get_registered_tool("execute_sql_ro")
    assert tool is not None
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is None
