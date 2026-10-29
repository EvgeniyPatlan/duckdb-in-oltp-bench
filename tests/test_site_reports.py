"""Reports per campaign, computed findings, author's notes and the explore page."""
from __future__ import annotations

import json
import re
from pathlib import Path

from dbench.results import ResultRecord, write
from sitegen import findings
from sitegen.build import build_site
from sitegen.reports import report_id

STAMP = "2026-10-29T06:00:00+00:00"
LATER = "2026-11-05T06:00:00+00:00"


def rec(system="percona", track="A", item="q01", status="ok", value=1.0, sf=10, run="gate",
        started=STAMP, measure="M1", reason=None, error=None, version="2.0.0",
        extra=None) -> ResultRecord:
    return ResultRecord(
        system=system, track=track, measure=measure, sf=sf, item=item, status=status, value=value,
        unit="s", config_ref=f"systems/{system}/", run_id=run, started_at=started, reason=reason,
        error=error, versions={"duckdb": version}, extra=extra or {})


def build(tmp_path: Path, records: list[ResultRecord], notes: dict[str, str] | None = None):
    root, out = tmp_path / "res", tmp_path / "site"
    for r in records:
        write(r, root)
    for rid, text in (notes or {}).items():
        (root / "notes").mkdir(parents=True, exist_ok=True)
        (root / "notes" / f"{rid}.md").write_text(text, encoding="utf-8")
    build_site(root, out)
    return out


def two_campaigns() -> list[ResultRecord]:
    gate = [rec(item=q, value=1.0) for q in ("q01", "q02")]
    gate += [rec(system="alisql", item=q, value=2.0, version="1.5.2-dev736")
             for q in ("q01", "q02")]
    gate.append(rec(system="villagesql", track="B", item="q01", run="gate-trackb", value=3.0))
    talk = [rec(item=q, value=10.0, sf=100, run="talk", started=LATER) for q in ("q01", "q02")]
    return gate + talk


def test_report_id_joins_track_b_to_its_campaign():
    assert report_id("gate-sf10-trackb") == "gate-sf10"
    assert report_id("gate-sf10") == "gate-sf10"


def test_each_campaign_gets_its_own_pages(tmp_path):
    out = build(tmp_path, two_campaigns())
    for rid in ("gate", "talk"):
        for page in ("index.html", "track_a.html", "track_b.html", "explore.html"):
            assert (out / "reports" / rid / page).is_file(), (rid, page)
    gate = json.loads((out / "reports/gate/data/track_a.json").read_text(encoding="utf-8"))
    assert list(gate["power"]) == ["10"]
    assert gate["run_ids"] == ["gate"]
    track_b = json.loads((out / "reports/gate/data/track_b.json").read_text(encoding="utf-8"))
    assert "villagesql" in json.dumps(track_b)
    talk = json.loads((out / "reports/talk/data/track_a.json").read_text(encoding="utf-8"))
    assert list(talk["power"]) == ["100"]


def test_root_still_shows_every_campaign(tmp_path):
    out = build(tmp_path, two_campaigns())
    root = json.loads((out / "data/track_a.json").read_text(encoding="utf-8"))
    assert set(root["power"]) == {"10", "100"}


def test_switcher_lists_reports_newest_first_and_marks_the_current(tmp_path):
    out = build(tmp_path, two_campaigns())
    html = (out / "reports/gate/track_a.html").read_text(encoding="utf-8")
    opts = re.findall(r'<option value="([^"]*)"( selected)?>', html)
    assert [v for v, _ in opts] == ["", "reports/talk/", "reports/gate/"]
    assert dict(opts)["reports/gate/"] == " selected"
    assert 'data-root="../../"' in html and 'data-page="track_a"' in html
    assert "Only this campaign's records are shown" in html


def test_report_pages_link_locally_and_to_shared_pages(tmp_path):
    out = build(tmp_path, two_campaigns())
    html = (out / "reports/gate/index.html").read_text(encoding="utf-8")
    assert 'href="explore.html"' in html and 'href="../../methodology.html"' in html
    assert 'src="../../static/charts.js"' in html
    system = (out / "systems/percona.html").read_text(encoding="utf-8")
    assert 'href="../explore.html"' in system


def test_reports_page_lists_and_compares_reports(tmp_path):
    out = build(tmp_path, two_campaigns())
    html = (out / "reports.html").read_text(encoding="utf-8")
    assert 'href="reports/gate/index.html"' in html and 'href="reports/talk/index.html"' in html
    blob = re.search(r'id="site-data">(.*?)</script>', html, re.S).group(1)
    doc = json.loads(blob)
    gate = next(r for r in doc["reports"] if r["id"] == "gate")
    assert gate["summary"]["10"]["percona"]["own_total"] == 2.0
    assert gate["summary"]["10"]["alisql"]["ok"] == 2


def test_findings_state_counts_and_ranges(tmp_path):
    recs = [rec(item=q, value=1.0) for q in ("q01", "q02")]
    recs += [rec(system="alisql", item="q01", value=2.0, version="1.5.2-dev736"),
             rec(system="alisql", item="q02", status="wrong_result", value=None,
                 version="1.5.2-dev736")]
    out = build(tmp_path, recs)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "1 of 2 systems answered all 2 queries with the reference result." in html
    assert "AliSQL: wrong result on q02" in html
    assert 'id="findings"' in (out / "reports/gate/index.html").read_text(encoding="utf-8")


def test_findings_never_rank():
    ta = {"systems": [{"name": "a", "label": "A", "kind": "integration"},
                      {"name": "b", "label": "B", "kind": "integration"}],
          "sfs": [10], "power": {"10": {
              "queries": ["q01"], "common_queries": ["q01"],
              "cells": {"a": {"q01": {"s": "ok", "v": 2.0}}, "b": {"q01": {"s": "ok", "v": 1.0}}},
              "totals": {"a": {"common": {"s": "ok", "v": 2.0}},
                         "b": {"common": {"s": "ok", "v": 1.0}}}}}}
    text = json.dumps(findings.for_report(ta, {"power": {}}, "10"))
    for word in ("best", "winner", "fastest", "worst", "rank", "beats"):
        assert word not in text.lower(), word
    assert "1.00 s to 2.00 s across all 2 integrations" in text
    speed = next(f for f in findings.for_report(ta, {"power": {}}, "10") if f["key"] == "speed")
    assert speed["items"] == ["A: 2.00 s", "B: 1.00 s"]  # every system, in the fixed order


def test_range_counts_systems_without_a_value():
    ta = {"systems": [{"name": "a", "label": "A", "kind": "integration"},
                      {"name": "b", "label": "B", "kind": "integration"}],
          "sfs": [10], "overhead": {"10": {
              "a": {"headline": {"s": "ok", "v": 1.5, "duckdb_version": "1.5.5"}},
              "b": {"headline": {"s": "n/a", "v": None, "r": "no plain run"}}}}}
    f = findings.overhead(ta, "10")
    assert "1.50x across 1 of 2 integrations" in f["text"]
    assert f["items"] == ["A: 1.50x against DuckDB 1.5.5", "B: n/a (no plain run)"]


def test_fallback_finding_does_not_claim_more_than_was_seen():
    ta = {"systems": [{"name": "a", "label": "A", "kind": "integration"}], "sfs": [10],
          "fallback": {"10": {"a": {"fallback_share_tpch": {"s": "ok", "v": 0.0,
                                                             "unobserved": 0.5}}}}}
    f = findings.fallback(ta, "10")
    assert "none on 0 of the 1 integrations" in f["text"]
    assert f["items"] == ["A: TPC-H 0% (50% unobserved), probes not reported"]
    assert findings.fallback({"systems": [], "fallback": {"10": {}}}, "10") is None


def test_unsafe_run_ids_get_no_report_pages(tmp_path):
    out = build(tmp_path, [rec(run="gate"), rec(system="alisql", run="../../x")])
    assert (out / "reports" / "gate" / "index.html").is_file()
    assert sorted(p.name for p in (out / "reports").iterdir()) == ["gate"]


def test_track_b_only_report_shows_its_findings(tmp_path):
    out = build(tmp_path, [rec(system="villagesql", track="B", run="tb-trackb")])
    html = (out / "reports/tb/index.html").read_text(encoding="utf-8")
    assert "Parquet entry points" in html
    section = html[html.index('class="findings"'):][:200]
    assert "hidden" not in section


def test_findings_list_silent_wrong_probe_results(tmp_path):
    probe = rec(measure="M5", item="dml_upsert", status="wrong_result", value=None,
                extra={"class": "wrong_result", "category": "dml", "title": "Upsert"})
    out = build(tmp_path, [rec(), probe])
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Wrong result, Percona: Upsert (dml_upsert)" in html


def test_author_notes_are_rendered_safely_with_disclosure(tmp_path):
    out = build(tmp_path, [rec()], notes={"gate": "# Reading\n\nQ4 <script>x</script> is slow."})
    html = (out / "reports/gate/index.html").read_text(encoding="utf-8")
    assert "Author&#39;s notes" in html or "Author's notes" in html
    assert "maintains the Percona engine" in html
    assert "<script>x</script>" not in html and "Q4" in html
    assert "Author's notes" not in (out / "index.html").read_text(encoding="utf-8")


def test_notes_without_a_report_are_ignored(tmp_path):
    out = build(tmp_path, [rec()], notes={"other": "unused"})
    assert "unused" not in (out / "reports/gate/index.html").read_text(encoding="utf-8")


def test_explore_page_has_its_tools_and_downloads(tmp_path):
    out = build(tmp_path, [rec()])
    html = (out / "explore.html").read_text(encoding="utf-8")
    for needle in ('data-chart="pair_ratio"', 'data-chart="query_detail"', 'id="probe-table"',
                   'data-csv="pair-table"', 'href="data/track_a.json"', "static/explore.js"):
        assert needle in html, needle


def test_empty_site_has_no_reports(tmp_path):
    out = tmp_path / "site"
    build_site(tmp_path / "none", out)
    assert not (out / "reports").exists()
    assert "No results are published yet" in (out / "reports.html").read_text(encoding="utf-8")
    assert 'id="report-switch"' not in (out / "index.html").read_text(encoding="utf-8")


def test_report_dates_are_shown_and_tell_reports_apart(tmp_path):
    from sitegen.reports import dates, label_dates
    d = dates("2026-10-28T16:45:06+00:00", "2026-10-29T06:39:14+00:00")
    assert d == {"date": "2026-10-29", "time": "06:39", "took": "13 h 54 min",
                 "period": "2026-10-28 16:45 to 2026-10-29 06:39 UTC"}
    assert dates("2026-10-29T06:00:00+02:00", "2026-10-29T06:20:00+02:00")["period"] == \
        "2026-10-29 04:00 to 04:20 UTC"
    assert dates(None, None)["date"] is None
    same_day = label_dates([{**dates(None, STAMP), "id": "a"},
                            {**dates(None, "2026-10-29T09:30:00+00:00"), "id": "b"},
                            {**dates(None, LATER), "id": "c"}, {**dates(None, None), "id": "d"}])
    assert [e["when"] for e in same_day] == ["2026-10-29 06:00", "2026-10-29 09:30", "2026-11-05",
                                             "undated"]
    out = build(tmp_path, two_campaigns())
    html = (out / "reports/gate/track_a.html").read_text(encoding="utf-8")
    assert re.search(r'<option value="reports/gate/" selected>2026-10-29 &middot; gate \(SF10\)',
                     html)
    assert "2026-11-05 &middot; talk (SF100)" in html
    assert '<time class="date" datetime="2026-10-29">2026-10-29</time> Report <strong>gate' in html
    listing = (out / "reports.html").read_text(encoding="utf-8")
    assert "measured 2026-10-29 06:00 to 06:00 UTC (0 min)" in listing
    assert "2026-10-29 to 2026-11-05" in listing
    doc = json.loads(re.search(r'id="site-data">(.*?)</script>', listing, re.S).group(1))
    when = {r["id"]: r["when"] for r in doc["reports"]}
    assert when == {"gate": "2026-10-29", "talk": "2026-11-05"}


def test_villagesql_is_listed_with_the_others_and_its_parquet_numbers_kept_apart(tmp_path):
    out = build(tmp_path, two_campaigns())
    html = (out / "reports/gate/index.html").read_text(encoding="utf-8")
    assert "Track B" not in html
    row = re.search(r'<tr><th scope="row"><a [^>]*>VillageSQL</a></th>.*?</tr>', html,
                    re.S).group(0)
    assert "measured on Parquet files only" in row and "&ndash;" not in row and "–" in row
    assert 'id="parquet"' in html and 'data-chart="trackb_total"' in html
    page = json.loads(re.search(r'id="site-data">(.*?)</script>', html, re.S).group(1))
    assert "villagesql" not in json.dumps(page["power"])          # never on a table-based chart
    assert [s["name"] for s in page["parquet"]["systems"]] == ["villagesql"]
    assert "villagesql" in page["parquet"]["power"]["10"]["cells"]
    talk = (out / "reports/talk/index.html").read_text(encoding="utf-8")
    assert 'id="parquet"' not in talk and "VillageSQL</a></th>" not in talk
