"""TPC-H dataset generation (DuckDB ``dbgen`` in a one-shot container) and its manifest.

Layout under ``<out>/sf<N>/``::

    csv/<table>/<table>.<k>.csv          '|' delimited, no header, no trailing delimiter
    parquet/<table>/<table>.<k>.parquet
    manifest.json                        sha256, rows and bytes of every file
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dbench import dockerctl

TABLES = ("region", "nation", "part", "supplier", "partsupp", "customer", "orders", "lineitem")
MANIFEST = "manifest.json"
PART_BYTES = 2 * 10**9          # hard cap per CSV part when splitting is active
PART_TARGET_BYTES = int(1.8e9)  # size parts to this, leaving margin under the cap
SPLIT_FROM_SF = 100
# Pipe-delimited CSV bytes per scale factor, rounded up from the measured SF1 files
# (lineitem 771865717, orders 173452161, partsupp 118184616, ...).
CSV_BYTES_PER_SF = {
    "region": 400, "nation": 2_300, "supplier": 1_430_000, "part": 24_800_000,
    "customer": 24_600_000, "partsupp": 118_300_000, "orders": 173_600_000,
    "lineitem": 772_000_000,
}
# Deterministic order used to cut row ranges into parts.
ORDER_BY = {
    "region": "r_regionkey", "nation": "n_nationkey", "part": "p_partkey",
    "supplier": "s_suppkey", "partsupp": "ps_partkey, ps_suppkey",
    "customer": "c_custkey", "orders": "o_orderkey", "lineitem": "l_orderkey, l_linenumber",
}
DEFAULT_IMAGE = "dbench/duckdb:1.5.5"
_CHUNK = 1 << 20


@dataclass(frozen=True)
class DatasetPaths:
    sf: float
    root: Path
    csv_dir: Path
    parquet_dir: Path
    manifest: Path
    tables: dict[str, list[Path]] = field(default_factory=dict)
    parquet_tables: dict[str, list[Path]] = field(default_factory=dict)


def data_root() -> Path:
    env = os.environ.get("DBENCH_DATA")
    return Path(env) if env else Path(__file__).resolve().parents[1] / "data"


def default_out() -> Path:
    return data_root() / "tpch"


def dataset_dir_name(sf: float) -> str:
    return f"sf{sf:g}"


def part_count(table: str, sf: float, part_bytes: int | None = None) -> int:
    """Number of files a table is split into.

    By default tables are split only from SF100, to PART_TARGET_BYTES per part. An explicit
    ``part_bytes`` forces splitting at any scale factor (used to exercise multi-part output).
    """
    if part_bytes is None:
        if sf < SPLIT_FROM_SF:
            return 1
        part_bytes = PART_TARGET_BYTES
    return max(1, math.ceil(CSV_BYTES_PER_SF[table] * sf / part_bytes))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _part_index(path: Path) -> int:
    return int(path.stem.rsplit(".", 1)[1])


def _build_paths(root: Path, sf: float, files: list[dict]) -> DatasetPaths:
    by_fmt: dict[str, dict[str, list[Path]]] = {"csv": {}, "parquet": {}}
    for entry in files:
        by_fmt[entry["format"]].setdefault(entry["table"], []).append(root / entry["path"])
    ordered = {
        fmt: {t: sorted(ps, key=_part_index) for t, ps in sorted(tables.items())}
        for fmt, tables in by_fmt.items()
    }
    return DatasetPaths(
        sf=sf, root=root, csv_dir=root / "csv", parquet_dir=root / "parquet",
        manifest=root / MANIFEST, tables=ordered["csv"], parquet_tables=ordered["parquet"],
    )


def write_manifest(
    root: Path, sf: float, duckdb_version: str, files: list[tuple[Path, str, str, int]]
) -> DatasetPaths:
    """Hash ``files`` ((path, table, format, rows) under ``root``) and write the manifest."""
    entries = [
        {
            "path": path.relative_to(root).as_posix(), "sha256": _sha256(path),
            "rows": rows, "bytes": path.stat().st_size, "table": table, "format": fmt,
        }
        for path, table, fmt, rows in sorted(files, key=lambda f: f[0].as_posix())
    ]
    doc = {
        "sf": sf, "duckdb_version": duckdb_version,
        "generated_at": datetime.now(UTC).isoformat(), "files": entries,
    }
    tmp = root / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(root / MANIFEST)
    return _build_paths(root, sf, entries)


def _read_manifest(root: Path) -> dict:
    return json.loads((root / MANIFEST).read_text())


def load_paths(root: Path) -> DatasetPaths:
    doc = _read_manifest(Path(root))
    return _build_paths(Path(root), doc["sf"], doc["files"])


def verify(paths: DatasetPaths) -> bool:
    """True when every manifest file exists with its recorded size and SHA-256."""
    try:
        doc = _read_manifest(paths.root)
        for entry in doc["files"]:
            path = paths.root / entry["path"]
            if not path.is_file() or path.stat().st_size != entry["bytes"]:
                return False
            if _sha256(path) != entry["sha256"]:
                return False
    except (OSError, ValueError, KeyError):
        return False
    return bool(doc["files"])


_GENERATOR = r"""
import json, os, shutil, sys
import duckdb

sf = float(sys.argv[1])
parts = json.loads(sys.argv[2])
order = json.loads(sys.argv[3])
work = "/out/.work"
os.makedirs(work, exist_ok=True)
con = duckdb.connect(work + "/tpch.duckdb")
con.execute("SET temp_directory='" + work + "/spill'")
result = []
try:
    con.execute("INSTALL tpch; LOAD tpch;")
    con.execute(f"CALL dbgen(sf={sf})")
    for table, nparts in parts.items():
        total = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        con.execute(f"CREATE TABLE _o AS SELECT * FROM {table} ORDER BY {order[table]}")
        step = -(-total // nparts)
        for k in range(nparts):
            lo, hi = k * step, min(total, (k + 1) * step)
            rows = max(0, hi - lo)
            sel = f"SELECT * FROM _o WHERE rowid >= {lo} AND rowid < {hi}"
            for fmt, ext, opts in (("csv", "csv", "FORMAT CSV, DELIMITER '|', HEADER false"),
                                   ("parquet", "parquet", "FORMAT PARQUET")):
                d = f"/out/{fmt}/{table}"
                os.makedirs(d, exist_ok=True)
                path = f"{d}/{table}.{k}.{ext}"
                con.execute(f"COPY ({sel}) TO '{path}' ({opts})")
                result.append({"path": path, "table": table, "format": fmt, "rows": rows})
        con.execute("DROP TABLE _o")
    version = duckdb.__version__
finally:
    con.close()
    shutil.rmtree(work, ignore_errors=True)
print("DBENCH_RESULT " + json.dumps({"duckdb_version": version, "files": result}))
"""


def _ensure_image(image: str) -> None:
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
        return
    version = image.rsplit(":", 1)[1]
    ctx = Path(__file__).resolve().parents[1] / "systems" / "duckdb"
    subprocess.run(
        ["docker", "build", "--build-arg", f"DUCKDB_VERSION={version}", "-t", image, str(ctx)],
        check=True,
    )


def _run_generator(sf: float, work: Path, image: str, part_bytes: int | None) -> dict:
    """Run dbgen in a one-shot container writing into ``work``; return its JSON report."""
    _ensure_image(image)
    parts = {t: part_count(t, sf, part_bytes) for t in TABLES}
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "python",
        "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
        "-v", f"{dockerctl.host_path(work)}:/out", image,
        "-c", _GENERATOR, f"{sf:g}", json.dumps(parts), json.dumps(ORDER_BY),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"dataset generation failed:\n{proc.stdout}\n{proc.stderr}")
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("DBENCH_RESULT ")), None
    )
    if line is None:
        raise RuntimeError(f"generator produced no report:\n{proc.stdout}\n{proc.stderr}")
    return json.loads(line.removeprefix("DBENCH_RESULT "))


def _check_part_sizes(files: list[tuple[Path, str, str, int]]) -> None:
    for path, table, fmt, _ in files:
        if fmt == "csv" and path.stat().st_size > PART_BYTES:
            raise RuntimeError(
                f"{table} part {path.name} is {path.stat().st_size} bytes and exceeds "
                f"the {PART_BYTES} byte part cap; recalibrate CSV_BYTES_PER_SF"
            )


def generate(
    sf: float,
    out: Path | str | None = None,
    image: str = DEFAULT_IMAGE,
    part_bytes: int | None = None,
) -> DatasetPaths:
    """Generate TPC-H at ``sf`` under ``<out>/sf<N>``; reuse it when its manifest verifies.

    Splitting into parts (and the part cap check) is active from SF100, or at any scale
    factor when ``part_bytes`` is given.
    """
    out = Path(out) if out else default_out()
    root = out / dataset_dir_name(sf)
    if (root / MANIFEST).is_file():
        existing = load_paths(root)
        if verify(existing):
            return existing
    work = out / f".{root.name}.tmp"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        report = _run_generator(sf, work, image, part_bytes)
        files = [
            (work / Path(f["path"]).relative_to("/out"), f["table"], f["format"], f["rows"])
            for f in report["files"]
        ]
        if part_bytes is not None or sf >= SPLIT_FROM_SF:
            _check_part_sizes(files)
        write_manifest(work, sf, report["duckdb_version"], files)
        shutil.rmtree(root, ignore_errors=True)
        work.replace(root)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return load_paths(root)
