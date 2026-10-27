"""Command-line entry point for dbench."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from dbench import __version__
from dbench.config import QUERY_TIMEOUT_S

SMOKE_DB_CPUSET = "0-7"
SMOKE_RUNNER_CPUSET = "8-11"
SMOKE_MEM_GB = 16


def results_root() -> Path:
    env = os.environ.get("DBENCH_RESULTS")
    return Path(env) if env else Path(__file__).resolve().parents[1] / "results"


def _sf(value: str) -> float:
    sf = float(value)
    if sf <= 0:
        raise argparse.ArgumentTypeError("scale factor must be positive")
    return int(sf) if sf == int(sf) else sf


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbench",
        description="Benchmark DuckDB integrations in MySQL, MariaDB and PostgreSQL servers.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("datagen", help="generate a TPC-H dataset with a checksummed manifest")
    gen.add_argument("--sf", type=_sf, required=True)
    gen.add_argument("--out", type=Path, help="parent of sf<N>/ (default: $DBENCH_DATA/tpch)")

    power = sub.add_parser("power", help="M1 power run of one system")
    power.add_argument("--system", required=True,
                       help="duckdb-<version> (e.g. duckdb-1.5.5), innodb or postgres")
    power.add_argument("--sf", type=_sf, required=True)
    power.add_argument("--compare-with", metavar="SYSTEM",
                       help="reference system whose results every ok query must match")
    power.add_argument("--timeout", type=int, default=QUERY_TIMEOUT_S,
                       help="per-query timeout in seconds (default: %(default)s)")
    power.add_argument("--run-id")
    power.add_argument("--results", type=Path, help="default: $DBENCH_RESULTS or <repo>/results")
    power.add_argument("--data", type=Path, help="dataset parent (default: $DBENCH_DATA/tpch)")
    return parser


def _datagen(args: argparse.Namespace) -> int:
    from dbench import datagen

    paths = datagen.generate(args.sf, args.out)
    print(f"dataset ready: {paths.root} (manifest {paths.manifest})")
    return 0


def _loaded_adapter(name: str, paths, limits, sf):
    """A started adapter with the schema created and the dataset bulk-loaded."""
    from dbench.systems.registry import get_adapter

    adapter = get_adapter(name, paths.root)
    adapter.remove_data()
    adapter.start(limits)
    try:
        adapter.create_schema(sf, "engine" if adapter.family == "duckdb" else "rowstore")
        load = adapter.load_bulk(paths)
        if load.status != "ok":
            raise SystemExit(f"bulk load of {name} failed: {load.error} {load.extra}")
    except BaseException:
        adapter.remove_data()
        raise
    return adapter


def _reference(args, paths, limits, root: Path, run_id: str) -> dict:
    from dbench.reference import build_reference, cached_reference

    queries = range(1, 23)
    cached = cached_reference(args.compare_with, args.sf, run_id, root, queries)
    if cached is not None:
        return cached
    adapter = _loaded_adapter(args.compare_with, paths, limits, args.sf)
    try:
        ref = build_reference(adapter, args.sf, run_id, root, queries, args.timeout)
    finally:
        adapter.remove_data()
    missing = [n for n in queries if n not in ref]
    if missing:
        print(f"reference {args.compare_with}: no result for queries {missing}; "
              "they are not compared")
    return ref


def _power(args: argparse.Namespace) -> int:
    from dbench import datagen
    from dbench.config import Limits
    from dbench.measures.power import run_power

    out = args.data or datagen.default_out()
    root = out / datagen.dataset_dir_name(args.sf)
    paths = datagen.load_paths(root) if (root / datagen.MANIFEST).is_file() else None
    if paths is None or not datagen.verify(paths):
        raise SystemExit(
            f"dataset missing or corrupt at {root}; run: dbench datagen --sf {args.sf}"
        )
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results = args.results or results_root()
    limits = Limits(SMOKE_DB_CPUSET, SMOKE_RUNNER_CPUSET, SMOKE_MEM_GB)
    reference = _reference(args, paths, limits, results, run_id) if args.compare_with else None
    adapter = _loaded_adapter(args.system, paths, limits, args.sf)
    try:
        records = run_power(adapter, args.sf, run_id, results, reference,
                            timeout_s=args.timeout)
    finally:
        adapter.remove_data()
    counts: dict[str, int] = {}
    for r in records:
        counts[r.status] = counts.get(r.status, 0) + 1
        print(f"  {r.item}: {r.status}" + (f" ({r.error})" if r.error else ""))
    print(f"run {run_id}: {len(records)} records, " + ", ".join(
        f"{n} {s}" for s, n in sorted(counts.items())))
    return 0 if counts.get("ok") == len(records) else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "datagen":
        return _datagen(args)
    if args.command == "power":
        return _power(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
