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

    trackb = sub.add_parser(
        "trackb", help="Track B: TPC-H over Parquet through each system's raw DuckDB entry point")
    trackb.add_argument("--sf", type=_sf, required=True)
    trackb.add_argument("--systems", nargs="+", metavar="SYSTEM",
                        help="default: duckdb-1.5.5 villagesql pg_duckdb-trackb mariadb-trackb")
    trackb.add_argument("--timeout", type=int, default=QUERY_TIMEOUT_S,
                        help="per-query timeout in seconds (default: %(default)s)")
    trackb.add_argument("--run-id")
    trackb.add_argument("--results", type=Path, help="default: $DBENCH_RESULTS or <repo>/results")
    trackb.add_argument("--data", type=Path, help="dataset parent (default: $DBENCH_DATA/tpch)")

    camp = sub.add_parser("campaign", help="run a campaign file (resumes cells already recorded)")
    camp.add_argument("file", type=Path)
    camp.add_argument("--results", type=Path, help="default: $DBENCH_RESULTS or <repo>/results")

    lk = sub.add_parser("lock", help="write systems/versions.lock.json from the local images")
    lk.add_argument("--force", action="store_true",
                    help="also change registry digests and build inputs (default: only refresh "
                         "local image ids)")

    failed = sub.add_parser(
        "mark-failed", help="record every planned cell of a system as failed")
    failed.add_argument("--system", required=True)
    failed.add_argument("--error", required=True, help="reason shown in the results")
    failed.add_argument("--repro", required=True, help="steps to reproduce the failure")
    failed.add_argument("--run-id", help="default: the campaign's run_id")
    failed.add_argument("--campaign", type=Path, required=True,
                        help="campaign file that plans the cells, e.g. campaigns/gate-sf10.yaml")
    failed.add_argument("--results", type=Path, help="default: $DBENCH_RESULTS or <repo>/results")
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
        adapter.create_schema(sf, adapter.bench_schema_kind)
        load = adapter.load_bulk(paths)
        if load.status != "ok":
            raise SystemExit(f"bulk load of {name} failed: {load.error} {load.extra}")
        print(f"loaded {name} at SF{sf}: {load.elapsed_s:.1f}s (incl. analyze)")
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


def _dataset(args: argparse.Namespace):
    """The verified dataset of ``--sf`` (exits with the datagen command when it is missing)."""
    from dbench import datagen

    out = args.data or datagen.default_out()
    root = out / datagen.dataset_dir_name(args.sf)
    paths = datagen.load_paths(root) if (root / datagen.MANIFEST).is_file() else None
    if paths is None or not datagen.verify(paths):
        raise SystemExit(
            f"dataset missing or corrupt at {root}; run: dbench datagen --sf {args.sf}"
        )
    return paths


def _trackb(args: argparse.Namespace) -> int:
    from dbench.config import Limits
    from dbench.measures.villagesql import TRACK_B_SYSTEMS, run_track_b

    paths = _dataset(args)
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    limits = Limits(SMOKE_DB_CPUSET, SMOKE_RUNNER_CPUSET, SMOKE_MEM_GB)
    records = run_track_b(args.sf, run_id, args.results or results_root(),
                          args.systems or TRACK_B_SYSTEMS, data=paths, limits=limits,
                          timeout_s=args.timeout)
    for r in records:
        match = ""
        if r.status in ("ok", "wrong_result") and r.extra.get("compared"):
            match = " match" if r.status == "ok" else " MISMATCH"
        detail = r.error or r.reason or ""
        note = f" ({detail[:200]})" if detail else ""
        print(f"  {r.system} {r.item}: {r.status}{match}{note}")
    print(f"run {run_id}: {len(records)} Track B records")
    return 0


def _power(args: argparse.Namespace) -> int:
    from dbench.config import Limits
    from dbench.measures.power import run_power

    paths = _dataset(args)
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


def _status_table(root: Path, run_ids: list[str]) -> None:
    from collections import Counter

    from dbench.results import iter_records

    for run_id in run_ids:
        by_system: dict[str, Counter] = {}
        for r in iter_records(root / run_id):
            if r.run_id == run_id:
                by_system.setdefault(r.system, Counter())[r.status] += 1
        for system, counts in sorted(by_system.items()):
            print(f"  {run_id} {system}: " + ", ".join(
                f"{n} {s}" for s, n in sorted(counts.items())))


def _campaign(args: argparse.Namespace) -> int:
    from dbench.campaign import CampaignError, load_campaign, run_campaign
    from dbench.lock import LockError

    root = args.results or results_root()
    try:
        campaign = load_campaign(args.file)
        run_ids = [campaign.run_id, *([campaign.trackb_run_id] if campaign.track_b_enabled else [])]
    except CampaignError as exc:
        print(f"campaign stopped: {exc}")
        return 2
    try:
        summary = run_campaign(args.file, root)
    except (CampaignError, LockError, ValueError) as exc:
        print(f"campaign stopped: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - never end a long run with a bare traceback
        print(f"campaign stopped unexpectedly: {type(exc).__name__}: {exc}")
        print("recorded so far (run the same command to resume):")
        _status_table(root, run_ids)
        return 1
    print(f"campaign {campaign.run_id}: {summary.written} records written, "
          f"{summary.skipped} cells skipped (already recorded)")
    _status_table(root, run_ids)
    return 0


def _lock(args: argparse.Namespace) -> int:
    from dbench import lock

    try:
        doc = lock.write_lock(force=args.force)
    except lock.LockError as exc:
        print(f"lock failed: {exc}")
        return 2
    for system, e in doc.items():
        print(f"  {system}: {e['image']} {e['digest_kind']} {e['digest'][:19]}")
    print(f"wrote {lock.LOCK_PATH}")
    return 0


def _mark_failed(args: argparse.Namespace) -> int:
    from dbench.campaign import CampaignError, mark_failed

    try:
        n = mark_failed(args.campaign, args.results or results_root(), system=args.system,
                        error=args.error, repro=args.repro, run_id=args.run_id)
    except CampaignError as exc:
        print(f"mark-failed: {exc}")
        return 2
    print(f"{args.system}: {n} cells recorded as failed")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "datagen":
        return _datagen(args)
    if args.command == "power":
        return _power(args)
    if args.command == "trackb":
        return _trackb(args)
    if args.command == "campaign":
        return _campaign(args)
    if args.command == "lock":
        return _lock(args)
    if args.command == "mark-failed":
        return _mark_failed(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
