"""Shared benchmark constants and resource limits."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    db_cpuset: str = "0-15"
    runner_cpuset: str = "16-23"
    mem_gb: int = 64


QUERY_TIMEOUT_S = 900
STREAMS = (1, 4, 8, 16, 32)
WARM_RUNS = 3
SCALE_FACTORS = (1, 10, 100, 300)
