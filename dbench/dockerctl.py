"""Container lifecycle with pinned cpusets and hard memory caps."""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from docker.errors import NotFound
from docker.models.containers import Container

import docker
from dbench.config import Limits

LABEL = {"dbench": "1"}
RESERVED_HOST_GB = 16


def parse_cpuset(spec: str) -> set[int]:
    """Parse docker cpuset syntax ("0-15", "0,2,4-7") into a set of CPU ids."""
    cpus: set[int] = set()
    if not spec or not spec.strip():
        raise ValueError(f"malformed cpuset: {spec!r} (empty)")
    for part in spec.split(","):
        bounds = part.split("-")
        if len(bounds) > 2 or not all(b.isdigit() for b in bounds):
            raise ValueError(f"malformed cpuset: {spec!r} (bad element {part!r})")
        lo, hi = int(bounds[0]), int(bounds[-1])
        if lo > hi:
            raise ValueError(f"malformed cpuset: {spec!r} (descending range {part!r})")
        cpus.update(range(lo, hi + 1))
    return cpus


DEV_MOUNT = Path("/w")


def host_path(path: Path | str) -> str:
    """Host-side path of ``path`` for docker bind mounts.

    Inside scripts/dev.sh the repo is mounted at /w and DBENCH_HOST_REPO names its host
    location; the daemon needs the host spelling of any bind-mount source.
    """
    p = Path(path).resolve()
    repo = os.environ.get("DBENCH_HOST_REPO")
    if repo and p.is_relative_to(DEV_MOUNT):
        return str(Path(repo) / p.relative_to(DEV_MOUNT))
    return str(p)


def read_host_mem_gb() -> int:
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // (1024 * 1024)
    raise RuntimeError("MemTotal not found in /proc/meminfo")


def validate_limits(limits: Limits, host_cpus: int, host_mem_gb: int | None = None) -> None:
    """Refuse overlapping, out-of-range or oversized limits before anything starts."""
    db = parse_cpuset(limits.db_cpuset)
    runner = parse_cpuset(limits.runner_cpuset)
    checks = (("db", db, limits.db_cpuset), ("runner", runner, limits.runner_cpuset))
    for label, cpus, spec in checks:
        if max(cpus) >= host_cpus:
            raise ValueError(
                f"{label} cpuset {spec!r} exceeds host cores 0..{host_cpus - 1}"
            )
    both = db & runner
    if both:
        raise ValueError(f"db and runner cpusets overlap on cpus {sorted(both)}")
    total = read_host_mem_gb() if host_mem_gb is None else host_mem_gb
    if limits.mem_gb > total - RESERVED_HOST_GB:
        raise ValueError(
            f"mem_gb={limits.mem_gb} exceeds host RAM {total} GB minus "
            f"{RESERVED_HOST_GB} GB reserve"
        )


def run_args(limits: Limits, role: Literal["db", "runner"], *, postgres: bool = False) -> dict:
    if role == "db":
        mem = f"{limits.mem_gb}g"
        args: dict = {"cpuset_cpus": limits.db_cpuset, "mem_limit": mem, "memswap_limit": mem}
    elif role == "runner":
        args = {"cpuset_cpus": limits.runner_cpuset}
    else:
        raise ValueError(f"unknown role: {role!r}")
    if postgres:
        args["shm_size"] = "8g"
    return args


def _client() -> docker.DockerClient:
    return docker.from_env()


def start(
    image: str,
    name: str,
    limits: Limits,
    env: dict[str, str],
    volumes: dict,
    ports: dict,
    command: list[str] | str | None = None,
    *,
    role: Literal["db", "runner"] = "db",
    postgres: bool = False,
    user: str | None = None,
) -> Container:
    stop(name)
    return _client().containers.run(
        image,
        command,
        name=name,
        detach=True,
        labels=dict(LABEL),
        environment=env,
        volumes=volumes,
        ports=ports,
        user=user,
        **run_args(limits, role, postgres=postgres),
    )


def wait_ready(
    probe: Callable[[], bool], timeout_s: float = 600, interval_s: float = 1.0
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if probe():
                return
        except Exception:  # noqa: BLE001 - server not accepting connections yet
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(f"not ready after {timeout_s}s")
        time.sleep(interval_s)


def stop(name: str) -> None:
    try:
        c = _client().containers.get(name)
    except NotFound:
        return
    c.remove(force=True, v=True)


def drop_caches() -> None:
    """Drop the host page cache (privileged container; needs root in the daemon)."""
    _client().containers.run(
        "alpine:3.20",
        ["sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
        privileged=True,
        remove=True,
        labels=dict(LABEL),
    )


def container_state(name: str) -> dict:
    c = _client().containers.get(name)
    st = c.attrs["State"]
    return {
        "status": st["Status"],
        "OOMKilled": st["OOMKilled"],
        "ExitCode": st["ExitCode"],
        "logs": c.logs(tail=200).decode("utf-8", "replace"),
    }


def container_ip(name: str) -> str:
    """IP of a running container on its first attached network."""
    c = _client().containers.get(name)
    nets = c.attrs["NetworkSettings"]["Networks"]
    return next(n["IPAddress"] for n in nets.values() if n["IPAddress"])


def image_digest(image: str) -> str:
    """Resolved registry digest of a local image (its id when it has none)."""
    img = _client().images.get(image)
    digests = img.attrs.get("RepoDigests") or []
    return digests[0] if digests else img.id
