"""Every non-default setting of each system, read from the published files in ``systems/``.

The templates are rendered with the talk limits (64 GB, 16 cores) so the page shows the values
the benchmark used. A comment block right above a setting is its reason.
"""
from __future__ import annotations

import re
from pathlib import Path

from dbench.config import Limits
from dbench.systems import baseline_conf as bc
from dbench.systems.alisql import SPILL_DIR as MYSQL_SPILL_DIR

SYSTEMS_DIR = bc.SYSTEMS_DIR
CONFIG_FILES = {
    "alisql": ["my.cnf"], "mariadb": ["duckdb.cnf", "trackb.cnf"], "percona": ["mysqld-args.j2"],
    "innodb": ["my.cnf"], "postgres": ["postgresql.conf"], "pgduckdb": ["postgresql.conf"],
    "pgducklake": ["postgresql.conf"], "villagesql": ["my.cnf", "settings.sql"],
}
_INI = re.compile(r"^([A-Za-z][\w.-]*)\s*(?:=\s*(.*))?$")
_ARG = re.compile(r"^--([\w-]+)(?:=(.*))?$")
_SET = re.compile(r"^SET PERSIST\s+([\w.]+)\s*=\s*(.*?);?\s*$")


def variables(config_dir: str, limits: Limits) -> dict:
    engine = bc.duckdb_engine_settings(limits, MYSQL_SPILL_DIR)
    return {
        "alisql": engine, "mariadb": engine, "percona": engine, "villagesql": {
            **engine, "memory_limit_mb": int(0.75 * limits.mem_gb * 1024),
            "timeout_ms": 900_000},
        "innodb": bc.innodb_settings(limits), "postgres": bc.postgres_settings(limits),
        "pgduckdb": bc.duckdb_pg_settings(limits, "pgduckdb"),
        "pgducklake": bc.duckdb_pg_settings(limits, "pgducklake"),
    }.get(config_dir, {})


def _render(path: Path, config_dir: str, limits: Limits) -> str:
    try:
        return bc.render(path, variables(config_dir, limits))
    except Exception:  # noqa: BLE001 - an unrenderable template is shown as written
        return path.read_text(encoding="utf-8")


def _entry(line: str) -> tuple[str, str] | None:
    for pattern in (_SET, _ARG, _INI):
        m = pattern.match(line)
        if m:
            return m.group(1), (m.group(2) or "").strip()
    return None


def parse(text: str) -> list[dict]:
    """(key, value, reason) of each setting line.

    The comment lines directly above a setting are its reason. A blank line or a ``[section]``
    header ends a comment block, so a file header followed by a blank line is never attached to
    the first setting; a comment block with no blank line before the setting belongs to it.
    """
    out, reason = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or line.startswith("-- ") or line == "--":
            reason.append(line.lstrip("#-").strip())
            continue
        if not line or line.startswith("["):
            reason = []
            continue
        found = _entry(line)
        if found:
            out.append({"key": found[0], "value": found[1], "reason": " ".join(reason)})
        reason = []
    return out


def readme_settings(config_dir: str) -> list[dict]:
    """Rows of a ``Setting | Value | Reason`` table in the system's README (settings that are
    not in a config file, such as environment variables)."""
    path = SYSTEMS_DIR / config_dir / "README.md"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and line.lstrip().startswith("|") and "`" in cells[0]:
            for key in re.findall(r"`([A-Z][A-Z0-9_]+)`", cells[0]):
                rows.append({"key": key, "value": cells[1].replace("`", ""),
                             "reason": cells[2].replace("`", "")})
    return rows


def for_system(config_dir: str, limits: Limits | None = None) -> list[dict]:
    limits = limits or Limits()
    out: list[dict] = []
    for name in CONFIG_FILES.get(config_dir, []):
        path = SYSTEMS_DIR / config_dir / name
        for item in parse(_render(path, config_dir, limits)):
            out.append({**item, "file": f"systems/{config_dir}/{name}"})
    seen = {e["key"] for e in out}
    if (SYSTEMS_DIR / config_dir / "README.md").is_file():
        out += [{**r, "file": f"systems/{config_dir}/README.md"}
                for r in readme_settings(config_dir) if r["key"] not in seen]
    return out
