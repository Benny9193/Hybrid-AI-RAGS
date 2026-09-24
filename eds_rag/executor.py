"""Read-only query execution against SQL Server.

The guard has already rejected anything that isn't a single SELECT. This
layer enforces the limits that must hold even if the guard were bypassed:
row cap, query timeout, lock timeout, deadlock priority - and it refuses to
run at all if the login turns out to have write permissions.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Database-level permissions a read-only login must NOT have.
WRITE_PERMISSIONS = ("INSERT", "UPDATE", "DELETE", "ALTER", "EXECUTE", "CREATE TABLE", "CONTROL")

PERMISSION_CHECK_SQL = (
    "SELECT "
    + ", ".join(f"HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', '{p}') AS [{p}]" for p in WRITE_PERMISSIONS)
    + ", IS_SRVROLEMEMBER('sysadmin') AS [sysadmin], IS_ROLEMEMBER('db_owner') AS [db_owner]"
    + ", IS_ROLEMEMBER('db_datawriter') AS [db_datawriter], IS_ROLEMEMBER('db_ddladmin') AS [db_ddladmin]"
)


class NotReadOnlyError(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int
    warnings: list[str] = field(default_factory=list)


def json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v) if abs(v) < 1e15 else str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        return "0x" + b[:32].hex() + ("..." if len(b) > 32 else "")
    return str(v)


def _pyodbc_connect(conn_str: str, login_timeout: int) -> Any:
    import pyodbc  # optional dependency: pip install eds-rag[sqlserver]

    return pyodbc.connect(conn_str, autocommit=True, readonly=True, timeout=login_timeout)


class ReadOnlyExecutor:
    def __init__(
        self,
        conn_str: str,
        *,
        max_rows: int = 1000,
        timeout_s: int = 10,
        verify_read_only: bool = True,
        audit_log: str | Path | None = None,
        connect: Callable[[], Any] | None = None,
    ):
        self.conn_str = conn_str
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.audit_log = Path(audit_log) if audit_log else None
        self._connect_fn = connect or (lambda: _pyodbc_connect(conn_str, login_timeout=min(timeout_s, 15)))
        self._verified = not verify_read_only

    # ---------------------------------------------------------- connection
    def connect(self) -> Any:
        conn = self._connect_fn()
        if hasattr(conn, "timeout"):
            conn.timeout = self.timeout_s  # pyodbc per-statement query timeout
        cur = conn.cursor()
        # Don't wait on locks longer than the query budget, and if we ever
        # deadlock with production traffic, we're the one that gets killed.
        cur.execute(f"SET LOCK_TIMEOUT {int(self.timeout_s * 1000)}")
        cur.execute("SET DEADLOCK_PRIORITY LOW")
        # Hard server-side row cap, independent of the TOP the guard injected.
        cur.execute(f"SET ROWCOUNT {int(self.max_rows) + 1}")
        cur.close()
        if not self._verified:
            self._verify(conn)
        return conn

    def _verify(self, conn: Any) -> None:
        cur = conn.cursor()
        cur.execute(PERMISSION_CHECK_SQL)
        row = cur.fetchone()
        names = [d[0] for d in cur.description]
        cur.close()
        granted = [n for n, v in zip(names, row) if v]
        if granted:
            conn.close()
            raise NotReadOnlyError(
                "refusing to run: the SQL login is not read-only (has "
                + ", ".join(granted)
                + "). Use a login with db_datareader only - see sql/create_readonly_login.sql"
            )
        self._verified = True

    # ------------------------------------------------------------ queries
    def run(self, sql: str) -> QueryResult:
        start = time.monotonic()
        status, n_rows = "ok", 0
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql)
            if cur.description is None:
                return QueryResult([], [], False, int((time.monotonic() - start) * 1000))
            columns = [d[0] for d in cur.description]
            fetched = cur.fetchmany(self.max_rows + 1)
            truncated = len(fetched) > self.max_rows
            rows = [[json_safe(v) for v in r] for r in fetched[: self.max_rows]]
            n_rows = len(rows)
            return QueryResult(columns, rows, truncated, int((time.monotonic() - start) * 1000))
        except Exception as e:
            status = f"error: {type(e).__name__}: {e}"
            raise
        finally:
            conn.close()
            self._audit("run", sql, status, n_rows, start)

    def showplan_xml(self, sql: str) -> str:
        """Estimated plan only - the query is compiled, never executed."""
        start = time.monotonic()
        status = "ok"
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SET SHOWPLAN_XML ON")
            try:
                cur.execute(sql)
                row = cur.fetchone()
                return str(row[0]) if row else ""
            finally:
                cur.execute("SET SHOWPLAN_XML OFF")
        except Exception as e:
            status = f"error: {type(e).__name__}: {e}"
            raise
        finally:
            conn.close()
            self._audit("explain", sql, status, 0, start)

    def _audit(self, action: str, sql: str, status: str, rows: int, start: float) -> None:
        if not self.audit_log:
            return
        entry = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "action": action,
            "status": status,
            "rows": rows,
            "elapsed_ms": int((time.monotonic() - start) * 1000),
            "sql": sql,
        }
        self.audit_log.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_log.open("a") as f:
            f.write(json.dumps(entry) + "\n")


def format_result(res: QueryResult, max_cell: int = 120) -> str:
    """Markdown table - compact and easy for the model to read."""
    if not res.columns:
        return "(no result set)"

    def cell(v: Any) -> str:
        s = "NULL" if v is None else str(v)
        s = s.replace("|", "\\|").replace("\n", " ")
        return s if len(s) <= max_cell else s[: max_cell - 3] + "..."

    lines = [
        "| " + " | ".join(res.columns) + " |",
        "|" + "---|" * len(res.columns),
        *("| " + " | ".join(cell(v) for v in r) + " |" for r in res.rows),
    ]
    footer = f"\n{len(res.rows)} row(s) in {res.elapsed_ms} ms"
    if res.truncated:
        footer += " - TRUNCATED at the row cap; add filters/aggregation instead of paging"
    return "\n".join(lines) + footer
