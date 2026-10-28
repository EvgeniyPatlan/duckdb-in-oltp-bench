"""The methodology page's content: settings, limits, tolerances, dialect diffs, host, campaigns."""
from __future__ import annotations

import inspect
from pathlib import Path

import yaml

from dbench.config import QUERY_TIMEOUT_S, SCALE_FACTORS, STREAMS, WARM_RUNS, Limits
from dbench.correctness import compare
from dbench.queries.dialect import dialect_diff
from dbench.results import ResultRecord
from sitegen import md, settings

REPO = Path(__file__).resolve().parents[1]
DIALECTS = ("mysql", "postgres")
DIR_LABELS = {"pgduckdb": "pg_duckdb", "pgducklake": "pg_ducklake", "duckdb": "Plain DuckDB",
              "innodb": "InnoDB (MySQL 9.7)", "postgres": "PostgreSQL 18 (heap)",
              "alisql": "AliSQL", "mariadb": "MariaDB DuckDB engine",
              "percona": "Percona ducksdb-mysql-engine", "villagesql": "VillageSQL vsql_duckdb"}


def tolerances() -> dict:
    params = inspect.signature(compare).parameters
    return {name: params[name].default for name in ("rel_tol", "abs_tol")}


def limits() -> dict:
    lim = Limits()
    return {"db_cpuset": lim.db_cpuset, "runner_cpuset": lim.runner_cpuset, "mem_gb": lim.mem_gb,
            "timeout_s": QUERY_TIMEOUT_S, "warm_runs": WARM_RUNS,
            "streams": ", ".join(map(str, STREAMS)),
            "scale_factors": ", ".join(map(str, SCALE_FACTORS))}


def dialect_diffs() -> list[dict]:
    """Per dialect the unified diff of every query that the mechanical translation changes."""
    out = []
    for dialect in DIALECTS:
        diffs = [(n, d) for n in range(1, 23) if (d := dialect_diff(n, dialect))]
        out.append({"dialect": dialect, "diffs": diffs,
                    "unchanged": 22 - len(diffs)})
    return out


def host_facts(records: list[ResultRecord]) -> list[dict]:
    seen, out = set(), []
    for r in records:
        facts = {k: v for k, v in r.host.items() if k != "synthetic"}
        key = tuple(sorted(facts.items(), key=lambda kv: kv[0]))
        if facts and key not in seen:
            seen.add(key)
            out.append(facts)
    return out


def campaigns() -> list[dict]:
    out = []
    for path in sorted((REPO / "campaigns").glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        out.append({"file": f"campaigns/{path.name}", "sf": doc.get("sf"),
                    "limits": doc.get("limits") or {}, "timeout_s": doc.get("timeout_s"),
                    "measures": ", ".join(doc.get("measures") or []),
                    "systems": ", ".join(doc.get("systems") or [])})
    return out


def all_settings() -> list[dict]:
    out = []
    for config_dir in settings.CONFIG_FILES:
        rows = settings.for_system(config_dir)
        out.append({"dir": config_dir, "rows": rows,
                    "label": DIR_LABELS.get(config_dir, config_dir)})
    return out


def method_docs() -> list[dict]:
    return [{"name": p.stem, "html": md.render(p.read_text(encoding="utf-8"), base_level=3)}
            for p in sorted((REPO / "docs" / "methodology").glob("*.md"))]


def context(records: list[ResultRecord]) -> dict:
    return {"limits": limits(), "tolerances": tolerances(), "dialects": dialect_diffs(),
            "hosts": host_facts(records), "campaigns": campaigns(), "settings": all_settings(),
            "docs": method_docs()}
