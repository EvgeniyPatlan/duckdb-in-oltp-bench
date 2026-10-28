"""Track B: TPC-H over Parquet through each system's raw DuckDB entry point.

Plain DuckDB 1.5.5 reads the files directly; VillageSQL calls ``duckdb_query()``, pg_duckdb
``duckdb.query()`` and MariaDB ``run_in_duckdb()``. Every system runs the same DuckDB-dialect text
(``dbench.queries.trackb``) and is compared against the plain DuckDB rows. VillageSQL also runs
its documented pattern: a ``duckdb_query()`` aggregate over Parquet joined to an InnoDB table
through ``JSON_TABLE`` (item ``join_innodb``). A system that cannot start is recorded as
``failed`` for every item, never dropped.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dbench import dockerctl, hostinfo
from dbench.config import QUERY_TIMEOUT_S, WARM_RUNS, Limits
from dbench.datagen import DatasetPaths
from dbench.measures.power import QUERY_NUMBERS, run_power
from dbench.queries.trackb import default_globs, trackb_query
from dbench.reference import build_reference
from dbench.results import ResultRecord, write
from dbench.systems.base import SystemAdapter
from dbench.systems.registry import get_adapter
from dbench.systems.trackb import DuckDBTrackB
from dbench.systems.villagesql import NativeStatement, escape_sql_literal

REFERENCE_SYSTEM = "duckdb-1.5.5"
TRACK_B_SYSTEMS = (REFERENCE_SYSTEM, "villagesql", "pg_duckdb-trackb", "mariadb-trackb")
JOIN_ITEM = "join_innodb"
CUSTOMER_PARQUET = default_globs()["customer"]
NATION_PARQUET = default_globs()["nation"]

# DuckDB side of the join: customers and their balance per nation (the aggregate VillageSQL
# computes in duckdb_query()).
_AGGREGATE = (
    "SELECT c_nationkey, count(*) AS customers, sum(c_acctbal) AS balance "
    f"FROM read_parquet('{CUSTOMER_PARQUET}') AS customer GROUP BY c_nationkey"
)
# VillageSQL: the aggregate through duckdb_query(), its JSON turned into rows by JSON_TABLE and
# joined to the InnoDB ``nation`` table (the pattern the extension documents).
JOIN_VILLAGESQL = NativeStatement(
    "SELECT n.n_name AS nation, t.customers, t.balance "
    f"FROM JSON_TABLE(duckdb_query({escape_sql_literal(_AGGREGATE)}), '$[*]' COLUMNS ("
    "c_nationkey INT PATH '$.c_nationkey', customers BIGINT PATH '$.customers', "
    "balance DECIMAL(38,2) PATH '$.balance')) AS t "
    "JOIN nation n ON n.n_nationkey = t.c_nationkey ORDER BY n.n_name"
)
# The same result computed entirely in plain DuckDB, the reference for ``join_innodb``.
JOIN_REFERENCE = (
    "SELECT n_name AS nation, count(*) AS customers, sum(c_acctbal) AS balance "
    f"FROM read_parquet('{CUSTOMER_PARQUET}') AS customer "
    f"JOIN read_parquet('{NATION_PARQUET}') AS nation ON n_nationkey = c_nationkey "
    "GROUP BY n_name ORDER BY n_name"
)
JOIN_NA_REASONS = {
    "villagesql": None,
    "duckdb": "plain DuckDB has no MySQL table to join; the join is VillageSQL's documented "
              "JSON_TABLE pattern",
    "pg_duckdb": "pg_duckdb's Track B path reads Parquet only; joining Postgres tables is "
                 "Track A pg_duckdb, and the JSON_TABLE join is VillageSQL's pattern",
    "mariadb": "run_in_duckdb returns text with no way to join it to an InnoDB table in SQL; the "
               "JSON_TABLE join is VillageSQL's pattern",
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
            extra: dict | None = None) -> ResultRecord:
    return ResultRecord(
        system=adapter.name, track=adapter.track, measure="M1", sf=plan.sf, item=item,
        status=status, value=None, unit="s", config_ref=f"systems/{adapter.config_dir}/",
        run_id=plan.run_id, started_at=plan.clock().isoformat(), error=error, reason=reason,
        host=host, extra=extra or {},
    )


def _all_items(queries: Iterable[int]) -> list[str]:
    return [f"q{n:02d}" for n in queries] + [JOIN_ITEM]


def _failed_records(adapter: SystemAdapter, plan, exc: Exception,
                    host: dict) -> list[ResultRecord]:
    """Every item of a system that did not start or finish: ``failed`` with the error.
    ``extra.auto`` marks it as the runner's own finding, which a resumed campaign retries."""
    reason = f"{adapter.name} did not start or load: {type(exc).__name__}: {exc}"
    return [
        _record(adapter, plan, item, "failed", host, reason=reason, error=str(exc)[:2000],
                extra={"auto": True})
        for item in _all_items(plan.queries)
    ]


def _na_join_record(adapter: SystemAdapter, plan, host: dict) -> ResultRecord:
    return _record(adapter, plan, JOIN_ITEM, "n/a", host,
                   reason=JOIN_NA_REASONS[_na_group(adapter.name)])


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
    reference: dict | None = None
    join_reference: dict | None = None


def _start(adapter: SystemAdapter, plan: _Plan) -> None:
    adapter.start(plan.limits)
    if adapter.name == "villagesql":  # nation is the one table that lives in MySQL
        adapter.create_schema(plan.sf, "engine")
        load = adapter.load_bulk(plan.data)
        if load.status != "ok":
            raise RuntimeError(f"nation load failed: {load.error} {load.extra}")


def _run_system(adapter: SystemAdapter, plan: _Plan) -> list[ResultRecord]:
    """Start one system, measure the 22 queries (and the join where it applies), always clean up."""
    host_facts = plan.host()
    adapter.remove_data()
    try:
        try:
            _start(adapter, plan)
        except Exception as exc:  # noqa: BLE001 - a system that cannot start is a result
            records = _failed_records(adapter, plan, exc, host_facts)
            for rec in records:
                write(rec, plan.root)
            return records
        joins = [(JOIN_ITEM, JOIN_VILLAGESQL, plan.join_reference)] \
            if adapter.name == "villagesql" else []
        try:
            records = run_power(
                adapter, plan.sf, plan.run_id, plan.root, plan.reference, queries=plan.queries,
                drop_caches=plan.drop_caches, clock=plan.clock, host=plan.host,
                timeout_s=plan.timeout_s, warm_runs=plan.warm_runs, query_text=plan.text,
                extra_queries=joins)
        except Exception as exc:  # noqa: BLE001 - one system must not end the Track B run
            records = _failed_records(adapter, plan, exc, host_facts)
            for rec in records:
                write(rec, plan.root)
            return records
        if not joins:
            rec = _na_join_record(adapter, plan, host_facts)
            write(rec, plan.root)
            records.append(rec)
        return records
    finally:
        adapter.remove_data()


def _reference(adapter, sf, run_id, root, queries, timeout_s, text):
    """Plain DuckDB rows for the numbered queries and the join (cached per run)."""
    ref = build_reference(adapter, sf, run_id, root, queries, timeout_s, query_text=text)
    res = adapter.execute(JOIN_REFERENCE, timeout_s)
    join = None
    if res.status == "ok":
        join = {"rows": list(res.rows or []), "numeric_cols": set(res.numeric_cols),
                "columns": list(res.columns)}
    return ref, join


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
) -> list[ResultRecord]:
    """Run Track B for ``systems`` one at a time and return all records (written under ``root``).

    The reference rows come from plain DuckDB 1.5.5 over the same Parquet files; every system's
    ok result is compared with them (``wrong_result`` when it differs).
    """
    plan = _Plan(sf, run_id, root, limits or Limits(), data, list(queries),
                 lambda n: trackb_query(n, default_globs()), timeout_s, warm_runs, drop_caches,
                 clock, host)
    ref_adapter = adapter_factory(REFERENCE_SYSTEM, data.root)
    try:
        ref_adapter.remove_data()
        ref_adapter.start(plan.limits)
        try:
            plan.reference, plan.join_reference = _reference(
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
