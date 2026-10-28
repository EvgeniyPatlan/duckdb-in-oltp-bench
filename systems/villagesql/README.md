# VillageSQL Server 0.0.7 + vsql_duckdb 0.0.3 (Track B)

## Why VillageSQL is a separate track

The other MySQL-family systems (Percona, MariaDB, AliSQL) are measured in Track A as storage
engines: the TPC-H tables live in a DuckDB engine inside the server, the same SQL text is sent to
every system, and the server decides whether DuckDB runs it. VillageSQL's `vsql_duckdb` extension
is built differently. It adds three SQL functions, `duckdb_query(sql)`, `duckdb_scalar(sql)` and
`duckdb_status()`, that hand DuckDB SQL text to one embedded DuckDB and return the answer as text.
There is no DuckDB table type, no `ENGINE=` clause and no automatic routing of ordinary SQL. The
data stays in external files (Parquet, CSV, JSON; local or remote), and the query is DuckDB SQL
over `read_parquet(...)`, not MySQL SQL over tables.

So the question Track A asks ("how fast is this server on TPC-H tables it stores?") has no
VillageSQL answer, and a different question applies: how fast is each system's raw DuckDB entry
point on the same Parquet files? Track B asks that of every system that has one:

| System (Track B name) | Entry point | Query text |
|---|---|---|
| `duckdb-1.5.5` (the reference) | the DuckDB query server, `read_parquet()` | canonical DuckDB text |
| `villagesql` | `SELECT duckdb_query('<sql>')`, JSON parsed into rows | the same |
| `pg_duckdb-trackb` | `SELECT * FROM duckdb.query($dbq$<sql>$dbq$)` | the same |
| `mariadb-trackb` | `SELECT run_in_duckdb('<sql>')`, text parsed into rows | the same |

Track B results never appear on a Track A chart, and VillageSQL is not compared with the
storage-engine systems in Track A.

## The Track B query text

`dbench/queries/trackb.py` has the one substitution function (`substitute_parquet`) all four
systems share. It takes the canonical DuckDB text of each TPC-H query (`dbench/queries/tpch/`) and
replaces every table reference (the word after `FROM`, `JOIN` or a comma in a `FROM` list) by
`read_parquet('/data/parquet/<table>/<table>.*.parquet')`, appending `AS <table>` when the query
gives no alias and keeping an explicit alias (`nation n1`, `lineitem AS l1`) as written. Nothing
else changes. Tests run it on all 22 queries and check that every alias survives and that undoing
the substitution restores the canonical text. `trackb_diff(n)` prints the difference for one query.

## How a result is read back

`duckdb_query()` returns one string: a JSON array with one object per row, keys in column order.

- Integers, decimals, floats and booleans keep their JSON types. The adapter parses JSON numbers to
  `int` and `Decimal` (never binary `float`), so DECIMAL sums keep their digits.
- Dates, timestamps and every other type arrive as JSON strings, and the function does not expose
  column types. The adapter leaves them as text; `correctness.compare` equates an ISO date string
  with the reference's `date`, and numeric columns are the union of the reference's (typed) and
  the adapter's (JSON number) columns. **Limitation:** a column that arrives as a string and holds
  a number is only compared numerically because the reference marks it numeric; the adapter itself
  cannot tell.
- The JSON parse runs after the timed region; the time covers the call and the transfer of the
  string.

## Settings (and why)

The image is `dbench/villagesql:0.0.7-duckdb-0.0.3`, built by the `Dockerfile` here:

1. Stage 1 (Ubuntu 24.04, the server image's base, pinned by digest) builds `vsql-duckdb` at tag
   `0.0.3` (commit `a3cefdb`) against the 0.0.7 extension SDK (release asset
   `villagesql-extension-sdk-0.0.7.tar.gz`, SHA-256 checked) with `DUCKDB_TAG=v1.5.5`, the
   extension's default, plus the two development-ABI source edits described below. The extension
   builds DuckDB 1.5.5 from source and links it in statically.
2. Stage 2 is `villagesql/server:mysql-8.4_0.0.7`, pinned by digest, with the resulting
   `vsql_duckdb.veb` copied into `/usr/lib/veb` (the server's `veb_dir`).
3. `install-extension.sql` runs once on an empty datadir: `SET PERSIST
   vsql_allow_preview_extensions = ON; INSTALL EXTENSION vsql_duckdb;`.

Settings are rendered from the container limits (`dbench/systems/villagesql.py`). `my.cnf` is
mounted at `/etc/my.cnf`; `settings.sql` is mounted into `/docker-entrypoint-initdb.d/` and `SET
PERSIST`s the extension's variables, so a restarted container keeps them. Every line has a reason
comment in the file. At 64 GB and 16 cores:

| Setting | Value | Reason |
|---|---|---|
| `vsql_duckdb.threads` | cores = `16` | One DuckDB worker thread per pinned core (extension default 2). |
| `vsql_duckdb.memory_limit_mb` | 0.75 x mem_gb x 1024 = `49152` | Same 75% share the other MySQL-family engines give DuckDB (default 1024). |
| `vsql_duckdb.allow_local_files` | `ON` | Without it the extension refuses to read the Parquet files on the dataset mount; the same switch also lets DuckDB spill to disk. |
| `vsql_duckdb.temp_directory` | `/var/lib/mysql/duckdb_tmp` | Explicit spill directory on the data volume. |
| `vsql_duckdb.timeout_ms` | `900000` (the 900 s query timeout) | The extension's own deadline; its default is 30000 and its maximum 3600000. |
| `vsql_duckdb.max_result_bytes` | `16777216` (its maximum) | Default is 1048576; effective only on the development-ABI build, see below. |
| `innodb_buffer_pool_size` | mem_gb/16 = `4G` | The InnoDB side only holds `nation` for the join example. |
| `innodb_parallel_read_threads`, `innodb_redo_log_capacity`, `join_buffer_size`, `sort_buffer_size`, `tmp_table_size`, `max_heap_table_size`, `temptable_max_ram` | as the other MySQL-family engines | Shared formulas from `baseline_conf.duckdb_engine_settings`. |
| `skip-log-bin`, `secure_file_priv=/data`, `local_infile=OFF`, `max_connections=200`, `character-set-server=utf8mb4` | as the other MySQL-family servers | Same on every MySQL-family system. |

`timeout_ms` is a global variable (there is no session form). When a run uses a shorter
`--timeout`, the adapter sets `SET GLOBAL vsql_duckdb.timeout_ms` before each query; the extension
does not rebuild its DuckDB instance for that variable. Changing any other `vsql_duckdb.*` variable
does rebuild it, which is why they are set before the first query.

## Non-default build: development ABI and the result cap

The benchmark uses a non-default build of the same 0.0.3 release. The extension's own README
("Returning more than one megabyte") says the released build is made against the stable extension
ABI, whose `duckdb_query()` result buffer is a fixed 1 MiB: `max_result_bytes` is clamped to it
whatever it is set to, and a larger answer is refused. The README documents the way to lift this:
build against the development ABI (the SDK's `include-dev` headers) and raise `kQueryBufferSize` /
declare `.max_result_length()`. This image does exactly that, at the benchmark
owner's decision, so that the documented 16 MiB `max_result_bytes` applies:

- `systems/villagesql/Dockerfile` configures with `-DVSQL_USE_DEV_ABI=ON`, the SDK's own switch for
  its `include-dev` headers (the same `villagesql-extension-sdk-0.0.7.tar.gz`, SHA-256 checked, so
  no extra artifact), sets `kQueryBufferSize` to 16777216 and adds
  `.max_result_length(kQueryBufferSize)` for `duckdb_query()`. These are the only two source edits;
  the tag, commit, DuckDB version and server image are unchanged. The build args `ABI=development`
  and `RESULT_BUFFER_BYTES=16777216` are recorded in the lock's build inputs.
- The SDK and the server image are the same 0.0.7 release. That this server loads the
  development-ABI `.veb` and honours the 16 MiB buffer is checked live by
  `test_live_result_cap_is_16_mib_on_the_development_abi` and
  `test_live_result_cap_boundary_at_16_mib` in `tests/test_villagesql.py`. `versions` records
  `abi: "development"`, and the lock file lists the build args in the build inputs.
- The development ABI is not the stable surface, and an extension built against it needs a matching
  server; results here are for this build, not for the released `.veb`.
- Cost of the 16 MiB buffer, measured at 16 GB / cores 0-7 on this build and on the stable-ABI build
  of the same release (two rounds each, 1000 x `SELECT 1` and 50 x TPC-H q06 at SF1, timed region
  of `execute()`): `SELECT 1` median 0.40-0.43 ms (dev) vs 0.42-0.47 ms (stable), p95 about 1.6 ms
  on both; q06 median 21.4 / 19.3 ms (dev) vs 19.8 / 18.2 ms (stable), p95 30.8 / 21.7 ms vs
  27.9 / 20.7 ms, so the development build was about 1 ms (5-8%) slower on q06 in both rounds,
  a difference of the size of the run-to-run spread; container memory after the runs was
  734-784 MiB (dev) vs 735-924 MiB (stable), peak 763-811 vs 763-939 MiB, no measurable memory
  cost.

A query whose answer is larger than 16 MiB is still refused with an error rather than returned cut
(a cut JSON array would not parse), and is recorded as `error` with `extra.kind = "result_cap"`.
It is a result, not a harness failure, and the query text is not changed. Tests check on the
live server that about 2 MiB and 4.5 MiB come back whole and that about 22 MiB is a `result_cap`.
At SF1 TPC-H Q16 (18314 rows) now succeeds; with the stable build it was a `result_cap`.

## KILL does not reach `duckdb_query()`

`KILL QUERY` does not interrupt the embedded DuckDB, so `cancel()` returns `False`. The deadline is
the extension's `timeout_ms`, which interrupts the DuckDB connection; the query then fails with
`query passed vsql_duckdb.timeout_ms (N ms) and was stopped`, recorded as `timeout`. After any
timeout the power run restarts the container (a `cancel()` that cannot cancel forces it), and the
record carries `extra.restarted_after_timeout = true` and `extra.cancel_unavailable = true`.

## The JSON_TABLE join (`join_innodb`)

The extension documents joining a DuckDB aggregate to a MySQL table with `JSON_TABLE`, since DuckDB
cannot see MySQL tables. Item `join_innodb` is that pattern: `duckdb_query()` aggregates the
`customer` Parquet files per nation (count and balance), `JSON_TABLE` turns the JSON into rows,
and they join the InnoDB `nation` table loaded in VillageSQL, ordered by nation name. The reference
is plain DuckDB 1.5.5 computing the same result from the `customer` and `nation` Parquet files. The
other Track B systems record `n/a` with a reason for this item.

## Other disclosures

- One embedded DuckDB serves the whole server. Tables, macros and settings changes made through it
  are visible to every connection; the benchmark runs one stream.
- `versions` in every record holds the server version, `vsql_duckdb 0.0.3`, the DuckDB version
  reported by `duckdb_status()` and the image digest.
- Track B pg_duckdb bundles DuckDB 1.4.3 (the image `pgduckdb/pgduckdb:18-v1.1.1`), not 1.5.5.
  MariaDB reports 1.5.5; `mariadb-trackb` additionally sets `duckdb_allow_run_in_duckdb = ON`
  (`systems/mariadb/trackb.cnf`), which Track A never does.
- Track B pg_duckdb and MariaDB use the Track A images and settings otherwise.
- M11 (conversion) and M12 (pushdown) are n/a: VillageSQL has no engine to convert tables into
  and no routing decision to detect.
