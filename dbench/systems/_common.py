"""Pieces shared by the server-container adapters (MySQL and Postgres families)."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from docker.errors import NotFound

from dbench import dockerctl
from dbench.config import Limits
from dbench.datagen import DatasetPaths
from dbench.systems.base import LoadOutcome, Pushed, SystemAdapter
from dbench.systems.storage_layout import FIND_COMMAND, Classifier, parse_listing, summarize

DATA_MOUNT = "/data"
PROBE_TIMEOUT_S = 10
READ_MARGIN_S = 30
LOAD_TIMEOUT_S = 24 * 3600
READY_TIMEOUT_S = 900
NO_DUCKDB_COPY = "row-store baseline: no DuckDB copy"
NO_DUCKDB_REASON = "row-store baseline: no DuckDB engine, nothing to convert or pushdown"


def read_status_counter(cur, sql: str) -> int | None:
    """Value of a ``SHOW ... STATUS LIKE '<name>'`` counter, or None when it is absent."""
    cur.execute(sql)
    row = cur.fetchone()
    return int(row[1]) if row else None


def pushed_from_counts(before: int | None, after: int | None, *, rises: bool = True) -> Pushed:
    """Pushdown from a status counter around a query. ``rises=True``: the engine bumps the
    counter for each pushed query (Percona, AliSQL); ``rises=False``: a pushed query leaves it
    unchanged and the row path raises it (MariaDB ``Rows_read``)."""
    if before is None or after is None:
        return "unknown"
    pushed = after > before if rises else after == before
    return "duckdb" if pushed else "fallback"


class ActiveQueries:
    """Server-side ids (thread id / backend pid) of running queries and their deadlines.

    ``cancel()`` only ever touches queries past their own deadline, so concurrent
    streams still inside their timeout are left alone.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._deadlines: dict[int, float] = {}

    def add(self, ident: int, timeout_s: float) -> None:
        with self._lock:
            self._deadlines[ident] = time.monotonic() + timeout_s

    def remove(self, ident: int) -> None:
        with self._lock:
            self._deadlines.pop(ident, None)

    def pop_expired(self) -> list[int]:
        now = time.monotonic()
        with self._lock:
            expired = [i for i, d in self._deadlines.items() if d <= now]
            for i in expired:
                del self._deadlines[i]
        return expired


def container_data_path(data_dir: Path | None, path: Path | str) -> str:
    if data_dir is None:
        raise RuntimeError("data_dir is required for load_bulk")
    return f"{DATA_MOUNT}/{Path(path).resolve().relative_to(data_dir.resolve())}"


class ContainerServerAdapter(SystemAdapter):
    """One database container with a named data volume and a read-only /data mount.

    Subclasses set the class attributes below and implement the protocol parts
    (``connect``, ``alive``, ``execute``, ``cancel``, loading, versions).
    """

    track = "baseline"
    image: str
    datadir: str  # where the data volume is mounted in the container
    du_command: str  # shell command printing the datadir size in bytes (first field)
    layout_dir: str  # directory (shell-quoted) whose files ``storage_breakdown`` classifies
    layout_classifier: Classifier
    postgres_shm = False
    container_user: str | None = None  # None: the image's default user

    def __init__(self, data_dir: Path | str | None = None, rendered_dir: Path | None = None):
        """``data_dir``: host path mounted read-only at /data (bulk load input)."""
        self.container_name = f"dbench-{self.name}"
        self.volume = f"dbench-{self.name}-data"
        self.data_dir = Path(data_dir) if data_dir else None
        self.rendered_dir = rendered_dir
        self._limits: Limits | None = None
        self.active = ActiveQueries()

    # engine hooks
    def container_env(self) -> dict[str, str]:
        return {}

    def container_command(self, limits: Limits) -> list[str] | None:
        return None

    def config_mounts(self, limits: Limits) -> dict:
        """Extra read-only bind mounts (rendered config files)."""
        return {}

    # lifecycle
    def start(self, limits: Limits) -> None:
        self._limits = limits
        volumes: dict = {self.volume: {"bind": self.datadir, "mode": "rw"}}
        volumes.update(self.config_mounts(limits))
        if self.data_dir:
            volumes[dockerctl.host_path(self.data_dir)] = {"bind": DATA_MOUNT, "mode": "ro"}
        dockerctl.start(self.image, self.container_name, limits, self.container_env(), volumes,
                        {}, self.container_command(limits), postgres=self.postgres_shm,
                        user=self.container_user)
        self._wait_ready()

    def _wait_ready(self) -> None:
        """Wait until the server answers; fail at once if its container has exited."""
        deadline = time.monotonic() + READY_TIMEOUT_S
        while not self.alive():
            state = self.crash_info()
            if state.get("status") not in ("running", "created", "restarting"):
                tail = str(state.get("logs", ""))[-2000:]
                raise RuntimeError(f"{self.name} container exited during startup: {tail}")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{self.name} not ready after {READY_TIMEOUT_S}s")
            time.sleep(1.0)

    def stop(self) -> None:
        dockerctl.stop(self.container_name)

    def remove_data(self) -> None:
        self.stop()
        try:
            dockerctl._client().volumes.get(self.volume).remove(force=True)
        except NotFound:
            pass

    def restart(self) -> None:
        if self._limits is None:
            raise RuntimeError("restart() before start()")
        self.start(self._limits)

    def host(self) -> str:
        return dockerctl.container_ip(self.container_name)

    def crash_info(self) -> dict:
        try:
            return dockerctl.container_state(self.container_name)
        except Exception as exc:  # noqa: BLE001
            return {"status": "missing", "error": str(exc)}

    def image_info(self) -> dict:
        try:
            digest = dockerctl.image_digest(self.image)
        except Exception as exc:  # noqa: BLE001 - versions() must not fail on a missing image
            digest = f"unknown ({type(exc).__name__})"
        return {"image": self.image, "image_digest": digest}

    # no DuckDB in the baselines
    def load_convert(self, data: DatasetPaths) -> LoadOutcome:
        return LoadOutcome("n/a", None, None, {"reason": NO_DUCKDB_COPY})

    def pushdown(self, sql: str) -> Pushed:
        return "n/a"

    def supports(self, measure: str) -> tuple[bool, str | None]:
        if measure in ("M11", "M12"):
            return False, NO_DUCKDB_REASON
        return True, None

    def _sh(self, command: str) -> str:
        c = dockerctl._client().containers.get(self.container_name)
        code, out = c.exec_run(["sh", "-c", command])
        if code != 0:
            raise RuntimeError(f"`{command}` failed ({code}): {out.decode(errors='replace')}")
        return out.decode(errors="replace")

    def storage_bytes(self) -> int:
        return int(self._sh(self.du_command).split()[0])

    def storage_breakdown(self) -> dict:
        listing = self._sh(FIND_COMMAND.format(dir=self.layout_dir))
        return summarize(parse_listing(listing), self.layout_classifier)
