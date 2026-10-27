# AliSQL 8.0.44-3 + DuckDB engine (`ENGINE=DuckDB`)

Track A. AliSQL is Alibaba's MySQL 8.0.44 fork with a built-in DuckDB storage engine. There is no
official Docker image, so `systems/alisql/Dockerfile` builds `dbench/alisql:8.0.44-3` from
`ubuntu:24.04` (pinned by digest) and the official release asset
`alisql-8.0.44-3-linux-glibc2.17-x86_64.tar.xz` (653924236 bytes) from
`https://github.com/alibaba/AliSQL/releases/download/AliSQL-8.0.44-3/`, verified by SHA-256
`9b3ca762483d14e4ee0206b543c1541df91efd02d7f1ba16fcd5a3c1d359fbf7` (computed by us; pinned in the
Dockerfile). Build it with `docker build -t dbench/alisql:8.0.44-3 systems/alisql`. The resolved
image digest is recorded in every result as `versions.image_digest`. The dataset is mounted
read-only at `/data`.

The tarball's `mysqld` is built on CentOS 7 and needs OpenSSL 1.0.2 (`libssl.so.10`,
`libcrypto.so.10`), which Ubuntu 24.04 does not ship. The Dockerfile therefore also installs the
CentOS 7.9 `openssl-libs-1.0.2k-19.el7` RPM (SHA-256 pinned) into `/opt/alisql/lib/private`, where
`mysqld` already looks (RPATH), and links Ubuntu's `libaio.so.1t64` as `libaio.so.1`. Nothing else
differs from the release tarball.

## Image and entrypoint

`entrypoint.sh` is minimal: if the datadir is empty it runs `mysqld --initialize-insecure` with an
init file that creates `root@'%'` (empty password, `mysql_native_password`) and the `tpch` database,
creates the DuckDB spill directory, then `exec`s `mysqld --user=root --defaults-file=...`. No port is
published; the server is reachable only over the benchmark's docker network.

## How settings are applied

`my.cnf` is a jinja template rendered from the container limits (`dbench/systems/alisql.py`,
formulas from `baseline_conf.py`, shared with the Percona and MariaDB engines) and bind-mounted
read-only at `/etc/alisql/dbench.cnf`. Each setting carries a reason comment in the file. At 64 GB
and 16 cores (mem_gb = container memory):

| Setting | Value | Reason |
|---|---|---|
| `duckdb_mode` | `ON` | The engine is off unless set (static, default `NONE`). |
| `duckdb_memory_limit` | 0.75 x mem_gb = `48G` | Caps DuckDB (the query engine) at 75% of the container, the share the InnoDB baseline gives its buffer pool. The default `0` means about 80% of the host's RAM, not the container's. The variable is in bytes; the `G` suffix is accepted and the live value is verified in the docker test. |
| `duckdb_threads` | cores in the DB cpuset = `16` | One DuckDB thread per pinned core (default `0` = auto). |
| `duckdb_temp_directory` | `/var/lib/mysql/duckdb_tmp` | Explicit spill location on the datadir volume (created by the entrypoint); same spill rule for all engines. |
| `duckdb_use_double_for_decimal` | `OFF` | Set explicitly and disclosed. The default `ON` maps DECIMAL with precision > 38 to DOUBLE; the benchmark schema has no such column, but MariaDB gets the same setting. |
| `duckdb_sql_normalization` | `OFF` | Set explicitly and disclosed (the default). Queries reach DuckDB as written, with no MySQL-to-DuckDB rewriting; anything it rejects is an error. |
| `duckdb_require_primary_key` | `ON` | Set explicitly (the default). The schema declares primary keys. |
| `innodb_buffer_pool_size` | mem_gb/16 = `4G` | InnoDB only holds the catalog and `load_convert` staging; the data lives in DuckDB. |
| `innodb_parallel_read_threads` | cores = `16` | Same as the InnoDB baseline. Only `COUNT(*)` without `WHERE` and `CHECK TABLE` use parallel read threads; ordinary scans stay single-threaded. |
| `innodb_redo_log_capacity` | mem_gb/8 = `8G` | Same as the InnoDB baseline. |
| `join_buffer_size`, `sort_buffer_size` | `256M` | Same as the InnoDB baseline. |
| `tmp_table_size`, `max_heap_table_size`, `temptable_max_ram` | mem_gb/16 = `4G` | Same as the InnoDB baseline. |
| `secure_file_priv` | `/data` | Server-side `LOAD DATA INFILE` may read only the dataset mount. |
| `local_infile` | `OFF` | Loading is server-side. |
| `max_connections` | `200` | Headroom for the 32-stream throughput run plus probes. |

Left at the default on purpose, all verified on the running server with `SHOW VARIABLES`:
`duckdb_max_temp_directory_size=0` (per the variable documentation 0 means 90% of the free disk
space, so spilling to the datadir volume is not capped and no value is set),
`duckdb_dml_in_batch=ON`, `duckdb_checkpoint_threshold` (256 MiB), `duckdb_max_threads_per_query`
(1000000, i.e. no per-query limit), `force_innodb_to_duckdb=OFF`, `duckdb_prefer_high_precision=OFF`,
and the binary log (MySQL 8.0 enables it by default; see the storage note below).

## Verified facts (observed on the running server)

- `SHOW ENGINES` lists `DUCKDB` (`YES`). Data lives in one file, `<datadir>/duckdb.db`.
- Server version `8.0.44-3-alisql-dev`.
- DuckDB version: **not exposed at runtime** (no variable, status or function). The only
  version-like string in the `mysqld` binary is `v1.5.2-dev736`, the snapshot the release notes name.
  `versions()` records `duckdb: 1.5.2-dev736` with that source. It differs from the 1.5.5 reference
  system, so result values are compared against the reference as for any engine.
- The shared TPC-H "engine" schema (`ENGINE=DuckDB`, primary keys only) was accepted without any
  override.

## Pushdown detection

AliSQL has no fallback. When every table of a `SELECT` is DuckDB, the original query text goes to
DuckDB; a query that mixes engines fails with error 7501 `Does not support mixed queries of duckdb
engine and other engines`, and a query DuckDB rejects fails with a DuckDB error. Both are recorded
as `error` (never as `fallback`); the mixed-engine message adds `extra.kind = "mixed_engine"`.

Detection reads the session status counter `Com_duckdb_select` (SELECTs handed to DuckDB) before and
after the query, outside the timed region (`_query_begin` / `_query_end`): it rises for a pushed
query (`duckdb`), an unchanged counter would be a silent row-path execution (`fallback`), an
unreadable counter is `unknown`. Observed: a `SELECT` on a DuckDB table raises it by 1; a `SELECT`
on an InnoDB table does not. MySQL 8.0 has no `Rows_read` status variable, so MariaDB's approach
does not apply.

## Timeouts

The per-query timeout is the base MySQL hook, `SET SESSION max_execution_time` in milliseconds.
Tested with a heavy pushed query (three-way cross join of a 1M-row DuckDB table, 5 s limit; docker
test `test_heavy_pushed_query_times_out_and_server_recovers`): the **server-side limit fires** at
5.006 s. It reaches the client as a DuckDB error (`7501 [DuckDB] INTERRUPT Error: Interrupted!`, or
`7513 ... send result error. Interrupted!` while streaming results), not as MySQL's error 3024, so
the adapter classifies an interrupt that arrives at the deadline as `timeout` (an interrupt long
before the deadline stays an `error`). The query did **not** linger (`lingering_queries()` was 0
right after `cancel()` and 3 s later) and the next query ran at once.

## Loading

- `load_bulk`: engine schema with `ENGINE=DuckDB`, `LOAD DATA INFILE` per CSV part (works; with
  `duckdb_dml_in_batch=ON`, the default), row-count check against the manifest, then
  `ANALYZE TABLE`. SF1 on the smoke limits: 41 s including analyze.
- `load_convert`: InnoDB staging with the engine schema (primary keys only), timed as
  `extra.rowstore_load_s`; then `ALTER TABLE t ENGINE=DuckDB` per table and `ANALYZE TABLE`, all
  inside `elapsed_s`. Shared code with Percona and MariaDB (`ConvertToDuckDBMixin`). SF1: staging
  44.8 s, conversion plus analyze 9.5 s.

## Measurement notes

`storage_bytes()` is `du -sb` of the datadir excluding binlog files (`binlog.*`); it counts
`duckdb.db` and its WAL plus the InnoDB catalog, tablespaces and the redo log
(`innodb_redo_log_capacity`). Replication logs are excluded, the redo log is counted. The server
runs with MySQL's default binary logging, so binlog files do exist and are excluded by name.
