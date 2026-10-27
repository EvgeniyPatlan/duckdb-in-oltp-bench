# MySQL 9.7 (InnoDB) baseline

Image `mysql:9.7` (digest recorded in every result as `versions.image_digest`), started as the
`mysql` user with the config rendered from `my.cnf` and mounted read-only at
`/etc/mysql/conf.d/dbench.cnf`. The dataset is mounted read-only at `/data`.

## Non-default settings

Rendered from the container limits (`dbench/systems/baseline_conf.py`); at 64 GB and 16 cores:

| Setting | Value | Reason |
|---|---|---|
| `innodb_buffer_pool_size` | 0.75 x container GB = `48G` | Most of the memory for the buffer pool; the rest covers connections and per-query buffers. |
| `innodb_parallel_read_threads` | cores in the DB cpuset = `16` | Parallel clustered-index scans use every core given to the database. |
| `secure_file_priv` | `/data` | Server-side `LOAD DATA INFILE` may read only the read-only dataset mount. |
| `local_infile` | `OFF` | Loading is server-side; client-side `LOCAL` loads stay off. |
| `max_connections` | `200` | Headroom over the 32-stream throughput run plus probe and cancel connections. |

Everything else, including the binary log, stays at the MySQL 9.7 default.

## Loading and measurement notes

- `LOAD DATA INFILE '/data/...' INTO TABLE t FIELDS TERMINATED BY '|' OPTIONALLY ENCLOSED BY '"'
  LINES TERMINATED BY '\n'` per CSV part. The generated data contains no backslashes, so the
  default escape character does not affect it.
- Row counts are checked against the manifest, then `ANALYZE TABLE` runs on all tables. Load time
  covers the loads and the analyze, not the count check.
- Tables are loaded with the primary key and foreign-key indexes already in place.
- Queries run with the session `max_execution_time` set to the per-query timeout; `cancel()` is
  `KILL QUERY` from a second connection.
- `storage_bytes()` is `du -sb` of the data directory without the `binlog.*` files, which are a
  logging artifact of the default configuration, not table storage.

## Schema

TPC-H tables with primary keys, plus a plain index on every foreign-key column (row-store
baseline rule); no other indexes and no declared foreign-key constraints. Strings are
`VARCHAR(n)` with the spec lengths. Default charset and collation (`utf8mb4_0900_ai_ci`) are
kept. There are no schema deviations.

## Dialect

Queries are the DuckDB text plus the mechanical rules in `dbench/queries/dialect.py`; only Q13
is rewritten (derived-table column list).

M11 and M12 are n/a: there is no DuckDB copy to convert or push down to.
