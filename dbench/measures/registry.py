"""Measure id -> runner. Every runner has the signature ``run(adapter, ctx) -> list[ResultRecord]``.

A campaign may only name registered measures (checked when its YAML is loaded). Later tasks
register M5..M10 here.

``Measure.phase`` says when the campaign runs a measure on a system: ``query`` (after the bulk
load, M1), ``after_load`` (right after the bulk load, before any query: M3) or ``last`` (after
every other measure, on a fresh datadir: M2's ``load_convert``). M2's ``load_bulk`` cell is the
campaign's own bulk load, timed by ``dbench.measures.load.run_load_bulk``.

Track B is not a per-adapter measure; the campaign calls ``run_track_b`` from its ``track_b``
section.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from dbench import dockerctl, hostinfo
from dbench.config import QUERY_TIMEOUT_S, STREAMS, Limits
from dbench.datagen import DatasetPaths
from dbench.measures import load, storage
from dbench.measures.power import QUERY_NUMBERS, run_power
from dbench.measures.throughput import ITEM_FORMAT, run_throughput
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
    phase: str = "query"  # "query" | "after_load" | "last"


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


def stream_items(streams: Iterable[int]) -> list[str]:
    return [ITEM_FORMAT.format(n=n) for n in streams]


def _throughput_items(sf: int) -> list[str]:
    return stream_items(STREAMS)  # a campaign's ``streams:`` key narrows this (Campaign.planned)


def _run_throughput(adapter: SystemAdapter, ctx: MeasureContext) -> list[ResultRecord]:
    return [run_throughput(adapter, ctx.sf, int(item.removeprefix("streams=")), ctx.run_id,
                           ctx.results, host=ctx.host, timeout_s=ctx.timeout_s)
            for item in ctx.items]


register(Measure("M4", "qph", _throughput_items, _run_throughput))


LOAD_BULK_ITEM = "load_bulk"  # recorded by the campaign's own bulk load, not by M2's runner
CONVERT_ITEM = "load_convert"


def _load_items(sf: int) -> list[str]:
    return [LOAD_BULK_ITEM, CONVERT_ITEM]


def _run_convert(adapter: SystemAdapter, ctx: MeasureContext) -> list[ResultRecord]:
    if CONVERT_ITEM not in ctx.items:
        return []
    return [load.run_convert(adapter, ctx.data, ctx.sf, ctx.run_id, ctx.results, ctx.limits,
                             host=ctx.host)]


def _storage_items(sf: int) -> list[str]:
    return list(storage.ITEMS)


def _run_storage(adapter: SystemAdapter, ctx: MeasureContext) -> list[ResultRecord]:
    return storage.run_storage(adapter, ctx.sf, ctx.run_id, ctx.results, ctx.items,
                               host=ctx.host)


register(Measure("M2", "s", _load_items, _run_convert, phase="last"))
register(Measure("M3", "bytes", _storage_items, _run_storage, phase="after_load"))
