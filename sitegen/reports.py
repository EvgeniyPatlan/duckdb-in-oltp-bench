"""Reports: the records of one campaign, Track A and its Track B run together.

A campaign writes Track A under ``run_id`` and Track B under ``run_id-trackb``; both belong to the
report ``run_id``. Each report gets its own pages, so numbers from different campaigns (other
versions, other scale factors, other hosts) are never mixed on one chart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sitegen.model import TRACKB_SUFFIX, Loaded, pick_runs

NOTES_DIR = "notes"
# A run id becomes a directory and a URL; anything else is skipped rather than written.
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]*$")


def report_id(run_id: str) -> str:
    return run_id.removesuffix(TRACKB_SUFFIX)


@dataclass(frozen=True)
class Report:
    id: str
    loaded: Loaded
    started: str | None
    finished: str | None
    sfs: list[int]
    notes_md: str | None

    @property
    def slug(self) -> str:
        return f"reports/{self.id}/"


def notes_for(results_root: Path, rid: str) -> str | None:
    """The author's notes for a report: ``<results>/notes/<report id>.md``, if present."""
    path = Path(results_root) / NOTES_DIR / f"{rid}.md"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def split(loaded: Loaded, results_root: Path) -> list[Report]:
    """One report per campaign, newest first (by its last record)."""
    by_id: dict[str, list] = {}
    for r in loaded.records:
        by_id.setdefault(report_id(r.run_id), []).append(r)
    reports = []
    for rid, recs in by_id.items():
        if not SAFE_ID.match(rid) or ".." in rid:
            continue
        kept, excluded = pick_runs(recs)
        stamps = sorted(r.started_at for r in kept if r.started_at)
        reports.append(Report(
            id=rid,
            loaded=Loaded(kept, loaded.synthetic, sorted({r.run_id for r in kept}), excluded),
            started=stamps[0] if stamps else None, finished=stamps[-1] if stamps else None,
            sfs=sorted({r.sf for r in kept}), notes_md=notes_for(results_root, rid)))
    return sorted(reports, key=lambda r: (r.finished or "", r.id), reverse=True)
