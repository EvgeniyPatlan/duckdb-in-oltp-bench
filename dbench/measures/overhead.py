"""M7: integration overhead of a Track A system against plain DuckDB.

For every Track A system and query the ratio is the system's M1 warm value divided by the M1
warm value of plain DuckDB *at the same DuckDB version* (the system's ``versions["duckdb"]``,
normalised, against the ``duckdb-<version>`` baselines). Nothing is compared across versions: a
system whose version has no plain run gets ``n/a``. A side that is not ``ok`` gives ``n/a`` with
the status named. Each system also gets a ``geomean`` item over its ok ratios. When the system's
DuckDB version differs from the campaign's reference version, ``extra.version_gap`` names the
reference version so the reader knows the plain baseline used is not the reference.

M7 records are derived data: ``run_overhead`` deletes the run's M7 records and writes them again
from the M1 records, so it can be repeated at any time.
"""
from __future__ import annotations

import math
from collections.abc import Iterable
from pathlib import Path

from dbench.results import ResultRecord, iter_records, remove, write

MEASURE = "M7"
UNIT = "ratio"
GEOMEAN_ITEM = "geomean"
PLAIN_PREFIX = "duckdb-"
REFERENCE_VERSION = "1.5.5"


def normalise_version(version: str | None) -> str:
    """``" v1.5.5"`` -> ``"1.5.5"``; a missing version is ``"unknown"``."""
    text = (version or "").strip().removeprefix("v").strip()
    return text or "unknown"


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


def _na_reason(sys_rec: ResultRecord, plain: ResultRecord | None, plain_name: str) -> str | None:
    if sys_rec.status != "ok" or sys_rec.value is None:
        return f"{sys_rec.system} M1 {sys_rec.item} is {sys_rec.status}: no ratio"
    if plain is None:
        return f"{plain_name} has no M1 record for {sys_rec.item}: no ratio"
    if plain.status != "ok" or not plain.value:
        return f"plain DuckDB {plain_name} M1 {sys_rec.item} is {plain.status}: no ratio"
    return None


def _item_record(sys_rec: ResultRecord, plain_run: dict[str, ResultRecord] | None,
                 version: str, reference_version: str) -> ResultRecord:
    extra: dict = {"duckdb_version": version}
    if version != reference_version:
        extra["version_gap"] = reference_version
    if plain_run is None:
        return _derived(sys_rec, sys_rec.item, "n/a", None,
                        f"no plain DuckDB run at version {version}", extra)
    plain = plain_run.get(sys_rec.item)
    plain_name = f"{PLAIN_PREFIX}{version}"
    extra["plain_system"] = plain_name
    reason = _na_reason(sys_rec, plain, plain_name)
    if reason is not None:
        return _derived(sys_rec, sys_rec.item, "n/a", None, reason, extra)
    assert plain is not None and plain.value and sys_rec.value is not None
    extra.update({"system_s": sys_rec.value, "plain_s": plain.value})
    return _derived(sys_rec, sys_rec.item, "ok", sys_rec.value / plain.value, None, extra)


def _geomean(base: ResultRecord, items: list[ResultRecord], version: str,
             reference_version: str) -> ResultRecord:
    ok = [r.value for r in items if r.status == "ok" and r.value]
    excluded = sorted(r.item for r in items if r.status != "ok")
    extra: dict = {"duckdb_version": version, "queries_used": len(ok), "excluded": excluded}
    if version != reference_version:
        extra["version_gap"] = reference_version
    if not ok:
        reasons = sorted({r.reason for r in items if r.reason})
        reason = "no query has an ok ratio" + (f" ({reasons[0]})" if reasons else "")
        return _derived(base, GEOMEAN_ITEM, "n/a", None, reason, extra)
    value = math.exp(sum(math.log(v) for v in ok) / len(ok))
    return _derived(base, GEOMEAN_ITEM, "ok", value, None, extra)


def compute_overhead(records: Iterable[ResultRecord],
                     reference_version: str = REFERENCE_VERSION) -> list[ResultRecord]:
    """The M7 records of every Track A system found in ``records`` (M1 records only are used)."""
    records = list(records)
    plain = _plain_index(records)
    groups: dict[tuple[str, int], list[ResultRecord]] = {}
    for r in records:
        if r.measure == "M1" and r.track == "A":
            groups.setdefault((r.system, r.sf), []).append(r)
    out: list[ResultRecord] = []
    for (_, sf), recs in sorted(groups.items()):
        recs = sorted(recs, key=lambda r: r.item)
        version = normalise_version(recs[0].versions.get("duckdb"))
        items = [_item_record(r, plain.get((version, sf)), version, reference_version)
                 for r in recs]
        out += [*items, _geomean(recs[0], items, version, reference_version)]
    return out


def run_overhead(root: Path, run_id: str,
                 reference_version: str = REFERENCE_VERSION) -> list[ResultRecord]:
    """Rewrite the M7 records of ``run_id`` from its M1 records; returns the new records."""
    root = Path(root)
    stored = [r for r in iter_records(root / run_id) if r.run_id == run_id]
    for stale in (r for r in stored if r.measure == MEASURE):
        remove(root, stale)
    fresh = compute_overhead((r for r in stored if r.measure == "M1"), reference_version)
    for rec in fresh:
        write(rec, root)
    return fresh
