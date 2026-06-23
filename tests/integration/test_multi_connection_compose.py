import ast
import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

RUN_COMPOSE_TESTS = os.environ.get("RUN_DOCKER_COMPOSE_TESTS") == "1"
COMPOSE_FILE = Path(__file__).resolve().parents[1] / "docker-compose.multi-connection.yml"
MCP_PORT = int(os.environ.get("POSTGRES_MCP_MULTI_CONNECTION_PORT", "18080"))
MCP_URL = f"http://127.0.0.1:{MCP_PORT}/mcp"


pytestmark = pytest.mark.skipif(not RUN_COMPOSE_TESTS, reason="Set RUN_DOCKER_COMPOSE_TESTS=1 to run Docker Compose integration tests")


def _run_compose(*args):
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        check=True,
        text=True,
        capture_output=True,
    )


def _wait_for_port(host: str, port: int, timeout_seconds: int = 90):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise TimeoutError(f"Timed out waiting for {host}:{port}: {last_error}")


def _wait_for_mcp_http(url: str, timeout_seconds: int = 60):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, headers={"Accept": "text/event-stream"}, timeout=2)
            if response.status_code < 500:
                return
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise TimeoutError(f"Timed out waiting for MCP HTTP readiness at {url}: {last_error}")


@pytest.fixture(scope="module")
def multi_connection_compose_stack():
    try:
        _run_compose("up", "--build", "-d", "postgres-mcp")
        _wait_for_port("127.0.0.1", MCP_PORT)
        _wait_for_mcp_http(MCP_URL)
        yield
    finally:
        _run_compose("down", "-v", "--remove-orphans")


async def _call_tool(session: ClientSession, name: str, arguments: dict[str, object]) -> str:
    result = await session.call_tool(name, arguments)
    assert result.content
    content = result.content[0]
    assert isinstance(content, TextContent)
    return content.text


@pytest.mark.asyncio
async def test_multi_connection_tools_via_docker_compose(multi_connection_compose_stack):
    async with streamable_http_client(MCP_URL) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            connections_text = await _call_tool(session, "list_connections", {})
            connections = ast.literal_eval(connections_text)
            assert [connection["name"] for connection in connections] == ["main", "analytics"]
            assert connections[0]["is_default"] is True
            assert connections[0]["mode"] == "restricted"
            assert connections[1]["mode"] == "unrestricted"

            strict_error = await _call_tool(session, "execute_sql_ro", {"sql": "SELECT 1"})
            assert "connection parameter is required" in strict_error

            main_marker = await _call_tool(session, "execute_sql_ro", {"connection": "main", "sql": "SELECT name FROM connection_marker"})
            analytics_marker = await _call_tool(
                session,
                "execute_sql_ro",
                {"connection": "analytics", "sql": "SELECT name FROM connection_marker"},
            )
            assert ast.literal_eval(main_marker) == [{"name": "main"}]
            assert ast.literal_eval(analytics_marker) == [{"name": "analytics"}]

            readonly_error = await _call_tool(session, "execute_sql_ro", {"connection": "analytics", "sql": "DROP TABLE connection_marker"})
            assert "Error:" in readonly_error

            unrestricted_result = await _call_tool(
                session,
                "execute_sql",
                {
                    "connection": "analytics",
                    "sql": "CREATE TABLE IF NOT EXISTS unrestricted_probe (id integer); DROP TABLE unrestricted_probe;",
                },
            )
            assert unrestricted_result == "No results"
