"""Build the static results site: ``python -m sitegen.build --results <dir> --out <dir>``.

The site root shows every published result (one run per system, track and scale factor). Each
campaign is also built on its own under ``reports/<run id>/`` with the same pages, so one report
never mixes numbers from another campaign. ``reports.html`` lists the reports and compares them.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from sitegen import findings, md, methodology, pages, reports
from sitegen.model import Loaded, load

HERE = Path(__file__).resolve().parent
PAGES = {"index.html": "index.html.j2", "track_a.html": "track_a.html.j2",
         "track_b.html": "track_b.html.j2", "explore.html": "explore.html.j2",
         "methodology.html": "methodology.html.j2", "reports.html": "reports.html.j2"}
REPORT_PAGES = ("index.html", "track_a.html", "track_b.html", "explore.html")


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True,
                       undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _default_sf(data: dict) -> str | None:
    sfs = [str(s) for s in data["sfs"] if str(s) in data["power"]]
    return str(max(map(int, sfs))) if sfs else None


def _left_out(data: dict, sf: str | None) -> list[dict]:
    """Systems outside the common query set at ``sf``, with their reasons, for the page note."""
    labels = {s["name"]: s["label"] for s in data["systems"]}
    entries = data["power"].get(sf, {}).get("systems_left_out", []) if sf else []
    return [{"label": labels.get(e["system"], e["system"]), "reason": e["reason"]}
            for e in entries]


def _findings(ta: dict, tb: dict) -> list[dict]:
    """Findings for every scale factor the page can show, largest first: Track A's, or Track B's
    when the page has no Track A results (the overview then has no scale factor selector)."""
    sfs = sorted({str(s) for s in (ta["sfs"] or tb["sfs"])}, key=int, reverse=True)
    return [{"sf": sf, "items": findings.for_report(ta, tb, sf)} for sf in sfs]


def _report_entry(r: reports.Report | None, ta: dict) -> dict:
    return {"id": r.id if r else None, "url": r.slug if r else "", "sfs": [str(s) for s in ta["sfs"]],
            "started": (r.started or "")[:10] if r else None,
            "finished": (r.finished or "")[:10] if r else None}


def _summary(ta: dict) -> dict:
    """Per scale factor and system: the numbers the reports page compares across reports."""
    out: dict[str, dict] = {}
    for sf in ta["power"]:
        power, over = ta["power"][sf], ta.get("overhead", {}).get(sf, {})
        thr, sc = ta.get("throughput", {}).get(sf, {}), ta.get("sqlcompat", {}).get(sf, {})
        per = {}
        for s in ta["systems"]:
            n, tot = s["name"], power["totals"].get(s["name"])
            common = tot["common"] if tot else {}
            per[n] = {
                "common_total": common.get("v") if common.get("s") == "ok" else None,
                "n_common": len(power["common_queries"]),
                "own_total": tot["own_s"] if tot else None,
                "ok": tot["used"] if tot else None, "queries": tot["total"] if tot else None,
                "overhead": (over.get(n, {}).get("headline") or {}).get("v"),
                "qph16": ((thr.get(n) or {}).get("16") or {}).get("v"),
                "probe_errors": (sc.get("counts", {}).get(n) or {}).get("error"),
                "probe_wrong": (sc.get("counts", {}).get(n) or {}).get("wrong_result")}
        out[sf] = per
    return out


class Site:
    def __init__(self, loaded: Loaded, out: Path, nav_systems: list[dict], all_reports: list[dict]):
        self.loaded, self.out, self.env = loaded, out, _env()
        self.base = {"synthetic": loaded.synthetic, "empty": not loaded.records,
                     "nav_systems": nav_systems, "reports": all_reports}

    def render(self, name: str, target: str, depth: int, report: dict, excluded: list,
               run_ids: list, **ctx) -> None:
        root = "../" * depth
        html = self.env.get_template(name).render(
            root=root, here=root if name in ("methodology.html.j2", "system.html.j2",
                                             "reports.html.j2") else "",
            report=report, excluded_runs=excluded, run_ids=run_ids, **self.base, **ctx)
        _write(self.out / target, html)

    def report_pages(self, loaded: Loaded, prefix: str, depth: int,
                     r: reports.Report | None) -> dict:
        ta, tb = pages.track_a(loaded), pages.track_b(loaded)
        entry = _report_entry(r, ta)
        for name, payload in (("track_a", ta), ("track_b", tb)):
            _write(self.out / prefix / "data" / f"{name}.json", json.dumps(payload, sort_keys=True))
        sf = _default_sf(ta)
        common = {"depth": depth, "report": entry, "excluded": loaded.excluded,
                  "run_ids": loaded.run_ids}
        notes = md.render(r.notes_md, base_level=3) if r and r.notes_md else None
        self.render(PAGES["index.html"], prefix + "index.html", data_json=pages.dumps(ta),
                    page="index", rows=pages.headline_rows(ta, sf), default_sf=sf,
                    left_out=_left_out(ta, sf), systems=ta["systems"],
                    findings=_findings(ta, tb), notes=notes, **common)
        self.render(PAGES["track_a.html"], prefix + "track_a.html", data_json=pages.dumps(ta),
                    page="track_a", default_sf=sf, **common)
        self.render(PAGES["track_b.html"], prefix + "track_b.html", data_json=pages.dumps(tb),
                    page="track_b", default_sf=_default_sf(tb), systems=tb["systems"],
                    left_out=_left_out(tb, _default_sf(tb)), **common)
        self.render(PAGES["explore.html"], prefix + "explore.html", data_json=pages.dumps(ta),
                    page="explore", default_sf=sf, **common)
        return {**entry, "summary": _summary(ta), "systems": ta["systems"]}


def build_site(results_root: Path, out: Path) -> None:
    loaded = load(Path(results_root))
    out = Path(out)
    sys_pages = pages.system_pages(loaded)
    nav = [{"slug": p["slug"], "label": p["label"]} for p in sys_pages]
    split = reports.split(loaded, Path(results_root)) if loaded.records else []
    listing = [{"id": r.id, "url": r.slug, "sfs": [str(s) for s in r.sfs],
                "finished": (r.finished or "")[:10]} for r in split]
    site = Site(loaded, out, nav, listing)
    combined = site.report_pages(loaded, "", 0, None)
    built = [site.report_pages(r.loaded, r.slug, 2, r) for r in split]
    none = {"depth": 0, "report": _report_entry(None, {"sfs": []}), "excluded": loaded.excluded,
            "run_ids": loaded.run_ids}
    site.render(PAGES["methodology.html"], "methodology.html", page="methodology",
                m=methodology.context(loaded.records), **none)
    site.render(PAGES["reports.html"], "reports.html", page="reports",
                data_json=pages.dumps({"synthetic": loaded.synthetic, "combined": combined,
                                       "reports": built}), **none)
    for p in sys_pages:
        site.render("system.html.j2", f"systems/{p['slug']}", page="system", p=p,
                    **{**none, "depth": 1})
    shutil.copytree(HERE / "static", out / "static", dirs_exist_ok=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="sitegen.build")
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    build_site(args.results, args.out)


if __name__ == "__main__":
    main()
