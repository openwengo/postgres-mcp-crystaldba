import asyncio
import sys
from typing import Any
from typing import Generator
from typing import cast

import pytest
from dotenv import load_dotenv
from utils import create_postgres_container

from postgres_mcp.sql import reset_postgres_version_cache

load_dotenv()


# Define a custom event loop policy that handles cleanup better
@pytest.fixture(scope="session")
def event_loop_policy():
    """Create and return a custom event loop policy for tests."""
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture(scope="class", params=["postgres:12", "postgres:15", "postgres:16"])
def test_postgres_connection_string(request) -> Generator[tuple[str, str], None, None]:
    yield from create_postgres_container(request.param)


@pytest.fixture(autouse=True)
def reset_pg_version_cache():
    """Reset the PostgreSQL version cache before each test."""
    reset_postgres_version_cache()
    yield


@pytest.fixture(autouse=True)
def reset_server_connection_state():
    """Reset mutable server connection globals after tests that import the server module."""
    yield
    server = cast(Any, sys.modules.get("postgres_mcp.server"))
    if server is None:
        return

    server.db_connections = {}
    server.connection_configs = {}
    server.default_connection_name = "default"
    server.connection_selection = "default"
    server.current_access_mode = server.AccessMode.UNRESTRICTED
    server.shutdown_in_progress = False
    server.mcp.auth = None
    server.db_connection.connection_url = None
    server.db_connection.pool = None
    server.db_connection._is_valid = False
    server.db_connection._last_error = None
