"""M1: TPC-H power run (one cold run and WARM_RUNS warm runs per query).

After a timeout the adapter is asked to cancel the query, then ``lingering_queries()`` is polled
for up to LINGER_WAIT_S. A query that is still running (cancel can fail, e.g. a pushed DuckDB
query on MariaDB) would steal CPU from the next measurements, so the server is restarted; the
record carries ``extra["restarted_after_timeout"]=True`` and ``extra["lingering"]=n``.
A ``cancel()`` that reports False (the server cannot reach the query, as with VillageSQL's
``duckdb_query()``, which ``KILL`` does not touch) forces the restart too, whatever the processlist
shows, and is flagged ``extra["cancel_unavailable"]=True``.

A host suspend (laptop sleep) while a run is in flight invalidates that attempt: the servers'
own deadlines run on the wall clock, which jumps forward across the sleep, so Postgres reports
``statement_timeout`` on resume for a query that had run for under a second. The attempt is
discarded (after the usual cancel/restart handling) and the query is measured again from its
cold run, up to SUSPEND_RETRIES more times; the record lists the sleeps in
``extra["host_suspended_s"]`` and, per discarded attempt, the sleep and any cancel/restart flags
in ``extra["discarded_attempts"]``. If the host sleeps through every attempt, ``HostSuspended`` is
raised and nothing is recorded for that query (the campaign records an auto-failure that a
resume retries).
"""
from __future__ import annotations

import statistics
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from dbench import dockerctl, hostinfo
from dbench.config import QUERY_TIMEOUT_S, WARM_RUNS
from dbench.correctness import checksum, compare
from dbench.queries.dialect import load_query, translate
from dbench.results import ResultRecord, write
from dbench.systems.base import QueryOutcome, SystemAdapter

QUERY_NUMBERS = range(1, 23)
LINGER_WAIT_S = 10.0
LINGER_POLL_S = 1.0
SUSPEND_TOLERANCE_S = 1.0  # BOOTTIME - MONOTONIC only moves across a suspend
SUSPEND_RETRIES = 2
SUSPEND_REASON = "host suspended during measurement; retried on resume"


class HostSuspended(RuntimeError):
    """The host slept during every attempt at one query, so it has no valid measurement."""


class _Interrupted(Exception):
    """The host slept while a run was in flight; the attempt is discarded."""

    def __init__(self, slept_s: float, restart: bool, flags: dict):
        super().__init__(f"host suspended for {slept_s:.1f}s")
        self.slept_s, self.restart, self.flags = slept_s, restart, flags


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _probe(adapter: SystemAdapter) -> bool:
    try:
        return adapter.alive()
    except Exception:  # noqa: BLE001 - a probe that raises means not alive
        return False


def _lingering(adapter: SystemAdapter) -> int:
    """Queries still running after cancel(), polled for up to LINGER_WAIT_S."""
    deadline = time.monotonic() + LINGER_WAIT_S
    count = 0
    while True:
        try:
            count = adapter.lingering_queries()
        except Exception:  # noqa: BLE001 - an unreachable server is handled by the probe
            return 0
        if count == 0 or time.monotonic() >= deadline:
            return count
        time.sleep(LINGER_POLL_S)


def _diagnose(adapter: SystemAdapter, outcome: QueryOutcome,
              extra: dict) -> tuple[str, dict, bool]:
    """Final status, container details and whether a restart is needed for a failed query.
    A timeout that leaves a query running is flagged in ``extra`` and needs a restart.

    Nothing here restarts the server: the record is written first (see run_power).
    """
    if outcome.status == "crashed":
        return "crashed", adapter.crash_info(), True
    if outcome.status == "timeout":
        cancelled = adapter.cancel()
        lingering = _lingering(adapter)
        if lingering:
            extra["lingering"] = lingering
        if not cancelled:
            extra["cancel_unavailable"] = True
        if lingering or not cancelled:
            extra["restarted_after_timeout"] = True
        return "timeout", {}, bool(lingering) or not cancelled or not _probe(adapter)
    if _probe(adapter):
        return outcome.status, {}, False
    info = adapter.crash_info()
    status = "error" if info.get("status") == "running" else "crashed"
    return status, info, True


def _execute(adapter: SystemAdapter, sql: str, timeout_s: int,
             slept: Callable[[], float]) -> QueryOutcome:
    """One run of ``sql``; raises _Interrupted if the host was suspended while it ran. A failed
    interrupted run is still cancelled and diagnosed, so the retry starts on a usable server."""
    before = slept()
    out = adapter.execute(sql, timeout_s)
    gap = slept() - before
    if gap <= SUSPEND_TOLERANCE_S:
        return out
    flags: dict = {}
    restart = _diagnose(adapter, out, flags)[2] if out.status != "ok" else False
    raise _Interrupted(gap, restart, flags)


def _diff_rows(diffs: list[tuple]) -> list[list]:
    return [[pos, repr(a), repr(b)] for pos, a, b in diffs]


def _measure(
    adapter: SystemAdapter,
    sql: str,
    expected: dict | None,
    drop_caches: Callable[[], None],
    timeout_s: int,
    warm_runs: int,
    slept: Callable[[], float],
) -> tuple[str, float | None, str | None, dict, bool]:
    """One query: (status, value, error, extra, restart_needed)."""
    drop_caches()
    cold = _execute(adapter, sql, timeout_s, slept)
    extra: dict = {**cold.extra, "cold_s": cold.elapsed_s, "warm_s": [], "pushed": cold.pushed}
    if cold.status != "ok":
        status, info, restart = _diagnose(adapter, cold, extra)
        if info:
            extra["container"] = info
        return status, None, cold.error, extra, restart

    extra["checksum"] = checksum(cold.rows or [], cold.numeric_cols)
    for _ in range(warm_runs):
        warm = _execute(adapter, sql, timeout_s, slept)
        if warm.status != "ok":
            status, info, restart = _diagnose(adapter, warm, extra)
            if info:
                extra["container"] = info
            return status, None, warm.error, extra, restart
        extra["warm_s"].append(warm.elapsed_s)

    value = statistics.median(extra["warm_s"]) if extra["warm_s"] else cold.elapsed_s
    status = "ok"
    if expected is not None:
        numeric = set(expected.get("numeric_cols", ())) | cold.numeric_cols
        extra["compared"] = True
        cmp = compare(expected["rows"], cold.rows or [], numeric)
        if not cmp.match:
            status = "wrong_result"
            extra["row_count"] = [cmp.row_count_a, cmp.row_count_b]
            extra["diffs"] = _diff_rows(cmp.diffs)
    return status, value, None, extra, False


def _restart(adapter: SystemAdapter) -> str | None:
    """Restart the server; the error text if that fails (never raised)."""
    try:
        adapter.restart()
    except Exception as exc:  # noqa: BLE001 - keep the campaign going
        return f"{type(exc).__name__}: {exc}"
    return None


def _measure_valid(adapter: SystemAdapter, item: str, measure: Callable[[], tuple],
                   clock: Callable[[], datetime]) -> tuple[str, tuple, list[dict], str | None]:
    """``measure()`` until no attempt was interrupted by a host suspend:
    (started_at, its result, one entry per discarded attempt, a restart error). An entry has
    ``host_suspended_s`` and the attempt's ``_diagnose`` flags (``restarted_after_timeout``,
    ``lingering``, ``cancel_unavailable``)."""
    discarded: list[dict] = []
    restart_error: str | None = None
    for _ in range(SUSPEND_RETRIES + 1):
        started = clock().isoformat()
        try:
            return started, measure(), discarded, restart_error
        except _Interrupted as exc:
            discarded.append({"host_suspended_s": round(exc.slept_s, 1), **exc.flags})
            if exc.restart:
                restart_error = _restart(adapter) or restart_error
    sleeps = [d["host_suspended_s"] for d in discarded]
    raise HostSuspended(
        f"{adapter.name} {item}: the host was suspended during each of {len(sleeps)} attempts "
        f"({sleeps} s); keep it awake and resume the run")


def run_power(
    adapter: SystemAdapter,
    sf: int,
    run_id: str,
    root: Path,
    reference: dict[int, dict] | None = None,
    *,
    queries: Iterable[int] = QUERY_NUMBERS,
    drop_caches: Callable[[], None] = dockerctl.drop_caches,
    clock: Callable[[], datetime] = _utcnow,
    host: Callable[[], dict] = hostinfo.host_info,
    timeout_s: int = QUERY_TIMEOUT_S,
    warm_runs: int = WARM_RUNS,
    query_text: Callable[[int], str] | None = None,
    extra_queries: Iterable[tuple[str, str, dict | None]] = (),
    suspended_s: Callable[[], float] | None = None,
) -> list[ResultRecord]:
    """Run the queries, write one M1 record per query under ``root`` and return them.

    ``reference`` maps a query number to ``{rows, numeric_cols, ...}`` (see
    ``dbench.reference``); results are compared with the union of both numeric columns.
    A record is written before any restart. A restart that raises never aborts the
    run: the error is attached to the next record as ``extra["restart_error"]``.

    ``query_text(n)`` replaces the default ``translate(load_query(n), adapter.dialect)`` (Track B
    substitutes Parquet readers for the table names). ``extra_queries`` are further
    ``(item, sql, expected)`` entries measured after the numbered queries, with the same
    cold/warm, timeout and restart handling. ``suspended_s()`` reports the host's total sleep
    time (default ``hostinfo.suspended_s``); a run during which it grows is discarded and the
    query measured again.
    """
    slept = suspended_s or hostinfo.suspended_s
    host_facts = host()
    versions = adapter.versions()
    records: list[ResultRecord] = []
    restart_error: str | None = None

    text = query_text or (lambda n: translate(load_query(n), adapter.dialect))
    work = [(f"q{n:02d}", text(n), reference.get(n) if reference is not None else None)
            for n in queries]
    work.extend(extra_queries)

    for item, sql, expected in work:
        started, result, discarded, retry_error = _measure_valid(
            adapter, item,
            lambda sql=sql, expected=expected: _measure(
                adapter, sql, expected, drop_caches, timeout_s, warm_runs, slept),
            clock)
        status, value, error, extra, restart = result
        if discarded:
            extra["host_suspended_s"] = [d["host_suspended_s"] for d in discarded]
            extra["discarded_attempts"] = discarded
        restart_error = restart_error or retry_error
        if restart_error:
            extra["restart_error"] = restart_error
            restart_error = None

        rec = ResultRecord(
            system=adapter.name,
            track=adapter.track,
            measure="M1",
            sf=sf,
            item=item,
            status=status,
            value=value,
            unit="s",
            config_ref=f"systems/{adapter.config_dir}/",
            run_id=run_id,
            started_at=started,
            extra=extra,
            error=error,
            versions=versions,
            host=host_facts,
        )
        write(rec, root)
        records.append(rec)

        if restart:
            restart_error = _restart(adapter)
    return records
