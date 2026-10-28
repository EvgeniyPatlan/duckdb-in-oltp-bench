"""What the site says about a cell that is not ok: a wrong result names how it differs, and the
reason of an ``n/a`` (such as the M5 server control) is shown, escaped like any other text."""
from __future__ import annotations

from dbench.results import ResultRecord
from sitegen import aggregate, pages
from tests.test_site_review import build, data, rec


def m5(item="fn_concat", status="wrong_result", reason=None, error=None, **extra):
    return ResultRecord(
        system="mariadb", track="A", measure="M5", sf=10, item=item, status=status, value=None,
        unit="class", config_ref="systems/mariadb/", run_id="real1",
        started_at="2026-10-01T00:00:00+00:00", reason=reason, error=error,
        extra={"category": "functions", "title": "t", **extra})


def test_wrong_result_message_counts_the_differences():
    r = m5(diffs=[[0, "(1,)", "(2,)"], [3, "(4,)", "None"]], row_count=[5, 4],
           **{"class": "wrong_result"})
    msg = aggregate.message(r)
    assert msg == "result differs from the reference: 2 rows differ (5 expected, 4 returned)"
    assert aggregate.cell(r)["r"] == msg


def test_wrong_result_message_with_capped_diffs_and_one_row():
    capped = m5(diffs=[[i, "a", "b"] for i in range(20)], row_count=[30, 30])
    assert "at least 20 rows differ" in aggregate.message(capped)
    one = m5(diffs=[[0, "a", "b"]], row_count=[1, 1])
    assert "1 row differs" in aggregate.message(one)
    bare = m5()
    assert aggregate.message(bare) == "result differs from the reference"


def test_reason_and_error_come_first():
    assert aggregate.message(m5(status="n/a", reason="because")) == "because"
    assert aggregate.message(m5(status="error", error="boom")) == "boom"
    ok = m5(status="ok")
    assert aggregate.message(ok) is None


def test_system_facts_list_wrong_results_and_control_reasons():
    reason = ("not supported by the mariadb server itself (same failure on a row-store table): "
              "(1305, 'FUNCTION tpch.REGEXP_LIKE does not exist')")
    facts = pages._facts([m5(diffs=[[0, "a", "b"]], row_count=[1, 1]),
                          m5("fn_regexp", status="n/a", reason=reason)])
    assert any("fn_concat (wrong_result): result differs from the reference" in r
               for r in facts["reasons"])
    assert any(reason in r for r in facts["reasons"])


def test_m5_cells_carry_the_message_and_stay_escaped(tmp_path):
    evil = '</script><img src=x onerror=alert(1)>'
    _, out = build(tmp_path, [
        rec(system="mariadb"),
        m5(diffs=[[0, evil, evil]], row_count=[1, 1], **{"class": "wrong_result"}),
        m5("fn_regexp", status="n/a", reason=f"not supported by the server itself: {evil}",
           **{"class": "n/a"})])
    cells = data(out)["sqlcompat"]["10"]["cells"]["mariadb"]
    assert cells["fn_concat"]["r"].startswith("result differs from the reference")
    assert cells["fn_regexp"]["r"].endswith(evil)
    for page in ("track_a.html", "systems/mariadb.html", "index.html"):
        html = (out / page).read_text(encoding="utf-8")
        assert "<img src=x" not in html, page
        assert html.count("</script>") == html.count("<script"), page
    system = (out / "systems" / "mariadb.html").read_text(encoding="utf-8")
    assert "result differs from the reference" in system
