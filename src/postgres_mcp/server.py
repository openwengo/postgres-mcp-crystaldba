# ruff: noqa: B008
import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any
from typing import List
from typing import Literal
from typing import Union

import mcp.types as types
from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic import validate_call

from postgres_mcp.audit import get_caller_identity
from postgres_mcp.audit import log_safe
from postgres_mcp.config import AccessMode
from postgres_mcp.config import ConnectionSelection
from postgres_mcp.config import DatabaseConnectionConfig
from postgres_mcp.config import DatabasesConfig
from postgres_mcp.config import load_database_config
from postgres_mcp.index.dta_calc import DatabaseTuningAdvisor
from postgres_mcp.oauth import build_auth_provider

from .artifacts import ErrorResult
from .artifacts import ExplainPlanArtifact
from .database_health import DatabaseHealthTool
from .database_health import HealthType
from .explain import ExplainPlanTool
from .index.index_opt_base import MAX_NUM_INDEX_TUNING_QUERIES
from .index.llm_opt import LLMOptimizerTool
from .index.presentation import TextPresentation
from .sql import DbConnPool
from .sql import SafeSqlDriver
from .sql import SqlDriver
from .sql import check_hypopg_installation_status
from .sql import obfuscate_password
from .top_queries import TopQueriesCalc

# Initialize FastMCP with default settings
mcp = FastMCP("postgres-mcp")

# Constants
PG_STAT_STATEMENTS = "pg_stat_statements"
HYPOPG_EXTENSION = "hypopg"

ResponseType = List[types.TextContent | types.ImageContent | types.EmbeddedResource]

logger = logging.getLogger(__name__)


# Global variables
db_connection = DbConnPool()
db_connections: dict[str, DbConnPool] = {}
connection_configs: dict[str, DatabaseConnectionConfig] = {}
default_connection_name = "default"
connection_selection: ConnectionSelection = "default"
current_access_mode = AccessMode.UNRESTRICTED
shutdown_in_progress = False


def _audit_fields(**fields: Any) -> str:
    parts = [get_caller_identity().as_log_fields()]
    for key, value in fields.items():
        cleaned = log_safe(value)
        if cleaned:
            parts.append(f"{key}={cleaned}")
    return " ".join(parts)


def log_tool_call(tool_name: str, **fields: Any) -> None:
    logger.info("Tool %s %s", tool_name, _audit_fields(**fields))


def configure_http_auth(*, host: str, port: int) -> None:
    """Attach FastMCP HTTP auth for audit identity when enabled."""
    auth_provider = build_auth_provider(host=host, port=port)
    mcp.auth = auth_provider
    if auth_provider is not None:
        logger.info(
            "OAuth 2.1/JWT authentication enabled for audit identity only; database connection access remains controlled by static configuration."
        )


def _available_connection_names() -> str:
    names = sorted(db_connections) if db_connections else ["default"]
    return ", ".join(names)


def _resolve_connection_name(connection: str | None = None) -> str:
    """Resolve a requested connection name according to current selection policy."""
    requested_connection = connection.strip() if isinstance(connection, str) else connection
    if not requested_connection:
        if connection_selection == "strict":
            raise ValueError(
                f"A connection parameter is required for this tool call. Available connections: {_available_connection_names()}",
            )
        requested_connection = default_connection_name

    if db_connections and requested_connection not in db_connections:
        raise ValueError(f"Unknown connection '{requested_connection}'. Available connections: {_available_connection_names()}")

    if not db_connections and requested_connection != "default":
        raise ValueError(f"Unknown connection '{requested_connection}'. Available connections: default")

    return requested_connection


async def get_sql_driver(connection: str | None = None, force_restricted: bool = False) -> Union[SqlDriver, SafeSqlDriver]:
    """Get the appropriate SQL driver for a configured connection."""
    if db_connections:
        connection_name = _resolve_connection_name(connection)
        selected_pool = db_connections[connection_name]
        selected_mode = connection_configs[connection_name].mode
    else:
        # Pre-startup/test fallback preserves the previous single-connection behavior.
        connection_name = _resolve_connection_name(connection)
        selected_pool = db_connection
        selected_mode = current_access_mode

    base_driver = SqlDriver(conn=selected_pool, connection_name=connection_name)

    if force_restricted or selected_mode == AccessMode.RESTRICTED:
        logger.debug("Using SafeSqlDriver with restrictions (RESTRICTED mode)")
        return SafeSqlDriver(sql_driver=base_driver, timeout=30)  # 30 second timeout

    logger.debug("Using unrestricted SqlDriver (UNRESTRICTED mode)")
    return base_driver


def format_text_response(text: Any) -> ResponseType:
    """Format a text response."""
    return [types.TextContent(type="text", text=str(text))]


def format_error_response(error: str) -> ResponseType:
    """Format an error response."""
    return format_text_response(f"Error: {error}")


def configure_database_connections(config: DatabasesConfig) -> None:
    """Apply database connection configuration to the server registry."""
    global connection_configs
    global connection_selection
    global current_access_mode
    global db_connections
    global default_connection_name

    connection_configs = config.connections
    default_connection_name = config.default_connection
    connection_selection = config.connection_selection

    db_connections = {}
    for name, connection_config in config.connections.items():
        if config.is_legacy and name == "default":
            db_connection.connection_url = connection_config.uri
            db_connections[name] = db_connection
        else:
            db_connections[name] = DbConnPool(connection_config.uri)

    if config.is_legacy:
        current_access_mode = config.connections[config.default_connection].mode


async def initialize_database_connections(config: DatabasesConfig) -> None:
    """Initialize configured database pools without failing startup for unavailable databases."""
    configure_database_connections(config)

    for name, pool in db_connections.items():
        connection_config = connection_configs[name]
        try:
            await pool.pool_connect(connection_config.uri)
            logger.info(f"Successfully connected to database '{name}' and initialized connection pool")
        except Exception as e:
            logger.warning(f"Could not connect to database '{name}': {obfuscate_password(str(e))}")
            logger.warning(
                "The MCP server will start but operations against this connection will fail until it becomes available.",
            )


def _has_unrestricted_connection() -> bool:
    if connection_configs:
        return any(config.mode == AccessMode.UNRESTRICTED for config in connection_configs.values())
    return current_access_mode == AccessMode.UNRESTRICTED


def register_execute_sql_tool() -> None:
    """Register or update execute_sql with annotations matching configured connection modes."""
    if _has_unrestricted_connection():
        description = "Execute any SQL query"
        annotations = ToolAnnotations(
            title="Execute SQL",
            destructiveHint=True,
        )
    else:
        description = "Execute a read-only SQL query"
        annotations = ToolAnnotations(
            title="Execute SQL (Read-Only)",
            readOnlyHint=True,
        )

    try:
        mcp.local_provider.remove_tool("execute_sql")
    except KeyError:
        pass

    mcp.local_provider.add_tool(
        Tool.from_function(
            execute_sql,
            name="execute_sql",
            description=description,
            annotations=annotations,
        )
    )


def get_registered_tool(name: str) -> Tool | None:
    """Return a registered local tool by name for tests and startup checks."""
    for component in mcp.local_provider._components.values():  # type: ignore[attr-defined]
        if isinstance(component, Tool) and component.name == name:
            return component
    return None


def _connection_status(name: str, pool: DbConnPool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "is_default": name == default_connection_name,
        "mode": connection_configs[name].mode.value,
        "status": "connected" if pool.is_valid else "error" if pool.last_error else "not_connected",
    }
    if pool.last_error:
        result["last_error"] = obfuscate_password(pool.last_error)
    return result


@mcp.tool(
    description="List configured database connections",
    annotations=ToolAnnotations(
        title="List Connections",
        readOnlyHint=True,
    ),
)
async def list_connections() -> ResponseType:
    """List configured database connections without exposing connection URIs."""
    try:
        log_tool_call("list_connections")
        if not db_connections:
            mode = current_access_mode.value
            status = "connected" if db_connection.is_valid else "error" if db_connection.last_error else "not_connected"
            result: dict[str, Any] = {
                "name": "default",
                "is_default": True,
                "mode": mode,
                "status": status,
            }
            if db_connection.last_error:
                result["last_error"] = obfuscate_password(db_connection.last_error)
            return format_text_response([result])

        names = sorted(db_connections)
        names.sort(key=lambda name: (name != default_connection_name, name))
        return format_text_response([_connection_status(name, db_connections[name]) for name in names])
    except Exception as e:
        logger.error(f"Error listing connections: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="List all schemas in the database",
    annotations=ToolAnnotations(
        title="List Schemas",
        readOnlyHint=True,
    ),
)
async def list_schemas(
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """List all schemas in the database."""
    try:
        log_tool_call("list_schemas", connection=connection)
        sql_driver = await get_sql_driver(connection)
        rows = await sql_driver.execute_query(
            """
            SELECT
                schema_name,
                schema_owner,
                CASE
                    WHEN schema_name LIKE 'pg_%' THEN 'System Schema'
                    WHEN schema_name = 'information_schema' THEN 'System Information Schema'
                    ELSE 'User Schema'
                END as schema_type
            FROM information_schema.schemata
            ORDER BY schema_type, schema_name
            """
        )
        schemas = [row.cells for row in rows] if rows else []
        return format_text_response(schemas)
    except Exception as e:
        logger.error(f"Error listing schemas: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="List objects in a schema",
    annotations=ToolAnnotations(
        title="List Objects",
        readOnlyHint=True,
    ),
)
async def list_objects(
    schema_name: str = Field(description="Schema name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """List objects of a given type in a schema."""
    try:
        log_tool_call("list_objects", connection=connection, schema=schema_name, object_type=object_type)
        sql_driver = await get_sql_driver(connection)

        if object_type in ("table", "view"):
            table_type = "BASE TABLE" if object_type == "table" else "VIEW"
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT table_schema, table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = {} AND table_type = {}
                ORDER BY table_name
                """,
                [schema_name, table_type],
            )
            objects = (
                [{"schema": row.cells["table_schema"], "name": row.cells["table_name"], "type": row.cells["table_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type
                FROM information_schema.sequences
                WHERE sequence_schema = {}
                ORDER BY sequence_name
                """,
                [schema_name],
            )
            objects = (
                [{"schema": row.cells["sequence_schema"], "name": row.cells["sequence_name"], "data_type": row.cells["data_type"]} for row in rows]
                if rows
                else []
            )

        elif object_type == "extension":
            # Extensions are not schema-specific
            rows = await sql_driver.execute_query(
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                ORDER BY extname
                """
            )
            objects = (
                [{"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]} for row in rows]
                if rows
                else []
            )

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(objects)
    except Exception as e:
        logger.error(f"Error listing objects: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Show detailed information about a database object",
    annotations=ToolAnnotations(
        title="Get Object Details",
        readOnlyHint=True,
    ),
)
async def get_object_details(
    schema_name: str = Field(description="Schema name"),
    object_name: str = Field(description="Object name"),
    object_type: str = Field(description="Object type: 'table', 'view', 'sequence', or 'extension'", default="table"),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """Get detailed information about a database object."""
    try:
        log_tool_call(
            "get_object_details",
            connection=connection,
            schema=schema_name,
            object=object_name,
            object_type=object_type,
        )
        sql_driver = await get_sql_driver(connection)

        if object_type in ("table", "view"):
            # Get columns
            col_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = {} AND table_name = {}
                ORDER BY ordinal_position
                """,
                [schema_name, object_name],
            )
            columns = (
                [
                    {
                        "column": r.cells["column_name"],
                        "data_type": r.cells["data_type"],
                        "is_nullable": r.cells["is_nullable"],
                        "default": r.cells["column_default"],
                    }
                    for r in col_rows
                ]
                if col_rows
                else []
            )

            # Get constraints
            con_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT tc.constraint_name, tc.constraint_type, kcu.column_name
                FROM information_schema.table_constraints AS tc
                LEFT JOIN information_schema.key_column_usage AS kcu
                  ON tc.constraint_name = kcu.constraint_name
                 AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = {} AND tc.table_name = {}
                """,
                [schema_name, object_name],
            )

            constraints = {}
            if con_rows:
                for row in con_rows:
                    cname = row.cells["constraint_name"]
                    ctype = row.cells["constraint_type"]
                    col = row.cells["column_name"]

                    if cname not in constraints:
                        constraints[cname] = {"type": ctype, "columns": []}
                    if col:
                        constraints[cname]["columns"].append(col)

            constraints_list = [{"name": name, **data} for name, data in constraints.items()]

            # Get indexes
            idx_rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = {} AND tablename = {}
                """,
                [schema_name, object_name],
            )

            indexes = [{"name": r.cells["indexname"], "definition": r.cells["indexdef"]} for r in idx_rows] if idx_rows else []

            result = {
                "basic": {"schema": schema_name, "name": object_name, "type": object_type},
                "columns": columns,
                "constraints": constraints_list,
                "indexes": indexes,
            }

        elif object_type == "sequence":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT sequence_schema, sequence_name, data_type, start_value, increment
                FROM information_schema.sequences
                WHERE sequence_schema = {} AND sequence_name = {}
                """,
                [schema_name, object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {
                    "schema": row.cells["sequence_schema"],
                    "name": row.cells["sequence_name"],
                    "data_type": row.cells["data_type"],
                    "start_value": row.cells["start_value"],
                    "increment": row.cells["increment"],
                }
            else:
                result = {}

        elif object_type == "extension":
            rows = await SafeSqlDriver.execute_param_query(
                sql_driver,
                """
                SELECT extname, extversion, extrelocatable
                FROM pg_extension
                WHERE extname = {}
                """,
                [object_name],
            )

            if rows and rows[0]:
                row = rows[0]
                result = {"name": row.cells["extname"], "version": row.cells["extversion"], "relocatable": row.cells["extrelocatable"]}
            else:
                result = {}

        else:
            return format_error_response(f"Unsupported object type: {object_type}")

        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting object details: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Explains the execution plan for a SQL query, showing how the database will execute it and provides detailed cost estimates.",
    annotations=ToolAnnotations(
        title="Explain Query",
        readOnlyHint=True,
    ),
)
async def explain_query(
    sql: str = Field(description="SQL query to explain"),
    analyze: bool = Field(
        description="When True, actually runs the query to show real execution statistics instead of estimates. "
        "Takes longer but provides more accurate information.",
        default=False,
    ),
    hypothetical_indexes: list[dict[str, Any]] = Field(
        description="""A list of hypothetical indexes to simulate. Each index must be a dictionary with these keys:
    - 'table': The table name to add the index to (e.g., 'users')
    - 'columns': List of column names to include in the index (e.g., ['email'] or ['last_name', 'first_name'])
    - 'using': Optional index method (default: 'btree', other options include 'hash', 'gist', etc.)

Examples: [
    {"table": "users", "columns": ["email"], "using": "btree"},
    {"table": "orders", "columns": ["user_id", "created_at"]}
]
If there is no hypothetical index, you can pass an empty list.""",
        default=[],
    ),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """
    Explains the execution plan for a SQL query.

    Args:
        sql: The SQL query to explain
        analyze: When True, actually runs the query for real statistics
        hypothetical_indexes: Optional list of indexes to simulate
    """
    try:
        log_tool_call(
            "explain_query",
            connection=connection,
            analyze=analyze,
            hypothetical_indexes=len(hypothetical_indexes or []),
            sql_chars=len(sql),
        )
        sql_driver = await get_sql_driver(connection)
        explain_tool = ExplainPlanTool(sql_driver=sql_driver)
        result: ExplainPlanArtifact | ErrorResult | None = None

        # If hypothetical indexes are specified, check for HypoPG extension
        if hypothetical_indexes and len(hypothetical_indexes) > 0:
            if analyze:
                return format_error_response("Cannot use analyze and hypothetical indexes together")
            try:
                # Use the common utility function to check if hypopg is installed
                (
                    is_hypopg_installed,
                    hypopg_message,
                ) = await check_hypopg_installation_status(sql_driver)

                # If hypopg is not installed, return the message
                if not is_hypopg_installed:
                    return format_text_response(hypopg_message)

                # HypoPG is installed, proceed with explaining with hypothetical indexes
                result = await explain_tool.explain_with_hypothetical_indexes(sql, hypothetical_indexes)
            except Exception:
                raise  # Re-raise the original exception
        elif analyze:
            try:
                # Use EXPLAIN ANALYZE
                result = await explain_tool.explain_analyze(sql)
            except Exception:
                raise  # Re-raise the original exception
        else:
            try:
                # Use basic EXPLAIN
                result = await explain_tool.explain(sql)
            except Exception:
                raise  # Re-raise the original exception

        if result and isinstance(result, ExplainPlanArtifact):
            return format_text_response(result.to_text())
        else:
            error_message = "Error processing explain plan"
            if isinstance(result, ErrorResult):
                error_message = result.to_text()
            return format_error_response(error_message)
    except Exception as e:
        logger.error(f"Error explaining query: {e}")
        return format_error_response(str(e))


# Query function declaration without the decorator - we'll add it dynamically based on access mode
async def execute_sql(
    sql: str = Field(description="SQL to run", default="all"),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """Executes a SQL query against the database."""
    try:
        log_tool_call("execute_sql", connection=connection, sql_chars=len(sql))
        sql_driver = await get_sql_driver(connection)
        rows = await sql_driver.execute_query(sql)  # type: ignore
        if rows is None:
            return format_text_response("No results")
        return format_text_response(list([r.cells for r in rows]))
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return format_error_response(str(e))


@mcp.tool(
    name="execute_sql_ro",
    description="Execute a read-only SQL query",
    annotations=ToolAnnotations(
        title="Execute SQL (Read-Only)",
        readOnlyHint=True,
    ),
)
async def execute_sql_ro(
    sql: str = Field(description="SQL to run", default="all"),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """Executes a SQL query in read-only restricted mode against the selected database."""
    try:
        log_tool_call("execute_sql_ro", connection=connection, sql_chars=len(sql))
        sql_driver = await get_sql_driver(connection, force_restricted=True)
        rows = await sql_driver.execute_query(sql)  # type: ignore
        if rows is None:
            return format_text_response("No results")
        return format_text_response(list([r.cells for r in rows]))
    except Exception as e:
        logger.error(f"Error executing read-only query: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze frequently executed queries in the database and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Workload Indexes",
        readOnlyHint=True,
    ),
)
@validate_call
async def analyze_workload_indexes(
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """Analyze frequently executed queries in the database and recommend optimal indexes."""
    try:
        log_tool_call(
            "analyze_workload_indexes",
            connection=connection,
            method=method,
            max_index_size_mb=max_index_size_mb,
        )
        sql_driver = await get_sql_driver(connection)
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_workload(max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing workload: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyze a list of (up to 10) SQL queries and recommend optimal indexes",
    annotations=ToolAnnotations(
        title="Analyze Query Indexes",
        readOnlyHint=True,
    ),
)
@validate_call
async def analyze_query_indexes(
    queries: list[str] = Field(description="List of Query strings to analyze"),
    max_index_size_mb: int = Field(description="Max index size in MB", default=10000),
    method: Literal["dta", "llm"] = Field(description="Method to use for analysis", default="dta"),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """Analyze a list of SQL queries and recommend optimal indexes."""
    log_tool_call(
        "analyze_query_indexes",
        connection=connection,
        method=method,
        query_count=len(queries),
        max_index_size_mb=max_index_size_mb,
    )
    if len(queries) == 0:
        return format_error_response("Please provide a non-empty list of queries to analyze.")
    if len(queries) > MAX_NUM_INDEX_TUNING_QUERIES:
        return format_error_response(f"Please provide a list of up to {MAX_NUM_INDEX_TUNING_QUERIES} queries to analyze.")

    try:
        sql_driver = await get_sql_driver(connection)
        if method == "dta":
            index_tuning = DatabaseTuningAdvisor(sql_driver)
        else:
            index_tuning = LLMOptimizerTool(sql_driver)
        dta_tool = TextPresentation(sql_driver, index_tuning)
        result = await dta_tool.analyze_queries(queries=queries, max_index_size_mb=max_index_size_mb)
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error analyzing queries: {e}")
        return format_error_response(str(e))


@mcp.tool(
    description="Analyzes database health. Here are the available health checks:\n"
    "- index - checks for invalid, duplicate, and bloated indexes\n"
    "- connection - checks the number of connection and their utilization\n"
    "- vacuum - checks vacuum health for transaction id wraparound\n"
    "- sequence - checks sequences at risk of exceeding their maximum value\n"
    "- replication - checks replication health including lag and slots\n"
    "- buffer - checks for buffer cache hit rates for indexes and tables\n"
    "- constraint - checks for invalid constraints\n"
    "- all - runs all checks\n"
    "You can optionally specify a single health check or a comma-separated list of health checks. The default is 'all' checks.",
    annotations=ToolAnnotations(
        title="Analyze Database Health",
        readOnlyHint=True,
    ),
)
async def analyze_db_health(
    health_type: str = Field(
        description=f"Optional. Valid values are: {', '.join(sorted([t.value for t in HealthType]))}.",
        default="all",
    ),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    """Analyze database health for specified components.

    Args:
        health_type: Comma-separated list of health check types to perform.
                    Valid values: index, connection, vacuum, sequence, replication, buffer, constraint, all
    """
    log_tool_call("analyze_db_health", connection=connection, health_type=health_type)
    health_tool = DatabaseHealthTool(await get_sql_driver(connection))
    result = await health_tool.health(health_type=health_type)
    return format_text_response(result)


@mcp.tool(
    name="get_top_queries",
    description=f"Reports the slowest or most resource-intensive queries using data from the '{PG_STAT_STATEMENTS}' extension.",
    annotations=ToolAnnotations(
        title="Get Top Queries",
        readOnlyHint=True,
    ),
)
async def get_top_queries(
    sort_by: str = Field(
        description="Ranking criteria: 'total_time' for total execution time or 'mean_time' for mean execution time per call, or 'resources' "
        "for resource-intensive queries",
        default="resources",
    ),
    limit: int = Field(description="Number of queries to return when ranking based on mean_time or total_time", default=10),
    connection: str = Field(description="Configured database connection name. Required when connection_selection is strict.", default=""),
) -> ResponseType:
    try:
        log_tool_call("get_top_queries", connection=connection, sort_by=sort_by, limit=limit)
        sql_driver = await get_sql_driver(connection)
        top_queries_tool = TopQueriesCalc(sql_driver=sql_driver)

        if sort_by == "resources":
            result = await top_queries_tool.get_top_resource_queries()
            return format_text_response(result)
        elif sort_by == "mean_time" or sort_by == "total_time":
            # Map the sort_by values to what get_top_queries_by_time expects
            result = await top_queries_tool.get_top_queries_by_time(limit=limit, sort_by="mean" if sort_by == "mean_time" else "total")
        else:
            return format_error_response("Invalid sort criteria. Please use 'resources' or 'mean_time' or 'total_time'.")
        return format_text_response(result)
    except Exception as e:
        logger.error(f"Error getting slow queries: {e}")
        return format_error_response(str(e))


async def main():
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="PostgreSQL MCP Server")
    parser.add_argument("database_url", help="Database connection URL", nargs="?")
    parser.add_argument(
        "--access-mode",
        type=str,
        choices=[mode.value for mode in AccessMode],
        default=AccessMode.UNRESTRICTED.value,
        help="Set SQL access mode: unrestricted (unrestricted) or restricted (read-only with protections)",
    )
    parser.add_argument(
        "--transport",
        type=str,
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Select MCP transport: stdio (default), sse, or streamable-http",
    )
    parser.add_argument(
        "--sse-host",
        type=str,
        default="localhost",
        help="Host to bind SSE server to (default: localhost)",
    )
    parser.add_argument(
        "--sse-port",
        type=int,
        default=8000,
        help="Port for SSE server (default: 8000)",
    )
    parser.add_argument(
        "--streamable-http-host",
        type=str,
        default="localhost",
        help="Host to bind streamable HTTP server to (default: localhost)",
    )
    parser.add_argument(
        "--streamable-http-port",
        type=int,
        default=8000,
        help="Port for streamable HTTP server (default: 8000)",
    )

    args = parser.parse_args()

    # Store the access mode in the global variable
    global current_access_mode
    current_access_mode = AccessMode(args.access_mode)

    database_config = load_database_config(
        database_connections=os.environ.get("DATABASE_CONNECTIONS"),
        database_uri=os.environ.get("DATABASE_URI"),
        positional_database_url=args.database_url,
        legacy_access_mode=current_access_mode,
    )

    await initialize_database_connections(database_config)
    register_execute_sql_tool()

    mode_summary = ", ".join(f"{name}={config.mode.value}" for name, config in sorted(connection_configs.items()))
    logger.info(
        "Starting PostgreSQL MCP Server with %s connection(s), default='%s', selection='%s', modes: %s",
        len(connection_configs),
        default_connection_name,
        connection_selection,
        mode_summary,
    )

    # Set up proper shutdown handling
    try:
        loop = asyncio.get_running_loop()
        signals = (signal.SIGTERM, signal.SIGINT)
        for s in signals:
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown(s)))
    except NotImplementedError:
        # Windows doesn't support signals properly
        logger.warning("Signal handling not supported on Windows")
        pass

    # Run the server with the selected transport (always async)
    if args.transport == "stdio":
        mcp.auth = None
        if os.getenv("MCP_ENABLE_OAUTH21", "").strip().lower() in {"1", "true", "yes", "y", "on"}:
            logger.warning("OAuth 2.1/JWT authentication is only applied to HTTP transports; stdio will audit as anonymous.")
        await mcp.run_async(transport="stdio")
    elif args.transport == "sse":
        configure_http_auth(host=args.sse_host, port=args.sse_port)
        await mcp.run_async(transport="sse", host=args.sse_host, port=args.sse_port)
    elif args.transport == "streamable-http":
        configure_http_auth(host=args.streamable_http_host, port=args.streamable_http_port)
        await mcp.run_async(
            transport="http",
            host=args.streamable_http_host,
            port=args.streamable_http_port,
            stateless_http=True,
        )


async def shutdown(sig=None):
    """Clean shutdown of the server."""
    global shutdown_in_progress

    if shutdown_in_progress:
        logger.warning("Forcing immediate exit")
        # Use sys.exit instead of os._exit to allow for proper cleanup
        sys.exit(1)

    shutdown_in_progress = True

    if sig:
        logger.info(f"Received exit signal {sig.name}")

    # Close database connections
    pools = db_connections or {"default": db_connection}
    for name, pool in pools.items():
        try:
            await pool.close()
            logger.info(f"Closed database connection '{name}'")
        except Exception as e:
            logger.error(f"Error closing database connection '{name}': {e}")

    # Exit with appropriate status code
    sys.exit(128 + sig if sig is not None else 0)
