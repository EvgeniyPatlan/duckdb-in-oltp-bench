# Throughput (M4)

N concurrent streams each run the 22 TPC-H queries once, every stream in its own deterministic
permutation (`stream_order`; streams are numbered from 1). One record per stream count,
`item = streams=N`, `unit = qph`, `value = ok_queries * 3600 / elapsed_s`. Queries that errored,
timed out or crashed are counted (`counts_by_status`) and do not count in the numerator.
`elapsed_s` is the wall time from the moment every stream is released until the last one finishes.

- **What is inside the timed region (the same for every family).** Before the clock starts, each
  stream thread opens its own persistent connection, applies the session settings and the
  per-query timeout once, and runs one trivial statement. That statement executes in DuckDB on
  pg_duckdb, pg_ducklake, VillageSQL and plain DuckDB (a Postgres backend's DuckDB instance
  already exists when timing starts). The MySQL DuckDB engines (AliSQL, MariaDB, Percona) share
  one server-wide DuckDB instance, which the unrecorded warm-up pass already warms. All streams meet at a barrier and the clock starts. Per query a
  stream only sends the statement and fetches the result on its connection: no connect, no
  per-query `SET`, no `SHOW STATUS`/`EXPLAIN` probe. The clock stops when the last stream
  finishes; only then are the connections closed. Client-side decoding of a result (VillageSQL's
  JSON) is inside the stream. A query that fails drops that stream's connection and the next
  query reopens one (a rare path, identical everywhere).
- **No correctness check.** M1 owns correctness; M4 keeps only each query's status.
- **Warm cache.** One unrecorded warm-up pass of the 22 queries on a single stream runs before
  each stream count. The OS page cache is not dropped between stream counts.
- **Memory per backend.** pg_duckdb (`0.5 * mem_gb / N`) and pg_ducklake (`0.75 * mem_gb / N`)
  divide DuckDB's memory by the stream count per session (`prepare_streams`), recorded as
  `memory_per_backend`. MySQL-family engines and VillageSQL share one DuckDB instance per server
  and get no per-stream setting.
- **Pushdown.** M4 does not probe pushdown (that is M1/M8's job); `pushed_counts` only counts
  `not_measured`. `pushdown_counter_scope` (session, global, explain, none) is recorded for M8.
- **Contamination.** After a timeout the stream calls `cancel()` and polls whether that very query
  is still running (`still_running`: processlist / `pg_stat_activity`) for up to 10 s. The run is
  `contaminated` when it survives, when `cancel()` could not be issued, when a probe fails, when a
  timeout's stop was never confirmed and several streams ran, or when `lingering_queries()` still
  shows queries after all streams are done. The server is restarted after the record is written,
  never mid-run. After a timeout without a server-confirmed stop, that stream's poll (up to 10 s)
  is inside the timed region.
- **Host suspend.** If the host slept more than 1 s during a run, the run is discarded and repeated
  (up to 2 retries, recorded in `discarded_attempts`); if every attempt slept the campaign records
  an automatic failure that a resume retries.
