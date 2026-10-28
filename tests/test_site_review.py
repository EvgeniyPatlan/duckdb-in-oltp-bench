"""Site hardening: markup safety, run selection, common-set bookkeeping, vendored ECharts,
the Pages workflow and its guard."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from dbench.results import ResultRecord, write
from sitegen import aggregate, guard, md
from sitegen.build import build_site
from tests.fixtures.synthetic import STAMP, write_synthetic

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "pages.yml"


def rec(system="percona", track="A", item="q01", status="ok", value=1.0, sf=10, run="real1",
        started=STAMP, measure="M1", reason=None, error=None, version="1.5.3") -> ResultRecord:
    return ResultRecord(
        system=system, track=track, measure=measure, sf=sf, item=item, status=status, value=value,
        unit="s", config_ref=f"systems/{system}/", run_id=run, started_at=started, reason=reason,
        error=error, versions={"duckdb": version})


def build(tmp_path: Path, records: list[ResultRecord]) -> tuple[Path, Path]:
    root, out = tmp_path / "res", tmp_path / "site"
    for r in records:
        write(r, root)
    build_site(root, out)
    return root, out


def data(out: Path, name="track_a") -> dict:
    return json.loads((out / "data" / f"{name}.json").read_text(encoding="utf-8"))


# ---- markup safety ----

def test_markdown_link_cannot_break_out_of_href():
    html = md.render('[x](https://a.example/"onmouseover="alert(1))')
    assert '"onmouseover' not in html
    assert "&quot;" in html


@pytest.mark.parametrize("url", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x",
                                 "vbscript:x"])
def test_markdown_drops_unsafe_schemes(url):
    html = md.render(f"[click]({url})")
    assert "href" not in html and "click" in html


def test_markdown_keeps_http_relative_and_fragment_links():
    html = md.render("[a](https://x.org/p?a=1&b=2) [b](../systems/x.html) [c](#top)")
    assert 'href="https://x.org/p?a=1&amp;b=2"' in html
    assert 'href="../systems/x.html"' in html and 'href="#top"' in html


def test_markdown_escapes_markup_in_text_and_urls():
    html = md.render("text </script><b>x</b> [l](https://x.org/<b>)")
    assert "</script>" not in html and "<b>" not in html


def test_record_reason_and_error_cannot_inject_markup(tmp_path):
    evil = '</script><img src=x onerror=alert(1)> "quoted" <b>'
    _, out = build(tmp_path, [
        rec(status="error", value=None, error=evil),
        rec(item="q02", status="n/a", value=None, reason=evil),
        rec(system="alisql", item="q01", status="timeout", value=None, error=evil)])
    for page in ("index.html", "track_a.html", "systems/percona.html", "systems/alisql.html"):
        html = (out / page).read_text(encoding="utf-8")
        assert "<img src=x" not in html, page
        assert html.count("</script>") == html.count("<script"), page
    raw = (out / "index.html").read_text(encoding="utf-8")
    blob = re.search(r'id="site-data">(.*?)</script>', raw, re.S).group(1)
    assert "<" not in blob
    assert json.loads(blob)["power"]["10"]["cells"]["percona"]["q01"]["r"] == evil
    assert "<b>" not in (out / "systems" / "percona.html").read_text(encoding="utf-8")


# ---- run selection ----

def test_largest_run_wins_and_others_are_footnoted(tmp_path):
    big = [rec(item=f"q{n:02d}", value=1.0, run="runA", version="1.5.3") for n in range(1, 6)]
    small = [rec(item="q01", value=99.0, run="runB", started="2026-12-01T00:00:00+00:00",
                 version="1.5.9")]
    _, out = build(tmp_path, big + small)
    power = data(out)["power"]["10"]
    assert power["totals"]["percona"]["used"] == 5
    assert power["cells"]["percona"]["q01"]["v"] == 1.0
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "also present, not shown" in html and "runB (1 records)" in html
    assert data(out)["run_ids"] == ["runA"]


def test_newest_run_wins_a_tie(tmp_path):
    a = rec(run="runA", value=1.0)
    b = rec(run="runB", value=2.0, started="2026-12-01T00:00:00+00:00")
    _, out = build(tmp_path, [a, b])
    assert data(out)["power"]["10"]["cells"]["percona"]["q01"]["v"] == 2.0


def test_runs_are_chosen_per_system_and_scale_factor(tmp_path):
    recs = [rec(system="percona", run="runA"), rec(system="alisql", run="runB"),
            rec(system="percona", run="runC", sf=1)]
    _, out = build(tmp_path, recs)
    assert set(data(out)["power"]["10"]["cells"]) == {"percona", "alisql"}
    assert "not shown" not in (out / "index.html").read_text(encoding="utf-8")


# ---- common set bookkeeping ----

def cells(*specs):
    out = {}
    for system, per in specs:
        out[system] = {q: {"s": s, "v": 1.0 if s == "ok" else None, "r": None, "mark": None}
                       for q, s in per.items()}
    return out


def test_member_without_m1_records_is_listed_with_a_reason():
    c = cells(("a", {"q01": "ok", "q02": "ok"}))
    common, left = aggregate.common_queries(c, ["q01", "q02"], {"a", "b"})
    assert common == ["q01", "q02"]
    assert left == [{"system": "b", "reason": "no M1 records at this scale factor"}]


def test_integration_failing_everything_is_left_out_not_emptying_the_set():
    c = cells(("a", {"q01": "ok", "q02": "ok"}), ("b", {"q01": "error", "q02": "timeout"}))
    common, left = aggregate.common_queries(c, ["q01", "q02"], {"a", "b"})
    assert common == ["q01", "q02"]
    assert [x["system"] for x in left] == ["b"] and "no ok query" in left[0]["reason"]


def test_baseline_and_track_b_do_not_reach_the_track_a_common_set(tmp_path):
    recs = [rec(system="percona", item=q, run="a") for q in ("q01", "q02")]
    recs += [rec(system="innodb", track="baseline", item="q01", run="a", version=None),
             rec(system="innodb", track="baseline", item="q02", status="timeout", value=None,
                 run="a", error="t", version=None),
             rec(system="villagesql", track="B", item="q01", run="b")]
    _, out = build(tmp_path, recs)
    track_a = data(out)
    assert track_a["power"]["10"]["common_queries"] == ["q01", "q02"]
    assert "villagesql" not in json.dumps(track_a)


def test_left_out_systems_are_shown_on_the_overview(tmp_path):
    recs = [rec(system="percona", run="a"),
            rec(system="alisql", item="q01", status="error", value=None, error="boom", run="b")]
    _, out = build(tmp_path, recs)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert "Left out of the common query set" in html and "AliSQL" in html


# ---- vendored ECharts ----

def test_echarts_is_vendored_and_pages_load_no_cdn(tmp_path):
    root = tmp_path / "r"
    write_synthetic(root)
    out = tmp_path / "o"
    build_site(root, out)
    static = out / "static"
    assert (static / "echarts.min.js").stat().st_size > 500_000
    assert "Apache License" in (static / "LICENSE-echarts.txt").read_text(encoding="utf-8")
    for page in ("index.html", "track_a.html", "track_b.html"):
        html = (out / page).read_text(encoding="utf-8")
        assert 'src="static/echarts.min.js"' in html
        assert not re.search(r'<script[^>]+src="https?:', html)


# ---- workflow and guard ----

def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_triggers_and_published_source():
    wf = workflow()
    on = wf.get("on", wf.get(True))
    assert on["push"]["branches"] == ["main"]
    assert set(on["push"]["paths"]) >= {"published/**", "sitegen/**",
                                        ".github/workflows/pages.yml"}
    assert "workflow_dispatch" in on
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--results published" in text and "--results results" not in text


def test_workflow_tests_before_build_and_guards():
    steps = workflow()["jobs"]["build"]["steps"]
    runs = [s.get("run", "") for s in steps]
    idx = {k: next(i for i, r in enumerate(runs) if k in r)
           for k in ("pytest", "sitegen.guard --published", "sitegen.build",
                     "sitegen.guard --site")}
    assert idx["pytest"] < idx["sitegen.build"]
    assert idx["sitegen.guard --published"] < idx["sitegen.build"] < idx["sitegen.guard --site"]


def test_workflow_permissions_pins_and_concurrency():
    wf = workflow()
    assert "pages" not in (wf.get("permissions") or {})
    assert wf["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert not (wf["jobs"]["build"].get("permissions") or {}).get("pages")
    assert wf["concurrency"]["cancel-in-progress"] is False
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        m = re.search(r"uses:\s*(\S+)", line)
        if m:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", m.group(1)), line
            assert re.search(r"#\s*v\d", line), line


def test_guard_rejects_missing_or_empty_published(tmp_path):
    assert guard.check_published(tmp_path / "nope")
    (tmp_path / "pub").mkdir()
    (tmp_path / "pub" / "README.md").write_text("x")
    assert guard.check_published(tmp_path / "pub")
    write(rec(), tmp_path / "pub")
    assert guard.check_published(tmp_path / "pub") == []


def test_guard_rejects_synthetic_site(tmp_path):
    root = tmp_path / "r"
    write_synthetic(root)
    out = tmp_path / "o"
    build_site(root, out)
    assert guard.check_site(out)
    real = tmp_path / "real"
    write(rec(), real)
    out2 = tmp_path / "o2"
    build_site(real, out2)
    assert guard.check_site(out2) == []


def test_guard_cli_exit_codes(tmp_path):
    assert guard.main(["--published", str(tmp_path / "missing")]) == 1
    write(rec(), tmp_path / "p")
    assert guard.main(["--published", str(tmp_path / "p")]) == 0


def test_published_dir_is_documented():
    assert (REPO / "published").is_dir()
    assert "published/" in (REPO / "README.md").read_text(encoding="utf-8")


