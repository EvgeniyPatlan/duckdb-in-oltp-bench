"""M8: silent slow paths, derived from a system's own M1 and M5 records.

Three cells per system:

``fallback_share_tpch``    share of the M1 queries that ran on the row path (``pushed=fallback``)
``fallback_share_probes``  the same over the M5 read probes that ran
``visibility``             1 when the adapter can observe pushdown at all, else 0;
                           ``extra.mechanism`` is ``status_counter``, ``explain`` or ``none``

Both shares count the statements that ran (status ``ok`` or ``wrong_result``). DML and DDL probes
are left out of the probe share and its ``not_run`` count: their pushdown is the follow-up SELECT's,
not the statement under test (see ``extra.check_pushed``).

A statement whose pushdown could not be told is counted as ``unknown`` and never as ``duckdb``;
``extra.unobserved_share`` says how much of a share rests on unknowns. A system with no way to
observe pushdown has no share (``n/a``): reporting 0% fallback there would mean nothing.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING

from dbench.measures.records import make_record
from dbench.results import ResultRecord, iter_records
from dbench.systems.base import SystemAdapter

if TYPE_CHECKING:
    from dbench.measures.registry import MeasureContext

ITEMS = ("fallback_share_tpch", "fallback_share_probes", "visibility")
UNIT = "ratio"
MECHANISM = {"session": "status_counter", "global": "status_counter", "explain": "explain",
             "none": "none"}
GLOBAL_NOTE = ("ambiguous under concurrency: the counter is server-wide, so it is read here "
               "only because probes and the power run are single-threaded")
NO_OBSERVATION = ("this system exposes no status counter or plan that shows whether DuckDB ran "
                  "a statement")
PUSHED = ("duckdb", "fallback", "unknown")
RAN = ("ok", "wrong_result")
WRITE_CATEGORIES = ("dml", "ddl")


def _share(pushed: Iterable[str], scope: str, label: str) -> dict:
    """Cell content for one share: fallback / (duckdb + fallback + unknown)."""
    counts = Counter("unknown" if p not in ("duckdb", "fallback") else p for p in pushed)
    total = sum(counts.values())
    full = {k: counts.get(k, 0) for k in PUSHED}
    if scope == "none":
        return {"status": "n/a", "value": None, "reason": NO_OBSERVATION,
                "extra": {"counts": full, "scope": scope}}
    if total == 0:
        return {"status": "n/a", "value": None, "reason": f"no {label} ran to measure",
                "extra": {"counts": full, "scope": scope}}
    return {"status": "ok", "value": full["fallback"] / total,
            "extra": {"counts": full, "total": total, "scope": scope,
                      "unobserved_share": full["unknown"] / total}}


def summarize_fallback(records: Iterable[ResultRecord], scope: str) -> dict[str, dict]:
    """The three M8 cells (``status``, ``value``, ``extra``, ``reason``) of one system from its
    M1 and M5 records; ``scope`` is the adapter's ``pushdown_counter_scope``."""
    records = list(records)
    m1 = [r.extra.get("pushed") for r in records if r.measure == "M1" and r.status in RAN]
    reads = [r for r in records if r.measure == "M5"
             and r.extra.get("category") not in WRITE_CATEGORIES]
    m5_ran = [r for r in reads if r.status in RAN]
    tpch = _share(m1, scope, "TPC-H queries")
    probes = _share([r.extra.get("pushed") for r in m5_ran], scope, "probes")
    not_run = Counter(r.extra.get("class", r.status) for r in reads
                      if r.status not in (*RAN, "n/a"))
    if not_run:
        probes["extra"]["not_run"] = dict(not_run)
    mechanism = MECHANISM[scope]
    visibility_extra = {"mechanism": mechanism, "scope": scope}
    if scope == "global":
        visibility_extra["note"] = GLOBAL_NOTE
    return {
        "fallback_share_tpch": tpch,
        "fallback_share_probes": probes,
        "visibility": {"status": "ok", "value": 0 if scope == "none" else 1,
                       "extra": visibility_extra},
    }


def run_fallback(adapter: SystemAdapter, ctx: MeasureContext) -> list[ResultRecord]:
    """Write the M8 cells in ``ctx.items`` from the system's M1 and M5 records of this run."""
    own = [r for r in iter_records(ctx.results / ctx.run_id)
           if r.system == adapter.name and r.run_id == ctx.run_id and r.sf == ctx.sf]
    cells = summarize_fallback(own, adapter.pushdown_counter_scope)
    host_facts = ctx.host()
    out = []
    for item in ITEMS:
        if item not in ctx.items:
            continue
        cell = cells[item]
        out.append(make_record(
            adapter, measure="M8", item=item, status=cell["status"], sf=ctx.sf,
            run_id=ctx.run_id, root=ctx.results, unit=UNIT, value=cell["value"],
            extra=cell["extra"], reason=cell.get("reason"), host=lambda: host_facts))
    return out
