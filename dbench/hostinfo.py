"""Facts about the benchmark host, recorded with every result."""
from __future__ import annotations

import os
import platform

from dbench import dockerctl


def host_info() -> dict:
    return {
        "cpus": os.cpu_count(),
        "mem_gb": dockerctl.read_host_mem_gb(),
        "kernel": platform.release(),
        "docker_version": dockerctl._client().version().get("Version"),
    }
