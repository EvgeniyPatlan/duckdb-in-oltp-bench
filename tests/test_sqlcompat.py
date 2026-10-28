"""M5 (SQL compatibility probes) and M8 (silent slow paths)."""
from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from dbench.campaign import Campaign
from dbench.config import Limits
from dbench.measures import registry
from dbench.measures.fallback import run_fallback, summarize_fallback
from dbench.measures.sqlcompat import (
    CATEGORIES,
    CLASSES,
    Probe,
    ProbeReferenceStore,
    classify_probe,
    load_probes,
    render_sql,
    run_sqlcompat,
)
from dbench.results import ResultRecord, iter_records, write
from dbench.systems.alisql import AliSQLDuckDB
from dbench.systems.base import QueryOutcome
from dbench.systems.innodb import InnoDB
from dbench.systems.mariadb import MariaDBDuckDB
from dbench.systems.percona import PerconaEngine
from dbench.systems.pgduckdb import PgDuckDB
from dbench.systems.pgducklake import PgDuckLake
from dbench.systems.postgres import Postgres
from tests.fakes import FakeAdapter

HOST = {"cpus": 4, "mem_gb": 8, "kernel": "k", "docker_version": "d"}
CLOCK = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731


def out(rows=None, pushed="duckdb", status="ok", error=None, numeric=()):
    return QueryOutcome(status, 0.01, rows if rows is not None else [(1,)], set(numeric), error,
                        pushed)


# ---- classification ---------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    (dict(status="ok", pushed="duckdb", scope="session", matches=True, write=False), "duckdb"),
    (dict(status="ok", pushed="fallback", scope="session", matches=True, write=False),
     "fallback"),
    (dict(status="error", pushed="n/a", scope="session", matches=None, write=False), "error"),
    (dict(status="timeout", pushed="n/a", scope="explain", matches=None, write=False), "error"),
    (dict(status="ok", pushed="duckdb", scope="session", matches=False, write=False),
     "wrong_result"),
    (dict(status="ok", pushed="unknown", scope="session", matches=True, write=False),
     "unknown"),
    (dict(status="ok", pushed="duckdb", scope="none", matches=True, write=False), "unknown"),
    (dict(status="ok", pushed="duckdb", scope="none", matches=True, write=True),
     "ok_unobserved"),
    (dict(status="ok", pushed="unknown", scope="global", matches=True, write=True),
     "ok_unobserved"),
    (dict(status="ok", pushed="fallback", scope="explain", matches=True, write=True),
     "fallback"),
    (dict(status="ok", pushed="duckdb", scope="global", matches=None, write=False), "duckdb"),
    (dict(status="error", pushed="n/a", scope="none", matches=None, write=True), "error"),
])
def test_classification_matrix(kwargs, expected):
    assert classify_probe(**kwargs) == expected
    assert expected in CLASSES


# ---- the probe file ---------------------------------------------------------------------

def _statements(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [s for v in value.values() for s in _statements(v)]
    if isinstance(value, str):
        return [value]
    return [s for v in value for s in _statements(v)]


def _per_dialect(value, dialect: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return _statements(value.get(dialect))
    return _statements(value)


def test_probe_file_schema_valid():
    probes = load_probes()
    assert 90 <= len(probes) <= 130
    ids = [p.id for p in probes]
    assert len(set(ids)) == len(ids)
    for p in probes:
        assert re.fullmatch(r"[a-z0-9_]+", p.id), p.id
        assert p.category in CATEGORIES, p.id
        assert p.title, p.id
        assert _statements(p.sql["mysql"]) and _statements(p.sql["postgres"]), p.id
        if p.category in ("dml", "ddl"):
            assert p.check["mysql"] and p.check["postgres"], f"{p.id} needs a follow-up check"
    assert {p.category for p in probes} == set(CATEGORIES)


def test_probe_file_setup_teardown_symmetry():
    create = re.compile(r"CREATE\s+(?:TEMPORARY\s+)?TABLE\s+(?:IF NOT EXISTS\s+)?(\w+)", re.I)
    drop = re.compile(r"DROP\s+TABLE\s+IF EXISTS\s+(\w+)", re.I)
    for p in load_probes():
        for dialect in ("mysql", "postgres"):
            made = {m.group(1).lower() for s in (_per_dialect(p.setup, dialect)
                                                 + _statements(p.sql[dialect]))
                    for m in [create.search(s)] if m}
            dropped = {m.group(1).lower() for s in _per_dialect(p.teardown, dialect)
                       for m in [drop.search(s)] if m}
            assert made <= dropped, f"{p.id} [{dialect}] creates {made - dropped} without a DROP"
            for s in _per_dialect(p.setup, dialect):
                assert not re.search(r"CREATE\s+TABLE", s, re.I) or "{{" in s, (
                    f"{p.id}: scratch tables take the engine placeholder")


def test_probe_scratch_tables_use_the_clause_placeholders():
    for p in load_probes():
        for dialect in ("mysql", "postgres"):
            for s in _per_dialect(p.setup, dialect):
                if re.match(r"\s*CREATE\s+TABLE", s, re.I):
                    assert "{{ engine_clause }}" in s or "{{ rowstore_clause }}" in s, (p.id, s)


# ---- template rendering -----------------------------------------------------------------

@pytest.mark.parametrize("cls, engine, rowstore", [
    (AliSQLDuckDB, "ENGINE=DuckDB", "ENGINE=InnoDB"),
    (MariaDBDuckDB, "ENGINE=DuckDB", "ENGINE=InnoDB"),
    (PerconaEngine, "ENGINE=DuckDB", "ENGINE=InnoDB"),
    (InnoDB, "ENGINE=InnoDB", "ENGINE=InnoDB"),
    (PgDuckLake, "USING ducklake", ""),
    (PgDuckDB, "", ""),
    (Postgres, "", ""),
])
def test_template_rendering_per_adapter(cls, engine, rowstore):
    sql = "CREATE TABLE a (x INT) {{ engine_clause }}; CREATE TABLE b (y INT) {{ rowstore_clause }}"
    rendered = render_sql(sql, cls)
    want = f"CREATE TABLE a (x INT) {engine}; CREATE TABLE b (y INT) {rowstore}"
    assert rendered == want.rstrip()


def test_render_rejects_unknown_placeholder():
    with pytest.raises(Exception):  # noqa: B017 - jinja2 UndefinedError
        render_sql("SELECT {{ nope }}", AliSQLDuckDB)


# ---- the reference cache ----------------------------------------------------------------

def test_probe_reference_cache_roundtrip(tmp_path):
    import datetime as dt
    import decimal

    store = ProbeReferenceStore(tmp_path, "r1", sf=1)
    rows = [(1, decimal.Decimal("1.50"), dt.date(2024, 1, 2), None, b"\x01")]
    store.save("mysql", "p1", "innodb", rows, {0, 1}, ["a", "b", "c", "d", "e"])
    got = store.load("mysql", "p1")
    assert got["rows"] == rows and got["numeric_cols"] == {0, 1}
    assert (tmp_path / "r1" / "_probe_reference").is_dir()
    assert store.load("postgres", "p1") is None  # per dialect
    assert ProbeReferenceStore(tmp_path, "r1", sf=10).load("mysql", "p1") is None  # per sf
    assert ProbeReferenceStore(tmp_path, "r2", sf=1).load("mysql", "p1") is None  # per run


def test_reference_dir_is_not_an_m5_record(tmp_path):
    ProbeReferenceStore(tmp_path, "r1", sf=1).save("mysql", "p1", "innodb", [(1,)], {0}, ["a"])
    assert list(iter_records(tmp_path)) == []


# ---- running probes ---------------------------------------------------------------------

def probe(pid="p1", category="functions", **kw) -> Probe:
    return Probe(
        id=pid, category=category, title=kw.pop("title", "t"),
        setup=kw.pop("setup", None), teardown=kw.pop("teardown", None),
        sql=kw.pop("sql", {"mysql": "SELECT 1 FROM t", "postgres": "SELECT 1 FROM t"}),
        check=kw.pop("check", {"mysql": None, "postgres": None}),
        expected=kw.pop("expected", None), note=kw.pop("note", None))


class ProbeAdapter(FakeAdapter):
    """Scripted per-statement outcomes: ``script`` maps a substring of the SQL to an outcome."""

    def __init__(self, name, dialect, scope, script, track="A", engine_clause="ENGINE=DuckDB"):
        super().__init__([])
        self.name, self.dialect, self.pushdown_counter_scope = name, dialect, scope
        self.track = track
        self.engine_clause, self.rowstore_clause = engine_clause, "ENGINE=InnoDB"
        self.script = script
        self.sessions: list[list[str]] = []

    def _pick(self, sql):
        for needle, outcome in self.script.items():
            if needle in sql:
                return outcome
        return out()

    def execute(self, sql, timeout_s):
        self.executed.append(sql)
        return self._pick(sql)

    def execute_session(self, statements, timeout_s):
        self.sessions.append(list(statements))
        return self._pick(statements[-1])


def run(adapter, probes, tmp_path, run_id="r1"):
    return run_sqlcompat(adapter, run_id, tmp_path, sf=1, probes=probes, host=lambda: HOST,
                         clock=CLOCK)


def test_engine_probe_classes(tmp_path):
    ref = ProbeReferenceStore(tmp_path, "r1", sf=1)
    for pid, rows in (("ok", [(1,)]), ("slow", [(1,)]), ("bad", [(1,)]), ("err", [(1,)])):
        ref.save("mysql", pid, "innodb", rows, {0}, ["a"])
    a = ProbeAdapter("alisql", "mysql", "session", {
        "FROM ok": out([(1,)], "duckdb"),
        "FROM slow": out([(1,)], "fallback"),
        "FROM bad": out([(2,)], "duckdb", numeric={0}),
        "FROM err": out(None, "n/a", status="error", error="boom"),
    })
    probes = [probe(pid, sql={"mysql": f"SELECT 1 FROM {pid}", "postgres": "SELECT 1"})
              for pid in ("ok", "slow", "bad", "err")]
    recs = {r.item: r for r in run(a, probes, tmp_path)}
    assert {k: r.extra["class"] for k, r in recs.items()} == {
        "ok": "duckdb", "slow": "fallback", "bad": "wrong_result", "err": "error"}
    assert recs["ok"].status == "ok" and recs["slow"].status == "ok"
    assert recs["bad"].status == "wrong_result" and recs["bad"].extra["diffs"]
    assert recs["err"].status == "error" and recs["err"].extra["error"] == "boom"
    assert all(r.measure == "M5" and r.extra["category"] == "functions" for r in recs.values())


def test_no_counter_scope_is_unknown_unless_error(tmp_path):
    ref = ProbeReferenceStore(tmp_path, "r1", sf=1)
    ref.save("mysql", "p1", "innodb", [(1,)], {0}, ["a"])
    a = ProbeAdapter("x", "mysql", "none", {})
    (rec,) = run(a, [probe("p1")], tmp_path)
    assert rec.extra["class"] == "unknown" and rec.status == "ok"


def test_baselines_recorded_na_and_build_the_reference(tmp_path):
    inno = ProbeAdapter("innodb", "mysql", "none", {"FROM t": out([(7,)], "n/a")},
                        track="baseline", engine_clause="ENGINE=InnoDB")
    (rec,) = run(inno, [probe("p1")], tmp_path)
    assert rec.status == "n/a" and rec.extra["class"] == "n/a"
    assert "reference for the probe suite" in rec.reason
    cached = ProbeReferenceStore(tmp_path, "r1", sf=1).load("mysql", "p1")
    assert cached["rows"] == [(7,)]
    # an engine run then compares against it
    eng = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([(8,)], "duckdb")})
    (r2,) = run(eng, [probe("p1")], tmp_path)
    assert r2.extra["class"] == "wrong_result"


def test_baseline_error_leaves_no_reference_and_engine_is_not_compared(tmp_path):
    inno = ProbeAdapter("innodb", "mysql", "none",
                        {"FROM t": out(None, "n/a", status="error", error="bad probe")},
                        track="baseline")
    (rec,) = run(inno, [probe("p1")], tmp_path)
    assert rec.status == "n/a" and rec.extra["baseline_error"] == "bad probe"
    assert ProbeReferenceStore(tmp_path, "r1", sf=1).load("mysql", "p1") is None
    eng = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([(8,)], "duckdb")})
    (r2,) = run(eng, [probe("p1")], tmp_path)
    assert r2.extra["class"] == "duckdb" and r2.extra["compared"] is False


def test_baselines_disagreement_is_flagged(tmp_path):
    inno = ProbeAdapter("innodb", "mysql", "none", {"FROM t": out([(1,)], "n/a")},
                        track="baseline")
    run(inno, [probe("p1")], tmp_path)
    pg = ProbeAdapter("postgres", "postgres", "none", {"FROM t": out([(2,)], "n/a")},
                      track="baseline", engine_clause="")
    (rec,) = run(pg, [probe("p1")], tmp_path)
    assert rec.extra["baseline_disagreement"] is True and rec.extra["diffs"]


def test_literal_expected_overrides_baseline(tmp_path):
    ProbeReferenceStore(tmp_path, "r1", sf=1).save("mysql", "p1", "innodb", [(9,)], {0}, ["a"])
    a = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([(1,)], "duckdb")})
    (rec,) = run(a, [probe("p1", expected=[[1]])], tmp_path)
    assert rec.extra["class"] == "duckdb"


def test_setup_failure_is_an_error_and_teardown_still_runs(tmp_path):
    a = ProbeAdapter("alisql", "mysql", "session", {
        "CREATE TABLE s": out(None, "n/a", status="error", error="no engine")})
    p = probe("p1", setup=["CREATE TABLE s (x INT) {{ engine_clause }}"],
              teardown=["DROP TABLE IF EXISTS s"])
    (rec,) = run(a, [p], tmp_path)
    assert rec.extra["class"] == "error" and rec.extra["stage"] == "setup"
    assert any("DROP TABLE IF EXISTS s" in s for s in a.executed)
    assert "ENGINE=DuckDB" in " ".join(a.executed)


def test_dml_probe_is_classified_by_its_check(tmp_path):
    ref = ProbeReferenceStore(tmp_path, "r1", sf=1)
    ref.save("mysql", "d1", "innodb", [(2,)], {0}, ["n"])
    a = ProbeAdapter("percona", "mysql", "global", {
        "UPDATE": out([], "n/a"), "SELECT n": out([(2,)], "duckdb")})
    d = probe("d1", "dml", sql={"mysql": "UPDATE s SET x = 1", "postgres": "UPDATE s SET x = 1"},
              check={"mysql": "SELECT n FROM s", "postgres": "SELECT n FROM s"})
    (rec,) = run(a, [d], tmp_path)
    assert rec.extra["class"] == "duckdb" and rec.extra["pushed"] == "duckdb"
    # the check differs from the baseline -> wrong_result
    a2 = ProbeAdapter("percona", "mysql", "global", {"SELECT n": out([(3,)], "duckdb")})
    (rec2,) = run(a2, [d], tmp_path, run_id="r1")
    assert rec2.extra["class"] == "wrong_result"
    # no counter: success plus a matching check is ok_unobserved
    a3 = ProbeAdapter("x", "mysql", "none", {"SELECT n": out([(2,)], "n/a")})
    (rec3,) = run(a3, [d], tmp_path)
    assert rec3.extra["class"] == "ok_unobserved" and rec3.status == "ok"


def test_session_script_runs_on_one_session(tmp_path):
    ProbeReferenceStore(tmp_path, "r1", sf=1).save("mysql", "v1", "innodb", [(5,)], {0}, ["a"])
    a = ProbeAdapter("alisql", "mysql", "session", {"@v": out([(5,)], "duckdb")})
    v = probe("v1", "variables", sql={"mysql": ["SET @v = 5", "SELECT @v FROM t"],
                                      "postgres": ["SET x.v = 5", "SELECT 5 FROM t"]})
    (rec,) = run(a, [v], tmp_path)
    assert a.sessions == [["SET @v = 5", "SELECT @v FROM t"]]
    assert rec.extra["class"] == "duckdb"


def test_crash_restarts_server_and_run_continues(tmp_path):
    a = ProbeAdapter("alisql", "mysql", "session", {
        "FROM boom": QueryOutcome("crashed", None, None, set(), "gone", "n/a")})
    recs = run(a, [probe("p1", sql={"mysql": "SELECT 1 FROM boom", "postgres": "x"}),
                   probe("p2")], tmp_path)
    assert [r.extra["class"] for r in recs] == ["error", "duckdb"]
    assert recs[0].extra["outcome"] == "crashed" and a.restarts == 1


def test_records_are_written(tmp_path):
    a = ProbeAdapter("alisql", "mysql", "session", {})
    run(a, [probe("p1"), probe("p2")], tmp_path)
    assert {r.item for r in iter_records(tmp_path)} == {"p1", "p2"}


# ---- M8 ---------------------------------------------------------------------------------

def rec(measure, item, status="ok", **extra):
    return ResultRecord("alisql", "A", measure, 1, item, status, None, "s", "systems/alisql/",
                        "r1", "t", extra=extra)


def test_fallback_share_math():
    m1 = ([rec("M1", f"q{i}", pushed="duckdb") for i in range(6)]
          + [rec("M1", "q7", pushed="fallback"), rec("M1", "q8", pushed="fallback"),
             rec("M1", "q9", pushed="unknown"), rec("M1", "q10", status="error", pushed="n/a")])
    m5 = ([rec("M5", "a", pushed="duckdb", **{"class": "duckdb"}),
           rec("M5", "b", pushed="fallback", **{"class": "fallback"}),
           rec("M5", "c", pushed="unknown", **{"class": "unknown"}),
           rec("M5", "d", pushed="n/a", **{"class": "ok_unobserved"}),
           rec("M5", "e", status="error", **{"class": "error"}),
           rec("M5", "f", status="wrong_result", pushed="duckdb", **{"class": "wrong_result"})])
    s = summarize_fallback(m1 + m5, "session")
    tpch, probes = s["fallback_share_tpch"], s["fallback_share_probes"]
    assert tpch["status"] == "ok" and tpch["value"] == pytest.approx(2 / 9)
    assert tpch["extra"]["counts"] == {"duckdb": 6, "fallback": 2, "unknown": 1}
    assert tpch["extra"]["unobserved_share"] == pytest.approx(1 / 9)
    assert probes["value"] == pytest.approx(1 / 5)
    assert probes["extra"]["counts"] == {"duckdb": 2, "fallback": 1, "unknown": 2}
    assert probes["extra"]["not_run"] == {"error": 1}
    assert s["visibility"]["value"] == 1
    assert s["visibility"]["extra"]["mechanism"] == "status_counter"


@pytest.mark.parametrize("scope, mech, value, note", [
    ("session", "status_counter", 1, False), ("global", "status_counter", 1, True),
    ("explain", "explain", 1, False), ("none", "none", 0, False)])
def test_visibility_by_counter_scope(scope, mech, value, note):
    v = summarize_fallback([], scope)["visibility"]
    assert v["value"] == value and v["extra"]["mechanism"] == mech
    assert ("ambiguous under concurrency" in v["extra"].get("note", "")) is note


def test_no_pushdown_info_gives_na_share():
    s = summarize_fallback([rec("M1", "q1", pushed="n/a")], "none")
    assert s["fallback_share_tpch"]["status"] == "n/a" and s["fallback_share_tpch"]["reason"]
    assert s["fallback_share_probes"]["status"] == "n/a"


def test_run_fallback_writes_three_records_from_the_systems_own_records(tmp_path):
    a = ProbeAdapter("alisql", "mysql", "global", {})
    for r in [rec("M1", "q01", pushed="duckdb"), rec("M1", "q02", pushed="fallback"),
              rec("M5", "p1", pushed="duckdb", **{"class": "duckdb"})]:
        write(r, tmp_path)
    other = ResultRecord("mariadb", "A", "M1", 1, "q01", "ok", None, "s", "c", "r1", "t",
                         extra={"pushed": "fallback"})
    write(other, tmp_path)
    ctx = registry.MeasureContext(
        sf=1, run_id="r1", results=tmp_path, data=None, reference=None,
        items=["fallback_share_tpch", "fallback_share_probes", "visibility"], host=lambda: HOST)
    recs = run_fallback(a, ctx)
    assert [r.item for r in recs] == ["fallback_share_tpch", "fallback_share_probes",
                                      "visibility"]
    assert all(r.measure == "M8" for r in recs)
    assert recs[0].value == pytest.approx(0.5) and recs[1].value == 0.0
    assert recs[2].value == 1 and "ambiguous under concurrency" in recs[2].extra["note"]


# ---- registration -----------------------------------------------------------------------

def test_m5_and_m8_registered_after_m1_and_m4():
    assert {"M5", "M8"} <= set(registry.registered())
    assert registry.get("M5").phase == registry.get("M8").phase == "query"
    c = Campaign("r", 1, ("alisql",), ("M1", "M5", "M8"), Limits())
    assert c.items("M8") == ["fallback_share_tpch", "fallback_share_probes", "visibility"]
    assert set(c.items("M5")) == {p.id for p in load_probes()}


def test_campaign_files_include_m5_m8():
    from pathlib import Path

    import yaml

    for name in ("smoke-sf1", "gate-sf10"):
        doc = yaml.safe_load((Path(__file__).parents[1] / "campaigns" / f"{name}.yaml").read_text())
        assert {"M5", "M8"} <= set(doc["measures"])


def test_baselines_and_plain_duckdb_support_flags():
    assert Postgres.supports(Postgres("x"), "M5") == (True, None)
    ok_, reason = Postgres.supports(Postgres("x"), "M8")
    assert not ok_ and "row-store baseline" in reason
    from dbench.systems.duckdb_native import DuckDBNative
    ok_, reason = DuckDBNative("1.5.5").supports("M5")
    assert not ok_ and reason


# ---- EXPLAIN inside a transaction ------------------------------------------------------

class _Info:
    def __init__(self, status):
        self.transaction_status = status


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Conn:
    """Records statements; EXPLAIN raises when ``explain_fails``."""

    def __init__(self, status, explain_fails=False, plan="Custom Scan (DuckDBScan)"):
        self.info = _Info(status)
        self.sent: list[str] = []
        self._fails, self._plan = explain_fails, plan

    def execute(self, sql):
        self.sent.append(sql)
        if sql.startswith("EXPLAIN") and self._fails:
            raise RuntimeError("EXPLAIN not supported")
        return _Cursor([(self._plan,)])


def test_explain_is_not_sent_inside_a_transaction():
    from psycopg import pq

    from dbench.systems.postgres_family import PostgresFamilyAdapter

    conn = _Conn(pq.TransactionStatus.INTRANS)
    assert PostgresFamilyAdapter._explain_pushed(PgDuckDB("x"), conn, "COMMIT") == "unknown"
    assert conn.sent == []  # no EXPLAIN and no SAVEPOINT: either could abort or be refused


def test_explain_outside_a_transaction_is_plain():
    from psycopg import pq

    from dbench.systems.postgres_family import PostgresFamilyAdapter

    conn = _Conn(pq.TransactionStatus.IDLE, explain_fails=True)
    assert PostgresFamilyAdapter._explain_pushed(PgDuckDB("x"), conn, "SELECT 1") == "unknown"
    assert conn.sent == ["EXPLAIN SELECT 1"]


def test_probe_scratch_tables_have_primary_keys_except_the_dedicated_probe():
    create = re.compile(r"CREATE\s+TABLE\s+\w+\s*\(", re.I)
    for p in load_probes():
        for dialect in ("mysql", "postgres"):
            for s in _per_dialect(p.setup, dialect):
                if create.match(s.strip()):
                    assert "PRIMARY KEY" in s.upper(), f"{p.id} [{dialect}]: {s}"
    dedicated = next(p for p in load_probes() if p.id == "ddl_create_table_without_pk")
    assert not _per_dialect(dedicated.setup, "mysql")
    assert "PRIMARY KEY" not in " ".join(_per_dialect(dedicated.sql["mysql"], "mysql")).upper()
