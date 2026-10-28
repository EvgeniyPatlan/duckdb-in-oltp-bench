"""M3: storage size in bytes.

``after_load``        right after the bulk load, before any query and before a checkpoint
``after_checkpoint``  after the system's checkpoint statement; ``n/a`` with the reason when the
                      system has none

The value is the total data directory (``SystemAdapter.storage_bytes``: replication logs
excluded, redo and WAL counted). ``extra.breakdown`` splits it into data, redo/WAL, temp and
other bytes; see docs/methodology/storage.md.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from dbench.measures.records import adapter_versions, make_record, utcnow
from dbench.results import ResultRecord
from dbench.systems.base import SystemAdapter

ITEMS = ["after_load", "after_checkpoint"]


def run_storage(
    adapter: SystemAdapter, sf: int, run_id: str, root, items: list[str], *,
    clock: Callable[[], datetime] = utcnow, host: Callable[[], dict] | None = None,
) -> list[ResultRecord]:
    """Measure the pending ``items``. A failed measurement is an ``error`` cell, not an abort."""
    common = {"measure": "M3", "sf": sf, "run_id": run_id, "root": root, "unit": "bytes",
              "clock": clock, "host": host, "versions": adapter_versions(adapter)}
    records: list[ResultRecord] = []

    def measure(item: str) -> ResultRecord:
        try:
            total = adapter.storage_bytes()
            breakdown = adapter.storage_breakdown()
        except Exception as exc:  # noqa: BLE001 - recorded
            return make_record(adapter, item=item, status="error",
                               error=f"{type(exc).__name__}: {exc}", **common)
        return make_record(adapter, item=item, status="ok", value=total,
                           extra={"breakdown": breakdown}, **common)

    if "after_load" in items:
        records.append(measure("after_load"))
    if "after_checkpoint" in items:
        try:
            reason = adapter.checkpoint()
        except Exception as exc:  # noqa: BLE001 - recorded
            records.append(make_record(adapter, item="after_checkpoint", status="error",
                                       error=f"checkpoint failed: {type(exc).__name__}: {exc}",
                                       **common))
        else:
            if reason:
                records.append(make_record(adapter, item="after_checkpoint", status="n/a",
                                           reason=reason, **common))
            else:
                records.append(measure("after_checkpoint"))
    return records
