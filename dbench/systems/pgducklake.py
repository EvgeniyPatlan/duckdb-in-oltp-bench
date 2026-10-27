"""pg_ducklake on PostgreSQL 18: DuckLake tables (``CREATE TABLE ... USING ducklake``), stored as
Parquet files on the data volume with the catalog in Postgres, analysed by DuckDB.

The image is built from the v1.0.3 tag by ``systems/pgducklake/build.sh`` (Docker Hub has no
v1.0.3 image); settings come from ``systems/pgducklake/postgresql.conf``.
"""
from __future__ import annotations

import time

from dbench.config import Limits
from dbench.datagen import DatasetPaths, expected_rows
from dbench.schema import TABLES, render_schema, split_statements
from dbench.systems import baseline_conf, pgduckdb
from dbench.systems._common import container_data_path
from dbench.systems.base import LoadOutcome
from dbench.systems.pgduckdb import PgDuckBase

IMAGE = "dbench/pgducklake:18-v1.0.3"
DUCKDB_VERSION_SQL = "SELECT * FROM ducklake.query('SELECT version() AS v')"
STAGING_SCHEMA = "dbench_stage"
READ_CSV = ("ducklake.read_csv('{path}', header := false, delim := '|', quote := '\"', "
            "all_varchar := true)")
COLUMN_TYPES_SQL = (
    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
    "WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped ORDER BY attnum")


def render_config(limits: Limits) -> str:
    return pgduckdb.render_config(limits, "pgducklake", "pgducklake")


def duckdb_session_sql(limits: Limits) -> str:
    """DuckDB-side settings for one session. Memory goes in MiB so ``0.75 * mem_gb`` GB is exact
    (DuckDB reads 'GB' as 10^9 bytes)."""
    s = baseline_conf.duckdb_pg_settings(limits, "pgducklake")
    return (f"SET threads = {s['duck_threads']}; SET memory_limit = '{s['duck_memory_mib']}MiB'; "
            f"SET temp_directory = '{s['spill_dir']}'")


def insert_from_csv_sql(table: str, path: str, types: list[str]) -> str:
    """``INSERT ... SELECT`` of one headerless '|' CSV part through DuckDB's reader; every field
    is read as text and cast to the table's column type by position. DuckDB names headerless
    columns ``column0``..; once there are ten or more it zero-pads them (``column00``..)."""
    width = len(str(len(types) - 1))
    cols = ", ".join(f"r['column{i:0{width}d}']::{t}" for i, t in enumerate(types))
    return f"INSERT INTO {table} SELECT {cols} FROM {READ_CSV.format(path=path)} r"


class PgDuckLake(PgDuckBase):
    """DuckLake tables with the engine schema. ``COPY`` into a DuckLake table reports success but
    stores nothing visible (checked on the real server), so ``load_bulk`` uses the documented
    DuckDB reader path, ``INSERT INTO t SELECT ... FROM ducklake.read_csv(...)``."""

    name = "pg_ducklake"
    config_dir = "pgducklake"
    mode = "pgducklake"
    image = IMAGE
    engine_clause = "USING ducklake"
    bench_schema_kind = "engine"
    extension = "pg_ducklake"

    def _duckdb_version_sql(self) -> str:
        return DUCKDB_VERSION_SQL

    def supports(self, measure: str) -> tuple[bool, str | None]:
        # M11 is freshness. The project documents heap-to-DuckLake CDC (pg_duckpipe, a separate
        # project that is not in this image), so the measure applies; the mechanism is to be
        # designed post-talk. M12 (pushdown) applies as DuckLake tables are DuckDB-native.
        return True, None

    # DuckDB has no postgresql.conf settings in this extension: set them per session
    def _session_setup(self, conn) -> None:
        if self._limits is None:
            raise RuntimeError("session setup before start()")
        conn.execute("SELECT ducklake.raw_query(%s)", (duckdb_session_sql(self._limits),))

    # loading
    def _load_parts(self, conn, data: DatasetPaths) -> None:
        self._session_setup(conn)
        for table in TABLES:
            types = [r[0] for r in conn.execute(COLUMN_TYPES_SQL, (table,)).fetchall()]
            for part in data.tables[table]:
                path = container_data_path(self.data_dir, part)
                conn.execute(insert_from_csv_sql(table, path, types))

    @staticmethod
    def _finalize_sql(table: str) -> str:
        """VACUUM is a no-op on DuckLake tables (compaction is a background worker); ANALYZE is
        the statistics step, as for the other engines."""
        return f"ANALYZE {table}"

    def load_convert(self, data: DatasetPaths) -> LoadOutcome:
        """Load plain heap tables into a staging schema (COPY, engine schema), then
        ``INSERT INTO ducklake_t SELECT * FROM heap_t`` per table and ANALYZE. ``elapsed_s`` is
        conversion plus analyze; the staging load is ``extra.rowstore_load_s``."""
        started = time.perf_counter()
        try:
            self._run_statements([f"DROP TABLE IF EXISTS {t} CASCADE" for t in reversed(TABLES)])
            self._run_statements(split_statements(
                render_schema(self.dialect, self.engine_clause, "engine")))
            conn = self._connect()
            started = time.perf_counter()  # the staging timer starts after the DDL
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        try:
            return self._convert(conn, data, started)
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        finally:
            try:
                conn.execute(f"DROP SCHEMA IF EXISTS {STAGING_SCHEMA} CASCADE")
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass
            conn.close()

    def _convert(self, conn, data: DatasetPaths, started: float) -> LoadOutcome:
        conn.execute("SET statement_timeout = 0")
        conn.execute(f"DROP SCHEMA IF EXISTS {STAGING_SCHEMA} CASCADE")
        conn.execute(f"CREATE SCHEMA {STAGING_SCHEMA}")
        conn.execute(f"SET search_path = {STAGING_SCHEMA}")  # staging heap tables, plain PG
        for stmt in split_statements(render_schema(self.dialect, "", "engine")):
            conn.execute(stmt)
        PgDuckBase._load_parts(self, conn, data)  # plain COPY into the heap tables
        loaded_s = time.perf_counter() - started
        expected = expected_rows(data) if data is not None else None
        staged = {t: conn.execute(f"SELECT COUNT(*) FROM {STAGING_SCHEMA}.{t}").fetchone()[0]
                  for t in TABLES}
        if expected is not None and staged != expected:
            return LoadOutcome("error", loaded_s, "row count does not match the manifest",
                               {"kind": "row_count", "expected": expected, "actual": staged,
                                "rowstore_load_s": loaded_s})
        vacuum_start = time.perf_counter()
        for table in TABLES:  # as the Postgres load path: hint bits and freezing before reads
            conn.execute(f"VACUUM (FREEZE, ANALYZE) {STAGING_SCHEMA}.{table}")
        rowstore_s = loaded_s + (time.perf_counter() - vacuum_start)
        conn.execute("SET search_path = public")
        self._session_setup(conn)
        convert_start = time.perf_counter()
        for table in TABLES:
            conn.execute(f"INSERT INTO public.{table} SELECT * FROM {STAGING_SCHEMA}.{table}")
        for table in TABLES:
            conn.execute(self._finalize_sql(f"public.{table}"))
        converted_s = time.perf_counter() - convert_start
        actual = {t: conn.execute(f"SELECT COUNT(*) FROM public.{t}").fetchone()[0]
                  for t in TABLES}
        if actual != staged:
            return LoadOutcome("error", converted_s, "row count changed during conversion",
                               {"kind": "row_count", "expected": staged, "actual": actual,
                                "rowstore_load_s": rowstore_s})
        return LoadOutcome("ok", converted_s, None,
                           {"rows": actual, "rowstore_load_s": rowstore_s})
