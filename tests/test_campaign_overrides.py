"""Per-system measure overrides, automatic M7, the unchecked-cell summary and `dbench overhead`."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dbench import cli
from dbench.campaign import CampaignError, Summary, load_campaign, run_campaign, unchecked_cells
from dbench.results import iter_records, write
from tests.test_campaign import Env, write_yaml
from tests.test_overhead import m1

OVER = {"innodb": {"measures": ["M1"]}}


def test_override_replaces_measures_of_one_system(tmp_path):
    c = load_campaign(write_yaml(tmp_path, measures=["M1", "M3"], overrides=OVER))
    assert c.measures_for("innodb") == ("M1",)
    assert c.measures_for("postgres") == ("M1", "M3")
    assert {m for _, m, _, _ in c.planned("innodb")} == {"M1"}
    assert {m for _, m, _, _ in c.planned("postgres")} == {"M1", "M3"}


def test_override_limits_what_runs(tmp_path):
    res = tmp_path / "res"
    run_campaign(write_yaml(tmp_path, measures=["M1", "M3"], overrides=OVER), res,
                 Env(tmp_path).deps())
    seen = {(r.system, r.measure) for r in iter_records(res) if r.measure != "M7"}
    assert ("innodb", "M3") not in seen and ("innodb", "M1") in seen
    assert ("postgres", "M3") in seen


@pytest.mark.parametrize("overrides,msg", [
    ({"nope": {"measures": ["M1"]}}, "nope"),
    ({"mariadb": {"measures": ["M1"]}}, "not in systems"),
    ({"innodb": {"measures": ["M99"]}}, "M99"),
    ({"innodb": {"measures": []}}, "non-empty"),
    ({"innodb": {"extra": 1}}, "innodb"),
    (["innodb"], "mapping"),
])
def test_bad_overrides_are_rejected(tmp_path, overrides, msg):
    with pytest.raises(CampaignError, match=msg):
        load_campaign(write_yaml(tmp_path, overrides=overrides))


def test_campaign_end_writes_m7_without_counting_it(tmp_path):
    res = tmp_path / "res"
    summary = run_campaign(write_yaml(tmp_path), res, Env(tmp_path).deps())
    m7 = [r for r in iter_records(res) if r.measure == "M7"]
    assert {r.system for r in m7} == {"alisql"}  # only the Track A system
    assert summary.written == 4 * 22


def test_unchecked_cells_lists_ok_results_without_comparison(tmp_path):
    res = tmp_path / "res"
    checked = m1("alisql", "q01", 1.0, version="1.5.5")
    write(replace(checked, extra={"compared": True}), res)
    write(replace(checked, item="q02"), res)
    write(replace(checked, item="q03", extra={"compared": False}, status="wrong_result"), res)
    assert unchecked_cells(res, "r1") == [("alisql", "M1", "q02")]


def test_cli_campaign_prints_unchecked_cells(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("dbench.campaign.run_campaign", lambda *a, **k: Summary(
        written=1, unchecked=[("alisql", "M1", "q02")]))
    assert cli.main(["campaign", str(write_yaml(tmp_path)), "--results", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "1 cells were not checked" in out and "alisql M1 q02" in out


def test_cli_overhead(tmp_path, capsys):
    res = tmp_path / "res"
    write(m1("duckdb-1.5.5", "q01", 1.0, version="v1.5.5", track="baseline"), res)
    write(m1("alisql", "q01", 3.0, version="1.5.5"), res)
    assert cli.main(["overhead", "r1", "--results", str(res),
                     "--reference-version", "1.5.5"]) == 0
    assert "alisql q01: 3.000x" in capsys.readouterr().out
    items = {r.item for r in iter_records(res) if r.measure == "M7"}
    assert items == {"q01", "geomean", "geomean_common"}
    assert cli.main(["overhead", "missing", "--results", str(res)]) == 2


@pytest.mark.parametrize("name", ["gate-sf10", "talk-sf100"])
def test_committed_campaigns_load(name):
    from pathlib import Path

    c = load_campaign(Path(__file__).resolve().parents[1] / "campaigns" / f"{name}.yaml")
    assert c.planned("innodb")


@pytest.mark.parametrize("overrides,missing", [
    ({"innodb": {"measures": ["M1"]}}, "innodb"),
    ({"postgres": {"measures": ["M1", "M2"]}}, "postgres"),
])
def test_engine_probes_need_their_dialect_baseline_probes(tmp_path, overrides, missing):
    """An engine's M5 probes are compared with the same-dialect baseline's results, so a
    campaign that runs engine M5 without the baseline's M5 would leave every probe unchecked."""
    path = write_yaml(tmp_path, systems=["duckdb-1.5.5", "innodb", "postgres", "alisql",
                                         "pg_duckdb"],
                      measures=["M1", "M5"], overrides=overrides)
    with pytest.raises(CampaignError, match=f"{missing}.*M5"):
        load_campaign(path)


def test_baseline_probes_not_needed_without_engine_probes(tmp_path):
    over = {"innodb": {"measures": ["M1"]}, "alisql": {"measures": ["M1"]}}
    c = load_campaign(write_yaml(tmp_path, measures=["M1", "M5"], overrides=over))
    assert c.measures_for("innodb") == ("M1",)


def test_committed_campaigns_keep_baseline_probes():
    for name in ("gate-sf10", "talk-sf100"):
        c = load_campaign(Path(__file__).parents[1] / "campaigns" / f"{name}.yaml")
        for baseline in ("innodb", "postgres"):
            assert "M5" in c.measures_for(baseline), (name, baseline)
            assert "M4" not in c.measures_for(baseline), (name, baseline)


def test_baseline_probes_must_run_before_engine_probes(tmp_path):
    path = write_yaml(tmp_path, sf=100, measures=["M1", "M5"],
                      systems=["duckdb-1.5.5", "alisql", "innodb", "postgres"])
    with pytest.raises(CampaignError, match="innodb.*before alisql"):
        load_campaign(path)
