"""TPC-H DDL rendered per SQL dialect."""
from __future__ import annotations

from importlib import resources
from typing import Literal

from jinja2 import Environment, StrictUndefined

TABLES = ("region", "nation", "part", "supplier", "partsupp", "customer", "orders", "lineitem")
DIALECTS = ("mysql", "postgres")
TEMPLATE = "tpch.sql.j2"


def render_schema(
    dialect: str,
    engine_clause: str,
    kind: Literal["engine", "rowstore"],
    *,
    primary_keys: bool = True,
) -> str:
    """CREATE TABLE statements for the 8 TPC-H tables.

    ``engine_clause`` is appended after each column list (``ENGINE=DuckDB``,
    ``USING ducklake``, or empty). ``kind="rowstore"`` adds a plain index on every
    foreign-key column; ``kind="engine"`` adds none. ``primary_keys=False`` is for
    engines that cannot declare them (the adapter documents why).
    """
    if dialect not in DIALECTS:
        raise ValueError(f"unknown dialect {dialect!r}; expected one of {list(DIALECTS)}")
    if kind not in ("engine", "rowstore"):
        raise ValueError(f"unknown schema kind {kind!r}")
    text = (resources.files("dbench.schema") / TEMPLATE).read_text(encoding="utf-8")
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    return env.from_string(text).render(
        dialect=dialect, engine_clause=engine_clause.strip(), fk_indexes=kind == "rowstore",
        primary_keys=primary_keys,
    )


def split_statements(sql: str) -> list[str]:
    """Split rendered DDL on ';' (no literal contains one)."""
    return [s.strip() for s in sql.split(";") if s.strip()]
