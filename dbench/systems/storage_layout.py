"""Classify a data directory's files into the four storage buckets of measure M3.

``data_bytes``      table and DuckDB data files (see each ``classify_*`` for what counts)
``redo_wal_bytes``  InnoDB redo log, Postgres ``pg_wal``, the DuckDB write-ahead log
``temp_bytes``      DuckDB spill and temporary directories
``other_bytes``     everything else: catalogs, config, doublewrite, directory entries

Entries come from ``find <dir> -printf '%y %s %P\\n'`` (type, apparent size, path relative to the
directory), so the buckets add up to what ``du -sb`` reports. MySQL-family binlogs are left out
of every bucket, as they are left out of the total. Pure functions; the adapters run ``find``.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from fnmatch import fnmatch
from pathlib import PurePosixPath

BUCKETS = ("data_bytes", "redo_wal_bytes", "temp_bytes", "other_bytes")
FIND_COMMAND = "find {dir} -printf '%y %s %P\\n'"

Entry = tuple[str, int, str]  # (kind, bytes, relative path)
Classifier = Callable[[str, str], str | None]  # (relative path, kind) -> bucket, None: excluded


def parse_listing(text: str) -> list[Entry]:
    """Entries of a ``find`` listing; lines that do not parse are ignored."""
    entries = []
    for line in text.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        entries.append((parts[0], int(parts[1]), parts[2]))
    return entries


def summarize(entries: Iterable[Entry], classify: Classifier) -> dict[str, int]:
    totals = dict.fromkeys(BUCKETS, 0)
    for kind, size, path in entries:
        bucket = classify(path, kind)
        if bucket is not None:
            totals[bucket] += size
    return totals


def _parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(path).parts


def _name(path: str) -> str:
    return PurePosixPath(path).name


def classify_mysql(path: str, kind: str) -> str | None:
    """MySQL-family datadir (MySQL, Percona, MariaDB, AliSQL).

    data: DuckDB files (``*.duckdb``, ``duckdb.db*``) and InnoDB tablespaces (``ibdata*``,
    ``*.ibd``). The InnoDB system tablespace and user tablespaces hold rows and the
    dictionary and are not logs. Undo tablespaces (``undo*``) are transient and count as other.
    redo_wal: ``#innodb_redo/`` (MySQL 8.0.30+), ``ib_logfile*`` (MariaDB), and DuckDB ``*.wal``.
    temp: ``duckdb_tmp/``, DuckDB's ``*.tmp`` spill directories, and the InnoDB temporary
    tablespace (``ibtmp1``, ``#innodb_temp/``).
    Binlogs (``binlog.*``) are excluded."""
    parts = _parts(path)
    name = _name(path)
    if any(fnmatch(p, "binlog.*") for p in parts):
        return None
    if "#innodb_redo" in parts or name.startswith("ib_logfile"):
        return "redo_wal_bytes"
    if ("duckdb_tmp" in parts or "#innodb_temp" in parts or name == "ibtmp1"
            or any(p.endswith(".tmp") for p in parts)):
        return "temp_bytes"
    if name.endswith(".wal"):
        return "redo_wal_bytes"
    if (name.endswith(".duckdb") or name.startswith("duckdb.db") or name.startswith("ibdata")
            or name.endswith(".ibd")):
        return "data_bytes"
    return "other_bytes"


# Directories under PGDATA where the DuckDB-side extensions keep data files (pg_ducklake keeps
# its Parquet files in ``pg_ducklake/<schema>/<table>/``).
DUCKDB_DATA_DIRS: tuple[str, ...] = ("pg_duckdb", "pg_ducklake")


def classify_postgres(path: str, kind: str) -> str | None:
    """Postgres ``PGDATA`` (PostgreSQL, pg_duckdb, pg_ducklake).

    redo_wal: ``pg_wal/``. temp: ``pgsql_tmp`` and DuckDB spill (``duckdb_tmp``).
    data: ``base/`` (heap and index files, and the pg_ducklake catalog) plus the DuckLake and
    DuckDB data directories under PGDATA. Nothing is excluded. ``global/``, the commit logs
    and the config files are other."""
    parts = _parts(path)
    if parts[:1] == ("pg_wal",):
        return "redo_wal_bytes"
    if "pgsql_tmp" in parts or "duckdb_tmp" in parts:
        return "temp_bytes"
    if parts[:1] == ("base",) or any(p in DUCKDB_DATA_DIRS for p in parts[:1]):
        return "data_bytes"
    return "other_bytes"


def classify_duckdb(path: str, kind: str) -> str | None:
    """Plain DuckDB directory: the database file is data, ``.wal`` is the write-ahead log,
    ``*.tmp`` (the spill directory) is temp. Directory entries are not counted: the plain DuckDB
    total (``storage_bytes``) is the database file plus its WAL."""
    if kind == "d" and not path.endswith(".tmp"):
        return None
    parts = _parts(path)
    if any(p.endswith(".tmp") for p in parts):
        return "temp_bytes"
    if _name(path).endswith(".wal"):
        return "redo_wal_bytes"
    if _name(path).endswith(".duckdb"):
        return "data_bytes"
    return "other_bytes"
