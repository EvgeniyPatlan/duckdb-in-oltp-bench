import json
from datetime import date
from decimal import Decimal

import pytest

from dbench.reference import build_reference, cached_reference
from dbench.systems._crash import classify_connection_failure
from dbench.systems.duckdb_native import DuckDBNative
from dbench.systems.innodb import InnoDB
from dbench.systems.postgres import Postgres
from dbench.systems.registry import get_adapter
from tests.fakes import FakeAdapter, ok


def test_registry():
    assert isinstance(get_adapter("duckdb-1.5.5", None), DuckDBNative)
    assert isinstance(get_adapter("innodb", None), InnoDB)
    assert isinstance(get_adapter("postgres", None), Postgres)
    with pytest.raises(ValueError, match="innodb"):
        get_adapter("oracle", None)


@pytest.mark.parametrize("cls,name,dialect,family,cfg", [
    (InnoDB, "innodb", "mysql", "mysql", "innodb"),
    (Postgres, "postgres", "postgres", "postgres", "postgres"),
])
def test_contract(cls, name, dialect, family, cfg):
    a = cls(None)
    assert (a.name, a.track, a.dialect, a.family, a.config_dir) == (
        name, "baseline", dialect, family, cfg)
    for m in ("M11", "M12"):
        ok_, reason = a.supports(m)
        assert not ok_ and reason
    assert a.supports("M1") == (True, None)
    out = a.load_convert(None)
    assert out.status == "n/a" and out.extra["reason"] == "row-store baseline: no DuckDB copy"
    assert a.pushdown("select 1") == "n/a"
    assert a.image == {"innodb": "mysql:9.7", "postgres": "postgres:18"}[name]


def test_crash_helper_polls_until_exit():
    states = iter([{"status": "running"}, {"status": "exited", "ExitCode": 137}])
    out = classify_connection_failure(lambda: next(states), ConnectionResetError("x"),
                                      sleep=lambda s: None)
    assert out.status == "crashed" and "ConnectionResetError" in out.error


def test_crash_helper_running_is_error():
    out = classify_connection_failure(lambda: {"status": "running"}, OSError("x"),
                                      sleep=lambda s: None, poll_s=0.0)
    assert out.status == "error"


def test_mysql_type_codes_and_postgres_oids():
    from dbench.systems.mysql_family import numeric_cols_from_codes as m
    from dbench.systems.postgres_family import numeric_cols_from_oids as p
    assert m([246, 253, 10, 5, 8, 3, 0, 4]) == {0, 3, 4, 5, 6, 7}
    assert p([1700, 1043, 1082, 701, 700, 20, 21, 23]) == {0, 3, 4, 5, 6, 7}


def test_mysql_timeout_error_mapping():
    import pymysql

    from dbench.systems.mysql_family import is_timeout_error
    assert is_timeout_error(pymysql.err.OperationalError(3024, "max execution time"), 1.0, 10)
    assert is_timeout_error(pymysql.err.OperationalError(2013, "Lost (timed out)"), 100.0, 10)
    assert not is_timeout_error(pymysql.err.OperationalError(2013, "Lost connection"), 0.5, 10)


def test_expected_rows_from_manifest(tmp_path):
    from dbench import datagen
    root = tmp_path
    (root / "csv" / "t").mkdir(parents=True)
    for i in range(2):
        (root / "csv" / "t" / f"t.{i}.csv").write_text("1\n")
    paths = datagen.write_manifest(
        root, 1, "x", [(root / "csv" / "t" / f"t.{i}.csv", "t", "csv", 5 + i) for i in range(2)])
    assert datagen.expected_rows(paths) == {"t": 11}


def test_reference_builds_and_caches(tmp_path):
    rows = [(Decimal("1.50"), date(1998, 1, 2), "x", b"\x00\xff")]
    a = FakeAdapter([ok(rows=rows, numeric=(0,))] * 3)
    ref = build_reference(a, 1, "r1", tmp_path, queries=[1, 2])
    assert ref[1]["rows"] == rows and ref[1]["numeric_cols"] == {0}
    assert len(a.executed) == 2
    f = tmp_path / "r1" / "_reference" / "q01.json"
    assert json.loads(f.read_text())["rows"][0][0] == {"$dec": "1.50"}
    b = FakeAdapter([])
    again = build_reference(b, 1, "r1", tmp_path, queries=[1, 2])
    assert b.executed == [] and again[1]["rows"] == rows
    assert cached_reference("fake", 1, "r1", tmp_path, [1, 2]) is not None
    assert cached_reference("fake", 1, "r1", tmp_path, [1, 2, 3]) is None


# --- fix round 1 -----------------------------------------------------------------------
import threading  # noqa: E402

import psycopg  # noqa: E402
import pymysql  # noqa: E402

from dbench import datagen  # noqa: E402
from dbench.systems import postgres_family  # noqa: E402
from dbench.systems.mysql_family import MySQLFamilyAdapter  # noqa: E402


def test_mysql_family_default_storage_excludes_binlog():
    assert "--exclude='binlog.*'" in MySQLFamilyAdapter.du_command
    assert "--exclude='binlog.*'" in InnoDB.du_command


class _Cur:
    description = None

    def __init__(self, fail):
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        if not sql.startswith("SET"):
            raise self.fail


class _MyConn:
    def __init__(self, fail):
        self.fail = fail

    def cursor(self):
        return _Cur(self.fail)

    def thread_id(self):
        return 7

    def close(self):
        pass


@pytest.mark.parametrize("state,expected", [("exited", "crashed"), ("running", "timeout")])
def test_mysql_lost_connection_after_deadline_checks_container(monkeypatch, state, expected):
    a = InnoDB(None)
    monkeypatch.setattr(a, "_connect", lambda **kw: _MyConn(
        pymysql.err.OperationalError(2013, "Lost connection (timed out)")))
    monkeypatch.setattr(a, "crash_info", lambda: {"status": state})
    monkeypatch.setattr("dbench.systems.mysql_family.time.perf_counter", iter(
        [0.0, 0.0, 100.0, 100.0]).__next__)
    monkeypatch.setattr("dbench.systems._crash.EXIT_POLL_S", 0.0)
    assert a.execute("SELECT 1", 10).status == expected


class _PgState:
    def __init__(self):
        self.released = threading.Event()
        self.calls: list[str] = []


class _PgConn:
    """Fake server that ignores statement_timeout and honours only terminate."""

    def __init__(self, st, pid):
        self.st = st
        self.info = type("I", (), {"backend_pid": pid})()

    def execute(self, sql, params=None):
        if "pg_cancel_backend" in sql:
            self.st.calls.append("cancel")
            return self
        if "pg_terminate_backend" in sql:
            self.st.calls.append("terminate")
            self.st.released.set()
            return self
        if sql.startswith("SET"):
            return self
        self.st.released.wait(10)
        raise psycopg.OperationalError("terminating connection due to administrator command")

    def close(self):
        pass


def test_postgres_watchdog_cancels_then_terminates_and_records_timeout(monkeypatch):
    st = _PgState()
    pids = iter(range(100, 200))
    a = Postgres(None)
    monkeypatch.setattr(a, "_connect", lambda *x, **k: _PgConn(st, next(pids)))
    monkeypatch.setattr(a, "crash_info", lambda: {"status": "running"})
    monkeypatch.setattr(postgres_family, "READ_MARGIN_S", 0.2)
    monkeypatch.setattr(postgres_family, "WATCHDOG_GRACE_S", 0.2)
    out = a.execute("SELECT pg_sleep(60)", 0)
    assert out.status == "timeout"
    assert st.calls == ["cancel", "terminate"]


def test_postgres_load_vacuums_freezes_each_table_instead_of_analyze(tmp_path):
    csv = tmp_path / "csv"
    files = []
    for t in datagen.TABLES:
        (csv / t).mkdir(parents=True)
        f = csv / t / f"{t}.0.csv"
        f.write_text("1\n")
        files.append((f, t, "csv", 1))
    paths = datagen.write_manifest(tmp_path, 1, "x", files)

    stmts: list[str] = []

    class C:
        def execute(self, sql, params=None):
            stmts.append(sql)
            return self

        def fetchone(self):
            return (1,)

    out = Postgres(tmp_path)._load(C(), paths, 0.0)
    assert out.status == "ok"
    vac = [s for s in stmts if s.startswith("VACUUM")]
    assert vac == [f"VACUUM (FREEZE, ANALYZE) {t}" for t in datagen.TABLES]
    assert "ANALYZE" not in stmts


class _RaceConn:
    """Fake server where the watchdog's cancel arrives just as the query completes: the query
    still returns its rows, so a cancel that is not tied to it would hit the next query."""

    def __init__(self, st, pid):
        self.st, self.closed = st, False
        self.info = type("I", (), {"backend_pid": pid})()

    def execute(self, sql, params=None):
        if "pg_cancel_backend" in sql or "pg_terminate_backend" in sql:
            self.st.calls.append((sql.split("(")[0].split()[-1], params[0]))
            self.st.released.set()
            return self
        if not sql.startswith("SET"):
            self.st.ran_on.append((sql, self.info.backend_pid))
            if sql == "SELECT race":
                self.st.released.wait(10)
        cur = type("Cur", (), {})()
        cur.description = [type("D", (), {"type_code": 23, "name": "n"})()]
        cur.fetchall = lambda: [(1,)]
        return cur

    def close(self):
        self.closed = True


def _race_adapter(monkeypatch):
    st = _PgState()
    st.ran_on = []
    pids = iter(range(100, 200))
    a = Postgres(None)
    monkeypatch.setattr(a, "_connect", lambda *x, **k: _RaceConn(st, next(pids)))
    monkeypatch.setattr(a, "crash_info", lambda: {"status": "running"})
    return a, st


def test_postgres_watchdog_does_nothing_once_the_query_is_done(monkeypatch):
    a, st = _race_adapter(monkeypatch)
    done, fired = threading.Event(), threading.Event()
    done.set()
    a._watchdog(100, done, fired)
    assert st.calls == [] and not fired.is_set()


def test_postgres_watchdog_racing_a_finished_query_never_reaches_the_next_one(monkeypatch):
    a, st = _race_adapter(monkeypatch)
    monkeypatch.setattr(postgres_family, "READ_MARGIN_S", 0.05)
    monkeypatch.setattr(postgres_family, "WATCHDOG_GRACE_S", 0.5)
    first = a.execute("SELECT race", 0)  # the watchdog fires, the query still returns rows
    assert first.status == "ok"
    assert st.calls and st.calls[0] == ("pg_cancel_backend", 100)
    assert a.execute("SELECT next", 900).status == "ok"
    (_, next_pid), = [r for r in st.ran_on if r[0] == "SELECT next"]
    assert next_pid != 100  # the backend the cancel was sent to is not reused
