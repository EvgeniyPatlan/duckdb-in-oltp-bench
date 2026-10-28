"""M5 (SQL compatibility probes) and M8 (silent slow paths)."""
from __future__ import annotations

import json
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
    (dict(status="ok", pushed="duckdb", scope="session", matches=True, write=True),
     "ok_unobserved"),
    (dict(status="ok", pushed="duckdb", scope="session", matches=False, write=True),
     "wrong_result"),
    (dict(status="ok", pushed="unknown", scope="global", matches=True, write=True),
     "ok_unobserved"),
    (dict(status="ok", pushed="fallback", scope="explain", matches=True, write=True),
     "ok_unobserved"),
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

SETUP_T = ["CREATE TABLE t (x INT) {{ engine_clause }}"]  # the engine table probes read


def probe(pid="p1", category="functions", **kw) -> Probe:
    return Probe(
        id=pid, category=category, title=kw.pop("title", "t"),
        setup=kw.pop("setup", SETUP_T), teardown=kw.pop("teardown", None),
        sql=kw.pop("sql", {"mysql": "SELECT 1 FROM t", "postgres": "SELECT 1 FROM t"}),
        check=kw.pop("check", {"mysql": None, "postgres": None}),
        expected=kw.pop("expected", None), note=kw.pop("note", None),
        compare=kw.pop("compare", None))


class ProbeAdapter(FakeAdapter):
    """Scripted per-statement outcomes: ``script`` maps a substring of the SQL to an outcome.

    The script describes the integration: it answers while the probe's first table is an engine
    table. When the first table is a row-store table (the M5 server control, where every table
    is one), ``control`` answers instead; an empty ``control`` means the host server runs
    everything."""

    def __init__(self, name, dialect, scope, script, track="A", engine_clause="ENGINE=DuckDB",
                 control=None):
        super().__init__([])
        self.name, self.dialect, self.pushdown_counter_scope = name, dialect, scope
        self.track = track
        self.engine_clause, self.rowstore_clause = engine_clause, "ENGINE=InnoDB"
        self.script, self.control = script, control or {}
        self.sessions: list[list[str]] = []
        self.creates: list[str] = []
        self.mode = "engine"
        self.last = ""  # the previous statement

    def _track_tables(self, sql):
        """The first CREATE of a setup (one not following another setup statement) sets the
        mode: an engine table means the integration, a row-store table the server control."""
        first = not self.last.startswith(("CREATE", "INSERT"))
        self.last = sql
        if not sql.startswith("CREATE"):
            return
        self.creates.append(sql)
        if first and self.engine_clause != self.rowstore_clause:
            self.mode = "engine" if self.engine_clause in sql else "control"

    def _pick(self, sql):
        self._track_tables(sql)
        script = self.script if self.mode == "engine" else self.control
        for needle, outcome in script.items():
            if needle in sql:
                return outcome
        return out()

    def execute(self, sql, timeout_s):
        self.executed.append(sql)
        return self._pick(sql)

    def execute_session(self, statements, timeout_s):
        self.sessions.append(list(statements))
        return self._pick(statements[-1])


def save_ref(root, p, rows, numeric=(0,), run_id="r1"):
    """Cache ``rows`` as the baseline result of probe ``p`` (keyed to its current wording)."""
    ProbeReferenceStore(root, run_id, sf=1).save(
        "mysql", p.id, "innodb", rows, set(numeric), ["a"], p.content_key("mysql"))


def run(adapter, probes, tmp_path, run_id="r1"):
    return run_sqlcompat(adapter, run_id, tmp_path, sf=1, probes=probes, host=lambda: HOST,
                         clock=CLOCK)


def test_engine_probe_classes(tmp_path):
    probes = [probe(pid, sql={"mysql": f"SELECT 1 FROM {pid}", "postgres": "SELECT 1"})
              for pid in ("ok", "slow", "bad", "err")]
    for p in probes:
        save_ref(tmp_path, p, [(1,)])
    a = ProbeAdapter("alisql", "mysql", "session", {
        "FROM ok": out([(1,)], "duckdb"),
        "FROM slow": out([(1,)], "fallback"),
        "FROM bad": out([(2,)], "duckdb", numeric={0}),
        "FROM err": out(None, "n/a", status="error", error="boom"),
    })
    recs = {r.item: r for r in run(a, probes, tmp_path)}
    assert {k: r.extra["class"] for k, r in recs.items()} == {
        "ok": "duckdb", "slow": "fallback", "bad": "wrong_result", "err": "error"}
    assert recs["ok"].status == "ok" and recs["slow"].status == "ok"
    assert recs["bad"].status == "wrong_result" and recs["bad"].extra["diffs"]
    assert recs["err"].status == "error" and recs["err"].extra["error"] == "boom"
    assert all(r.measure == "M5" and r.extra["category"] == "functions" for r in recs.values())


def test_no_counter_scope_is_unknown_unless_error(tmp_path):
    save_ref(tmp_path, probe("p1"), [(1,)])
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
    p = probe("p1", expected=[[1]])
    save_ref(tmp_path, p, [(9,)])
    a = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([(1,)], "duckdb")})
    (rec,) = run(a, [p], tmp_path)
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


def test_dml_probe_is_ok_unobserved_and_keeps_the_check_pushdown(tmp_path):
    ref = ProbeReferenceStore(tmp_path, "r1", sf=1)
    d = probe("d1", "dml", sql={"mysql": "UPDATE s SET x = 1", "postgres": "UPDATE s SET x = 1"},
              check={"mysql": "SELECT n FROM s", "postgres": "SELECT n FROM s"})
    ref.save("mysql", "d1", "innodb", [(2,)], {0}, ["n"], d.content_key("mysql"))
    a = ProbeAdapter("percona", "mysql", "global", {
        "UPDATE": out([], "n/a"), "SELECT n": out([(2,)], "duckdb")})
    (rec,) = run(a, [d], tmp_path)
    assert rec.extra["class"] == "ok_unobserved" and rec.status == "ok"
    assert rec.extra["check_pushed"] == "duckdb" and "pushed" not in rec.extra
    # the check differs from the baseline -> wrong_result
    a2 = ProbeAdapter("percona", "mysql", "global", {"SELECT n": out([(3,)], "duckdb")})
    (rec2,) = run(a2, [d], tmp_path)
    assert rec2.extra["class"] == "wrong_result" and rec2.status == "wrong_result"
    # the statement fails -> error
    a3 = ProbeAdapter("x", "mysql", "none", {
        "UPDATE": out(None, "n/a", status="error", error="no option")})
    (rec3,) = run(a3, [d], tmp_path)
    assert rec3.extra["class"] == "error" and rec3.extra["stage"] == "statement"


def test_session_script_runs_on_one_session(tmp_path):
    v = probe("v1", "variables", sql={"mysql": ["SET @v = 5", "SELECT @v FROM t"],
                                      "postgres": ["SET x.v = 5", "SELECT 5 FROM t"]})
    save_ref(tmp_path, v, [(5,)])
    a = ProbeAdapter("alisql", "mysql", "session", {"@v": out([(5,)], "duckdb")})
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


def test_fallback_share_excludes_write_probes_and_counts_the_same_statuses():
    m1 = [rec("M1", "q1", pushed="duckdb"),
          rec("M1", "q2", status="wrong_result", pushed="fallback")]
    m5 = [rec("M5", "r", pushed="fallback", category="functions", **{"class": "fallback"}),
          rec("M5", "w", check_pushed="fallback", category="dml", **{"class": "ok_unobserved"}),
          rec("M5", "e", status="error", category="ddl", **{"class": "error"})]
    s = summarize_fallback(m1 + m5, "session")
    assert s["fallback_share_tpch"]["value"] == pytest.approx(0.5)  # wrong_result counts too
    probes = s["fallback_share_probes"]
    assert probes["value"] == 1.0 and probes["extra"]["total"] == 1
    assert "not_run" not in probes["extra"]


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
    ok_conn = _Conn(pq.TransactionStatus.IDLE)
    assert PostgresFamilyAdapter._explain_pushed(PgDuckDB("x"), ok_conn, "SELECT 1") == "duckdb"
    heap = _Conn(pq.TransactionStatus.IDLE, plan="Seq Scan on t")
    assert PostgresFamilyAdapter._explain_pushed(PgDuckDB("x"), heap, "SELECT 1") == "fallback"
    assert ok_conn.sent == ["EXPLAIN SELECT 1"]


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


# ---- json comparison, content key, teardown, mixed-engine rule ----------------------------

def test_json_compare_ignores_whitespace_and_key_order(tmp_path):
    p = probe("j1", "functions", compare="json")
    save_ref(tmp_path, p, [(1, '{"k": 1, "a": [1, 2]}')], numeric=(0,))
    a = ProbeAdapter("alisql", "mysql", "session",
                     {"FROM t": out([(1, '{"a":[1,2],"k":1}')], "duckdb", numeric={0})})
    (rec,) = run(a, [p], tmp_path)
    assert rec.extra["class"] == "duckdb"
    b = ProbeAdapter("alisql", "mysql", "session",
                     {"FROM t": out([(1, '{"a":[1,3],"k":1}')], "duckdb", numeric={0})})
    (rec2,) = run(b, [p], tmp_path)
    assert rec2.extra["class"] == "wrong_result"  # a real difference still shows


def test_json_compare_is_off_by_default(tmp_path):
    p = probe("j2", "functions")
    save_ref(tmp_path, p, [('{"k": 1}',)], numeric=())
    a = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([('{"k":1}',)], "duckdb")})
    (rec,) = run(a, [p], tmp_path)
    assert rec.extra["class"] == "wrong_result"


def test_json_compare_accepts_parsed_documents():
    from dbench.measures.sqlcompat import _canonical_json

    assert _canonical_json({"b": 1, "a": [1]}) == _canonical_json('{ "a": [1], "b": 1 }')
    assert _canonical_json("plain") == "plain" and _canonical_json(5) == 5


def test_reference_with_another_content_key_is_rebuilt_not_used(tmp_path):
    p = probe("p1")
    store = ProbeReferenceStore(tmp_path, "r1", sf=1)
    store.save("mysql", "p1", "innodb", [(1,)], {0}, ["a"], "old-wording")
    assert store.load("mysql", "p1", p.content_key("mysql")) is None
    assert store.load("mysql", "p1", "old-wording") is not None
    # an engine run finds no usable reference and does not compare
    a = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([(5,)], "duckdb")})
    (rec,) = run(a, [p], tmp_path)
    assert rec.extra["compared"] is False
    # a baseline run rewrites it under the current key
    inno = ProbeAdapter("innodb", "mysql", "none", {"FROM t": out([(7,)], "n/a")},
                        track="baseline")
    run(inno, [p], tmp_path)
    assert store.load("mysql", "p1", p.content_key("mysql"))["rows"] == [(7,)]


def test_content_key_changes_with_any_part_of_the_probe():
    base = probe("p1")
    keys = {base.content_key("mysql")}
    for variant in (probe("p1", setup=["CREATE TABLE x (a INT) {{ engine_clause }}"]),
                    probe("p1", sql={"mysql": "SELECT 2 FROM t", "postgres": "SELECT 1 FROM t"}),
                    probe("p1", check={"mysql": "SELECT 1", "postgres": None}),
                    probe("p1", compare="json")):
        keys.add(variant.content_key("mysql"))
    assert len(keys) == 5
    assert base.content_key("mysql") == probe("p1").content_key("mysql")


def test_teardown_continues_past_a_failing_drop(tmp_path):
    a = ProbeAdapter("alisql", "mysql", "session",
                     {"DROP TABLE IF EXISTS first": out(None, "n/a", status="error", error="x")})
    p = probe("p1", teardown=["DROP TABLE IF EXISTS first", "DROP TABLE IF EXISTS second"])
    run(a, [p], tmp_path)
    drops = [s for s in a.executed if s.startswith("DROP")]
    assert drops.count("DROP TABLE IF EXISTS second") == 2  # before setup and after the probe


def test_mixed_engine_probe_is_na_when_clauses_are_the_same(tmp_path):
    mixed = probe("m1", "joins", setup=["CREATE TABLE a (x INT) {{ engine_clause }}",
                                        "CREATE TABLE b (x INT) {{ rowstore_clause }}"])
    plain = probe("p2", setup=["CREATE TABLE a (x INT) {{ engine_clause }}"])
    heap = ProbeAdapter("pg_duckdb", "postgres", "explain", {}, engine_clause="")
    heap.rowstore_clause = ""
    recs = {r.item: r for r in run(heap, [mixed, plain], tmp_path)}
    assert recs["m1"].status == "n/a" and recs["m1"].extra["class"] == "n/a"
    assert "no separate engine table to mix with" in recs["m1"].reason
    assert recs["p2"].extra["class"] != "n/a"
    assert not any("CREATE TABLE b" in s for s in heap.executed)
    # an engine with a distinct row-store clause runs it; so does the baseline
    eng = ProbeAdapter("alisql", "mysql", "session", {})
    assert run(eng, [mixed], tmp_path / "e")[0].extra["class"] != "n/a"
    inno = ProbeAdapter("innodb", "mysql", "none", {}, track="baseline", engine_clause="")
    inno.rowstore_clause = ""
    assert run(inno, [mixed], tmp_path / "b")[0].extra["outcome"] == "ok"


def test_probe_file_flags_json_probe_and_keeps_keys_stable():
    probes = {p.id: p for p in load_probes()}
    assert probes["fn_json_build"].compare == "json"
    assert all(p.compare in (None, "json") for p in probes.values())
    assert probes["ddl_truncate"].main("mysql")[-1].startswith("INSERT")


# ---- reference keyed to the baseline rendering; resume rebuilds ---------------------------

def test_changed_baseline_clause_invalidates_the_reference(tmp_path, monkeypatch):
    p = probe("p1", setup=["CREATE TABLE a (x INT) {{ engine_clause }}"])
    before = p.content_key("mysql")
    store = ProbeReferenceStore(tmp_path, "r1", sf=1)
    store.save("mysql", "p1", "innodb", [(1,)], {0}, ["a"], before)
    assert store.state("mysql", "p1", before) == "ok"
    monkeypatch.setattr(InnoDB, "engine_clause", "ENGINE=MyISAM")
    after = p.content_key("mysql")
    assert after != before
    assert store.state("mysql", "p1", after) == "stale"
    assert ProbeReferenceStore(tmp_path, "other", sf=1).state("mysql", "p1", after) == "absent"
    # an engine clause of the adapter under test does not enter the key: only the baseline's does
    assert probe("p1", setup=["CREATE TABLE a (x INT) {{ engine_clause }}"]).content_key(
        "postgres") != after


def test_engine_record_without_a_usable_reference_says_why(tmp_path):
    p = probe("p1")
    store = ProbeReferenceStore(tmp_path, "r1", sf=1)
    a = ProbeAdapter("alisql", "mysql", "session", {})
    (absent,) = run(a, [p], tmp_path)
    assert absent.extra["reference"] == "absent" and absent.extra["compared"] is False
    store.save("mysql", "p1", "innodb", [(1,)], {0}, ["a"], "old-wording")
    (stale,) = run(a, [p], tmp_path)
    assert stale.extra["reference"] == "stale" and stale.extra["compared"] is False
    store.save("mysql", "p1", "innodb", [(1,)], {0}, ["a"], p.content_key("mysql"))
    (fresh,) = run(a, [p], tmp_path)
    assert fresh.extra["reference"] == "ok" and fresh.extra["compared"] is True
    assert fresh.extra["reference_key"] == p.content_key("mysql")


def test_stale_items_after_a_probe_edit(tmp_path):
    from dbench.measures.sqlcompat import stale_items

    old = probe("p1")
    inno = ProbeAdapter("innodb", "mysql", "none", {"FROM t": out([(7,)], "n/a")},
                        track="baseline")
    eng = ProbeAdapter("alisql", "mysql", "session", {"FROM t": out([(7,)], "duckdb")})
    run(inno, [old], tmp_path)
    run(eng, [old], tmp_path)
    assert stale_items(inno, tmp_path, "r1", 1, [old]) == set()
    assert stale_items(eng, tmp_path, "r1", 1, [old]) == set()
    edited = probe("p1", sql={"mysql": "SELECT 2 FROM t", "postgres": "SELECT 1 FROM t"})
    assert stale_items(inno, tmp_path, "r1", 1, [edited]) == {"p1"}
    # the engine cell is only stale once the baseline has rebuilt the reference
    assert stale_items(eng, tmp_path, "r1", 1, [edited]) == set()
    run(inno, [edited], tmp_path)
    assert stale_items(inno, tmp_path, "r1", 1, [edited]) == set()
    assert stale_items(eng, tmp_path, "r1", 1, [edited]) == {"p1"}
    (again,) = run(eng, [edited], tmp_path)
    assert again.extra["compared"] is True and again.extra["reference"] == "ok"
    assert stale_items(eng, tmp_path, "r1", 1, [edited]) == set()


def test_baseline_cell_with_a_lost_cache_is_stale(tmp_path):
    from dbench.measures.sqlcompat import stale_items

    p = probe("p1")
    inno = ProbeAdapter("innodb", "mysql", "none", {}, track="baseline")
    run(inno, [p], tmp_path)
    assert stale_items(inno, tmp_path, "r1", 1, [p]) == set()
    for f in (tmp_path / "r1" / "_probe_reference").glob("*.json"):
        f.unlink()
    assert stale_items(inno, tmp_path, "r1", 1, [p]) == {"p1"}


def test_failed_baseline_probe_is_not_stale_forever(tmp_path):
    from dbench.measures.sqlcompat import stale_items

    p = probe("p1")
    inno = ProbeAdapter("innodb", "mysql", "none",
                        {"FROM t": out(None, "n/a", status="error", error="bad")},
                        track="baseline")
    run(inno, [p], tmp_path)
    assert stale_items(inno, tmp_path, "r1", 1, [p]) == set()


class _CampaignProbeAdapter(ProbeAdapter):
    def remove_data(self):
        pass


def test_campaign_resume_rebuilds_the_reference_after_a_probe_edit(tmp_path, monkeypatch):
    from dbench.campaign import run_campaign
    from dbench.measures import sqlcompat
    from tests.test_campaign import Env, write_yaml

    yaml_copy = tmp_path / "probes.yaml"
    text = sqlcompat.PROBES_FILE.read_text(encoding="utf-8")
    yaml_copy.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sqlcompat, "PROBES_FILE", yaml_copy)

    env = Env(tmp_path)

    def factory(name, data_dir):
        if name == "innodb":
            return _CampaignProbeAdapter("innodb", "mysql", "none", {}, track="baseline",
                                         engine_clause="ENGINE=InnoDB")
        return _CampaignProbeAdapter(name, "mysql", "session", {})

    deps = env.deps()
    deps.adapter_factory = factory
    res = tmp_path / "res"
    path = write_yaml(tmp_path, sf=100, systems=["innodb", "alisql"], measures=["M5", "M8"],
                      reference={"system": "duckdb-1.5.5"})
    run_campaign(path, res, deps)
    cell = res / "r1" / "alisql" / "M5-sf100-fn_upper_lower.json"
    other = res / "r1" / "alisql" / "M5-sf100-fn_substring.json"
    kept = other.read_text()
    assert json.loads(cell.read_text())["extra"]["reference"] == "ok"

    # nothing edited: a resume writes nothing
    again = run_campaign(path, res, deps)
    assert again.written == 0

    yaml_copy.write_text(text.replace("SELECT id, UPPER(s), LOWER(s) FROM pc_fn_upper_lower",
                                      "SELECT id, UPPER(s) FROM pc_fn_upper_lower"),
                         encoding="utf-8")
    summary = run_campaign(path, res, deps)
    assert summary.written > 0
    edited_key = next(p for p in sqlcompat.load_probes()
                      if p.id == "fn_upper_lower").content_key("mysql")
    base = json.loads((res / "r1" / "innodb" / "M5-sf100-fn_upper_lower.json").read_text())
    eng = json.loads(cell.read_text())
    assert base["extra"]["reference_key"] == edited_key
    assert eng["extra"]["reference"] == "ok" and eng["extra"]["compared"] is True
    assert eng["extra"]["reference_key"] == edited_key
    assert other.read_text() == kept  # untouched probes are not redone


def test_session_prepare_query_canceled_is_a_timeout_not_a_crash():
    from psycopg import errors as pgerrors

    class Info:
        backend_pid = 42

    class Conn:
        info = Info()

        def execute(self, sql):
            if sql == "BEGIN":
                raise pgerrors.QueryCanceled("canceling statement due to statement timeout")
            return _Cursor([])

    a = PgDuckDB("x")
    a._session = lambda: Conn()
    a._drop_session = lambda: None
    outcome = a.execute_session(["BEGIN", "SELECT 1"], 5)
    assert outcome.status == "timeout" and outcome.stopped is True


def test_join_mixed_tpch_dimension_is_na_on_pg_duckdb(tmp_path):
    from dbench.measures.sqlcompat import _nothing_to_mix

    p = next(p for p in load_probes() if p.id == "join_mixed_tpch_dimension")
    assert p.mixes is True and p.mixes_engines("mysql")
    assert _nothing_to_mix(PgDuckDB("x"), p)
    assert not _nothing_to_mix(PgDuckLake("x"), p)
    assert not _nothing_to_mix(AliSQLDuckDB("x"), p)
    heap = ProbeAdapter("pg_duckdb", "postgres", "explain", {}, engine_clause="")
    heap.rowstore_clause = ""
    (rec,) = run(heap, [p], tmp_path)
    assert rec.status == "n/a" and "no separate engine table to mix with" in rec.reason


def test_content_key_survives_non_json_expected():
    import datetime as dt

    p = probe("p1", expected=[[dt.date(2024, 1, 1), 1]])
    assert p.content_key("mysql") == probe("p1", expected=[[dt.date(2024, 1, 1), 1]]).content_key(
        "mysql")
