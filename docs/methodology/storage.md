# Storage size and load paths (M2, M3)

## What is measured

M3 records the size of each system's data directory in bytes, at two points:

| item | taken |
|---|---|
| `after_load` | right after the bulk load, before any query and before a checkpoint |
| `after_checkpoint` | after the system's checkpoint statement |

A system without a checkpoint statement gets `n/a` for `after_checkpoint` with the reason; its
`after_load` value is still recorded.

The value of each cell is the whole data directory, measured inside the database container with
`du -sb` (apparent bytes). Replication logs are left out (MySQL-family `binlog.*`; every server
runs with binary logging off, so these files are normally absent). Redo log and WAL files are
counted: they are on the data volume and the system cannot run without them. Plain DuckDB is the
exception: its value is the database file plus its `.wal` file, the only files the engine keeps.

Checkpoint statements:

| system | statement |
|---|---|
| plain DuckDB | `CHECKPOINT` |
| PostgreSQL, pg_duckdb, pg_ducklake | `CHECKPOINT` |
| AliSQL | `CALL dbms_duckdb.query('CHECKPOINT')` (the server rejects a bare `CHECKPOINT`) |
| MySQL InnoDB | none: `n/a`, InnoDB flushes pages continuously |
| Percona | none: `n/a`, no statement or function exists |
| MariaDB | none: `n/a`, `run_in_duckdb()` is refused unless the server runs with `--duckdb-allow-run-in-duckdb`, which is off by default and not changed here |

## Breakdown

Each M3 record carries `extra.breakdown`, the same total split by file:

| key | contents |
|---|---|
| `data_bytes` | DuckDB database files and table data files |
| `redo_wal_bytes` | redo log and write-ahead log (preallocated or not) |
| `temp_bytes` | DuckDB spill and temporary directories, InnoDB temporary tablespace |
| `other_bytes` | everything else: catalogs, config, doublewrite files, directory entries |

The buckets add up to the total (`du -sb` counts directory entries, which land in
`other_bytes`; binlogs are excluded from all four). The listing is taken with
`find <dir> -printf '%y %s %P\n'` in the container, and each path is classified by these rules.

MySQL family (MySQL, Percona, MariaDB, AliSQL), under `/var/lib/mysql`:

- `redo_wal_bytes`: `#innodb_redo/` (MySQL 8.0.30 and later, including its spare files),
  `ib_logfile*` (MariaDB), and DuckDB `*.wal`
- `temp_bytes`: `duckdb_tmp/`, directories ending in `.tmp`, `ibtmp1`, `#innodb_temp/`
- `data_bytes`: `*.duckdb` and `duckdb.db*` (DuckDB files), `ibdata*`, `*.ibd`
  (InnoDB system and user tablespaces: they hold rows and the dictionary, and are not logs)
- undo tablespaces (`undo*`) are transient and count as `other_bytes`
- a DuckDB `.wal` file counts as `redo_wal_bytes`; engines without a checkpoint statement may
  keep rows that are not yet checkpointed there
- excluded: `binlog.*`
- everything else is `other_bytes`

PostgreSQL family (PostgreSQL, pg_duckdb, pg_ducklake), under `$PGDATA`:

- `redo_wal_bytes`: `pg_wal/`
- `temp_bytes`: `pgsql_tmp` and `duckdb_tmp` directories
- `data_bytes`: `base/` (heap, index and catalog files) and `pg_ducklake/` / `pg_duckdb/` (the
  DuckLake Parquet files)
- everything else is `other_bytes`

The DuckDB spill directory configured for the Postgres engines lies beside `$PGDATA`
(`/var/lib/postgresql/duckdb_tmp`), outside both the total and the breakdown.

Plain DuckDB, under `/var/lib/duckdb`: the `.duckdb` file is `data_bytes`, the `.wal` file is
`redo_wal_bytes`, `*.tmp` is `temp_bytes`; directory entries are not counted.

The redo log is preallocated on InnoDB-based servers, so `redo_wal_bytes` is large and nearly
constant whatever the data size; compare `data_bytes` when comparing engines.

## Load paths (M2)

| item | what the time covers |
|---|---|
| `load_bulk` | wall time of `create_schema` plus the system's bulk load, including the row-count check against the dataset manifest and the statistics step (ANALYZE, or VACUUM FREEZE for Postgres) |
| `load_convert` | only the conversion of row-store tables to the DuckDB engine, plus the analyze step; the row-store staging load is in `extra.rowstore_load_s` |

`load_bulk` is the same load that sets up the other measures of the system. It is timed once. A
resumed campaign that has to load the data again to run missing cells does not record it again.
`load_convert` runs after all the system's other measures, on a fresh datadir that is removed
afterwards. Systems with no DuckDB engine or no row store to convert from record `n/a` with the
reason. A row-count mismatch is recorded as `error` with `extra.kind = "row_count"`.
