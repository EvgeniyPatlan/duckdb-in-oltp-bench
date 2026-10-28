"""Result record schema and on-disk storage layout.

Layout: <root>/<run_id>/<system>/<measure>-sf<sf>-<item>.json
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Literal

Track = Literal["A", "B", "baseline"]
Status = Literal["ok", "timeout", "error", "crashed", "wrong_result", "failed", "n/a"]

TRACKS = ("A", "B", "baseline")
STATUSES = ("ok", "timeout", "error", "crashed", "wrong_result", "failed", "n/a")
MEASURES = tuple(f"M{i}" for i in range(1, 14))
REASON_REQUIRED = ("n/a", "failed")

_UNSAFE = re.compile(r"[^A-Za-z0-9._=-]")


@dataclass(frozen=True)
class ResultRecord:
    system: str
    track: Track
    measure: str
    sf: int
    item: str
    status: Status
    value: float | None
    unit: str
    config_ref: str
    run_id: str
    started_at: str
    extra: dict = field(default_factory=dict)
    error: str | None = None
    reason: str | None = None
    versions: dict = field(default_factory=dict)
    host: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.track not in TRACKS:
            raise ValueError(f"invalid track {self.track!r}")
        if self.measure not in MEASURES:
            raise ValueError(f"invalid measure {self.measure!r}")
        if self.status not in STATUSES:
            raise ValueError(f"invalid status {self.status!r}")
        if self.status in REASON_REQUIRED and not self.reason:
            raise ValueError(f"status {self.status!r} requires a reason")


def key(rec: ResultRecord) -> tuple:
    return (rec.system, rec.measure, rec.sf, rec.item, rec.run_id)


def _safe(name: str) -> str:
    return _UNSAFE.sub("_", name)


def _path(root: Path, system: str, measure: str, sf: int, item: str, run_id: str) -> Path:
    return Path(root) / _safe(run_id) / _safe(system) / f"{measure}-sf{sf}-{_safe(item)}.json"


def write(rec: ResultRecord, root: Path) -> Path:
    path = _path(root, rec.system, rec.measure, rec.sf, rec.item, rec.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(rec), fh, sort_keys=True, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def read(path: Path) -> ResultRecord:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {f.name for f in fields(ResultRecord)}
    return ResultRecord(**{k: v for k, v in data.items() if k in names})


def iter_records(root: Path) -> Iterator[ResultRecord]:
    root = Path(root)
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.json")):
        rel_dirs = path.relative_to(root).parts[:-1]
        if any(part.startswith("_") for part in rel_dirs):
            continue
        yield read(path)


def exists(root: Path, system: str, measure: str, sf: int, item: str, run_id: str) -> bool:
    return _path(root, system, measure, sf, item, run_id).is_file()


def cell_record(root: Path, system: str, measure: str, sf: int, item: str,
                run_id: str) -> ResultRecord | None:
    """The stored record of a cell, or None when it is missing or unreadable."""
    path = _path(root, system, measure, sf, item, run_id)
    try:
        return read(path)
    except (OSError, ValueError, TypeError):
        return None


def is_auto_failed(rec: ResultRecord) -> bool:
    """A failure the campaign recorded itself (start, load or measure abort): retried on resume."""
    return rec.status == "failed" and bool(rec.extra.get("auto"))


def is_done(root: Path, system: str, measure: str, sf: int, item: str, run_id: str) -> bool:
    """A cell resume leaves alone: it has a record that is not an automatic failure."""
    rec = cell_record(root, system, measure, sf, item, run_id)
    return rec is not None and not is_auto_failed(rec)
