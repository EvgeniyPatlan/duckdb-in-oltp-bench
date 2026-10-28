"""M2: load paths.

``load_bulk``     the system's native bulk path: ``create_schema`` plus ``load_bulk`` (the schema
                  kind is the system's own: row-store schema for the baselines, engine schema for
                  the engines), then the manifest row-count check done by the adapter.
``load_convert``  load row-store tables, then convert them to the DuckDB engine; only the
                  conversion is the value, the row-store staging load is ``extra.rowstore_load_s``.
                  Systems with nothing to convert get an ``n/a`` cell with the adapter's reason.

Each path starts from a fresh datadir. The campaign times its own bulk load with
``run_load_bulk`` (it needs that load anyway for the other measures) and runs ``run_convert``
last, on a new datadir that it removes afterwards.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from dbench.config import Limits
from dbench.datagen import DatasetPaths
from dbench.measures.records import make_record, utcnow
from dbench.results import ResultRecord
from dbench.systems.base import LoadOutcome, SystemAdapter

NO_REASON = "the adapter gave no reason"


def _from_outcome(adapter: SystemAdapter, outcome: LoadOutcome, item: str, *, elapsed_s: float,
                  extra: dict, **common) -> ResultRecord:
    if outcome.status == "ok":
        return make_record(adapter, item=item, status="ok", value=elapsed_s, extra=extra,
                           **common)
    if outcome.status == "n/a":
        reason = str(outcome.extra.get("reason") or NO_REASON)
        return make_record(adapter, item=item, status="n/a", reason=reason, extra=extra,
                           **common)
    return make_record(
        adapter, item=item, status=outcome.status, error=outcome.error,
        extra={**extra, "elapsed_s": outcome.elapsed_s}, **common)


def run_load_bulk(
    adapter: SystemAdapter, data: DatasetPaths, sf: int, run_id: str, root, *,
    timer: Callable[[], float] = time.perf_counter, clock: Callable[[], datetime] = utcnow,
    host: Callable[[], dict] | None = None,
) -> ResultRecord:
    """Create the schema and bulk-load on the started adapter; write and return the M2
    ``load_bulk`` cell. The value covers both steps. Exceptions propagate (the campaign records
    them as an automatic failure)."""
    started_at = clock()
    t0 = timer()
    adapter.create_schema(sf, adapter.bench_schema_kind)
    outcome = adapter.load_bulk(data)
    elapsed = timer() - t0
    checked = outcome.status == "ok" or outcome.extra.get("kind") == "row_count"
    extra = {**outcome.extra, "rows_checked": checked}
    return _from_outcome(
        adapter, outcome, "load_bulk", elapsed_s=elapsed, extra=extra, measure="M2", sf=sf,
        run_id=run_id, root=root, unit="s", clock=clock, host=host, started=started_at)


def run_convert(
    adapter: SystemAdapter, data: DatasetPaths, sf: int, run_id: str, root, limits: Limits, *,
    clock: Callable[[], datetime] = utcnow, host: Callable[[], dict] | None = None,
) -> ResultRecord:
    """The M2 ``load_convert`` cell on a fresh datadir (any earlier one is removed first, and
    the new one is removed afterwards)."""
    adapter.remove_data()
    try:
        adapter.start(limits)
        started_at = clock()
        outcome = adapter.load_convert(data)
        return _from_outcome(
            adapter, outcome, "load_convert", elapsed_s=outcome.elapsed_s or 0.0,
            extra=dict(outcome.extra), measure="M2", sf=sf, run_id=run_id, root=root, unit="s",
            clock=clock, host=host, started=started_at)
    finally:
        adapter.remove_data()
