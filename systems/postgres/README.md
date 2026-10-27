# PostgreSQL 18 baseline

Image `postgres:18` (digest recorded in every result as `versions.image_digest`), started with
`-c config_file=/etc/postgresql/postgresql.conf`, the file rendered from `postgresql.conf` and
mounted read-only. The dataset is mounted read-only at `/data`, `shm_size` is 8 GB, and
authentication is `trust` on the private docker network.

## Non-default settings

Rendered from the container limits (`dbench/systems/baseline_conf.py`); at 64 GB and 16 cores:

| Setting | Value | Reason |
|---|---|---|
| `listen_addresses` | `*` | The runner container connects over the docker bridge (the custom config file replaces the default one). |
| `shared_buffers` | 0.25 x container GB = `16GB` | The usual starting point; the OS page cache holds the rest. |
| `effective_cache_size` | 0.75 x container GB = `48GB` | Planner's estimate of buffer plus page cache. |
| `max_parallel_workers_per_gather` | cores in the DB cpuset = `16` | One query may use every core given to the database. |
| `max_parallel_workers` | `16` | Cap across sessions: one worker per core. |
| `max_worker_processes` | `16` | Must be at least `max_parallel_workers`. |
| `work_mem` | `256MB` | Analytical sorts and hashes stay in memory instead of spilling at the 4MB default. |
| `max_wal_size` | mem_gb/4 = `16GB` | Keeps checkpoints from throttling bulk load at the 1GB default. |
| `checkpoint_timeout` | `30min` | Timed checkpoints do not interrupt load and query runs. |
| `max_connections` | `200` | Headroom over the 32-stream throughput run plus probe and cancel connections. |

Everything else stays at the PostgreSQL 18 default (including JIT).

## Loading and measurement notes

- `COPY t FROM '/data/...' WITH (FORMAT csv, DELIMITER '|', QUOTE '"')` per CSV part.
- Row counts are checked against the manifest, then `VACUUM (FREEZE, ANALYZE)` runs per table.
  Load time covers the copies and the vacuums, not the count check. The vacuum sets hint bits
  and freezes tuples so cold runs do not pay for it.
- Tables are loaded with the primary key and foreign-key indexes already in place.
- Queries run after `SET statement_timeout`; `cancel()` is `pg_cancel_backend` from a second
  connection.
- `storage_bytes()` is `du -sb $PGDATA` after a `CHECKPOINT`. Storage rule for the Postgres
  family: WAL is counted (replication logs are excluded in the MySQL family, redo/WAL are counted).
- Queries also have a client-side deadline at timeout + 30 s: `pg_cancel_backend`, then
  `pg_terminate_backend` after 10 s, recorded as `timeout`. This protects engines that ignore
  `statement_timeout`.

## Schema

TPC-H tables with primary keys, plus a plain index on every foreign-key column (row-store
baseline rule); no other indexes and no declared foreign-key constraints. Strings are
`VARCHAR(n)` with the spec lengths. The default collation of the image is kept. There are no
schema deviations.

## Dialect

The 22 DuckDB query texts run unchanged.

M11 and M12 are n/a: there is no DuckDB copy to convert or push down to.
