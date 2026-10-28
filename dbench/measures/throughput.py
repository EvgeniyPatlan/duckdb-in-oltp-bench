"""M4: TPC-H throughput run. N concurrent streams, each running the 22 queries once in its own
deterministic order, one record per stream count (``item="streams=N"``, ``unit="qph"``).

Execution path. Every stream thread runs its queries through the adapter's normal ``execute()``,
so per-session setup and timeouts behave as in the power run: a persistent per-thread session on
the Postgres family (pg_ducklake threads/memory, pg_duckdb ``force_execution``), a fresh connection
per query on the MySQL family (its connect time is outside each query's timed region but inside
the stream's wall time, the same for every MySQL-family system), one cursor per connection on
the DuckDB server. Correctness is M1's job: results are not compared, only each query's status is
kept. ``value`` is ``ok_queries * 3600 / elapsed_s`` (failed queries do not count).

Memory. Before the streams open, ``adapter.prepare_streams(N)`` lets engines whose backends each
own a DuckDB instance (pg_duckdb, pg_ducklake) divide its memory by N; it is called with 1 once
the run is over. The value in force is recorded as ``extra["memory_per_backend"]``.

Warm cache. Before each stream count one unrecorded warm-up pass of the 22 queries runs on a
single stream. Nothing drops the caches between stream counts: this is a warm-cache measure.

Pushdown. ``extra["pushed_counts"]`` holds, per stream, how many ``ok`` queries had each
``pushed`` value. An adapter whose counter is server-wide (``pushdown_counter_scope == "global"``,
Percona) cannot attribute it with several streams, so ``pushed`` is ``"unknown"`` when N > 1.

Timeouts and contamination. The per-query timeout applies. A stream whose query timed out calls
``cancel()`` at once (it only touches queries past their deadline). Once every stream is done,
``lingering_queries()`` is polled for up to ``power.LINGER_WAIT_S``; a query still running (or a
``cancel()`` that could not be issued) marks the record ``extra["contaminated"]=True`` with the
lingering count. The server is never restarted mid-run: the record is written, then the server
is restarted, before the next stream count. A crash or an unresponsive server restarts it the
same way.

Host suspend. ``hostinfo.suspended_s()`` is read around the timed run; if the host slept more
than SUSPEND_TOLERANCE_S the run is discarded and repeated, up to SUSPEND_RETRIES more times
(after a restart and a fresh warm-up if the discarded run left the server unusable); if every
attempt slept, ``HostSuspended`` is raised and nothing is written.

Errors, timeouts and crashes are counted in ``extra["counts_by_status"]``, never raised.
"""
from __future__ import annotations

import random
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
from dbench.measures.power import (
    QUERY_NUMBERS,
    SUSPEND_RETRIES,
    SUSPEND_TOLERANCE_S,
    HostSuspended,
    _lingering,
    _probe,
    _restart,
)
from dbench.queries.dialect import load_query, translate
from dbench.results import ResultRecord, write
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
    pushed: Counter = field(default_factory=Counter)
    errors: list[list] = field(default_factory=list)  # [qNN, status, message]
    cancel_failed: bool = False


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


def _run_stream(adapter: SystemAdapter, order: list[int], texts: dict[int, str], timeout_s: int,
                start: threading.Barrier | None, hide_pushed: bool) -> _Stream:
    if start is not None:
        start.wait()
    stream = _Stream()
    for n in order:
        out = _run_query(adapter, texts[n], timeout_s)
        name = f"q{n:02d}"
        stream.latencies.append(
            [name, None if out.elapsed_s is None else round(out.elapsed_s, 6), out.status])
        if out.status == "ok":
            stream.pushed["unknown" if hide_pushed else out.pushed] += 1
            continue
        if len(stream.errors) < MAX_ERRORS_KEPT:
            stream.errors.append([name, out.status, (out.error or "")[:ERROR_TEXT_CHARS]])
        if out.status == "timeout" and not _cancel(adapter):
            stream.cancel_failed = True
    return stream


def _warm_up(adapter: SystemAdapter, texts: dict[int, str], timeout_s: int) -> None:
    """One unrecorded pass of the queries on a single stream (in a thread of its own, so a
    per-thread session does not outlive it)."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_run_stream, adapter, stream_order(0), texts, timeout_s, None, False).result()


def _run_streams(adapter: SystemAdapter, streams: int, texts: dict[int, str], timeout_s: int,
                 timer: Callable[[], float]) -> _Attempt:
    hide_pushed = streams > 1 and adapter.pushdown_counter_scope == "global"
    barrier = threading.Barrier(streams + 1)
    with ThreadPoolExecutor(max_workers=streams) as pool:
        futures = [pool.submit(_run_stream, adapter, stream_order(s), texts, timeout_s, barrier,
                               hide_pushed) for s in range(1, streams + 1)]
        barrier.wait()
        started = timer()
        results = [f.result() for f in futures]
        elapsed = timer() - started
    return _Attempt(elapsed, results)


def _needs_restart(adapter: SystemAdapter, attempt: _Attempt, extra: dict) -> bool:
    """Whether the server must be restarted after the run; fills ``extra`` with why.
    Contamination is a query still running after its timeout was cancelled."""
    counts = attempt.statuses()
    contaminated = False
    if counts["timeout"]:
        lingering = _lingering(adapter)
        cancel_failed = any(s.cancel_failed for s in attempt.streams)
        contaminated = bool(lingering) or cancel_failed
        if lingering:
            extra["lingering"] = lingering
        if cancel_failed:
            extra["cancel_unavailable"] = True
    extra["contaminated"] = contaminated
    crashed = bool(counts["crashed"])
    if crashed:
        extra["container"] = adapter.crash_info()
    unreachable = not crashed and any(c for st, c in counts.items() if st != "ok") \
        and not _probe(adapter)
    if unreachable:
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
        "pushed_counts": [dict(s.pushed) for s in attempt.streams],
    }
    if errors:
        out["errors"] = errors
    return out


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
    restart_error: str | None = None
    _warm_up(adapter, texts, timeout_s)
    adapter.prepare_streams(streams)
    try:
        for _ in range(SUSPEND_RETRIES + 1):
            started_at = clock().isoformat()
            before = slept()
            attempt = _run_streams(adapter, streams, texts, timeout_s, timer)
            gap = slept() - before
            extra: dict = {}
            restart = _needs_restart(adapter, attempt, extra)
            memory = adapter.memory_per_backend()
            if gap <= SUSPEND_TOLERANCE_S:
                break
            discarded.append({"host_suspended_s": round(gap, 1), **extra})
            if restart:
                restart_error = _restart(adapter) or restart_error
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
    if restart_error:
        extra["restart_error"] = restart_error

    ok_queries = extra["ok_queries"]
    failed = ok_queries == 0
    rec = ResultRecord(
        system=adapter.name, track=adapter.track, measure="M4", sf=sf, item=item,
        status="error" if failed else "ok",
        value=None if failed else queries_per_hour(ok_queries, attempt.elapsed_s),
        unit="qph", config_ref=f"systems/{adapter.config_dir}/", run_id=run_id,
        started_at=started_at, extra=extra,
        error=(f"no query succeeded: {extra['counts_by_status']}; first errors: "
               f"{extra.get('errors', [])[:3]}") if failed else None,
        versions=versions, host=host_facts)
    write(rec, root)
    if restart:
        error = _restart(adapter)
        if error:
            rec = replace(rec, extra={**rec.extra, "restart_error": error})
            write(rec, root)
    return rec
