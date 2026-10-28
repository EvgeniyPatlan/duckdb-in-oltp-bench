"""M4 throughput: stream order, qph math, error accounting, contamination, suspend, campaign."""
from __future__ import annotations

import dataclasses
import json
import threading
from datetime import UTC, datetime

import pytest

from dbench.campaign import load_campaign, run_campaign
from dbench.config import STREAMS, Limits
from dbench.measures import registry as measures
from dbench.measures.power import HostSuspended
from dbench.measures.throughput import (
    queries_per_hour,
    run_throughput,
    stream_order,
)
from dbench.results import iter_records
from dbench.systems.base import QueryOutcome, SystemAdapter
from dbench.systems.registry import get_adapter
from tests.fakes import FakeAdapter, timeout
from tests.test_campaign import Env, records, write_yaml

HOST = {"cpus": 4, "mem_gb": 8, "kernel": "k", "docker_version": "d"}
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def qnum(sql: str) -> int:
    return int(sql.removeprefix("Q"))


class StreamFake(FakeAdapter):
    """Thread-safe fake: ``script[n]`` is the outcome of query ``n`` (ok(1.0) by default);
    ``log`` records every call in order."""

    def __init__(self, script=None, scope="session", pushed="duckdb", **kw):
        super().__init__([], **kw)
        self.script = script or {}
        self.pushdown_counter_scope = scope
        self.pushed = pushed
        self.log: list[tuple] = []
        self._lock = threading.Lock()

    def execute(self, sql, timeout_s):
        with self._lock:
            self.log.append(("execute", qnum(sql)))
        out = self.script.get(qnum(sql))
        if isinstance(out, Exception):
            raise out
        return out or QueryOutcome("ok", 1.0, [(1,)], set(), None, self.pushed)

    def cancel(self):
        with self._lock:
            self.log.append(("cancel",))
        return True

    def restart(self):
        with self._lock:
            self.log.append(("restart",))
        super().restart()

    def prepare_streams(self, n):
        with self._lock:
            self.log.append(("prepare", n))


def throughput(adapter, tmp_path, streams=2, timer=None, **kw):
    ticks = iter([0.0, 100.0]) if timer is None else timer
    return run_throughput(
        adapter, 1, streams, "run1", tmp_path, clock=lambda: NOW, host=lambda: HOST,
        query_text=lambda n: f"Q{n}", timer=lambda: next(ticks), **kw)


# ---- order and arithmetic ----------------------------------------------------------------

def test_stream_order_deterministic_and_permutation():
    assert stream_order(0) == list(range(1, 23))
    for s in range(1, 9):
        order = stream_order(s)
        assert sorted(order) == list(range(1, 23))
        assert order == stream_order(s)
    assert stream_order(1) != stream_order(2) != stream_order(0)
    assert stream_order(1) != stream_order(1, seed=1)


def test_qph_math(tmp_path):
    assert queries_per_hour(88, 3600.0) == 88.0
    assert queries_per_hour(22, 100.0) == 792.0
    assert queries_per_hour(0, 100.0) == 0.0
    rec = throughput(StreamFake(), tmp_path, streams=4, timer=iter([0.0, 100.0]))
    assert rec.measure == "M4" and rec.item == "streams=4" and rec.unit == "qph"
    assert rec.value == 22 * 4 * 3600 / 100.0
    assert rec.extra["elapsed_s"] == 100.0 and rec.extra["total_queries"] == 88
    assert rec.extra["counts_by_status"] == {"ok": 88}
    assert len(rec.extra["per_query_latency"]) == 4
    assert all(len(s) == 22 for s in rec.extra["per_query_latency"])
    assert rec.extra["p50"] == 1.0 and rec.extra["p95"] == 1.0
    assert [r[0] for r in rec.extra["per_query_latency"][0]] == [
        f"q{n:02d}" for n in stream_order(1)]
    assert rec.config_ref == "systems/fake/" and rec.host == HOST


def test_only_ok_queries_count_in_the_numerator(tmp_path):
    a = StreamFake({3: QueryOutcome("error", 0.1, None, set(), "boom", "n/a")})
    rec = throughput(a, tmp_path, streams=2)
    assert rec.value == 42 * 3600 / 100.0 and rec.extra["total_queries"] == 44


# ---- errors are counted, never raised ------------------------------------------------------

def test_errors_counted_not_raised(tmp_path):
    a = StreamFake({
        1: QueryOutcome("error", 0.1, None, set(), "syntax", "n/a"),
        2: RuntimeError("driver blew up"),
        3: timeout(),
        4: QueryOutcome("crashed", None, None, set(), "gone", "n/a"),
    })
    rec = throughput(a, tmp_path, streams=2)
    assert rec.status == "ok"
    assert rec.extra["counts_by_status"] == {"ok": 36, "error": 4, "timeout": 2, "crashed": 2}
    lat = {q: (s, st) for q, s, st in rec.extra["per_query_latency"][0]}
    assert lat["q02"][1] == "error" and lat["q03"] == (None, "timeout")
    assert len(list(iter_records(tmp_path))) == 1
    assert a.log.count(("restart",)) == 1  # the crash restarts after the run


def test_all_queries_failing_records_an_error_without_a_value(tmp_path):
    a = StreamFake({n: QueryOutcome("error", 0.1, None, set(), "x", "n/a") for n in range(1, 23)})
    rec = throughput(a, tmp_path, streams=1)
    assert rec.status == "error" and rec.value is None and rec.error


# ---- timeouts, contamination -----------------------------------------------------------------

def test_lingering_after_timeout_marks_contaminated_and_restarts_after_the_run(
        tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_WAIT_S", 0.05)
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    a = StreamFake({5: timeout()}, lingering=[1] * 100)
    rec = throughput(a, tmp_path, streams=2)
    assert rec.extra["contaminated"] is True and rec.extra["lingering"] == 1
    assert rec.extra["counts_by_status"]["timeout"] == 2
    assert a.log.count(("restart",)) == 1
    last_execute = max(i for i, e in enumerate(a.log) if e[0] == "execute")
    assert a.log.index(("restart",)) > last_execute  # never mid-run
    assert ("cancel",) in a.log


def test_timeout_that_stops_is_not_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    a = StreamFake({5: timeout()}, lingering=[2, 0])
    rec = throughput(a, tmp_path, streams=2)
    assert rec.extra["contaminated"] is False and "restart" not in [e[0] for e in a.log]


def test_restart_failure_is_attached_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_WAIT_S", 0.02)
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    a = StreamFake({5: timeout()}, lingering=[1] * 100, restart_error=RuntimeError("no start"))
    rec = throughput(a, tmp_path, streams=1)
    assert "no start" in rec.extra["restart_error"]
    (stored,) = list(iter_records(tmp_path))
    assert "no start" in stored.extra["restart_error"]


# ---- warm-up, prepare_streams ----------------------------------------------------------------

def test_warmup_runs_once_unrecorded_then_streams_are_prepared_and_reset(tmp_path):
    a = StreamFake()
    rec = throughput(a, tmp_path, streams=4)
    assert [e for e in a.log[:22]] == [("execute", n) for n in range(1, 23)]
    assert a.log[22] == ("prepare", 4)
    assert all(e[0] == "execute" for e in a.log[23:-1]) and len(a.log) == 22 + 1 + 88 + 1
    assert a.log[-1] == ("prepare", 1)
    assert rec.extra["total_queries"] == 88


def test_prepare_streams_is_reset_even_when_the_host_slept_through_every_attempt(tmp_path):
    a = StreamFake()
    with pytest.raises(HostSuspended):
        throughput(a, tmp_path, streams=2, suspended_s=Ticking(step=60.0),
                   timer=iter(range(100)))
    assert a.log[-1] == ("prepare", 1)
    assert list(iter_records(tmp_path)) == []


# ---- suspend ---------------------------------------------------------------------------------

class Ticking:
    """suspended_s() that advances ``step`` per call from the ``first``-th call on."""

    def __init__(self, step: float, first: int = 0):
        self.step, self.first, self.calls, self.total = step, first, 0, 0.0

    def __call__(self) -> float:
        if self.calls >= self.first:
            self.total += self.step
        self.calls += 1
        return self.total


def test_suspend_discards_the_run_and_repeats_it(tmp_path):
    # calls: before/after attempt 1 (slept 60 s), before/after attempt 2 (no change)
    readings = iter([0.0, 60.0, 60.0, 60.0])
    a = StreamFake()
    rec = throughput(a, tmp_path, streams=2, suspended_s=lambda: next(readings),
                     timer=iter([0.0, 5.0, 10.0, 100.0]))
    assert rec.extra["host_suspended_s"] == [60.0]
    assert rec.extra["elapsed_s"] == 90.0  # the second attempt's own time
    assert len(rec.extra["discarded_attempts"]) == 1
    executes = [e for e in a.log if e[0] == "execute"]
    assert len(executes) == 22 + 44 + 44
    assert len(list(iter_records(tmp_path))) == 1


def test_suspend_on_every_attempt_raises_host_suspended_and_writes_nothing(tmp_path):
    a = StreamFake()
    with pytest.raises(HostSuspended, match="streams=2"):
        throughput(a, tmp_path, streams=2, suspended_s=Ticking(60.0), timer=iter(range(100)))
    assert len([e for e in a.log if e[0] == "execute"]) == 22 + 3 * 44
    assert list(iter_records(tmp_path)) == []


# ---- pushdown under concurrency ------------------------------------------------------------

def test_pushed_is_unknown_for_a_global_counter_at_more_than_one_stream(tmp_path):
    rec = throughput(StreamFake(scope="global"), tmp_path, streams=2)
    assert rec.extra["pushed_counts"] == [{"unknown": 22}, {"unknown": 22}]
    assert rec.extra["pushdown_counter_scope"] == "global"


def test_pushed_is_kept_for_a_global_counter_at_one_stream(tmp_path):
    rec = throughput(StreamFake(scope="global"), tmp_path, streams=1)
    assert rec.extra["pushed_counts"] == [{"duckdb": 22}]


@pytest.mark.parametrize("scope", ["session", "explain", "none"])
def test_pushed_is_kept_for_per_session_counters(tmp_path, scope):
    rec = throughput(StreamFake(scope=scope, pushed="fallback"), tmp_path, streams=2)
    assert rec.extra["pushed_counts"] == [{"fallback": 22}, {"fallback": 22}]


EXPECTED_SCOPE = {
    "alisql": "session", "mariadb": "session", "percona": "global", "pg_duckdb": "explain",
    "pg_ducklake": "explain", "innodb": "none", "postgres": "none", "villagesql": "none",
    "duckdb-1.5.5": "none", "mariadb-trackb": "none", "pg_duckdb-trackb": "none",
}


@pytest.mark.parametrize("name,scope", sorted(EXPECTED_SCOPE.items()))
def test_every_adapter_declares_its_pushdown_counter_scope(name, scope):
    assert get_adapter(name, None).pushdown_counter_scope == scope


def test_base_adapter_default_is_none_and_prepare_streams_is_a_no_op():
    assert SystemAdapter.pushdown_counter_scope == "none"
    assert FakeAdapter([]).prepare_streams(8) is None


# ---- memory per backend (PG engines) ----------------------------------------------------------

SMOKE = Limits("0-7", "8-11", 16)


class SqlConn:
    def __init__(self):
        self.seen = []

    def execute(self, sql, params=None):
        self.seen.append((sql, params))


def test_pgducklake_divides_session_memory_by_the_stream_count():
    from dbench.systems.pgducklake import PgDuckLake, duckdb_session_sql

    assert "memory_limit = '12288MiB'" in duckdb_session_sql(SMOKE)
    assert "memory_limit = '3072MiB'" in duckdb_session_sql(SMOKE, streams=4)
    a = PgDuckLake(None)
    a._limits = SMOKE
    a.prepare_streams(4)
    conn = SqlConn()
    a._session_setup(conn)
    assert "3072MiB" in conn.seen[0][1][0] and a.memory_per_backend() == "3072MiB"
    a.prepare_streams(1)
    conn = SqlConn()
    a._session_setup(conn)
    assert "12288MiB" in conn.seen[0][1][0] and a.memory_per_backend() == "12288MiB"


def test_pgduckdb_sets_max_memory_per_session_only_under_concurrency():
    from dbench.systems.pgduckdb import PgDuckDB

    a = PgDuckDB(None)
    a._limits = SMOKE
    conn = SqlConn()
    a._session_setup(conn)
    assert [s for s, _ in conn.seen] == ["SET duckdb.force_execution = true"]
    a.prepare_streams(4)  # 0.5 * 16 GB / 4 = 2 GB
    conn = SqlConn()
    a._session_setup(conn)
    assert [s for s, _ in conn.seen] == [
        "SET duckdb.force_execution = true", "SET duckdb.max_memory = '2048MB'"]
    assert a.memory_per_backend() == "2048MB"
    a.prepare_streams(1)
    assert a.memory_per_backend() == "8192MB"


def test_prepare_streams_makes_open_postgres_sessions_stale():
    from dbench.systems.pgduckdb import PgDuckDB

    a = PgDuckDB(None)
    before = a._generation
    a.prepare_streams(8)
    assert a._generation == before + 1


def test_memory_per_backend_is_recorded(tmp_path):
    class Mem(StreamFake):
        def memory_per_backend(self):
            return "2048MB"

    rec = throughput(Mem(), tmp_path, streams=4)
    assert rec.extra["memory_per_backend"] == "2048MB"
    assert "memory_per_backend" not in throughput(StreamFake(), tmp_path / "b").extra


# ---- registry and campaign --------------------------------------------------------------------

def test_m4_is_registered():
    m = measures.get("M4")
    assert m.unit == "qph" and m.phase == "query"
    assert m.items(1) == [f"streams={n}" for n in STREAMS]


def test_campaign_streams_key_defaults_and_validates(tmp_path):
    assert load_campaign(write_yaml(tmp_path, measures=["M1", "M4"])).streams == STREAMS
    c = load_campaign(write_yaml(tmp_path, measures=["M4"], streams=[1, 4]))
    assert c.streams == (1, 4)
    assert [i for _, m, i, _ in c.planned("alisql") if m == "M4"] == ["streams=1", "streams=4"]
    from dbench.campaign import CampaignError
    for bad in ([], [0], [1, 1], ["a"], "4"):
        with pytest.raises(CampaignError, match="streams"):
            load_campaign(write_yaml(tmp_path, measures=["M4"], streams=bad))


def test_campaign_files_include_m4():
    from pathlib import Path

    import yaml
    root = Path(__file__).resolve().parents[1] / "campaigns"
    smoke = yaml.safe_load((root / "smoke-sf1.yaml").read_text())
    gate = yaml.safe_load((root / "gate-sf10.yaml").read_text())
    assert "M4" in smoke["measures"] and smoke["streams"] == [1, 4]
    assert "M4" in gate["measures"] and "streams" not in gate
    assert load_campaign(root / "smoke-sf1.yaml").streams == (1, 4)
    assert load_campaign(root / "gate-sf10.yaml").streams == STREAMS


def test_campaign_runs_m4_after_m1_and_before_convert(tmp_path, monkeypatch):
    order: list[str] = []
    for mid in ("M1", "M2", "M3", "M4"):
        m = measures.get(mid)
        monkeypatch.setitem(
            measures._MEASURES, mid,
            dataclasses.replace(m, run=lambda ad, ctx, _r=m.run, _id=mid: (
                order.append(_id), _r(ad, ctx))[1]))
    env = Env(tmp_path)
    run_campaign(write_yaml(tmp_path, systems=["innodb", "postgres", "alisql"],
                            measures=["M4", "M2", "M1", "M3"], streams=[1, 4]),
                 tmp_path / "res", env.deps())
    per_system = len(order) // 3
    assert order[:per_system] == ["M3", "M1", "M4", "M2"]
    items = sorted(r.item for r in records(tmp_path / "res", "alisql") if r.measure == "M4")
    assert items == ["streams=1", "streams=4"]
    m4 = [r for r in records(tmp_path / "res") if r.measure == "M4"]
    assert len(m4) == 6 and {r.status for r in m4} == {"ok"}


def test_campaign_resume_skips_recorded_stream_counts(tmp_path):
    res = tmp_path / "res"
    path = write_yaml(tmp_path, systems=["innodb", "postgres"], measures=["M4"], streams=[1, 4])
    run_campaign(path, res, Env(tmp_path).deps())
    json_before = {p: p.read_text() for p in res.rglob("M4-*.json")}
    assert len(json_before) == 4
    run_campaign(path, res, Env(tmp_path).deps())
    assert {p: p.read_text() for p in res.rglob("M4-*.json")} == json_before
    assert json.loads(next(iter(json_before.values())))["measure"] == "M4"
