import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems.innodb import InnoDB
from dbench.systems.postgres import Postgres

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1"),
]
LIMITS = Limits(db_cpuset="0-3", runner_cpuset="8-11", mem_gb=4)
SLOW = {
    "innodb": "SELECT COUNT(*) FROM information_schema.columns a, "
              "information_schema.columns b, information_schema.columns c",
    "postgres": "SELECT pg_sleep(60)",
}


@pytest.fixture(params=[InnoDB, Postgres], ids=["innodb", "postgres"])
def adapter(request):
    empty = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
    empty.mkdir(parents=True, exist_ok=True)  # host-visible, so the daemon can bind it
    a = request.param(empty)
    a.remove_data()
    a.start(LIMITS)
    try:
        yield a
    finally:
        a.remove_data()


def test_query_types_timeout_and_recovery(adapter):
    adapter.create_schema(1, "rowstore")
    adapter.execute("INSERT INTO region VALUES (1, 'EUROPE', 'x')", 30)
    adapter.execute("INSERT INTO orders (o_orderkey, o_totalprice, o_orderdate) "
                    "VALUES (1, 2.50, '1998-01-02')", 30)
    out = adapter.execute("SELECT r_regionkey, r_name FROM region", 30)
    assert out.status == "ok" and out.rows == [(1, "EUROPE")]
    assert out.numeric_cols == {0} and out.columns == ["r_regionkey", "r_name"]
    out = adapter.execute("SELECT o_totalprice, o_orderdate FROM orders", 30)
    assert out.rows == [(Decimal("2.50"), date(1998, 1, 2))] and out.numeric_cols == {0}
    assert adapter.execute("SELECT nope FROM region", 30).status == "error"
    assert adapter.versions()["image_digest"]
    assert adapter.storage_bytes() > 0
    slow = adapter.execute(SLOW[adapter.name], 2)
    assert slow.status == "timeout"
    assert adapter.cancel() and adapter.alive()
    assert adapter.execute("SELECT 1", 10).rows == [(1,)]


def test_killed_container_is_crashed_and_restart_keeps_data(adapter):
    adapter.create_schema(1, "rowstore")
    adapter.execute("INSERT INTO region VALUES (1, 'EUROPE', 'x')", 30)
    dockerctl._client().containers.get(adapter.container_name).kill()
    out = adapter.execute("SELECT 1", 10)
    assert out.status == "crashed"
    assert adapter.crash_info()["ExitCode"] == 137
    adapter.restart()
    assert adapter.execute("SELECT COUNT(*) FROM region", 30).rows == [(1,)]


def test_rendered_settings_took_effect(adapter):
    """LIMITS: 4 GB, 4 cores."""
    mib = 2**20
    conn = adapter.connect()
    try:
        if adapter.name == "innodb":
            cur = conn.cursor()
            got = {}
            for var in ("innodb_buffer_pool_size", "join_buffer_size", "sort_buffer_size",
                        "tmp_table_size", "max_heap_table_size", "temptable_max_ram",
                        "innodb_redo_log_capacity", "innodb_parallel_read_threads"):
                cur.execute(f"SELECT @@{var}")
                got[var] = int(cur.fetchone()[0])
            assert got == {
                "innodb_buffer_pool_size": 3072 * mib, "join_buffer_size": 256 * mib,
                "sort_buffer_size": 256 * mib, "tmp_table_size": 256 * mib,
                "max_heap_table_size": 256 * mib, "temptable_max_ram": 256 * mib,
                "innodb_redo_log_capacity": 512 * mib, "innodb_parallel_read_threads": 4,
            }
        else:
            got = {k: conn.execute(f"SHOW {k}").fetchone()[0]
                   for k in ("shared_buffers", "work_mem", "max_wal_size", "checkpoint_timeout",
                             "effective_cache_size", "max_parallel_workers_per_gather")}
            assert got == {"shared_buffers": "1GB", "work_mem": "256MB", "max_wal_size": "1GB",
                           "checkpoint_timeout": "30min", "effective_cache_size": "3GB",
                           "max_parallel_workers_per_gather": "4"}
    finally:
        conn.close()
