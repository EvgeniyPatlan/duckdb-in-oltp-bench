"""Reference results: one run of each query on a trusted system, cached on disk.

Cache file ``<root>/<run_id>/_reference/q<NN>.json`` holds ``{rows, numeric_cols,
columns}`` with typed values ($dec/$date/$ts/$time/$hex), the same encoding the DuckDB
query server uses, so Decimal and date values survive the round trip.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
from collections.abc import Callable, Iterable
from pathlib import Path

from dbench import hostinfo
from dbench.config import QUERY_TIMEOUT_S
from dbench.measures.power import SUSPEND_RETRIES, SUSPEND_TOLERANCE_S
from dbench.queries.dialect import load_query, translate
from dbench.systems.base import SystemAdapter
from dbench.systems.duckdb_native import decode


def encode(v):
    """Inverse of ``duckdb_native.decode``."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return {"$dec": str(v)}
    if isinstance(v, dt.datetime):
        return {"$ts": v.isoformat()}
    if isinstance(v, dt.date):
        return {"$date": v.isoformat()}
    if isinstance(v, dt.time):
        return {"$time": v.isoformat()}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$hex": bytes(v).hex()}
    return {"$str": str(v)}


def _file(root: Path, run_id: str, n: int) -> Path:
    return Path(root) / run_id / "_reference" / f"q{n:02d}.json"


def _read(path: Path, system: str, sf: float) -> dict | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["system"] != system or doc["sf"] != sf:
            return None
        return {
            "rows": [tuple(decode(v) for v in row) for row in doc["rows"]],
            "numeric_cols": set(doc["numeric_cols"]),
            "columns": list(doc["columns"]),
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write(path: Path, system: str, sf: float, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "system": system, "sf": sf,
        "rows": [[encode(v) for v in row] for row in entry["rows"]],
        "numeric_cols": sorted(entry["numeric_cols"]),
        "columns": entry["columns"],
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    tmp.replace(path)


def cached_reference(
    system: str, sf: float, run_id: str, root: Path, queries: Iterable[int]
) -> dict[int, dict] | None:
    """All cached entries for ``queries``, or None when any is missing."""
    out: dict[int, dict] = {}
    for n in queries:
        entry = _read(_file(root, run_id, n), system, sf)
        if entry is None:
            return None
        out[n] = entry
    return out


def _run_awake(adapter: SystemAdapter, sql: str, timeout_s: int,
               suspended_s: Callable[[], float]):
    """``adapter.execute`` until a run is not interrupted by a host suspend; None if every
    attempt was."""
    for _ in range(SUSPEND_RETRIES + 1):
        before = suspended_s()
        res = adapter.execute(sql, timeout_s)
        if suspended_s() - before <= SUSPEND_TOLERANCE_S:
            return res
    return None


def build_reference(
    adapter: SystemAdapter,
    sf: float,
    run_id: str,
    root: Path,
    queries: Iterable[int] = range(1, 23),
    timeout_s: int = QUERY_TIMEOUT_S,
    query_text: Callable[[int], str] | None = None,
    suspended_s: Callable[[], float] | None = None,
) -> dict[int, dict]:
    """Run each query once on ``adapter`` (cached ones are reused). Queries that do
    not finish ``ok`` are left out of the result and not cached. ``query_text(n)`` replaces the
    default ``translate(load_query(n), adapter.dialect)`` (Track B reads Parquet). A run during
    which the host slept (``suspended_s()`` grew) is run again, up to SUSPEND_RETRIES times."""
    text = query_text or (lambda n: translate(load_query(n), adapter.dialect))
    slept = suspended_s or hostinfo.suspended_s
    out: dict[int, dict] = {}
    for n in queries:
        path = _file(root, run_id, n)
        entry = _read(path, adapter.name, sf)
        if entry is None:
            res = _run_awake(adapter, text(n), timeout_s, slept)
            if res is None or res.status != "ok":
                continue
            entry = {"rows": list(res.rows or []), "numeric_cols": set(res.numeric_cols),
                     "columns": list(res.columns)}
            _write(path, adapter.name, sf, entry)
        out[n] = entry
    return out
