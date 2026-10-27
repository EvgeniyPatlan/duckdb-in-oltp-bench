import re

import pytest

from dbench.schema import TABLES, render_schema, split_statements

DIALECTS = ("mysql", "postgres")
FK_INDEXED = {
    "nation": ["n_regionkey"], "supplier": ["s_nationkey"], "customer": ["c_nationkey"],
    "orders": ["o_custkey"], "partsupp": ["ps_partkey", "ps_suppkey"],
    "lineitem": ["l_orderkey", "l_partkey", "l_suppkey"],
}


def _flat(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


@pytest.mark.parametrize("dialect", DIALECTS)
def test_rowstore_schema_has_pk_and_fk_indexes(dialect):
    sql = _flat(render_schema(dialect, "", "rowstore"))
    assert "PRIMARY KEY (l_orderkey, l_linenumber)" in sql
    assert "PRIMARY KEY (ps_partkey, ps_suppkey)" in sql
    for table, cols in FK_INDEXED.items():
        for col in cols:
            assert re.search(rf"CREATE INDEX \w+ ON {table} \({col}\)", sql), (table, col)
    assert sql.count("CREATE INDEX") == sum(len(c) for c in FK_INDEXED.values())


@pytest.mark.parametrize("dialect", DIALECTS)
def test_engine_schema_pk_only(dialect):
    sql = render_schema(dialect, "", "engine")
    assert "CREATE INDEX" not in sql
    assert "PRIMARY KEY (l_orderkey, l_linenumber)" in _flat(sql)
    assert len(split_statements(sql)) == len(TABLES)


@pytest.mark.parametrize("dialect,clause", [("mysql", "ENGINE=DuckDB"),
                                            ("postgres", "USING ducklake")])
def test_engine_clause_injected(dialect, clause):
    stmts = split_statements(render_schema(dialect, clause, "engine"))
    assert len(stmts) == 8
    assert all(s.rstrip().endswith(f") {clause}") for s in stmts)


def test_unknown_inputs_rejected():
    with pytest.raises(ValueError):
        render_schema("oracle", "", "engine")
    with pytest.raises(ValueError):
        render_schema("mysql", "", "bogus")


def test_primary_keys_can_be_disabled_for_engines_without_them():
    sql = render_schema("postgres", "USING ducklake", "engine", primary_keys=False)
    assert "PRIMARY KEY" not in sql


def test_single_shared_template():
    from importlib import resources
    names = {p.name for p in resources.files("dbench.schema").iterdir() if p.name.endswith(".j2")}
    assert names == {"tpch.sql.j2"}
    assert render_schema("mysql", "", "rowstore") == render_schema("postgres", "", "rowstore")
