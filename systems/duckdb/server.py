"""Tiny TCP JSON-lines query server around one DuckDB database file.

Each TCP connection gets its own DuckDB cursor on the shared database.
Request (one JSON object per line):  {"sql": "...", "timeout_s": 900}
                                      {"cmd": "interrupt"}   (interrupt every running query)
Response: {"status": "ok"|"timeout"|"error", "elapsed_s": float, "columns":
          [{"name", "type"}], "rows": [[...]], "error": str|null}

Elapsed time is measured around execute + fetch only. Values keep their types
across JSON: Decimal -> {"$dec"}, date -> {"$date"}, datetime -> {"$ts"},
time -> {"$time"}, bytes -> {"$hex"}, anything else non-JSON -> {"$str"}.
"""
import datetime as dt
import decimal
import json
import os
import socketserver
import threading
import time

import duckdb

DB_PATH = os.environ.get("DBENCH_DB_PATH", "/var/lib/duckdb/tpch.duckdb")
PORT = int(os.environ.get("DBENCH_PORT", "9999"))

_active: set = set()
_active_lock = threading.Lock()


def encode(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return {"$dec": str(v)}
    if isinstance(v, dt.datetime):
        return {"$ts": v.isoformat()}
    if isinstance(v, dt.date):
        return {"$date": v.isoformat()}
    if isinstance(v, dt.time):
        return {"$time": v.isoformat()}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$hex": bytes(v).hex()}
    return {"$str": str(v)}


def run_query(cur, sql, timeout_s):
    timed_out = threading.Event()

    def on_timeout():
        timed_out.set()
        cur.interrupt()

    timer = threading.Timer(timeout_s, on_timeout)
    with _active_lock:
        _active.add(cur)
    timer.start()
    start = time.perf_counter()
    try:
        cur.execute(sql)
        desc = cur.description or []
        rows = cur.fetchall() if desc else []
        elapsed = time.perf_counter() - start
    except Exception as exc:  # noqa: BLE001 - reported to the client
        elapsed = time.perf_counter() - start
        status = "timeout" if timed_out.is_set() or isinstance(
            exc, duckdb.InterruptException) else "error"
        return {"status": status, "elapsed_s": elapsed, "columns": [], "rows": [],
                "error": f"{type(exc).__name__}: {exc}"}
    finally:
        timer.cancel()
        with _active_lock:
            _active.discard(cur)
    return {
        "status": "ok", "elapsed_s": elapsed, "error": None,
        "columns": [{"name": d[0], "type": str(d[1])} for d in desc],
        "rows": [[encode(v) for v in row] for row in rows],
    }


def interrupt_all():
    with _active_lock:
        cursors = list(_active)
    for cur in cursors:
        cur.interrupt()
    return {"status": "ok", "interrupted": len(cursors)}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        cur = self.server.db.cursor()
        try:
            for line in self.rfile:
                if not line.strip():
                    continue
                try:
                    req = json.loads(line)
                    if req.get("cmd") == "interrupt":
                        resp = interrupt_all()
                    else:
                        resp = run_query(cur, req["sql"], float(req.get("timeout_s", 900)))
                except Exception as exc:  # noqa: BLE001 - malformed request
                    resp = {"status": "error", "elapsed_s": 0.0, "columns": [], "rows": [],
                            "error": f"bad request: {exc}"}
                self.wfile.write(json.dumps(resp).encode() + b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            cur.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    config = {}
    threads = os.environ.get("DBENCH_THREADS")
    if threads:
        config["threads"] = int(threads)
    mem = os.environ.get("DBENCH_MEMORY_LIMIT")
    if mem:
        config["memory_limit"] = mem
    # Settings go in at open time: some snapshot builds fail to open without a memory limit.
    db = duckdb.connect(DB_PATH, config=config)
    server = Server(("0.0.0.0", PORT), Handler)
    server.db = db
    server.serve_forever()


if __name__ == "__main__":
    main()
