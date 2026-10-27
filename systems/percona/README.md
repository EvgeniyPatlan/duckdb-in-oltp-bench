# Percona Server 9.7 + DuckDB engine (`ENGINE=DuckDB`)

Image `perconalab/ducksdb-mysql-engine:9.7-duckdb-v0.2.3`, pinned by digest
`sha256:5e4af8b2bede83db7b296be977a9c22fb30e8a05a565e3b3b0fe08fea9a96640` (the image reference
in `dbench/systems/percona.py` carries it; the resolved digest is also recorded in every result as
`versions.image_digest`). Track A. The dataset is mounted read-only at `/data`.

## How settings are applied

The image entrypoint starts `mysqld --no-defaults ...`, so a `my.cnf` mount is **ignored**. Every
server setting is therefore a mysqld command-line argument, rendered from `mysqld-args.j2` with
the container limits and passed as the container command. Two engine settings are environment
variables. The container runs as root (the image's default); the read-only `/data` mount causes no
chown problem because the entrypoint only creates the datadir.

## Non-default settings

At 64 GB and 16 cores (mem_gb = container memory):

| Setting | Value | Reason |
|---|---|---|
| env `DUCKSDB_MEMORY_LIMIT` | 0.75 x mem_gb = `48GB` | Caps DuckDB's memory (the engine that runs the queries) at 75% of the container, the same share the InnoDB baseline gives its buffer pool; the rest covers MySQL, connections and the OS. |
| `innodb_buffer_pool_size` | mem_gb/16 = `4G` | The InnoDB side only holds the catalog and row-store staging (the `load_convert` path); the data lives in DuckDB, so a large pool would only take memory from DuckDB. |
| `innodb_parallel_read_threads` | cores in the DB cpuset = `16` | Same as the InnoDB baseline. |
| `secure_file_priv` | `/data` | Server-side `LOAD DATA INFILE` may read only the read-only dataset mount. |
| `local_infile` | `OFF` | Loading is server-side. |
| `max_connections` | `200` | Headroom for the 32-stream throughput run plus probes. |
| `join_buffer_size`, `sort_buffer_size` | `256M` | Same as the InnoDB baseline. |
| `tmp_table_size`, `max_heap_table_size`, `temptable_max_ram` | mem_gb/16 = `4G` | Same as the InnoDB baseline. |
| `innodb_redo_log_capacity` | mem_gb/8 = `8G` | Same as the InnoDB baseline. |
| env `DUCKSDB_TEMP_DIR` | `/var/lib/mysql/duckdb_tmp` | Explicit spill location for DuckDB, on the datadir volume (created at start) instead of the engine default; same spill-location rule for all engines. |
| env `MYSQL_ROOT_PASSWORD` | empty | Passwordless root, as every MySQL-family adapter. |

All formulas except the buffer pool are shared with the InnoDB baseline (`baseline_conf.py`).
Everything else, including the binary log, is the server default.

## Disclosed limitations

- **DuckDB threads: no setting.** The v0.2.3 engine exposes no DuckDB thread-count knob, so DuckDB
  auto-detects the thread count from the container's CPU set (the 16 pinned cores). Neither a
  status variable nor a system variable shows the detected count, so it is not observable and
  is not recorded.
- **DuckDB version 1.5.3**, source:
  "stated by the engine maintainer; engine exposes no version at runtime".
  `versions()` records `duckdb: 1.5.3`, that `duckdb_source`, and
  `engine: ducksdb-mysql-engine v0.2.3`. The plain-DuckDB image `dbench/duckdb:1.5.3` (system
  `duckdb-1.5.3`) allows an overhead comparison on the same DuckDB version.
- `Ducksdb_pushdown_count` is a global counter; see Pushdown.

## Schema

`create_schema(sf, "engine")`: the shared TPC-H DDL with `ENGINE=DuckDB` and primary keys. The
engine accepts `PRIMARY KEY` declarations (`SHOW CREATE TABLE` keeps them). No other indexes.
`kind="rowstore"` is plain InnoDB with foreign-key indexes. Each MySQL schema is one DuckDB file,
`<datadir>/tpch.duckdb`. The image creates no database; the adapter creates `tpch` on first
connect.

## Loading

- `load_bulk`: engine schema, `LOAD DATA INFILE` per CSV part (the server's COPY fast path), row
  count check against the manifest, then `ANALYZE TABLE` on all tables (supported). Same procedure
  as every MySQL-family adapter.
- `load_convert`: drops and recreates the tables as InnoDB with the engine schema (primary keys,
  no foreign-key indexes, so the converted tables equal the bulk-loaded ones), loads them with
  `LOAD DATA INFILE` (timed as `extra.rowstore_load_s`), checks row counts, then runs
  `ALTER TABLE t ENGINE=DuckDB` per table, then `ANALYZE TABLE` on all tables, as `load_bulk` does, so both paths end in the same state. `elapsed_s` is the conversion plus analyze, not the staging load.

## Pushdown

`execute()` reads `SHOW GLOBAL STATUS LIKE 'Ducksdb_pushdown_count'` on the query's own
connection before and after the query (outside the timed region) and sets `pushed` to `duckdb`
if it rose, `fallback` if not, `unknown` if the counter could not be read. `pushdown(sql)` does
the same for an arbitrary statement. The counter is server-wide, so the delta is attributable to
one query only while a single stream runs (the power run); under concurrent streams it can be
ambiguous.

## Measurement notes

Queries run with the session `max_execution_time` set to the per-query timeout; `cancel()` is
`KILL QUERY`. `storage_bytes()` is `du -sb` of the datadir excluding `binlog.*`, so it counts the
`.duckdb` file and its WAL plus the InnoDB catalog, redo log and tablespaces. Queries use the
mechanical MySQL dialect translation shared with the InnoDB baseline.
