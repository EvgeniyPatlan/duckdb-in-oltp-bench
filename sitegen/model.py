"""Records -> the systems and cells the site shows, with one neutral presentation per system."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dbench.results import ResultRecord, iter_records

# Okabe-Ito, colour-blind safe. Assigned to the engines in alphabetical order of their base name.
PALETTE = ("#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442",
           "#999999")
PLAIN_SHADES = ("#8a8f98", "#6f7580", "#a3a8b0", "#575c66", "#b8bcc3")
ROWSTORE_COLORS = {"innodb": "#8c7b6b", "postgres": "#6b8c86"}
# Engines the repo knows; fixed so a colour does not depend on which results exist.
KNOWN_ENGINES = ("alisql", "mariadb", "percona", "pg_duckdb", "pg_ducklake", "villagesql")
TRACKB_SUFFIX = "-trackb"
# Short labels name who provides the DuckDB integration, in one style for everyone: the
# project/vendor for MySQL-family servers, the extension for PostgreSQL (both run on the same
# server). The full product with versions comes from systems/versions.lock.json on each page.
LABELS = {
    "alisql": "AliSQL", "mariadb": "MariaDB", "percona": "Percona",
    "pg_duckdb": "pg_duckdb", "pg_ducklake": "pg_ducklake", "villagesql": "VillageSQL",
    "innodb": "MySQL InnoDB", "postgres": "PostgreSQL",
    "pg_duckdb-trackb": "pg_duckdb (duckdb.query)",
    "mariadb-trackb": "MariaDB (run_in_duckdb)",
}
CONFIG_DIRS = {"pg_duckdb": "pgduckdb", "pg_ducklake": "pgducklake"}
SYNTHETIC_KEY = "synthetic"


def base_engine(name: str) -> str:
    return name.removesuffix(TRACKB_SUFFIX)


def palette_for(names: Iterable[str]) -> dict[str, str]:
    """Colour of each engine: alphabetical order over the known engines plus any others given."""
    engines = sorted({*KNOWN_ENGINES, *(base_engine(n) for n in names
                                        if not n.startswith("duckdb-")
                                        and n not in ROWSTORE_COLORS)})
    by_engine = {e: PALETTE[i % len(PALETTE)] for i, e in enumerate(engines)}
    out: dict[str, str] = {}
    for n in names:
        if n in ROWSTORE_COLORS:
            out[n] = ROWSTORE_COLORS[n]
        elif not n.startswith("duckdb-"):
            out[n] = by_engine[base_engine(n)]
    return out


def kind_of(name: str, track: str) -> str:
    if name.startswith("duckdb-"):
        return "plain"
    return "rowstore" if name in ROWSTORE_COLORS else "integration"


def config_dir(name: str) -> str:
    if name.startswith("duckdb-"):
        return "duckdb"
    base = base_engine(name)
    return CONFIG_DIRS.get(base, base)


def label_of(name: str) -> str:
    if name.startswith("duckdb-"):
        return f"DuckDB {name.removeprefix('duckdb-')}"
    return LABELS.get(name, name)


@dataclass(frozen=True)
class Loaded:
    records: list[ResultRecord]
    synthetic: bool
    run_ids: list[str]
    excluded: list[dict]


def is_synthetic(rec: ResultRecord) -> bool:
    return bool(rec.extra.get(SYNTHETIC_KEY))


def pick_runs(records: list[ResultRecord]) -> tuple[list[ResultRecord], list[dict]]:
    """For each (track, system, sf) keep only the run with the most records (the newest on a
    tie), so one system's numbers never mix runs with different versions or digests. The other
    runs are returned as ``excluded`` entries for the page footnote."""
    runs: dict[tuple, dict[str, list[ResultRecord]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        runs[(r.track, r.system, r.sf)][r.run_id].append(r)
    kept: list[ResultRecord] = []
    excluded: list[dict] = []
    for (track, system, sf), by_run in sorted(runs.items()):
        best = max(by_run, key=lambda rid: (len(by_run[rid]),
                                            max(r.started_at for r in by_run[rid])))
        kept += by_run[best]
        excluded += [{"track": track, "system": system, "sf": sf, "run_id": rid,
                      "records": len(recs)} for rid, recs in sorted(by_run.items())
                     if rid != best]
    return kept, excluded


def read(results_root: Path) -> tuple[list[ResultRecord], bool]:
    """Every record under ``results_root`` and whether they are fixtures; fixture records are
    dropped as soon as one real record exists."""
    records = list(iter_records(Path(results_root)))
    real = [r for r in records if not is_synthetic(r)]
    return real or records, bool(records) and not real


def loaded_from(records: list[ResultRecord], synthetic: bool) -> Loaded:
    """The records a page shows: one run per (track, system, sf), in a stable order."""
    kept, excluded = pick_runs(records)
    out = sorted(kept, key=lambda r: (r.system, r.measure, r.sf, r.item))
    return Loaded(out, synthetic, sorted({r.run_id for r in out}), excluded)


def load(results_root: Path) -> Loaded:
    """The records the site shows; fixture records are dropped as soon as one real record exists."""
    return loaded_from(*read(results_root))


def system_entries(records: Iterable[ResultRecord]) -> list[dict]:
    """One neutral description per system: integrations A-Z, then plain DuckDB, then row stores."""
    seen: dict[str, ResultRecord] = {}
    versions: dict[str, str | None] = defaultdict(lambda: None)
    for r in records:
        seen.setdefault(r.system, r)
        if r.versions.get("duckdb") and not versions[r.system]:
            versions[r.system] = str(r.versions["duckdb"])
    names = sorted(seen)
    colors = palette_for(names)
    plain = sorted(n for n in names if n.startswith("duckdb-"))
    shades = {n: PLAIN_SHADES[i % len(PLAIN_SHADES)] for i, n in enumerate(plain)}
    order = ([n for n in names if kind_of(n, "") == "integration"] + plain
             + [n for n in names if n in ROWSTORE_COLORS])
    return [{"name": n, "label": label_of(n), "kind": kind_of(n, seen[n].track),
             "color": shades.get(n) or colors[n], "track": seen[n].track,
             "duckdb_version": versions[n] or (n.removeprefix("duckdb-")
                                               if n.startswith("duckdb-") else None),
             "page": f"systems/{config_dir(n)}.html"} for n in order]
