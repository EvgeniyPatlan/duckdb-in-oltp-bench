# pg_ducklake v1.0.3 (PostgreSQL 18)

Image `dbench/pgducklake:18-v1.0.3`, built from the `v1.0.3` git tag with the project's own
Dockerfile (`systems/pgducklake/build.sh`; roughly 15-20 minutes). Docker Hub has no v1.0.3 image
(`pgducklake/pgducklake` stops at `18-v1.0.2`; `18-main` is unreleased), so the image is local and
`versions.image_digest` is its local image id, not a registry digest. The build succeeded; no
fallback to `18-v1.0.2` was needed. The image carries PostgreSQL 18.6, pg_ducklake 1.0.3 and a
bundled DuckDB 1.5.3, reported by `versions()` (server, extension, DuckDB via
`ducklake.query('SELECT version()')`, image id).

pg_ducklake tables are DuckLake tables: `CREATE TABLE ... USING ducklake`. Data is Parquet files
on the data volume (`$PGDATA/pg_ducklake/`, the extension's default local path, so `du` of
`$PGDATA` counts it) with the catalog in Postgres. DuckDB scans and writes them.

## Configuration

Started with `-c config_file=/etc/postgresql/postgresql.conf`, rendered from `postgresql.conf` and
mounted read-only; it sets `shared_preload_libraries = 'pg_ducklake'`. Authentication is `trust`
on the private docker network, `shm_size` is 8 GB. Rendered by
`duckdb_pg_settings(limits, "pgducklake")` in `dbench/systems/baseline_conf.py`; at 64 GB and
16 cores:

| Setting | Value | Reason |
|---|---|---|
| `shared_buffers` | mem_gb/16 = `4GB` | Only the heap/catalog side uses it (the InnoDB-pool rule of the MySQL engines). |
| DuckDB memory limit | 0.75 x container GB = `48GB` (`49152MiB`) | DuckDB holds the data path, as for the MySQL-family engines. |
| DuckDB threads | cores in the DB cpuset = `16` | One thread per core. |
| DuckDB `temp_directory` | `/var/lib/postgresql/duckdb_tmp` | Spill directory made explicit, on the data volume. |
| `effective_cache_size`, `max_parallel_workers_per_gather`, `max_parallel_workers`, `max_worker_processes`, `work_mem`, `max_connections`, `max_wal_size`, `checkpoint_timeout` | as in the Postgres baseline | Same formulas as `systems/postgres/`. |

**The DuckDB settings are applied per session, not in `postgresql.conf`.** pg_ducklake has no
`duckdb.max_memory`, `duckdb.threads` or `duckdb.temporary_directory` GUCs (those belong to
pg_duckdb; `SET ducklake.threads` is accepted as a placeholder but changes nothing). Left alone,
the embedded DuckDB runs with `threads = 1` and a memory limit of 80% of the host's RAM. The adapter
therefore runs `SELECT ducklake.raw_query('SET threads = N; SET memory_limit = ...MiB; SET
temp_directory = ...')` on every query, load and stream connection (`connect()`), before the timed
region. Memory is given in MiB because DuckDB reads `GB` as 10^9 bytes. The spill directory is
created by DuckDB when it first spills; no spill happened at SF1, so its use is not yet exercised.

Everything else is at the extension's defaults, including the **background maintenance worker**
(`ducklake.maintenance_enabled = on`, a cycle every 60 s: flush inlined data, merge files, expire
snapshots) and `data_inlining_row_limit = 10`, so the 5-row `region` table is stored inline in the
catalog. Maintenance can use CPU during a run; it was not disabled (no tuning beyond the formulas).

## Sessions and concurrency

Each Postgres backend holds its own DuckDB instance (and its cache). `execute()` and `pushdown()`
run on one persistent session connection per calling thread, with the DuckDB settings applied once
when it opens; it is dropped and reopened after an error, a timeout, a restart or a crash. The
settings apply per backend: with N concurrent streams the memory in use can reach N times the
limit, so for the throughput run (Task 16) the per-backend memory will be the formula divided by
the number of streams.

## Schema and loading

- `bench_schema_kind = "engine"`: the TPC-H tables with `USING ducklake`, primary keys, no
  foreign-key indexes. **Primary keys are accepted but not enforced**: `CREATE TABLE` succeeds and
  creates an (unmaintained) btree definition in the Postgres catalog, but duplicate keys insert
  without error (checked on the real server). They are kept because the schema rule is "PKs where
  the system accepts them"; no uniqueness is enforced or relied on.
- **`COPY` into a DuckLake table does not work for this load**: `COPY t FROM '/data/...'` reports
  `COPY 3` but the rows are not visible to any query (`COPY FROM STDIN` needs a prepared inlined
  data table). So `load_bulk` uses the documented DuckDB reader path: per CSV part
  `INSERT INTO t SELECT r['columnN']::<type>, ... FROM ducklake.read_csv(path, header := false,
  delim := '|', quote := '"', all_varchar := true) r`, reading every field as text and casting to
  the column type by position. Row counts are checked against the manifest, then `ANALYZE` per
  table (`VACUUM` is a no-op on DuckLake tables). Load time covers the inserts and the analyzes.
- `load_convert` (M11): plain heap tables with the engine schema are loaded with `COPY` into a
  staging schema, then `VACUUM (FREEZE, ANALYZE)` on the staging heap tables (as the Postgres load path), then
  `INSERT INTO ducklake_t SELECT * FROM heap_t` per table (DuckDB reads the heap) and `ANALYZE`. `elapsed_s` is the conversion plus analyze; the staging load is
  `extra.rowstore_load_s` (COPY plus the staging vacuum, timed after the DDL). The staging schema is dropped afterwards.

## Pushdown detection (M12)

Marker: the plan text of a plan-only `EXPLAIN <query>` contains `Custom Scan (DuckLakeScan)`
(`Custom Scan (DuckLakeScan)  (cost=0.00..0.00 rows=0 width=0)`, then `DuckDB Execution Plan:` and
a `DUCKLAKE_SCAN` node); pg_duckdb uses `DuckDBScan`, and the detector accepts either. Any other
plan is `fallback`. The `EXPLAIN` runs on the query's own connection before the timer and the
timed region start. pg_ducklake needs no `force_execution`: queries over DuckLake tables are
always planned into DuckDB.

## Timeouts

`statement_timeout` fires for a pushed query (docker test: a 3-way cross join of 3M rows times out
at 5 s). The cancel is reported as `InternalError_: ... Query cancelled`, which the adapter records
as `timeout` once the deadline has passed. Nothing lingered afterwards. The client watchdog
(`pg_cancel_backend`, then `pg_terminate_backend` after 10 s) stays in place as a backstop.

## Other notes

`storage_bytes()` is `du -sb $PGDATA` after a `CHECKPOINT`: Parquet data, catalog and WAL. The 22
DuckDB query texts run unchanged; failed queries, mismatches and fallbacks are results.

## M11 (freshness)

`supports("M11")` is `(True, None)`: the project documents heap-to-DuckLake change capture
(pg_duckpipe, a separate project that is not in this image), so the measure applies. The freshness
mechanism is to be designed post-talk; `load_convert` is a one-time copy, not a freshness path.
