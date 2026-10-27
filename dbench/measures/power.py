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


def _recover(adapter: SystemAdapter, outcome: QueryOutcome) -> dict:
    """Make the server usable again after a failed query; return crash details if any."""
    info: dict = {}
    if outcome.status == "crashed":
        info = adapter.crash_info()
        adapter.restart()
        return info
    if outcome.status == "timeout":
        adapter.cancel()
    if not adapter.alive():
        adapter.restart()
    return info


def _diff_rows(diffs: list[tuple]) -> list[list]:
    return [[pos, repr(a), repr(b)] for pos, a, b in diffs]


def run_power(
    adapter: SystemAdapter,
    sf: int,
    run_id: str,
    root: Path,
    reference: dict[int, list] | None = None,
    *,
    queries: Iterable[int] = QUERY_NUMBERS,
    drop_caches: Callable[[], None] = dockerctl.drop_caches,
    clock: Callable[[], datetime] = _utcnow,
    host: Callable[[], dict] = hostinfo.host_info,
    timeout_s: int = QUERY_TIMEOUT_S,
    warm_runs: int = WARM_RUNS,
) -> list[ResultRecord]:
    """Run the queries, write one M1 record per query under ``root`` and return them."""
    host_facts = host()
    versions = adapter.versions()
    records: list[ResultRecord] = []

    for n in queries:
        started = clock().isoformat()
        sql = translate(load_query(n), adapter.dialect)
        drop_caches()
        cold = adapter.execute(sql, timeout_s)
        extra: dict = {"cold_s": cold.elapsed_s, "warm_s": [], "pushed": cold.pushed}
        status, value, error = "ok", None, cold.error

        if cold.status != "ok":
            status = cold.status
            crash = _recover(adapter, cold)
            if crash:
                extra["container"] = crash
        else:
            extra["checksum"] = checksum(cold.rows or [], cold.numeric_cols)
            for _ in range(warm_runs):
                warm = adapter.execute(sql, timeout_s)
                if warm.status != "ok":
                    status, error = warm.status, warm.error
                    crash = _recover(adapter, warm)
                    if crash:
                        extra["container"] = crash
                    break
                extra["warm_s"].append(warm.elapsed_s)
            if status == "ok":
                value = statistics.median(extra["warm_s"]) if extra["warm_s"] else cold.elapsed_s
                if reference is not None and n in reference:
                    cmp = compare(reference[n], cold.rows or [], cold.numeric_cols)
                    if not cmp.match:
                        status = "wrong_result"
                        extra["row_count"] = [cmp.row_count_a, cmp.row_count_b]
                        extra["diffs"] = _diff_rows(cmp.diffs)

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
    return records
