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
