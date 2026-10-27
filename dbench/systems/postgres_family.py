"""Shared base for PostgreSQL servers (Postgres, pg_duckdb, pg_ducklake).

Engine-specific adapters override the class attributes and hooks: ``image``,
``engine_clause``, ``container_env``, ``container_command``, ``config_mounts`` and
``version_key``.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Literal

import psycopg
from psycopg import errors as pgerrors

from dbench.datagen import DatasetPaths, expected_rows
from dbench.schema import TABLES, render_schema, split_statements
from dbench.systems._common import (
    PROBE_TIMEOUT_S,
    READ_MARGIN_S,
    ContainerServerAdapter,
    container_data_path,
)
from dbench.systems._crash import classify_connection_failure
from dbench.systems.base import LoadOutcome, Pushed, QueryOutcome

WATCHDOG_GRACE_S = 10
PORT = 5432
DATABASE = "tpch"
USER = "postgres"
# int2, int4, int8, float4, float8, numeric
NUMERIC_OIDS = frozenset({21, 23, 20, 700, 701, 1700})
PUSHDOWN_MARKERS = ("DuckDBScan", "DuckLakeScan")
COPY_SQL = "COPY {table} FROM '{path}' WITH (FORMAT csv, DELIMITER '|', QUOTE '\"')"


def pushed_from_explain(lines: list[str]) -> Pushed:
    """Pushdown from ``EXPLAIN`` text: pg_duckdb plans a query that DuckDB runs as
    ``Custom Scan (DuckDBScan)``, pg_ducklake as ``Custom Scan (DuckLakeScan)``; any other plan
    ran in the Postgres executor."""
    if not lines:
        return "unknown"
    pushed = any(marker in line for line in lines for marker in PUSHDOWN_MARKERS)
    return "duckdb" if pushed else "fallback"


def is_cancelled_by_deadline(exc: Exception, elapsed_s: float, timeout_s: float) -> bool:
    """DuckDB's executor inside pg_duckdb/pg_ducklake reports a ``statement_timeout`` cancel as
    ``InternalError_: ... Query cancelled`` (SQLSTATE XX000) rather than QueryCanceled (57014);
    it is a timeout when the message says cancelled and the deadline has passed."""
    return "query cancelled" in str(exc).lower() and elapsed_s >= timeout_s * 0.95


def numeric_cols_from_oids(oids: list[int]) -> set[int]:
    """Indexes of numeric columns from psycopg ``cursor.description`` type OIDs."""
    return {i for i, o in enumerate(oids) if o in NUMERIC_OIDS}


class PostgresFamilyAdapter(ContainerServerAdapter):
    dialect = "postgres"
    family = "postgres"
    engine_clause = ""
    version_key = "postgres"
    postgres_shm = True

    def _connect(self, connect_timeout: float = PROBE_TIMEOUT_S):
        return psycopg.connect(
            host=self.host(), port=PORT, user=USER, dbname=DATABASE, autocommit=True,
            connect_timeout=int(connect_timeout),
        )

    def connect(self) -> Any:
        return self._connect()

    def alive(self) -> bool:
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001 - unreachable means not alive
            return False
        try:
            conn.execute("SET statement_timeout = 10000")
            return conn.execute("SELECT 1").fetchone() == (1,)
        except Exception:  # noqa: BLE001
            return False
        finally:
            conn.close()

    def before_storage_measure(self) -> None:
        conn = self._connect()
        try:
            conn.execute("CHECKPOINT")
        finally:
            conn.close()

    # versions and DDL
    def versions(self) -> dict:
        conn = self._connect()
        try:
            version = conn.execute("SHOW server_version").fetchone()[0]
        finally:
            conn.close()
        return {self.version_key: version, **self.image_info()}

    def _run_statements(self, statements: list[str]) -> None:
        conn = self._connect()
        try:
            for stmt in statements:
                conn.execute(stmt)
        finally:
            conn.close()

    def create_schema(self, sf: int, kind: Literal["engine", "rowstore"]) -> None:
        self._run_statements([f"DROP TABLE IF EXISTS {t} CASCADE" for t in reversed(TABLES)])
        ddl = render_schema(self.dialect, self.engine_clause, kind)
        self._run_statements(split_statements(ddl))

    # loading
    def load_bulk(self, data: DatasetPaths) -> LoadOutcome:
        """COPY per part, row-count check against the manifest, then VACUUM (FREEZE,
        ANALYZE) per table; the load time covers the copies and the vacuums."""
        started = time.perf_counter()
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        try:
            return self._load(conn, data, started)
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        finally:
            conn.close()

    def _load_parts(self, conn, data: DatasetPaths) -> None:
        """Load every CSV part: ``COPY`` here; engines without a working COPY override it."""
        for table in TABLES:
            for part in data.tables[table]:
                path = container_data_path(self.data_dir, part)
                conn.execute(COPY_SQL.format(table=table, path=path))

    @staticmethod
    def _finalize_sql(table: str) -> str:
        """Statement run per table after the row-count check (part of the load time).
        Freezing sets hint bits and freezes tuples now, so cold runs do not pay for it."""
        return f"VACUUM (FREEZE, ANALYZE) {table}"

    def _load(self, conn, data: DatasetPaths, started: float) -> LoadOutcome:
        conn.execute("SET statement_timeout = 0")
        self._load_parts(conn, data)
        loaded_s = time.perf_counter() - started
        expected = expected_rows(data)
        actual = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        if actual != expected:
            return LoadOutcome("error", loaded_s, "row count does not match the manifest",
                               {"kind": "row_count", "expected": expected, "actual": actual})
        analyze_start = time.perf_counter()
        for table in TABLES:
            conn.execute(self._finalize_sql(table))
        elapsed = loaded_s + (time.perf_counter() - analyze_start)
        return LoadOutcome("ok", elapsed, None, {"rows": actual})

    def _load_failure(self, exc: Exception, started: float) -> LoadOutcome:
        elapsed = time.perf_counter() - started
        state = self.crash_info()
        if state.get("status") != "running":
            return LoadOutcome("crashed", elapsed, f"{type(exc).__name__}: {exc}",
                               {"container": state})
        return LoadOutcome("error", elapsed, f"{type(exc).__name__}: {exc}")

    # queries
    def _session_setup(self, conn) -> None:
        """Hook: per-session statements run right after ``statement_timeout`` (not timed)."""

    def _query_begin(self, conn, sql: str) -> Any:
        """Hook run on the query's connection just before it executes (not timed)."""
        return None

    def _query_end(self, conn, token: Any) -> Pushed:
        """Hook run just after the query finished (not timed); fills ``QueryOutcome.pushed``."""
        return "n/a"

    def _explain_pushed(self, conn, sql: str) -> Pushed:
        """Plan-only ``EXPLAIN`` (never runs the query), classified by ``pushed_from_explain``."""
        try:
            return pushed_from_explain([r[0] for r in conn.execute(f"EXPLAIN {sql}").fetchall()])
        except Exception:  # noqa: BLE001 - detection must never fail a query
            return "unknown"

    def _watchdog(self, pid: int, done: threading.Event, fired: threading.Event) -> None:
        """Client-side deadline for servers that ignore statement_timeout: cancel the
        backend, and terminate it if it is still busy after WATCHDOG_GRACE_S."""
        fired.set()
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001 - nothing more we can do
            return
        try:
            conn.execute("SELECT pg_cancel_backend(%s)", (pid,))
            if not done.wait(WATCHDOG_GRACE_S):
                conn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        except psycopg.Error:
            pass
        finally:
            conn.close()

    def execute(self, sql: str, timeout_s: int) -> QueryOutcome:
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            return classify_connection_failure(self.crash_info, exc)
        pid = conn.info.backend_pid
        done, fired = threading.Event(), threading.Event()
        timer = threading.Timer(timeout_s + READ_MARGIN_S, self._watchdog, (pid, done, fired))
        timer.daemon = True
        started = time.perf_counter()
        try:
            conn.execute(f"SET statement_timeout = {int(timeout_s * 1000)}")
            self._session_setup(conn)
            token = self._query_begin(conn, sql)
            self.active.add(pid, timeout_s)
            timer.start()
            started = time.perf_counter()
            cur = conn.execute(sql)
            rows = cur.fetchall() if cur.description else []
            elapsed = time.perf_counter() - started
            desc = cur.description or []
            pushed = self._query_end(conn, token)
            return QueryOutcome(
                "ok", elapsed, [tuple(r) for r in rows],
                numeric_cols_from_oids([d.type_code for d in desc]), None, pushed,
                [d.name for d in desc],
            )
        except pgerrors.QueryCanceled as exc:
            elapsed = time.perf_counter() - started
            return QueryOutcome("timeout", elapsed, None, set(), f"{type(exc).__name__}: {exc}")
        except psycopg.OperationalError as exc:
            if fired.is_set():  # the watchdog terminated the backend
                elapsed = time.perf_counter() - started
                return QueryOutcome("timeout", elapsed, None, set(),
                                    f"watchdog: {type(exc).__name__}: {exc}")
            return classify_connection_failure(self.crash_info, exc)
        except psycopg.Error as exc:
            elapsed = time.perf_counter() - started
            if is_cancelled_by_deadline(exc, elapsed, timeout_s):
                return QueryOutcome("timeout", elapsed, None, set(),
                                    f"{type(exc).__name__}: {exc}")
            return QueryOutcome("error", elapsed, None, set(), f"{type(exc).__name__}: {exc}")
        finally:
            done.set()
            timer.cancel()
            self.active.remove(pid)
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def lingering_queries(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT count(*) FROM pg_stat_activity WHERE state = 'active' "
                               "AND pid <> pg_backend_pid() AND usename = %s", (USER,)).fetchone()
            return int(row[0])
        finally:
            conn.close()

    def cancel(self) -> bool:
        """pg_cancel_backend, from a second connection, for queries past their deadline."""
        ids = self.active.pop_expired()
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001
            return False
        try:
            for pid in ids:
                conn.execute("SELECT pg_cancel_backend(%s)", (pid,))
            return True
        except psycopg.Error:
            return False
        finally:
            conn.close()
