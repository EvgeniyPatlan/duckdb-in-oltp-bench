"""MariaDB 13.0.2 with the DuckDB storage engine (``MariaDB-duckdb-engine``, gamma).

The image is built by ``systems/mariadb/Dockerfile`` (the official Docker image has no engine);
settings come from ``systems/mariadb/duckdb.cnf``, rendered from the limits and bind-mounted.
"""
from __future__ import annotations

from dbench import dockerctl
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.systems import baseline_conf
from dbench.systems._common import (
    PROBE_TIMEOUT_S,
    READ_MARGIN_S,
    pushed_from_counts,
    read_status_counter,
)
from dbench.systems.base import Pushed
from dbench.systems.mysql_family import ConvertToDuckDBMixin, MySQLFamilyAdapter

IMAGE = "dbench/mariadb-duckdb:13.0.2"
CONF_TARGET = "/etc/my.cnf.d/dbench.cnf"
SPILL_DIR = "/var/lib/mysql/duckdb_tmp"
ROWS_READ_SQL = "SHOW SESSION STATUS LIKE 'Rows_read'"
VERSION_SQL = "SHOW GLOBAL STATUS LIKE 'Duckdb_version'"


def render_config(limits: Limits) -> str:
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "mariadb" / "duckdb.cnf",
        baseline_conf.duckdb_engine_settings(limits, SPILL_DIR))


def duckdb_version_from_status(value: str) -> str:
    """``Duckdb_version`` reads 'v1.5.5'."""
    return value.removeprefix("v")


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
    no_checkpoint_reason = (
        "MariaDB has no CHECKPOINT statement; the DuckDB engine's run_in_duckdb() is refused "
        "unless the server runs with --duckdb-allow-run-in-duckdb, which is off by default")

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
                row = cur.fetchone()
                duck = duckdb_version_from_status(row[1]) if row else "unknown"
        finally:
            conn.close()
        return {**info, "engine": "MariaDB-duckdb-engine 13.0.2-1.el9 (gamma)", "duckdb": duck,
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
