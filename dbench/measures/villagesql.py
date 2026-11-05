"""Track B: TPC-H over Parquet through each system's raw DuckDB entry point.

Plain DuckDB 1.5.5 reads the files directly; VillageSQL calls ``duckdb_query()``, pg_duckdb
``duckdb.query()`` and MariaDB ``run_in_duckdb()``. Every system runs the same DuckDB-dialect text
(``dbench.queries.trackb``) and is compared against the plain DuckDB rows. VillageSQL and pg_duckdb
also run the hybrid joins (``dbench.queries.hybrid``): DuckDB work over Parquet joined to tables
in the server's row store (InnoDB, heap), each in its documented pattern; the other systems
record them as ``n/a``. A system that cannot start is recorded as ``failed`` for every item,
never dropped.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dbench import dockerctl, hostinfo
from dbench.config import QUERY_TIMEOUT_S, WARM_RUNS, Limits
from dbench.datagen import DatasetPaths
from dbench.measures.power import QUERY_NUMBERS, SUSPEND_REASON, HostSuspended, run_power
from dbench.measures.throughput import ITEM_FORMAT, run_throughput
from dbench.queries.hybrid import HYBRID_ITEMS, HYBRID_JOINS
from dbench.queries.trackb import default_globs, trackb_query
from dbench.reference import build_reference
from dbench.results import ResultRecord, cell_record, is_done, write
from dbench.systems.base import SystemAdapter
from dbench.systems.registry import get_adapter
from dbench.systems.trackb import DuckDBTrackB

REFERENCE_SYSTEM = "duckdb-1.5.5"
TRACK_B_SYSTEMS = (REFERENCE_SYSTEM, "villagesql", "pg_duckdb-trackb", "mariadb-trackb")
JOINS_BY_ITEM = {j.item: j for j in HYBRID_JOINS}
# Systems that run the hybrid joins: the statement each one sends (see dbench.queries.hybrid).
JOIN_STATEMENT = {"villagesql": lambda j: j.villagesql, "pg_duckdb-trackb": lambda j: j.pg_duckdb}
# The InnoDB/heap tables the joins use are loaded with the server before it is measured.
HYBRID_SYSTEMS = tuple(JOIN_STATEMENT)
JOIN_NA_REASONS = {
    "duckdb": "plain DuckDB has no database table to join; the hybrid joins are measured on "
              "VillageSQL (JSON_TABLE over duckdb_query()) and pg_duckdb (read_parquet() joined "
              "to heap tables)",
    "mariadb": "run_in_duckdb returns text with no way to join it to an InnoDB table in SQL",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def make_adapter(name: str, data_dir) -> SystemAdapter:
    """Track B adapter for ``name``: plain DuckDB 1.5.5 reads files directly, the others are
    the registry's servers (``pg_duckdb-trackb`` and ``mariadb-trackb`` use their raw entry
    points)."""
    if name.startswith("duckdb-"):
        return DuckDBTrackB(name.removeprefix("duckdb-"), data_dir=data_dir)
    return get_adapter(name, data_dir)


def _na_group(name: str) -> str:
    return "duckdb" if name.startswith("duckdb-") else name.removesuffix("-trackb")


def _record(adapter: SystemAdapter, plan, item: str, status: str, host: dict, *,
            reason: str | None = None, error: str | None = None,
            extra: dict | None = None, measure: str = "M1") -> ResultRecord:
    return ResultRecord(
        system=adapter.name, track=adapter.track, measure=measure, sf=plan.sf, item=item,
        status=status, value=None, unit="qph" if measure == "M4" else "s",
        config_ref=f"systems/{adapter.config_dir}/",
        run_id=plan.run_id, started_at=plan.clock().isoformat(), error=error, reason=reason,
        host=host, extra=extra or {},
    )


def _all_items(queries: Iterable[int]) -> list[str]:
    return [f"q{n:02d}" for n in queries] + list(HYBRID_ITEMS)


def _cells(plan) -> list[tuple[str, str]]:
    """(measure, item) of every Track B system: M1 queries and joins, then M4 stream counts."""
    return [("M1", item) for item in _all_items(plan.queries)] + \
        [("M4", ITEM_FORMAT.format(n=n)) for n in plan.streams]


def _pending(adapter: SystemAdapter, plan, measure: str | None = None) -> list:
    """Cells of this system with no record yet (or only an automatic failure): items of
    ``measure``, or (measure, item) pairs of every measure."""
    cells = [(m, item) for m, item in _cells(plan)
             if not is_done(plan.root, adapter.name, m, plan.sf, item, plan.run_id)
             or _stale_join(adapter, plan, m, item)]
    return cells if measure is None else [item for m, item in cells if m == measure]


def _stale_join(adapter: SystemAdapter, plan, measure: str, item: str) -> bool:
    """An ``n/a`` hybrid join of a system that now runs it (pg_duckdb's ``join_innodb`` was n/a
    before the hybrid joins were added): measured again on resume, replacing the n/a."""
    if measure != "M1" or item not in JOINS_BY_ITEM or adapter.name not in JOIN_STATEMENT:
        return False
    rec = cell_record(plan.root, adapter.name, "M1", plan.sf, item, plan.run_id)
    return rec is not None and rec.status == "n/a"


def _failed_records(adapter: SystemAdapter, plan, exc: Exception,
                    host: dict) -> list[ResultRecord]:
    """Every item still pending for a system that did not start or finish: ``failed`` with the
    error. Items already measured are left alone. ``extra.auto`` marks it as the runner's own
    finding, which a resumed campaign retries."""
    if isinstance(exc, HostSuspended):
        reason = f"{SUSPEND_REASON}: {exc}"
    else:
        reason = f"{adapter.name} did not start, load or finish: {type(exc).__name__}: {exc}"
    return [
        _record(adapter, plan, item, "failed", host, reason=reason, error=str(exc)[:2000],
                extra={"auto": True}, measure=measure)
        for measure, item in _pending(adapter, plan)
    ]


def _na_join_records(adapter: SystemAdapter, plan, host: dict,
                     items: list[str]) -> list[ResultRecord]:
    if not items:
        return []
    reason = JOIN_NA_REASONS[_na_group(adapter.name)]
    return [_record(adapter, plan, item, "n/a", host, reason=reason) for item in items]


@dataclass
class _Plan:
    """Everything one Track B run shares between systems."""

    sf: int
    run_id: str
    root: Path
    limits: Limits
    data: DatasetPaths
    queries: list[int]
    text: Callable[[int], str]
    timeout_s: int
    warm_runs: int
    drop_caches: Callable[[], None]
    clock: Callable[[], datetime]
    host: Callable[[], dict]
    streams: tuple[int, ...] = ()  # M4 stream counts; empty: Track B runs M1 only
    reference: dict | None = None
    join_references: dict[str, dict | None] = field(default_factory=dict)


def _start(adapter: SystemAdapter, plan: _Plan, joins_pending: bool) -> None:
    adapter.start(plan.limits)
    if joins_pending and adapter.name in HYBRID_SYSTEMS:  # the tables the joins read
        adapter.create_schema(plan.sf, "rowstore")
        load = adapter.load_bulk(plan.data)
        if load.status != "ok":
            raise RuntimeError(f"hybrid-join tables failed to load: {load.error} {load.extra}")


def _run_system(adapter: SystemAdapter, plan: _Plan) -> list[ResultRecord]:
    """Start one system, measure the 22 queries and the joins (where they apply), then M4 at each
    stream count, always clean up."""
    host_facts = plan.host()
    if not _pending(adapter, plan):
        return []
    pending = _pending(adapter, plan, "M1")
    adapter.remove_data()
    try:
        try:
            _start(adapter, plan, any(item in JOINS_BY_ITEM for item in pending))
        except Exception as exc:  # noqa: BLE001 - a system that cannot start is a result
            records = _failed_records(adapter, plan, exc, host_facts)
            for rec in records:
                write(rec, plan.root)
            return records
        statement = JOIN_STATEMENT.get(adapter.name)
        joins = [(item, statement(JOINS_BY_ITEM[item]), plan.join_references.get(item))
                 for item in HYBRID_ITEMS if item in pending] if statement else []
        queries = [n for n in plan.queries if f"q{n:02d}" in pending]
        try:
            records = run_power(
                adapter, plan.sf, plan.run_id, plan.root, plan.reference, queries=queries,
                drop_caches=plan.drop_caches, clock=plan.clock, host=plan.host,
                timeout_s=plan.timeout_s, warm_runs=plan.warm_runs, query_text=plan.text,
                extra_queries=joins)
        except Exception as exc:  # noqa: BLE001 - one system must not end the Track B run
            records = _failed_records(adapter, plan, exc, host_facts)
            for rec in records:
                write(rec, plan.root)
            return records
        na_items = [item for item in HYBRID_ITEMS if item in pending] if not statement else []
        for rec in _na_join_records(adapter, plan, host_facts, na_items):
            write(rec, plan.root)
            records.append(rec)
        return records + _run_streams(adapter, plan, host_facts)
    finally:
        adapter.remove_data()


def _run_streams(adapter: SystemAdapter, plan: _Plan, host_facts: dict) -> list[ResultRecord]:
    """M4 over Parquet: the 22 Track B queries at each pending stream count (the hybrid joins
    are M1 only). A system that fails here keeps its M1 records."""
    records: list[ResultRecord] = []
    for item in _pending(adapter, plan, "M4"):
        streams = int(item.removeprefix("streams="))
        try:
            records.append(run_throughput(
                adapter, plan.sf, streams, plan.run_id, plan.root, clock=plan.clock,
                host=plan.host, timeout_s=plan.timeout_s, query_text=plan.text))
        except Exception as exc:  # noqa: BLE001 - one system must not end the Track B run
            failed = _failed_records(adapter, plan, exc, host_facts)
            for rec in failed:
                write(rec, plan.root)
            return records + failed
    return records


def _reference(adapter, sf, run_id, root, queries, timeout_s, text):
    """Plain DuckDB rows for the numbered queries and the hybrid joins (cached per run)."""
    ref = build_reference(adapter, sf, run_id, root, queries, timeout_s, query_text=text)
    joins: dict[str, dict | None] = {}
    for j in HYBRID_JOINS:
        res = adapter.execute(j.reference, timeout_s)
        joins[j.item] = None if res.status != "ok" else {
            "rows": list(res.rows or []), "numeric_cols": set(res.numeric_cols),
            "columns": list(res.columns)}
    return ref, joins


def run_track_b(
    sf: int,
    run_id: str,
    root: Path,
    systems: Iterable[str] = TRACK_B_SYSTEMS,
    *,
    data: DatasetPaths,
    limits: Limits | None = None,
    queries: Iterable[int] = QUERY_NUMBERS,
    timeout_s: int = QUERY_TIMEOUT_S,
    warm_runs: int = WARM_RUNS,
    drop_caches: Callable[[], None] = dockerctl.drop_caches,
    clock: Callable[[], datetime] = _utcnow,
    host: Callable[[], dict] = hostinfo.host_info,
    adapter_factory: Callable[[str, Path], SystemAdapter] = make_adapter,
    streams: Iterable[int] = (),
) -> list[ResultRecord]:
    """Run Track B for ``systems`` one at a time and return all records (written under ``root``).

    The reference rows come from plain DuckDB 1.5.5 over the same Parquet files; every system's
    ok result is compared with them (``wrong_result`` when it differs). With ``streams`` each
    system also runs M4 (the 22 queries, concurrently) at each stream count.
    """
    plan = _Plan(sf, run_id, root, limits or Limits(), data, list(queries),
                 lambda n: trackb_query(n, default_globs()), timeout_s, warm_runs, drop_caches,
                 clock, host, streams=tuple(streams))
    ref_adapter = adapter_factory(REFERENCE_SYSTEM, data.root)
    try:
        ref_adapter.remove_data()
        ref_adapter.start(plan.limits)
        try:
            plan.reference, plan.join_references = _reference(
                ref_adapter, sf, run_id, root, plan.queries, timeout_s, plan.text)
        finally:
            ref_adapter.remove_data()
    except Exception as exc:  # noqa: BLE001 - no reference: every cell is an automatic failure
        records = []
        for name in systems:
            adapter = adapter_factory(name, data.root)
            failed = _failed_records(adapter, plan, exc, plan.host())
            for rec in failed:
                write(rec, root)
            records.extend(failed)
        return records

    records = []
    for name in systems:
        records.extend(_run_system(adapter_factory(name, data.root), plan))
    return records
