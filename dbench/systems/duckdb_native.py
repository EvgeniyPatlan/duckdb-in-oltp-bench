"""Plain DuckDB reference: one container running systems/duckdb/server.py."""
from __future__ import annotations

import json
import socket
import threading
from datetime import date, datetime
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from docker.errors import NotFound

from dbench import dockerctl
from dbench.config import Limits
from dbench.datagen import DatasetPaths, expected_rows
from dbench.systems._crash import classify_connection_failure
from dbench.systems.base import (
    LoadOutcome,
    QueryOutcome,
    SystemAdapter,
    numeric_cols_from_types,
)
from dbench.systems.storage_layout import FIND_COMMAND, classify_duckdb, parse_listing, summarize

PORT = 9999
DB_FILE = "/var/lib/duckdb/tpch.duckdb"
DB_DIR = "/var/lib/duckdb"
DATA_MOUNT = "/data"
SOCKET_MARGIN_S = 30
PROBE_TIMEOUT_S = 10
LOAD_TIMEOUT_S = 24 * 3600
MEMORY_FRACTION = 0.8
NO_ROW_STORE = "plain DuckDB reference has no row store"
NO_COMPAT = "plain DuckDB is the engine itself: no MySQL/Postgres dialect to probe, no fallback"

TPCH_DDL: dict[str, str] = {
    "region": "r_regionkey INTEGER PRIMARY KEY, r_name VARCHAR, r_comment VARCHAR",
    "nation": "n_nationkey INTEGER PRIMARY KEY, n_name VARCHAR, n_regionkey INTEGER, "
              "n_comment VARCHAR",
    "part": "p_partkey INTEGER PRIMARY KEY, p_name VARCHAR, p_mfgr VARCHAR, p_brand VARCHAR, "
            "p_type VARCHAR, p_size INTEGER, p_container VARCHAR, p_retailprice DECIMAL(15,2), "
            "p_comment VARCHAR",
    "supplier": "s_suppkey INTEGER PRIMARY KEY, s_name VARCHAR, s_address VARCHAR, "
                "s_nationkey INTEGER, s_phone VARCHAR, s_acctbal DECIMAL(15,2), s_comment VARCHAR",
    "partsupp": "ps_partkey INTEGER, ps_suppkey INTEGER, ps_availqty INTEGER, "
                "ps_supplycost DECIMAL(15,2), ps_comment VARCHAR, "
                "PRIMARY KEY (ps_partkey, ps_suppkey)",
    "customer": "c_custkey INTEGER PRIMARY KEY, c_name VARCHAR, c_address VARCHAR, "
                "c_nationkey INTEGER, c_phone VARCHAR, c_acctbal DECIMAL(15,2), "
                "c_mktsegment VARCHAR, c_comment VARCHAR",
    "orders": "o_orderkey BIGINT PRIMARY KEY, o_custkey INTEGER, o_orderstatus VARCHAR, "
              "o_totalprice DECIMAL(15,2), o_orderdate DATE, o_orderpriority VARCHAR, "
              "o_clerk VARCHAR, o_shippriority INTEGER, o_comment VARCHAR",
    "lineitem": "l_orderkey BIGINT, l_partkey INTEGER, l_suppkey INTEGER, l_linenumber INTEGER, "
                "l_quantity DECIMAL(15,2), l_extendedprice DECIMAL(15,2), "
                "l_discount DECIMAL(15,2), l_tax DECIMAL(15,2), l_returnflag VARCHAR, "
                "l_linestatus VARCHAR, l_shipdate DATE, l_commitdate DATE, l_receiptdate DATE, "
                "l_shipinstruct VARCHAR, l_shipmode VARCHAR, l_comment VARCHAR, "
                "PRIMARY KEY (l_orderkey, l_linenumber)",
}


def decode(v: Any) -> Any:
    """Inverse of the server's value encoding."""
    if isinstance(v, dict):
        if "$dec" in v:
            return Decimal(v["$dec"])
        if "$date" in v:
            return date.fromisoformat(v["$date"])
        if "$ts" in v:
            return datetime.fromisoformat(v["$ts"])
        if "$time" in v:
            return dtime.fromisoformat(v["$time"])
        if "$hex" in v:
            return bytes.fromhex(v["$hex"])
        if "$str" in v:
            return v["$str"]
    return v


class DuckDBConnection:
    """One TCP connection to the query server (its own DuckDB cursor server-side)."""

    def __init__(self, host: str, port: int = PORT, connect_timeout_s: float = PROBE_TIMEOUT_S):
        self._sock = socket.create_connection((host, port), timeout=connect_timeout_s)
        self._file = self._sock.makefile("rwb")

    def request(self, payload: dict, socket_timeout_s: float) -> dict:
        self._sock.settimeout(socket_timeout_s)
        self._file.write(json.dumps(payload).encode() + b"\n")
        self._file.flush()
        line = self._file.readline()
        if not line:
            raise ConnectionError("server closed the connection")
        return json.loads(line)

    def execute(self, sql: str, timeout_s: float) -> dict:
        return self.request({"sql": sql, "timeout_s": timeout_s}, timeout_s + SOCKET_MARGIN_S)

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()


class DuckDBNative(SystemAdapter):
    track = "baseline"
    dialect = "duckdb"
    family = "duckdb"
    config_dir = "duckdb"

    def __init__(self, version: str, data_dir: Path | str | None = None):
        """``data_dir`` is a host path mounted read-only at /data (bulk load input)."""
        self.version = version
        self.name = f"duckdb-{version}"
        self.image = f"dbench/duckdb:{version}"
        self.container_name = f"dbench-{self.name}"
        self.volume = f"dbench-{self.name}-data"
        self.data_dir = Path(data_dir) if data_dir else None
        self._limits: Limits | None = None
        self._stream = threading.local()  # this thread's throughput-stream state

    # lifecycle
    def start(self, limits: Limits) -> None:
        self._limits = limits
        threads = len(dockerctl.parse_cpuset(limits.db_cpuset))
        env = {
            "DBENCH_THREADS": str(threads),
            "DBENCH_MEMORY_LIMIT": f"{limits.mem_gb * MEMORY_FRACTION:.1f}GB",
        }
        volumes: dict = {self.volume: {"bind": "/var/lib/duckdb", "mode": "rw"}}
        if self.data_dir:
            volumes[dockerctl.host_path(self.data_dir)] = {"bind": DATA_MOUNT, "mode": "ro"}
        dockerctl.start(self.image, self.container_name, limits, env, volumes, {})
        dockerctl.wait_ready(self.alive, timeout_s=120)

    def stop(self) -> None:
        dockerctl.stop(self.container_name)

    def remove_data(self) -> None:
        """Delete the database volume (stops the container first)."""
        self.stop()
        try:
            dockerctl._client().volumes.get(self.volume).remove(force=True)
        except NotFound:
            pass

    def restart(self) -> None:
        if self._limits is None:
            raise RuntimeError("restart() before start()")
        self.start(self._limits)

    def _host(self) -> str:
        return dockerctl.container_ip(self.container_name)

    def connect(self) -> DuckDBConnection:
        return DuckDBConnection(self._host())

    def alive(self) -> bool:
        try:
            conn = self.connect()
        except Exception:  # noqa: BLE001 - not reachable means not alive
            return False
        try:
            resp = conn.execute("SELECT 1", PROBE_TIMEOUT_S)
            return resp["status"] == "ok"
        except Exception:  # noqa: BLE001
            return False
        finally:
            conn.close()

    def crash_info(self) -> dict:
        try:
            return dockerctl.container_state(self.container_name)
        except Exception as exc:  # noqa: BLE001
            return {"status": "missing", "error": str(exc)}

    # versions and DDL
    def versions(self) -> dict:
        conn = self.connect()
        try:
            resp = conn.execute("SELECT version()", PROBE_TIMEOUT_S)
        finally:
            conn.close()
        return {"duckdb": resp["rows"][0][0], "image": self.image, "requested": self.version}

    def _run(self, sql: str, timeout_s: int = 600) -> dict:
        conn = self.connect()
        try:
            return conn.execute(sql, timeout_s)
        finally:
            conn.close()

    def create_schema(self, sf: int, kind: Literal["engine", "rowstore"]) -> None:
        if kind != "engine":
            raise ValueError(NO_ROW_STORE)
        for table, cols in TPCH_DDL.items():
            self._run(f"DROP TABLE IF EXISTS {table}")
            resp = self._run(f"CREATE TABLE {table} ({cols})")
            if resp["status"] != "ok":
                raise RuntimeError(f"create {table}: {resp['error']}")

    # loading
    def load_bulk(self, data: DatasetPaths) -> LoadOutcome:
        """COPY each table from its CSV parts (``data.tables``: table -> files under
        ``data_dir``; '|' delimited, no header, no trailing delimiter)."""
        total = 0.0
        for table in TPCH_DDL:
            for part in data.tables[table]:
                resp = self._run(
                    f"COPY {table} FROM '{self._container_path(part)}' "
                    "(DELIMITER '|', HEADER false)",
                    LOAD_TIMEOUT_S,
                )
                total += resp["elapsed_s"] or 0.0
                if resp["status"] != "ok":
                    return LoadOutcome(resp["status"], total, f"{table}: {resp['error']}")
        return self._check_rows(data, total)

    def _check_rows(self, data: DatasetPaths, loaded_s: float) -> LoadOutcome:
        """Row counts against the manifest (not in the adapter's elapsed_s)."""
        expected = expected_rows(data)
        actual = {}
        for table in TPCH_DDL:
            resp = self._run(f"SELECT COUNT(*) FROM {table}")
            if resp["status"] != "ok":
                return LoadOutcome(resp["status"], loaded_s, f"count {table}: {resp['error']}")
            actual[table] = int(resp["rows"][0][0])
        if actual != expected:
            return LoadOutcome("error", loaded_s, "row count does not match the manifest",
                               {"kind": "row_count", "expected": expected, "actual": actual})
        return LoadOutcome("ok", loaded_s, None, {"rows": actual})

    def _container_path(self, path: Path | str) -> str:
        if self.data_dir is None:
            raise RuntimeError("data_dir is required for load_bulk")
        return f"{DATA_MOUNT}/{Path(path).resolve().relative_to(self.data_dir.resolve())}"

    def load_convert(self, data: DatasetPaths) -> LoadOutcome:
        reason = "plain DuckDB has no row store to convert from"
        return LoadOutcome("n/a", None, None, {"reason": reason})

    # queries
    # Throughput streams: one TCP connection (one server-side cursor) per stream thread, opened
    # and warmed before the timed region. The server enforces the per-query deadline itself.
    def open_stream(self, timeout_s: int) -> None:
        self.close_stream()
        self._stream.conn = None
        self._stream.streaming = True  # a failed open is redone by execute(), on the stream path
        conn = self.connect()
        self._stream.conn = conn
        conn.execute("SELECT 1", timeout_s)

    def close_stream(self) -> None:
        self._stream.streaming = False
        self._drop_stream_conn()

    def _drop_stream_conn(self) -> None:
        conn = getattr(self._stream, "conn", None)
        self._stream.conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def execute(self, sql: str, timeout_s: int) -> QueryOutcome:
        streaming = getattr(self._stream, "streaming", False)
        try:
            conn = self._stream.conn if streaming and self._stream.conn else self.connect()
            if streaming:
                self._stream.conn = conn
        except Exception as exc:  # noqa: BLE001
            return self._connection_failure(exc)
        try:
            resp = conn.execute(sql, timeout_s)
        except TimeoutError:
            if streaming:
                self._drop_stream_conn()
            return QueryOutcome("timeout", None, None, set(),
                                "no response from server within timeout + margin", "duckdb")
        except Exception as exc:  # noqa: BLE001
            if streaming:
                self._drop_stream_conn()
            return self._connection_failure(exc)
        finally:
            if not streaming:
                conn.close()
        if resp["status"] != "ok":
            if streaming:
                self._drop_stream_conn()
            return QueryOutcome(resp["status"], resp["elapsed_s"], None, set(),
                                resp["error"], "duckdb", stopped=resp["status"] == "timeout")
        rows = [tuple(decode(v) for v in row) for row in resp["rows"]]
        numeric = numeric_cols_from_types([c["type"] for c in resp["columns"]])
        return QueryOutcome("ok", resp["elapsed_s"], rows, numeric, None, "duckdb",
                            [c["name"] for c in resp["columns"]])

    def _connection_failure(self, exc: Exception) -> QueryOutcome:
        return classify_connection_failure(self.crash_info, exc, "duckdb")

    def cancel(self) -> bool:
        """Interrupt queries that are already past their deadline. Queries still within
        their own timeout (other streams) are never touched."""
        try:
            conn = self.connect()
            try:
                return conn.request({"cmd": "interrupt_expired"}, PROBE_TIMEOUT_S)["status"] == "ok"
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return False

    def pushdown(self, sql: str) -> Literal["duckdb", "fallback", "unknown"]:
        return "duckdb"

    # storage and capability
    def storage_bytes(self) -> int:
        """Database file plus its WAL; measures only, never checkpoints."""
        c = dockerctl._client().containers.get(self.container_name)
        code, out = c.exec_run(
            ["sh", "-c", f"stat -c %s {DB_FILE} {DB_FILE}.wal 2>/dev/null || true"]
        )
        return sum(int(x) for x in out.decode().split())

    def storage_breakdown(self) -> dict:
        c = dockerctl._client().containers.get(self.container_name)
        code, out = c.exec_run(["sh", "-c", FIND_COMMAND.format(dir=DB_DIR)])
        if code != 0:
            raise RuntimeError(f"listing {DB_DIR} failed ({code}): {out.decode(errors='replace')}")
        return summarize(parse_listing(out.decode(errors="replace")), classify_duckdb)

    def checkpoint(self) -> str | None:
        resp = self._run("CHECKPOINT", LOAD_TIMEOUT_S)
        if resp["status"] != "ok":
            raise RuntimeError(f"CHECKPOINT failed: {resp['error']}")
        return None

    def supports(self, measure: str) -> tuple[bool, str | None]:
        if measure in ("M11", "M12"):
            return False, NO_ROW_STORE
        if measure in ("M5", "M8"):
            return False, NO_COMPAT
        return True, None
