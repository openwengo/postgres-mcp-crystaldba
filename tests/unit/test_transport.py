import sys
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
async def test_transport_argument_parsing(transport):
    """Test that all transport options are parsed correctly."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            f"--transport={transport}",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.build_auth_provider", return_value=None) as mock_auth,
            patch("postgres_mcp.server.mcp.run_async", AsyncMock()) as mock_run,
        ):
            await main()

            if transport == "stdio":
                mock_auth.assert_not_called()
                mock_run.assert_awaited_once_with(transport="stdio")
            elif transport == "sse":
                mock_auth.assert_called_once_with(host="localhost", port=8000)
                mock_run.assert_awaited_once_with(transport="sse", host="localhost", port=8000)
            elif transport == "streamable-http":
                mock_auth.assert_called_once_with(host="localhost", port=8000)
                mock_run.assert_awaited_once_with(transport="http", host="localhost", port=8000, stateless_http=True)
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_streamable_http_host_port_arguments():
    """Test that streamable-http host and port arguments are applied correctly."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--transport=streamable-http",
            "--streamable-http-host=0.0.0.0",
            "--streamable-http-port=9000",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.build_auth_provider", return_value=None) as mock_auth,
            patch("postgres_mcp.server.mcp.run_async", AsyncMock()) as mock_run,
        ):
            await main()

            mock_auth.assert_called_once_with(host="0.0.0.0", port=9000)
            mock_run.assert_awaited_once_with(transport="http", host="0.0.0.0", port=9000, stateless_http=True)
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_sse_host_port_arguments():
    """Test that SSE host and port arguments are applied correctly."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
            "--transport=sse",
            "--sse-host=0.0.0.0",
            "--sse-port=8080",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.build_auth_provider", return_value=None) as mock_auth,
            patch("postgres_mcp.server.mcp.run_async", AsyncMock()) as mock_run,
        ):
            await main()

            mock_auth.assert_called_once_with(host="0.0.0.0", port=8080)
            mock_run.assert_awaited_once_with(transport="sse", host="0.0.0.0", port=8080)
    finally:
        sys.argv = original_argv


@pytest.mark.asyncio
async def test_default_transport_is_stdio():
    """Test that the default transport is stdio when not specified."""
    from postgres_mcp.server import main

    original_argv = sys.argv
    try:
        sys.argv = [
            "postgres_mcp",
            "postgresql://user:password@localhost/db",
        ]

        with (
            patch("postgres_mcp.server.db_connection.pool_connect", AsyncMock()),
            patch("postgres_mcp.server.build_auth_provider", return_value=None) as mock_auth,
            patch("postgres_mcp.server.mcp.run_async", AsyncMock()) as mock_run,
        ):
            await main()

            mock_auth.assert_not_called()
            mock_run.assert_awaited_once_with(transport="stdio")
    finally:
        sys.argv = original_argv


def test_configure_http_auth_sets_provider():
    from postgres_mcp.server import configure_http_auth
    from postgres_mcp.server import mcp

    provider = object()
    with patch("postgres_mcp.server.build_auth_provider", return_value=provider):
        configure_http_auth(host="0.0.0.0", port=9000)

    assert mcp.auth is provider
