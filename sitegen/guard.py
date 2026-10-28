"""Publication guards for the Pages workflow.

``python -m sitegen.guard --published published`` fails when the curated results directory is
missing. With no records in it the site is a preview: methodology and system pages, no numbers.
``--site _site`` fails when the built site is marked synthetic (fixture data must never be
deployed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def check_published(published: Path) -> list[str]:
    published = Path(published)
    if not published.is_dir():
        return [f"{published}/ is missing: nothing has been published"]
    return []  # no records yet is fine: the site is then a preview without numbers


def check_site(site: Path) -> list[str]:
    problems = []
    files = sorted((Path(site) / "data").glob("*.json"))
    if not files:
        return [f"{site}/data has no data files"]
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("synthetic"):
            problems.append(f"{path.name} is marked synthetic")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sitegen.guard")
    ap.add_argument("--published", type=Path)
    ap.add_argument("--site", type=Path)
    args = ap.parse_args(argv)
    problems = []
    if args.published:
        problems += check_published(args.published)
    if args.site:
        problems += check_site(args.site)
    for p in problems:
        print(f"guard: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
