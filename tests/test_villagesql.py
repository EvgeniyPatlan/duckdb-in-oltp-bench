import json
import os
import re
from decimal import Decimal
from pathlib import Path

import pymysql
import pytest

from dbench.config import Limits
from dbench.systems.registry import get_adapter
from dbench.systems.villagesql import (
    IMAGE,
    VillageSQL,
    escape_sql_literal,
    parse_json_rows,
    render_config,
    render_settings_sql,
    wrap_duckdb_query,
)

PLAN = Limits("0-15", "16-23", 64)
SMOKE = Limits("0-7", "8-11", 16)
CAP_ERROR = (3200, "VDF error in function 'duckdb_query': vsql_duckdb: result reached 1048576 "
                   "bytes at row 8748, the limit set by vsql_duckdb.max_result_bytes. "
                   "Narrow the query or add a LIMIT")
TIMEOUT_ERROR = (3200, "VDF error in function 'duckdb_query': vsql_duckdb: query passed "
                       "vsql_duckdb.timeout_ms (300000 ms) and was stopped")


def mysql_unquote(literal: str) -> str:
    """What MySQL's lexer makes of a single-quoted literal (default sql_mode)."""
    assert literal[0] == literal[-1] == "'"
    body, out, i = literal[1:-1], [], 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            out.append(body[i + 1])
            i += 2
        elif c == "'":
            assert body[i + 1] == "'", "unescaped quote"
            out.append("'")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def test_sql_escaping_roundtrip():
    inner = ("SELECT 'it''s', '\\', 'a\\'b', \"x\\\\y\", '%\\_' "
             "FROM read_parquet('/data/p/*.parquet')")
    wrapped = wrap_duckdb_query(inner)
    assert wrapped.startswith("SELECT duckdb_query(") and wrapped.endswith(")")
    literal = wrapped[len("SELECT duckdb_query("):-1]
    assert mysql_unquote(literal) == inner
    assert mysql_unquote(escape_sql_literal(inner)) == inner
    assert mysql_unquote("'" + pymysql.converters.escape_string(inner) + "'") == inner


def test_json_rows_parsed_with_types():
    text = ('[{"a":1,"b":2.50,"c":"1998-01-01","d":null,"e":1.5e-3,"f":"x"},'
            '{"a":2,"b":10,"c":"1998-01-02","d":null,"e":2.0,"f":null}]')
    columns, rows, numeric = parse_json_rows(text)
    assert columns == ["a", "b", "c", "d", "e", "f"]
    assert rows[0] == (1, Decimal("2.50"), "1998-01-01", None, Decimal("0.0015"), "x")
    assert isinstance(rows[0][1], Decimal) and rows[1][1] == 10 and rows[1][5] is None
    assert numeric == {0, 1, 4}


def test_json_empty_result_and_duplicate_names_and_float_strings():
    assert parse_json_rows("[]") == ([], [], set())
    columns, rows, _ = parse_json_rows('[{"x":1,"x":2}]')
    assert columns == ["x", "x"] and rows == [(1, 2)]
    _, rows, numeric = parse_json_rows('[{"v":"nan"}]')
    assert rows == [("nan",)] and numeric == set()


def test_json_preserves_decimal_precision_and_bool_is_not_numeric():
    _, rows, numeric = parse_json_rows('[{"v":123456789012345.67,"b":true}]')
    assert rows[0][0] == Decimal("123456789012345.67") and numeric == {0}


class Cur:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.executed = []
        self.description = (("duckdb_query(...)", 253),)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, args=None):
        self.executed.append(sql)
        if self._error and sql.startswith("SELECT duckdb_query"):
            raise pymysql.err.OperationalError(*self._error)

    def fetchall(self):
        return [(self._result,)]

    def fetchone(self):
        return (0,)


class Conn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def thread_id(self):
        return 7

    def close(self):
        pass


def adapter_with(monkeypatch, cur):
    a = VillageSQL(None)
    monkeypatch.setattr(a, "_connect", lambda **kw: Conn(cur))
    return a


def test_execute_wraps_sql_and_parses_json(monkeypatch):
    cur = Cur('[{"n":3,"s":"a"}]')
    out = adapter_with(monkeypatch, cur).execute("SELECT 3 AS n, 'a' AS s", 30)
    assert out.status == "ok" and out.rows == [(3, "a")] and out.columns == ["n", "s"]
    assert out.numeric_cols == {0} and out.pushed == "duckdb"
    assert cur.executed[-1] == "SELECT duckdb_query('SELECT 3 AS n, ''a'' AS s')"


def test_result_cap_classified(monkeypatch):
    out = adapter_with(monkeypatch, Cur(error=CAP_ERROR)).execute("SELECT 1", 30)
    assert out.status == "error" and out.extra["kind"] == "result_cap"
    assert "max_result_bytes" in out.error


def test_value_cap_and_buffer_cap_are_result_caps(monkeypatch):
    for msg in ("vsql_duckdb: result reached 1048576 bytes at row 3, the limit set by the "
                "result buffer this function declares. Narrow the query or add a LIMIT",
                "vsql_duckdb: value is 70000 bytes, past the 65536 byte limit set by "
                "the result buffer this function declares"):
        out = adapter_with(monkeypatch, Cur(error=(3200, msg))).execute("SELECT 1", 30)
        assert out.status == "error" and out.extra["kind"] == "result_cap", msg


def test_duckdb_error_is_a_plain_error(monkeypatch):
    err = (3200, "VDF error in function 'duckdb_query': vsql_duckdb: Catalog Error: nope")
    out = adapter_with(monkeypatch, Cur(error=err)).execute("SELECT 1", 30)
    assert out.status == "error" and "kind" not in out.extra


def test_extension_deadline_is_a_timeout(monkeypatch):
    out = adapter_with(monkeypatch, Cur(error=TIMEOUT_ERROR)).execute("SELECT 1", 300)
    assert out.status == "timeout"


def test_cancel_is_unavailable():
    assert VillageSQL(None).cancel() is False


def test_timeout_statement_sets_the_extension_deadline_in_ms():
    assert VillageSQL(None)._timeout_sql(300) == "SET GLOBAL vsql_duckdb.timeout_ms = 300000"


def test_registry_and_contract():
    a = get_adapter("villagesql", None)
    assert isinstance(a, VillageSQL)
    assert (a.name, a.track, a.family, a.dialect, a.config_dir) == (
        "villagesql", "B", "villagesql", "duckdb", "villagesql")
    assert IMAGE == "dbench/villagesql:0.0.7-duckdb-0.0.3"
    assert a.supports("M11")[0] is False and a.supports("M12")[0] is False
    assert a.supports("M1") == (True, None)


def kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[", "--")):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def test_config_at_plan_limits():
    got = kv(render_config(PLAN))
    expected = {"skip-log-bin": "", "secure_file_priv": "/data", "local_infile": "OFF",
                "max_connections": "200", "innodb_buffer_pool_size": "4G",
                "innodb_redo_log_capacity": "8G", "innodb_parallel_read_threads": "16",
                "tmp_table_size": "4G", "max_heap_table_size": "4G",
                "character-set-server": "utf8mb4"}
    for key, value in expected.items():
        assert got[key] == value, key


def test_extension_settings_at_plan_limits():
    sql = render_settings_sql(PLAN)
    got = {m[0]: m[1] for m in re.findall(r"SET PERSIST vsql_duckdb\.(\w+) = ([^;]+);", sql)}
    assert got == {"threads": "16", "memory_limit_mb": "49152", "allow_local_files": "ON",
                   "temp_directory": "'/var/lib/mysql/duckdb_tmp'", "timeout_ms": "900000",
                   "max_result_bytes": "16777216"}


def test_extension_settings_at_16_gb():
    sql = render_settings_sql(SMOKE)
    assert "vsql_duckdb.threads = 8;" in sql and "vsql_duckdb.memory_limit_mb = 12288;" in sql
    assert kv(render_config(SMOKE))["innodb_buffer_pool_size"] == "1G"


def test_every_setting_has_a_reason_comment():
    cnf = render_config(PLAN).splitlines()
    for i, line in enumerate(cnf):
        if line.strip() and not line.startswith(("#", "[")):
            assert cnf[i - 1].startswith("#"), line
    sql = render_settings_sql(PLAN).splitlines()
    for i, line in enumerate(sql):
        if line.startswith("SET PERSIST"):
            assert sql[i - 1].startswith("--"), line


def _start_empty(limits=None):
    limits = limits or Limits("0-3", "8-11", 8)
    empty = Path(__file__).resolve().parents[1] / ".rendered" / "empty-data"
    empty.mkdir(parents=True, exist_ok=True)
    a = VillageSQL(empty)
    a.remove_data()
    a.start(limits)
    return a


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_live_settings_match_config():
    a = _start_empty()
    try:
        conn = a.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT @@vsql_duckdb.threads, @@vsql_duckdb.memory_limit_mb, "
                        "@@vsql_duckdb.allow_local_files, @@vsql_duckdb.timeout_ms, "
                        "@@vsql_duckdb.max_result_bytes, @@vsql_duckdb.temp_directory, "
                        "@@log_bin, @@secure_file_priv, @@innodb_buffer_pool_size")
            assert cur.fetchone() == (4, 6144, 1, 900000, 16777216,
                                      "/var/lib/mysql/duckdb_tmp", 0, "/data/", 512 << 20)
            cur.execute("SELECT duckdb_status()")
            status = json.loads(cur.fetchone()[0])
            assert status["duckdb_version"] == "v1.5.5" and status["engine_error"] == ""
        finally:
            conn.close()
        v = a.versions()
        assert v["duckdb"] == "1.5.5" and v["villagesql"].startswith("8.4.")
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_live_escaping_roundtrip():
    a = _start_empty()
    try:
        out = a.execute("SELECT 'it''s' AS a, '\\' AS b", 30)
        assert out.status == "ok" and out.rows == [("it's", "\\")]
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_live_result_cap_is_16_mib_on_the_development_abi():
    """The image is built against the development ABI, so max_result_bytes (16 MiB) is the
    effective limit: about 2 MiB (and 4.6 MB) come back whole, above 16 MiB is a result_cap."""
    a = _start_empty()
    try:
        out = a.execute("SELECT i, repeat('x', 200) AS pad FROM range(10000) t(i)", 60)
        assert out.status == "ok" and len(out.rows) == 10000, out  # ~2.2 MiB of JSON
        out = a.execute("SELECT i, repeat('x', 200) AS pad FROM range(20000) t(i)", 60)
        assert out.status == "ok" and len(out.rows) == 20000, out  # ~4.5 MiB, over the old 1 MiB
        out = a.execute("SELECT i, repeat('x', 200) AS pad FROM range(100000) t(i)", 120)
        assert out.status == "error" and out.extra["kind"] == "result_cap", out  # ~22 MiB
        assert "16777216" in out.error and "max_result_bytes" in out.error
        assert a.execute("SELECT 1 AS one", 10).rows == [(1,)]
        assert a.versions()["abi"] == "development"
    finally:
        a.remove_data()


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_heavy_duckdb_query_is_stopped_by_timeout_ms_and_server_recovers():
    a = _start_empty()
    try:
        out = a.execute("SELECT count(*) FROM range(3000000) x(a), range(3000000) y(b) "
                        "WHERE (a + b) % 7 = 3", 5)
        assert out.status == "timeout", out
        assert out.elapsed_s is not None and out.elapsed_s < 60
        assert a.cancel() is False
        assert a.lingering_queries() == 0  # timeout_ms stopped it; nothing keeps running
        assert a.execute("SELECT 1 AS one", 10).rows == [(1,)]
    finally:
        a.remove_data()


def test_join_statement_embeds_the_aggregate_as_one_escaped_literal():
    from dbench.measures.villagesql import _AGGREGATE, JOIN_REFERENCE, JOIN_VILLAGESQL
    from dbench.systems.villagesql import NativeStatement

    assert isinstance(JOIN_VILLAGESQL, NativeStatement)
    start = JOIN_VILLAGESQL.index("duckdb_query(") + len("duckdb_query(")
    end = JOIN_VILLAGESQL.index("), '$[*]'")
    assert mysql_unquote(JOIN_VILLAGESQL[start:end]) == _AGGREGATE
    assert "JOIN nation n ON n.n_nationkey = t.c_nationkey" in JOIN_VILLAGESQL
    assert "read_parquet('/data/parquet/nation/nation.*.parquet')" in JOIN_REFERENCE


def test_native_statement_is_not_wrapped(monkeypatch):
    from dbench.systems.villagesql import NativeStatement

    cur = Cur()
    cur.description = (("n_name", 253), ("customers", 8))
    cur.fetchall = lambda: [("FRANCE", 3)]
    out = adapter_with(monkeypatch, cur).execute(NativeStatement("SELECT 1"), 30)
    assert cur.executed[-1] == "SELECT 1"
    assert out.rows == [("FRANCE", 3)] and out.columns == ["n_name", "customers"]


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_restart_keeps_extension_settings_and_data():
    a = _start_empty()
    try:
        assert a.execute("SELECT 41 + 1 AS answer", 30).rows == [(42,)]
        a.restart()  # what the power run does after a timeout: new container, same volume
        out = a.execute("SELECT 41 + 1 AS answer", 30)
        assert out.status == "ok" and out.rows == [(42,)]
        conn = a.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT @@vsql_duckdb.threads, @@vsql_duckdb.allow_local_files")
            assert cur.fetchone() == (4, 1)
        finally:
            conn.close()
    finally:
        a.remove_data()
