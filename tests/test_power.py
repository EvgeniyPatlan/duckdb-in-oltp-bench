from datetime import UTC, datetime

from dbench.measures.power import run_power
from dbench.results import iter_records
from tests.fakes import FakeAdapter, crashed, ok, timeout

HOST = {"cpus": 4, "mem_gb": 8, "kernel": "k", "docker_version": "d"}


def power(adapter, tmp_path, queries, reference=None, drops=None):
    return run_power(
        adapter, 1, "run1", tmp_path, reference,
        queries=queries,
        drop_caches=(lambda: drops.append(1)) if drops is not None else (lambda: None),
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        host=lambda: HOST,
    )


def test_power_records_median_warm(tmp_path):
    drops: list[int] = []
    a = FakeAdapter([ok(9.0), ok(3.0), ok(1.0), ok(2.0)])
    (rec,) = power(a, tmp_path, [1], drops=drops)
    assert rec.status == "ok" and rec.measure == "M1" and rec.item == "q01"
    assert rec.value == 2.0 and rec.unit == "s"
    assert rec.extra["cold_s"] == 9.0 and rec.extra["warm_s"] == [3.0, 1.0, 2.0]
    assert rec.extra["pushed"] == "duckdb" and rec.extra["checksum"]
    assert rec.system == "fake" and rec.track == "A" and rec.config_ref == "systems/fake/"
    assert rec.host == HOST and rec.versions == {"fake": "1"}
    assert drops == [1]
    assert len(a.executed) == 4
    assert len(list(iter_records(tmp_path))) == 1


def test_cold_timeout_skips_warm(tmp_path):
    a = FakeAdapter([timeout()])
    (rec,) = power(a, tmp_path, [1])
    assert rec.status == "timeout" and rec.value is None
    assert len(a.executed) == 1 and a.cancels == 1


def test_timeout_then_server_usable(tmp_path):
    a = FakeAdapter([timeout()], alive_after=[True])
    recs = power(a, tmp_path, [1, 2])
    assert [r.status for r in recs] == ["timeout", "ok"]
    assert a.restarts == 0

    b = FakeAdapter([timeout()], alive_after=[False])
    recs = power(b, tmp_path / "b", [1, 2])
    assert [r.status for r in recs] == ["timeout", "ok"]
    assert b.restarts == 1


def test_crash_recorded_and_restarted(tmp_path):
    a = FakeAdapter([crashed()])
    recs = power(a, tmp_path, [1, 2, 3])
    assert [r.status for r in recs] == ["crashed", "ok", "ok"]
    assert recs[0].extra["container"]["OOMKilled"] is True
    assert a.restarts == 1
    assert len(a.executed) == 1 + 4 + 4


def test_warm_failure_marks_query_and_stops(tmp_path):
    a = FakeAdapter([ok(1.0), ok(1.0), timeout()])
    (rec,) = power(a, tmp_path, [1])
    assert rec.status == "timeout" and rec.value is None and rec.extra["warm_s"] == [1.0]
    assert len(a.executed) == 3


def test_wrong_result_marked(tmp_path):
    a = FakeAdapter([ok(rows=[(1,), (2,)])] * 4)
    (rec,) = power(a, tmp_path, [1], reference={1: [(1,), (3,)]})
    assert rec.status == "wrong_result"
    assert rec.value is not None
    assert rec.extra["diffs"] and rec.extra["row_count"] == [2, 2]


def test_matching_reference_is_ok(tmp_path):
    a = FakeAdapter([ok(rows=[(2,), (1,)])] * 4)
    (rec,) = power(a, tmp_path, [1], reference={1: [(1,), (2,)]})
    assert rec.status == "ok"
