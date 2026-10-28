"""Build the static results site: ``python -m sitegen.build --results <dir> --out <dir>``."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from sitegen import methodology, pages
from sitegen.model import load

HERE = Path(__file__).resolve().parent
PAGES = {"index.html": "index.html.j2", "track_a.html": "track_a.html.j2",
         "track_b.html": "track_b.html.j2", "methodology.html": "methodology.html.j2"}


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


def build_site(results_root: Path, out: Path) -> None:
    loaded = load(Path(results_root))
    out = Path(out)
    ta, tb = pages.track_a(loaded), pages.track_b(loaded)
    sys_pages = pages.system_pages(loaded)
    base = {"synthetic": loaded.synthetic, "empty": not loaded.records,
            "excluded_runs": loaded.excluded,
            "nav_systems": [{"slug": p["slug"], "label": p["label"]} for p in sys_pages],
            "run_ids": loaded.run_ids}
    env = _env()

    def render(name: str, target: str, depth: int = 0, **ctx) -> None:
        html = env.get_template(name).render(root="../" * depth, **base, **ctx)
        _write(out / target, html)

    for name, payload in (("track_a", ta), ("track_b", tb)):
        _write(out / "data" / f"{name}.json", json.dumps(payload, sort_keys=True))
    sf = _default_sf(ta)
    render(PAGES["index.html"], "index.html", data_json=pages.dumps(ta), page="index",
           rows=pages.headline_rows(ta, sf), default_sf=sf, left_out=_left_out(ta, sf),
           systems=ta["systems"])
    render(PAGES["track_a.html"], "track_a.html", data_json=pages.dumps(ta), page="track_a",
           default_sf=sf)
    render(PAGES["track_b.html"], "track_b.html", data_json=pages.dumps(tb), page="track_b",
           default_sf=_default_sf(tb), systems=tb["systems"],
           left_out=_left_out(tb, _default_sf(tb)))
    render(PAGES["methodology.html"], "methodology.html", page="methodology",
           m=methodology.context(loaded.records))
    for p in sys_pages:
        render("system.html.j2", f"systems/{p['slug']}", depth=1, page="system", p=p)
    shutil.copytree(HERE / "static", out / "static", dirs_exist_ok=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="sitegen.build")
    ap.add_argument("--results", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    build_site(args.results, args.out)


if __name__ == "__main__":
    main()
