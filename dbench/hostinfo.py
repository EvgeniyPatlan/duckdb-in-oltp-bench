"""Facts about the benchmark host, recorded with every result."""
from __future__ import annotations

import os
import platform
import time

from dbench import dockerctl


def host_info() -> dict:
    return {
        "cpus": os.cpu_count(),
        "mem_gb": dockerctl.read_host_mem_gb(),
        "kernel": platform.release(),
        "docker_version": dockerctl._client().version().get("Version"),
    }


def suspended_s() -> float:
    """Seconds the host has spent suspended (sleep, hibernation) since boot.

    Linux ``CLOCK_BOOTTIME`` keeps counting while the machine sleeps and ``CLOCK_MONOTONIC``
    does not, so their difference only grows across a suspend. Containers share the host's
    clocks, so this works inside the runner container too.
    """
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
