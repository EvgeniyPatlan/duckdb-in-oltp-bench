"""Chart data per measure. One cell format for every system: value, status, reason, mark.

``mark`` is None for a normal bar, ``"failure"`` for a wrong result (never drawn as a bar) and
``"na"`` for n/a, failed, timeout, error and crashed cells (drawn as a hollow marker with the
reason on hover).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable

from dbench.results import ResultRecord

COMMON = "geomean_common"
OWN = "geomean"


def cell(rec: ResultRecord, **more) -> dict:
    mark = None if rec.status == "ok" else ("failure" if rec.status == "wrong_result" else "na")
    return {"v": rec.value, "s": rec.status, "r": rec.reason or rec.error, "mark": mark, **more}


def _of(records: Iterable[ResultRecord], measure: str) -> dict[int, list[ResultRecord]]:
    out: dict[int, list[ResultRecord]] = defaultdict(list)
    for r in records:
        if r.measure == measure:
            out[r.sf].append(r)
    return out


def _per_sf(records, measure: str, build: Callable[[list[ResultRecord]], dict]) -> dict:
    return {str(sf): build(recs) for sf, recs in sorted(_of(records, measure).items())}


def _nested(recs: list[ResultRecord], key: Callable[[ResultRecord], str],
            extra: Callable[[ResultRecord], dict] = lambda r: {}) -> dict:
    out: dict[str, dict] = defaultdict(dict)
    for r in recs:
        out[r.system][key(r)] = cell(r, **extra(r))
    return dict(out)


def _totals(cells: dict[str, dict[str, dict]], queries: list[str], common: list[str]) -> dict:
    out = {}
    for system, per in cells.items():
        ok = [q for q in queries if per.get(q, {}).get("s") == "ok"]
        bad = {q: per[q]["s"] for q in queries if q in per and per[q]["s"] != "ok"}
        entry = {"own_s": sum(per[q]["v"] for q in ok) if ok else None, "used": len(ok),
                 "total": len(queries), "partial": len(ok) < len(queries)}
        if not common:
            entry["common"] = {"v": None, "s": "n/a", "mark": "na",
                               "r": "no query is ok on every integration"}
        elif all(per.get(q, {}).get("s") == "ok" for q in common):
            entry["common"] = {"v": sum(per[q]["v"] for q in common), "s": "ok", "mark": None,
                               "r": None}
        else:
            why = ", ".join(f"{q}: {s}" for q, s in sorted(bad.items())
                            if q in common) or "common queries missing"
            entry["common"] = {"v": None, "s": "incomplete", "mark": "na", "r": why}
        out[system] = entry
    return out


def common_queries(cells: dict[str, dict[str, dict]], queries: list[str],
                   members: set[str]) -> tuple[list[str], list[str]]:
    """Queries ok on every member that has any ok query, and the members left out for having
    none (they would otherwise empty the set for everyone else)."""
    ok = {s: {q for q, c in cells[s].items() if c["s"] == "ok"} for s in members if s in cells}
    left_out = sorted(s for s, qs in ok.items() if not qs)
    sets = [qs for qs in ok.values() if qs]
    return ([q for q in queries if all(q in qs for qs in sets)] if sets else []), left_out


def power(records: list[ResultRecord], members: set[str]) -> dict:
    def build(recs: list[ResultRecord]) -> dict:
        cells = _nested(recs, lambda r: r.item, _query_extra)
        queries = sorted({r.item for r in recs})
        common, left_out = common_queries(cells, [q for q in queries if q.startswith("q")],
                                          members)
        return {"queries": queries, "cells": cells, "common_queries": common,
                "systems_left_out": left_out,
                "totals": _totals(cells, [q for q in queries if q.startswith("q")], common)}
    return _per_sf(records, "M1", build)


def _query_extra(r: ResultRecord) -> dict:
    return {"cold": r.extra.get("cold_s"), "pushed": r.extra.get("pushed")}


def load(records) -> dict:
    return _per_sf(records, "M2", lambda recs: _nested(
        recs, lambda r: r.item, lambda r: {"staging_s": r.extra.get("rowstore_load_s")}))


def storage(records) -> dict:
    return _per_sf(records, "M3", lambda recs: _nested(
        recs, lambda r: r.item, lambda r: {"breakdown": r.extra.get("breakdown")}))


def _stream_extra(r: ResultRecord) -> dict:
    return {"p50": r.extra.get("p50"), "p95": r.extra.get("p95"),
            "counts": r.extra.get("counts_by_status")}


def throughput(records) -> dict:
    return _per_sf(records, "M4", lambda recs: _nested(
        recs, lambda r: r.item.removeprefix("streams="), _stream_extra))


def sqlcompat(records) -> dict:
    def build(recs: list[ResultRecord]) -> dict:
        probes = {r.item: {"id": r.item, "category": r.extra.get("category", ""),
                           "title": r.extra.get("title", r.item)} for r in recs}
        cells = _nested(recs, lambda r: r.item, lambda r: {"c": r.extra.get("class", r.status)})
        counts: dict[str, dict[str, int]] = {}
        for system, per in cells.items():
            tally: dict[str, int] = defaultdict(int)
            for c in per.values():
                tally[c["c"]] += 1
            counts[system] = dict(tally)
        ordered = sorted(probes.values(), key=lambda p: (p["category"], p["id"]))
        return {"probes": ordered, "cells": cells, "counts": counts}
    return _per_sf(records, "M5", build)


def fallback(records) -> dict:
    def extra(r: ResultRecord) -> dict:
        e = r.extra
        return {"counts": e.get("counts"), "unobserved": e.get("unobserved_share"),
                "mechanism": e.get("mechanism"), "scope": e.get("scope"), "note": e.get("note")}
    return _per_sf(records, "M8", lambda recs: _nested(recs, lambda r: r.item, extra))


def _overhead_cell(r: ResultRecord) -> dict:
    e = r.extra
    return cell(r, item=r.item, used=e.get("queries_used"), total=e.get("queries_total"),
                partial=bool(e.get("partial")), plain=e.get("plain_system"),
                duckdb_version=e.get("duckdb_version"), version_gap=e.get("version_gap"),
                excluded=e.get("excluded"), left_out=e.get("systems_excluded"))


def overhead(records) -> dict:
    def build(recs: list[ResultRecord]) -> dict:
        out: dict[str, dict] = defaultdict(lambda: {"ratios": {}})
        for r in recs:
            entry = out[r.system]
            if r.item == COMMON:
                entry["headline"] = _overhead_cell(r)
            elif r.item == OWN:
                entry["own"] = _overhead_cell(r)
            else:
                entry["ratios"][r.item] = cell(r)
        return {s: e for s, e in out.items() if "headline" in e and "own" in e}
    return _per_sf(records, "M7", build)

