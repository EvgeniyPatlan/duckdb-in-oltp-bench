"""Measure id -> runner. Every runner has the signature ``run(adapter, ctx) -> list[ResultRecord]``.

A campaign may only name registered measures (checked when its YAML is loaded). Later tasks
register M2..M10 here. Track B is not a per-adapter measure; the campaign calls
``run_track_b`` from its ``track_b`` section.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dbench import dockerctl, hostinfo
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.datagen import DatasetPaths
from dbench.measures.power import QUERY_NUMBERS, run_power
from dbench.results import ResultRecord
from dbench.systems.base import SystemAdapter


@dataclass
class MeasureContext:
    """What a measure needs besides the adapter. ``items`` are the cells still to run
    (already-recorded cells of a resumed campaign are left out)."""

    sf: int
    run_id: str
    results: Path
    data: DatasetPaths
    reference: dict[int, dict] | None
    timeout_s: int = QUERY_TIMEOUT_S
    limits: Limits = field(default_factory=Limits)
    items: list[str] = field(default_factory=list)
    drop_caches: Callable[[], None] = dockerctl.drop_caches
    host: Callable[[], dict] = hostinfo.host_info


Runner = Callable[[SystemAdapter, MeasureContext], list[ResultRecord]]


@dataclass(frozen=True)
class Measure:
    id: str
    unit: str
    items: Callable[[int], list[str]]  # planned items at a scale factor
    run: Runner
    needs_reference: bool = False


_MEASURES: dict[str, Measure] = {}


def register(measure: Measure) -> None:
    _MEASURES[measure.id] = measure


def registered() -> list[str]:
    return sorted(_MEASURES, key=lambda m: int(m[1:]))


def get(measure_id: str) -> Measure:
    try:
        return _MEASURES[measure_id]
    except KeyError:
        raise KeyError(
            f"measure {measure_id!r} is not registered; registered: {registered()}") from None


def _power_items(sf: int) -> list[str]:
    return [f"q{n:02d}" for n in QUERY_NUMBERS]


def _run_power(adapter: SystemAdapter, ctx: MeasureContext) -> list[ResultRecord]:
    return run_power(
        adapter, ctx.sf, ctx.run_id, ctx.results, ctx.reference,
        queries=[int(item[1:]) for item in ctx.items],
        drop_caches=ctx.drop_caches, host=ctx.host, timeout_s=ctx.timeout_s)


register(Measure("M1", "s", _power_items, _run_power, needs_reference=True))
