"""AliSQL 8.0.44-3 with its built-in DuckDB storage engine (``ENGINE=DuckDB``).

The image is built by ``systems/alisql/Dockerfile`` from the official release tarball; settings
come from ``systems/alisql/my.cnf``, rendered from the limits and bind-mounted. AliSQL has no
fallback: when every table is DuckDB the original query text goes to DuckDB, and a query it
rejects (or one that mixes engines) is an error.
"""
from __future__ import annotations

from dataclasses import replace

from dbench import dockerctl
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.systems import baseline_conf
from dbench.systems._common import READ_MARGIN_S, pushed_from_counts, read_status_counter
from dbench.systems.base import Pushed, QueryOutcome
from dbench.systems.mysql_family import ConvertToDuckDBMixin, MySQLFamilyAdapter

IMAGE = "dbench/alisql:8.0.44-3"
CONF_TARGET = "/etc/alisql/dbench.cnf"
SPILL_DIR = "/var/lib/mysql/duckdb_tmp"
MIXED_ENGINE_MESSAGE = "Does not support mixed queries of duckdb engine and other engines"
ENGINE_VERSION = "AliSQL 8.0.44-3"
# max_execution_time reaches a running DuckDB query as an interrupt, reported as a DuckDB error
# (7501 "INTERRUPT Error: Interrupted!" or 7513 "DuckDB send result error. Interrupted!").
INTERRUPT_CODES = ("(7501,", "(7513,")
COUNTER_SQL = "SHOW SESSION STATUS LIKE 'Com_duckdb_select'"
DUCKDB_VERSION = "1.5.2-dev736"
DUCKDB_SOURCE = ("AliSQL 8.0.44-3 release notes (bundled snapshot v1.5.2-dev736, also the only "
                 "such string in the mysqld binary); no DuckDB version is exposed at runtime")


def render_config(limits: Limits) -> str:
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "alisql" / "my.cnf",
        baseline_conf.duckdb_engine_settings(limits, SPILL_DIR))


def is_interrupt(error: str, elapsed_s: float | None, timeout_s: float) -> bool:
    """A DuckDB interrupt that arrives at the deadline is the server's max_execution_time."""
    return ("Interrupted" in error and any(c in error for c in INTERRUPT_CODES)
            and elapsed_s is not None and elapsed_s >= timeout_s - 1)


class AliSQLDuckDB(ConvertToDuckDBMixin, MySQLFamilyAdapter):
    """AliSQL has no fallback: a query on all-DuckDB tables goes to DuckDB as written and a query
    it cannot take (mixed engines, unsupported syntax) fails with an error, which is recorded as an
    ``error`` and never as a fallback. ``pushed`` comes from the session ``Com_duckdb_select``
    counter around the query, so a silent row-path execution would show up as ``fallback``."""

    name = "alisql"
    track = "A"
    config_dir = "alisql"
    image = IMAGE
    engine_clause = "ENGINE=DuckDB"
    version_key = "mysql"
    datadir = "/var/lib/mysql"
    # AliSQL has no CHECKPOINT statement; the dbms_duckdb package runs one inside DuckDB.
    checkpoint_sql = "CALL dbms_duckdb.query('CHECKPOINT')"

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            render_config(limits), self.name, "dbench.cnf", self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}

    def supports(self, measure: str) -> tuple[bool, str | None]:
        return True, None  # has a DuckDB engine: conversion (M11) and pushdown (M12) apply

    def versions(self) -> dict:
        info = super().versions()
        return {**info, "engine": ENGINE_VERSION, **self._duckdb_version()}

    def _duckdb_version(self) -> dict:
        return {"duckdb": DUCKDB_VERSION, "duckdb_source": DUCKDB_SOURCE}

    # pushdown
    def _query_begin(self, cur) -> int | None:
        try:
            return read_status_counter(cur, COUNTER_SQL)
        except Exception:  # noqa: BLE001 - detection must never fail a query
            return None

    def _query_end(self, cur, token: int | None) -> Pushed:
        try:
            return pushed_from_counts(token, read_status_counter(cur, COUNTER_SQL))
        except Exception:  # noqa: BLE001
            return "unknown"

    def execute(self, sql: str, timeout_s: int) -> QueryOutcome:
        out = super().execute(sql, timeout_s)
        if out.status != "error" or not out.error:
            return out
        if MIXED_ENGINE_MESSAGE in out.error:
            return replace(out, extra={**out.extra, "kind": "mixed_engine"})
        if is_interrupt(out.error, out.elapsed_s, timeout_s):
            return replace(out, status="timeout")
        return out

    def pushdown(self, sql: str) -> Pushed:
        """Raises when the query fails (mixed engines, unsupported syntax): the caller records
        that as an error, never as a fallback."""
        conn = self._connect(read_timeout=QUERY_TIMEOUT_S + READ_MARGIN_S)
        try:
            with conn.cursor() as cur:
                before = read_status_counter(cur, COUNTER_SQL)
                cur.execute(sql)
                if cur.description:
                    cur.fetchall()
                return pushed_from_counts(before, read_status_counter(cur, COUNTER_SQL))
        finally:
            conn.close()
