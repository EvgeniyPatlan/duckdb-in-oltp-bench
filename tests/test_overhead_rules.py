"""M7 review fixes: per-record versions, partial and common geomeans, derived reference."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from dbench import cli
from dbench.campaign import Summary, run_campaign
from dbench.measures.overhead import compute_overhead
from dbench.reference import reference_system
from dbench.results import iter_records, write
from tests.test_campaign import Env, write_yaml
from tests.test_overhead import by_item, m1


def plain(items=("q01", "q02", "q03"), value=1.0):
    return [m1("duckdb-1.5.5", q, value, version="v1.5.5", track="baseline") for q in items]


def test_unknown_version_is_na_per_record_not_whole_system():
    recs = plain() + [m1("pg_duckdb", "q01", 2.0, version="unknown"),
                      m1("pg_duckdb", "q02", 2.0, version="1.5.5"),
                      m1("pg_duckdb", "q03", 4.0, version="1.5.5")]
    out = by_item(compute_overhead(recs), "pg_duckdb")
    assert out["q01"].status == "n/a" and out["q01"].reason == "DuckDB version not recorded"
    assert out["q02"].value == 2.0 and out["q03"].value == 4.0
    assert out["geomean"].extra["excluded"] == ["q01"]
    assert out["geomean"].value == pytest.approx(math.sqrt(8.0))


def test_mixed_versions_make_geomean_na():
    recs = plain() + [m1("duckdb-1.4.3", "q01", 1.0, version="1.4.3", track="baseline"),
                      m1("pg_duckdb", "q01", 2.0, version="1.4.3"),
                      m1("pg_duckdb", "q02", 2.0, version="1.5.5")]
    out = by_item(compute_overhead(recs), "pg_duckdb")
    assert out["q01"].status == "ok" and out["q02"].status == "ok"
    assert out["geomean"].status == "n/a"
    assert out["geomean"].reason == "mixed DuckDB versions: 1.4.3, 1.5.5"
    assert out["geomean_common"].status == "n/a"


def test_partial_geomean_and_common_geomean():
    recs = plain() + [
        m1("percona", "q01", 2.0, version="1.5.5"),
        m1("percona", "q02", 2.0, version="1.5.5"),
        m1("percona", "q03", 2.0, version="1.5.5"),
        m1("alisql", "q01", 8.0, version="1.5.5"),
        m1("alisql", "q02", 8.0, version="1.5.5"),
        m1("alisql", "q03", None, version="1.5.5", status="timeout")]
    out = compute_overhead(recs)
    pe, al = by_item(out, "percona"), by_item(out, "alisql")
    assert "partial" not in pe["geomean"].extra
    assert al["geomean"].extra["partial"] is True
    assert al["geomean"].extra["queries_total"] == 3 and al["geomean"].extra["queries_used"] == 2
    for gm in (pe["geomean_common"], al["geomean_common"]):
        assert gm.extra["queries"] == ["q01", "q02"]
    assert pe["geomean_common"].value == pytest.approx(2.0)
    assert al["geomean_common"].value == pytest.approx(8.0)


def test_common_geomean_na_when_no_common_query():
    recs = plain(("q01", "q02")) + [m1("percona", "q01", 2.0, version="1.5.5"),
                                    m1("alisql", "q02", 2.0, version="1.5.5")]
    gm = by_item(compute_overhead(recs), "percona")["geomean_common"]
    assert gm.status == "n/a" and "every Track A system" in gm.reason


def test_plain_zero_and_missing_value_reasons():
    recs = plain(("q01",), value=0.0) + [m1("percona", "q01", 2.0, version="1.5.5")]
    rec = by_item(compute_overhead(recs), "percona")["q01"]
    assert rec.status == "n/a" and rec.reason == "plain DuckDB time is zero"
    ok_no_value = plain(("q01",), value=None) + [m1("percona", "q01", 2.0, version="1.5.5")]
    rec = by_item(compute_overhead(ok_no_value), "percona")["q01"]
    assert rec.status == "n/a" and "is ok" not in rec.reason and "no value" in rec.reason


def test_reference_system_is_read_from_the_cached_reference(tmp_path):
    ref = tmp_path / "res" / "r1" / "_reference"
    assert reference_system(tmp_path / "res", "r1") is None
    ref.mkdir(parents=True)
    (ref / "q01.json").write_text(json.dumps({"system": "duckdb-1.5.3", "sf": 1}))
    assert reference_system(tmp_path / "res", "r1") == "duckdb-1.5.3"


def seed(res):
    write(m1("duckdb-1.5.3", "q01", 1.0, version="v1.5.3", track="baseline"), res)
    write(m1("alisql", "q01", 3.0, version="1.5.3"), res)


def test_cli_overhead_derives_reference_version(tmp_path, capsys):
    res = tmp_path / "res"
    seed(res)
    ref = res / "r1" / "_reference"
    ref.mkdir()
    (ref / "q01.json").write_text(json.dumps({"system": "duckdb-1.5.3", "sf": 1}))
    assert cli.main(["overhead", "r1", "--results", str(res)]) == 0
    rec = by_item([r for r in iter_records(res) if r.measure == "M7"], "alisql")["q01"]
    assert "version_gap" not in rec.extra  # reference is 1.5.3, not a hardcoded 1.5.5
    out = capsys.readouterr().out
    assert "geomean" in out and "1/1 queries" in out


def test_cli_overhead_without_reference_requires_flag(tmp_path, capsys):
    res = tmp_path / "res"
    seed(res)
    assert cli.main(["overhead", "r1", "--results", str(res)]) == 2
    assert "--reference-version" in capsys.readouterr().out
    assert cli.main(["overhead", "r1", "--results", str(res),
                     "--reference-version", "1.5.5"]) == 0


def test_campaign_runs_m7_even_when_aborted_and_does_not_mask_error(tmp_path, monkeypatch):
    res = tmp_path / "res"
    env = Env(tmp_path)
    deps = env.deps()
    calls = []
    monkeypatch.setattr("dbench.campaign.run_overhead", lambda *a, **k: calls.append(a) or [])
    deps.run_track_b = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("original"))
    with pytest.raises(RuntimeError, match="original"):
        run_campaign(write_yaml(tmp_path, track_b={"enabled": True, "systems": ["villagesql"]}),
                     res, deps)
    assert len(calls) == 1

    def broken(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr("dbench.campaign.run_overhead", broken)
    with pytest.raises(RuntimeError, match="original"):
        run_campaign(write_yaml(tmp_path, track_b={"enabled": True, "systems": ["villagesql"]}),
                     res, deps)


def test_campaign_summary_carries_overhead_lines(tmp_path):
    summary = run_campaign(write_yaml(tmp_path), tmp_path / "res", Env(tmp_path).deps())
    assert isinstance(summary, Summary)
    assert any(line.startswith("alisql") for line in summary.overhead)


def test_status_table_separates_m7(tmp_path, capsys):
    res = tmp_path / "res"
    seed(res)
    cli.main(["overhead", "r1", "--results", str(res), "--reference-version", "1.5.3"])
    capsys.readouterr()
    cli._status_table(res, ["r1"])
    lines = capsys.readouterr().out.splitlines()
    assert any("r1 alisql: 1 ok" in line for line in lines)
    assert any("r1 M7:" in line for line in lines)


def test_build_images_refuses_root_parent(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "build-images.sh"
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin", "DBENCH_DATA": "/data"})
    if "buildx" in out.stderr and "not available" in out.stderr:
        pytest.skip("docker buildx check comes first on this machine")
    assert out.returncode != 0 and "DBENCH_DATA" in out.stderr


def test_system_without_any_ratio_does_not_empty_common_set():
    recs = plain(("q01", "q02")) + [
        m1("percona", "q01", 2.0, version="1.5.5"),
        m1("percona", "q02", 2.0, version="1.5.5"),
        m1("alisql", "q01", 4.0, version="1.5.5"),
        m1("alisql", "q02", 4.0, version="1.5.5"),
        m1("pg_ducklake", "q01", 3.0, version="9.9.9"),   # no plain run at 9.9.9
        m1("pg_ducklake", "q02", None, version="1.5.5", status="timeout")]
    out = compute_overhead(recs)
    pe, dl = by_item(out, "percona"), by_item(out, "pg_ducklake")
    assert pe["geomean_common"].status == "ok"
    assert pe["geomean_common"].extra["queries"] == ["q01", "q02"]
    assert pe["geomean_common"].extra["systems_excluded"] == ["pg_ducklake"]
    assert dl["geomean_common"].status == "n/a"
    assert "no ok ratio" in dl["geomean_common"].reason
