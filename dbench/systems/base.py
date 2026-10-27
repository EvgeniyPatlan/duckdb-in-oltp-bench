"""Contract every system adapter implements.

An adapter owns one database container (or server) and exposes the same
operations for every system under test, so the measures in ``dbench.measures``
never contain system-specific code.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from dbench.config import Limits

if TYPE_CHECKING:
    from dbench.datagen import DatasetPaths

Family = Literal["mysql", "mariadb", "alisql", "postgres", "duckdb", "villagesql"]
QueryStatus = Literal["ok", "timeout", "error", "crashed"]
Pushed = Literal["duckdb", "fallback", "unknown", "n/a"]
LoadStatus = Literal["ok", "timeout", "error", "crashed", "n/a"]

_NUMERIC_TYPE = re.compile(
    r"^\s*(u?(tiny|small|medium|big|huge)?int(eger)?\d*|decimal|numeric|number|"
    r"double|float\d*|real)\b",
    re.IGNORECASE,
)


def is_numeric_type(type_name: str) -> bool:
    """True for integer, DECIMAL/NUMERIC and floating-point driver type names."""
    return bool(_NUMERIC_TYPE.match(type_name))


def numeric_cols_from_types(type_names: list[str]) -> set[int]:
    """Indexes of numeric columns, derived from a driver's column type metadata."""
    return {i for i, t in enumerate(type_names) if is_numeric_type(t)}


@dataclass(frozen=True)
class QueryOutcome:
    status: QueryStatus
    elapsed_s: float | None
    rows: list[tuple] | None
    numeric_cols: set[int] = field(default_factory=set)
    error: str | None = None
    pushed: Pushed = "n/a"
    columns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LoadOutcome:
    status: LoadStatus
    elapsed_s: float | None
    error: str | None = None
    extra: dict = field(default_factory=dict)


class SystemAdapter(ABC):
    name: str
    track: str
    dialect: str
    family: Family
    config_dir: str  # directory under systems/ holding this system's config and README
    # Schema the benchmark creates: "engine" (PKs only) or "rowstore" (PKs + FK indexes).
    bench_schema_kind: Literal["engine", "rowstore"] = "engine"

    # lifecycle
    @abstractmethod
    def start(self, limits: Limits) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def restart(self) -> None:
        """Replace a dead or hung server with a fresh one on the same data."""

    @abstractmethod
    def alive(self) -> bool:
        """Liveness probe: ``SELECT 1`` answered within 10 s."""

    @abstractmethod
    def connect(self) -> Any:
        """A new client connection (one per throughput stream)."""

    def crash_info(self) -> dict:
        """Exit status and log tail of a dead server, recorded with ``crashed`` results."""
        return {}

    # versions and DDL
    @abstractmethod
    def versions(self) -> dict: ...

    def remove_data(self) -> None:
        """Stop the server and delete its data volume."""
        raise NotImplementedError(f"{type(self).__name__} does not implement remove_data()")

    @abstractmethod
    def create_schema(self, sf: int, kind: Literal["engine", "rowstore"]) -> None: ...

    # loading
    @abstractmethod
    def load_bulk(self, data: DatasetPaths) -> LoadOutcome: ...

    @abstractmethod
    def load_convert(self, data: DatasetPaths) -> LoadOutcome: ...

    # queries
    @abstractmethod
    def execute(self, sql: str, timeout_s: int) -> QueryOutcome: ...

    @abstractmethod
    def cancel(self) -> bool:
        """Try to abort the running query; True if a cancel was issued."""

    @abstractmethod
    def pushdown(self, sql: str) -> Pushed:
        """Whether DuckDB ran the query; "n/a" for systems that never use it."""

    # storage and capability
    @abstractmethod
    def storage_bytes(self) -> int: ...

    @abstractmethod
    def supports(self, measure: str) -> tuple[bool, str | None]:
        """(applies?, n/a reason when it does not)."""
