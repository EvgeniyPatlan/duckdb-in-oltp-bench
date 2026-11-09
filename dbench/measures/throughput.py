"""M4: TPC-H throughput run. N concurrent streams, each running the 22 queries once in its own
deterministic order, one record per stream count (``item="streams=N"``, ``unit="qph"``).

What is timed. Every stream thread runs the same way on every system. Before the clock starts
each thread calls ``adapter.open_stream(timeout_s)``: it opens its own persistent connection,
applies the session settings and the per-query timeout once, and runs one trivial statement
through DuckDB (so a Postgres backend's DuckDB instance exists already). All threads then meet at
a barrier and the clock starts. Inside the timed region a thread does only this, per query:
send the statement and fetch the result on its connection. There is no per-query SET, no
connect, no ``SHOW STATUS``/``EXPLAIN`` probe: M4 does not measure pushdown (that is M1/M8's
job), so ``pushed_counts`` only counts ``not_measured``. When every stream has finished the clock
stops and only then does each thread call ``adapter.close_stream()``. A query that fails drops
the connection; the next query of that stream reopens one inside the timed region (rare failure
path, identical on every system). The client-side decoding of a result (VillageSQL's JSON
parse) happens inside the stream and is counted: it is part of what that entry point costs.
If an adapter's ``open_stream`` fails, the adapter is already in stream mode and reopens the
connection on the stream path (settings once, no probes), the same as after a failed query.
The warm statement executes in DuckDB on pg_duckdb, pg_ducklake, VillageSQL and the DuckDB server;
the MySQL DuckDB engines (AliSQL, MariaDB, Percona) share one server-wide DuckDB instance that
the unrecorded warm-up pass has already warmed, so for them it only warms the connection.
Correctness is M1's job: results are not compared, only each query's status is kept.
``value`` is ``ok_queries * 3600 / elapsed_s`` (failed queries do not count).

Memory. Before the streams open, ``adapter.prepare_streams(N)`` lets engines whose backends each
own a DuckDB instance (pg_duckdb, pg_ducklake) divide its memory by N; it is called with 1 once
the run is over. The value in force is recorded as ``extra["memory_per_backend"]``.

Warm cache. Before each stream count one unrecorded warm-up pass of the 22 queries runs on a
single stream. Nothing drops the caches between stream counts: this is a warm-cache measure.

Timeouts and contamination. The per-query timeout applies. A stream whose query timed out
calls ``cancel()`` at once (it only touches queries past their deadline), then polls
``adapter.still_running(query_id)`` for up to ``power.LINGER_WAIT_S``. The run is contaminated
(``extra["contaminated"]=True``) when that query survives, when ``cancel()`` could not be issued,
when a probe raises, when the stop of a timed-out query was never confirmed and several streams
ran (``unconfirmed_stops``), or when ``lingering_queries()`` still shows queries after all streams
finished. After a timeout whose stop the server did not confirm, that stream's poll (up to
``power.LINGER_WAIT_S``) is inside the timed region. The server is never restarted mid-run:
the record is written, then the server is restarted, before the next stream count. A crash or
an unresponsive server restarts it the same way.

Host suspend. ``hostinfo.suspended_s()`` is read around the timed run; if the host slept more
than SUSPEND_TOLERANCE_S the run is discarded and repeated, up to SUSPEND_RETRIES more times
(after a restart and a fresh warm-up if the discarded run left the server unusable); if every
attempt slept, ``HostSuspended`` is raised and nothing is written.

Errors, timeouts and crashes are counted in ``extra["counts_by_status"]``, never raised.

Status. Only the per-query outcomes decide it (``level_status``): a run in which any query
``crashed`` (its server's container had stopped) is ``crashed``, else one in which any query
failed with an ``error`` is ``error``; neither has a value, because a failed query frees its
stream early and the queries-per-hour of the rest would read as throughput. The figure is kept
as ``extra["ok_queries_qph"]``. A run of ``ok`` and ``timeout`` queries only is ``ok`` with its
value: a timeout is a measured outcome. ``contaminated`` and ``restarted_after_run`` describe what
the harness did after the run and never change the status.
"""
from __future__ import annotations

import random
import shutil
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from dbench import hostinfo
from dbench.config import QUERY_TIMEOUT_S
from dbench.measures import power
from dbench.measures.power import (
    QUERY_NUMBERS,
    SUSPEND_RETRIES,
    SUSPEND_TOLERANCE_S,
    HostSuspended,
    _probe,
    _restart,
)
from dbench.queries.dialect import load_query, translate
from dbench.results import ResultRecord, _path, iter_records, write
from dbench.systems.base import QueryOutcome, SystemAdapter

DEFAULT_SEED = 20261027
ITEM_FORMAT = "streams={n}"
MAX_ERRORS_KEPT = 50
ERROR_TEXT_CHARS = 300


def stream_order(stream_id: int, seed: int = DEFAULT_SEED) -> list[int]:
    """Query order of one stream: a deterministic permutation of 1..22 (stream 0 is 1..22)."""
    order = list(QUERY_NUMBERS)
    if stream_id != 0:
        random.Random(seed * 1000 + stream_id).shuffle(order)
    return order


def queries_per_hour(ok_queries: int, elapsed_s: float) -> float:
    return ok_queries * 3600 / elapsed_s


def _oom_killed(container: dict | None) -> bool:
    return str((container or {}).get("OOMKilled", "")).lower() == "true"


def level_status(counts: dict[str, int],
                 container: dict | None = None) -> tuple[str, str | None]:
    """``(status, error)`` of a run from its ``counts_by_status`` (see Status above); a crash
    is named a memory kill only when ``container`` (``extra["container"]``) says OOMKilled."""
    total = sum(counts.values())
    if not counts.get("ok"):
        return "error", f"no query succeeded: {counts}"
    parts = ", ".join(f"{counts[k]} {k}" for k in ("crashed", "error", "timeout", "ok")
                      if counts.get(k))
    if counts.get("crashed"):
        what = ("the container was killed by its memory limit during the run (OOMKilled, exit "
                f"{container.get('ExitCode')})" if _oom_killed(container)
                else "the server stopped during the run")
        return "crashed", f"{what} ({parts} of {total} queries); no throughput is reported"
    if counts.get("error"):
        return "error", (f"queries failed during the run ({parts} of {total} queries); "
                         "no throughput is reported")
    return "ok", None


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile (``p`` in 0..100); None for no values."""
    if not values:
        return None
    ranked = sorted(values)
    rank = max(1, -(-len(ranked) * p // 100))  # ceil
    return ranked[int(rank) - 1]


@dataclass
class _Stream:
    latencies: list[list] = field(default_factory=list)  # [qNN, seconds or None, status]
    errors: list[list] = field(default_factory=list)  # [qNN, status, message]
    cancel_failed: bool = False
    survived_cancel: int = 0  # timed-out queries still running after cancel()
    unconfirmed_stops: int = 0  # timed-out queries whose stop nobody confirmed
    probe_failed: bool = False
    open_error: str | None = None


@dataclass
class _Attempt:
    elapsed_s: float
    streams: list[_Stream]

    def statuses(self) -> Counter:
        return Counter(row[2] for s in self.streams for row in s.latencies)


def _run_query(adapter: SystemAdapter, sql: str, timeout_s: int) -> QueryOutcome:
    try:
        return adapter.execute(sql, timeout_s)
    except Exception as exc:  # noqa: BLE001 - a failing query is counted, never raised
        return QueryOutcome("error", None, None, set(), f"{type(exc).__name__}: {exc}", "n/a")


def _cancel(adapter: SystemAdapter) -> bool:
    try:
        return bool(adapter.cancel())
    except Exception:  # noqa: BLE001
        return False


def _query_survives(adapter: SystemAdapter, query_id: int) -> bool | None:
    """Poll ``still_running`` for up to LINGER_WAIT_S: True when the query is still there (or
    the probe fails: unknown counts as contaminated), False once it is gone."""
    deadline = time.monotonic() + power.LINGER_WAIT_S
    while True:
        try:
            running = adapter.still_running(query_id)
        except Exception:  # noqa: BLE001
            return None
        if not running:
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(power.LINGER_POLL_S)


def _after_timeout(adapter: SystemAdapter, out: QueryOutcome, stream: _Stream) -> None:
    if not _cancel(adapter):
        stream.cancel_failed = True
    if out.stopped:
        return  # the server reported its own deadline stopping the query
    if out.query_id is None:
        stream.unconfirmed_stops += 1
        return
    survives = _query_survives(adapter, out.query_id)
    if survives is None:
        stream.probe_failed = True
    elif survives:
        stream.survived_cancel += 1


def _run_stream(adapter: SystemAdapter, order: list[int], texts: dict[int, str], timeout_s: int,
                start: threading.Barrier | None, finish: threading.Barrier | None) -> _Stream:
    """Open and warm this thread's connection, wait for the start, run the queries, wait for
    every stream to finish (the clock stops there), then close the connection."""
    stream = _Stream()
    try:
        adapter.open_stream(timeout_s)
    except Exception as exc:  # noqa: BLE001 - the adapter is already in stream mode, so
        stream.open_error = f"{type(exc).__name__}: {exc}"  # execute() reopens on the stream path
    try:
        if start is not None:
            try:
                start.wait()
            except Exception:  # noqa: BLE001 - broken barrier: the run is being aborted
                return stream
        for n in order:
            out = _run_query(adapter, texts[n], timeout_s)
            name = f"q{n:02d}"
            stream.latencies.append(
                [name, None if out.elapsed_s is None else round(out.elapsed_s, 6), out.status])
            if out.status == "ok":
                continue
            if len(stream.errors) < MAX_ERRORS_KEPT:
                stream.errors.append([name, out.status, (out.error or "")[:ERROR_TEXT_CHARS]])
            if out.status == "timeout":
                _after_timeout(adapter, out, stream)
    finally:
        if finish is not None:
            try:
                finish.wait()
            except Exception:  # noqa: BLE001 - broken barrier: just close
                pass
        try:
            adapter.close_stream()
        except Exception:  # noqa: BLE001
            pass
    return stream


def _warm_up(adapter: SystemAdapter, texts: dict[int, str], timeout_s: int) -> None:
    """One unrecorded pass of the queries on a single stream (in a thread of its own, so a
    per-thread session does not outlive it)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_run_stream, adapter, stream_order(0), texts, timeout_s, None, None).result()


def _run_streams(adapter: SystemAdapter, streams: int, texts: dict[int, str], timeout_s: int,
                 timer: Callable[[], float]) -> _Attempt:
    clock: dict[str, float] = {}
    # The barrier actions run before any waiting thread is released: the clock starts after every
    # stream is open and stops before any stream closes its connection.
    start = threading.Barrier(streams + 1, action=lambda: clock.update(start=timer()))
    finish = threading.Barrier(streams + 1, action=lambda: clock.update(stop=timer()))
    with ThreadPoolExecutor(max_workers=streams) as pool:
        futures = [pool.submit(_run_stream, adapter, stream_order(s), texts, timeout_s, start,
                               finish) for s in range(1, streams + 1)]
        try:
            start.wait()
            finish.wait()
        except BaseException:
            start.abort()  # release every waiting stream so none hangs; they close and exit
            finish.abort()
            raise
        elapsed = clock["stop"] - clock["start"]
        results = [f.result() for f in futures]
    return _Attempt(elapsed, results)


def _final_lingering(adapter: SystemAdapter) -> tuple[int, bool]:
    """(queries still running once every stream is done, whether the probe failed). Unlike the
    power run, a probe that raises is not read as "nothing running"."""
    deadline = time.monotonic() + power.LINGER_WAIT_S
    while True:
        try:
            count = adapter.lingering_queries()
        except Exception:  # noqa: BLE001
            return 0, True
        if count == 0 or time.monotonic() >= deadline:
            return count, False
        time.sleep(power.LINGER_POLL_S)


def _needs_restart(adapter: SystemAdapter, attempt: _Attempt, streams: int,
                   extra: dict) -> bool:
    """Whether the server must be restarted after the run; fills ``extra`` with why."""
    counts = attempt.statuses()
    survived = sum(s.survived_cancel for s in attempt.streams)
    unconfirmed = sum(s.unconfirmed_stops for s in attempt.streams)
    contaminated = bool(survived) or any(s.cancel_failed or s.probe_failed
                                         for s in attempt.streams)
    contaminated = contaminated or (unconfirmed > 0 and streams > 1)
    if counts["timeout"]:
        lingering, failed = _final_lingering(adapter)
        contaminated = contaminated or bool(lingering) or failed
        if lingering:
            extra["lingering"] = lingering
        if failed:
            extra["linger_probe_failed"] = True
    if survived:
        extra["survived_cancel"] = survived
    if unconfirmed:
        extra["unconfirmed_stops"] = unconfirmed
    if any(s.cancel_failed for s in attempt.streams):
        extra["cancel_unavailable"] = True
    extra["contaminated"] = contaminated
    crashed = bool(counts["crashed"])
    unreachable = not crashed and any(c for st, c in counts.items() if st != "ok") \
        and not _probe(adapter)
    if crashed or unreachable:
        extra["container"] = adapter.crash_info()
    return contaminated or crashed or unreachable


def _summary(attempt: _Attempt, streams: int) -> dict:
    ok_latencies = [row[1] for s in attempt.streams for row in s.latencies
                    if row[2] == "ok" and row[1] is not None]
    counts = attempt.statuses()
    errors = [e for s in attempt.streams for e in s.errors][:MAX_ERRORS_KEPT]
    out = {
        "streams": streams,
        "elapsed_s": attempt.elapsed_s,
        "total_queries": sum(counts.values()),
        "ok_queries": counts["ok"],
        "per_query_latency": [s.latencies for s in attempt.streams],
        "p50": percentile(ok_latencies, 50),
        "p95": percentile(ok_latencies, 95),
        "counts_by_status": dict(counts),
        "pushed_counts": [{"not_measured": sum(1 for r in s.latencies if r[2] == "ok")}
                          for s in attempt.streams],
    }
    open_errors = [s.open_error for s in attempt.streams if s.open_error]
    if open_errors:
        out["open_errors"] = open_errors
    if errors:
        out["errors"] = errors
    return out


def _attempts_left(discarded: list[dict]) -> bool:
    return len(discarded) <= SUSPEND_RETRIES


def _warm_after_restart(adapter: SystemAdapter, streams: int, texts: dict[int, str],
                        timeout_s: int) -> None:
    adapter.prepare_streams(1)
    _warm_up(adapter, texts, timeout_s)
    adapter.prepare_streams(streams)


def run_throughput(
    adapter: SystemAdapter,
    sf: int,
    streams: int,
    run_id: str,
    root: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    host: Callable[[], dict] = hostinfo.host_info,
    timeout_s: int = QUERY_TIMEOUT_S,
    suspended_s: Callable[[], float] | None = None,
    query_text: Callable[[int], str] | None = None,
    timer: Callable[[], float] = time.perf_counter,
) -> ResultRecord:
    """Measure ``streams`` concurrent streams, write the M4 record under ``root`` and return it."""
    if streams < 1:
        raise ValueError("streams must be at least 1")
    slept = suspended_s or hostinfo.suspended_s
    text = query_text or (lambda n: translate(load_query(n), adapter.dialect))
    texts = {n: text(n) for n in QUERY_NUMBERS}
    item = ITEM_FORMAT.format(n=streams)
    host_facts, versions = host(), adapter.versions()

    discarded: list[dict] = []
    _warm_up(adapter, texts, timeout_s)
    adapter.prepare_streams(streams)
    try:
        for _ in range(SUSPEND_RETRIES + 1):
            started_at = clock().isoformat()
            before = slept()
            attempt = _run_streams(adapter, streams, texts, timeout_s, timer)
            gap = slept() - before
            extra: dict = {}
            restart = _needs_restart(adapter, attempt, streams, extra)
            memory = adapter.memory_per_backend()
            if gap <= SUSPEND_TOLERANCE_S:
                break
            discarded.append({"host_suspended_s": round(gap, 1), **extra})
            if restart:
                error = _restart(adapter)
                if error:
                    discarded[-1]["restart_error"] = error
                if _attempts_left(discarded):
                    _warm_after_restart(adapter, streams, texts, timeout_s)
        else:
            sleeps = [d["host_suspended_s"] for d in discarded]
            raise HostSuspended(
                f"{adapter.name} {item}: the host was suspended during each of {len(sleeps)} "
                f"attempts ({sleeps} s); keep it awake and resume the run")
    finally:
        adapter.prepare_streams(1)

    extra = {**_summary(attempt, streams), **extra,
             "pushdown_counter_scope": adapter.pushdown_counter_scope}
    if memory is not None:
        extra["memory_per_backend"] = memory
    if discarded:
        extra["host_suspended_s"] = [d["host_suspended_s"] for d in discarded]
        extra["discarded_attempts"] = discarded
    if restart:
        extra["restarted_after_run"] = True

    ok_queries = extra["ok_queries"]
    status, error = level_status(extra["counts_by_status"], extra.get("container"))
    qph = queries_per_hour(ok_queries, attempt.elapsed_s) if ok_queries else None
    if status != "ok" and qph is not None:
        extra["ok_queries_qph"] = qph
    if error:
        error = f"{error}; first errors: {extra.get('errors', [])[:3]}"
    rec = ResultRecord(
        system=adapter.name, track=adapter.track, measure="M4", sf=sf, item=item,
        status=status, value=qph if status == "ok" else None,
        unit="qph", config_ref=f"systems/{adapter.config_dir}/", run_id=run_id,
        started_at=started_at, extra=extra, error=error,
        versions=versions, host=host_facts)
    write(rec, root)
    if restart:
        error = _restart(adapter)
        if error:
            rec = replace(rec, extra={**rec.extra, "restart_error": error})
            write(rec, root)
    return rec


def restate(root: Path, run_id: str, backup: Path | None) -> list[tuple[ResultRecord, str]]:
    """Apply ``level_status`` to the M4 records of ``run_id`` that were written ``ok`` before it
    existed, and refresh the error of records an earlier pass restated. Only the stored
    per-query counts and ``extra["container"]`` are read: nothing is measured or estimated. The
    value moves to ``extra["ok_queries_qph"]``; the lock, versions and every other field stay.
    Returns ``(record, new status)`` for each record that changes; with ``backup`` set, each
    original file is copied under ``backup/<run_id>/<system>/`` and the record is rewritten."""
    changes = []
    for rec in iter_records(Path(root) / run_id):
        counts = rec.extra.get("counts_by_status")
        restated = "restated" in rec.extra
        if rec.measure != "M4" or not counts or (rec.status != "ok" and not restated):
            continue
        status, error = level_status(counts, rec.extra.get("container"))
        error = f"{error}; first errors: {rec.extra.get('errors', [])[:3]}" if error else None
        if status != rec.status or (restated and error != rec.error):
            changes.append((rec, status, error))
    if backup is None:
        return [(rec, status) for rec, status, _ in changes]
    for rec, status, error in changes:
        src = _path(root, rec.system, rec.measure, rec.sf, rec.item, rec.run_id)
        dest = Path(backup) / run_id / src.parent.name / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():  # a second pass must not replace the original with a rewrite
            shutil.copy2(src, dest)
        extra = {**rec.extra,
                 "restated": {"from": "ok", "rule": "dbench.measures.throughput.level_status"}}
        if "ok_queries_qph" not in extra:
            extra["ok_queries_qph"] = rec.value
        write(replace(rec, status=status, value=None, extra=extra, error=error), root)
    return [(rec, status) for rec, status, _ in changes]
