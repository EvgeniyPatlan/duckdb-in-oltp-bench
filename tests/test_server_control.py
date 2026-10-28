"""M5 server control: a failing probe is re-run on row-store tables of the same server, so a
failure of the host server's own dialect is not charged to its DuckDB integration."""
from __future__ import annotations

import json

from dbench.measures.sqlcompat import load_probes, stale_items
from dbench.results import cell_record, read
from dbench.systems.base import QueryOutcome
from tests.test_sqlcompat import ProbeAdapter, out, probe, run, save_ref

SETUP = ["CREATE TABLE pc_a (id INT PRIMARY KEY) {{ engine_clause }}"]
TEARDOWN = ["DROP TABLE IF EXISTS pc_a"]
SQL = {"mysql": "SELECT id FROM pc_a", "postgres": "SELECT id FROM pc_a"}


def p_(pid="p1", **kw):
    kw.setdefault("setup", SETUP)
    kw.setdefault("teardown", TEARDOWN)
    kw.setdefault("sql", SQL)
    return probe(pid, **kw)


class ModeAdapter(ProbeAdapter):
    """``engine`` answers for the integration, ``control`` for the row-store server control."""

    def __init__(self, name="mariadb", engine=None, control=None, dialect="mysql",
                 engine_clause="ENGINE=DuckDB", rowstore_clause="ENGINE=InnoDB"):
        super().__init__(name, dialect, "session", engine or {}, engine_clause=engine_clause,
                         control=control)
        self.rowstore_clause = rowstore_clause


def err(msg):
    return out(None, "n/a", status="error", error=msg)


# ---- error -----------------------------------------------------------------------------

def test_error_that_the_server_itself_has_is_na(tmp_path):
    msg = "(1305, 'FUNCTION tpch.REGEXP_LIKE does not exist')"
    a = ModeAdapter(engine={"FROM pc_a": err(msg)}, control={"FROM pc_a": err(msg)})
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "n/a" and rec.extra["class"] == "n/a"
    assert rec.reason.startswith("not supported by the mariadb server itself "
                                 "(same failure on a row-store table)")
    assert msg in rec.reason
    assert rec.extra["integration_error"] == msg and rec.extra["integration_class"] == "error"
    assert rec.extra["stage"] == "statement"
    ctl = rec.extra["server_control"]
    assert ctl["outcome"] == "error" and ctl["error"] == msg and ctl["stage"] == "statement"
    # the control made its table as a row-store table, and both runs were torn down
    assert a.creates == ["CREATE TABLE pc_a (id INT PRIMARY KEY) ENGINE=DuckDB",
                         "CREATE TABLE pc_a (id INT PRIMARY KEY) ENGINE=InnoDB"]
    drops = [i for i, s in enumerate(a.executed) if s == "DROP TABLE IF EXISTS pc_a"]
    assert len(drops) == 4 and drops[-1] == len(a.executed) - 1
    assert cell_record(tmp_path, "mariadb", "M5", 1, "p1", "r1").status == "n/a"


def test_engine_failure_with_a_passing_control_stays_an_error(tmp_path):
    a = ModeAdapter(engine={"CREATE TABLE pc_a": err("This table type requires a primary key")})
    save_ref(tmp_path, p_(), [(1,)])
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error" and rec.extra["class"] == "error"
    assert rec.extra["stage"] == "setup"
    assert rec.extra["server_control"] == "ok"
    assert rec.error == "This table type requires a primary key"


def test_error_with_a_control_that_runs_but_differs_stays_an_error(tmp_path):
    p = p_()
    save_ref(tmp_path, p, [(1,)])
    a = ModeAdapter(engine={"FROM pc_a": err("boom")}, control={"FROM pc_a": out([(2,)])})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "error"
    assert rec.extra["server_control"]["outcome"] == "ok"
    assert rec.extra["server_control"]["matches"] is False


def test_ddl_probe_failing_on_the_engine_only_stays_an_error(tmp_path):
    d = p_("d1", category="ddl", sql={"mysql": "ALTER TABLE pc_a ADD COLUMN v INT",
                                      "postgres": "ALTER TABLE pc_a ADD COLUMN v INT"},
           check={"mysql": "SELECT id FROM pc_a", "postgres": "SELECT id FROM pc_a"})
    save_ref(tmp_path, d, [(1,)])
    a = ModeAdapter("percona", engine={"ALTER": err("not supported by the engine")})
    (rec,) = run(a, [d], tmp_path)
    assert rec.status == "error" and rec.extra["server_control"] == "ok"


# ---- wrong_result ----------------------------------------------------------------------

def test_wrong_result_reproduced_by_the_server_is_na(tmp_path):
    p = p_()
    save_ref(tmp_path, p, [(1,)])
    a = ModeAdapter(engine={"FROM pc_a": out([(2,)])}, control={"FROM pc_a": out([(2,)])})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "n/a" and rec.extra["class"] == "n/a"
    assert "not supported by the mariadb server itself" in rec.reason
    assert "differs from the reference" in rec.reason
    assert rec.extra["integration_class"] == "wrong_result" and rec.extra["diffs"]
    assert rec.extra["server_control"]["outcome"] == "ok"
    assert rec.extra["server_control"]["matches"] is False
    assert rec.extra["server_control"]["same_as_integration"] is True


def test_wrong_result_the_server_gets_right_stays_wrong(tmp_path):
    p = p_()
    save_ref(tmp_path, p, [(1,)])
    a = ModeAdapter(engine={"FROM pc_a": out([("x",)])}, control={"FROM pc_a": out([(1,)])})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "wrong_result" and rec.extra["server_control"] == "ok"


def test_wrong_result_that_also_differs_from_the_servers_own_result_stays_wrong(tmp_path):
    """The server differs from the reference, but the integration differs from the server too:
    that second difference is the integration's."""
    p = p_()
    save_ref(tmp_path, p, [(1,)])
    a = ModeAdapter(engine={"FROM pc_a": out([(3,)])}, control={"FROM pc_a": out([(2,)])})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "wrong_result"
    ctl = rec.extra["server_control"]
    assert ctl["matches"] is False and ctl["same_as_integration"] is False


def test_wrong_result_with_a_failing_control_is_na(tmp_path):
    p = p_()
    save_ref(tmp_path, p, [(1,)])
    a = ModeAdapter(engine={"FROM pc_a": out([(2,)])}, control={"FROM pc_a": err("syntax")})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "n/a" and "syntax" in rec.reason


def test_literal_expected_is_the_controls_reference_too(tmp_path):
    p = p_(expected=[[1]])
    a = ModeAdapter(engine={"FROM pc_a": out([(2,)])}, control={"FROM pc_a": out([(1,)])})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "wrong_result" and rec.extra["server_control"] == "ok"
    b = ModeAdapter(engine={"FROM pc_a": out([(2,)])}, control={"FROM pc_a": out([(2,)])})
    (rec2,) = run(b, [p], tmp_path / "b")
    assert rec2.status == "n/a"


# ---- when there is no control ------------------------------------------------------------

def test_passing_probe_runs_no_control(tmp_path):
    a = ModeAdapter()
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "ok" and "server_control" not in rec.extra
    assert len(a.creates) == 1


def test_identical_clauses_skip_the_control(tmp_path):
    a = ModeAdapter("pg_duckdb", dialect="postgres", engine_clause="", rowstore_clause="",
                    engine={"FROM pc_a": err("nope")})
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error"
    assert "same" in rec.extra["server_control"]["skipped"]
    assert len(a.creates) == 1


def test_baseline_runs_no_control(tmp_path):
    inno = ProbeAdapter("innodb", "mysql", "none", {"FROM pc_a": err("bad probe")},
                        track="baseline", engine_clause="ENGINE=InnoDB")
    (rec,) = run(inno, [p_()], tmp_path)
    assert rec.status == "n/a" and "server_control" not in rec.extra


def test_probe_reading_tpch_engine_tables_skips_the_control(tmp_path):
    p = probe("p1", sql={"mysql": "SELECT n_name FROM nation WHERE REGEXP_LIKE(n_name, 'x')",
                         "postgres": "SELECT 1"})
    a = ModeAdapter(engine={"nation": err("no REGEXP_LIKE")})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "error"
    assert "TPC-H" in rec.extra["server_control"]["skipped"]
    assert a.executed.count("SELECT n_name FROM nation WHERE REGEXP_LIKE(n_name, 'x')") == 1


def test_tpch_detection_matches_only_whole_table_names():
    from dbench.measures.sqlcompat import _reads_tpch

    by_id = {q.id: q for q in load_probes()}
    assert _reads_tpch(by_id["join_tpch_nation_region"], "mysql")
    assert _reads_tpch(by_id["join_mixed_tpch_dimension"], "mysql")
    for pid in ("fn_regexp", "sub_lateral", "win_lag_lead", "fn_concat"):
        assert not _reads_tpch(by_id[pid], "mysql"), pid


def test_crash_or_timeout_of_the_probe_is_never_excused(tmp_path):
    crash = QueryOutcome("crashed", None, None, set(), "gone", "n/a")
    a = ModeAdapter(engine={"FROM pc_a": crash}, control={"FROM pc_a": err("x")})
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error" and rec.extra["outcome"] == "crashed"
    assert "crashed" in rec.extra["server_control"]["skipped"]
    assert len(a.creates) == 1 and a.restarts == 1


def test_probe_whose_restart_failed_runs_no_control(tmp_path):
    crash = QueryOutcome("crashed", None, None, set(), "gone", "n/a")
    a = ModeAdapter(engine={"FROM pc_a": crash})
    a._restart_error = RuntimeError("no server")
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error" and "skipped" in rec.extra["server_control"]
    assert "no server" in rec.extra["restart_error"]


def test_failed_teardown_of_the_probe_skips_the_control(tmp_path):
    """A table left behind would make the control's CREATE fail for the wrong reason."""
    a = ModeAdapter(engine={"FROM pc_a": err("x"), "DROP": err("cannot drop")})
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error"
    assert "teardown failed" in rec.extra["server_control"]["skipped"]
    assert len(a.creates) == 1


def test_control_that_crashes_is_inconclusive_and_recovers(tmp_path):
    crash = QueryOutcome("crashed", None, None, set(), "gone", "n/a")
    a = ModeAdapter(engine={"FROM pc_a": err("x")}, control={"FROM pc_a": crash})
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error" and rec.extra["class"] == "error"
    ctl = rec.extra["server_control"]
    assert ctl["outcome"] == "crashed" and ctl["conclusive"] is False
    assert a.restarts == 1


def test_control_that_times_out_is_inconclusive(tmp_path):
    slow = QueryOutcome("timeout", None, None, set(), "too slow", "n/a")
    a = ModeAdapter(engine={"FROM pc_a": err("x")}, control={"FROM pc_a": slow})
    (rec,) = run(a, [p_()], tmp_path)
    assert rec.status == "error" and rec.extra["server_control"]["conclusive"] is False
    assert a.cancels == 1


def test_mixed_engine_probe_control_uses_row_store_tables_only(tmp_path):
    m = p_("m1", category="joins",
           setup=["CREATE TABLE pc_a (id INT PRIMARY KEY) {{ engine_clause }}",
                  "CREATE TABLE pc_b (id INT PRIMARY KEY) {{ rowstore_clause }}"],
           teardown=["DROP TABLE IF EXISTS pc_a", "DROP TABLE IF EXISTS pc_b"],
           sql={"mysql": "SELECT pc_a.id FROM pc_a JOIN pc_b USING (id)", "postgres": "x"})
    a = ModeAdapter("percona", engine={"JOIN": err("cannot mix engines")})
    (rec,) = run(a, [m], tmp_path)
    assert rec.status == "error" and rec.extra["server_control"] == "ok"
    assert a.creates[2:] == ["CREATE TABLE pc_a (id INT PRIMARY KEY) ENGINE=InnoDB",
                             "CREATE TABLE pc_b (id INT PRIMARY KEY) ENGINE=InnoDB"]


def test_rule_is_the_same_for_every_integration(tmp_path):
    for i, (name, dialect, eng, row) in enumerate([
            ("alisql", "mysql", "ENGINE=DuckDB", "ENGINE=InnoDB"),
            ("mariadb", "mysql", "ENGINE=DuckDB", "ENGINE=InnoDB"),
            ("percona", "mysql", "ENGINE=DuckDB", "ENGINE=InnoDB"),
            ("pg_ducklake", "postgres", "USING ducklake", "")]):
        a = ModeAdapter(name, dialect=dialect, engine_clause=eng, rowstore_clause=row,
                        engine={"FROM pc_a": err("e")}, control={"FROM pc_a": err("e")})
        (rec,) = run(a, [p_()], tmp_path / str(i))
        assert rec.status == "n/a", name
        assert f"the {name} server itself" in rec.reason
        assert a.creates[-1] == f"CREATE TABLE pc_a (id INT PRIMARY KEY) {row}".rstrip()
        b = ModeAdapter(name, dialect=dialect, engine_clause=eng, rowstore_clause=row,
                        engine={"FROM pc_a": err("e")})
        (kept,) = run(b, [p_()], tmp_path / f"k{i}")
        assert kept.status == "error" and kept.extra["server_control"] == "ok", name


# ---- resume: records made before the control are rerun -------------------------------------

def _strip_control(root, system, pid):
    path = root / "r1" / system / f"M5-sf1-{pid}.json"
    doc = json.loads(path.read_text())
    doc["extra"].pop("server_control", None)
    path.write_text(json.dumps(doc))


def test_stale_items_picks_failures_recorded_without_the_control(tmp_path):
    probes = [p_("ok"), p_("bad"), p_("wrong"), p_("crash"),
              probe("tpch", sql={"mysql": "SELECT 1 FROM nation", "postgres": "x"})]
    for q in probes:
        save_ref(tmp_path, q, [(1,)])
    crash = QueryOutcome("crashed", None, None, set(), "gone", "n/a")
    a = ModeAdapter()
    run(a, probes[:1], tmp_path)
    run(ModeAdapter(engine={"FROM pc_a": err("x")}), probes[1:2], tmp_path)
    run(ModeAdapter(engine={"FROM pc_a": out([(2,)])}), probes[2:3], tmp_path)
    run(ModeAdapter(engine={"FROM pc_a": crash}), probes[3:4], tmp_path)
    run(ModeAdapter(engine={"nation": err("x")}), probes[4:], tmp_path)
    assert stale_items(a, tmp_path, "r1", 1, probes) == set()
    for pid in ("ok", "bad", "wrong", "crash", "tpch"):
        _strip_control(tmp_path, "mariadb", pid)
    # only the cells that would now get a control are stale
    assert stale_items(a, tmp_path, "r1", 1, probes) == {"bad", "wrong"}


def test_stale_items_ignores_baselines_and_identical_clauses(tmp_path):
    p = p_()
    inno = ProbeAdapter("innodb", "mysql", "none", {}, track="baseline",
                        engine_clause="ENGINE=InnoDB")
    run(inno, [p], tmp_path)
    assert stale_items(inno, tmp_path, "r1", 1, [p]) == set()
    heap = ModeAdapter("pg_duckdb", dialect="postgres", engine_clause="", rowstore_clause="",
                       engine={"FROM pc_a": err("x")})
    run(heap, [p], tmp_path)
    _strip_control(tmp_path, "pg_duckdb", "p1")
    assert stale_items(heap, tmp_path, "r1", 1, [p]) == set()


def test_control_na_record_is_stale_when_its_reference_changes(tmp_path):
    p = p_()
    save_ref(tmp_path, p, [(1,)])
    a = ModeAdapter(engine={"FROM pc_a": out([(2,)])}, control={"FROM pc_a": out([(2,)])})
    (rec,) = run(a, [p], tmp_path)
    assert rec.status == "n/a" and stale_items(a, tmp_path, "r1", 1, [p]) == set()
    edited = p_(sql={"mysql": "SELECT id FROM pc_a WHERE 1", "postgres": "x"})
    save_ref(tmp_path, edited, [(1,)])
    assert stale_items(a, tmp_path, "r1", 1, [edited]) == {"p1"}


class _CampaignModeAdapter(ModeAdapter):
    def remove_data(self):
        pass


def test_campaign_resume_reruns_exactly_the_failures_without_a_control(tmp_path, monkeypatch):
    from dbench.campaign import run_campaign
    from tests.test_campaign import Env, write_yaml

    regexp = "REGEXP_LIKE(s, '^[a-z]+[0-9]+$', 'c')"
    concat = next(q for q in load_probes() if q.id == "fn_concat").main("mysql")[-1]
    upper = next(q for q in load_probes() if q.id == "fn_upper_lower").main("mysql")[-1]
    no_regexp = err("(1305, 'FUNCTION tpch.REGEXP_LIKE does not exist')")

    def factory(name, data_dir):
        if name == "innodb":
            inno = _CampaignModeAdapter("innodb", engine_clause="ENGINE=InnoDB")
            inno.track, inno.pushdown_counter_scope = "baseline", "none"
            return inno
        return _CampaignModeAdapter(
            "mariadb", engine={regexp: no_regexp, concat: out([("ab",)]),
                               upper: err("engine bug")},
            control={regexp: no_regexp})

    env = Env(tmp_path)
    deps = env.deps()
    deps.adapter_factory = factory
    res = tmp_path / "res"
    path = write_yaml(tmp_path, sf=100, systems=["innodb", "mariadb"], measures=["M5", "M8"],
                      reference={"system": "duckdb-1.5.5"})
    run_campaign(path, res, deps)
    cells = {pid: res / "r1" / "mariadb" / f"M5-sf100-{pid}.json"
             for pid in ("fn_regexp", "fn_concat", "fn_upper_lower", "fn_substring")}
    assert read(cells["fn_regexp"]).status == "n/a"
    assert read(cells["fn_concat"]).status == "wrong_result"
    assert read(cells["fn_upper_lower"]).status == "error"
    assert run_campaign(path, res, deps).written == 0

    # simulate records made before the control existed
    for pid in ("fn_regexp", "fn_concat", "fn_upper_lower"):
        doc = json.loads(cells[pid].read_text())
        doc["extra"].pop("server_control")
        if pid == "fn_regexp":
            doc.update(status="error", reason=None, error=doc["extra"]["integration_error"])
            doc["extra"]["class"] = "error"
        cells[pid].write_text(json.dumps(doc))
    kept = cells["fn_substring"].read_text()
    adapter = factory("mariadb", None)
    assert stale_items(adapter, res, "r1", 100) == {"fn_regexp", "fn_concat", "fn_upper_lower"}
    summary = run_campaign(path, res, deps)
    assert summary.written == 3 + 3  # the three M5 cells and the three M8 cells
    assert read(cells["fn_regexp"]).status == "n/a"
    assert read(cells["fn_concat"]).extra["server_control"] == "ok"
    assert read(cells["fn_upper_lower"]).extra["server_control"] == "ok"
    assert cells["fn_substring"].read_text() == kept
    assert stale_items(adapter, res, "r1", 100) == set()
