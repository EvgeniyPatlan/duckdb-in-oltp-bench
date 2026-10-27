import os
from pathlib import Path

import pytest

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems.percona import (
    DUCKDB_SOURCE,
    PerconaEngine,
    container_command,
    container_env,
)
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
    assert container_env(PLAN) == {"MYSQL_ROOT_PASSWORD": "", "DUCKSDB_MEMORY_LIMIT": "48GB",
                                  "DUCKSDB_TEMP_DIR": "/var/lib/mysql/duckdb_tmp"}
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
    analyzes = [i for i, s in enumerate(cur.executed) if s.startswith("ANALYZE TABLE")]
    last_alter = max(i for i, s in enumerate(cur.executed) if s.startswith("ALTER TABLE"))
    assert len(analyzes) == 8 and min(analyzes) > last_alter
    assert all(s.endswith("ENGINE=DuckDB") for s in alters)


def test_failed_spill_dir_stops_container(monkeypatch):
    a = PerconaEngine(None)
    stopped = []
    monkeypatch.setattr("dbench.systems._common.ContainerServerAdapter.start",
                        lambda self, limits: None)
    monkeypatch.setattr(a, "_make_spill_dir", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(a, "stop", lambda: stopped.append(1))
    with pytest.raises(RuntimeError):
        a.start(SMOKE)
    assert stopped == [1]


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
            cur = conn.cursor()
            # 8 GB limit: pool 8/16 G = 512M; the server adds a trailing slash
            cur.execute("SELECT @@innodb_buffer_pool_size, @@join_buffer_size, "
                        "@@secure_file_priv")
            assert cur.fetchone() == (512 << 20, 256 << 20, "/data/")
        finally:
            conn.close()
        v = a.versions()
        assert v["duckdb"] == "1.5.3"
        assert v["duckdb_source"] == DUCKDB_SOURCE
        code, _ = dockerctl._client().containers.get(a.container_name).exec_run(
            ["test", "-d", "/var/lib/mysql/duckdb_tmp"])
        assert code == 0
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_heavy_pushed_query_times_out_and_server_recovers():
    empty = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
    empty.mkdir(parents=True, exist_ok=True)
    a = PerconaEngine(empty)
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        a.execute("CREATE TABLE heavy (a INT NOT NULL, PRIMARY KEY (a)) ENGINE=DuckDB", 30)
        a.execute("CREATE TABLE seed (a INT NOT NULL, PRIMARY KEY (a)) ENGINE=InnoDB", 30)
        assert a.execute("INSERT INTO seed VALUES (1)", 30).status == "ok"
        for _ in range(21):  # doubling: 2^21 = 2M rows
            out = a.execute("INSERT INTO seed SELECT a + (SELECT MAX(a) FROM seed) FROM seed", 120)
            assert out.status == "ok", out
        assert a.execute("INSERT INTO heavy SELECT a FROM seed", 120).status == "ok"
        out = a.execute("SELECT COUNT(*) FROM heavy x, heavy y, heavy z "
                        "WHERE (x.a + y.a + z.a) % 7 = 3", 5)
        assert out.status == "timeout", out
        a.cancel()
        assert a.execute("SELECT 1", 10).rows == [(1,)]
    finally:
        a.remove_data()
