"""Scripted adapter for exercising measures without a database."""
from __future__ import annotations

from typing import Any

from dbench.config import Limits
from dbench.systems.base import LoadOutcome, QueryOutcome, SystemAdapter


def ok(elapsed: float = 1.0, rows: list[tuple] | None = None, numeric=(0,)) -> QueryOutcome:
    return QueryOutcome("ok", elapsed, rows if rows is not None else [(1,)], set(numeric),
                        None, "duckdb")


def timeout() -> QueryOutcome:
    return QueryOutcome("timeout", None, None, set(), "query exceeded timeout", "n/a")


def crashed() -> QueryOutcome:
    return QueryOutcome("crashed", None, None, set(), "connection lost", "n/a")


class FakeAdapter(SystemAdapter):
    name = "fake"
    track = "A"
    dialect = "duckdb"
    family = "duckdb"
    config_dir = "fake"

    def __init__(self, outcomes: list[QueryOutcome], alive_after: list[bool] | None = None,
                 restart_error: Exception | None = None, crash_status: str = "exited",
                 lingering: list[int] | None = None):
        """``outcomes`` are consumed one per execute(); ok() once exhausted.
        ``alive_after`` scripts successive alive() answers (True once exhausted)."""
        self._outcomes = list(outcomes)
        self._alive = list(alive_after or [])
        self._restart_error = restart_error
        self._crash_status = crash_status
        self._lingering = list(lingering or [])
        self.executed: list[str] = []
        self.cancels = 0
        self.restarts = 0
        self.alive_calls = 0

    def execute(self, sql: str, timeout_s: int) -> QueryOutcome:
        self.executed.append(sql)
        return self._outcomes.pop(0) if self._outcomes else ok()

    def cancel(self) -> bool:
        self.cancels += 1
        return True

    def alive(self) -> bool:
        self.alive_calls += 1
        return self._alive.pop(0) if self._alive else True

    def lingering_queries(self) -> int:
        return self._lingering.pop(0) if self._lingering else 0

    def restart(self) -> None:
        self.restarts += 1
        if self._restart_error is not None:
            raise self._restart_error

    def crash_info(self) -> dict:
        return {"status": self._crash_status, "ExitCode": 137, "OOMKilled": True, "logs": "tail"}

    def versions(self) -> dict:
        return {"fake": "1"}

    def start(self, limits: Limits) -> None: ...
    def stop(self) -> None: ...
    def connect(self) -> Any: return None
    def create_schema(self, sf, kind) -> None: ...
    def load_bulk(self, data) -> LoadOutcome: return LoadOutcome("ok", 0.0)
    def load_convert(self, data) -> LoadOutcome: return LoadOutcome("ok", 0.0)
    def pushdown(self, sql: str): return "duckdb"
    def storage_bytes(self) -> int: return 0
    def supports(self, measure: str): return True, None
