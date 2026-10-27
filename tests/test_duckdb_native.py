import os
from datetime import date, datetime
from decimal import Decimal

import pytest

from dbench.config import Limits
from dbench.systems.duckdb_native import DuckDBNative, decode


def test_decode_roundtrip_types():
    assert decode({"$dec": "1.50"}) == Decimal("1.50")
    assert decode({"$date": "1998-01-02"}) == date(1998, 1, 2)
    assert decode({"$ts": "1998-01-02T03:04:05"}) == datetime(1998, 1, 2, 3, 4, 5)
    assert decode({"$hex": "00ff"}) == b"\x00\xff"
    assert decode(5) == 5 and decode(None) is None


def test_contract_attributes_and_support():
    a = DuckDBNative("1.5.5")
    assert (a.name, a.track, a.dialect, a.family, a.config_dir) == (
        "duckdb-1.5.5", "baseline", "duckdb", "duckdb", "duckdb")
    assert a.supports("M1") == (True, None)
    for m in ("M11", "M12"):
        ok, reason = a.supports(m)
        assert not ok and "no row store" in reason
    assert a.pushdown("select 1") == "duckdb"
    out = a.load_convert(None)
    assert out.status == "n/a" and out.elapsed_s is None and out.extra["reason"]
    with pytest.raises(ValueError):
        a.create_schema(1, "rowstore")


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_container_query_and_timeout():
    a = DuckDBNative("1.5.5")
    a.remove_data()
    a.start(Limits(db_cpuset="0-3", runner_cpuset="8-11", mem_gb=4))
    try:
        assert a.alive()
        assert a.versions()["duckdb"].lstrip("v") == "1.5.5"
        ddl = "CREATE TABLE t(a INTEGER, b DECIMAL(10,2), c DATE, d VARCHAR)"
        assert a.execute(ddl, 30).status == "ok"
        ins = "INSERT INTO t VALUES (1, 2.50, DATE '2020-01-02', 'x')"
        assert a.execute(ins, 30).status == "ok"
        out = a.execute("SELECT a, b, c, d FROM t", 30)
        assert out.status == "ok" and out.rows == [(1, Decimal("2.50"), date(2020, 1, 2), "x")]
        assert out.numeric_cols == {0, 1} and out.elapsed_s > 0
        assert a.storage_bytes() > 0
        slow = a.execute("SELECT sum(hash(i)) FROM range(10000000000) t(i)", 2)
        assert slow.status == "timeout"
        assert a.alive()
        assert a.execute("SELECT 1", 10).rows == [(1,)]
        assert a.restart() is None and a.alive()
        assert a.execute("SELECT count(*) FROM t", 30).rows == [(1,)]
    finally:
        a.remove_data()


def test_bulk_load_copies_every_part_in_table_order(tmp_path):
    from types import SimpleNamespace

    from dbench.systems.duckdb_native import TPCH_DDL

    tables = {t: [tmp_path / f"{t}.1", tmp_path / f"{t}.2"] for t in TPCH_DDL}
    a = DuckDBNative("1.5.5", data_dir=tmp_path)
    sent = []
    a._run = lambda sql, timeout_s=600: sent.append(sql) or {"status": "ok", "elapsed_s": 1.0}
    out = a.load_bulk(SimpleNamespace(tables=tables))
    assert out.status == "ok" and out.elapsed_s == 16.0
    assert len(sent) == 16
    assert sent[0] == "COPY region FROM '/data/region.1' (DELIMITER '|', HEADER false)"
    assert sent[-1].startswith("COPY lineitem FROM '/data/lineitem.2'")


def test_bulk_load_stops_on_error(tmp_path):
    from types import SimpleNamespace

    from dbench.systems.duckdb_native import TPCH_DDL

    tables = {t: [tmp_path / f"{t}.1"] for t in TPCH_DDL}
    a = DuckDBNative("1.5.5", data_dir=tmp_path)
    a._run = lambda sql, timeout_s=600: {"status": "error", "elapsed_s": 0.5, "error": "boom"}
    out = a.load_bulk(SimpleNamespace(tables=tables))
    assert out.status == "error" and "region: boom" in out.error


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_create_schema_makes_all_tpch_tables():
    a = DuckDBNative("1.5.5")
    a.remove_data()
    a.start(Limits(db_cpuset="0-3", runner_cpuset="8-11", mem_gb=4))
    try:
        a.create_schema(1, "engine")
        out = a.execute("SELECT count(*) FROM information_schema.tables", 30)
        assert out.rows == [(8,)]
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
@pytest.mark.parametrize("version", ["1.4.3", "1.5.2-dev736"])
def test_other_bundled_versions_answer(version):
    a = DuckDBNative(version)
    a.remove_data()
    try:
        a.start(Limits(db_cpuset="0-3", runner_cpuset="8-11", mem_gb=4))
        assert a.versions()["duckdb"].lstrip("v") == version
        assert a.execute("SELECT 1", 10).rows == [(1,)]
    finally:
        a.remove_data()


def test_connection_failure_waits_for_docker_to_report_the_exit(monkeypatch):
    from dbench import dockerctl

    states = iter([{"status": "running"}, {"status": "running"},
                   {"status": "exited", "ExitCode": 137, "OOMKilled": True}])
    monkeypatch.setattr(dockerctl, "container_state", lambda name: next(states))
    monkeypatch.setattr("time.sleep", lambda s: None)
    out = DuckDBNative("1.5.5")._connection_failure(ConnectionResetError("reset"))
    assert out.status == "crashed"


def test_connection_failure_with_running_container_is_error(monkeypatch):
    from dbench import dockerctl

    monkeypatch.setattr(dockerctl, "container_state", lambda name: {"status": "running"})
    monkeypatch.setattr("time.sleep", lambda s: None)
    out = DuckDBNative("1.5.5")._connection_failure(ConnectionResetError("reset"))
    assert out.status == "error"


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_killed_container_is_crashed_with_exit_status():
    import threading
    import time as _time

    from dbench import dockerctl

    a = DuckDBNative("1.5.5")
    a.remove_data()
    a.start(Limits(db_cpuset="0-3", runner_cpuset="8-11", mem_gb=4))
    try:
        def kill():
            _time.sleep(2)
            dockerctl._client().containers.get(a.container_name).kill()

        t = threading.Thread(target=kill)
        t.start()
        out = a.execute("SELECT sum(hash(i)) FROM range(10000000000) t(i)", 60)
        t.join()
        assert out.status == "crashed"
        info = a.crash_info()
        assert info["ExitCode"] == 137 and info["status"] != "running"
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_cancel_after_timeout_spares_other_streams():
    import threading

    a = DuckDBNative("1.5.5")
    a.remove_data()
    a.start(Limits(db_cpuset="0-3", runner_cpuset="8-11", mem_gb=4))
    try:
        probe = "SELECT sum(hash(i)) FROM range({}) t(i)"
        n = 200_000_000
        t0 = a.execute(probe.format(n), 60).elapsed_s
        n6 = int(n * 6 / t0)  # sized to run about 6 s alone
        results = {}

        def other():
            results["other"] = a.execute(probe.format(n6), 60)

        th = threading.Thread(target=other)
        th.start()
        slow = a.execute("SELECT sum(hash(i)) FROM range(10000000000) t(i)", 2)
        assert slow.status == "timeout"
        assert a.cancel() is True
        th.join()
        assert results["other"].status == "ok", results["other"].error
    finally:
        a.remove_data()
