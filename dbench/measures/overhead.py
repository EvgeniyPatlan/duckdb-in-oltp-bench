"""M7: integration overhead of a Track A system against plain DuckDB.

For every Track A system and query the ratio is the system's M1 warm value divided by the M1
warm value of plain DuckDB *at the same DuckDB version* (the record's ``versions["duckdb"]``,
normalised, against the ``duckdb-<version>`` baselines). Nothing is compared across versions: a
query whose version has no plain run gets ``n/a``; so does a record without a recorded version.
A side that is not ``ok`` gives ``n/a`` with the status named.

Each system also gets two summary items over its ok ratios:

* ``geomean``: every ok query of the system. A system that times out on its slow queries scores
  better on this item, so ``extra.partial`` is true when anything is excluded and
  ``queries_used``/``queries_total`` say how many queries went in. It is ``n/a`` when the ok
  ratios come from more than one DuckDB version.
* ``geomean_common``: only the queries where every Track A system of the run has an ok ratio
  (``extra.queries`` lists them), so systems are scored on the same queries.

``extra.version_gap`` names the reference version when a record's version differs from it, so the
reader knows the plain baseline used is not the reference one.

M7 records are derived data: ``run_overhead`` deletes the run's M7 records and writes them again
from the M1 records. The rewrite is not atomic; rerun to repair after an interruption.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path

from dbench.results import ResultRecord, iter_records, remove, write

MEASURE = "M7"
UNIT = "ratio"
GEOMEAN_ITEM = "geomean"
COMMON_ITEM = "geomean_common"
SUMMARY_ITEMS = (GEOMEAN_ITEM, COMMON_ITEM)
PLAIN_PREFIX = "duckdb-"
REFERENCE_VERSION = "1.5.5"
UNKNOWN = "unknown"
NO_VERSION = "DuckDB version not recorded"


def normalise_version(version: str | None) -> str:
    """``" v1.5.5"`` -> ``"1.5.5"``; a missing version is ``"unknown"``."""
    text = (version or "").strip().removeprefix("v").strip()
    return text or UNKNOWN


def _plain_index(records: Iterable[ResultRecord]) -> dict[tuple[str, int], dict[str, dict]]:
    """(normalised version, sf) -> {item: M1 record} of the plain DuckDB baselines."""
    index: dict[tuple[str, int], dict[str, dict]] = {}
    for r in records:
        if r.measure == "M1" and r.track == "baseline" and r.system.startswith(PLAIN_PREFIX):
            version = normalise_version(r.system.removeprefix(PLAIN_PREFIX))
            index.setdefault((version, r.sf), {})[r.item] = r
    return index


def _derived(base: ResultRecord, item: str, status: str, value: float | None, reason: str | None,
             extra: dict) -> ResultRecord:
    return ResultRecord(
        system=base.system, track=base.track, measure=MEASURE, sf=base.sf, item=item,
        status=status, value=value, unit=UNIT, config_ref=base.config_ref, run_id=base.run_id,
        started_at=base.started_at, extra=extra, reason=reason, versions=base.versions,
        host=base.host)


def _side_reason(rec: ResultRecord | None, label: str, item: str) -> str | None:
    """Why ``rec`` cannot be a ratio operand (None when it can)."""
    if rec is None:
        return f"{label} has no M1 record for {item}: no ratio"
    if rec.status != "ok":
        return f"{label} M1 {item} is {rec.status}: no ratio"
    if rec.value is None:
        return f"{label} M1 {item} has no value: no ratio"
    return None


def _ratio(sys_rec: ResultRecord, plain: ResultRecord | None,
           plain_name: str) -> tuple[float | None, str | None]:
    """(ratio, None) or (None, why there is no ratio)."""
    reason = (_side_reason(sys_rec, sys_rec.system, sys_rec.item)
              or _side_reason(plain, f"plain DuckDB {plain_name}", sys_rec.item))
    if reason is not None:
        return None, reason
    if plain is None or plain.value is None or sys_rec.value is None:  # unreachable: checked above
        return None, "no ratio"
    if plain.value == 0:
        return None, "plain DuckDB time is zero"
    return sys_rec.value / plain.value, None


def _item_record(sys_rec: ResultRecord, plain_by_version: dict[str, dict],
                 sf: int, reference_version: str) -> ResultRecord:
    version = normalise_version(sys_rec.versions.get("duckdb"))
    if version == UNKNOWN:
        return _derived(sys_rec, sys_rec.item, "n/a", None, NO_VERSION, {})
    extra: dict = {"duckdb_version": version}
    if version != reference_version:
        extra["version_gap"] = reference_version
    plain_run = plain_by_version.get((version, sf))
    if plain_run is None:
        return _derived(sys_rec, sys_rec.item, "n/a", None,
                        f"no plain DuckDB run at version {version}", extra)
    plain_name = f"{PLAIN_PREFIX}{version}"
    extra["plain_system"] = plain_name
    plain = plain_run.get(sys_rec.item)
    ratio, reason = _ratio(sys_rec, plain, plain_name)
    if ratio is None:
        return _derived(sys_rec, sys_rec.item, "n/a", None, reason, extra)
    extra.update({"system_s": sys_rec.value, "plain_s": plain.value if plain else None})
    return _derived(sys_rec, sys_rec.item, "ok", ratio, None, extra)


def _geomean_of(values: list[float]) -> float:
    return math.exp(sum(math.log(v) for v in values) / len(values))


def _versions(items: list[ResultRecord]) -> list[str]:
    return sorted({r.extra["duckdb_version"] for r in items if r.status == "ok"})


def _summary(base: ResultRecord, item: str, used: list[ResultRecord], total: int,
             extra: dict, empty_reason: str) -> ResultRecord:
    """A geomean record over the ok ``used`` items; n/a when empty or of mixed versions."""
    versions = _versions(used)
    extra = {**extra, "queries_used": len(used), "queries_total": total}
    if len(versions) > 1:
        return _derived(base, item, "n/a", None,
                        f"mixed DuckDB versions: {', '.join(versions)}", extra)
    if not used:
        return _derived(base, item, "n/a", None, empty_reason, extra)
    if versions:
        extra["duckdb_version"] = versions[0]
    return _derived(base, item, "ok", _geomean_of([r.value for r in used if r.value]), None,
                    extra)


def _geomean(base: ResultRecord, items: list[ResultRecord]) -> ResultRecord:
    used = [r for r in items if r.status == "ok"]
    excluded = sorted(r.item for r in items if r.status != "ok")
    extra: dict = {"excluded": excluded}
    if excluded:
        extra["partial"] = True
    reasons = sorted({r.reason for r in items if r.reason})
    empty = "no query has an ok ratio" + (f" ({reasons[0]})" if reasons else "")
    return _summary(base, GEOMEAN_ITEM, used, len(items), extra, empty)


def _common_queries(by_system: dict[str, list[ResultRecord]]) -> list[str]:
    """Queries with an ok ratio on every system of ``by_system``."""
    sets = [{r.item for r in items if r.status == "ok"} for items in by_system.values()]
    return sorted(set.intersection(*sets)) if sets else []


def _geomean_common(base: ResultRecord, items: list[ResultRecord],
                    common: list[str]) -> ResultRecord:
    used = [r for r in items if r.status == "ok" and r.item in common]
    reason = "no query has an ok ratio on every Track A system of the run"
    return _summary(base, COMMON_ITEM, used, len(common), {"queries": common}, reason)


def compute_overhead(records: Iterable[ResultRecord],
                     reference_version: str = REFERENCE_VERSION) -> list[ResultRecord]:
    """The M7 records of every Track A system found in ``records`` (M1 records only are used)."""
    records = list(records)
    plain = _plain_index(records)
    groups: dict[tuple[str, int], list[ResultRecord]] = {}
    for r in records:
        if r.measure == "M1" and r.track == "A":
            groups.setdefault((r.system, r.sf), []).append(r)
    per_system = {key: [_item_record(r, plain, key[1], reference_version)
                        for r in sorted(recs, key=lambda r: r.item)]
                  for key, recs in sorted(groups.items())}
    out: list[ResultRecord] = []
    for (_, sf), items in per_system.items():
        same_sf = {s: i for (s, f), i in per_system.items() if f == sf}
        common = _common_queries(same_sf)
        out += [*items, _geomean(items[0], items), _geomean_common(items[0], items, common)]
    return out


def format_summary(rec: ResultRecord) -> str:
    """One line for a ``geomean``/``geomean_common`` record, with the queries behind it."""
    used = rec.extra.get("queries_used", 0)
    total = rec.extra.get("queries_total", 0)
    note = ", partial" if rec.extra.get("partial") else ""
    head = f"{rec.system} {rec.item}"
    if rec.value is None:
        return f"{head}: n/a ({rec.reason}); {used}/{total} queries"
    return f"{head}: {rec.value:.3f}x ({used}/{total} queries{note})"


def run_overhead(root: Path, run_id: str,
                 reference_version: str = REFERENCE_VERSION) -> list[ResultRecord]:
    """Rewrite the M7 records of ``run_id`` from its M1 records; returns the new records.
    The rewrite is not atomic: rerun to repair."""
    root = Path(root)
    stored = [r for r in iter_records(root / run_id) if r.run_id == run_id]
    for stale in (r for r in stored if r.measure == MEASURE):
        remove(root, stale)
    fresh = compute_overhead((r for r in stored if r.measure == "M1"), reference_version)
    for rec in fresh:
        write(rec, root)
    return fresh
