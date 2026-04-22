"""
Oracle tools exposed through the Model Context Protocol (MCP): direct SQL execution.

The MCP client (e.g. Cursor) runs this process and may invoke the ``query`` tool with
arbitrary SQL (subject to what ``ORACLE_USER`` is allowed to do). This is a single
unrestricted pass-through: no hard-coded list/describe helpers, no read-only filter,
and no row cap in code—large ``SELECT`` results load fully (watch memory and UI).

Configuration: optional project ``.env`` or process environment. See the README.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import oracledb
from dotenv import load_dotenv
from fastmcp import FastMCP

# Load .env from the same directory as this module.
load_dotenv(Path(__file__).resolve().parent / ".env")

# --- MCP app -----------------------------------------------------------------
mcp = FastMCP("Oracle (direct SQL)")

# Shared connection pool, lazy init.
_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> oracledb.ConnectionPool:
    """
    Return a singleton :class:`oracledb.ConnectionPool`, creating it on first use.
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


def _cell_for_json(v: Any) -> Any:
    """Best-effort value for JSON (LOB/Decimal/bytes, etc. become strings if needed)."""
    if v is None:
        return None
    if isinstance(v, (bool, int, float, str)):
        return v
    if hasattr(v, "isoformat") and callable(v.isoformat):
        try:
            return v.isoformat()
        except (TypeError, ValueError, OSError):
            pass
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        if len(b) > 1_000_000:
            return f"<{len(b)} bytes>"
        return b.decode("utf-8", errors="replace")
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError, OverflowError):
        return str(v)


def _rows_to_json(cursor: oracledb.Cursor, rows: list) -> str:
    names = [d[0] for d in (cursor.description or [])]
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for i, col in enumerate(names):
            item[col] = _cell_for_json(row[i])
        out.append(item)
    return json.dumps(
        {"columns": names, "rows": out, "row_count": len(out)}, indent=2
    )


def _run_sql(sql: str, params: dict[str, Any] | None = None) -> str:
    """
    Run one SQL statement. SELECT/WITH: return all rows as JSON. Other statements:
    commit and return rowcount.
    """
    sql = (sql or "").strip()
    if not sql:
        return json.dumps({"error": "empty sql"}, indent=2)

    bind = params or {}
    pool = get_pool()
    with pool.acquire() as conn:  # type: ignore[union-attr]
        with conn.cursor() as cur:
            if bind:
                cur.execute(sql, bind)
            else:
                cur.execute(sql)
            if cur.description is not None:
                # Query result: fetch the full set (no row cap in this server).
                rows = cur.fetchall()
                return _rows_to_json(cur, rows)
            # DML, DDL, PL/SQL, etc.
            n = cur.rowcount
            conn.commit()
            return json.dumps(
                {
                    "affected_rows": n,
                    "rowcount": n,
                    "message": "Statement completed (no result set).",
                },
                indent=2,
            )


@mcp.tool()
def query(
    sql: str,
    params: dict[str, Any] | None = None,
) -> str:
    """
    Execute a single SQL statement against Oracle as ``ORACLE_USER``.

    This is a direct pass-through: there is no read-only or row limit in the server
    code. You may run ``SELECT`` (full result in memory), ``INSERT``/``UPDATE``/``DELETE``,
    DDL, PL/SQL blocks—whatever the database user is permitted to do.

    Use named binds: ``:name`` in the SQL and matching keys in ``params``
    (e.g. ``{"id": 1}``).

    Args:
        sql: One complete SQL or PL/SQL string.
        params: Optional bind dictionary.

    Returns:
        For queries: JSON with ``columns``, ``rows``, ``row_count``. For other
        statements: JSON with ``affected_rows`` / ``rowcount`` when available.
    """
    return _run_sql(sql, params)


if __name__ == "__main__":
    mcp.run()
