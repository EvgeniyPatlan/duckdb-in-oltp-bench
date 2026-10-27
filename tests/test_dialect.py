import re

import pytest

from dbench.queries.dialect import RULES, dialect_diff, load_query, translate

ALL = range(1, 23)


def test_mysql_interval_add():
    sql = "WHERE d < date '1994-01-01' + interval '1' year"
    assert translate(sql, "mysql") == "WHERE d < DATE_ADD(date '1994-01-01', INTERVAL 1 YEAR)"


def test_mysql_interval_sub():
    sql = "WHERE d <= date '1998-12-01' - interval '90' day"
    assert translate(sql, "mysql") == "WHERE d <= DATE_SUB(date '1998-12-01', INTERVAL 90 DAY)"


def test_mysql_bare_interval():
    assert translate("x + interval '3' month", "mysql") == "x + INTERVAL 3 MONTH"


def test_mysql_cast_date():
    assert translate("d < '1998-12-01'::date", "mysql") == "d < DATE '1998-12-01'"


def test_mysql_derived_column_list():
    sql = (
        "SELECT c_custkey, count(o_orderkey)\nFROM t\n"
        "GROUP BY c_custkey) AS c_orders (c_custkey,\n  c_count)"
    )
    out = translate(sql, "mysql")
    assert "AS c_orders (" not in out
    assert "count(o_orderkey) AS c_count" in out


def test_duckdb_is_identity():
    for n in ALL:
        q = load_query(n)
        assert translate(q, "duckdb") == q


def test_postgres_identity_for_all_22():
    for n in ALL:
        assert translate(load_query(n), "postgres") == load_query(n)
        assert dialect_diff(n, "postgres") == ""


def test_every_query_translates_without_exception():
    for n in ALL:
        for dialect in ("duckdb", "postgres", "mysql"):
            assert translate(load_query(n), dialect)


def test_mysql_leaves_no_quoted_interval_or_double_colon_casts():
    for n in ALL:
        out = translate(load_query(n), "mysql")
        assert "interval '" not in out.lower(), n
        assert "::" not in out, n


def test_load_query_text_and_range():
    assert load_query(1).lstrip().upper().startswith("SELECT")
    assert load_query(1).endswith("\n")
    for bad in (0, 23, -1):
        with pytest.raises(ValueError):
            load_query(bad)


def test_unknown_dialect():
    with pytest.raises(ValueError):
        translate("select 1", "oracle")
    with pytest.raises(ValueError):
        dialect_diff(1, "oracle")


def test_dialect_diff_unified_headers():
    diff = dialect_diff(13, "mysql")
    assert diff.startswith("--- q13.duckdb.sql\n+++ q13.mysql.sql\n")
    assert "-" in diff and "@@" in diff
    assert dialect_diff(1, "mysql") == ""


def test_rules_are_listed_with_comments():
    assert RULES["mysql"]
    for rule in RULES["mysql"]:
        assert isinstance(rule.pattern, re.Pattern)
        assert rule.comment


def test_q13_real_mysql_translation():
    out = translate(load_query(13), "mysql")
    assert "AS c_count" in out
    assert "c_orders (" not in out


def test_only_q13_differs_for_mysql():
    assert {n for n in ALL if dialect_diff(n, "mysql")} == {13}
