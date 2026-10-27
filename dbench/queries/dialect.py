"""Canonical TPC-H query text and mechanical dialect translation.

The 22 queries are exported from DuckDB's ``tpch`` extension and stored
unchanged under ``dbench/queries/tpch``. Other dialects are produced only by
the ordered regex rules in :data:`RULES`; a query that still fails after
translation is recorded as failed, never hand-rewritten.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from importlib import resources
from typing import Literal, NamedTuple

Dialect = Literal["duckdb", "postgres", "mysql"]


class Rule(NamedTuple):
    pattern: re.Pattern[str]
    replacement: str | Callable[[re.Match[str]], str]
    comment: str


def _upper_unit(fmt: str) -> Callable[[re.Match[str]], str]:
    def repl(m: re.Match[str]) -> str:
        return fmt.format(*m.groups()[:-1], m.group(m.lastindex).upper())

    return repl


_UNIT = r"(year|month|day)"

# "mysql" covers MySQL, MariaDB, AliSQL and VillageSQL.
_MYSQL_RULES: tuple[Rule, ...] = (
    Rule(
        re.compile(rf"(date '[0-9-]+')\s*\+\s*interval '([0-9]+)' {_UNIT}", re.I),
        _upper_unit("DATE_ADD({0}, INTERVAL {1} {2})"),
        "date + interval 'N' unit -> DATE_ADD (classic TPC-H text; "
        "inert on the DuckDB 1.5.5 export, which is already folded; kept for other sources)",
    ),
    Rule(
        re.compile(rf"(date '[0-9-]+')\s*-\s*interval '([0-9]+)' {_UNIT}", re.I),
        _upper_unit("DATE_SUB({0}, INTERVAL {1} {2})"),
        "date - interval 'N' unit -> DATE_SUB (classic TPC-H text; "
        "inert on the DuckDB 1.5.5 export)",
    ),
    Rule(
        re.compile(rf"interval '([0-9]+)' {_UNIT}", re.I),
        _upper_unit("INTERVAL {0} {1}"),
        "bare interval 'N' unit -> INTERVAL N UNIT (unquoted amount)",
    ),
    Rule(
        re.compile(r"'([^']*)'::date", re.I),
        r"DATE '\1'",
        "'x'::date -> DATE 'x' (PostgreSQL-style cast not accepted by MySQL; inert on the "
        "DuckDB 1.5.5 export)",
    ),
    Rule(
        re.compile(r"(count\(o_orderkey\))(\s+FROM\b)", re.I),
        r"\1 AS c_count\2",
        "Q13: name the aggregate in the inner select so the column-list rule below can "
        "drop the list; MySQL 9.7 accepts the untranslated query, kept for MariaDB",
    ),
    Rule(
        re.compile(r"(\)\s+AS\s+c_orders)\s*\([^)]*\)", re.I),
        r"\1",
        "Q13: drop the derived-table column list '(c_custkey, c_count)'; MySQL 9.7 accepts "
        "it (supported since 8.0.19), kept for MariaDB, not yet verified there",
    ),
)

# Postgres runs the DuckDB text as is: every canonical query parses there.
RULES: dict[str, tuple[Rule, ...]] = {
    "duckdb": (),
    "postgres": (),
    "mysql": _MYSQL_RULES,
}


def _rules_for(dialect: str) -> tuple[Rule, ...]:
    try:
        return RULES[dialect]
    except KeyError:
        raise ValueError(f"unknown dialect {dialect!r}; expected one of {sorted(RULES)}") from None


def load_query(n: int) -> str:
    """Return the canonical DuckDB text of TPC-H query ``n`` (1..22)."""
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 22:
        raise ValueError(f"TPC-H query number must be in 1..22, got {n!r}")
    path = resources.files("dbench.queries") / "tpch" / f"q{n:02d}.sql"
    return path.read_text(encoding="utf-8")


def translate(sql: str, dialect: Dialect) -> str:
    """Mechanically translate canonical DuckDB SQL into ``dialect``."""
    for rule in _rules_for(dialect):
        sql = rule.pattern.sub(rule.replacement, sql)
    return sql


def dialect_diff(n: int, dialect: Dialect) -> str:
    """Unified diff of query ``n`` between DuckDB text and ``dialect``; '' if identical."""
    _rules_for(dialect)
    src = load_query(n)
    dst = translate(src, dialect)
    return "".join(
        difflib.unified_diff(
            src.splitlines(keepends=True),
            dst.splitlines(keepends=True),
            fromfile=f"q{n:02d}.duckdb.sql",
            tofile=f"q{n:02d}.{dialect}.sql",
        )
    )
