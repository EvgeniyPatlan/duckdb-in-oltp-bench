"""Static site: pages, data files, neutrality rules, methodology completeness."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dbench.results import STATUSES, iter_records
from sitegen.build import build_site
from sitegen.model import palette_for
from tests.fixtures.synthetic import write_synthetic

REPO = Path(__file__).resolve().parents[1]
PAGES = ["index.html", "track_a.html", "track_b.html", "methodology.html"]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    root = tmp_path_factory.mktemp("results")
    out = tmp_path_factory.mktemp("site")
    write_synthetic(root)
    build_site(root, out)
    return root, out


def data(out: Path, name: str) -> dict:
    return json.loads((out / "data" / f"{name}.json").read_text(encoding="utf-8"))


def text(out: Path, page: str) -> str:
    return (out / page).read_text(encoding="utf-8")


def test_fixture_covers_every_status(built):
    root, _ = built
    assert {r.status for r in iter_records(root)} == set(STATUSES)


def test_build_with_fixture_results(built):
    _, out = built
    for page in PAGES:
        assert (out / page).is_file(), page
    for system_dir in ("alisql", "mariadb", "percona", "pgduckdb", "pgducklake", "duckdb",
                       "innodb", "postgres", "villagesql"):
        assert (out / "systems" / f"{system_dir}.html").is_file(), system_dir
    assert (out / "static" / "charts.js").is_file() and (out / "static" / "style.css").is_file()
    track_a = data(out, "track_a")
    names = {s["name"] for s in track_a["systems"]}
    assert "villagesql" not in names
    assert not any(n.endswith("-trackb") for n in names)
    assert "villagesql" not in json.dumps(track_a)
    assert "villagesql" in json.dumps(data(out, "track_b"))


def test_wrong_result_flagged_in_data(built):
    _, out = built
    cell = data(out, "track_a")["power"]["10"]["cells"]["mariadb"]["q15"]
    assert cell["s"] == "wrong_result"
    assert cell["mark"] == "failure"


def test_na_reason_present(built):
    _, out = built
    track_a = data(out, "track_a")
    na = track_a["load"]["10"]["pg_duckdb"]["load_convert"]
    assert na["s"] == "n/a" and "heap" in na["r"]
    failed = track_a["throughput"]["10"]["pg_ducklake"]["32"]
    assert failed["s"] == "failed" and failed["r"]
    timeout = track_a["power"]["10"]["cells"]["pg_duckdb"]["q09"]
    assert timeout["s"] == "timeout" and timeout["r"]


def test_methodology_lists_every_nondefault_setting(built):
    _, out = built
    html = text(out, "methodology.html")
    keys = set()
    for path in (REPO / "systems").glob("*/*"):
        if path.name in ("my.cnf", "duckdb.cnf", "trackb.cnf", "postgresql.conf"):
            for line in path.read_text(encoding="utf-8").splitlines():
                m = re.match(r"^([A-Za-z][\w.-]*)\s*(=|$)", line)
                if m:
                    keys.add(m.group(1))
        elif path.name == "mysqld-args.j2":
            keys |= set(re.findall(r"^--([\w-]+)", path.read_text(encoding="utf-8"), re.M))
        elif path.name == "settings.sql":
            keys |= set(re.findall(r"SET PERSIST ([\w.]+)", path.read_text(encoding="utf-8")))
    assert len(keys) > 40
    missing = sorted(k for k in keys if k not in html)
    assert not missing


def test_methodology_has_limits_tolerances_dialects_rerun(built):
    _, out = built
    html = text(out, "methodology.html")
    for needle in ("900", "rel_tol", "of 22 queries change", "scripts/dev.sh dbench campaign",
                   "kernel", "geomean_common"):
        assert needle in html, needle


def test_synthetic_banner_only_for_fixture_only_roots(built, tmp_path):
    _, out = built
    assert "SYNTHETIC" in text(out, "index.html")
    assert data(out, "track_a")["synthetic"] is True
    empty = tmp_path / "empty"
    build_site(tmp_path / "nothing", empty)
    assert "SYNTHETIC" not in text(empty, "index.html")
    assert "No results" in text(empty, "index.html")


def test_synthetic_records_dropped_when_real_exist(tmp_path):
    from dbench.results import ResultRecord, write
    from tests.fixtures.synthetic import STAMP
    root = tmp_path / "r"
    write_synthetic(root)
    write(ResultRecord(system="percona", track="A", measure="M1", sf=10, item="q01",
                       status="ok", value=1.0, unit="s", config_ref="systems/percona/",
                       run_id="real1", started_at=STAMP, versions={"duckdb": "1.5.3"}), root)
    out = tmp_path / "o"
    build_site(root, out)
    track_a = data(out, "track_a")
    assert track_a["synthetic"] is False
    assert list(track_a["power"]["10"]["cells"]) == ["percona"]


def test_overhead_headline_shows_common_with_own_geomean(built):
    _, out = built
    cell = data(out, "track_a")["overhead"]["10"]["percona"]
    assert cell["headline"]["item"] == "geomean_common"
    assert cell["own"]["used"] <= cell["own"]["total"]
    assert cell["headline"]["v"] is not None and cell["own"]["v"] is not None
    assert cell["own"]["partial"] in (True, False)


def test_palette_is_alphabetical_and_stable():
    colors = palette_for(["percona", "alisql", "mariadb"])
    assert colors["alisql"] != colors["mariadb"] != colors["percona"]
    assert palette_for(["alisql"])["alisql"] == colors["alisql"]
    assert palette_for(["mariadb-trackb"])["mariadb-trackb"] == colors["mariadb"]


def test_no_system_is_marked_as_ours(built):
    _, out = built
    blob = "".join(p.read_text(encoding="utf-8") for p in out.rglob("*.html")).lower()
    blob += (out / "static" / "charts.js").read_text(encoding="utf-8").lower()
    for word in (r"\bours\b", r"\bour engine\b", r"\bwinner\b"):
        assert not re.search(word, blob)


def test_charts_js_has_exports_and_cdn(built):
    _, out = built
    js = (out / "static" / "charts.js").read_text(encoding="utf-8")
    assert "saveAsImage" in js and "svg" in js
    html = text(out, "track_a.html")
    assert "cdnjs.cloudflare.com/ajax/libs/echarts/5.5.0/echarts.min.js" in html


def test_track_b_page_explains_separate_track(built):
    _, out = built
    html = text(out, "track_b.html")
    assert "separate track" in html.lower() and "duckdb_query" in html


def test_system_pages_use_readme_and_measured_facts(built):
    _, out = built
    html = text(out, "systems/percona.html")
    assert "DUCKSDB_MEMORY_LIMIT" in html and "Observed in this run" in html


def test_system_labels_follow_one_rule():
    """Short labels name who provides the integration, never a product line or engine jargon;
    the full product with versions comes from the lock file on each system page."""
    from sitegen.methodology import DIR_LABELS
    from sitegen.model import LABELS, config_dir

    assert {k: LABELS[k] for k in ("alisql", "mariadb", "percona", "pg_duckdb", "pg_ducklake",
                                   "villagesql", "innodb", "postgres")} == {
        "alisql": "AliSQL", "mariadb": "MariaDB", "percona": "Percona",
        "pg_duckdb": "pg_duckdb", "pg_ducklake": "pg_ducklake", "villagesql": "VillageSQL",
        "innodb": "MySQL InnoDB", "postgres": "PostgreSQL"}
    for name, label in LABELS.items():
        if not name.endswith("-trackb"):
            assert DIR_LABELS[config_dir(name)] == label, name


def test_lock_names_the_server_each_image_runs():
    lock = json.loads((Path(__file__).parents[1] / "systems" / "versions.lock.json").read_text())
    assert lock["percona"]["server_version"].startswith("MySQL 9.7")
    assert "Percona Server" not in json.dumps(lock)
