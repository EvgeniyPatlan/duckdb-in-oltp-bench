# How to read this run

I maintain the Percona engine, one of the systems tested here. These notes explain the numbers.
They don't rank the systems.

Setup, from `campaigns/talk-sf100.yaml`: TPC-H scale factor 100, the database on 16 cores
(cpuset 0-15), the benchmark client on its own cores (16-23), a 64 GB memory limit per database
container, and a 900 s limit per query.

## Single stream: the three MySQL engines are close

AliSQL, Percona and MariaDB answer all 22 queries correctly. Summing each query's median over
three timed runs gives 47.2 s, 48.4 s and 49.2 s. Per run, AliSQL takes 47.2–47.4 s, Percona
48.1–49.1 s and MariaDB 48.8–49.9 s, so only AliSQL's lead holds in every run (by 0.8–1.9 s over
Percona in the same run). The totals hide large per-query
differences: Percona is fastest on 13 queries, AliSQL on 6 and MariaDB on 3. Percona's total is
held back by Q4 (7.3 s against 1.3–1.4 s on the other two, all pushed to DuckDB).

Each engine embeds a different DuckDB version (Percona 2.0.0, MariaDB 1.5.5, AliSQL 1.5.2-dev736),
and plain DuckDB 2.0.0 alone is about 24% faster than 1.5.5 here. To see what an integration
adds, use the overhead chart, which compares each system with plain DuckDB of its own version:
AliSQL 1.13×, MariaDB 1.21×, Percona 1.49×, pg_ducklake 1.92×. A rebuild of Percona on DuckDB
1.5.5, described below, separates the engine from the DuckDB version.

## pg_duckdb on tables is not columnar storage

pg_duckdb (36.6× overhead, 1481 s) keeps the data in ordinary PostgreSQL heap tables. Its
188.9 GB on disk equal plain PostgreSQL's 188.9 GB, and DuckDB scans those row-store tables.
That is the setup the table benchmark tests, not what pg_duckdb is mainly built for. Over Parquet
(the Parquet section) it takes 71.8 s, the same as plain DuckDB 1.5.5 (71.5 s) and VillageSQL
(71.3 s); MariaDB's `run_in_duckdb` takes 82.5 s.

## Throughput: what "crashed" means

Every throughput level marked `crashed` was a memory kill: Docker stopped the database container
at its 64 GB limit (`OOMKilled`, exit code 137). On tables, that applies to AliSQL from 8
streams, plain DuckDB 1.5.2 from 16, and DuckDB 1.4.3, 1.5.3 and 1.5.5 at 32. Over Parquet, it
applies to VillageSQL from 8 streams. The engines didn't
crash on their own, and a larger limit would move these points. No throughput figure is shown for
those levels.

On tables, four systems stayed within the limit at every level: Percona (1134 qph at 32
streams), its rebuild on DuckDB 1.5.5 (1258 qph, below), MariaDB (550 qph at 32 streams, with 17
queries timing out) and plain DuckDB 2.0.0 (726 qph at 32 streams, with 3 timeouts). Over
Parquet, plain DuckDB 1.5.5 (583 qph) and MariaDB (527 qph) also stayed within it at 32 streams,
each with 18 timeouts. Throughput over Parquet is not comparable with throughput on tables.

pg_duckdb and pg_ducklake split their memory between backends (`memory_per_backend`: at 8
streams, 4 GB per backend for pg_duckdb and 6 GB for pg_ducklake). From 8 streams up, their
levels are marked `error`. Each record keeps the first 50 error messages. At 8 and 16 streams
most of them are DuckDB failing to read back its own spill file (`IO Error: Could not read enough
bytes from file .../duckdb_temp_storage...`); at 32 streams most are lost connections from backend
restarts. The others are DuckDB `Out of Memory` errors at the per-backend limit and, on Q18, a
`sum(l_quantity)` that overflowed DECIMAL(38,3), an impossible value for that column, which
points to corrupted intermediate data rather than a real overflow. I haven't established the
cause of any of these.

## Percona rebuilt on DuckDB 1.5.5

Percona is the only MySQL engine here on DuckDB 2.0.0, so I also rebuilt the same engine release
(v0.2.4, same MySQL 9.7.0 server) on DuckDB 1.5.5, the version MariaDB embeds. It is listed as
"Percona (rebuilt on DuckDB 1.5.5)". It is not a Percona release, and its build recipe is in the
version lock. It ran single stream (M1) and throughput (M4) only, on the same host and limits.

- All 22 queries match and are pushed to DuckDB. The total is 55.3 s, against 48.4 s on 2.0.0 and
  49.2 s for MariaDB on the same 1.5.5. Q4 drops from 7.3 s to 1.7 s. Plain DuckDB 2.0.0 runs Q4
  in 1.2 s, so the slow Q4 comes from how the engine uses 2.0.0, not from DuckDB 2.0.0 itself.
  Q1 takes 7.7 s here (3.4 s on the 2.0.0 build, 2.7 s on MariaDB); I don't know why yet.
- The overhead against plain DuckDB 1.5.5 is 1.34× (1.49× for the 2.0.0 build against plain
  DuckDB 2.0.0).
- Throughput on tables stays within the memory limit at every level: 1427 qph at 16 streams and
  1258 qph at 32, with no timeouts. On the same DuckDB 1.5.5, MariaDB has 750 and 550 qph, and
  plain DuckDB 830 qph at 16 and a memory kill at 32 (plain DuckDB may use 80% of the container,
  the engines 75%). So DuckDB 2.0.0 is not what keeps Percona's throughput up under concurrency.
  This run doesn't show which part of the engine does.

## pg_duckdb over Parquet: a query that cannot be stopped

At 4 streams, TPC-H Q9 over Parquet ran for 58 hours using all 16 cores. It ignored
`statement_timeout`, `pg_cancel_backend` and `pg_terminate_backend`, and only `kill -9` stopped
it. A second run stuck the same way (three Q9 streams past 20 minutes) and was stopped by hand.
That level is recorded as failed with the reason. The 8, 16 and 32 stream levels ran to the end
and are marked `error` for the spill failures above.

## Loading: the paths differ

Each system loads the data its documented way, so the load times compare paths as well as
engines:

- MariaDB inserts the Parquet copy of the data through DuckDB (`run_in_duckdb`): 125 s.
- pg_ducklake reads the CSV files with DuckDB's CSV reader: 195 s.
- Percona and AliSQL load the CSV files through the server with `LOAD DATA INFILE`: 1999 s
  and 6235 s.

## Storage

Percona uses 67.3 GB after loading (58.9 GB of data and 8.3 GB of redo log), against 36–39 GB for
AliSQL, MariaDB and pg_ducklake. It has no checkpoint measurement, because the engine has no
`CHECKPOINT` statement. Why its data is larger is not yet known.

## SQL compatibility

The 8 wrong results (4 MariaDB, 2 AliSQL, 2 pg_ducklake) were each reproduced by hand: the same
statements on an engine table and on a row-store table (InnoDB or heap) in the same server, with
the same image versions as this run. Each record also keeps the rows that differed. The errors (15
AliSQL, 10 MariaDB, 15 pg_ducklake) are statements the engine refuses with an error message, mostly
MySQL or PostgreSQL syntax and functions that DuckDB doesn't support, and statements across
engines. They show where each integration stops, not wrong answers. I haven't checked each one
against the engine's documentation.
