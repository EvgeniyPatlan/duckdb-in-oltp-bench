# duckdb-in-oltp-bench

A reproducible benchmark of databases that embed DuckDB, run next to plain DuckDB and to the
row-store servers they extend. Everything runs in Docker through the `dbench` command-line tool,
and every number on the results site comes from a record this repository can regenerate.

Results site: <https://evgeniypatlan.github.io/duckdb-in-oltp-bench/> (placeholder until the first
publication; see [Results site](#results-site)).

No results are published yet, so this README makes no claims about which system is faster.

**Disclosure:** the author maintains one of the systems under test (Percona's DuckDB engine for
MySQL). The [fairness rules](#fairness-rules) below, the pinned versions and the published
configuration of every system are how this benchmark guards against that bias; setup problems
can be reported as described [below](#reporting-a-problem-with-a-systems-setup).

## What is compared, and why

Several projects now put DuckDB inside a transactional database so that analytical queries can run
on the same server as the application. They differ in how: a storage engine, an extension, or a
set of functions. This benchmark asks the same questions of each of them and shows the answers side
by side, together with the cases where a system could not answer.

| Group | Systems |
|---|---|
| DuckDB inside MySQL-family servers | AliSQL, MariaDB, Percona's engine on MySQL 9.7 |
| DuckDB inside PostgreSQL | pg_duckdb, pg_ducklake |
| DuckDB as a function library inside MySQL | VillageSQL (Track B) |
| Reference: plain DuckDB | 1.5.5, 1.5.3, 1.5.2-dev736, 1.4.3 |
| Reference: row stores | MySQL 9.7 InnoDB, PostgreSQL 18 |

Exact versions, images and digests are in [systems/README.md](systems/README.md) and
[`systems/versions.lock.json`](systems/versions.lock.json). The workload is TPC-H-derived and is
not an audited TPC-H result.

## The two tracks

**Track A** holds the systems that expose DuckDB through ordinary SQL: tables live in the
database, the same query text is sent to every system (translated mechanically for the MySQL and
PostgreSQL dialects; the diffs are published), and the server decides whether DuckDB runs it.
These systems are compared with each other, with plain DuckDB and with the row-store baselines.

**Track B** holds VillageSQL. Its `vsql_duckdb` extension adds functions that hand DuckDB SQL text
to an embedded DuckDB and return the result. There is no DuckDB table type and no routing of
ordinary SQL, so the workload is different: DuckDB SQL over Parquet files, not MySQL SQL over
tables. Putting it in the same charts as Track A would compare two different workloads, so it is
reported separately. For reference, Track B also measures the raw DuckDB entry points of plain
DuckDB, pg_duckdb (`duckdb.query`) and MariaDB (`run_in_duckdb`) on the same Parquet files.

## Measures

| Id | What it measures | Status |
|---|---|---|
| M1 | Query speed: the 22 TPC-H queries, one at a time. One cold run, then three warm runs; the median warm time and the cold time are reported | run |
| M2 | Load time, by two paths: each system's fastest documented bulk load, and conversion of data already in a row-store table | run |
| M3 | Storage size on disk, after load and after a checkpoint | run |
| M4 | Throughput with 1, 4, 8, 16 and 32 concurrent query streams | run |
| M5 | SQL compatibility: a fixed suite of probes (types, functions, joins, subqueries, CTEs, windows, DML, DDL), each classified by what happened | run |
| M6 | Correctness: every result is checked against a reference (see below) | applies to all measures |
| M7 | Integration overhead: a system's M1 warm time divided by plain DuckDB's at the same DuckDB version | run |
| M8 | Silent slow paths: the share of queries that quietly fall back to row execution, and whether the user can tell | run |
| M9 | Bigger than memory (SF300 under the memory cap) | not yet run |
| M10 | Crash safety (kill during load and during commit) | not yet run |

Per-measure details are in [docs/methodology](docs/methodology/) and on the site's methodology
page. Scale factors 1, 10, 100 and 300 are supported; the conference campaign uses SF100.

## Fairness rules

- **Pinned versions and digests.** Every image is pinned in the lock file: registry images by
  digest, locally built images by pinned base-image digests, package versions, checksums and git
  commits. A campaign refuses to start when an image or its build inputs differ from the lock.
- **Identical container limits.** Every database container gets the same CPUs and memory
  (16 pinned cores and 64 GB by default). The benchmark runner is pinned to separate cores.
- **Non-default settings only with a reason.** Each system's configuration lists every setting
  that differs from the default, with the reason. See the per-system READMEs in `systems/` and
  the methodology page.
- **Correctness before timing.** Each result is compared with a reference (plain DuckDB 1.5.5; at
  SF100 and above also cross-checked against 1.4.3). A time counts only if the result matches.
  At SF10 and below the row-store baselines run first, and the campaign stops if they disagree
  with the reference.
- **Wrong results are never shown as times.** A mismatch is recorded as `wrong_result` and drawn
  as a failure mark, not as a bar.
- **`n/a` always has a reason.** A measure a system does not support, or a ratio that cannot be
  computed, is shown as `n/a` with the cause. A system that cannot start or load is recorded as
  `failed` with the error and the steps to reproduce.
- **No overall ranking.** The site shows measures separately and publishes no combined score.
  Where a summary number exists (the M7 geometric mean), it states how many queries went into it.

## Hardware

The reference host has 80 CPUs and about 187 GiB of RAM visible to Docker. Each database container
gets 16 pinned cores (0-15) and 64 GB; the runner gets cores 16-23. The CPU model, disk type and
kernel are recorded in the `host` field of every result record and shown on the site's methodology
page. Smaller machines can run the smoke campaign (see below) with smaller limits.

## Quick start

Prerequisites: Linux, Docker with buildx, and git. Everything else runs in containers.

```bash
git clone https://github.com/EvgeniyPatlan/duckdb-in-oltp-bench.git
cd duckdb-in-oltp-bench
scripts/dev.sh pytest -q            # builds the tool's container on first use, runs the tests
scripts/dev.sh dbench --help
```

## One-command reproduction

After the one-time setup in [docs/reproduce.md](docs/reproduce.md) (images, dataset, three
environment variables), the conference campaign is one command:

```bash
scripts/dev.sh dbench campaign campaigns/talk-sf100.yaml
```

It is long (days at SF100) and can be interrupted and resumed with the same command. A smaller
campaign for a development machine is `campaigns/smoke-sf1.yaml`; `campaigns/gate-sf10.yaml` is
the SF10 correctness gate. The three variables `DBENCH_DATA`, `DBENCH_RESULTS` and
`DBENCH_RUNNER_CPUSET` must all be set, otherwise the run looks under the checkout or refuses to
start. [docs/reproduce.md](docs/reproduce.md) explains each step; [campaigns/README.md](campaigns/README.md)
covers campaign files, resuming, the version lock and committing results.

## Repository layout

| Path | Content |
|---|---|
| `dbench/` | the benchmark tool: adapters, measures, correctness checks, campaign runner |
| `systems/` | image recipes, server configuration and notes per system, and the version lock |
| `campaigns/` | campaign files and their documentation |
| `docs/` | reproduction guide and methodology notes |
| `sitegen/` | the static results-site generator |
| `published/` | curated result records the site is built from |
| `tests/` | the test suite |

## Results site

The site is a static page built from result records: interactive charts, the raw data as
`data/*.json`, and a methodology page listing every non-default setting, the limits, the
comparison tolerances, the translated query diffs and the host. It is built from the tracked
`published/` directory by `.github/workflows/pages.yml`, which runs the test suite first and
refuses to deploy when `published/` holds no records or the site is marked synthetic. To build it
locally from your own results, see the last step of [docs/reproduce.md](docs/reproduce.md).

## Reporting a problem with a system's setup

If a system is configured unfairly, a setting is wrong or missing, or a version or build is not
what its maintainers would run, open an issue at
<https://github.com/EvgeniyPatlan/duckdb-in-oltp-bench/issues>. Please include:

- the system (as named in [systems/README.md](systems/README.md)) and the measure or query;
- what you expected and what the setting or result in this repository is;
- a link to the documentation that supports it, and the command or configuration you would use.

Reports that come with documentation and a reproducible configuration are the easiest to act on.

## License

See [LICENSE](LICENSE).
