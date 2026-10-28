"""M7 integration overhead: version matching, n/a rules, geometric mean, CLI, summary."""
from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbench.measures.overhead import compute_overhead, normalise_version, run_overhead
from dbench.results import ResultRecord, iter_records, write

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def m1(system: str, item: str, value: float | None, *, version: str, track: str = "A",
       status: str = "ok") -> ResultRecord:
    return ResultRecord(
        system=system, track=track, measure="M1", sf=1, item=item, status=status, value=value,
        unit="s", config_ref=f"systems/{system}/", run_id="r1", started_at=NOW.isoformat(),
        reason="x" if status in ("n/a", "failed") else None,
        versions={"duckdb": version})


def by_item(recs, system):
    return {r.item: r for r in recs if r.system == system}


def test_normalise_version_drops_leading_v():
    assert normalise_version("v1.5.5") == "1.5.5"
    assert normalise_version("1.5.2-dev736") == "1.5.2-dev736"
    assert normalise_version(" v1.4.3 ") == "1.4.3"
    assert normalise_version(None) == "unknown"


def test_overhead_uses_matching_version():
    recs = [
        m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"),
        m1("duckdb-1.4.3", "q01", 4.0, version="v1.4.3", track="baseline"),
        m1("pg_duckdb", "q01", 8.0, version="1.4.3"),
        m1("percona", "q01", 3.0, version="1.5.3"),
        m1("duckdb-1.5.3", "q01", 2.0, version="v1.5.3", track="baseline"),
    ]
    out = compute_overhead(recs)
    pg = by_item(out, "pg_duckdb")["q01"]
    assert (pg.measure, pg.status, pg.value, pg.unit, pg.track) == ("M7", "ok", 2.0, "ratio", "A")
    assert pg.extra["duckdb_version"] == "1.4.3"
    assert pg.extra["plain_system"] == "duckdb-1.4.3"
    assert pg.extra["version_gap"] == "1.5.5"
    assert by_item(out, "percona")["q01"].value == 1.5
    assert not [r for r in out if r.system.startswith("duckdb-")]  # plain systems get no M7


def test_version_equal_to_reference_has_no_gap_note():
    recs = [m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"),
            m1("alisql", "q01", 2.0, version="1.5.5")]
    rec = by_item(compute_overhead(recs), "alisql")["q01"]
    assert rec.value == 2.0 and "version_gap" not in rec.extra
    assert rec.extra["duckdb_version"] == "1.5.5"


def test_missing_native_version_is_na():
    recs = [m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"),
            m1("alisql", "q01", 2.0, version="1.5.2-dev736")]
    out = by_item(compute_overhead(recs), "alisql")
    assert out["q01"].status == "n/a" and out["q01"].value is None
    assert out["q01"].reason == "no plain DuckDB run at version 1.5.2-dev736"
    assert out["geomean"].status == "n/a"


def test_wrong_result_and_failed_sides_are_na():
    recs = [
        m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"),
        m1("duckdb-1.5.5", "q02", 1.0, version="v1.5.5", track="baseline"),
        m1("duckdb-1.5.5", "q03", None, version="v1.5.5", track="baseline", status="timeout"),
        m1("mariadb", "q01", 0.5, version="1.5.5", status="wrong_result"),
        m1("mariadb", "q02", None, version="1.5.5", status="timeout"),
        m1("mariadb", "q03", 2.0, version="1.5.5"),
    ]
    out = by_item(compute_overhead(recs), "mariadb")
    assert all(out[q].status == "n/a" and out[q].value is None for q in ("q01", "q02", "q03"))
    assert "wrong_result" in out["q01"].reason and "no ratio" in out["q01"].reason
    assert "timeout" in out["q02"].reason
    assert "plain DuckDB" in out["q03"].reason and "timeout" in out["q03"].reason


def test_geomean_with_exclusions():
    recs = [m1("duckdb-1.5.5", f"q0{i}", 1.0, version="v1.5.5", track="baseline")
            for i in (1, 2, 3)]
    recs += [m1("percona", "q01", 2.0, version="1.5.5"),
             m1("percona", "q02", 8.0, version="1.5.5"),
             m1("percona", "q03", 5.0, version="1.5.5", status="wrong_result")]
    gm = by_item(compute_overhead(recs), "percona")["geomean"]
    assert gm.status == "ok" and gm.unit == "ratio"
    assert gm.value == pytest.approx(math.sqrt(2.0 * 8.0))
    assert gm.extra["queries_used"] == 2
    assert gm.extra["excluded"] == ["q03"]


def test_baselines_and_other_measures_are_ignored():
    other = m1("innodb", "q01", 9.0, version="unknown", track="baseline")
    m4 = ResultRecord(
        system="alisql", track="A", measure="M4", sf=1, item="streams=1", status="ok",
        value=1.0, unit="qph", config_ref="c", run_id="r1", started_at=NOW.isoformat())
    assert compute_overhead([other, m4]) == []


def test_run_overhead_is_idempotent_and_only_touches_m7(tmp_path: Path):
    root = tmp_path / "res"
    for r in [m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"),
              m1("alisql", "q01", 3.0, version="1.5.5")]:
        write(r, root)
    before = {r.system: r for r in iter_records(root / "r1") if r.measure == "M1"}
    first = run_overhead(root, "r1")
    second = run_overhead(root, "r1")
    m7 = [r for r in iter_records(root / "r1") if r.measure == "M7"]
    assert len(first) == len(second) == len(m7) == 2  # q01 + geomean
    after = {r.system: r for r in iter_records(root / "r1") if r.measure == "M1"}
    assert before == after


def test_run_overhead_drops_stale_m7(tmp_path: Path):
    root = tmp_path / "res"
    write(m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"), root)
    write(m1("alisql", "q01", 3.0, version="1.5.5"), root)
    run_overhead(root, "r1")
    (root / "r1" / "alisql" / "M1-sf1-q01.json").unlink()
    run_overhead(root, "r1")
    assert [r for r in iter_records(root / "r1") if r.measure == "M7"] == []
