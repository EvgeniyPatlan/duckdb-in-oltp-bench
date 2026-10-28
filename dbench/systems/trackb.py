"""Track B variants of the Track A servers: raw DuckDB SQL through each system's own entry point.

Track B runs the canonical DuckDB query text (with Parquet readers, ``dbench.queries.trackb``)
through ``duckdb.query()`` on pg_duckdb and ``run_in_duckdb()`` on MariaDB, next to plain DuckDB
and VillageSQL's ``duckdb_query()``. The variants only change the statement sent and how its
result is read; images, settings and containers are the Track A ones (MariaDB additionally sets
``duckdb_allow_run_in_duckdb = ON``, see ``systems/mariadb/trackb.cnf``).
"""
from __future__ import annotations

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems import baseline_conf
from dbench.systems.base import Pushed, is_numeric_type
from dbench.systems.duckdb_native import DuckDBNative
from dbench.systems.mariadb import CONF_TARGET, MariaDBDuckDB, render_config
from dbench.systems.pgduckdb import PgDuckDB
from dbench.systems.villagesql import escape_sql_literal

DOLLAR_TAG = "$dbq$"


def duckdb_query_statement(sql: str) -> str:
    """pg_duckdb: ``duckdb.query()`` runs DuckDB SQL and returns its rows as a table."""
    if DOLLAR_TAG in sql:
        raise ValueError(f"query text contains the quoting tag {DOLLAR_TAG}")
    return f"SELECT * FROM duckdb.query({DOLLAR_TAG}{sql}{DOLLAR_TAG})"


def parse_run_in_duckdb(text: str) -> tuple[list[str], list[tuple], set[int]]:
    """Rows, column names and numeric column indexes of a ``run_in_duckdb()`` result.

    The function returns text: a line of tab-terminated column names, a line of tab-terminated
    DuckDB type names, ``[ Rows: N]``, then N rows of tab-separated values (NULL is the word
    ``NULL``) and a closing blank line. Values stay strings; numeric columns are
    taken from the type line. A value containing a tab or newline would break the framing; the
    row count in the header is checked against the rows read.
    """
    if not text.endswith("\n\n"):
        raise ValueError(f"unrecognised run_in_duckdb result: {text[:80]!r}")
    lines = text[:-2].split("\n")  # the closing blank line is not a row
    if len(lines) < 3 or not lines[2].startswith("[ Rows: "):
        raise ValueError(f"unrecognised run_in_duckdb result: {text[:80]!r}")
    columns = lines[0].rstrip("\t").split("\t") if lines[0] else []
    types = lines[1].rstrip("\t").split("\t") if lines[1] else []
    count = int(lines[2][len("[ Rows: "):].rstrip("]"))
    body = lines[3:]
    if len(body) != count:
        raise ValueError(f"run_in_duckdb returned {len(body)} rows, header says {count}")
    rows = []
    for line in body:
        cells = line.split("\t")
        if len(cells) != len(columns):
            raise ValueError("run_in_duckdb row does not match its header (tab or newline in a "
                             f"value?): {line[:80]!r}")
        rows.append(tuple(None if c == "NULL" else c for c in cells))
    numeric = {i for i, t in enumerate(types) if is_numeric_type(t)}
    return columns, rows, numeric


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
    """MariaDB, queried with ``run_in_duckdb()`` over Parquet; the server runs with
    ``duckdb_allow_run_in_duckdb = ON``."""

    name = "mariadb-trackb"
    track = "B"
    pushdown_counter_scope = "none"  # raw DuckDB entry point: pushed is a constant
    dialect = "duckdb"

    def config_mounts(self, limits: Limits) -> dict:
        extra = (baseline_conf.SYSTEMS_DIR / "mariadb" / "trackb.cnf").read_text(encoding="utf-8")
        path = baseline_conf.write_rendered(
            render_config(limits) + "\n" + extra, self.name, "dbench.cnf", self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}

    def _statement(self, sql: str) -> str:
        return f"SELECT run_in_duckdb({escape_sql_literal(sql)})"

    def _query_begin(self, cur) -> None:
        return None

    def _query_end(self, cur, token) -> Pushed:
        return "duckdb"

    def _decode(self, rows, desc):
        if not rows or rows[0][0] is None:
            return [], [], set()
        text = rows[0][0]
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        columns, parsed, numeric = parse_run_in_duckdb(text)
        return parsed, columns, numeric

    def versions(self) -> dict:
        return {**super().versions(), "entry_point": "run_in_duckdb()",
                "duckdb_allow_run_in_duckdb": "ON (Track B only)"}

    def pushdown(self, sql: str) -> Pushed:
        return "duckdb"


class DuckDBTrackB(DuckDBNative):
    """Plain DuckDB as the Track B reference: the Track A query server, reading Parquet files."""

    track = "B"

    def versions(self) -> dict:
        return {**super().versions(), "entry_point": "read_parquet() in the DuckDB query server"}
