-- vsql_duckdb 0.0.3 settings, rendered by dbench/systems/villagesql.py from the container limits.
-- The entrypoint runs this once, after 10-install-extension.sql, on an empty datadir; SET PERSIST
-- writes mysqld-auto.cnf on the data volume, so a restarted container keeps the values.
-- Lines starting with '--' are the reason for the statement below them.
-- Sizing formulas (mem_gb = container memory, cores = CPUs in the DB cpuset):
--   memory_limit_mb = 0.75 * mem_gb * 1024   (64 GB -> 49152)
--   threads         = cores                  (16 cores -> 16)
-- There is one embedded DuckDB per server; changing threads, memory_limit_mb, allow_local_files or
-- temp_directory rebuilds it on the next query, so all of them are set here, before any query.

-- One DuckDB worker thread per core pinned to the database container (the extension default is 2).
SET PERSIST vsql_duckdb.threads = {{ cores }};
-- Caps DuckDB at 75% of the container, the share the other MySQL-family engines give DuckDB (the
-- extension default is 1024 MB).
SET PERSIST vsql_duckdb.memory_limit_mb = {{ memory_limit_mb }};
-- Required to read the Parquet files on the dataset mount (the default OFF refuses local files);
-- the same switch also lets DuckDB spill to disk.
SET PERSIST vsql_duckdb.allow_local_files = ON;
-- Explicit spill location on the data volume (only used while allow_local_files is ON).
SET PERSIST vsql_duckdb.temp_directory = '{{ spill_dir }}';
-- The benchmark's per-query timeout (900 s) in ms; the extension default is 30000 and its maximum
-- is 3600000. The adapter lowers it per query when a run uses a shorter --timeout.
SET PERSIST vsql_duckdb.timeout_ms = {{ timeout_ms }};
-- The largest result the extension accepts (default 1048576). The result buffer the extension
-- declares is 1 MiB on the stable extension ABI, so results above 1 MiB are still refused; see
-- systems/villagesql/README.md.
SET PERSIST vsql_duckdb.max_result_bytes = 16777216;
