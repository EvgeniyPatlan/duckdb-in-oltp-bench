"""Shared base for MySQL-protocol servers (MySQL, Percona, MariaDB, AliSQL, VillageSQL).

Engine-specific adapters override the class attributes and hooks: ``image``,
``engine_clause``, ``container_env``, ``config_mounts`` and ``version_key``.
"""
from __future__ import annotations

import time
from typing import Any, Literal

import pymysql
from pymysql.constants import FIELD_TYPE

from dbench.datagen import DatasetPaths, expected_rows
from dbench.schema import TABLES, render_schema, split_statements
from dbench.systems._common import (
    LOAD_TIMEOUT_S,
    PROBE_TIMEOUT_S,
    READ_MARGIN_S,
    ContainerServerAdapter,
    container_data_path,
)
from dbench.systems._crash import classify_connection_failure
from dbench.systems.base import LoadOutcome, Pushed, QueryOutcome

PORT = 3306
DATABASE = "tpch"
NUMERIC_CODES = frozenset({
    FIELD_TYPE.DECIMAL, FIELD_TYPE.NEWDECIMAL, FIELD_TYPE.FLOAT, FIELD_TYPE.DOUBLE,
    FIELD_TYPE.TINY, FIELD_TYPE.SHORT, FIELD_TYPE.INT24, FIELD_TYPE.LONG, FIELD_TYPE.LONGLONG,
})
ER_QUERY_INTERRUPTED = 1317
ER_MAX_EXECUTION_TIME = 3024
CR_LOST = 2013
CONNECTION_CODES = frozenset({2002, 2003, 2006, 2013})
LOAD_SQL = (
    "LOAD DATA INFILE '{path}' INTO TABLE {table} FIELDS TERMINATED BY '|' "
    "OPTIONALLY ENCLOSED BY '\"' LINES TERMINATED BY '\\n'"
)


def numeric_cols_from_codes(codes: list[int]) -> set[int]:
    """Indexes of numeric columns from pymysql ``cursor.description`` type codes."""
    return {i for i, c in enumerate(codes) if c in NUMERIC_CODES}


def _code(exc: BaseException) -> int | None:
    if isinstance(exc, pymysql.MySQLError) and exc.args and isinstance(exc.args[0], int):
        return exc.args[0]
    return None


def is_timeout_error(exc: BaseException, elapsed_s: float, timeout_s: float) -> bool:
    """The server's max_execution_time, our KILL QUERY, or a client read timeout."""
    code = _code(exc)
    if code in (ER_MAX_EXECUTION_TIME, ER_QUERY_INTERRUPTED):
        return True
    return code == CR_LOST and elapsed_s >= timeout_s


def is_connection_error(exc: BaseException) -> bool:
    code = _code(exc)
    if code is None:
        return isinstance(exc, OSError)
    return code in CONNECTION_CODES


class MySQLFamilyAdapter(ContainerServerAdapter):
    dialect = "mysql"
    family = "mysql"
    engine_clause = ""
    version_key = "mysql"
    user = "root"
    datadir = "/var/lib/mysql"
    # Replication logs (binlog) are excluded from storage; redo log and tablespaces count.
    du_command = "du -sb --exclude='binlog.*' /var/lib/mysql"

    def _connect(self, *, read_timeout: float | None, connect_timeout: float = PROBE_TIMEOUT_S):
        return pymysql.connect(
            host=self.host(), port=PORT, user=self.user, password="", database=DATABASE,
            connect_timeout=connect_timeout, read_timeout=read_timeout, autocommit=True,
        )

    def connect(self) -> Any:
        return self._connect(read_timeout=None)

    def alive(self) -> bool:
        try:
            conn = self._connect(read_timeout=PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - unreachable means not alive
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
        except Exception:  # noqa: BLE001
            return False
        finally:
            conn.close()

    # versions and DDL
    def versions(self) -> dict:
        conn = self._connect(read_timeout=PROBE_TIMEOUT_S)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION()")
                version = cur.fetchone()[0]
        finally:
            conn.close()
        return {self.version_key: version, **self.image_info()}

    def _run_statements(self, statements: list[str]) -> None:
        conn = self._connect(read_timeout=LOAD_TIMEOUT_S)
        try:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
        finally:
            conn.close()

    def create_schema(self, sf: int, kind: Literal["engine", "rowstore"]) -> None:
        drops = [f"DROP TABLE IF EXISTS {t}" for t in reversed(TABLES)]
        self._run_statements(drops)
        ddl = render_schema(self.dialect, self.engine_clause, kind)
        self._run_statements(split_statements(ddl))

    # loading
    def load_bulk(self, data: DatasetPaths) -> LoadOutcome:
        """LOAD DATA INFILE per part, row-count check against the manifest, then
        ANALYZE TABLE; the load time covers the loads and the analyze."""
        started = time.perf_counter()
        try:
            conn = self._connect(read_timeout=LOAD_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        try:
            with conn.cursor() as cur:
                return self._load(cur, data, started)
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        finally:
            conn.close()

    def _load_parts(self, cur, data: DatasetPaths) -> None:
        for table in TABLES:
            for part in data.tables[table]:
                path = container_data_path(self.data_dir, part)
                cur.execute(LOAD_SQL.format(path=path, table=table))

    @staticmethod
    def _count_rows(cur) -> dict[str, int]:
        actual = {}
        for table in TABLES:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            actual[table] = cur.fetchone()[0]
        return actual

    def _load(self, cur, data: DatasetPaths, started: float) -> LoadOutcome:
        self._load_parts(cur, data)
        loaded_s = time.perf_counter() - started
        expected = expected_rows(data)
        actual = self._count_rows(cur)
        if actual != expected:
            return LoadOutcome("error", loaded_s, "row count does not match the manifest",
                               {"kind": "row_count", "expected": expected, "actual": actual})
        analyze_start = time.perf_counter()
        for table in TABLES:
            cur.execute(f"ANALYZE TABLE {table}")
            cur.fetchall()
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
    def _query_begin(self, cur) -> Any:
        """Hook run on the query's connection just before it executes (not timed)."""
        return None

    def _query_end(self, cur, token: Any) -> Pushed:
        """Hook run just after the query finished (not timed); fills ``QueryOutcome.pushed``."""
        return "n/a"

    def execute(self, sql: str, timeout_s: int) -> QueryOutcome:
        started = time.perf_counter()
        try:
            conn = self._connect(read_timeout=timeout_s + READ_MARGIN_S)
        except Exception as exc:  # noqa: BLE001
            return classify_connection_failure(self.crash_info, exc)
        ident: int | None = None
        keep_active = False
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET SESSION max_execution_time = {int(timeout_s * 1000)}")
                ident = conn.thread_id()
                self.active.add(ident, timeout_s)
                token = self._query_begin(cur)
                started = time.perf_counter()
                cur.execute(sql)
                rows = list(cur.fetchall()) if cur.description else []
                elapsed = time.perf_counter() - started
                desc = cur.description or ()
                pushed = self._query_end(cur, token)
            return QueryOutcome(
                "ok", elapsed, rows, numeric_cols_from_codes([d[1] for d in desc]), None,
                pushed, [d[0] for d in desc],
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            elapsed = time.perf_counter() - started
            if is_timeout_error(exc, elapsed, timeout_s):
                if _code(exc) == CR_LOST:
                    # A lost connection past the deadline is a timeout only if the server
                    # is still there; a dead container is a crash.
                    lost = classify_connection_failure(self.crash_info, exc)
                    if lost.status == "crashed":
                        return lost
                # Leave the id registered: a client-side read timeout leaves the query
                # running, and cancel() kills it once the deadline has passed.
                keep_active = _code(exc) == CR_LOST
                return QueryOutcome("timeout", elapsed, None, set(), f"{type(exc).__name__}: {exc}")
            if is_connection_error(exc):
                return classify_connection_failure(self.crash_info, exc)
            return QueryOutcome("error", elapsed, None, set(), f"{type(exc).__name__}: {exc}")
        finally:
            if ident is not None and not keep_active:
                self.active.remove(ident)
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - connection may already be dead
                pass

    def cancel(self) -> bool:
        """KILL QUERY, from a second connection, for queries past their deadline."""
        ids = self.active.pop_expired()
        try:
            conn = self._connect(read_timeout=PROBE_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            return False
        try:
            with conn.cursor() as cur:
                for ident in ids:
                    try:
                        cur.execute(f"KILL QUERY {int(ident)}")
                    except pymysql.MySQLError:
                        pass  # thread already gone
            return True
        finally:
            conn.close()
