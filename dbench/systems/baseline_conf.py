"""Server configs for the row-store baselines, rendered from the container limits."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, StrictUndefined

from dbench import dockerctl
from dbench.config import Limits

DUCKDB_PG_SPILL_DIR = "/var/lib/postgresql/duckdb_tmp"  # on the data volume
SYSTEMS_DIR = Path(__file__).resolve().parents[2] / "systems"


def size(gb: float, unit: str) -> str:
    """``12`` -> '12G'; ``4.5`` -> '4608M' (units 'G'/'M' or 'GB'/'MB')."""
    if gb == int(gb):
        return f"{int(gb)}{unit}"
    return f"{int(round(gb * 1024))}{unit.replace('G', 'M')}"


def cores(limits: Limits) -> int:
    return len(dockerctl.parse_cpuset(limits.db_cpuset))


def innodb_settings(limits: Limits) -> dict:
    return {
        "buffer_pool": size(0.75 * limits.mem_gb, "G"),
        "read_threads": cores(limits),
        "temp_ram": size(limits.mem_gb / 16, "G"),
        "redo_capacity": size(limits.mem_gb / 8, "G"),
    }


def duckdb_engine_settings(limits: Limits, spill_dir: str) -> dict:
    """Formulas shared by every MySQL-family DuckDB engine (Percona, MariaDB, AliSQL): DuckDB gets
    0.75 * mem_gb and one thread per core; the InnoDB side only holds catalog and staging, so its
    pool is mem_gb/16; the rest equals the InnoDB baseline."""
    base = innodb_settings(limits)
    return {
        "duck_memory": size(0.75 * limits.mem_gb, "G"),
        "duck_memory_gb": size(0.75 * limits.mem_gb, "GB"),
        "cores": base["read_threads"],
        "read_threads": base["read_threads"],
        "spill_dir": spill_dir,
        "buffer_pool": size(limits.mem_gb / 16, "G"),
        "temp_ram": base["temp_ram"],
        "redo_capacity": base["redo_capacity"],
    }


def postgres_settings(limits: Limits) -> dict:
    return {
        "shared_buffers": size(0.25 * limits.mem_gb, "GB"),
        "effective_cache_size": size(0.75 * limits.mem_gb, "GB"),
        "workers": cores(limits),
        "max_wal_size": size(limits.mem_gb / 4, "GB"),
    }


def duckdb_pg_settings(limits: Limits, mode: str) -> dict:
    """Formulas shared by pg_duckdb and pg_ducklake: the Postgres baseline plus the DuckDB memory
    and threads. ``mode`` "pgducklake" (DuckDB-native tables): DuckDB gets 0.75 * mem_gb and
    shared_buffers drops to mem_gb/16, since only catalog/heap staging uses it. ``mode``
    "pgduckdb" (DuckDB scanning Postgres heap pages): shared_buffers stays at the baseline
    0.25 * mem_gb and DuckDB gets 0.5 * mem_gb."""
    base = postgres_settings(limits)
    if mode == "pgducklake":
        shared, duck = limits.mem_gb / 16, 0.75 * limits.mem_gb
    elif mode == "pgduckdb":
        shared, duck = 0.25 * limits.mem_gb, 0.5 * limits.mem_gb
    else:
        raise ValueError(f"unknown mode {mode!r}")
    return {**base, "shared_buffers": size(shared, "GB"), "duck_memory": size(duck, "GB"),
            "duck_memory_mib": int(duck * 1024),
            "duck_threads": cores(limits), "spill_dir": DUCKDB_PG_SPILL_DIR}


def render(template: Path, settings: dict) -> str:
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    return env.from_string(template.read_text(encoding="utf-8")).render(**settings)


def render_innodb_cnf(limits: Limits) -> str:
    return render(SYSTEMS_DIR / "innodb" / "my.cnf", innodb_settings(limits))


def render_postgres_conf(limits: Limits) -> str:
    return render(SYSTEMS_DIR / "postgres" / "postgresql.conf", postgres_settings(limits))


def write_rendered(text: str, name: str, filename: str, base: Path | None = None) -> Path:
    """Write a rendered config under ``<repo>/.rendered/<name>/`` for bind-mounting."""
    base = base or SYSTEMS_DIR.parent / ".rendered"
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    path = d / filename
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)
    d.chmod(0o755)
    return path
