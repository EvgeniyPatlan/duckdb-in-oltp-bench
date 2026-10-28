"""Human-readable campaign progress: one line per recorded cell, plus system start and end.

Lines go to the campaign's ``out`` (stdout by default) and look like::

    14:02:11 [innodb] start: M1 22, M5 120 to run; 3 already recorded
    14:02:11 [innodb] loading SF10
    14:51:40 [innodb] M1 5/22 q05: ok 12.3 s (match)
    16:10:02 [innodb] done in 2h07m: ok 141, n/a 1
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime

from dbench.results import ResultRecord

REASON_CHARS = 100


def _elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def _value(rec: ResultRecord) -> str:
    if rec.value is None:
        return ""
    if rec.unit == "s":
        return f" {rec.value:.1f} s"
    return f" {rec.value:g} {rec.unit}"


def _note(rec: ResultRecord) -> str:
    if rec.extra.get("compared"):
        return " (match)" if rec.status == "ok" else " (MISMATCH)"
    if rec.status == "ok":
        return ""
    text = (rec.reason or rec.error or "").strip().splitlines()
    return f" ({text[0][:REASON_CHARS]})" if text else ""


class Progress:
    """Counts cells per system and measure; ``record`` is meant for ``results.observe``."""

    def __init__(self, out: Callable[[str], None], clock: Callable[[], datetime]):
        self.out, self.clock = out, clock
        self._totals: dict[str, dict[str, int]] = {}
        self._done: dict[tuple[str, str], int] = {}
        self._statuses: dict[str, Counter] = {}
        self._started: dict[str, datetime] = {}

    def _say(self, system: str, msg: str) -> None:
        self.out(f"{self.clock():%H:%M:%S} [{system}] {msg}")

    def system_start(self, system: str, pending: dict[str, int], skipped: int) -> None:
        self._totals[system] = dict(pending)
        self._statuses[system] = Counter()
        self._started[system] = self.clock()
        todo = ", ".join(f"{m} {n}" for m, n in pending.items()) or "nothing"
        self._say(system, f"start: {todo} to run; {skipped} already recorded")

    def loading(self, system: str, sf: int) -> None:
        self._say(system, f"loading SF{sf}")

    def record(self, rec: ResultRecord) -> None:
        key = (rec.system, rec.measure)
        self._done[key] = self._done.get(key, 0) + 1
        self._statuses.setdefault(rec.system, Counter())[rec.status] += 1
        total = self._totals.get(rec.system, {}).get(rec.measure)
        count = f" {self._done[key]}/{total}" if total else ""
        self._say(rec.system,
                  f"{rec.measure}{count} {rec.item}: {rec.status}{_value(rec)}{_note(rec)}")

    def system_end(self, system: str) -> None:
        started = self._started.get(system, self.clock())
        took = _elapsed((self.clock() - started).total_seconds())
        counts = ", ".join(f"{s} {n}" for s, n in sorted(self._statuses.get(system, {}).items()))
        self._say(system, f"done in {took}: {counts or 'nothing recorded'}")
