import os
from pathlib import Path
from datetime import date
from decimal import Decimal

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
