"""Percona Server 9.7 with the built-in DuckDB storage engine (``ENGINE=DuckDB``).

The image entrypoint runs ``mysqld --no-defaults``, so settings are mysqld command-line
arguments rendered from ``systems/percona/mysqld-args.j2`` plus two environment variables.
"""
from __future__ import annotations

import pymysql

from dbench import dockerctl
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.systems import baseline_conf
from dbench.systems._common import (
    READ_MARGIN_S,
    pushed_from_counts,
    read_status_counter,
)
from dbench.systems.base import Pushed
from dbench.systems.mysql_family import (
    DATABASE,
    PORT,
    ConvertToDuckDBMixin,
    MySQLFamilyAdapter,
)

IMAGE = ("perconalab/ducksdb-mysql-engine:9.7-duckdb-v0.2.3"
         "@sha256:5e4af8b2bede83db7b296be977a9c22fb30e8a05a565e3b3b0fe08fea9a96640")
ENGINE_VERSION = "ducksdb-mysql-engine v0.2.3"
COUNTER = "Ducksdb_pushdown_count"
COUNTER_SQL = f"SHOW GLOBAL STATUS LIKE '{COUNTER}'"
ER_BAD_DB = 1049
DUCKDB_VERSION = "1.5.3"
DUCKDB_SOURCE = "stated by the engine maintainer; engine exposes no version at runtime"
SPILL_DIR = "/var/lib/mysql/duckdb_tmp"


def container_command(limits: Limits) -> list[str]:
    """The rendered mysqld arguments (comment and blank lines dropped)."""
    text = baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "percona" / "mysqld-args.j2",
        baseline_conf.duckdb_engine_settings(limits, SPILL_DIR))
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]


def container_env(limits: Limits) -> dict[str, str]:
    """Empty root password (as the other MySQL-family adapters) and DuckDB's memory cap."""
    return {
        "MYSQL_ROOT_PASSWORD": "",
        "DUCKSDB_MEMORY_LIMIT": baseline_conf.duckdb_engine_settings(
            limits, SPILL_DIR)["duck_memory_gb"],
        "DUCKSDB_TEMP_DIR": SPILL_DIR,
    }


class PerconaEngine(ConvertToDuckDBMixin, MySQLFamilyAdapter):
    """Pushdown detection reads the global ``Ducksdb_pushdown_count`` around a query. The
    counter is server-wide, so the delta is only attributable to one query when nothing else
    runs concurrently (the power run is single-stream); under concurrent streams a delta
    may belong to another stream's query."""

    name = "percona"
    track = "A"
    config_dir = "percona"
    image = IMAGE
    engine_clause = "ENGINE=DuckDB"
    version_key = "mysql"
    datadir = "/var/lib/mysql"

    def container_env(self) -> dict[str, str]:
        if self._limits is None:
            raise RuntimeError("container_env() before start()")
        return container_env(self._limits)

    def container_command(self, limits: Limits) -> list[str]:
        return container_command(limits)

    def start(self, limits: Limits) -> None:
        self._limits = limits  # the env is built from the limits
        super().start(limits)
        try:
            self._make_spill_dir()
        except Exception:
            self.stop()  # leave no running container behind a failed start
            raise

    def _make_spill_dir(self) -> None:
        """DuckDB spills here (on the datadir volume); created after the server is up."""
        c = dockerctl._client().containers.get(self.container_name)
        code, out = c.exec_run(["mkdir", "-p", SPILL_DIR])
        if code != 0:
            raise RuntimeError(f"cannot create {SPILL_DIR}: {out.decode(errors='replace')}")

    def _connect(self, *, read_timeout, connect_timeout=10):
        """The image creates no database; make ``tpch`` on first contact."""
        try:
            return super()._connect(read_timeout=read_timeout, connect_timeout=connect_timeout)
        except pymysql.err.OperationalError as exc:
            if not exc.args or exc.args[0] != ER_BAD_DB:
                raise
        boot = pymysql.connect(
            host=self.host(), port=PORT, user=self.user, password="",
            connect_timeout=connect_timeout, autocommit=True)
        try:
            with boot.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS {DATABASE}")
        finally:
            boot.close()
        return super()._connect(read_timeout=read_timeout, connect_timeout=connect_timeout)

    def supports(self, measure: str) -> tuple[bool, str | None]:
        return True, None  # has a DuckDB engine: conversion (M11) and pushdown (M12) apply

    def versions(self) -> dict:
        info = super().versions()
        return {**info, "engine": ENGINE_VERSION, "duckdb": DUCKDB_VERSION,
                "duckdb_source": DUCKDB_SOURCE,
                "duckdb_threads": "auto-detected; no setting or status variable exposes it"}

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

    def pushdown(self, sql: str) -> Pushed:
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
