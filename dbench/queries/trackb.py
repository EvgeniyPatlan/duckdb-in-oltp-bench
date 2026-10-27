"""Track B query text: the canonical DuckDB TPC-H queries reading Parquet instead of tables.

Track B systems run raw DuckDB SQL over the dataset's Parquet files, so every TPC-H table
reference is replaced by ``read_parquet('<glob>') [AS <name>]``. One function does the
substitution for every system (:func:`substitute_parquet`); nothing else in the query changes.
"""
from __future__ import annotations

import difflib
import re
from collections.abc import Mapping

from dbench.datagen import DatasetPaths
from dbench.queries.dialect import load_query
from dbench.schema import TABLES
from dbench.systems._common import DATA_MOUNT, container_data_path

PARQUET_SUBDIR = "parquet"

_TOKEN = re.compile(
    r"""(?P<string>'(?:[^']|'')*')"""
    r"""|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"""
    r"""|(?P<space>\s+)"""
    r"""|(?P<punct>.)""",
    re.DOTALL,
)
# Words that end a table reference without being its alias.
_NOT_ALIASES = frozenset({
    "where", "group", "order", "having", "limit", "offset", "on", "using", "join", "left", "right",
    "inner", "outer", "full", "cross", "natural", "asof", "positional", "semi", "anti", "union",
    "except", "intersect", "window", "qualify", "tablesample", "as",
})
_CLAUSE_END = frozenset({
    "where", "group", "order", "having", "limit", "offset", "union", "except", "intersect",
    "window", "qualify", "select",
})


def default_globs(data_mount: str = DATA_MOUNT) -> dict[str, str]:
    """Container-side glob of a table's Parquet parts (``parquet/<t>/<t>.<k>.parquet``)."""
    return {t: f"{data_mount}/{PARQUET_SUBDIR}/{t}/{t}.*.parquet" for t in TABLES}


def globs_from_dataset(data: DatasetPaths, data_dir) -> dict[str, str]:
    """Globs derived from a dataset's manifest, spelled with the container's /data mount."""
    out = {}
    for table, parts in data.parquet_tables.items():
        parent = container_data_path(data_dir, parts[0]).rsplit("/", 1)[0]
        out[table] = f"{parent}/{table}.*.parquet"
    return out


def _tokens(sql: str) -> list[tuple[str, str]]:
    return [(m.lastgroup, m.group()) for m in _TOKEN.finditer(sql)]


def _next_significant(tokens: list[tuple[str, str]], i: int) -> tuple[str, str] | None:
    for kind, text in tokens[i:]:
        if kind != "space":
            return kind, text
    return None


def substitute_parquet(sql: str, globs: Mapping[str, str]) -> str:
    """Replace each TPC-H table reference (the word after FROM, JOIN or a comma of a FROM list)
    by ``read_parquet('<glob>')``. A table without an explicit alias gets ``AS <table>``, so
    column references such as ``orders.o_orderkey`` keep working; an explicit alias
    (``nation n1``, ``lineitem AS l1``) is kept as written. Words inside string literals and
    column aliases that merely share a table's name (``... AS nation``) are left alone.
    """
    tokens = _tokens(sql)
    tables = set(TABLES)
    out: list[str] = []
    # per parenthesis depth: True while that depth is inside a FROM list
    in_from = [False]
    expect_table = False
    for i, (kind, text) in enumerate(tokens):
        low = text.lower()
        if kind == "punct" and text == "(":
            in_from.append(False)
            expect_table = False
        elif kind == "punct" and text == ")":
            if len(in_from) > 1:
                in_from.pop()
            expect_table = False
        elif kind == "punct" and text == ",":
            expect_table = in_from[-1]
        elif kind == "word":
            if low in ("from", "join"):
                in_from[-1] = True
                expect_table = True
            elif low in _CLAUSE_END:
                in_from[-1] = False
                expect_table = False
            elif expect_table and low in tables:
                if low not in globs:
                    raise KeyError(f"no Parquet glob for table {low!r}")
                out.append(f"read_parquet('{globs[low]}')")
                nxt = _next_significant(tokens, i + 1)
                has_alias = nxt is not None and nxt[0] == "word" and (
                    nxt[1].lower() == "as" or nxt[1].lower() not in _NOT_ALIASES)
                if not has_alias:
                    out.append(f" AS {text}")
                expect_table = False
                continue
            else:
                expect_table = False
        elif kind != "space":
            expect_table = False
        out.append(text)
    return "".join(out)


def trackb_query(n: int, globs: Mapping[str, str] | None = None) -> str:
    """Canonical DuckDB text of TPC-H query ``n`` with Parquet readers for the tables."""
    return substitute_parquet(load_query(n), globs if globs is not None else default_globs())


def trackb_diff(n: int, globs: Mapping[str, str] | None = None) -> str:
    """Unified diff of query ``n`` between the canonical text and its Track B form."""
    src = load_query(n)
    dst = trackb_query(n, globs)
    return "".join(difflib.unified_diff(
        src.splitlines(keepends=True), dst.splitlines(keepends=True),
        fromfile=f"q{n:02d}.duckdb.sql", tofile=f"q{n:02d}.trackb.sql"))
