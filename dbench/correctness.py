"""Tolerant, order-independent comparison of query results across systems.

Drivers return DECIMAL as Decimal, DOUBLE as float, CHAR as (possibly padded)
str, DATE as date or midnight datetime, and so on. This module reduces those
representation differences while still flagging real value differences.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from itertools import zip_longest
from typing import Any

NULL_TEXT = "\\N"
MAX_DIFFS = 20


@dataclass(frozen=True)
class Comparison:
    match: bool
    row_count_a: int
    row_count_b: int
    diffs: list[tuple]


def _to_float(v: Any) -> float | None:
    """Float value of a number or numeric string; None if not numeric."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _decimals(abs_tol: float) -> int:
    return max(0, math.ceil(-math.log10(abs_tol))) if 0 < abs_tol < 1 else 0


def _normalize_temporal(v: date) -> str:
    if isinstance(v, datetime):
        if v.time() == time(0):
            return v.date().isoformat()
        return v.isoformat(sep=" ")
    return v.isoformat()


def normalize_value(v: Any, numeric: bool = False, abs_tol: float = 0.01) -> str:
    """Canonical text for one cell. NULL becomes the literal ``\\N``."""
    if v is None:
        return NULL_TEXT
    if numeric:
        x = _to_float(v)
        if x is not None:
            if math.isnan(x) or math.isinf(x):
                return str(x)
            text = f"{round(x, _decimals(abs_tol)):.{_decimals(abs_tol)}f}"
            if float(text) == 0:
                text = text.lstrip("-")
            return text
    if isinstance(v, (date, datetime)):
        return _normalize_temporal(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    if isinstance(v, str):
        return v.rstrip()
    return str(v)


def checksum(
    rows: Iterable[Sequence],
    numeric_cols: set[int],
    rel_tol: float = 1e-6,
    abs_tol: float = 0.01,
) -> str:
    """Display-only SHA-256 of the normalized, sorted rows (not used to decide equality)."""
    lines = sorted(
        "\x1f".join(
            normalize_value(v, i in numeric_cols, abs_tol) for i, v in enumerate(row)
        )
        for row in rows
    )
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8", "surrogatepass"))
        h.update(b"\n")
    return h.hexdigest()


def _cell_key(v: Any, numeric: bool, abs_tol: float) -> tuple:
    # None sorts first and is distinct from every string, including "" and "\N".
    if v is None:
        return (0,)
    if numeric:
        x = _to_float(v)
        if x is not None:
            return (1, 0, 0.0 if math.isnan(x) else x, "")
    return (1, 1, 0.0, normalize_value(v, False, abs_tol))


def _row_key(row: Sequence, numeric_cols: set[int], abs_tol: float) -> tuple:
    return tuple(_cell_key(v, i in numeric_cols, abs_tol) for i, v in enumerate(row))


def _numbers_equal(a: float, b: float, rel_tol: float, abs_tol: float) -> bool:
    if a == b or (math.isnan(a) and math.isnan(b)):
        return True
    if math.isinf(a) or math.isinf(b):
        return False
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def _cells_equal(a: Any, b: Any, numeric: bool, rel_tol: float, abs_tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if numeric:
        fa, fb = _to_float(a), _to_float(b)
        if fa is not None and fb is not None:
            return _numbers_equal(fa, fb, rel_tol, abs_tol)
    return normalize_value(a, False, abs_tol) == normalize_value(b, False, abs_tol)


def _rows_equal(ra: Sequence, rb: Sequence, numeric_cols: set[int],
                rel_tol: float, abs_tol: float) -> bool:
    if len(ra) != len(rb):
        return False
    return all(
        _cells_equal(a, b, i in numeric_cols, rel_tol, abs_tol)
        for i, (a, b) in enumerate(zip(ra, rb, strict=True))
    )


def compare(
    rows_a: Iterable[Sequence],
    rows_b: Iterable[Sequence],
    numeric_cols: set[int],
    rel_tol: float = 1e-6,
    abs_tol: float = 0.01,
) -> Comparison:
    """Order-independent comparison: sort both sides, then walk pairwise.

    Numbers match if ``abs(a-b) <= max(abs_tol, rel_tol*max(|a|,|b|))``.
    Each diff is ``(position, row_a, row_b)``; the missing side is None when
    row counts differ. At most 20 diffs are kept.
    """
    def key(row: Sequence) -> tuple:
        return _row_key(row, numeric_cols, abs_tol)

    sa = sorted((tuple(r) for r in rows_a), key=key)
    sb = sorted((tuple(r) for r in rows_b), key=key)

    diffs: list[tuple] = []
    for pos, (ra, rb) in enumerate(zip_longest(sa, sb)):
        if ra is None or rb is None or not _rows_equal(ra, rb, numeric_cols, rel_tol, abs_tol):
            diffs.append((pos, ra, rb))
            if len(diffs) >= MAX_DIFFS:
                break
    return Comparison(
        match=not diffs and len(sa) == len(sb),
        row_count_a=len(sa),
        row_count_b=len(sb),
        diffs=diffs,
    )
