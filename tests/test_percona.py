import os
from pathlib import Path

import pytest

from dbench.config import Limits
from dbench.systems.percona import PerconaEngine, container_command, container_env
from dbench.systems.registry import get_adapter

PLAN = Limits("0-15", "16-23", 64)
SMOKE = Limits("0-7", "8-11", 16)


class FakeCursor:
    """Answers SHOW GLOBAL STATUS LIKE 'Ducksdb_pushdown_count' from a script."""

    def __init__(self, counts):
        self._counts = list(counts)
        self.executed = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if "Ducksdb_pushdown_count" in sql:
            n = self._counts.pop(0)
            self._row = None if n is None else ("Ducksdb_pushdown_count", str(n))
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


def adapter_with(monkeypatch, counts):
    cur = FakeCursor(counts)
    a = PerconaEngine(None)
    monkeypatch.setattr(a, "_connect", lambda **kw: FakeConn(cur))
    return a, cur


def test_pushdown_detects_counter_delta(monkeypatch):
    a, cur = adapter_with(monkeypatch, [5, 6])
    assert a.pushdown("SELECT 1") == "duckdb"
    assert "SELECT 1" in cur.executed


def test_no_delta_is_fallback(monkeypatch):
    a, _ = adapter_with(monkeypatch, [5, 5])
    assert a.pushdown("SELECT 1") == "fallback"


def test_missing_counter_is_unknown(monkeypatch):
    a, _ = adapter_with(monkeypatch, [None, None])
    assert a.pushdown("SELECT 1") == "unknown"


def test_registry_and_contract():
    a = get_adapter("percona", None)
    assert isinstance(a, PerconaEngine)
    assert (a.name, a.track, a.family, a.dialect, a.config_dir) == (
        "percona", "A", "mysql", "mysql", "percona")
    assert a.engine_clause == "ENGINE=DuckDB"
    assert a.supports("M11") == (True, None)
    assert "@sha256:" in a.image and "9.7-duckdb-v0.2.3" in a.image


def test_env_follows_ruled_formulas():
    assert container_env(PLAN) == {"MYSQL_ROOT_PASSWORD": "", "DUCKSDB_MEMORY_LIMIT": "48GB"}
    assert container_env(SMOKE)["DUCKSDB_MEMORY_LIMIT"] == "12GB"


def test_args_follow_ruled_formulas():
    args = container_command(PLAN)
    for a in ("--innodb_buffer_pool_size=4G", "--innodb_parallel_read_threads=16",
              "--secure_file_priv=/data", "--local_infile=OFF", "--max_connections=200",
              "--join_buffer_size=256M", "--sort_buffer_size=256M", "--tmp_table_size=4G",
              "--max_heap_table_size=4G", "--temptable_max_ram=4G",
              "--innodb_redo_log_capacity=8G"):
        assert a in args, a
    assert all(a.startswith("--") for a in args)
    small = container_command(SMOKE)
    assert "--innodb_buffer_pool_size=1G" in small and "--tmp_table_size=1G" in small
    assert "--innodb_redo_log_capacity=2G" in small


def test_load_convert_alters_each_table_to_duckdb(monkeypatch):
    cur = FakeCursor([])
    a = PerconaEngine(None)
    monkeypatch.setattr(a, "_connect", lambda **kw: FakeConn(cur))
    monkeypatch.setattr(a, "_run_statements", lambda stmts: None)
    monkeypatch.setattr(a, "_load_parts", lambda cur, data: None)
    monkeypatch.setattr(a, "_count_rows", lambda cur: {"region": 5})
    out = a.load_convert(None)
    alters = [s for s in cur.executed if s.startswith("ALTER TABLE")]
    assert out.status == "ok" and len(alters) == 8
    assert all(s.endswith("ENGINE=DuckDB") for s in alters)


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_engine_starts_and_lists_duckdb():
    empty = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
    empty.mkdir(parents=True, exist_ok=True)
    a = PerconaEngine(empty)
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        conn = a.connect()
        try:
            cur = conn.cursor()
            cur.execute("SHOW ENGINES")
            engines = {r[0]: r[1] for r in cur.fetchall()}
            assert engines.get("DuckDB") in ("YES", "DEFAULT")
        finally:
            conn.close()
        assert a.versions()["duckdb"]
    finally:
        a.remove_data()
