import pytest

from dbench.systems.base import is_numeric_type, numeric_cols_from_types


@pytest.mark.parametrize("t", ["DECIMAL(15,2)", "DOUBLE", "FLOAT", "REAL", "NUMERIC(10,2)",
                               "BIGINT", "INTEGER", "int4", "HUGEINT", "UBIGINT", "tinyint"])
def test_numeric_types(t):
    assert is_numeric_type(t)


@pytest.mark.parametrize("t", ["VARCHAR", "DATE", "INTERVAL", "TIMESTAMP", "BLOB", "char(1)"])
def test_non_numeric_types(t):
    assert not is_numeric_type(t)


def test_numeric_cols():
    assert numeric_cols_from_types(["VARCHAR", "DECIMAL(15,2)", "DATE", "BIGINT"]) == {1, 3}
