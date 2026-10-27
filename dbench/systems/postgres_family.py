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
from dbench.systems.base import LoadOutcome, QueryOutcome

WATCHDOG_GRACE_S = 10
PORT = 5432
DATABASE = "tpch"
USER = "postgres"
# int2, int4, int8, float4, float8, numeric
NUMERIC_OIDS = frozenset({21, 23, 20, 700, 701, 1700})
COPY_SQL = "COPY {table} FROM '{path}' WITH (FORMAT csv, DELIMITER '|', QUOTE '\"')"


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

    def _load(self, conn, data: DatasetPaths, started: float) -> LoadOutcome:
        conn.execute("SET statement_timeout = 0")
        for table in TABLES:
            for part in data.tables[table]:
                path = container_data_path(self.data_dir, part)
                conn.execute(COPY_SQL.format(table=table, path=path))
        loaded_s = time.perf_counter() - started
        expected = expected_rows(data)
        actual = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        if actual != expected:
            return LoadOutcome("error", loaded_s, "row count does not match the manifest",
                               {"kind": "row_count", "expected": expected, "actual": actual})
        analyze_start = time.perf_counter()
        for table in TABLES:
            # Freezing sets hint bits and freezes tuples now, so cold runs do not pay for it.
            conn.execute(f"VACUUM (FREEZE, ANALYZE) {table}")
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
            self.active.add(pid, timeout_s)
            timer.start()
            started = time.perf_counter()
            cur = conn.execute(sql)
            rows = cur.fetchall() if cur.description else []
            elapsed = time.perf_counter() - started
            desc = cur.description or []
            return QueryOutcome(
                "ok", elapsed, [tuple(r) for r in rows],
                numeric_cols_from_oids([d.type_code for d in desc]), None, "n/a",
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
