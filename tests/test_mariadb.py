import os
from pathlib import Path

import pytest

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems.mariadb import (
    IMAGE,
    MariaDBDuckDB,
    duckdb_version_from_status,
    render_config,
)
from dbench.systems.registry import get_adapter

PLAN = Limits("0-15", "16-23", 64)
SMOKE = Limits("0-7", "8-11", 16)


class FakeCursor:
    """Answers SHOW SESSION STATUS LIKE 'Rows_read' from a script of values."""

    def __init__(self, rows_read):
        self._values = list(rows_read)
        self.executed = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if "Rows_read" in sql:
            n = self._values.pop(0)
            self._row = None if n is None else ("Rows_read", str(n))
        return 0

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []

    description = None


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def close(self):
        pass


def adapter_with(monkeypatch, rows_read):
    cur = FakeCursor(rows_read)
    a = MariaDBDuckDB(None)
    monkeypatch.setattr(a, "_connect", lambda **kw: FakeConn(cur))
    return a, cur


def test_pushed_select_reads_no_rows_through_the_handler(monkeypatch):
    a, cur = adapter_with(monkeypatch, [7, 7])
    assert a.pushdown("SELECT 1") == "duckdb"
    assert "SELECT 1" in cur.executed


def test_row_path_query_reads_rows_and_is_fallback(monkeypatch):
    a, _ = adapter_with(monkeypatch, [7, 1007])
    assert a.pushdown("SELECT 1") == "fallback"


def test_unreadable_status_is_unknown(monkeypatch):
    a, _ = adapter_with(monkeypatch, [None, None])
    assert a.pushdown("SELECT 1") == "unknown"


def test_query_hooks_run_outside_the_query(monkeypatch):
    cur = FakeCursor([3, 3])
    a = MariaDBDuckDB(None)
    token = a._query_begin(cur)
    assert a._query_end(cur, token) == "duckdb"
    assert all("Rows_read" in s for s in cur.executed)


def test_duckdb_version_parsing():
    assert duckdb_version_from_status("v1.5.5") == "1.5.5"
    assert duckdb_version_from_status("1.5.5") == "1.5.5"


def test_registry_and_contract():
    a = get_adapter("mariadb", None)
    assert isinstance(a, MariaDBDuckDB)
    assert (a.name, a.track, a.family, a.dialect, a.config_dir) == (
        "mariadb", "A", "mysql", "mysql", "mariadb")
    assert a.engine_clause == "ENGINE=DuckDB"
    assert a.supports("M11") == (True, None)
    assert IMAGE == "dbench/mariadb-duckdb:13.0.2"


def settings(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_config_at_plan_limits():
    got = settings(render_config(PLAN))
    expected = {
        "plugin-load-add": "ha_duckdb.so", "plugin-maturity": "gamma",
        "duckdb_memory_limit": "48G", "duckdb_max_threads": "16",
        "duckdb_temp_directory": "/var/lib/mysql/duckdb_tmp",
        "duckdb_use_double_for_decimal": "OFF",
        "innodb_buffer_pool_size": "4G", "innodb_log_file_size": "8G",
        "join_buffer_size": "256M", "sort_buffer_size": "256M",
        "tmp_table_size": "4G", "max_heap_table_size": "4G",
        "secure_file_priv": "/data", "local_infile": "OFF", "max_connections": "200",
        "character-set-server": "utf8mb4", "collation-server": "utf8mb4_general_ci",
    }
    for key, value in expected.items():
        assert got[key] == value, key
    assert "duckdb_require_primary_key" not in got  # left at its default (ON)


def test_config_at_16_gb():
    got = settings(render_config(SMOKE))
    assert got["duckdb_memory_limit"] == "12G" and got["duckdb_max_threads"] == "8"
    assert got["innodb_buffer_pool_size"] == "1G" and got["tmp_table_size"] == "1G"
    assert got["innodb_log_file_size"] == "2G"


def test_every_setting_has_a_reason_comment():
    lines = render_config(PLAN).splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith(("#", "[")):
            assert lines[i - 1].startswith("#"), line


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_engine_starts_and_live_variables_match_config():
    empty = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
    empty.mkdir(parents=True, exist_ok=True)
    limits = Limits("0-3", "8-11", 8)
    a = MariaDBDuckDB(empty)
    a.remove_data()
    a.start(limits)
    try:
        conn = a.connect()
        try:
            cur = conn.cursor()
            cur.execute("SHOW ENGINES")
            engines = {r[0]: r[1] for r in cur.fetchall()}
            assert engines.get("DUCKDB") in ("YES", "DEFAULT")
            cur.execute("SELECT @@duckdb_memory_limit, @@duckdb_max_threads, "
                        "@@innodb_buffer_pool_size, @@secure_file_priv, "
                        "@@duckdb_use_double_for_decimal, @@duckdb_temp_directory")
            assert cur.fetchone() == (6 << 30, 4, 512 << 20, "/data/", 0,
                                      "/var/lib/mysql/duckdb_tmp")
        finally:
            conn.close()
        v = a.versions()
        assert v["duckdb"] == "1.5.5" and v["mariadb"].startswith("13.0.2")
        code, _ = dockerctl._client().containers.get(a.container_name).exec_run(
            ["test", "-d", "/var/lib/mysql/duckdb_tmp"])
        assert code == 0
    finally:
        a.remove_data()


def test_timeout_uses_mariadb_statement_time_and_error_code():
    import pymysql

    from dbench.systems.mysql_family import is_timeout_error
    a = MariaDBDuckDB(None)
    assert a._timeout_sql(900) == "SET SESSION max_statement_time = 900"
    assert is_timeout_error(pymysql.err.OperationalError(1969, "interrupted"), 1.0, 900)
