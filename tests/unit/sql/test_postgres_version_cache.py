from unittest.mock import AsyncMock

import pytest

from postgres_mcp.sql import SafeSqlDriver
from postgres_mcp.sql import SqlDriver
from postgres_mcp.sql.extension_utils import get_postgres_version


class MockDriver(SqlDriver):
    def __init__(self, connection_name: str, version: str):
        self.connection_name = connection_name
        self.execute_query_mock = AsyncMock(return_value=[SqlDriver.RowResult(cells={"server_version": version})])

    async def execute_query(self, query, params=None, force_readonly=False):
        return await self.execute_query_mock(query, params=params, force_readonly=force_readonly)


@pytest.mark.asyncio
async def test_postgres_version_cache_is_scoped_by_connection_name():
    pg12 = MockDriver("pg12", "12.17")
    pg16 = MockDriver("pg16", "16.2")

    assert await get_postgres_version(pg12) == 12
    assert await get_postgres_version(pg16) == 16
    assert await get_postgres_version(pg12) == 12
    assert await get_postgres_version(pg16) == 16

    pg12.execute_query_mock.assert_awaited_once()
    pg16.execute_query_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_postgres_version_cache_uses_wrapped_connection_name():
    base = MockDriver("analytics", "15.4")
    safe_driver = SafeSqlDriver(base)

    assert await get_postgres_version(safe_driver) == 15
    assert await get_postgres_version(safe_driver) == 15

    base.execute_query_mock.assert_awaited_once()
