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

    def __init__(self, script=None, scope="session", pushed="duckdb", running=False,
                 hook=None, **kw):
        super().__init__([], **kw)
        self.script = script or {}
        self.running = running  # what still_running() answers
        self.hook = hook  # hook(call_index) -> outcome overriding the script
        self.calls = 0
        self.pushdown_counter_scope = scope
        self.pushed = pushed
        self.log: list[tuple] = []
        self._lock = threading.Lock()

    def execute(self, sql, timeout_s):
        with self._lock:
            self.log.append(("execute", qnum(sql)))
            self.calls += 1
            idx = self.calls
        out = self.script.get(qnum(sql))
        if self.hook is not None:
            out = self.hook(idx) or out
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

    def open_stream(self, timeout_s):
        with self._lock:
            self.log.append(("open", timeout_s))

    def close_stream(self):
        with self._lock:
            self.log.append(("close",))

    def still_running(self, ident):
        with self._lock:
            self.log.append(("still_running", ident))
        if isinstance(self.running, list):  # scripted answers; the last one repeats
            return self.running.pop(0) if len(self.running) > 1 else self.running[0]
        return self.running


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


STOPPED = QueryOutcome("timeout", 1.0, None, set(), "server stopped it", "n/a", stopped=True)


def test_timeout_that_stops_is_not_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    a = StreamFake({5: STOPPED}, lingering=[2, 0])
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
    assert a.log[0][0] == "open"
    assert a.log[1:23] == [("execute", n) for n in range(1, 23)]
    assert a.log[23] == ("close",) and a.log[24] == ("prepare", 4)
    assert len(a.log) == 1 + 22 + 1 + 1 + 4 + 88 + 4 + 1
    assert a.log[-1] == ("prepare", 1)
    assert rec.extra["total_queries"] == 88


def test_streams_are_opened_before_the_clock_starts_and_closed_after_it_stops(tmp_path):
    a = StreamFake()
    marks: list[tuple] = []
    ticks = iter([0.0, 100.0])

    def timer():
        with a._lock:
            a.log.append(("clock",))
        return next(ticks)

    rec = run_throughput(a, 1, 3, "run1", tmp_path, clock=lambda: NOW, host=lambda: HOST,
                         query_text=lambda n: f"Q{n}", timer=timer)
    run = a.log[a.log.index(("prepare", 3)) + 1:-1]  # between prepare(3) and the reset
    start, stop = run.index(("clock",)), len(run) - 1 - run[::-1].index(("clock",))
    assert [e[0] for e in run[:start]] == ["open"] * 3  # opened and warmed before timing
    assert {e[0] for e in run[start + 1:stop]} == {"execute"} and stop - start - 1 == 66
    assert [e[0] for e in run[stop + 1:]] == ["close"] * 3  # closed after timing stopped
    assert rec.extra["elapsed_s"] == 100.0 and not marks


def test_a_failing_open_does_not_stop_the_stream_from_reaching_the_barrier(tmp_path):
    class Broken(StreamFake):
        def open_stream(self, timeout_s):
            raise RuntimeError("no session")

    rec = throughput(Broken(), tmp_path, streams=2)
    assert rec.extra["counts_by_status"] == {"ok": 44}
    assert rec.extra["open_errors"] and "no session" in rec.extra["open_errors"][0]


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

@pytest.mark.parametrize("scope", ["session", "global", "explain", "none"])
@pytest.mark.parametrize("streams", [1, 2])
def test_pushdown_is_not_measured_by_m4(tmp_path, scope, streams):
    rec = throughput(StreamFake(scope=scope, pushed="fallback"), tmp_path, streams=streams)
    assert rec.extra["pushed_counts"] == [{"not_measured": 22}] * streams
    assert rec.extra["pushdown_counter_scope"] == scope


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


# ---- per-query linger detection ----------------------------------------------------------------

def test_a_query_that_lingers_mid_run_marks_the_run_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_WAIT_S", 0.05)
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    survivor = QueryOutcome("timeout", 1.0, None, set(), "client read timeout", "n/a",
                            query_id=77)
    a = StreamFake({5: survivor}, running=True)  # the end-of-run processlist check shows nothing
    rec = throughput(a, tmp_path, streams=2)
    assert ("still_running", 77) in a.log
    assert rec.extra["contaminated"] is True and rec.extra["survived_cancel"] == 2
    assert a.log.count(("restart",)) == 1


def test_a_query_that_stops_after_cancel_is_not_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    gone = QueryOutcome("timeout", 1.0, None, set(), "client read timeout", "n/a", query_id=77)
    a = StreamFake({5: gone}, running=False)
    rec = throughput(a, tmp_path, streams=2)
    assert rec.extra["contaminated"] is False and ("restart",) not in a.log


def test_unconfirmed_stop_is_contaminated_only_with_several_streams(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    assert throughput(StreamFake({5: timeout()}), tmp_path, streams=2).extra["contaminated"]
    one = throughput(StreamFake({5: timeout()}), tmp_path / "one", streams=1)
    assert one.extra["contaminated"] is False


def test_a_failing_still_running_probe_counts_as_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)

    class Dead(StreamFake):
        def still_running(self, ident):
            raise ConnectionError("server gone")

    gone = QueryOutcome("timeout", 1.0, None, set(), "x", "n/a", query_id=1)
    assert throughput(Dead({5: gone}), tmp_path, streams=1).extra["contaminated"] is True


def test_a_failing_end_of_run_linger_probe_counts_as_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)

    class Dead(StreamFake):
        def lingering_queries(self):
            raise ConnectionError("server gone")

    rec = throughput(Dead({5: STOPPED}), tmp_path, streams=1)
    assert rec.extra["contaminated"] is True and rec.extra["linger_probe_failed"] is True


# ---- suspend retries: restart bookkeeping --------------------------------------------------------

def test_no_warm_up_after_the_restart_of_the_final_discarded_attempt(tmp_path):
    a = StreamFake(hook=lambda i: QueryOutcome("crashed", None, None, set(), "x", "n/a")
                   if i % 44 == 30 else None)
    with pytest.raises(HostSuspended):
        throughput(a, tmp_path, streams=2, suspended_s=Ticking(60.0), timer=iter(range(100)))
    # 1 initial + 2 re-warm-ups (after attempts 1 and 2) of 22, plus 3 attempts of 44
    assert len([e for e in a.log if e[0] == "execute"]) == 3 * 22 + 3 * 44
    assert a.log.count(("restart",)) == 3


def test_a_discarded_attempts_restart_error_stays_in_its_entry(tmp_path):
    readings = iter([0.0, 60.0, 60.0, 60.0])
    a = StreamFake(hook=lambda i: QueryOutcome("crashed", None, None, set(), "x", "n/a")
                   if i == 30 else None, restart_error=RuntimeError("no start"))
    rec = throughput(a, tmp_path, streams=2, suspended_s=lambda: next(readings),
                     timer=iter([0.0, 5.0, 10.0, 100.0]))
    assert "no start" in rec.extra["discarded_attempts"][0]["restart_error"]
    assert "restart_error" not in rec.extra


# ---- what runs inside the timed region, per family ------------------------------------------

class MyCur:
    def __init__(self, conn):
        self.conn, self.description, self._rows = conn, None, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.seen.append(sql)
        self._rows, self.description = [], None
        if sql.startswith("SELECT") and "STATUS" not in sql and "PROCESSLIST" not in sql:
            self._rows, self.description = [(1,)], (("a", 3),)
        if sql.startswith("SHOW"):
            self._rows = [("Com_duckdb_select", "1")]
        if "PROCESSLIST" in sql:
            self._rows = [(self.conn.listed,)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class MyConn:
    def __init__(self, listed=0):
        self.seen, self.closed, self.listed = [], False, listed

    def cursor(self):
        return MyCur(self)

    def thread_id(self):
        return 42

    def close(self):
        self.closed = True


def mysql_adapter(conns):
    from dbench.systems.alisql import AliSQLDuckDB

    a = AliSQLDuckDB(None)
    a._connect = lambda **kw: conns.append(MyConn()) or conns[-1]
    return a


def test_mysql_stream_connection_is_set_up_once_and_runs_no_probes(tmp_path):
    conns: list[MyConn] = []
    a = mysql_adapter(conns)
    a.open_stream(900)
    assert len(conns) == 1
    for _ in range(3):
        out = a.execute("SELECT 1 AS q", 900)
        assert out.status == "ok" and out.pushed == "n/a"
    assert len(conns) == 1  # one connection for the whole stream
    assert conns[0].seen == [
        "SET SESSION max_execution_time = 900000",  # once
        "SELECT 1", "SELECT 1 AS q", "SELECT 1 AS q", "SELECT 1 AS q"]
    assert not any("STATUS" in s for s in conns[0].seen)
    a.close_stream()
    assert conns[0].closed
    # outside a stream the per-query path is unchanged (fresh connection, probes)
    a.execute("SELECT 1 AS q", 900)
    assert len(conns) == 2 and any("STATUS" in s for s in conns[1].seen)


def test_mysql_stream_drops_its_connection_after_a_failure_and_reopens(tmp_path):
    conns: list[MyConn] = []
    a = mysql_adapter(conns)
    a.open_stream(900)

    def boom(sql, params=None):
        raise RuntimeError("bad statement")

    conns[0].cursor = lambda: type("C", (MyCur,), {"execute": lambda s, q, p=None: boom(q)})(
        conns[0])
    assert a.execute("SELECT 1", 900).status == "error"
    assert conns[0].closed
    assert a.execute("SELECT 1 AS q", 900).status == "ok"
    assert len(conns) == 2 and conns[1].seen[0] == "SET SESSION max_execution_time = 900000"


def test_mysql_still_running_asks_the_processlist_for_that_id():
    conns: list[MyConn] = []
    a = mysql_adapter(conns)
    conns.append(MyConn(listed=1))
    a._connect = lambda **kw: conns[0]
    assert a.still_running(42) is True
    sql = conns[0].seen[0]
    assert "PROCESSLIST" in sql and "COMMAND IN ('Query', 'Killed')" in sql


class PgCur:
    def __init__(self, conn, sql):
        conn.seen.append(sql)
        self.description = None
        self._rows = []
        if sql.startswith(("SELECT", "EXPLAIN")):
            self._rows, self.description = [(1,)], (type("D", (), {"type_code": 23,
                                                                    "name": "a"})(),)
        if "pg_stat_activity" in sql:
            self._rows = [(conn.active,)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0]


class PgConn:
    def __init__(self, active=0):
        self.seen, self.closed, self.active = [], False, active
        self.info = type("I", (), {"backend_pid": 99})()

    def execute(self, sql, params=None):
        return PgCur(self, sql)

    def close(self):
        self.closed = True


def pg_adapter(conns, cls_name="pg_duckdb"):
    from dbench.systems.pgduckdb import PgDuckDB

    a = PgDuckDB(None)
    a._limits = SMOKE
    a._connect = lambda connect_timeout=10: conns.append(PgConn()) or conns[-1]
    return a


def test_pg_stream_session_is_warmed_in_duckdb_before_timing_and_runs_no_probes():
    conns: list[PgConn] = []
    a = pg_adapter(conns)
    a.open_stream(900)
    assert conns[0].seen == [
        "SET duckdb.force_execution = true", "SET statement_timeout = 900000",
        "SELECT * FROM duckdb.query('SELECT 1')"]
    for _ in range(2):
        assert a.execute("SELECT 1 AS q", 900).status == "ok"
    assert len(conns) == 1
    assert conns[0].seen[3:] == ["SELECT 1 AS q", "SELECT 1 AS q"]  # no EXPLAIN, no SET
    a.close_stream()
    assert conns[0].closed


def test_pgducklake_warm_statement_runs_in_duckdb():
    from dbench.systems.pgducklake import PgDuckLake

    conns: list[PgConn] = []
    a = PgDuckLake(None)
    a._limits = SMOKE
    a._connect = lambda connect_timeout=10: conns.append(PgConn()) or conns[-1]
    a.open_stream(900)
    assert conns[0].seen[-1] == "SELECT * FROM ducklake.query('SELECT 1 AS one')"
    assert conns[0].seen[0].startswith("SELECT ducklake.raw_query")  # setup before the warm-up


def test_pg_still_running_asks_pg_stat_activity_for_that_pid():
    conns: list[PgConn] = []
    a = pg_adapter(conns)
    a._connect = lambda connect_timeout=10: conns.append(PgConn(active=1)) or conns[-1]
    assert a.still_running(99) is True
    assert "pid = %s" in conns[0].seen[0] and "state = 'active'" in conns[0].seen[0]


def test_base_still_running_is_conservative_and_stream_hooks_are_no_ops():
    a = FakeAdapter([])
    assert a.still_running(1) is True
    assert a.open_stream(900) is None and a.close_stream() is None


def test_a_query_that_lingers_then_stops_is_not_contaminated(tmp_path, monkeypatch):
    monkeypatch.setattr("dbench.measures.power.LINGER_POLL_S", 0.01)
    slow = QueryOutcome("timeout", 1.0, None, set(), "client read timeout", "n/a", query_id=77)
    a = StreamFake({5: slow}, running=[True, True, False] * 2)  # the warm-up pass polls too
    rec = throughput(a, tmp_path, streams=1)
    assert a.log.count(("still_running", 77)) == 6
    assert rec.extra["contaminated"] is False and ("restart",) not in a.log


# ---- a failed open, and barrier safety -----------------------------------------------------------

def test_mysql_failed_open_is_redone_on_the_stream_path_without_probes():
    conns: list[MyConn] = []
    a = mysql_adapter(conns)
    real = a._connect
    state = {"fail": True}

    def flaky(**kw):
        if state.pop("fail", False):
            raise ConnectionError("refused")
        return real(**kw)

    a._connect = flaky
    with pytest.raises(ConnectionError):
        a.open_stream(900)
    assert a.execute("SELECT 1 AS q", 900).status == "ok"
    assert a.execute("SELECT 1 AS q", 900).status == "ok"
    assert len(conns) == 1  # one reopened connection, kept for the stream
    assert conns[0].seen == ["SET SESSION max_execution_time = 900000",
                             "SELECT 1 AS q", "SELECT 1 AS q"]  # no STATUS probe, one SET


def test_pg_failed_open_is_redone_on_the_stream_path_without_probes():
    conns: list[PgConn] = []
    a = pg_adapter(conns)
    real = a._connect
    state = {"fail": True}

    def flaky(connect_timeout=10):
        if state.pop("fail", False):
            raise ConnectionError("refused")
        return real(connect_timeout)

    a._connect = flaky
    with pytest.raises(ConnectionError):
        a.open_stream(900)
    for _ in range(2):
        assert a.execute("SELECT 1 AS q", 900).status == "ok"
    assert len(conns) == 1
    assert conns[0].seen == ["SET duckdb.force_execution = true",
                             "SET statement_timeout = 900000",  # once
                             "SELECT 1 AS q", "SELECT 1 AS q"]  # no EXPLAIN


def test_a_clock_that_fails_aborts_the_run_without_hanging(tmp_path):
    a = StreamFake()
    outcome: dict = {}

    def run():
        try:
            run_throughput(a, 1, 3, "run1", tmp_path, clock=lambda: NOW, host=lambda: HOST,
                           query_text=lambda n: f"Q{n}", timer=BoomTimer())
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(10)
    assert not worker.is_alive(), "the run hung on a broken barrier"
    assert isinstance(outcome["error"], RuntimeError)
    assert a.log.count(("close",)) == 1 + 3 and a.log[-1] == ("prepare", 1)
    assert list(iter_records(tmp_path)) == []


class BoomTimer:
    def __call__(self):
        raise RuntimeError("clock failed")


# ---- DuckDB native stream path ------------------------------------------------------------------

class FakeSocket:
    created = 0

    def __init__(self, sent):
        self.sent = sent

    def settimeout(self, s):
        pass

    def makefile(self, mode):
        return FakeFile(self.sent)

    def close(self):
        pass


class FakeFile:
    def __init__(self, sent):
        self.sent, self._reply = sent, b""

    def write(self, data):
        self.sent.append(json.loads(data))
        self._reply = (json.dumps({
            "status": "ok", "elapsed_s": 0.1, "error": None,
            "columns": [{"name": "a", "type": "BIGINT"}], "rows": [[1]]}) + "\n").encode()

    def flush(self):
        pass

    def readline(self):
        return self._reply

    def close(self):
        pass


def test_duckdb_native_stream_uses_one_connection_for_warm_up_and_queries(monkeypatch):
    from dbench.systems import duckdb_native
    from dbench.systems.duckdb_native import DuckDBNative

    sent: list[dict] = []
    opened: list[FakeSocket] = []
    monkeypatch.setattr(duckdb_native.socket, "create_connection",
                        lambda *a, **k: opened.append(FakeSocket(sent)) or opened[-1])
    monkeypatch.setattr(duckdb_native.dockerctl, "container_ip", lambda name: "127.0.0.1")
    a = DuckDBNative("1.5.5")
    a.open_stream(900)
    outs = [a.execute("SELECT 2", 900) for _ in range(2)]
    assert [o.status for o in outs] == ["ok", "ok"] and outs[0].rows == [(1,)]
    assert len(opened) == 1  # one TCP connection for the warm-up and both queries
    assert [r["sql"] for r in sent] == ["SELECT 1", "SELECT 2", "SELECT 2"]
    a.close_stream()
    a.execute("SELECT 3", 900)  # outside a stream: a fresh connection per query again
    assert len(opened) == 2
