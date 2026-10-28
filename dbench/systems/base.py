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
# Where an adapter reads pushdown from: a per-session counter, a server-wide counter (ambiguous
# when several streams run), the plan-only EXPLAIN, or nowhere (n/a or a constant).
CounterScope = Literal["session", "global", "explain", "none"]
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
    extra: dict = field(default_factory=dict)  # adapter facts merged into the record's extra
    # Throughput streams: the server-side id (thread id / backend pid) of the query, and whether
    # the server itself reported the query stopped (its own deadline fired). A timeout with
    # neither is a stop nobody confirmed.
    query_id: int | None = None
    stopped: bool = False


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
    pushdown_counter_scope: CounterScope = "none"

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

    def open_stream(self, timeout_s: int) -> None:  # noqa: B027 - optional hook
        """Called on a stream's thread before the timed region: open this thread's persistent
        connection, apply the session/timeout settings once and run one trivial statement through
        DuckDB, so ``execute()`` on this thread then runs only the query. Default: nothing
        (``execute()`` keeps its per-query behaviour)."""

    def close_stream(self) -> None:  # noqa: B027 - optional hook
        """Called on a stream's thread after the timed region: close its connection."""

    def still_running(self, ident: int) -> bool:
        """Whether the server-side query ``ident`` (``QueryOutcome.query_id``) is still running.
        The default is conservative: True."""
        return True

    def prepare_streams(self, n: int) -> None:  # noqa: B027 - optional hook, no-op by default
        """Called by the throughput run before it opens ``n`` concurrent streams and again with
        ``1`` afterwards. Adapters whose backends each own a DuckDB instance divide its memory
        by ``n``; the default does nothing."""

    def memory_per_backend(self) -> str | None:
        """DuckDB memory each backend gets under the current ``prepare_streams`` (None when
        streams share one instance or the setting is not divided)."""
        return None

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

    def lingering_queries(self) -> int:
        """Queries of the bench user still running on the server (excluding this probe).
        Used after a timeout to detect a query that cancel() did not stop."""
        return 0

    @abstractmethod
    def pushdown(self, sql: str) -> Pushed:
        """Whether DuckDB ran the query; "n/a" for systems that never use it."""

    # storage and capability
    @abstractmethod
    def storage_bytes(self) -> int:
        """Total size of the data directory in bytes. Measures only; never flushes."""

    def storage_breakdown(self) -> dict:
        """``{data_bytes, redo_wal_bytes, temp_bytes, other_bytes}`` of the data directory (see
        ``dbench.systems.storage_layout``). It adds up to ``storage_bytes()``, except for plain
        DuckDB, whose total covers only the database file and its WAL."""
        raise NotImplementedError(f"{type(self).__name__} does not implement storage_breakdown()")

    def checkpoint(self) -> str | None:
        """Run the system's checkpoint so the data is in its final files. Returns None once
        done, or the reason when the system has no checkpoint statement (an n/a cell)."""
        return "no checkpoint statement is available for this system"

    @abstractmethod
    def supports(self, measure: str) -> tuple[bool, str | None]:
        """(applies?, n/a reason when it does not)."""
