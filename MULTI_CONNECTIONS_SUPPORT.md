# Multi-Connection Support

This document describes the work needed to add first-class support for multiple named PostgreSQL connections to this MCP server.

## Goal

Allow one running MCP server to expose several configured PostgreSQL connections. Clients should be able to:

- list the available connections;
- select a connection by name on every database-backed tool;
- optionally require explicit connection selection on every database-backed tool call;
- support a configured default connection when the server is not running in strict selection mode;
- keep the existing single-connection `DATABASE_URI` behavior working as legacy mode.

## Configuration Contract

Add a JSON configuration environment variable named `DATABASE_CONNECTIONS`.

Canonical shape:

```json
{
  "connections": {
    "main": {
      "uri": "postgresql://postgres:postgres@postgres-main:5432/main_db",
      "mode": "restricted"
    },
    "analytics": {
      "uri": "postgresql://postgres:postgres@postgres-analytics:5432/analytics_db",
      "mode": "unrestricted"
    }
  },
  "default_connection": "main",
  "connection_selection": "strict"
}
```

Notes:

- `connections` is required in multi-connection mode and must be a non-empty object.
- Each key in `connections` is the public connection name exposed to MCP clients.
- Each value in `connections` is an object with a `uri` and `mode`.
- `uri` is a PostgreSQL connection URI.
- `mode` is either `restricted` or `unrestricted` and controls that specific connection.
- `default_connection` is required and must match one configured connection name.
- `connection_selection` controls whether omitted `connection` tool parameters are allowed:
  - `strict`: database-backed tools must receive a non-empty `connection` parameter. The configured default is not used automatically.
  - `default`: omitted or empty `connection` parameters use `default_connection`.
- If `connection_selection` is omitted, use `strict` when more than one database connection is configured and `default` when exactly one connection is configured. Legacy `DATABASE_URI` mode always uses `default` to preserve backward compatibility.
- Use English/American spelling only in the public API and config: `connection`, `connections`, and `default_connection`. Do not accept French aliases such as `connexion`, `default_connexion`, or `defaut_connexion`.
- Connection names should be stable, human-readable identifiers such as `default`, `prod-readonly`, `staging`, or `analytics`. Keep names simple: letters, numbers, underscores, dashes, and dots are enough.

Legacy mode:

If `DATABASE_CONNECTIONS` is not set and `DATABASE_URI` is set, generate this internal configuration:

```json
{
  "connections": {
    "default": {
      "uri": "<DATABASE_URI>",
      "mode": "<--access-mode value>"
    }
  },
  "default_connection": "default",
  "connection_selection": "default"
}
```

Precedence:

1. If `DATABASE_CONNECTIONS` is set, use it.
2. Else if `DATABASE_URI` or positional `database_url` is set, enter legacy mode.
3. Else fail startup with a clear configuration error.

Do not include raw connection URIs in MCP tool output. Logs and errors must continue to use password obfuscation.

## Current Code Shape

The current implementation is single-connection oriented:

- `src/postgres_mcp/server.py` owns one global `db_connection = DbConnPool()`.
- `get_sql_driver()` always wraps that single pool.
- Every database-backed tool calls `get_sql_driver()` without arguments.
- Startup reads one database URL from `DATABASE_URI` or the positional CLI argument.
- Shutdown closes only the single global pool.
- `src/postgres_mcp/sql/sql_driver.py` already has a reusable `DbConnPool`, so multiple pools can be represented as multiple `DbConnPool` instances.
- `src/postgres_mcp/sql/extension_utils.py` has a global PostgreSQL version cache and already notes that it must become connection-specific for multi-connection support.

## Implementation Plan

### 1. Add Configuration Parsing

Create a small configuration model, preferably in a new module such as `src/postgres_mcp/config.py`.

Suggested dataclasses:

```python
@dataclass(frozen=True)
class DatabaseConnectionConfig:
    name: str
    uri: str
    mode: AccessMode


@dataclass(frozen=True)
class DatabasesConfig:
    connections: dict[str, DatabaseConnectionConfig]
    default_connection: str
    connection_selection: Literal["strict", "default"]
```

Parser responsibilities:

- parse `DATABASE_CONNECTIONS` as JSON;
- validate the top-level object;
- validate `connections` is non-empty;
- validate each connection entry is an object;
- validate every `uri` is a string;
- validate every `mode` is either `restricted` or `unrestricted`;
- validate `default_connection` exists in `connections`;
- validate `connection_selection`, defaulting it according to the rules above;
- support legacy `DATABASE_URI` by generating a one-connection config;
- obfuscate passwords in every raised error;
- produce deterministic ordering for `list_connections` output.

Do not let malformed multi-connection JSON silently fall back to `DATABASE_URI`. If `DATABASE_CONNECTIONS` is present but invalid, fail fast.

### 2. Replace the Global Pool with a Registry

Replace:

```python
db_connection = DbConnPool()
```

with something like:

```python
db_connections: dict[str, DbConnPool] = {}
connection_configs: dict[str, DatabaseConnectionConfig] = {}
default_connection_name = "default"
connection_selection = "strict"  # Derived from config, or from the number of configured connections.
```

Initialize one `DbConnPool` per configured connection.

Startup must be lenient: try to initialize every configured pool, but start the MCP server even when one or more configured databases are temporarily unavailable. `list_connections` should expose each connection's current status, and the pool should be able to reconnect later when a tool uses it.

### 3. Make Driver Lookup Connection-Aware

Change:

```python
async def get_sql_driver() -> SqlDriver | SafeSqlDriver:
```

to:

```python
async def get_sql_driver(connection: str | None = None) -> SqlDriver | SafeSqlDriver:
```

Behavior:

- if `connection` is `None`, empty, or omitted and `connection_selection` is `strict`, raise an error explaining that the tool call must include `connection`;
- if `connection` is `None`, empty, or omitted and `connection_selection` is `default`, use `default_connection_name`;
- if the name is unknown, raise a clear error that includes available connection names;
- wrap the selected `DbConnPool` in `SqlDriver`;
- apply the selected connection's `mode`;
- wrap the selected `SqlDriver` in `SafeSqlDriver` only when that connection's mode is `restricted`.

`--access-mode` remains useful for legacy `DATABASE_URI` mode. In `DATABASE_CONNECTIONS` mode, each configured connection's `mode` is authoritative.

A mixed-mode server is a server with at least one `restricted` connection and at least one `unrestricted` connection, such as the canonical example where `main` is restricted and `analytics` is unrestricted.

Mixed-mode servers have an annotation caveat: MCP tool annotations are static, while the effective SQL access mode is selected at runtime from the `connection` parameter. To make this clear and safe, expose two SQL execution tools:

- `execute_sql_ro`: always read-only and always annotated with `readOnlyHint=True`.
- `execute_sql`: follows the selected connection's configured `mode` and is annotated conservatively.

`execute_sql_ro` should use the requested connection, but force restricted execution behavior for that selected connection. It must not silently switch to a different configured connection. For example, `execute_sql_ro(connection="analytics", ...)` still connects to `analytics`, but wraps the driver in `SafeSqlDriver` and forces a read-only transaction even if `analytics` is configured as `unrestricted`.

`execute_sql` should be registered with:

- `destructiveHint=True` when any configured connection is `unrestricted`;
- `readOnlyHint=True` only when every configured connection is `restricted`.

Runtime enforcement must still happen per selected connection. The annotation is only a conservative static hint to the MCP client.

### 4. Add `list_connections`

Add a new read-only MCP tool:

```python
@mcp.tool(
    description="List configured database connections",
    annotations=ToolAnnotations(
        title="List Connections",
        readOnlyHint=True,
    ),
)
async def list_connections() -> ResponseType:
    ...
```

Suggested output:

```json
[
  {
    "name": "main",
    "is_default": true,
    "mode": "restricted",
    "status": "connected"
  },
  {
    "name": "analytics",
    "is_default": false,
    "mode": "unrestricted",
    "status": "error",
    "last_error": "Connection attempt failed: ..."
  }
]
```

Rules:

- never return raw URIs;
- obfuscate any password-like content in `last_error`;
- sort by name, with the default connection first if desired;
- include enough status for clients to decide which connection to use.

### 5. Add `execute_sql_ro`

Add a dedicated read-only SQL execution tool:

```python
@mcp.tool(
    name="execute_sql_ro",
    description="Execute a read-only SQL query",
    annotations=ToolAnnotations(
        title="Execute SQL (Read-Only)",
        readOnlyHint=True,
    ),
)
async def execute_sql_ro(
    sql: str = Field(description="SQL to run"),
    connection: str = Field(
        description="Configured database connection name. Required when connection_selection is strict.",
        default="",
    ),
) -> ResponseType:
    ...
```

Behavior:

- select the requested connection using the same strict/default selection rules as other tools;
- ignore the selected connection's configured `mode`;
- always wrap the selected connection's base `SqlDriver` in `SafeSqlDriver`;
- always force read-only transaction behavior;
- block unsafe SQL even when the selected connection is configured as `unrestricted`.

Keep `execute_sql` as the mode-aware SQL execution tool:

- restricted selected connection: use `SafeSqlDriver`;
- unrestricted selected connection: use `SqlDriver`;
- static annotation is `destructiveHint=True` if any configured connection is unrestricted, otherwise `readOnlyHint=True`.

### 6. Add a `connection` Parameter to Every Database Tool

Every tool that touches PostgreSQL should accept:

```python
connection: str = Field(
    description="Configured database connection name. Required when connection_selection is strict.",
    default="",
)
```

Using an empty-string default keeps the schema backward-compatible while allowing `get_sql_driver()` to enforce either strict selection or default fallback. A default literal name such as `"default"` is incorrect because the configured default may be named differently.

Tools to update:

- `list_schemas`
- `list_objects`
- `get_object_details`
- `explain_query`
- `execute_sql`
- `execute_sql_ro`
- `analyze_workload_indexes`
- `analyze_query_indexes`
- `analyze_db_health`
- `get_top_queries`

Each should call:

```python
sql_driver = await get_sql_driver(connection)
```

Any helper class that receives `sql_driver` can remain unchanged.

### 7. Fix Connection-Specific Caches

`extension_utils.get_postgres_version()` currently uses one global `_POSTGRES_VERSION`. That is incorrect when multiple configured databases may run different PostgreSQL major versions.

Replace it with a cache keyed by connection identity.

Options:

- add a stable `name` or `cache_key` attribute to `SqlDriver`;
- expose the selected connection name from `get_sql_driver()`;
- pass the connection name explicitly to version utilities.

The cleanest option is to give `SqlDriver` an optional `connection_name` field when created by `get_sql_driver()`. Then `extension_utils` can cache by that name:

```python
_POSTGRES_VERSION_BY_CONNECTION: dict[str, int] = {}
```

Tests must cover two connections with different mocked versions to ensure the cache does not leak.

### 8. Update Shutdown

Shutdown must close every pool in the registry:

```python
for name, pool in db_connections.items():
    await pool.close()
```

Log per-connection close failures and continue closing the rest.

### 9. Update Deployment Configuration

Update every place that currently assumes one `DATABASE_URI`:

- `README.md`
- `docker-compose.yml`
- `Dockerfile` examples if needed
- `docker-entrypoint.sh` localhost remapping
- `smithery.yaml`
- Helm chart values, secret template, deployment template, and chart README

For Docker and Helm, support both:

- legacy `DATABASE_URI`;
- new `DATABASE_CONNECTIONS`.

The Docker entrypoint currently rewrites `localhost` inside `DATABASE_URI`. It should also rewrite every `connections.*.uri` inside `DATABASE_CONNECTIONS` while preserving valid JSON. Use JSON parsing for this, not string replacement across the whole blob.

## Test Plan

### Unit Tests

Add tests for configuration parsing:

- valid `DATABASE_CONNECTIONS`;
- omitted `connection_selection` defaults to `strict` with more than one configured connection;
- omitted `connection_selection` defaults to `default` with exactly one configured connection;
- missing `connections`;
- empty `connections`;
- non-object connection entry;
- non-string URI;
- invalid or missing mode;
- missing `default_connection`;
- default name not found;
- invalid `connection_selection`;
- invalid JSON;
- legacy `DATABASE_URI` conversion;
- `DATABASE_CONNECTIONS` takes precedence over `DATABASE_URI`;
- password obfuscation in config errors.

Add tests for connection registry and driver lookup:

- omitted connection errors in strict selection mode;
- omitted connection uses configured default in default selection mode;
- explicit connection selects the right pool;
- unknown connection returns a clear error;
- restricted connection returns `SafeSqlDriver`;
- unrestricted connection returns `SqlDriver`;
- `execute_sql_ro` returns `SafeSqlDriver` behavior even for an unrestricted connection;
- `list_connections` does not expose URIs;
- `shutdown` closes all pools.

Add tests for SQL execution tool annotations:

- `execute_sql_ro` is always registered with `readOnlyHint=True`;
- `execute_sql` is registered with `destructiveHint=True` when at least one configured connection is unrestricted;
- `execute_sql` is registered with `readOnlyHint=True` when all configured connections are restricted;
- legacy restricted mode preserves the current read-only `execute_sql` annotation;
- legacy unrestricted mode preserves the current destructive `execute_sql` annotation.

Add tests for connection-specific PostgreSQL version caching:

- connection `pg12` caches version 12;
- connection `pg16` caches version 16;
- repeated calls do not cross-contaminate.

### Docker Compose Integration Tests

Create a dedicated compose setup for multi-connection testing, for example `tests/docker-compose.multi-connection.yml`, with:

- `postgres-main`
- `postgres-analytics`
- optionally `postgres-broken` omitted or intentionally unreachable for status testing
- one `postgres-mcp` service configured with `DATABASE_CONNECTIONS`

Example environment:

```yaml
DATABASE_CONNECTIONS: >-
  {
    "connections": {
      "main": {
        "uri": "postgresql://postgres:postgres@postgres-main:5432/main_db",
        "mode": "restricted"
      },
      "analytics": {
        "uri": "postgresql://postgres:postgres@postgres-analytics:5432/analytics_db",
        "mode": "unrestricted"
      }
    },
    "default_connection": "main",
    "connection_selection": "strict"
  }
```

Each database should contain distinguishable data, for example:

- `main_db` has table `public.connection_marker` with value `main`;
- `analytics_db` has table `public.connection_marker` with value `analytics`.

Integration assertions:

- `list_connections` returns `main` and `analytics`;
- in strict selection mode, a database-backed tool call without `connection` returns a clear error;
- passing `connection="main"` explicitly uses the configured default connection;
- `execute_sql` with `connection="main"` reads marker `main`;
- `execute_sql` with `connection="analytics"` reads marker `analytics`;
- `execute_sql_ro` with `connection="analytics"` reads marker `analytics`;
- `execute_sql_ro` blocks unsafe SQL even when `connection="analytics"` is configured as `unrestricted`;
- schema/object listing returns objects from the selected database only;
- unknown connection returns a clear tool error;
- restricted connections block unsafe SQL;
- unrestricted connections allow SQL according to existing unrestricted semantics;
- in a mixed-mode config, `execute_sql` is exposed as destructive-capable while `execute_sql_ro` remains read-only;
- lenient startup reports temporarily unavailable connections without preventing the MCP server from starting.

The test runner can start the compose stack, wait for health checks, run MCP tool calls against SSE or streamable HTTP, and tear the stack down with volumes removed.

Suggested command:

```bash
docker compose -f tests/docker-compose.multi-connection.yml up --build --abort-on-container-exit
```

For local Python integration tests, add a pytest marker such as `docker_compose` so these tests can be skipped in normal unit-test runs and enabled in CI jobs that support Docker.

## Backward Compatibility

Existing users should not need to change anything if they configure only `DATABASE_URI`.

Behavior in legacy mode:

- the only connection name is `default`;
- `list_connections` returns one entry;
- all tools work without a `connection` argument;
- passing `connection="default"` also works.

Potential breaking changes to avoid:

- do not require `DATABASE_CONNECTIONS` when `DATABASE_URI` exists;
- do not expose connection URIs in tool responses;
- do not force clients to pass `connection` explicitly in legacy mode;
- do not change legacy `--access-mode` semantics.

## Open Decisions

No open design decisions are known at this point. The main remaining work is implementation and test coverage.
