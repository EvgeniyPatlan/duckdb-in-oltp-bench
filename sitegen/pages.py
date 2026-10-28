"""Data for the pages: the two track data sets, the headline table and the system pages."""
from __future__ import annotations

import json
from pathlib import Path

from dbench.results import ResultRecord
from sitegen import aggregate, md, settings
from sitegen.methodology import DIR_LABELS, REPO
from sitegen.model import Loaded, config_dir, label_of, system_entries

GLYPHS = {"ok": "", "wrong_result": "✗", "timeout": "T/O", "error": "E", "crashed": "C",
          "failed": "!", "n/a": "–", "incomplete": "–"}
HEADLINE_STREAMS = "16"


def _meta(loaded: Loaded, systems: list[dict], records: list[ResultRecord]) -> dict:
    return {"synthetic": loaded.synthetic, "run_ids": sorted({r.run_id for r in records}),
            "systems": systems, "sfs": sorted({r.sf for r in records}),
            "glyphs": GLYPHS, "generated": max((r.started_at for r in records), default=None)}


def track_a(loaded: Loaded) -> dict:
    records = [r for r in loaded.records if r.track in ("A", "baseline")]
    systems = system_entries(records)
    engines = {s["name"] for s in systems if s["kind"] == "integration"}
    engine_recs = [r for r in records if r.system in engines]
    return {**_meta(loaded, systems, records),
            "power": aggregate.power(records, engines),
            "load": aggregate.load(records), "storage": aggregate.storage(records),
            "throughput": aggregate.throughput(records),
            "sqlcompat": aggregate.sqlcompat(engine_recs),
            "fallback": aggregate.fallback(engine_recs),
            "overhead": aggregate.overhead(engine_recs)}


def track_b(loaded: Loaded) -> dict:
    records = [r for r in loaded.records if r.track == "B"]
    systems = system_entries(records)
    return {**_meta(loaded, systems, records),
            "power": aggregate.power(records, {s["name"] for s in systems})}


def dumps(data: dict) -> str:
    """JSON safe to inline in a script tag."""
    return json.dumps(data, sort_keys=True, ensure_ascii=False).replace("<", "\\u003c")


def fmt_cell(c: dict | None, text: str) -> dict:
    """A table cell: ``text`` for an ok value, the status glyph (reason on hover) otherwise."""
    if c is None:
        return {"text": GLYPHS["n/a"], "title": "no record", "mark": "na"}
    if c.get("mark"):
        return {"text": GLYPHS.get(c["s"], c["s"]), "title": f"{c['s']}: {c.get('r') or ''}",
                "mark": c["mark"]}
    return {"text": text, "title": "", "mark": None}


def _num(v: float | None, unit: str = "") -> str:
    return "" if v is None else f"{v:,.2f}{unit}" if v < 100 else f"{v:,.0f}{unit}"


def headline_rows(data: dict, sf: str | None) -> list[dict]:
    """Per system: power total (common queries) with its own total and coverage, throughput at
    16 streams and the overhead geomean on common queries with the system's own beside it."""
    if sf is None:
        return []
    power = data["power"].get(sf, {})
    thr = data["throughput"].get(sf, {})
    over = data["overhead"].get(sf, {})
    rows = []
    for s in data["systems"]:
        n = s["name"]
        tot = power.get("totals", {}).get(n)
        head, own = over.get(n, {}).get("headline"), over.get(n, {}).get("own")
        rows.append({
            "system": s, "total": fmt_cell(tot["common"] if tot else None,
                                           _num(tot["common"]["v"], " s") if tot else ""),
            "own": f"{_num(tot['own_s'], ' s')} ({tot['used']}/{tot['total']})" if tot
            and tot["own_s"] is not None else "",
            "qph": fmt_cell(thr.get(n, {}).get(HEADLINE_STREAMS),
                            _num((thr.get(n, {}).get(HEADLINE_STREAMS) or {}).get("v"))),
            "over": fmt_cell(head, f"{head['v']:.2f}x" if head and head["v"] else ""),
            "over_own": (f"{own['v']:.2f}x ({own['used']}/{own['total']}"
                         f"{', partial' if own['partial'] else ''})" if own and own["v"] else ""),
            "plain": (head or {}).get("plain") or ""})
    return rows


def _facts(recs: list[ResultRecord]) -> dict:
    by_measure: dict[str, dict[str, int]] = {}
    for r in recs:
        tally = by_measure.setdefault(r.measure, {})
        tally[r.status] = tally.get(r.status, 0) + 1
    reasons = sorted({f"{r.measure} {r.item} ({r.status}): {r.reason or r.error}"
                      for r in recs if r.status not in ("ok",) and (r.reason or r.error)})
    return {"counts": by_measure, "reasons": reasons[:12], "more": max(0, len(reasons) - 12)}


def system_pages(loaded: Loaded) -> list[dict]:
    """One entry per config directory under ``systems/`` that has a README."""
    lock = json.loads((REPO / "systems" / "versions.lock.json").read_text(encoding="utf-8"))
    names: dict[str, set[str]] = {}
    for n in [*lock, *(r.system for r in loaded.records)]:
        names.setdefault(config_dir(n), set()).add(n)
    pages = []
    for d in sorted(p.name for p in (REPO / "systems").iterdir() if (p / "README.md").is_file()):
        members = sorted(names.get(d, set()))
        recs = [r for r in loaded.records if config_dir(r.system) == d]
        pages.append({
            "dir": d, "label": DIR_LABELS.get(d, d), "slug": f"{d}.html",
            "readme": md.render((REPO / "systems" / d / "README.md").read_text(encoding="utf-8")),
            "settings": settings.for_system(d),
            "lock": [{"name": n, "label": label_of(n), **{k: lock[n].get(k) for k in (
                "image", "server_version", "duckdb_version", "digest")}}
                for n in members if n in lock],
            "facts": [{"system": label_of(n), **_facts([r for r in recs if r.system == n])}
                      for n in members if any(r.system == n for r in recs)]})
    return pages


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
