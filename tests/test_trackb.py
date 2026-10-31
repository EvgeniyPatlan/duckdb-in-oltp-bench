import re
from datetime import UTC, datetime

import pytest

from dbench.measures.power import run_power
from dbench.queries.dialect import load_query
from dbench.queries.trackb import (
    default_globs,
    substitute_parquet,
    trackb_diff,
    trackb_query,
)
from dbench.schema import TABLES
from tests.fakes import FakeAdapter, ok, timeout

GLOBS = default_globs()
HOST = {"cpus": 4, "mem_gb": 8, "kernel": "k", "docker_version": "d"}
KEYWORDS = {"where", "group", "order", "having", "limit", "on", "join", "left", "right", "inner",
            "outer", "full", "cross", "using", "union", "except", "intersect", "window"}


def table_refs(sql: str) -> list[tuple[str, str | None]]:
    """(table, alias) of every FROM/JOIN/comma table reference, read from the original text."""
    refs = []
    pattern = re.compile(r"\b(" + "|".join(TABLES) + r")\b(\s+(?:as\s+)?([a-z_][a-z0-9_]*))?",
                         re.IGNORECASE)
    for m in pattern.finditer(sql):
        before = sql[:m.start()].rstrip().lower()
        if not (before.endswith(("from", "join", ",")) or before.endswith("from")):
            continue
        alias = m.group(3)
        if alias and alias.lower() in KEYWORDS:
            alias = None
        refs.append((m.group(1).lower(), alias))
    return refs


def test_default_globs_cover_every_table():
    assert set(GLOBS) == set(TABLES)
    assert GLOBS["lineitem"] == "/data/parquet/lineitem/lineitem.*.parquet"


def test_plain_table_gets_its_own_name_as_alias():
    out = substitute_parquet("SELECT 1 FROM orders WHERE o_orderkey = 1", GLOBS)
    assert out == ("SELECT 1 FROM read_parquet('/data/parquet/orders/orders.*.parquet') "
                   "AS orders WHERE o_orderkey = 1")


def test_explicit_alias_is_kept_with_and_without_as():
    out = substitute_parquet("FROM nation n1, nation AS n2, region", GLOBS)
    assert "read_parquet('/data/parquet/nation/nation.*.parquet') n1," in out
    assert "read_parquet('/data/parquet/nation/nation.*.parquet') AS n2," in out
    assert out.endswith("read_parquet('/data/parquet/region/region.*.parquet') AS region")


def test_bare_column_alias_named_like_a_table_is_untouched():
    sql = "SELECT n2.n_name AS nation FROM nation n2) AS t WHERE nation = 'BRAZIL'"
    out = substitute_parquet(sql, GLOBS)
    assert "AS nation FROM" in out and "WHERE nation = 'BRAZIL'" in out


def test_string_literals_are_untouched():
    sql = "SELECT 1 FROM part WHERE p_name LIKE '%from orders%'"
    assert "'%from orders%'" in substitute_parquet(sql, GLOBS)


def test_join_keyword_after_table_is_not_taken_for_an_alias():
    out = substitute_parquet("FROM customer LEFT OUTER JOIN orders ON c_custkey = o_custkey", GLOBS)
    assert "customer.*.parquet') AS customer LEFT OUTER JOIN read_parquet(" in out
    assert "orders.*.parquet') AS orders ON" in out


@pytest.mark.parametrize("n", range(1, 23))
def test_substitution_preserves_every_alias_in_all_22_queries(n):
    canonical = load_query(n)
    out = trackb_query(n, GLOBS)
    refs = table_refs(canonical)
    assert refs, f"q{n}: no table reference found by the test parser"
    produced = re.findall(
        r"read_parquet\('/data/parquet/(\w+)/\w+\.\*\.parquet'\)(?:\s+(?:AS\s+)?(\w+))?", out)
    assert len(produced) == len(refs)
    for (table, alias), (got_table, got_alias) in zip(refs, produced, strict=True):
        assert got_table == table
        assert got_alias.lower() == (alias or table).lower()
    # nothing but the table references changed: undoing them restores the canonical text
    restored = re.sub(
        r"read_parquet\('/data/parquet/(\w+)/\w+\.\*\.parquet'\)(\s+AS\s+\1\b)?", r"\1", out)
    assert re.sub(r"\s+", " ", restored) == re.sub(r"\s+", " ", canonical)


def test_substitution_leaves_no_table_in_a_from_position():
    for n in range(1, 23):
        out = trackb_query(n, GLOBS)
        assert not re.search(r"\b(from|join)\s+(" + "|".join(TABLES) + r")\b", out, re.I), n


def test_trackb_diff_shows_the_substitution_and_is_empty_free_of_other_changes():
    diff = trackb_diff(1, GLOBS)
    assert "read_parquet('/data/parquet/lineitem/lineitem.*.parquet')" in diff
    assert diff.startswith("--- q01.duckdb.sql")


def test_substitution_rejects_a_missing_glob():
    with pytest.raises(KeyError):
        substitute_parquet("FROM lineitem", {})


def test_run_power_uses_query_text_and_extra_items(tmp_path):
    a = FakeAdapter([ok(1.0), ok(1.0), ok(1.0), ok(1.0), ok(2.0), ok(2.0), ok(2.0), ok(2.0)])
    recs = run_power(
        a, 1, "r", tmp_path, None, queries=[3], drop_caches=lambda: None,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC), host=lambda: HOST,
        query_text=lambda n: f"Q{n} TEXT",
        extra_queries=[("join_innodb", "JOIN TEXT", None)],
    )
    assert [r.item for r in recs] == ["q03", "join_innodb"]
    assert a.executed[0] == "Q3 TEXT" and a.executed[4] == "JOIN TEXT"


class NoCancelAdapter(FakeAdapter):
    track = "B"

    def cancel(self) -> bool:
        self.cancels += 1
        return False


def test_cancel_false_forces_restart_on_timeout(tmp_path):
    a = NoCancelAdapter([timeout()], alive_after=[True], lingering=[0])
    recs = run_power(a, 1, "r", tmp_path, None, queries=[1, 2], drop_caches=lambda: None,
                     clock=lambda: datetime(2026, 1, 1, tzinfo=UTC), host=lambda: HOST)
    assert [r.status for r in recs] == ["timeout", "ok"]
    assert a.cancels == 1 and a.restarts == 1
    assert recs[0].extra["restarted_after_timeout"] is True
    assert recs[0].extra["cancel_unavailable"] is True
    assert recs[0].track == "B"


# --- Track B variants of pg_duckdb and MariaDB -------------------------------------------------

def test_pg_duckdb_statement_wraps_in_duckdb_query():
    from dbench.systems.trackb import duckdb_query_statement

    assert duckdb_query_statement("SELECT 1") == "SELECT * FROM duckdb.query($dbq$SELECT 1$dbq$)"
    with pytest.raises(ValueError):
        duckdb_query_statement("SELECT '$dbq$'")


# Real output of run_in_duckdb() on MariaDB 13.0.2, the same on 12.3.3 (tabs shown as \t).
RUN_IN_DUCKDB = ("a\tb\tc\td\te\t\nINTEGER\tVARCHAR\tDECIMAL(15,2)\tDATE\tDOUBLE\t\n"
                 "[ Rows: 2]\nNULL\t\t1.50\t1998-01-01\t1.5\n7\tx y\t2.00\t1998-01-02\t2.5\n\n")


def test_run_in_duckdb_text_is_parsed_with_its_types():
    from dbench.systems.mariadb import parse_run_in_duckdb

    columns, rows, numeric = parse_run_in_duckdb(RUN_IN_DUCKDB)
    assert columns == ["a", "b", "c", "d", "e"]
    assert rows[0] == (None, "", "1.50", "1998-01-01", "1.5")
    assert rows[1] == ("7", "x y", "2.00", "1998-01-02", "2.5")
    assert numeric == {0, 2, 4}


def test_run_in_duckdb_empty_result_and_bad_framing():
    from dbench.systems.mariadb import parse_run_in_duckdb

    assert parse_run_in_duckdb("range\t\nBIGINT\t\n[ Rows: 0]\n\n") == (["range"], [], {0})
    assert parse_run_in_duckdb("a\t\nVARCHAR\t\n[ Rows: 1]\n\n\n")[1] == [("",)]
    with pytest.raises(ValueError):
        parse_run_in_duckdb("not a result")
    with pytest.raises(ValueError):
        parse_run_in_duckdb("a\t\nINTEGER\t\n[ Rows: 2]\n1\n\n")


def test_run_in_duckdb_rows_compare_equal_to_typed_reference():
    from datetime import date
    from decimal import Decimal

    from dbench.correctness import compare
    from dbench.systems.mariadb import parse_run_in_duckdb

    _, rows, numeric = parse_run_in_duckdb(RUN_IN_DUCKDB)
    reference = [(None, "", Decimal("1.50"), date(1998, 1, 1), 1.5),
                 (7, "x y", Decimal("2.00"), date(1998, 1, 2), 2.5)]
    assert compare(reference, rows, numeric).match
    assert not compare(reference, [rows[0], rows[1][:2] + ("2.01",) + rows[1][3:]], numeric).match


def test_mariadb_trackb_uses_the_track_a_config():
    from dbench.config import Limits
    from dbench.systems.mariadb import MariaDBDuckDB, render_config
    from dbench.systems.trackb import MariaDBTrackB

    plan = Limits("0-15", "16-23", 64)
    assert "duckdb_allow_run_in_duckdb = ON" in render_config(plan).splitlines()
    assert MariaDBTrackB.config_mounts is MariaDBDuckDB.config_mounts
    a = MariaDBTrackB(None)
    assert (a.name, a.track, a.dialect, a.config_dir) == ("mariadb-trackb", "B", "duckdb",
                                                          "mariadb")
    assert a._statement("SELECT 'a'") == "SELECT run_in_duckdb('SELECT ''a''')"


def test_trackb_registry_names_and_tracks():
    from dbench.systems.registry import get_adapter

    for name, track in (("villagesql", "B"), ("pg_duckdb-trackb", "B"), ("mariadb-trackb", "B"),
                        ("pg_duckdb", "A"), ("mariadb", "A")):
        assert get_adapter(name, None).track == track, name
    assert get_adapter("pg_duckdb-trackb", None).config_dir == "pgduckdb"


def test_track_a_statements_are_unchanged():
    from dbench.systems.registry import get_adapter

    assert get_adapter("pg_duckdb", None)._statement("SELECT 1") == "SELECT 1"
    assert get_adapter("mariadb", None)._statement("SELECT 1") == "SELECT 1"
    assert get_adapter("innodb", None)._statement("SELECT 1") == "SELECT 1"


class StartFails(FakeAdapter):
    name = "villagesql"
    track = "B"
    config_dir = "villagesql"

    def start(self, limits):
        raise RuntimeError("extension would not load")

    def remove_data(self):
        pass


class Reference(FakeAdapter):
    name = "duckdb-1.5.5"
    track = "B"
    config_dir = "duckdb"

    def remove_data(self):
        pass


def test_run_track_b_records_a_failed_start_for_every_item(tmp_path):
    from dbench.config import Limits
    from dbench.measures.villagesql import run_track_b
    from dbench.results import iter_records

    def factory(name, data_dir):
        return StartFails([]) if name == "villagesql" else Reference([ok(1.0, [(1,)])] * 40)

    class Data:
        root = tmp_path

    recs = run_track_b(1, "r", tmp_path, ["villagesql"], data=Data,
                       limits=Limits("0-7", "8-11", 16), queries=[1, 2], warm_runs=0,
                       drop_caches=lambda: None,
                       clock=lambda: datetime(2026, 1, 1, tzinfo=UTC), host=lambda: HOST,
                       adapter_factory=factory)
    assert [(r.item, r.status, r.track) for r in recs] == [
        ("q01", "failed", "B"), ("q02", "failed", "B"), ("join_innodb", "failed", "B")]
    assert "extension would not load" in recs[0].reason and recs[0].error
    assert len(list(iter_records(tmp_path))) == 3


def test_run_track_b_marks_the_join_n_a_for_other_systems(tmp_path):
    from dbench.config import Limits
    from dbench.measures.villagesql import run_track_b

    class Other(Reference):
        name = "pg_duckdb-trackb"
        config_dir = "pgduckdb"

    def factory(name, data_dir):
        return Other([ok(1.0, [(1,)])] * 40) if name == "pg_duckdb-trackb" \
            else Reference([ok(1.0, [(1,)])] * 40)

    class Data:
        root = tmp_path

    recs = run_track_b(1, "r", tmp_path, ["pg_duckdb-trackb"], data=Data,
                       limits=Limits("0-7", "8-11", 16), queries=[1], warm_runs=0,
                       drop_caches=lambda: None, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                       host=lambda: HOST, adapter_factory=factory)
    assert [(r.item, r.status) for r in recs] == [("q01", "ok"), ("join_innodb", "n/a")]
    assert recs[0].extra["compared"] is True and recs[1].reason


def test_run_track_b_auto_fails_a_system_that_raises_and_goes_on(tmp_path):
    from dbench.config import Limits
    from dbench.measures.villagesql import run_track_b

    class Raises(Reference):
        name = "villagesql"
        config_dir = "villagesql"

        def execute(self, sql, timeout_s):
            raise RuntimeError("driver blew up")

    class Other(Reference):
        name = "pg_duckdb-trackb"
        config_dir = "pgduckdb"

    def factory(name, data_dir):
        if name == "villagesql":
            return Raises([])
        return Other([ok(1.0, [(1,)])] * 40) if name == "pg_duckdb-trackb" \
            else Reference([ok(1.0, [(1,)])] * 40)

    class Data:
        root = tmp_path

    recs = run_track_b(1, "r", tmp_path, ["villagesql", "pg_duckdb-trackb"], data=Data,
                       limits=Limits("0-7", "8-11", 16), queries=[1], warm_runs=0,
                       drop_caches=lambda: None, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                       host=lambda: HOST, adapter_factory=factory)
    by = {(r.system, r.item): r for r in recs}
    assert by[("villagesql", "q01")].status == "failed"
    assert by[("villagesql", "q01")].extra["auto"] is True
    assert by[("pg_duckdb-trackb", "q01")].status == "ok"


def test_run_track_b_failed_start_is_auto(tmp_path):
    from dbench.config import Limits
    from dbench.measures.villagesql import run_track_b

    def factory(name, data_dir):
        return StartFails([]) if name == "villagesql" else Reference([ok(1.0, [(1,)])] * 40)

    class Data:
        root = tmp_path

    recs = run_track_b(1, "r", tmp_path, ["villagesql"], data=Data,
                       limits=Limits("0-7", "8-11", 16), queries=[1], warm_runs=0,
                       drop_caches=lambda: None, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                       host=lambda: HOST, adapter_factory=factory)
    assert all(r.extra["auto"] is True for r in recs)


class _Counting(Reference):
    name = "pg_duckdb-trackb"
    config_dir = "pgduckdb"

    def __init__(self, log):
        super().__init__([])
        self.log = log

    def execute(self, sql, timeout_s):
        self.log.append(sql)
        return ok(1.0, [(1,)])


def _trackb(tmp_path, log, clock, monkeypatch):
    from dbench.config import Limits
    from dbench.measures.villagesql import run_track_b
    monkeypatch.setattr("dbench.hostinfo.suspended_s", clock)

    def factory(name, data_dir):
        return _Counting(log) if name == "pg_duckdb-trackb" else Reference([ok(1.0, [(1,)])] * 40)

    class Data:
        root = tmp_path

    return run_track_b(1, "r", tmp_path, ["pg_duckdb-trackb"], data=Data,
                       limits=Limits("0-7", "8-11", 16), queries=[1, 2, 3], warm_runs=0,
                       drop_caches=lambda: None, clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                       host=lambda: HOST, adapter_factory=factory)


def test_track_b_host_suspend_keeps_measured_cells_and_resume_retries_the_rest(tmp_path,
                                                                              monkeypatch):
    from dbench.measures.power import SUSPEND_REASON
    from dbench.results import iter_records
    log: list[str] = []
    # from the 2nd query on, the host "sleeps" 60 s during every run: q02 and q03 abort
    _trackb(tmp_path, log, lambda: 60.0 * max(0, len(log) - 1), monkeypatch)
    q01 = tmp_path / "r" / "pg_duckdb-trackb" / "M1-sf1-q01.json"
    kept = q01.read_bytes()
    by = {r.item: r for r in iter_records(tmp_path) if r.system == "pg_duckdb-trackb"}
    assert by["q01"].status == "ok" and "auto" not in by["q01"].extra
    for item in ("q02", "q03", "join_innodb"):
        assert by[item].status == "failed" and by[item].extra["auto"] is True
        assert by[item].reason.startswith(SUSPEND_REASON)

    log.clear()
    _trackb(tmp_path, log, lambda: 0.0, monkeypatch)
    assert len(log) == 2  # q02 and q03 only
    assert q01.read_bytes() == kept
    by = {r.item: r for r in iter_records(tmp_path) if r.system == "pg_duckdb-trackb"}
    assert [by[i].status for i in ("q01", "q02", "q03", "join_innodb")] == [
        "ok", "ok", "ok", "n/a"]
