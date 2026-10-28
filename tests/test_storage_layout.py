"""Storage classification into data / redo-WAL / temp / other, from fake directory listings."""
from __future__ import annotations

import pytest

from dbench.systems.storage_layout import (
    classify_duckdb,
    classify_mysql,
    classify_postgres,
    parse_listing,
    summarize,
)

MYSQL_LISTING = """\
d 4096 
d 4096 #innodb_redo
f 104857600 #innodb_redo/#ib_redo10
f 104857600 #innodb_redo/#ib_redo11_tmp
f 12582912 ibdata1
f 25165824 undo_001
f 8388608 mysql.ibd
d 4096 tpch
f 98304 tpch/orders.ibd
f 52428800 tpch.duckdb
f 4096 tpch.duckdb.wal
d 4096 duckdb_tmp
f 1000 duckdb_tmp/duckdb_temp_storage_BLOCK-0.tmp
f 7777 binlog.000001
f 55 binlog.index
f 56 auto.cnf
f 1048576 #ib_16384_0.dblwr
"""


def test_parse_listing_reads_type_size_and_path():
    entries = parse_listing("f 12 a b/c\nd 4096 \nbad line\n")
    assert entries == [("f", 12, "a b/c"), ("d", 4096, "")]


def test_mysql_classification():
    got = summarize(parse_listing(MYSQL_LISTING), classify_mysql)
    assert got == {
        "data_bytes": 12582912 + 8388608 + 98304 + 52428800,
        "redo_wal_bytes": 4096 + 104857600 * 2 + 4096,  # redo dir entry, redo files, duckdb wal
        "temp_bytes": 4096 + 1000,
        "other_bytes": 4096 * 2 + 56 + 1048576 + 25165824,  # dirs, undo, other
    }


def test_mysql_binlog_is_excluded_from_every_bucket():
    got = summarize(parse_listing("f 7777 binlog.000001\nf 55 binlog.index\n"), classify_mysql)
    assert sum(got.values()) == 0


@pytest.mark.parametrize("path, bucket", [
    ("#innodb_redo/#ib_redo9", "redo_wal_bytes"),
    ("ib_logfile0", "redo_wal_bytes"),
    ("ibdata1", "data_bytes"),
    ("tpch/lineitem.ibd", "data_bytes"),
    ("undo_002", "other_bytes"),
    ("undo003", "other_bytes"),
    ("ibtmp1", "temp_bytes"),
    ("tpch.duckdb", "data_bytes"),
    ("duckdb.db", "data_bytes"),
    ("duckdb.db.wal", "redo_wal_bytes"),
    ("duckdb_tmp/x.tmp", "temp_bytes"),
    ("#innodb_temp/temp_1.ibt", "temp_bytes"),
    ("mysql/general.log", "other_bytes"),
])
def test_mysql_paths(path, bucket):
    assert classify_mysql(path, "f") == bucket


@pytest.mark.parametrize("path, kind, bucket", [
    ("pg_wal/000000010000000000000001", "f", "redo_wal_bytes"),
    ("pg_wal", "d", "redo_wal_bytes"),
    ("pg_wal/archive_status", "d", "redo_wal_bytes"),
    ("base/16384/16385", "f", "data_bytes"),
    ("pg_ducklake/public/lineitem/ducklake-1.parquet", "f", "data_bytes"),
    ("base", "d", "data_bytes"),
    ("duckdb_tmp/spill.tmp", "f", "temp_bytes"),
    ("global/pg_control", "f", "other_bytes"),
    ("pg_xact/0000", "f", "other_bytes"),
    ("postgresql.conf", "f", "other_bytes"),
])
def test_postgres_paths(path, kind, bucket):
    assert classify_postgres(path, kind) == bucket


def test_postgres_never_excludes_anything():
    assert classify_postgres("pg_replslot/x", "f") == "other_bytes"


@pytest.mark.parametrize("path, bucket", [
    ("tpch.duckdb", "data_bytes"),
    ("tpch.duckdb.wal", "redo_wal_bytes"),
    ("tpch.duckdb.tmp/duckdb_temp_storage-0.tmp", "temp_bytes"),
    ("", None),
    ("stray.txt", "other_bytes"),
])
def test_plain_duckdb_paths(path, bucket):
    assert classify_duckdb(path, "d" if not path else "f") == bucket


def test_summarize_always_has_the_four_keys():
    assert summarize([], classify_mysql) == {
        "data_bytes": 0, "redo_wal_bytes": 0, "temp_bytes": 0, "other_bytes": 0}
