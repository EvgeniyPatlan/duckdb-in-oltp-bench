"""SYNTHETIC result set for testing and previewing the site. Nothing here was measured.

Every record carries ``extra.synthetic = True`` and the run ids start with ``synthetic``; the site
shows a banner when a results root holds nothing else. The numbers come from a seeded generator
and have no relation to any real system. The set covers every record status and every measure
the site shows (M1-M5, M7, M8, and Track B M1).

    python -m tests.fixtures.synthetic <out-dir>
"""
from __future__ import annotations

import random
import sys
from dataclasses import replace
from pathlib import Path

from dbench.measures.fallback import ITEMS as FALLBACK_ITEMS
from dbench.measures.fallback import summarize_fallback
from dbench.measures.overhead import compute_overhead
from dbench.results import ResultRecord, write

RUN = "synthetic-fixture"
RUN_B = "synthetic-fixture-trackb"
STAMP = "2026-11-01T10:00:00+00:00"
SFS = (1, 10)
QUERIES = [f"q{n:02d}" for n in range(1, 23)]
STREAMS = (1, 4, 8, 16, 32)
HOST = {"cpus": 80, "mem_gb": 180, "kernel": "6.8.0-synthetic", "docker_version": "0.0-synthetic",
        "synthetic": True}

# name -> (track, config dir, bundled DuckDB, overhead factor over plain DuckDB, pushdown scope)
INTEGRATIONS = {
    "alisql": ("A", "alisql", "1.5.2-dev736", 1.3, "session"),
    "mariadb": ("A", "mariadb", "1.5.5", 2.1, "session"),
    "percona": ("A", "percona", "1.5.3", 1.2, "global"),
    "pg_duckdb": ("A", "pgduckdb", "1.4.3", 1.8, "explain"),
    "pg_ducklake": ("A", "pgducklake", "1.5.5", 1.5, "none"),
}
PLAIN = {"duckdb-1.4.3": "1.4.3", "duckdb-1.5.2-dev736": "1.5.2-dev736",
         "duckdb-1.5.3": "1.5.3", "duckdb-1.5.5": "1.5.5"}
ROWSTORES = {"innodb": ("innodb", 40.0), "postgres": ("postgres", 14.0)}

# (system, sf, query) -> (status, reason or error)
M1_FAULTS = {
    ("mariadb", "q21"): ("error", "synthetic: unsupported correlated subquery form"),
    ("mariadb", "q15"): ("wrong_result", "synthetic: 1 of 1 rows differ"),
    ("alisql", "q22"): ("error", "synthetic: mixed engine query refused"),
    ("pg_duckdb", "q09"): ("timeout", "synthetic: no result after 900 s"),
    ("pg_ducklake", "q18"): ("crashed", "synthetic: server exited during the query"),
    ("innodb", "q17"): ("timeout", "synthetic: no result after 900 s"),
    ("innodb", "q20"): ("timeout", "synthetic: no result after 900 s"),
    ("postgres", "q17"): ("timeout", "synthetic: no result after 900 s"),
}
CLASS_PLAN = ["duckdb", "duckdb", "fallback", "duckdb", "error", "unknown", "duckdb",
              "wrong_result", "ok_unobserved", "fallback", "duckdb", "n/a"]
PROBES = [("types_decimal", "types"), ("fn_group_concat", "functions"),
          ("join_semi", "joins"), ("sub_correlated", "subqueries"), ("cte_recursive", "cte"),
          ("win_rank", "window"), ("dml_upsert", "dml"), ("dml_bulk_insert", "dml"),
          ("ddl_alter_add", "ddl"), ("ddl_rename", "ddl"), ("var_user", "variables"),
          ("join_mixed_engine", "joins")]


def _rec(system: str, track: str, measure: str, sf: int, item: str, status: str,
         value: float | None, unit: str, *, config: str, run: str = RUN,
         extra: dict | None = None, reason: str | None = None, error: str | None = None,
         version: str | None = None) -> ResultRecord:
    return ResultRecord(
        system=system, track=track, measure=measure, sf=sf, item=item, status=status,
        value=value, unit=unit, config_ref=f"systems/{config}/", run_id=run, started_at=STAMP,
        extra={"synthetic": True, **(extra or {})}, reason=reason, error=error,
        versions={"duckdb": version, "server": f"synthetic {system}"} if version else {},
        host=dict(HOST))


def _base_time(rng: random.Random, sf: int) -> dict[str, float]:
    return {q: (0.05 + rng.random() * 0.6) * sf ** 0.9 for q in QUERIES}


def _m1(system: str, track: str, config: str, version: str, sf: int, run: str,
        base: dict[str, float], factor: float, items: list[str]) -> list[ResultRecord]:
    out = []
    for q in items:
        status, why = M1_FAULTS.get((system, q), ("ok", None))
        if status == "ok":
            warm = base.get(q, 0.5) * factor
            extra = {"cold_s": warm * 1.6, "warm_s": [warm] * 3, "pushed": "duckdb"}
            out.append(_rec(system, track, "M1", sf, q, "ok", warm, "s", config=config, run=run,
                            extra=extra, version=version))
            continue
        extra = {"diffs": [["1", "2"]]} if status == "wrong_result" else {}
        value = base.get(q, 0.5) * factor if status == "wrong_result" else None
        out.append(_rec(system, track, "M1", sf, q, status, value, "s", config=config, run=run,
                        extra=extra, error=why, version=version))
    return out


def power_records(rng: random.Random) -> list[ResultRecord]:
    out: list[ResultRecord] = []
    for sf in SFS:
        base = _base_time(rng, sf)
        for name, version in PLAIN.items():
            out += _m1(name, "baseline", "duckdb", version, sf, RUN, base, 1.0, QUERIES)
        for name, (track, config, version, factor, _) in INTEGRATIONS.items():
            out += _m1(name, track, config, version, sf, RUN, base, factor, QUERIES)
        for name, (config, factor) in ROWSTORES.items():
            out += _m1(name, "baseline", config, "", sf, RUN, base, factor, QUERIES)
    return out


def _load(name: str, track: str, config: str, version: str, sf: int,
          rng: random.Random) -> list[ResultRecord]:
    bulk = (20 + rng.random() * 30) * sf ** 0.8
    common = {"config": config, "version": version}
    out = [_rec(name, track, "M2", sf, "load_bulk", "ok", bulk, "s", **common)]
    if name == "pg_duckdb":
        out.append(_rec(name, track, "M2", sf, "load_convert", "n/a", None, "s", **common,
                        reason="synthetic: tables are already heap tables, nothing to convert"))
    else:
        out.append(_rec(name, track, "M2", sf, "load_convert", "ok", bulk * 0.6, "s", **common,
                        extra={"rowstore_load_s": bulk * 1.4}))
    size = 1e9 * sf * (0.3 + rng.random() * 0.2)
    breakdown = {"data": size * 0.9, "redo_wal": size * 0.08, "temp": 0, "other": size * 0.02}
    out.append(_rec(name, track, "M3", sf, "after_load", "ok", size, "bytes", **common,
                    extra={"breakdown": breakdown}))
    if name == "percona":
        out.append(_rec(name, track, "M3", sf, "after_checkpoint", "n/a", None, "bytes",
                        **common, reason="synthetic: no checkpoint statement"))
    else:
        out.append(_rec(name, track, "M3", sf, "after_checkpoint", "ok", size * 0.93, "bytes",
                        **common, extra={"breakdown": breakdown}))
    return out


def _throughput(name: str, track: str, config: str, version: str, sf: int,
                factor: float) -> list[ResultRecord]:
    out = []
    for n in STREAMS:
        item = f"streams={n}"
        common = {"config": config, "version": version}
        if name == "pg_ducklake" and n == 32:
            out.append(_rec(name, track, "M4", sf, item, "failed", None, "qph", **common,
                            reason="synthetic: measure aborted, server unresponsive",
                            extra={"auto": True}))
            continue
        qph = 3600 * 22 / (4 * factor) * min(n, 10) ** 0.7 / (1 + 0.02 * n)
        extra = {"p50": 0.3 * factor * (1 + n / 8), "p95": 0.9 * factor * (1 + n / 5),
                 "streams": n, "counts_by_status": {"ok": 22 * n}}
        out.append(_rec(name, track, "M4", sf, item, "ok", qph, "qph", **common, extra=extra))
    return out


def _probes(name: str, config: str, version: str, sf: int, shift: int) -> list[ResultRecord]:
    out = []
    for i, (pid, category) in enumerate(PROBES):
        cls = CLASS_PLAN[(i + shift) % len(CLASS_PLAN)]
        status = {"error": "error", "wrong_result": "wrong_result", "n/a": "n/a"}.get(cls, "ok")
        extra = {"class": cls, "category": category, "title": f"synthetic probe {pid}"}
        out.append(_rec(name, "A", "M5", sf, pid, status, None, "class", config=config,
                        version=version, extra=extra,
                        error="synthetic: statement failed" if status == "error" else None,
                        reason="synthetic: not applicable" if status == "n/a" else None))
    return out


def track_a_records() -> list[ResultRecord]:
    rng = random.Random(20261027)
    out = power_records(rng)
    for sf in SFS:
        for shift, (name, (track, config, version, factor, scope)) in enumerate(
                INTEGRATIONS.items()):
            out += _load(name, track, config, version, sf, rng)
            out += _throughput(name, track, config, version, sf, factor)
            probes = _probes(name, config, version, sf, shift)
            out += probes
            own = [r for r in out if r.system == name and r.sf == sf and r.measure == "M1"]
            cells = summarize_fallback(own + probes, scope)
            for item in FALLBACK_ITEMS:
                cell = cells[item]
                out.append(_rec(name, track, "M8", sf, item, cell["status"], cell["value"],
                                "ratio", config=config, version=version, extra=cell["extra"],
                                reason=cell.get("reason")))
        for name, version in PLAIN.items():
            out += _throughput(name, "baseline", "duckdb", version, sf, 1.0)
    out += compute_overhead(out)
    return [replace(r, extra={"synthetic": True, **r.extra}) for r in out]


def track_b_records() -> list[ResultRecord]:
    rng = random.Random(7)
    items = [*QUERIES, "join_innodb"]
    systems = {"duckdb-1.5.5": ("duckdb", "1.5.5", 1.0), "villagesql": ("villagesql", "1.5.5", 1.9),
               "pg_duckdb-trackb": ("pgduckdb", "1.4.3", 1.4),
               "mariadb-trackb": ("mariadb", "1.5.5", 1.6)}
    out: list[ResultRecord] = []
    for sf in SFS:
        base = {**_base_time(rng, sf), "join_innodb": 0.2}
        for name, (config, version, factor) in systems.items():
            if name == "mariadb-trackb":
                out += [_rec(name, "B", "M1", sf, i, "failed", None, "s", config=config,
                             run=RUN_B, version=version, extra={"auto": True},
                             reason="synthetic: run_in_duckdb() returned no result")
                        for i in items]
            elif name != "villagesql":
                out += _m1(name, "B", config, version, sf, RUN_B, base, factor, QUERIES)
            else:
                out += _m1(name, "B", config, version, sf, RUN_B, base, factor, items)
    return out


def write_synthetic(root: Path) -> list[ResultRecord]:
    records = track_a_records() + track_b_records()
    for rec in records:
        write(rec, root)
    return records


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m tests.fixtures.synthetic <out-dir>")
    print(f"wrote {len(write_synthetic(Path(sys.argv[1])))} synthetic records")
