"""Track B variants of the Track A servers: raw DuckDB SQL through each system's own entry point.

Track B runs the canonical DuckDB query text (with Parquet readers, ``dbench.queries.trackb``)
through ``duckdb.query()`` on pg_duckdb and ``run_in_duckdb()`` on MariaDB, next to plain DuckDB
and VillageSQL's ``duckdb_query()``. The variants only change the statement sent and how its
result is read; images, settings and containers are the Track A ones.
"""
from __future__ import annotations

from dbench.systems.base import Pushed
from dbench.systems.duckdb_native import DuckDBNative
from dbench.systems.mariadb import MariaDBDuckDB, parse_run_in_duckdb, result_text
from dbench.systems.pgduckdb import PgDuckDB
from dbench.systems.villagesql import escape_sql_literal

DOLLAR_TAG = "$dbq$"


def duckdb_query_statement(sql: str) -> str:
    """pg_duckdb: ``duckdb.query()`` runs DuckDB SQL and returns its rows as a table."""
    if DOLLAR_TAG in sql:
        raise ValueError(f"query text contains the quoting tag {DOLLAR_TAG}")
    return f"SELECT * FROM duckdb.query({DOLLAR_TAG}{sql}{DOLLAR_TAG})"


class PgDuckDBTrackB(PgDuckDB):
    """pg_duckdb, queried with ``duckdb.query()`` over Parquet instead of heap tables."""

    name = "pg_duckdb-trackb"
    track = "B"
    pushdown_counter_scope = "none"  # raw DuckDB entry point: pushed is a constant
    dialect = "duckdb"

    def _statement(self, sql: str) -> str:
        return duckdb_query_statement(sql)

    def _query_begin(self, conn, sql: str) -> Pushed:
        return "duckdb"  # raw DuckDB entry point: nothing to fall back from

    def versions(self) -> dict:
        return {**super().versions(), "entry_point": "duckdb.query()"}

    def pushdown(self, sql: str) -> Pushed:
        return "duckdb"


class MariaDBTrackB(MariaDBDuckDB):
    """MariaDB, queried with ``run_in_duckdb()`` over Parquet (Track A's server and settings)."""

    name = "mariadb-trackb"
    track = "B"
    pushdown_counter_scope = "none"  # raw DuckDB entry point: pushed is a constant
    dialect = "duckdb"

    def _statement(self, sql: str) -> str:
        return f"SELECT run_in_duckdb({escape_sql_literal(sql)})"

    def _query_begin(self, cur) -> None:
        return None

    def _query_end(self, cur, token) -> Pushed:
        return "duckdb"

    def _decode(self, rows, desc):
        if not rows or rows[0][0] is None:
            return [], [], set()
        columns, parsed, numeric = parse_run_in_duckdb(result_text(rows[0][0]))
        return parsed, columns, numeric

    def versions(self) -> dict:
        return {**super().versions(), "entry_point": "run_in_duckdb()"}

    def pushdown(self, sql: str) -> Pushed:
        return "duckdb"


class DuckDBTrackB(DuckDBNative):
    """Plain DuckDB as the Track B reference: the Track A query server, reading Parquet files."""

    track = "B"

    def versions(self) -> dict:
        return {**super().versions(), "entry_point": "read_parquet() in the DuckDB query server"}
