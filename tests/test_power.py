from datetime import UTC, datetime

import pytest

from dbench.hostinfo import suspended_s
from dbench.measures.power import HostSuspended, run_power
from dbench.results import iter_records
from dbench.systems.base import QueryOutcome
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
    (rec,) = power(a, tmp_path, [1], reference={1: {'rows': [(1,), (3,)], 'numeric_cols': {0}}})
    assert rec.status == "wrong_result"
    assert rec.value is not None
    assert rec.extra["diffs"] and rec.extra["row_count"] == [2, 2]


def test_matching_reference_is_ok(tmp_path):
    a = FakeAdapter([ok(rows=[(2,), (1,)])] * 4)
    (rec,) = power(a, tmp_path, [1], reference={1: {'rows': [(1,), (2,)], 'numeric_cols': set()}})
    assert rec.status == "ok"


def error() -> QueryOutcome:
    return QueryOutcome("error", None, None, set(), "ConnectionResetError", "n/a")


def test_error_with_dead_server_upgraded_to_crashed(tmp_path):
    a = FakeAdapter([error()], alive_after=[False])
    recs = power(a, tmp_path, [1, 2])
    assert [r.status for r in recs] == ["crashed", "ok"]
    assert recs[0].extra["container"]["ExitCode"] == 137
    assert a.restarts == 1


def test_error_with_running_container_stays_error(tmp_path):
    a = FakeAdapter([error()], alive_after=[False], crash_status="running")
    recs = power(a, tmp_path, [1, 2])
    assert [r.status for r in recs] == ["error", "ok"]
    assert a.restarts == 1


def test_error_with_live_server_stays_error_without_restart(tmp_path):
    a = FakeAdapter([error()], alive_after=[True])
    recs = power(a, tmp_path, [1])
    assert recs[0].status == "error" and a.restarts == 0


def test_restart_failure_does_not_abort(tmp_path):
    a = FakeAdapter([crashed()] * 22, restart_error=RuntimeError("cannot start"))
    recs = run_power(
        a, 1, "run1", tmp_path, None,
        drop_caches=lambda: None, host=lambda: HOST,
    )
    assert len(recs) == 22 and len(list(iter_records(tmp_path))) == 22
    assert all(r.status == "crashed" for r in recs)
    assert "restart_error" not in recs[0].extra
    assert "cannot start" in recs[1].extra["restart_error"]


def test_timeout_with_lingering_query_restarts_and_flags(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_WAIT_S", 0.05)
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    a = FakeAdapter([timeout()], lingering=[1] * 100)
    recs = power(a, tmp_path, [1, 2])
    assert recs[0].status == "timeout" and a.cancels == 1 and a.restarts == 1
    assert recs[0].extra["restarted_after_timeout"] is True
    assert recs[0].extra["lingering"] == 1
    assert "restarted_after_timeout" not in recs[1].extra


def test_timeout_whose_query_stops_does_not_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    a = FakeAdapter([timeout()], lingering=[2, 0])
    (rec,) = power(a, tmp_path, [1])
    assert rec.status == "timeout" and a.restarts == 0
    assert "restarted_after_timeout" not in rec.extra and "lingering" not in rec.extra


class SleepClock:
    """Seconds the host spent suspended, as ``hostinfo.suspended_s`` reports them; ``during``
    maps an execute() index to the seconds the host sleeps while that run is in flight."""

    def __init__(self, during: dict[int, float]):
        self.during, self.total = during, 0.0

    def __call__(self) -> float:
        return self.total


class SleepingAdapter(FakeAdapter):
    def __init__(self, outcomes, clock: SleepClock, **kw):
        super().__init__(outcomes, **kw)
        self.clock = clock

    def execute(self, sql, timeout_s):
        self.clock.total += self.clock.during.get(len(self.executed), 0.0)
        return super().execute(sql, timeout_s)


def sleeping_power(adapter, tmp_path, queries, clock):
    return run_power(
        adapter, 1, "run1", tmp_path, None, queries=queries, drop_caches=lambda: None,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC), host=lambda: HOST, suspended_s=clock)


STATEMENT_TIMEOUT = QueryOutcome(
    "timeout", 0.4, None, set(),
    "QueryCanceled: canceling statement due to statement timeout", "n/a")


def test_host_suspend_during_a_warm_run_is_measured_again_not_a_timeout(tmp_path):
    # The smoke-sf1 incident: q06 cold ok, then the host slept 24864 s during warm run 1 and
    # Postgres, whose statement_timeout deadline is on the wall clock, cancelled it on resume.
    clock = SleepClock({1: 24864.0})
    a = SleepingAdapter([ok(0.92), STATEMENT_TIMEOUT], clock)
    (rec,) = sleeping_power(a, tmp_path, [6], clock)
    assert rec.status == "ok" and rec.error is None
    assert rec.extra["host_suspended_s"] == [24864.0]
    assert len(rec.extra["warm_s"]) == 3
    assert len(a.executed) == 2 + 4  # the interrupted attempt, then a full cold + 3 warm
    assert a.cancels == 1  # the interrupted query was still cleaned up before the retry


def test_host_suspend_during_an_ok_run_is_measured_again(tmp_path):
    clock = SleepClock({0: 30.0})
    a = SleepingAdapter([], clock)
    (rec,) = sleeping_power(a, tmp_path, [1], clock)
    assert rec.status == "ok" and rec.extra["host_suspended_s"] == [30.0]
    assert len(a.executed) == 1 + 4


def test_no_suspend_leaves_records_unchanged(tmp_path):
    clock = SleepClock({})
    a = SleepingAdapter([ok(1.0), STATEMENT_TIMEOUT], clock)
    (rec,) = sleeping_power(a, tmp_path, [1], clock)
    assert rec.status == "timeout" and "host_suspended_s" not in rec.extra


def test_suspend_on_every_attempt_aborts_instead_of_recording(tmp_path):
    clock = SleepClock({i: 60.0 for i in range(100)})
    a = SleepingAdapter([], clock)
    with pytest.raises(HostSuspended, match="q01"):
        sleeping_power(a, tmp_path, [1, 2], clock)
    assert list(iter_records(tmp_path)) == []


def test_suspended_s_is_boottime_minus_monotonic():
    assert suspended_s() >= 0.0
