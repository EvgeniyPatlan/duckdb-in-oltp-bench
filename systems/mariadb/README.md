# MariaDB 13.0.2 + DuckDB engine (`ENGINE=DuckDB`)

Track A. MariaDB's DuckDB storage engine (`MariaDB-duckdb-engine`, plugin maturity **gamma**).
The official MariaDB Docker image does not include it, so `systems/mariadb/Dockerfile` builds
`dbench/mariadb-duckdb:13.0.2` from `rockylinux:9` (pinned by digest) and the official repo
`https://rpm.mariadb.org/13.0/rhel/9/x86_64/`, installing exactly
`MariaDB-server-13.0.2-1.el9`, `MariaDB-client-13.0.2-1.el9` and
`MariaDB-duckdb-engine-13.0.2-1.el9`. Build it with
`docker build -t dbench/mariadb-duckdb:13.0.2 systems/mariadb`. The resolved image digest is
recorded in every result as `versions.image_digest`. The dataset is mounted read-only at `/data`.

## Image and entrypoint

`entrypoint.sh` is minimal: if the datadir is empty it runs `mariadb-install-db` (the Dockerfile
empties the datadir the RPM pre-creates), creates `root@'%'` with an empty password and the `tpch`
database, creates the DuckDB spill directory, then `exec`s `mariadbd --user=root`. No port is
published; the server is reachable only over the benchmark's docker network.

## How settings are applied

`duckdb.cnf` is a jinja template rendered from the container limits (`dbench/systems/mariadb.py`,
formulas from `baseline_conf.py`, shared with the Percona and AliSQL engines) and bind-mounted
read-only at `/etc/my.cnf.d/dbench.cnf`. Each setting carries a reason comment in the file.

At 64 GB and 16 cores (mem_gb = container memory):

| Setting | Value | Reason |
|---|---|---|
| `plugin-load-add` | `ha_duckdb.so` | The engine is a plugin library. |
| `plugin-maturity` | `gamma` | The server refuses a gamma plugin under the default maturity. |
| `duckdb_memory_limit` | 0.75 x mem_gb = `48G` | Caps DuckDB (the query engine) at 75% of the container, the share the InnoDB baseline gives its buffer pool. The engine default is 1 GiB. |
| `duckdb_max_threads` | cores in the DB cpuset = `16` | One DuckDB thread per pinned core. |
| `duckdb_temp_directory` | `/var/lib/mysql/duckdb_tmp` | Explicit spill location on the datadir volume (created by the entrypoint); same spill rule for all engines. |
| `duckdb_use_double_for_decimal` | `OFF` | Exact TPC-H DECIMAL results. The default (ON) maps DECIMAL to DOUBLE. AliSQL gets the same setting. |
| `innodb_buffer_pool_size` | mem_gb/16 = `4G` | InnoDB only holds the catalog and `load_convert` staging; the data lives in DuckDB. |
| `innodb_log_file_size` | mem_gb/8 = `8G` | MariaDB's name for the redo log size; equals the InnoDB baseline's `innodb_redo_log_capacity`. |
| `join_buffer_size`, `sort_buffer_size` | `256M` | Same as the InnoDB baseline. |
| `tmp_table_size`, `max_heap_table_size` | mem_gb/16 = `4G` | Same as the InnoDB baseline. |
| `query_cache_type`, `query_cache_size` | `OFF`, `0` | MySQL 9.7 has no query cache; switched off explicitly for parity. |
| `secure_file_priv` | `/data` | Server-side `LOAD DATA INFILE` may read only the dataset mount. |
| `local_infile` | `OFF` | Loading is server-side. |
| `max_connections` | `200` | Headroom for the 32-stream throughput run plus probes. |
| `character-set-server`, `collation-server` | `utf8mb4`, `utf8mb4_general_ci` | The engine accepts only utf8mb4. |

Left at the default on purpose: `duckdb_require_primary_key=ON` (the schema declares primary keys),
`duckdb_checkpoint_threshold` (256 MiB), `duckdb_max_temp_directory_size=0` (per the engine source,
`ha_duckdb.cc`, 0 means "90% of the available disk", i.e. DuckDB's own default, so spilling to the
datadir volume is allowed and no cap is set),
`duckdb_allow_run_in_duckdb=OFF`, and the binary log (MariaDB's is **off** by default). There is
no `innodb_parallel_read_threads` equivalent in MariaDB, so none is set. `temptable_max_ram` does
not exist in MariaDB either.

## Verified facts (MariaDB 13.0.2-1.el9 RPMs, observed on the running server)

- `SHOW ENGINES` lists `DUCKDB`; one DuckDB file per server, `/var/lib/mysql/duckdb.db`.
- DuckDB version: `SHOW GLOBAL STATUS LIKE 'Duckdb_version'` returns `v1.5.5`; `versions()`
  records `duckdb: 1.5.5` from it, plus the server version `13.0.2-MariaDB`, the engine package
  and the image digest.
- The engine reports `SHOW CREATE TABLE ... ENGINE=DUCKDB` and keeps declared `PRIMARY KEY`s. The
  shared TPC-H DDL needed no override.

## Pushdown detection (differs from the task's assumption)

The engine plugs into MariaDB's `select_handler`: a whole `SELECT` is handed to DuckDB. The
documented marker is `select_type = PUSHED SELECT` in `EXPLAIN`, **but it never appears for this
engine**: the engine builds its handler only when the statement text itself starts with `SELECT`
(or `WITH`), so `EXPLAIN SELECT ...` is not pushed and shows an ordinary plan
(`select_type SIMPLE`, `type ALL`) for queries that really are pushed when run. Detection through
EXPLAIN would therefore report every query as a fallback. Instead the adapter reads the
**session status `Rows_read`** before and after the query, outside the timed region
(`_query_begin` / `_query_end`): a pushed query does not use the handler row API, so the counter
does not move (`duckdb`); the row path raises it (`fallback`); an unreadable counter is `unknown`.
Observed: an InnoDB scan of 3 rows moves `Rows_read` by 3; pushed single-engine and cross-engine
(InnoDB + DuckDB) joins leave it at 0. Limitation: a row-path query that reads zero rows cannot be
told from a pushed one. Queries that cannot be pushed (user variables, `LAST_INSERT_ID`, `SLEEP`,
and so on) are not handled by the row path either in this build: they fail with
`ERROR 1031: Storage engine DUCKDB ... doesn't have this option`, which the harness records as
an error, never a rewrite.

## Timeouts on pushed queries (engine property, not worked around)

Tested with a heavy pushed query (3-way cross join of a 3M-row DuckDB table, 5 s limit, docker
test `test_heavy_pushed_query_times_out_and_server_recovers`). The server-side
`max_statement_time` does **not** stop a pushed query on this engine. The harness records a
`timeout` only when the client read timeout (timeout + 30 s margin) fires; `KILL QUERY` is accepted
but the DuckDB query **keeps running** in the server (observed still `Executing` 20 s after the
kill), so later queries can overlap leftover work until it finishes. The server stays responsive
(`SELECT 1` succeeds). A campaign that needs a clean slate after a timeout must restart the
container. (Percona's engine honours its server-side limit: error 3024 after about 5 s.)

## Loading

- `load_bulk`: engine schema with `ENGINE=DuckDB`, `LOAD DATA INFILE` per CSV part, row-count check
  against the manifest, then `ANALYZE TABLE` on all tables. Same procedure as every MySQL-family
  adapter. The benchmark creates the "engine" schema (primary keys only, no foreign-key indexes) for
  this adapter (`bench_schema_kind = "engine"`). MariaDB's `run_in_duckdb('COPY ...')` would load faster but bypasses SQL and needs
  `duckdb_allow_run_in_duckdb`; it is **not** used.
- `load_convert`: InnoDB staging with the engine schema (primary keys only), timed as
  `extra.rowstore_load_s`; then `ALTER TABLE t ENGINE=DuckDB` per table and `ANALYZE TABLE`, all
  inside `elapsed_s`. Shared code with Percona (`ConvertToDuckDBMixin`).

## Measurement notes

The per-query timeout is `SET SESSION max_statement_time` (MariaDB's name for
`max_execution_time`, in seconds; MariaDB applies it to `SELECT`); `cancel()` is `KILL QUERY`.
`storage_bytes()` is `du -sb` of the datadir excluding binlog files (none are written by default);
it counts `duckdb.db` and its WAL plus the InnoDB catalog and redo log (`innodb_log_file_size`).
