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
        print("timeout error:", out.error, "elapsed", out.elapsed_s, "lingering",
              a.lingering_queries())
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
        print("timeout error:", out.error, "elapsed", out.elapsed_s, "lingering",
              a.lingering_queries())
        assert a.execute("SELECT 1", 10).rows == [(1,)]
    finally:
        a.remove_data()
