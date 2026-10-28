# Throughput (M4)

N concurrent streams each run the 22 TPC-H queries once, every stream in its own deterministic
permutation (`stream_order`; streams are numbered from 1). One record per stream count,
`item = streams=N`, `unit = qph`, `value = ok_queries * 3600 / elapsed_s`. Queries that errored,
timed out or crashed are counted (`counts_by_status`) and do not count in the numerator.
`elapsed_s` is the wall time from the moment every stream is released until the last one finishes.

- **Execution path.** Each stream thread uses the adapter's normal `execute()`, so per-session setup
  and timeouts are those of the power run (900 s per query). Postgres-family systems keep one
  persistent session per thread. MySQL-family systems open a fresh connection per query: its
  connect time is outside each query's timed region but inside the stream's wall time, for every
  MySQL-family system alike.
- **No correctness check.** M1 owns correctness; M4 keeps only each query's status.
- **Warm cache.** One unrecorded warm-up pass of the 22 queries on a single stream runs before
  each stream count. The OS page cache is not dropped between stream counts.
- **Memory per backend.** pg_duckdb (`0.5 * mem_gb / N`) and pg_ducklake (`0.75 * mem_gb / N`)
  divide DuckDB's memory by the stream count per session (`prepare_streams`), recorded as
  `memory_per_backend`. MySQL-family engines and VillageSQL share one DuckDB instance per server
  and get no per-stream setting.
- **Pushdown.** `pushed_counts` holds, per stream, the `pushed` values of its ok queries. Percona's
  counter is server-wide (`pushdown_counter_scope = "global"`), so its `pushed` is `unknown`
  when N > 1.
- **Contamination.** After a timeout the stream calls `cancel()`. When all streams are done,
  `lingering_queries()` is polled for up to 10 s; anything still running (or a `cancel()` that
  cannot be issued) sets `contaminated = true` and the server is restarted after the record is
  written, never mid-run.
- **Host suspend.** If the host slept more than 1 s during a run, the run is discarded and repeated
  (up to 2 retries, recorded in `discarded_attempts`); if every attempt slept the campaign records
  an automatic failure that a resume retries.
