# SQL compatibility and silent slow paths (M5, M8)

An OLTP user does not read an engine's pushdown rules before writing a query. M5 asks what happens
when they simply try the things a MySQL or PostgreSQL application does; M8 turns M1 and M5 into
the share of statements that quietly left DuckDB.

## M5: the probe suite

`probes/probes.yaml` holds 120 probes in nine categories: `types` (each column type round trips),
`functions` (string, date, math, `GROUP_CONCAT`/`string_agg`, `DATE_FORMAT`, JSON), `joins`
(inner, outer, semi, anti, and joins between an engine table and a row-store table), `subqueries`,
`cte` (plain and recursive), `window`, `dml` (single-row and bulk writes, `INSERT ... SELECT`,
upsert, transactions with rollback), `ddl` (`ALTER`, rename, `TRUNCATE`, index, CTAS) and
`variables` (user variables, prepared statements, session settings).

- **The suite was fixed before any result was seen.** A probe changes afterwards only when its
  two dialects turn out not to express the same intent, or when the baseline's own result is
  invalid. It is never changed to move an engine's outcome. Such changes are listed in the results
  write-up.
- **Two spellings per probe.** `sql_mysql` and `sql_postgres` say the same thing. Where a feature
  has no equivalent (MySQL user variables, `FULL JOIN`, inline `ENUM`) the PostgreSQL spelling is
  the nearest idiom, and the probe says so in its `note`.
- **Scratch tables.** Each probe creates its own tables (`pc_*`) in `setup`, with
  `{{ engine_clause }}` (the system's engine: `ENGINE=DuckDB`, `USING ducklake`, or nothing for
  `pg_duckdb` and the baselines) and drops them in `teardown`. Mixed-engine probes use
  `{{ rowstore_clause }}` (`ENGINE=InnoDB`, or a plain heap table) for a small row-store table
  joined with an engine table. Read-only probes may read the TPC-H tables `nation`, `region` and
  `supplier`. A leftover table from an interrupted run is dropped before `setup`.
- **Every probe table has a primary key** (a natural `id` column; a surrogate `pk` column where
  ids repeat), as the TPC-H schema has wherever a system accepts one. Without it, an engine that
  requires a key (AliSQL and MariaDB: error 1173) would fail every probe at `setup`, and the run
  would never show whether it supports the feature a probe is about. The single exception is
  `ddl_create_table_without_pk`, which creates a table without a key and inserts a row, so that
  requirement is measured once, on its own. (A MySQL `CREATE TABLE ... AS SELECT` declares its key;
  PostgreSQL has no such syntax.) This was decided before any engine-specific tuning; the first
  version of the suite had no keys and was replaced for that reason.
- **Every read touches a table.** A statement that reads no rows leaves a status counter such as
  MariaDB's `Rows_read` unchanged and would look pushed.

### Expected results

When a probe has no literal `expected`, the expectation is the result of the same probe on the
row-store baseline of the same dialect (`innodb` for MySQL-dialect probes, `postgres` for
PostgreSQL ones). The baselines run first, cache each result under
`results/<run_id>/_probe_reference/<probe>.<dialect>.json` together with a hash of the probe's
text for that dialect (setup, teardown, statements, check, compare mode, literal expected) and of
its rendering for the baseline (so a change to the baseline's clauses or to the template rendering
counts too), and record every
probe as `n/a`
(reason "row-store baseline; reference for the probe suite"). A cached result whose hash differs from the probe's current one is treated as missing, never
reused: an engine record without a usable reference carries `extra.reference = "stale"` or
`"absent"` (`"ok"` otherwise) and `extra.compared = false`. On resume the stale cells are redone:
a baseline's cell whose cache is missing or keyed differently runs again and rebuilds the
reference, an engine's cell compared with an older reference (including one set aside as `n/a`
by the server control below after a wrong result) runs again and is compared with the new one,
an engine's failure recorded without the server control runs again with it, and M8 is
recomputed. Comparison is the one M1 uses
(`dbench.correctness.compare`: order independent, numeric and CHAR tolerance, NULL only equals
NULL). A probe with `compare: json` additionally parses every cell that holds a JSON object or
array and re-serialises it canonically (sorted keys, no whitespace) on both sides, because systems
print the same document with different spacing; real differences still show. A baseline that fails a probe leaves no reference; the engines' record then has
`extra.compared = false` and is classified from pushdown only. When both baselines have run, the
second also compares its result with the first's; `extra.baseline_disagreement` marks a probe whose
dialects do not behave alike, which is a probe bug, not an engine finding.

### Server control: failures of the host server itself

The reference comes from one server per dialect, MySQL 9.7 with InnoDB and Postgres 18. The
integrations run on other servers: AliSQL, MariaDB, Percona's MySQL 9.7 build, Postgres with an
extension. A probe can therefore fail because the host server's own SQL dialect lacks the
feature, in its parser or its function library, before the DuckDB integration is involved at all
(MariaDB has no `REGEXP_LIKE`, no `LATERAL`, and its `LAG` takes no default argument). Such a
failure says nothing about the integration, so M5 checks for it. The rule is the same for every
system:

- Whenever an engine probe ends as `error` or `wrong_result`, it runs once more as a **server
  control**: the same probe on the same server and session, with every `{{ engine_clause }}`
  rendered as the system's `{{ rowstore_clause }}` (`ENGINE=InnoDB`, or a plain heap table). That
  is the same SQL on plain row-store tables, without the integration. Mixed-engine probes already
  use the row-store clause for some tables; in the control every table is a row-store table.
- The control uses the probe's own tables and teardown: the probe's tables are dropped after the
  probe and again before and after the control, so the two never share a table. It has the same
  timeout and the same recovery after a crash or a timeout as the probe.
- **The host server fails too.** If the control fails with an error, or, for a wrong result,
  returns the same rows as the integration (so the server's own result differs from the reference
  in the same way), the probe does not test the integration on this server. It is recorded as
  `n/a` with the reason "not supported by the *system* server itself (same failure on a row-store
  table): *the control's error*" (or "same result on a row-store table"). The integration's own
  outcome is kept: `extra.integration_class`, `extra.integration_error` or `extra.diffs`, and
  `extra.server_control` holds what the control saw.
- **Only the integration fails.** If the control runs and matches the expectation, the original
  class stands (an `ENGINE=DuckDB` table that refuses a table without a primary key, a `CONCAT`
  that ignores `NULL`) and `extra.server_control` is `"ok"`, so a reader can see the check was
  made. The original class also stands when the control runs but differs from the reference,
  as long as the integration behaves differently from its own server: it failed where the
  control ran, or its wrong result is not the control's result. `extra.server_control` then
  records the control's mismatch.
- **No verdict.** A control that crashes or times out proves nothing; the original class stands
  and `extra.server_control.conclusive` is `false`.
- **No control** (`extra.server_control.skipped` says why) where the engine and row-store clauses
  are identical (pg_duckdb: there is no row-store variant to compare with), for probes that read
  the TPC-H tables (`nation`, `region`, `supplier` are engine tables on every integration, so the
  control would not be free of it), when the probe itself crashed or timed out (never set aside),
  when the server did not recover from the probe, and when the probe's teardown failed (a table
  left behind would make the control fail for the wrong reason). The baselines get no control.

When the probe has a literal `expected`, the control is compared with it, as the probe is.

### Classes

`extra.class` is exactly one of:

| Class | Record status | Meaning |
|---|---|---|
| `duckdb` | ok | A read probe ran, matched the expectation, and pushdown shows DuckDB ran it. |
| `fallback` | ok | It ran and matched, on the row path (the engine did not take it). |
| `error` | error | A statement failed: `extra.stage` is `setup`, `statement` or `check`, `extra.error` the server's message. A timeout or crash is an error too (`extra.outcome`); a crash restarts the server and the run goes on. |
| `wrong_result` | wrong_result | It ran, but the rows differ from the expectation (`extra.diffs`). Reported even when the statement was pushed. |
| `unknown` | ok | It ran and matched, but the adapter cannot tell where: `pushdown_counter_scope` is `none`, or the counter could not be read. |
| `ok_unobserved` | ok | A DML or DDL probe that succeeded and whose follow-up check matched. Their pushdown is not scored (see below). |
| `n/a` | n/a | The row-store baseline (reference builder); a mixed-engine probe on a system where the engine and row-store clauses are the same (pg_duckdb queries heap tables: "there is no separate engine table to mix with"); or a probe the host server itself does not support, as the server control shows ("not supported by the ... server itself"). |

**Pushdown detection** is the adapter's normal path, the same as M1: a session status counter
(AliSQL `Com_duckdb_select`, MariaDB `Rows_read`), a global counter (Percona
`Ducksdb_pushdown_count`; fine here because probes run one at a time), or a plan-only `EXPLAIN`
(pg_duckdb and pg_ducklake). Systems with `pushdown_counter_scope = none` are `unknown` unless
they errored.

**DML and DDL probes.** The counters watch SELECT pushdown only, so a counter around an `UPDATE`
says nothing, and the follow-up SELECT (`check_*`) is not the statement under test. Write probes
(categories `dml` and `ddl`) are therefore never `duckdb` or `fallback`: a correct one is
`ok_unobserved`, a failed write is `error`, and a follow-up result that differs from the baseline
is `wrong_result` (which is how a transaction that does not roll back shows up). The check's
pushdown is kept as `extra.check_pushed`, as a fact about the check. Write probes are excluded
from M8's probe share and from any per-category pushdown count. Scripts with several statements
(`BEGIN ... ROLLBACK`, `SET` then `SELECT`) run on one session: the statements before the last
are not observed and the last one is the probe, or the check when the category is `dml` or `ddl`.
On PostgreSQL a plan-only `EXPLAIN` is never sent inside an open transaction (a failed one would
abort it, and a `SAVEPOINT` guard is refused by DuckDB-backed tables), so the pushdown of a
statement inside `BEGIN ... COMMIT` is `unknown`. The statements before the last get the same
`statement_timeout` / watchdog bound as a query.

Each M5 record has `item = <probe id>`, `extra.category`, `extra.title`, `extra.pushed`
(a read probe's pushdown; `extra.check_pushed` for a write probe), `extra.class`, and, when relevant, `extra.error`,
`extra.stage`, `extra.diffs`, `extra.row_count`, `extra.note`. A probe that ended as `error` or
`wrong_result` also has `extra.server_control` (`"ok"`, what the control saw, or `skipped`), and
one set aside by the control has `extra.integration_class` and, for an error,
`extra.integration_error`.

## M8: silent slow paths

M8 is derived, after M1 and M5, from the same system's records of the same run. It has three
cells:

- `fallback_share_tpch`: of the M1 queries that ran (status `ok` or `wrong_result`), the share
  with `pushed = fallback`.
- `fallback_share_probes`: the same status set over the M5 *read* probes (write probes are left
  out; errors are counted separately in `extra.not_run`).
- `visibility`: 1 when the adapter can observe pushdown at all, else 0. `extra.mechanism` is
  `status_counter` (session or global counter), `explain` or `none`. For a global counter
  `extra.note` says the reading is ambiguous under concurrency.

A share is `fallback / (duckdb + fallback + unknown)`. Statements whose pushdown is unknown are
counted in `extra.counts.unknown` and in `extra.unobserved_share`, never as `duckdb`. A system
with no way to observe pushdown has no share (`n/a` with the reason): 0% fallback there would
only mean "not looked at". The baselines have no M8 (`n/a`: every statement is on the row path by
construction), and plain DuckDB and Track B have neither M5 nor M8.

## Changes after the first run

Made for every system alike, none for an engine's outcome; the YAML header lists them too.

- `fn_json_build` has `compare: json`: JSON text was a false `wrong_result` (`{"k": 1}` vs
  `{"k":1}`), a representation difference the comparison now ignores.
- Write probes are classed `ok_unobserved` instead of by the follow-up SELECT's pushdown.
- The probe reference is keyed by a hash of the probe text and its baseline rendering; resume
  rebuilds stale references and re-runs the cells that depend on them.
- M8's TPC-H share now counts the same statuses as the probe share (`ok` and `wrong_result`).
- `join_mixed_tpch_dimension` carries `mixes: true` (it joins a row-store table with the TPC-H
  engine table), so the generic mixed-engine rule makes it `n/a` where the two clauses are the
  same.
- `ddl_truncate` inserts a sentinel row after `TRUNCATE` and counts rows, so the check reads a row.
  `var_session_time_zone` now reads a wall-clock timestamp on both sides (MySQL `DATETIME`,
  PostgreSQL `TIMESTAMP`, cast to `timestamptz`) and converts it in the session time zone (UTC+2);
  the MySQL side had used `TIMESTAMP`, an absolute instant that ignores the session zone, so the
  two dialects did not agree (the baselines disagreed on it).
- Mixed-engine probes are `n/a` where the engine and row-store clauses are identical.
- The server control was added: a probe that fails, or returns the same wrong result, on a
  row-store table of the same server is `n/a` ("not supported by the ... server itself") instead
  of an integration `error` or `wrong_result`. It applies to every integration alike. On resume,
  every engine record with status `error` or `wrong_result` and no `extra.server_control` (made
  before the control existed) runs again, unless no control would apply to it (see above); M8 is
  recomputed with it.
- Earlier: primary keys on every scratch table (with `ddl_create_table_without_pk` for the no-PK
  case), `ddl_add_primary_key` became `ddl_change_primary_key`, `types_long_text` uses `LONGTEXT`
  and `cte_recursive_path` uses `TEXT` on PostgreSQL because the baseline result was invalid.
- PostgreSQL probes use JSONB, the type applications normally choose; MySQL has one JSON type.
