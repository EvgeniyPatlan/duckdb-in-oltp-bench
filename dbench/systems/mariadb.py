"""MariaDB 12.3.3 (LTS) with the DuckDB storage engine (``MariaDB-duckdb-engine``, gamma).

The image is built by ``systems/mariadb/Dockerfile`` (the official Docker image has no engine);
settings come from ``systems/mariadb/duckdb.cnf``, rendered from the limits and bind-mounted.
``load_bulk`` and ``checkpoint`` reach the embedded DuckDB through ``run_in_duckdb()``.
"""
from __future__ import annotations

from dbench import dockerctl
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.datagen import DatasetPaths
from dbench.schema import TABLES
from dbench.systems import baseline_conf
from dbench.systems._common import (
    LOAD_TIMEOUT_S,
    PROBE_TIMEOUT_S,
    READ_MARGIN_S,
    container_data_path,
    pushed_from_counts,
    read_status_counter,
)
from dbench.systems.base import Pushed, is_numeric_type
from dbench.systems.mysql_family import DATABASE, ConvertToDuckDBMixin, MySQLFamilyAdapter
from dbench.systems.villagesql import escape_sql_literal

IMAGE = "dbench/mariadb-duckdb:12.3.3"
CONF_TARGET = "/etc/my.cnf.d/dbench.cnf"
SPILL_DIR = "/var/lib/mysql/duckdb_tmp"
ROWS_READ_SQL = "SHOW SESSION STATUS LIKE 'Rows_read'"
VERSION_SQL = "SHOW GLOBAL STATUS LIKE 'Duckdb_version'"
# MariaDB's documented load (storage/duckdb/tpch/04_load.sh): the engine tables are
# <database>.<table> inside DuckDB, filled server-side from the Parquet files.
PARQUET_LOAD_SQL = "INSERT INTO {database}.{table} SELECT * FROM read_parquet([{files}])"


def render_config(limits: Limits) -> str:
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "mariadb" / "duckdb.cnf",
        baseline_conf.duckdb_engine_settings(limits, SPILL_DIR))


def duckdb_version_from_status(value: str) -> str:
    """``Duckdb_version`` reads 'v1.5.5'."""
    return value.removeprefix("v")


def parse_run_in_duckdb(text: str) -> tuple[list[str], list[tuple], set[int]]:
    """Rows, column names and numeric column indexes of a ``run_in_duckdb()`` result.

    The function returns text: a line of tab-terminated column names, a line of tab-terminated
    DuckDB type names, ``[ Rows: N]``, then N rows of tab-separated values (NULL is the word
    ``NULL``) and a closing blank line. Values stay strings; numeric columns are
    taken from the type line. A value containing a tab or newline would break the framing; the
    row count in the header is checked against the rows read.
    """
    if not text.endswith("\n\n"):
        raise ValueError(f"unrecognised run_in_duckdb result: {text[:80]!r}")
    lines = text[:-2].split("\n")  # the closing blank line is not a row
    if len(lines) < 3 or not lines[2].startswith("[ Rows: "):
        raise ValueError(f"unrecognised run_in_duckdb result: {text[:80]!r}")
    columns = lines[0].rstrip("\t").split("\t") if lines[0] else []
    types = lines[1].rstrip("\t").split("\t") if lines[1] else []
    count = int(lines[2][len("[ Rows: "):].rstrip("]"))
    body = lines[3:]
    if len(body) != count:
        raise ValueError(f"run_in_duckdb returned {len(body)} rows, header says {count}")
    rows = []
    for line in body:
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise ValueError("run_in_duckdb row does not match its header (tab or newline in a "
                             f"value?): {line[:80]!r}")
        rows.append(tuple(None if c == "NULL" else c for c in cells))
    numeric = {i for i, t in enumerate(types) if is_numeric_type(t)}
    return columns, rows, numeric


def result_text(value) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else (value or "")


def run_in_duckdb(cur, sql: str) -> list[tuple]:
    """Run DuckDB SQL through ``run_in_duckdb()`` and return its rows. The function reports a
    DuckDB error as its text result, not as a server error, so a result that does not parse
    raises with that text."""
    cur.execute(f"SELECT run_in_duckdb({escape_sql_literal(sql)})")
    row = cur.fetchone()
    text = result_text(row[0] if row else None)
    try:
        return parse_run_in_duckdb(text)[1]
    except ValueError as exc:
        raise RuntimeError(f"run_in_duckdb failed: {text[:300]}") from exc


class MariaDBDuckDB(ConvertToDuckDBMixin, MySQLFamilyAdapter):
    """EXPLAIN cannot show the pushdown here (the engine only builds its select_handler for
    statements that start with SELECT, so ``EXPLAIN SELECT`` never reports ``PUSHED SELECT``);
    detection reads the session ``Rows_read`` counter around the query instead."""

    name = "mariadb"
    track = "A"
    pushdown_counter_scope = "session"
    config_dir = "mariadb"
    image = IMAGE
    engine_clause = "ENGINE=DuckDB"
    version_key = "mariadb"
    datadir = "/var/lib/mysql"

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            render_config(limits), self.name, "dbench.cnf", self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}

    def checkpoint(self) -> str | None:
        """MariaDB has no CHECKPOINT statement; DuckDB's runs through ``run_in_duckdb()``."""
        conn = self._connect(read_timeout=LOAD_TIMEOUT_S)
        try:
            with conn.cursor() as cur:
                run_in_duckdb(cur, "CHECKPOINT")
        finally:
            conn.close()
        return None

    def _bulk_parts(self, cur, data: DatasetPaths) -> None:
        """One ``INSERT ... SELECT * FROM read_parquet()`` per table over all its Parquet parts."""
        for table in TABLES:
            files = ", ".join(f"'{container_data_path(self.data_dir, part)}'"
                              for part in data.parquet_tables[table])
            run_in_duckdb(cur, PARQUET_LOAD_SQL.format(database=DATABASE, table=table, files=files))

    def _timeout_sql(self, timeout_s: int) -> str:
        return f"SET SESSION max_statement_time = {int(timeout_s)}"  # MariaDB's name, in seconds

    def supports(self, measure: str) -> tuple[bool, str | None]:
        return True, None  # has a DuckDB engine: conversion (M11) and pushdown (M12) apply

    def versions(self) -> dict:
        info = super().versions()
        conn = self._connect(read_timeout=PROBE_TIMEOUT_S)
        try:
            with conn.cursor() as cur:
                cur.execute(VERSION_SQL)
                row = cur.fetchone()
                duck = duckdb_version_from_status(row[1]) if row else "unknown"
        finally:
            conn.close()
        return {**info, "engine": "MariaDB-duckdb-engine 12.3.3-1.el9 (gamma)", "duckdb": duck,
                "duckdb_source": "status variable Duckdb_version"}

    # pushdown
    def _query_begin(self, cur) -> int | None:
        try:
            return read_status_counter(cur, ROWS_READ_SQL)
        except Exception:  # noqa: BLE001 - detection must never fail a query
            return None

    def _query_end(self, cur, token: int | None) -> Pushed:
        try:
            return pushed_from_counts(token, read_status_counter(cur, ROWS_READ_SQL), rises=False)
        except Exception:  # noqa: BLE001
            return "unknown"

    def pushdown(self, sql: str) -> Pushed:
        conn = self._connect(read_timeout=QUERY_TIMEOUT_S + READ_MARGIN_S)
        try:
            with conn.cursor() as cur:
                before = read_status_counter(cur, ROWS_READ_SQL)
                cur.execute(sql)
                if cur.description:
                    cur.fetchall()
                after = read_status_counter(cur, ROWS_READ_SQL)
                return pushed_from_counts(before, after, rises=False)
        finally:
            conn.close()
