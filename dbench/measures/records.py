"""Record construction shared by the load and storage measures."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from dbench.results import ResultRecord, Status, write
from dbench.systems.base import SystemAdapter


def utcnow() -> datetime:
    return datetime.now(UTC)


def adapter_versions(adapter: SystemAdapter) -> dict:
    """The server's versions for the record; a server that cannot answer must not lose the
    measurement."""
    try:
        return adapter.versions()
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        return {"error": f"versions() failed: {type(exc).__name__}: {exc}"}


def make_record(
    adapter: SystemAdapter, *, measure: str, item: str, status: Status, sf: int, run_id: str,
    root, unit: str, value: float | None = None, extra: dict | None = None,
    error: str | None = None, reason: str | None = None, versions: dict | None = None,
    clock: Callable[[], datetime] = utcnow, host: Callable[[], dict] | None = None,
    started: datetime | None = None,
) -> ResultRecord:
    """Build and write one cell. ``host`` is resolved here so every record carries host facts."""
    if host is None:
        from dbench import hostinfo
        host = hostinfo.host_info
    rec = ResultRecord(
        system=adapter.name, track=adapter.track, measure=measure, sf=sf, item=item,
        status=status, value=value, unit=unit, config_ref=f"systems/{adapter.config_dir}/",
        run_id=run_id, started_at=(started or clock()).isoformat(), extra=extra or {},
        error=error, reason=reason,
        versions=adapter_versions(adapter) if versions is None else versions, host=host())
    write(rec, root)
    return rec
