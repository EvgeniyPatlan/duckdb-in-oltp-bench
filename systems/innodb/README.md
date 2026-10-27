# MySQL 9.7 (InnoDB) baseline

Image `mysql:9.7` (digest recorded in every result as `versions.image_digest`), started as the
`mysql` user with the config rendered from `my.cnf` and mounted read-only at
`/etc/mysql/conf.d/dbench.cnf`. The dataset is mounted read-only at `/data`.

## Non-default settings

Rendered from the container limits (`dbench/systems/baseline_conf.py`); at 64 GB and 16 cores:

| Setting | Value | Reason |
|---|---|---|
| `innodb_buffer_pool_size` | 0.75 x container GB = `48G` | Most of the memory for the buffer pool; the rest covers connections and per-query buffers. |
| `innodb_parallel_read_threads` | cores in the DB cpuset = `16` | Only `COUNT(*)` without `WHERE` and `CHECK TABLE` use parallel read threads; MySQL's ordinary scans stay single-threaded. Set to every core so those two are not held back. |
| `secure_file_priv` | `/data` | Server-side `LOAD DATA INFILE` may read only the read-only dataset mount. |
| `local_infile` | `OFF` | Loading is server-side; client-side `LOCAL` loads stay off. |
| `join_buffer_size` | `256M` | Per-join memory comparable to Postgres `work_mem=256MB`. |
| `sort_buffer_size` | `256M` | Per-sort memory comparable to Postgres `work_mem=256MB`. |
| `tmp_table_size`, `max_heap_table_size` | mem_gb/16 = `4G` | Internal temporary tables (GROUP BY, DISTINCT, derived tables) stay in memory. |
| `temptable_max_ram` | mem_gb/16 = `4G` | RAM the TempTable engine may use before spilling; equal to the caps above. |
| `innodb_redo_log_capacity` | mem_gb/8 = `8G` | Keeps checkpoints from throttling bulk load at the 100MB default. |
| `max_connections` | `200` | Headroom over the 32-stream throughput run plus probe and cancel connections. |

`skip-log-bin`: binary logging is off on every MySQL-family server (it is on by default for MySQL, Percona and AliSQL, off for MariaDB); replication is not under test and the default differs between families. Everything else stays at the MySQL 9.7 default.

## Loading and measurement notes

- `LOAD DATA INFILE '/data/...' INTO TABLE t FIELDS TERMINATED BY '|' OPTIONALLY ENCLOSED BY '"'
  LINES TERMINATED BY '\n'` per CSV part. The generated data contains no backslashes, so the
  default escape character does not affect it.
- Row counts are checked against the manifest, then `ANALYZE TABLE` runs on all tables. Load time
  covers the loads and the analyze, not the count check.
- Tables are loaded with the primary key and foreign-key indexes already in place.
- Queries run with the session `max_execution_time` set to the per-query timeout; `cancel()` is
  `KILL QUERY` from a second connection.
- `storage_bytes()` is `du -sb` of the data directory. Storage rule for the MySQL family (every
  MySQL-family adapter inherits it): replication logs (`binlog.*`) are excluded (none are written, as binary logging is off);
  the redo log and tablespaces are counted.

## Schema

TPC-H tables with primary keys, plus a plain index on every foreign-key column (row-store
baseline rule); no other indexes and no declared foreign-key constraints. Strings are
`VARCHAR(n)` with the spec lengths. Default charset and collation (`utf8mb4_0900_ai_ci`) are
kept. There are no schema deviations.

## Dialect

Queries are the DuckDB text plus the mechanical rules in `dbench/queries/dialect.py`; only Q13
is rewritten (derived-table column list).

M11 and M12 are n/a: there is no DuckDB copy to convert or push down to.
