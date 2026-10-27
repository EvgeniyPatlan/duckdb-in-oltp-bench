"""Shared classification of a dropped client connection."""
from __future__ import annotations

import time
from collections.abc import Callable

from dbench.systems.base import Pushed, QueryOutcome

EXIT_POLL_S = 5.0
EXIT_POLL_INTERVAL_S = 0.25


def classify_connection_failure(
    crash_info: Callable[[], dict],
    exc: Exception,
    pushed: Pushed = "n/a",
    *,
    poll_s: float | None = None,
    interval_s: float | None = None,
    sleep: Callable[[float], None] | None = None,
) -> QueryOutcome:
    """``crashed`` when the container is no longer running, else ``error``.

    Docker can lag behind the kill, so poll up to ``poll_s`` for the container to report
    that it stopped before deciding.
    """
    poll_s = EXIT_POLL_S if poll_s is None else poll_s
    interval_s = EXIT_POLL_INTERVAL_S if interval_s is None else interval_s
    sleep = sleep or time.sleep
    deadline = time.monotonic() + poll_s
    state = crash_info()
    while state.get("status") == "running" and time.monotonic() < deadline:
        sleep(interval_s)
        state = crash_info()
    status = "crashed" if state.get("status") != "running" else "error"
    return QueryOutcome(status, None, None, set(), f"{type(exc).__name__}: {exc}", pushed)
