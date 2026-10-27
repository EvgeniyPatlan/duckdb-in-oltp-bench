"""MariaDB 13.0.2 with the DuckDB storage engine (``MariaDB-duckdb-engine``, gamma).

The image is built by ``systems/mariadb/Dockerfile`` (the official Docker image has no engine);
settings come from ``systems/mariadb/duckdb.cnf``, rendered from the limits and bind-mounted.
"""
from __future__ import annotations

from dbench import dockerctl
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.systems import baseline_conf
from dbench.systems._common import PROBE_TIMEOUT_S, READ_MARGIN_S
from dbench.systems.base import Pushed
from dbench.systems.mysql_family import ConvertToDuckDBMixin, MySQLFamilyAdapter

IMAGE = "dbench/mariadb-duckdb:13.0.2"
CONF_TARGET = "/etc/my.cnf.d/dbench.cnf"
SPILL_DIR = "/var/lib/mysql/duckdb_tmp"
ROWS_READ_SQL = "SHOW SESSION STATUS LIKE 'Rows_read'"
VERSION_SQL = "SHOW GLOBAL STATUS LIKE 'Duckdb_version'"


def mariadb_settings(limits: Limits) -> dict:
    """Same formulas as the Percona/AliSQL engines: the InnoDB side only holds catalog and
    staging, so its pool is mem_gb/16; DuckDB gets the 0.75 * mem_gb share."""
    base = baseline_conf.innodb_settings(limits)
    return {
        "duck_memory": baseline_conf.size(0.75 * limits.mem_gb, "G"),
        "cores": base["read_threads"],
        "spill_dir": SPILL_DIR,
        "buffer_pool": baseline_conf.size(limits.mem_gb / 16, "G"),
        "temp_ram": base["temp_ram"],
        "redo_capacity": base["redo_capacity"],
    }


def render_config(limits: Limits) -> str:
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "mariadb" / "duckdb.cnf", mariadb_settings(limits))


def duckdb_version_from_status(value: str) -> str:
    """``Duckdb_version`` reads 'v1.5.5'."""
    return value.removeprefix("v")


def rows_read(cur) -> int | None:
    """Session ``Rows_read``: rows the server read through table handlers. A query handled by
    the engine's select_handler never goes through the handler row API, so the counter does not
    move; the row path (any fallback) raises it."""
    cur.execute(ROWS_READ_SQL)
    row = cur.fetchone()
    return int(row[1]) if row else None


def pushed_from_rows_read(before: int | None, after: int | None) -> Pushed:
    if before is None or after is None:
        return "unknown"
    return "duckdb" if after == before else "fallback"


class MariaDBDuckDB(ConvertToDuckDBMixin, MySQLFamilyAdapter):
    """EXPLAIN cannot show the pushdown here (the engine only builds its select_handler for
    statements that start with SELECT, so ``EXPLAIN SELECT`` never reports ``PUSHED SELECT``);
    detection reads the session ``Rows_read`` counter around the query instead."""

    name = "mariadb"
    track = "A"
    config_dir = "mariadb"
    image = IMAGE
    engine_clause = "ENGINE=DuckDB"
    version_key = "mariadb"
    datadir = "/var/lib/mysql"

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            render_config(limits), self.name, "dbench.cnf", self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}

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
                duck = duckdb_version_from_status(cur.fetchone()[1])
        finally:
            conn.close()
        return {**info, "engine": "MariaDB-duckdb-engine 13.0.2-1.el9 (gamma)", "duckdb": duck,
                "duckdb_source": "status variable Duckdb_version"}

    # pushdown
    def _query_begin(self, cur) -> int | None:
        try:
            return rows_read(cur)
        except Exception:  # noqa: BLE001 - detection must never fail a query
            return None

    def _query_end(self, cur, token: int | None) -> Pushed:
        try:
            return pushed_from_rows_read(token, rows_read(cur))
        except Exception:  # noqa: BLE001
            return "unknown"

    def pushdown(self, sql: str) -> Pushed:
        conn = self._connect(read_timeout=QUERY_TIMEOUT_S + READ_MARGIN_S)
        try:
            with conn.cursor() as cur:
                before = rows_read(cur)
                cur.execute(sql)
                if cur.description:
                    cur.fetchall()
                return pushed_from_rows_read(before, rows_read(cur))
        finally:
            conn.close()
