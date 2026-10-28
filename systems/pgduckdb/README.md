# pg_duckdb 1.1.1 (PostgreSQL 18)

Image `pgduckdb/pgduckdb:18-v1.1.1` (digest recorded in every result as `versions.image_digest`).
It ships PostgreSQL 18.1 and bundles DuckDB 1.4.3, which is older than the DuckDB used by the
native `duckdb-<version>` system, so small plan and timing differences against it are expected.
`versions()` reports the server version, the extension version (`pg_duckdb 1.1.0`, the SQL
extension version of release 1.1.1), the DuckDB version (`duckdb.query('SELECT version()')`) and
the image digest.

pg_duckdb lets DuckDB run a query over ordinary Postgres heap tables: the heap is read page by
page through the Postgres buffer manager. There is no second copy of the data.

## Configuration

Started with `-c config_file=/etc/postgresql/postgresql.conf`, the file rendered from
`postgresql.conf` and mounted read-only. It sets `shared_preload_libraries = 'pg_duckdb'` (the image
appends that to the default file, which is replaced here). Authentication is `trust` on the private
docker network and `shm_size` is 8 GB. Every setting has a one-line reason comment in the file.

Rendered from the container limits (`duckdb_pg_settings(limits, "pgduckdb")` in
`dbench/systems/baseline_conf.py`); at 64 GB and 16 cores:

| Setting | Value | Reason |
|---|---|---|
| `shared_buffers` | 0.25 x container GB = `16GB` | DuckDB reads Postgres heap pages through it, so it keeps the Postgres baseline value. |
| `duckdb.max_memory` | 0.5 x container GB = `32GB` | The rest of the container goes to `shared_buffers` and the page cache. |
| `duckdb.threads` | cores in the DB cpuset = `16` | One DuckDB thread per core (default `-1`). |
| `duckdb.temporary_directory` | `/var/lib/postgresql/duckdb_tmp` | Spill directory made explicit, on the data volume. |
| `duckdb.threads_for_postgres_scan`, `duckdb.max_workers_per_postgres_scan` | cores = `16` | Heap-scan parallelism sized to the container (default 2), the analogue of the baseline's `max_parallel_workers_per_gather = cores`. |
| `effective_cache_size`, `max_parallel_workers_per_gather`, `max_parallel_workers`, `max_worker_processes`, `work_mem`, `max_connections`, `max_wal_size`, `checkpoint_timeout` | as in the Postgres baseline | Same formulas as `systems/postgres/`. |

Per session, every benchmark query runs after `SET duckdb.force_execution = true`, so DuckDB takes
the query even where the planner would not choose it.

DuckDB's memory limit was checked from DuckDB itself (`duckdb.query('SELECT
current_setting(''memory_limit'')')`): `duckdb.max_memory = 4GB` reads `4.0 GiB`, so the `MB`/`GB`
unit is binary and no conversion is needed; `duckdb.threads` reads back as set.

## Sessions and concurrency

Each Postgres backend holds its own DuckDB instance (and its cache). `execute()` and `pushdown()`
therefore run on one persistent session connection per calling thread, set up once
(`SET duckdb.force_execution`), so warm runs keep the backend's DuckDB state. The session is
dropped and reopened after an error, a timeout, a restart or a crash. `duckdb.max_memory` and
`duckdb.threads` apply per backend: with N concurrent streams the memory in use could reach N times
the limit, so the throughput run (M4) divides it: before opening N streams (N > 1) the adapter
runs `SET duckdb.max_memory = '<0.5 * mem_gb / N>MB'` on every session (the GUC is
superuser-settable per session, checked on the real server, and DuckDB picks it up when the
backend's instance starts). The value in force is recorded as `memory_per_backend` in the M4 record.
Threads are not divided. With one stream the `postgresql.conf` value applies.

## Schema and loading

- `bench_schema_kind = "rowstore"`: the same tables as the Postgres baseline (primary keys plus a
  plain index on every foreign-key column), because the point of pg_duckdb is querying normal
  Postgres tables. No schema deviations.
- `load_bulk` is the Postgres baseline path: `COPY` per CSV part, row-count check, then
  `VACUUM (FREEZE, ANALYZE)` per table (`force_execution` is off while loading).
- `load_convert` (M11) is n/a: "queries normal Postgres tables directly; no conversion step".
  `supports("M11")` is `(False, "no separate DuckDB copy to keep fresh")`.

## Pushdown detection (M12)

Marker: the plan text of a plain, plan-only `EXPLAIN <query>` contains `Custom Scan (DuckDBScan)`
(on the real server: `Custom Scan (DuckDBScan)  (cost=0.00..0.00 rows=0 width=0)` followed by
`DuckDB Execution Plan:`). Any other plan is `fallback`. The `EXPLAIN` runs on the query's own
connection after `SET duckdb.force_execution` and before the timer and the timed region start, so
it is never inside the measured time and never executes the query.

## Timeouts

`statement_timeout` is set per session and fires for a pushed query (verified by a docker test:
a 3-way cross join of 3M rows times out at 5 s). DuckDB's executor reports the cancel as
`InternalError_: ... Query cancelled` (SQLSTATE XX000, not 57014), so the adapter records it as
`timeout` when the message says cancelled and the deadline has passed. No query lingered after the
cancel, so no restart is needed. The client watchdog (`pg_cancel_backend`, then
`pg_terminate_backend` after 10 s) from the Postgres family stays in place as a backstop.

## Storage and other notes

`storage_bytes()` is `du -sb $PGDATA` after a `CHECKPOINT` (WAL counted, as for the baseline).
The 22 DuckDB query texts run unchanged. Failed queries, mismatches and fallbacks are results; no
query is rewritten.

## Track B variant (`pg_duckdb-trackb`)

Track B runs the canonical DuckDB text over Parquet (`dbench/queries/trackb.py`) through the raw
entry point: `SELECT * FROM duckdb.query($dbq$<sql>$dbq$)`. Same image and `postgresql.conf` as
above; no tables are loaded and nothing about Track A changes. The DuckDB behind it is the bundled
1.4.3, not the 1.5.5 of the other Track B systems. See `systems/villagesql/README.md` for why
Track B exists.
