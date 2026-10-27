import os
from pathlib import Path

import pytest

from dbench.config import Limits
from dbench.systems.base import SystemAdapter
from dbench.systems.baseline_conf import duckdb_pg_settings
from dbench.systems.pgduckdb import IMAGE as PGDUCKDB_IMAGE
from dbench.systems.pgduckdb import PgDuckDB
from dbench.systems.pgduckdb import render_config as render_pgduckdb
from dbench.systems.pgducklake import (
    IMAGE as PGDUCKLAKE_IMAGE,
)
from dbench.systems.pgducklake import (
    PgDuckLake,
    duckdb_session_sql,
    insert_from_csv_sql,
)
from dbench.systems.pgducklake import (
    render_config as render_pgducklake,
)
from dbench.systems.postgres_family import is_cancelled_by_deadline, pushed_from_explain
from dbench.systems.registry import get_adapter

PLAN = Limits("0-15", "16-23", 64)
SMOKE = Limits("0-7", "8-11", 16)

# Real output of `EXPLAIN SELECT count(*), sum(a) FROM t WHERE a > 5` on pgduckdb 18-v1.1.1.
EXPLAIN_DUCKDB = [
    "Custom Scan (DuckDBScan)  (cost=0.00..0.00 rows=0 width=0)",
    "  DuckDB Execution Plan: ",
    " ",
    " ┌───────────────────────────┐",
    " │    UNGROUPED_AGGREGATE    │",
    " │  PGDUCKDB_POSTGRES_SCAN   │",
]
EXPLAIN_POSTGRES = [
    "Aggregate  (cost=1514.35..1514.36 rows=1 width=16)",
    "  ->  Seq Scan on t  (cost=0.00..1399.84 rows=22902 width=4)",
    "        Filter: (a > 5)",
]


# Real output of `EXPLAIN SELECT count(*) FROM l WHERE a > 1`, a ducklake table (pg_ducklake 1.0.2).
EXPLAIN_DUCKLAKE = [
    "Custom Scan (DuckLakeScan)  (cost=0.00..0.00 rows=0 width=0)",
    "  DuckDB Execution Plan: ",
    " ┌───────────────────────────┐",
    " │       DUCKLAKE_SCAN       │",
    " │          Table: l         │",
]


def settings(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("'")
    return out


def test_explain_duckdbscan_detected():
    assert pushed_from_explain(EXPLAIN_DUCKDB) == "duckdb"
    assert pushed_from_explain(EXPLAIN_DUCKLAKE) == "duckdb"
    assert pushed_from_explain(EXPLAIN_POSTGRES) == "fallback"
    assert pushed_from_explain([]) == "unknown"


def test_duckdb_executor_cancel_is_timeout_only_past_the_deadline():
    err = RuntimeError("(PGDuckDB/Duckdb_ExecCustomScan_Cpp) Executor Error: Query cancelled")
    assert is_cancelled_by_deadline(err, 5.02, 5)
    assert not is_cancelled_by_deadline(err, 1.0, 5)  # cancelled by someone else
    assert not is_cancelled_by_deadline(RuntimeError("syntax error"), 9.0, 5)


def test_pgduckdb_convert_is_na_with_reason():
    out = PgDuckDB(None).load_convert(None)
    assert out.status == "n/a"
    assert out.extra["reason"] == "queries normal Postgres tables directly; no conversion step"


def test_pgduckdb_contract_and_registry():
    a = get_adapter("pg_duckdb", None)
    assert isinstance(a, PgDuckDB) and isinstance(a, SystemAdapter)
    assert (a.name, a.track, a.family, a.dialect, a.config_dir, a.bench_schema_kind) == (
        "pg_duckdb", "A", "postgres", "postgres", "pgduckdb", "rowstore")
    assert a.supports("M11") == (False, "no separate DuckDB copy to keep fresh")
    assert a.supports("M12") == (True, None)
    assert PGDUCKDB_IMAGE == "pgduckdb/pgduckdb:18-v1.1.1"


def test_duckdb_pg_settings_at_plan_limits():
    pgd = duckdb_pg_settings(PLAN, "pgduckdb")
    assert pgd["shared_buffers"] == "16GB" and pgd["duck_memory"] == "32GB"
    assert pgd["duck_threads"] == 16
    lake = duckdb_pg_settings(PLAN, "pgducklake")
    assert lake["shared_buffers"] == "4GB" and lake["duck_memory"] == "48GB"
    assert lake["duck_threads"] == 16
    for s in (pgd, lake):  # the rest is the Postgres baseline
        assert s["effective_cache_size"] == "48GB" and s["workers"] == 16
        assert s["max_wal_size"] == "16GB"


def test_pgduckdb_config_at_plan_limits():
    got = settings(render_pgduckdb(PLAN))
    expected = {
        "shared_preload_libraries": "pg_duckdb", "shared_buffers": "16GB",
        "effective_cache_size": "48GB", "max_parallel_workers_per_gather": "16",
        "work_mem": "256MB", "max_connections": "200", "max_wal_size": "16GB",
        "checkpoint_timeout": "30min", "duckdb.max_memory": "32GB", "duckdb.threads": "16",
        "duckdb.temporary_directory": "/var/lib/postgresql/duckdb_tmp",
        "duckdb.max_workers_per_postgres_scan": "16", "duckdb.threads_for_postgres_scan": "16",
    }
    for key, value in expected.items():
        assert got[key] == value, key


def test_every_setting_has_a_reason_comment():
    lines = render_pgduckdb(PLAN).splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            assert lines[i - 1].startswith("#"), line


def test_query_hooks_explain_before_timing():
    class Conn:
        def __init__(self, lines):
            self.lines, self.sql = lines, []

        def execute(self, sql):
            self.sql.append(sql)
            lines = self.lines

            class Cur:
                def fetchall(self_):
                    return [(x,) for x in lines]
            return Cur()

    a = PgDuckDB(None)
    conn = Conn(EXPLAIN_DUCKDB)
    assert a._query_begin(conn, "SELECT 1") == "duckdb"
    assert conn.sql == ["EXPLAIN SELECT 1"]
    assert a._query_end(conn, "duckdb") == "duckdb"
    assert PgDuckDB(None)._query_begin(Conn(EXPLAIN_POSTGRES), "SELECT 1") == "fallback"


def test_force_execution_set_per_session():
    seen = []

    class Conn:
        def execute(self, sql):
            seen.append(sql)

    PgDuckDB(None)._session_setup(Conn())
    assert seen == ["SET duckdb.force_execution = true"]


def _empty_data() -> Path:
    empty = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
    empty.mkdir(parents=True, exist_ok=True)
    return empty


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_pgduckdb_live_settings_and_pushdown():
    a = PgDuckDB(_empty_data())
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        conn = a.connect()
        try:
            got = conn.execute(
                "SELECT current_setting('shared_buffers'), current_setting('duckdb.max_memory'), "
                "current_setting('duckdb.threads'), current_setting('duckdb.temporary_directory'), "
                "current_setting('duckdb.force_execution')").fetchone()
        finally:
            conn.close()
        # connect() applies the per-session setup, so force_execution reads "on"
        assert got == ("2GB", "4GB", "4", "/var/lib/postgresql/duckdb_tmp", "on")
        conn = a.connect()
        try:
            scan = conn.execute(
                "SELECT current_setting('duckdb.max_workers_per_postgres_scan'), "
                "current_setting('duckdb.threads_for_postgres_scan')").fetchone()
            # DuckDB's own view: 'MB' is read as MiB, so 4GB is exactly 4.0 GiB
            duck = conn.execute(
                "SELECT * FROM duckdb.query('SELECT current_setting(''memory_limit'') AS m, "
                "current_setting(''threads'') AS t')").fetchone()
        finally:
            conn.close()
        assert scan == ("4", "4") and duck == ("4.0 GiB", 4)
        v = a.versions()
        assert v["duckdb"] == "1.4.3" and v["postgres"].startswith("18")
        a.execute("CREATE TABLE t AS SELECT g AS a FROM generate_series(1, 1000) g", 30)
        assert a.pushdown("SELECT count(*) FROM t") == "duckdb"
        out = a.execute("SELECT count(*) FROM t", 30)
        assert out.rows == [(1000,)] and out.pushed == "duckdb"
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_pgduckdb_heavy_pushed_query_times_out_and_server_recovers():
    a = PgDuckDB(_empty_data())
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        a.execute("CREATE TABLE heavy AS SELECT g AS a FROM generate_series(1, 3000000) g", 120)
        out = a.execute("SELECT COUNT(*) FROM heavy x, heavy y, heavy z "
                        "WHERE (x.a + y.a + z.a) % 7 = 3", 5)
        assert out.status == "timeout", out
        a.cancel()
        assert out.error and "cancel" in out.error.lower()
        assert out.pushed == "duckdb"
        assert a.lingering_queries() == 0
        assert a.execute("SELECT 1", 10).rows == [(1,)]
    finally:
        a.remove_data()


def test_explain_ducklakescan_detected_by_the_adapter_hook():
    class Conn:
        def execute(self, sql):
            class Cur:
                def fetchall(self_):
                    return [(x,) for x in EXPLAIN_DUCKLAKE]
            return Cur()

    assert PgDuckLake(None)._query_begin(Conn(), "SELECT 1") == "duckdb"


def test_pgducklake_convert_loads_heap_then_inserts_and_is_not_na():
    a = PgDuckLake(None)
    assert a.supports("M11") == (True, None) and a.supports("M12") == (True, None)
    assert type(a).load_convert is not PgDuckDB.load_convert


def test_pgducklake_contract_and_registry():
    a = get_adapter("pg_ducklake", None)
    assert isinstance(a, PgDuckLake)
    assert (a.name, a.track, a.family, a.dialect, a.config_dir, a.bench_schema_kind) == (
        "pg_ducklake", "A", "postgres", "postgres", "pgducklake", "engine")
    assert a.engine_clause == "USING ducklake"
    assert PGDUCKLAKE_IMAGE == "dbench/pgducklake:18-v1.0.3"


def test_pgducklake_config_at_plan_limits():
    got = settings(render_pgducklake(PLAN))
    expected = {
        "shared_preload_libraries": "pg_ducklake", "shared_buffers": "4GB",
        "effective_cache_size": "48GB", "max_parallel_workers_per_gather": "16",
        "work_mem": "256MB", "max_connections": "200", "max_wal_size": "16GB",
        "checkpoint_timeout": "30min",
    }
    for key, value in expected.items():
        assert got[key] == value, key
    assert not any(k.startswith("duckdb.") for k in got)  # pg_ducklake has no such GUCs
    lines = render_pgducklake(PLAN).splitlines()
    for i, line in enumerate(lines):
        if line.strip() and not line.startswith("#"):
            assert lines[i - 1].startswith("#"), line


def test_pgducklake_duckdb_session_settings_at_plan_and_smoke_limits():
    assert duckdb_session_sql(PLAN) == (
        "SET threads = 16; SET memory_limit = '49152MiB'; "
        "SET temp_directory = '/var/lib/postgresql/duckdb_tmp'")
    assert "threads = 8; SET memory_limit = '12288MiB'" in duckdb_session_sql(SMOKE)
    assert duckdb_pg_settings(SMOKE, "pgducklake")["shared_buffers"] == "1GB"
    assert duckdb_pg_settings(SMOKE, "pgduckdb")["duck_memory"] == "8GB"


def test_insert_from_csv_casts_by_position_from_text():
    sql = insert_from_csv_sql("nation", "/data/n.csv", ["integer", "character(25)"])
    assert sql.startswith("INSERT INTO nation SELECT r['column0']::integer, "
                          "r['column1']::character(25) FROM ducklake.read_csv('/data/n.csv'")
    assert "header := false" in sql and "all_varchar := true" in sql and "delim := '|'" in sql


def test_insert_from_csv_zero_pads_column_names_from_ten_columns():
    sql = insert_from_csv_sql("lineitem", "/data/l.csv", ["integer"] * 16)
    assert "r['column00']::integer" in sql and "r['column15']::integer" in sql


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_pgducklake_live_settings_and_pushdown():
    a = PgDuckLake(_empty_data())
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        conn = a.connect()  # applies the per-session DuckDB settings
        try:
            pg = conn.execute("SELECT current_setting('shared_buffers'), "
                              "current_setting('shared_preload_libraries')").fetchone()
            duck = conn.execute(
                "SELECT * FROM ducklake.query('SELECT current_setting(''threads'') AS t, "
                "current_setting(''memory_limit'') AS m, "
                "current_setting(''temp_directory'') AS d')").fetchone()
        finally:
            conn.close()
        assert pg == ("512MB", "pg_ducklake")
        assert duck == (4, "6.0 GiB", "/var/lib/postgresql/duckdb_tmp")
        v = a.versions()
        assert v["extension"].startswith("pg_ducklake ") and v["duckdb"] != "unknown"
        a.create_schema(1, "engine")
        assert a.pushdown("SELECT count(*) FROM region") == "duckdb"
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_pgducklake_heavy_pushed_query_times_out_and_server_recovers():
    a = PgDuckLake(_empty_data())
    a.remove_data()
    a.start(Limits("0-3", "8-11", 8))
    try:
        a.execute("CREATE TABLE heavy (a BIGINT) USING ducklake", 30)
        load = a.execute("INSERT INTO heavy SELECT g FROM generate_series(1, 3000000) g", 120)
        assert load.status == "ok", load
        out = a.execute("SELECT COUNT(*) FROM heavy x, heavy y, heavy z "
                        "WHERE (x.a + y.a + z.a) % 7 = 3", 5)
        assert out.status == "timeout", out
        a.cancel()
        assert out.error and "cancel" in out.error.lower()
        assert out.pushed == "duckdb"
        assert a.lingering_queries() == 0
        assert a.execute("SELECT 1", 10).rows == [(1,)]
    finally:
        a.remove_data()


class SessionConn:
    """Fake psycopg connection that records statements and fails on 'BOOM*' / 'GONE*'."""

    opened = 0

    def __init__(self, log):
        type(self).opened += 1
        self.log, self.closed = log, False
        self.info = type("Info", (), {"backend_pid": 100 + type(self).opened})()

    def execute(self, sql, params=None):
        from psycopg import OperationalError
        from psycopg import errors as pgerrors
        self.log.append(sql)
        if sql.startswith("BOOM"):
            raise pgerrors.QueryCanceled("canceling statement due to statement timeout")
        if sql.startswith("GONE"):
            raise OperationalError("server closed the connection")
        if sql.startswith("BAD"):
            raise pgerrors.UndefinedTable("relation does not exist")
        cur = type("Cur", (), {})()
        cur.description = [type("D", (), {"type_code": 23, "name": "n"})()]
        cur.fetchall = lambda: [(1,)] if not sql.startswith("EXPLAIN") else [(x,) for x in
                                                                          EXPLAIN_DUCKDB]
        return cur

    def close(self):
        self.closed = True


def session_adapter(monkeypatch):
    SessionConn.opened = 0
    log: list[str] = []
    conns: list[SessionConn] = []

    def connect(*a, **kw):
        c = SessionConn(log)
        conns.append(c)
        return c

    a = PgDuckDB(None)
    monkeypatch.setattr(a, "_connect", connect)
    monkeypatch.setattr(a, "crash_info", lambda: {"status": "running"})
    return a, log, conns


def test_consecutive_executes_reuse_one_session_and_set_it_up_once(monkeypatch):
    a, log, conns = session_adapter(monkeypatch)
    for _ in range(3):
        out = a.execute("SELECT 1", 5)
        assert out.status == "ok" and out.pushed == "duckdb"
    assert len(conns) == 1
    assert log.count("SET duckdb.force_execution = true") == 1
    assert a.pushdown("SELECT 1") == "duckdb" and len(conns) == 1  # pushdown shares the session


def test_timeout_error_and_lost_connection_force_a_reconnect(monkeypatch):
    a, log, conns = session_adapter(monkeypatch)
    assert a.execute("SELECT 1", 5).status == "ok"
    out = a.execute("BOOM", 5)
    assert out.status == "timeout" and out.pushed == "duckdb"  # the EXPLAIN token is kept
    assert conns[0].closed
    assert a.execute("SELECT 1", 5).status == "ok" and len(conns) == 2
    out = a.execute("BAD", 5)
    assert out.status == "error" and out.pushed == "duckdb" and conns[1].closed
    assert a.execute("SELECT 1", 5).status == "ok" and len(conns) == 3
    assert a.execute("GONE", 5).status in ("error", "crashed") and conns[2].closed
    assert a.execute("SELECT 1", 5).status == "ok" and len(conns) == 4


def test_restart_and_stop_drop_the_session(monkeypatch):
    a, _, conns = session_adapter(monkeypatch)
    a.execute("SELECT 1", 5)
    monkeypatch.setattr("dbench.systems._common.dockerctl.stop", lambda name: None)
    a.stop()
    assert conns[0].closed
    a.execute("SELECT 1", 5)
    assert len(conns) == 2


def test_versions_does_not_raise_when_the_server_is_unreachable(monkeypatch):
    a = PgDuckDB(None)

    def boom(*args, **kw):
        raise OSError("refused")

    monkeypatch.setattr(a, "_connect", boom)
    v = a.versions()
    assert v["postgres"] == "unknown" and v["duckdb"] == "unknown"


def test_pgducklake_convert_vacuums_staging_before_inserting(monkeypatch):
    from types import SimpleNamespace

    from dbench.schema import TABLES
    from dbench.systems import pgducklake

    log: list[str] = []

    class Conn:
        def execute(self, sql, params=None):
            log.append(sql)
            return type("Cur", (), {"fetchone": lambda s: (5,), "fetchall": lambda s: []})()

    a = PgDuckLake(None)
    a._limits = SMOKE
    monkeypatch.setattr(pgducklake, "expected_rows", lambda d: {t: 5 for t in TABLES})
    out = a._convert(Conn(), SimpleNamespace(tables={t: [] for t in TABLES}), 0.0)
    assert out.status == "ok" and "rowstore_load_s" in out.extra
    first_insert = next(i for i, s in enumerate(log) if s.startswith("INSERT INTO public."))
    for t in TABLES:
        vac = log.index(f"VACUUM (FREEZE, ANALYZE) {pgducklake.STAGING_SCHEMA}.{t}")
        assert vac < first_insert


def test_pgducklake_m11_has_a_freshness_story():
    assert PgDuckLake(None).supports("M11") == (True, None)
