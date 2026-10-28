"""VillageSQL Server 0.0.7 with the ``vsql_duckdb`` extension 0.0.3 (Track B).

VillageSQL is not a storage engine: ``duckdb_query(sql)`` runs DuckDB SQL over external files and
returns a JSON array as text. Every Track B query is the canonical DuckDB text with Parquet readers
for the TPC-H tables (``dbench.queries.trackb``), sent as ``SELECT duckdb_query('<escaped>')`` and
parsed back into rows. The image is built by ``systems/villagesql/Dockerfile``; settings come from
``systems/villagesql/my.cnf`` (MySQL side, rendered and mounted at /etc/my.cnf) and
``settings.sql`` (extension variables, rendered and run by the entrypoint on first start).
"""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal

from dbench import dockerctl
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.datagen import DatasetPaths, expected_rows
from dbench.schema import render_schema, split_statements
from dbench.systems import baseline_conf
from dbench.systems._common import LOAD_TIMEOUT_S, PROBE_TIMEOUT_S, container_data_path
from dbench.systems.base import LoadOutcome, Pushed, QueryOutcome
from dbench.systems.mysql_family import DATABASE, LOAD_SQL, MySQLFamilyAdapter, _code

IMAGE = "dbench/villagesql:0.0.7-duckdb-0.0.3"
CONF_TARGET = "/etc/my.cnf"
SETTINGS_TARGET = "/docker-entrypoint-initdb.d/20-vsql-duckdb-settings.sql"
SPILL_DIR = "/var/lib/mysql/duckdb_tmp"
MAX_TIMEOUT_MS = 3_600_000  # the extension's upper bound for vsql_duckdb.timeout_ms
NO_STORAGE = ("VillageSQL is not a storage engine: queries run in DuckDB over Parquet files, "
              "so there is no table conversion or engine pushdown")
ER_VDF = 3200
RESULT_CAP = re.compile(r"vsql_duckdb: (?:result reached \d+ bytes|value is \d+ bytes, past)")
EXTENSION_DEADLINE = "passed vsql_duckdb.timeout_ms"


class NativeStatement(str):
    """A MySQL statement to run as written (the JSON_TABLE join), not as DuckDB text for
    ``duckdb_query()``; ``VillageSQL.execute`` sends it unwrapped."""


def render_config(limits: Limits) -> str:
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "villagesql" / "my.cnf",
        baseline_conf.duckdb_engine_settings(limits, SPILL_DIR))


def render_settings_sql(limits: Limits) -> str:
    cores = baseline_conf.cores(limits)
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / "villagesql" / "settings.sql",
        {"cores": cores, "memory_limit_mb": int(0.75 * limits.mem_gb * 1024),
         "spill_dir": SPILL_DIR, "timeout_ms": QUERY_TIMEOUT_S * 1000})


def escape_sql_literal(text: str) -> str:
    """``text`` as a MySQL single-quoted literal: backslash doubled, quote doubled (valid under
    the default sql_mode; the benchmark never enables NO_BACKSLASH_ESCAPES)."""
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def wrap_duckdb_query(sql: str) -> str:
    return f"SELECT duckdb_query({escape_sql_literal(sql)})"


def parse_json_rows(text: str) -> tuple[list[str], list[tuple], set[int]]:
    """Rows, column names and numeric column indexes of a ``duckdb_query`` result.

    The function returns a JSON array with one object per row (keys in column order). Integers,
    decimals, floats and booleans keep their JSON types; dates, timestamps and everything else
    arrive as strings, and the function exposes no column types. JSON numbers are parsed to
    ``int``/``Decimal`` (never binary float, so DECIMAL sums keep their digits); a column is numeric
    when its non-NULL values are numbers. Date strings are left as text, which
    ``correctness.compare`` equates with the reference's ``date`` by ISO text; a DECIMAL that
    arrives as a string is covered by the numeric columns of the reference (union in run_power).
    """
    objects = json.loads(text, parse_float=Decimal, object_pairs_hook=list)
    if not objects:
        return [], [], set()
    columns = [k for k, _ in objects[0]]
    rows = [tuple(v for _, v in obj) for obj in objects]
    numeric = {
        i for i in range(len(columns))
        if any(isinstance(r[i], (int, Decimal)) and not isinstance(r[i], bool) for r in rows)
        and all(r[i] is None or (isinstance(r[i], (int, Decimal)) and not isinstance(r[i], bool))
                for r in rows)
    }
    return columns, rows, numeric


def duckdb_version_from_status(status_json: str) -> str:
    return str(json.loads(status_json)["duckdb_version"]).removeprefix("v")


class VillageSQL(MySQLFamilyAdapter):
    """Track B only. ``execute`` takes DuckDB-dialect text; KILL does not reach the embedded
    DuckDB, so ``cancel()`` reports False and the power run restarts the container after a timeout
    (the extension's own ``timeout_ms`` normally stops the query first)."""

    name = "villagesql"
    track = "B"
    dialect = "duckdb"
    family = "villagesql"
    config_dir = "villagesql"
    image = IMAGE
    version_key = "villagesql"
    datadir = "/var/lib/mysql"
    # Started as mysql so the image entrypoint does not try to chown the read-only /data mount.
    container_user = "mysql"

    def container_env(self) -> dict[str, str]:
        return {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes", "MYSQL_DATABASE": DATABASE}

    def config_mounts(self, limits: Limits) -> dict:
        cnf = baseline_conf.write_rendered(
            render_config(limits), self.name, "my.cnf", self.rendered_dir)
        sql = baseline_conf.write_rendered(
            render_settings_sql(limits), self.name, "settings.sql", self.rendered_dir)
        return {dockerctl.host_path(cnf): {"bind": CONF_TARGET, "mode": "ro"},
                dockerctl.host_path(sql): {"bind": SETTINGS_TARGET, "mode": "ro"}}

    def supports(self, measure: str) -> tuple[bool, str | None]:
        if measure in ("M11", "M12"):
            return False, NO_STORAGE
        return True, None

    def versions(self) -> dict:
        info = super().versions()
        duck = "unknown"
        try:
            conn = self._connect(read_timeout=PROBE_TIMEOUT_S)
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT duckdb_status()")
                    duck = duckdb_version_from_status(cur.fetchone()[0])
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - versions() must not fail on a missing function
            pass
        return {**info, "extension": "vsql_duckdb 0.0.3", "abi": "development",
                "entry_point": "duckdb_query()", "duckdb": duck,
                "duckdb_source": "SELECT duckdb_status()"}

    # queries
    def _timeout_sql(self, timeout_s: int) -> str:
        """The extension's deadline is a global variable that does not rebuild the DuckDB
        instance when it changes; ``max_execution_time`` never reaches a function call."""
        return f"SET GLOBAL vsql_duckdb.timeout_ms = {min(int(timeout_s * 1000), MAX_TIMEOUT_MS)}"

    def _statement(self, sql: str) -> str:
        return sql if isinstance(sql, NativeStatement) else wrap_duckdb_query(sql)

    def _query_begin(self, cur) -> None:
        return None

    def _query_end(self, cur, token) -> Pushed:
        return "duckdb"  # the whole query is DuckDB's; there is no row-path to fall back to

    def _decode(self, rows, desc):
        if desc and not str(desc[0][0]).startswith("duckdb_query("):
            return super()._decode(rows, desc)  # a NativeStatement: ordinary rows
        if not rows or rows[0][0] is None:
            return [], [], set()
        columns, parsed, numeric = parse_json_rows(rows[0][0])
        return parsed, columns, numeric

    def _classify_error(self, exc, elapsed_s, timeout_s) -> QueryOutcome | None:
        if _code(exc) != ER_VDF:
            return None
        message = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
        text = f"{type(exc).__name__}: {message}"
        if EXTENSION_DEADLINE in message:
            return QueryOutcome("timeout", elapsed_s, None, set(), text, "duckdb")
        if RESULT_CAP.search(message):
            return QueryOutcome("error", elapsed_s, None, set(), text, "duckdb",
                                extra={"kind": "result_cap"})
        return None

    def pushdown(self, sql: str) -> Pushed:
        return "duckdb"

    # the one table that lives in MySQL: nation, for the JSON_TABLE join example
    def create_schema(self, sf, kind) -> None:
        self._run_statements(["DROP TABLE IF EXISTS nation"])
        ddl = render_schema("mysql", "ENGINE=InnoDB", "engine")
        self._run_statements([s for s in split_statements(ddl) if "TABLE nation" in s])

    def load_bulk(self, data: DatasetPaths) -> LoadOutcome:
        """Load ``nation`` (InnoDB) from its CSV; the other tables stay Parquet-only."""
        started = time.perf_counter()
        try:
            conn = self._connect(read_timeout=LOAD_TIMEOUT_S)
            try:
                with conn.cursor() as cur:
                    for part in data.tables["nation"]:
                        path = container_data_path(self.data_dir, part)
                        cur.execute(LOAD_SQL.format(path=path, table="nation"))
                    cur.execute("SELECT COUNT(*) FROM nation")
                    actual = cur.fetchone()[0]
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return self._load_failure(exc, started)
        elapsed = time.perf_counter() - started
        expected = expected_rows(data)["nation"]
        if actual != expected:
            return LoadOutcome("error", elapsed, "nation row count does not match the manifest",
                               {"kind": "row_count", "expected": expected, "actual": actual})
        return LoadOutcome("ok", elapsed, None, {"rows": {"nation": actual}})

    def cancel(self) -> bool:
        """``KILL QUERY`` does not reach a running ``duckdb_query()``; nothing can be cancelled
        from outside, so report that and let the caller restart the server."""
        self.active.pop_expired()
        return False
