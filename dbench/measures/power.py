"""M1: TPC-H power run (one cold run and WARM_RUNS warm runs per query)."""
from __future__ import annotations

import statistics
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _probe(adapter: SystemAdapter) -> bool:
    try:
        return adapter.alive()
    except Exception:  # noqa: BLE001 - a probe that raises means not alive
        return False


def _diagnose(adapter: SystemAdapter, outcome: QueryOutcome) -> tuple[str, dict, bool]:
    """Final status, container details and whether a restart is needed for a failed query.

    Nothing here restarts the server: the record is written first (see run_power).
    """
    if outcome.status == "crashed":
        return "crashed", adapter.crash_info(), True
    if outcome.status == "timeout":
        adapter.cancel()
        return "timeout", {}, not _probe(adapter)
    if _probe(adapter):
        return outcome.status, {}, False
    info = adapter.crash_info()
    status = "error" if info.get("status") == "running" else "crashed"
    return status, info, True


def _diff_rows(diffs: list[tuple]) -> list[list]:
    return [[pos, repr(a), repr(b)] for pos, a, b in diffs]


def _measure(
    adapter: SystemAdapter,
    sql: str,
    expected: dict | None,
    drop_caches: Callable[[], None],
    timeout_s: int,
    warm_runs: int,
) -> tuple[str, float | None, str | None, dict, bool]:
    """One query: (status, value, error, extra, restart_needed)."""
    drop_caches()
    cold = adapter.execute(sql, timeout_s)
    extra: dict = {"cold_s": cold.elapsed_s, "warm_s": [], "pushed": cold.pushed}
    if cold.status != "ok":
        status, info, restart = _diagnose(adapter, cold)
        if info:
            extra["container"] = info
        return status, None, cold.error, extra, restart

    extra["checksum"] = checksum(cold.rows or [], cold.numeric_cols)
    for _ in range(warm_runs):
        warm = adapter.execute(sql, timeout_s)
        if warm.status != "ok":
            status, info, restart = _diagnose(adapter, warm)
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
) -> list[ResultRecord]:
    """Run the queries, write one M1 record per query under ``root`` and return them.

    ``reference`` maps a query number to ``{rows, numeric_cols, ...}`` (see
    ``dbench.reference``); results are compared with the union of both numeric columns.
    A record is written before any restart. A restart that raises never aborts the
    run: the error is attached to the next record as ``extra["restart_error"]``.
    """
    host_facts = host()
    versions = adapter.versions()
    records: list[ResultRecord] = []
    restart_error: str | None = None

    for n in queries:
        started = clock().isoformat()
        sql = translate(load_query(n), adapter.dialect)
        expected = reference.get(n) if reference is not None else None
        status, value, error, extra, restart = _measure(
            adapter, sql, expected, drop_caches, timeout_s, warm_runs
        )
        if restart_error:
            extra["restart_error"] = restart_error
            restart_error = None

        rec = ResultRecord(
            system=adapter.name,
            track=adapter.track,
            measure="M1",
            sf=sf,
            item=f"q{n:02d}",
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
            try:
                adapter.restart()
            except Exception as exc:  # noqa: BLE001 - keep the campaign going
                restart_error = f"{type(exc).__name__}: {exc}"
    return records
