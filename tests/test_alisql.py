import os
from pathlib import Path

import pymysql
import pytest

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems.alisql import (
    IMAGE,
    MIXED_ENGINE_MESSAGE,
    AliSQLDuckDB,
    render_config,
)
from dbench.systems.mysql_family import MySQLFamilyAdapter
from dbench.systems.registry import get_adapter

PLAN = Limits("0-15", "16-23", 64)
SMOKE = Limits("0-7", "8-11", 16)


def settings(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_registry_and_contract():
    a = get_adapter("alisql", None)
    assert isinstance(a, AliSQLDuckDB)
    assert (a.name, a.track, a.family, a.dialect, a.config_dir) == (
        "alisql", "A", "mysql", "mysql", "alisql")
    assert a.engine_clause == "ENGINE=DuckDB"
    assert a.bench_schema_kind == "engine"
    assert a.supports("M11") == (True, None)
    assert IMAGE == "dbench/alisql:8.0.44-3"


def test_config_at_plan_limits():
    got = settings(render_config(PLAN))
    expected = {
        "duckdb_mode": "ON", "duckdb_memory_limit": "48G", "duckdb_threads": "16",
        "duckdb_temp_directory": "/var/lib/mysql/duckdb_tmp",
        "duckdb_use_double_for_decimal": "OFF", "duckdb_sql_normalization": "OFF",
        "duckdb_require_primary_key": "ON",
        "innodb_buffer_pool_size": "4G", "innodb_redo_log_capacity": "8G",
        "innodb_parallel_read_threads": "16",
        "join_buffer_size": "256M", "sort_buffer_size": "256M",
        "tmp_table_size": "4G", "max_heap_table_size": "4G", "temptable_max_ram": "4G",
        "secure_file_priv": "/data", "local_infile": "OFF", "max_connections": "200",
    }
    for key, value in expected.items():
        assert got[key] == value, key
    assert "duckdb_max_temp_directory_size" not in got  # default 0 = 90% of free disk: spills


def test_config_at_16_gb():
    got = settings(render_config(SMOKE))
    assert got["duckdb_memory_limit"] == "12G" and got["duckdb_threads"] == "8"
    assert got["innodb_buffer_pool_size"] == "1G" and got["tmp_table_size"] == "1G"
    assert got["temptable_max_ram"] == "1G" and got["innodb_redo_log_capacity"] == "2G"
    assert got["innodb_parallel_read_threads"] == "8"


def test_every_setting_has_a_reason_comment():
    lines = render_config(PLAN).splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith(("#", "[")):
            assert lines[i - 1].startswith("#"), line


class FakeCursor:
    """Answers SHOW SESSION STATUS LIKE 'Com_duckdb_select' from a script of values."""

    def __init__(self, fail=None, counts=(0, 1), description=(("x", 8),)):
        self.fail = fail
        self.description = description
        self.executed = []
        self._counts = list(counts)
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if "Com_duckdb_select" in sql:
            n = self._counts.pop(0)
            self._row = None if n is None else ("Com_duckdb_select", str(n))
        elif self.fail and not sql.startswith("SET"):
            raise self.fail
        return 0

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [(1,)]


class FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def thread_id(self):
        return 7

    def close(self):
        pass


def adapter_with(monkeypatch, cur):
    a = AliSQLDuckDB(None)
    monkeypatch.setattr(a, "_connect", lambda **kw: FakeConn(cur))
    return a


def test_successful_query_on_duckdb_tables_is_pushed(monkeypatch):
    a = adapter_with(monkeypatch, FakeCursor(counts=(0, 1, 0, 1)))
    assert a.pushdown("SELECT 1") == "duckdb"
    out = a.execute("SELECT 1", 10)
    assert out.status == "ok" and out.pushed == "duckdb"


def test_error_not_reported_as_fallback(monkeypatch):
    err = pymysql.err.OperationalError(1064, "You have an error in your SQL syntax")
    a = adapter_with(monkeypatch, FakeCursor(fail=err))
    out = a.execute("SELECT nope", 10)
    assert out.status == "error" and out.pushed != "fallback"
    assert out.extra == {}
    with pytest.raises(pymysql.err.OperationalError):
        a.pushdown("SELECT nope")


def test_mixed_engine_error_message_classified(monkeypatch):
    err = pymysql.err.OperationalError(
        1235, f"{MIXED_ENGINE_MESSAGE} and other engines")
    a = adapter_with(monkeypatch, FakeCursor(fail=err))
    out = a.execute("SELECT * FROM a, b", 10)
    assert out.status == "error"
    assert out.extra == {"kind": "mixed_engine"}
    assert MIXED_ENGINE_MESSAGE in out.error
    assert out.pushed != "fallback"


def test_interrupt_at_the_deadline_is_a_timeout_but_early_interrupt_is_an_error(monkeypatch):
    err = pymysql.err.OperationalError(7501, "[DuckDB] INTERRUPT Error: Interrupted!.")
    a = adapter_with(monkeypatch, FakeCursor(fail=err))
    assert a.execute("SELECT 1", 0).status == "timeout"  # deadline already reached
    assert a.execute("SELECT 1", 900).status == "error"  # interrupted long before it


def test_unmoved_select_counter_is_fallback_and_unreadable_is_unknown(monkeypatch):
    a = adapter_with(monkeypatch, FakeCursor(counts=(3, 3)))
    assert a.pushdown("SELECT 1") == "fallback"
    a = adapter_with(monkeypatch, FakeCursor(counts=(None, None)))
    assert a.pushdown("SELECT 1") == "unknown"


def test_versions_record_the_bundled_snapshot_and_its_source(monkeypatch):
    a = adapter_with(monkeypatch, FakeCursor())
    monkeypatch.setattr(
        MySQLFamilyAdapter, "versions", lambda self: {"mysql": "8.0.44-3-alisql-dev"})
    v = a.versions()
    assert v["duckdb"] == "1.5.2-dev736" and "release notes" in v["duckdb_source"]


def test_timeout_is_max_execution_time_in_ms():
    assert AliSQLDuckDB(None)._timeout_sql(900) == "SET SESSION max_execution_time = 900000"


EMPTY = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
DOCKER = pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")


@pytest.mark.docker
@DOCKER
def test_engine_starts_and_live_variables_match_config():
    EMPTY.mkdir(parents=True, exist_ok=True)
    a = AliSQLDuckDB(EMPTY)
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        conn = a.connect()
        try:
            cur = conn.cursor()
            cur.execute("SHOW ENGINES")
            engines = {r[0]: r[1] for r in cur.fetchall()}
            assert engines.get("DUCKDB") in ("YES", "DEFAULT")
            cur.execute("SELECT @@duckdb_mode, @@duckdb_memory_limit, @@duckdb_threads, "
                        "@@duckdb_use_double_for_decimal, @@duckdb_sql_normalization, "
                        "@@duckdb_require_primary_key, @@innodb_buffer_pool_size, "
                        "@@secure_file_priv, @@duckdb_temp_directory")
            assert cur.fetchone() == ("ON", 6 << 30, 4, 0, 0, 1, 512 << 20, "/data/",
                                      "/var/lib/mysql/duckdb_tmp")
        finally:
            conn.close()
        v = a.versions()
        assert v["mysql"].startswith("8.0.44") and v["duckdb"]
        code, _ = dockerctl._client().containers.get(a.container_name).exec_run(
            ["test", "-d", "/var/lib/mysql/duckdb_tmp"])
        assert code == 0
    finally:
        a.remove_data()


@pytest.mark.docker
@DOCKER
def test_heavy_pushed_query_times_out_and_server_recovers():
    EMPTY.mkdir(parents=True, exist_ok=True)
    a = AliSQLDuckDB(EMPTY)
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        a.execute("CREATE TABLE heavy (a INT NOT NULL, PRIMARY KEY (a)) ENGINE=DuckDB", 30)
        a.execute("CREATE TABLE digits (d INT NOT NULL, PRIMARY KEY (d)) ENGINE=InnoDB", 30)
        a.execute("INSERT INTO digits VALUES (0),(1),(2),(3),(4),(5),(6),(7),(8),(9)", 30)
        cross = ", ".join(f"digits d{k}" for k in range(6))
        value = " + ".join(f"d{k}.d * {10 ** k}" for k in range(6))
        assert a.execute(f"INSERT INTO heavy SELECT {value} FROM {cross}", 600).status == "ok"
        out = a.execute("SELECT COUNT(*) FROM heavy x, heavy y, heavy z "
                        "WHERE (x.a + y.a + z.a) % 7 = 3", 5)
        assert out.status == "timeout", out
        a.cancel()
        a.lingering_queries()  # recorded in the report: whether the pushed query lingers
        assert a.execute("SELECT 1", 60).rows == [(1,)]
    finally:
        a.remove_data()
