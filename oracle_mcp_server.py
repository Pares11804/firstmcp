"""
Oracle read-only tools exposed through the Model Context Protocol (MCP).

This process is started by an MCP client (e.g. Cursor). It does not read chat text
directly: the client sends JSON-RPC messages, and the assistant invokes *tools* such
as ``query`` with SQL you intend to run. Results are returned as text (JSON) to the
model, which then answers you in natural language.

Configuration is via environment variables. See the project README for setup and for
``ORACLE_DSN`` formats (Easy Connect / TNS with Instant Client).
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

import oracledb
from fastmcp import FastMCP

# --- MCP app -----------------------------------------------------------------

# fastmcp.FastMCP: registers tools, handles stdio transport, protocol lifecycle.
mcp = FastMCP("Oracle (read tools)")

# Cap returned rows so large result sets do not overwhelm the model or the UI.
_MAX_ROWS = 200

# Shared pool: created lazily on first use; one pool per process is typical for stdio.
_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()

# Must start with WITH or SELECT; allows leading parentheses.
_SELECT_START = re.compile(
    r"^\s*(\(\s*)*(with|select)\b", re.IGNORECASE | re.DOTALL
)


def _is_read_only_select(sql: str) -> bool:
    """
    Heuristic check that the statement is a single read-only query.

    This is *not* a SQL parser. It blocks obvious DML/DDL substrings and multiple
    statements. Defense in depth: also use a least-privileged DB user.

    Args:
        sql: Raw SQL from the model or tool caller.

    Returns:
        True if the string passes the heuristic; False otherwise.
    """
    s = sql.strip()
    # Reject `SELECT ...; DELETE ...` style batches (semicolon before trailing space).
    if ";" in s.rstrip().rstrip(";"):
        return False
    if not _SELECT_START.match(s):
        return False
    # Pad so keywords only match as tokens (e.g. not inside identifiers).
    upper = s.upper()
    for bad in (
        " INSERT ",
        " UPDATE ",
        " DELETE ",
        " MERGE ",
        " DROP ",
        " ALTER ",
        " TRUNCATE ",
        " CREATE ",
        " GRANT ",
        " REVOKE ",
    ):
        if bad in f" {upper} ":
            return False
    return True


def get_pool() -> oracledb.ConnectionPool:
    """
    Return a singleton :class:`oracledb.ConnectionPool`, creating it on first use.

    Reads ``ORACLE_USER``, ``ORACLE_PASSWORD``, ``ORACLE_DSN``, and optional
    ``ORACLE_USE_THICK``, ``ORACLE_CLIENT_LIB_DIR``, ``ORACLE_POOL_MAX`` from
    the environment. Thread-safe: only one pool is created per process.

    Returns:
        The connection pool for subsequent ``acquire()`` calls.

    Raises:
        RuntimeError: If required variables are missing.
    """
    global _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        user = os.environ.get("ORACLE_USER", "").strip()
        password = os.environ.get("ORACLE_PASSWORD", "")
        dsn = os.environ.get("ORACLE_DSN", "").strip()
        if not user or dsn is None or dsn == "":
            raise RuntimeError(
                "Set ORACLE_USER, ORACLE_PASSWORD, and ORACLE_DSN in the environment."
            )
        # Thick client: TNS names, LDAP, and full Oracle Net (including tnsnames.ora
        # when TNS_ADMIN is set). Installs Instant Client and init_oracle_client.
        thick = os.environ.get("ORACLE_USE_THICK", "").lower() in (
            "1",
            "true",
            "yes",
        )
        if thick:
            lib = os.environ.get("ORACLE_CLIENT_LIB_DIR")
            oracledb.init_oracle_client(lib_dir=lib if lib else None)
        _pool = oracledb.create_pool(
            user=user,
            password=password,
            dsn=dsn,
            min=1,
            max=int(os.environ.get("ORACLE_POOL_MAX", "4")),
            increment=1,
        )
        return _pool


def _rows_to_json(cursor: oracledb.Cursor, rows: list) -> str:
    """
    Format fetched rows as a JSON string the assistant can read.

    Column names come from the cursor description. datetime-like values use
    ``isoformat()``; other types are left as returned by the driver and then
    serialized by :func:`json.dumps` where possible.

    Args:
        cursor: Cursor after ``execute`` (used for ``description`` and row layout).
        rows: Tuples of column values, same length as ``description``.

    Returns:
        Pretty-printed JSON: ``columns``, ``rows`` (list of objects), ``row_count``.
    """
    names = [d[0] for d in (cursor.description or [])]
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for i, col in enumerate(names):
            v = row[i]
            if hasattr(v, "isoformat"):
                v = v.isoformat() if v is not None else None
            item[col] = v
        out.append(item)
    return json.dumps(
        {"columns": names, "rows": out, "row_count": len(out)}, indent=2
    )


def _run_read_query(sql: str, params: dict[str, Any] | None = None) -> str:
    """
    Execute a validated read query and return JSON text or a short error string.

    Fetches at most ``_MAX_ROWS`` rows. Uses ``fetchmany(_MAX_ROWS + 1)`` to
    detect whether the result was truncated at the cap.

    Args:
        sql: A single ``SELECT`` or ``WITH`` statement.
        params: Optional bind map for named binds ``:name`` in the SQL.

    Returns:
        JSON result string, or a human-readable error if validation fails.
    """
    if not _is_read_only_select(sql):
        return (
            "Error: only a single SELECT or WITH...SELECT is allowed. "
            "No DML, DDL, or multiple statements."
        )
    bind = params or {}
    pool = get_pool()
    with pool.acquire() as conn:  # type: ignore[union-attr]
        with conn.cursor() as cur:
            # Named binds: pass a dict. Empty dict uses execute(sql) (no bind struct).
            cur.execute(sql, bind) if bind else cur.execute(sql)
            # One extra row tells us if we need to add a truncation notice.
            rows = cur.fetchmany(_MAX_ROWS + 1)
            if len(rows) > _MAX_ROWS:
                rows = rows[:_MAX_ROWS]
                body = _rows_to_json(cur, rows)
                return body + f"\n\n(Truncated to {_MAX_ROWS} rows.)\n"
            return _rows_to_json(cur, rows)


@mcp.tool()
def query(
    sql: str,
    params: dict[str, Any] | None = None,
) -> str:
    """
    Run a read-only SQL query against the configured Oracle database.

    Only a single ``SELECT`` or ``WITH`` (common table expression) query is
    allowed. Results are limited to 200 rows. Use named binds in the SQL, e.g.
    ``... WHERE id = :id`` and set ``params`` to a map whose keys match, such as
    ``{"id": 123}`` (as structured input from the tool caller).

    Args:
        sql: The SQL string (read-only).
        params: Optional mapping of bind names to values.

    Returns:
        JSON with ``columns``, ``rows``, and ``row_count``, or an error message.
    """
    return _run_read_query(sql, params)


@mcp.tool()
def list_tables(owner: str, name_like: str | None = None) -> str:
    """
    List tables visible in ``ALL_TABLES`` for a given schema (owner).

    Args:
        owner: Oracle schema name (e.g. ``SCOTT``). Compared case-insensitively
            in the query via ``UPPER`` on the bind.
        name_like: Optional ``LIKE`` pattern for ``TABLE_NAME``; default is ``%``.

    Returns:
        JSON table listing (and ``row_count``) or an error string.
    """
    like = name_like if name_like is not None else "%"
    sql = """
        SELECT table_name, num_rows
        FROM all_tables
        WHERE owner = :owner
          AND UPPER(table_name) LIKE UPPER(:like_pattern)
        ORDER BY table_name
    """
    return _run_read_query(
        sql, {"owner": owner.upper(), "like_pattern": like}
    )


@mcp.tool()
def describe_table(owner: str, table: str) -> str:
    """
    Describe columns for a table using ``ALL_TAB_COLUMNS``.

    Args:
        owner: Schema name owning the table.
        table: Table name (not case-sensitive; uppercased for the data dictionary).

    Returns:
        JSON with column metadata or an error string.
    """
    sql = """
        SELECT column_id, column_name, data_type, data_length, nullable, data_default
        FROM all_tab_columns
        WHERE owner = :owner
          AND table_name = :tname
        ORDER BY column_id
    """
    return _run_read_query(
        sql, {"owner": owner.upper(), "tname": table.upper()}
    )


if __name__ == "__main__":
    mcp.run()
