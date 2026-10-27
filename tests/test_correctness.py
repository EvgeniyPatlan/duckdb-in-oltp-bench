import dataclasses
from datetime import date, datetime
from decimal import Decimal

import pytest

from dbench.correctness import Comparison, checksum, compare, normalize_value


def test_char_padding_equal():
    assert compare([("AIR   ",)], [("AIR",)], set()).match


def test_decimal_vs_float_equal():
    assert compare([(Decimal("123.45"),)], [(123.4500001,)], {0}).match


def test_double_for_decimal_drift_within_tol():
    assert compare([(Decimal("38250865.47"),)], [(38250865.4699999,)], {0}).match


def test_real_difference_detected():
    res = compare([(Decimal("100.00"),)], [(100.05,)], {0})
    assert not res.match
    assert res.diffs


def test_null_vs_empty_not_equal():
    assert not compare([(None,)], [("",)], set()).match
    assert not compare([(None,)], [("\\N",)], set()).match
    assert not compare([(None,)], [("",)], {0}).match
    assert compare([(None,)], [(None,)], {0}).match


def test_negative_zero():
    assert normalize_value(-0.0, numeric=True) == normalize_value(0, numeric=True)
    assert normalize_value(Decimal("-0.00"), numeric=True) == "0.00"
    assert normalize_value(-0.001, numeric=True) == "0.00"
    assert compare([(-0.0,)], [(Decimal("0.00"),)], {0}).match


def test_order_independent():
    a = [(1, "x", None), (2, "y", 3.5), (None, "z", 1.0)]
    b = [(None, "z", 1.0), (2, "y", 3.5), (1, "x", None)]
    assert compare(a, b, {0, 2}).match
    assert checksum(a, {0, 2}) == checksum(b, {0, 2})


def test_row_count_mismatch_reports_counts():
    res = compare([(1,), (2,), (3,)], [(1,), (2,)], {0})
    assert not res.match
    assert (res.row_count_a, res.row_count_b) == (3, 2)
    assert res.diffs


def test_diffs_capped_at_20():
    res = compare([(i,) for i in range(100)], [(i + 1000,) for i in range(100)], {0})
    assert not res.match
    assert len(res.diffs) == 20


def test_date_vs_midnight_datetime():
    assert compare([(date(1998, 9, 2),)], [(datetime(1998, 9, 2, 0, 0, 0),)], set()).match
    assert not compare([(date(1998, 9, 2),)], [(datetime(1998, 9, 2, 1, 0, 0),)], set()).match


def test_non_numeric_ints_and_bytes():
    assert normalize_value(5) == "5"
    assert normalize_value(b"\x01\xff") == "01ff"
    assert normalize_value(None) == "\\N"
    assert normalize_value("ab  ") == "ab"


def test_numeric_string_in_numeric_column():
    assert compare([("12.50",)], [(Decimal("12.5"),)], {0}).match


def test_relative_tolerance_for_large_values():
    assert compare([(1e12,)], [(1e12 + 100,)], {0}).match
    assert not compare([(1e12,)], [(1e12 + 1e8,)], {0}).match


def test_null_sorts_consistently_in_numeric_column():
    a = [(None, "a"), (1.0, "b")]
    b = [(1.0, "b"), (None, "a")]
    assert compare(a, b, {0}).match


def test_comparison_is_frozen():
    res = compare([(1,)], [(1,)], set())
    assert isinstance(res, Comparison)
    with pytest.raises(dataclasses.FrozenInstanceError):
        res.match = False  # type: ignore[misc]
