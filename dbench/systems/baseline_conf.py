"""Server configs for the row-store baselines, rendered from the container limits."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, StrictUndefined

from dbench import dockerctl
from dbench.config import Limits

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


def postgres_settings(limits: Limits) -> dict:
    return {
        "shared_buffers": size(0.25 * limits.mem_gb, "GB"),
        "effective_cache_size": size(0.75 * limits.mem_gb, "GB"),
        "workers": cores(limits),
        "max_wal_size": size(limits.mem_gb / 4, "GB"),
    }


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
