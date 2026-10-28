# Campaigns

A campaign file names the systems, measures, scale factor and container limits of one pass.
`dbench campaign <file>` runs the systems one at a time (start, load, measure, stop) and writes
one JSON record per cell under `$DBENCH_RESULTS/<run_id>/`.

| File | Where | Limits |
|---|---|---|
| `smoke-sf1.yaml` | 20-core development machine | database `0-7`, runner `8-11`, 16 GB |
| `gate-sf10.yaml` | 80-core / 180 GB bench host | database `0-15`, runner `16-23`, 64 GB |
| `talk-sf100.yaml` | same host; SF100, multi-day | same as the gate |

Results and data live outside the checkout. In the commands below `/srv/dbench/data` and
`/srv/dbench/results` are examples; use any directories with enough space. Set the variables
inline on every command, as shown (do not `export` them).

## Smoke run on a development machine

Same flow with `campaigns/smoke-sf1.yaml` (20 cores: database `0-7`, runner `8-11`):

```bash
DBENCH_DATA=$HOME/dbench/data scripts/dev.sh dbench datagen --sf 1
DBENCH_DATA=$HOME/dbench/data DBENCH_RESULTS=$HOME/dbench/results \
DBENCH_RUNNER_CPUSET=8-11 \
    scripts/dev.sh dbench campaign campaigns/smoke-sf1.yaml
```

## Running the SF10 gate on the bench host

Prerequisites: Docker with buildx, and git. Everything else runs in containers.

```bash
# 1. clone
git clone https://github.com/EvgeniyPatlan/duckdb-in-oltp-bench.git
cd duckdb-in-oltp-bench
git switch feat/initial-harness        # until it is merged

# 2. build the tool's container (the first call builds it)
scripts/dev.sh dbench --version

# 3. build the locally built images (DuckDB 1.5.5, 1.4.3, 1.5.3 and 1.5.2-dev736, AliSQL,
#    MariaDB, VillageSQL, pg_ducklake) and pull the registry images at the digests of the
#    committed lock; the pg_ducklake build takes about 20 minutes
scripts/build-images.sh

# 4. generate the dataset
DBENCH_DATA=/srv/dbench/data scripts/dev.sh dbench datagen --sf 10

# 5. refresh the local image ids of this host in the lock
scripts/dev.sh dbench lock

# 6. run the gate (long: use tmux or nohup); the runner is pinned to the YAML's runner_cpuset
DBENCH_DATA=/srv/dbench/data DBENCH_RESULTS=/srv/dbench/results \
DBENCH_RUNNER_CPUSET=16-23 \
    scripts/dev.sh dbench campaign campaigns/gate-sf10.yaml
```

The campaign refuses to start when the runner is not pinned inside `runner_cpuset` (the message
names `DBENCH_RUNNER_CPUSET`), when the limits do not fit the host, or when an image differs from
the lock.

### Resume

Run step 6 again with the same variables. A cell with a record is skipped, a system whose cells
are all present is not even started, and no record is duplicated. Failures the campaign recorded
itself (a system that did not start or load, a measure that aborted) carry `extra.auto` and are
retried on resume. The output ends with the records written and cells skipped, and a status count
per system. To redo any other cell, delete its file under
`/srv/dbench/results/<run_id>/<system>/`.

### When the gate blocks

The campaign stops with a message and exits non-zero. Fix the cause, then delete the files below
and run step 6 again.

- **InnoDB or Postgres disagrees with the reference** (SF1 and SF10). The `wrong_result` records of
  that system carry `extra.reference_disagreement`. Delete that system's files:
  `rm /srv/dbench/results/<run_id>/<system>/M1-*.json` (`<system>` is `innodb` or `postgres`).
  If the reference rows themselves were wrong, also delete
  `/srv/dbench/results/<run_id>/_reference/`.
- **The reference has no result for a query** (error or timeout at SF10 or below). Nothing to
  delete: the reference is rebuilt on the next run. Check the query on plain DuckDB first.
- **Cross-check failure** (SF100 and above). Delete
  `/srv/dbench/results/<run_id>/_crosscheck/` (the cross-check rows and the disagreement records)
  and, if the primary reference was the wrong one, `/srv/dbench/results/<run_id>/_reference/`.
- **A system is still running after a failed cleanup.** Run `docker rm -f dbench-<system>`.

### What the gate checks

- Before any container starts: the cpusets and memory limits against the CPUs and RAM that
  `docker info` reports, the runner's own CPU affinity, and every image against
  `systems/versions.lock.json`.
- At SF1 and SF10 the reference is plain DuckDB 1.5.5. InnoDB and Postgres run first and must
  agree with it on every query that finishes.
- At SF100 and above the reference is cross-checked against DuckDB 1.4.3.
- A measure a system does not support is recorded as `n/a` with the reason. A system that cannot
  start or load is recorded as `failed` with the error for all of its cells.
- Track B (VillageSQL and the raw DuckDB entry points) runs after Track A under the run id
  `<run_id>-trackb`.

### A system that does not work

After the 2-day time-box, record it. This writes a final `failed` record (reason and
`extra.repro`) for every planned cell of the system that has no real measurement:

```bash
DBENCH_RESULTS=/srv/dbench/results scripts/dev.sh dbench mark-failed --system percona \
    --error "server does not start: <message>" \
    --repro "docker run ... ; <steps>" \
    --campaign campaigns/gate-sf10.yaml
```

### The version lock

`dbench lock` writes `systems/versions.lock.json`: image, digest, server and DuckDB version and
`build_inputs` for every system. Registry images are pinned by their registry digest;
`scripts/build-images.sh` pulls them by that digest. Locally built images have no registry digest,
so the local image id is recorded, and that id is specific to the host that built the image.

`dbench lock` only refreshes local image ids. It refuses to change a registry digest, an image
name or any `build_inputs` (base image digests, pinned package versions, tarball SHA-256 values,
git commits, read from the Dockerfiles and build scripts) relative to the committed lock; pass
`--force` to move the pins on purpose. The campaign refuses to start when the build inputs differ
from the lock or when a running image differs from it.

## Per-system measures (`overrides`)

By default every system runs the campaign's `measures`. `overrides` replaces that list for named
systems:

```yaml
overrides:
  innodb: {measures: [M1]}
  duckdb-1.5.5: {measures: [M1, M4]}
```

Each key must be in `systems` and each measure registered, otherwise the campaign refuses to
start. A system whose list leaves out M2 is still bulk-loaded (it needs the data), but that load
is setup and is not recorded. `talk-sf100.yaml` uses this: Track A engines run M1-M5 and M8, the
row-store baselines only M1, plain DuckDB (1.5.5, 1.4.3, 1.5.3, 1.5.2-dev736) M1 and M4.

## M7: integration overhead

For each Track A system and query, M7 is the system's M1 warm value divided by the M1 warm value
of plain DuckDB at the same DuckDB version (the record's `versions.duckdb`, `v` prefix dropped, so
`pg_duckdb` on 1.4.3 is compared with `duckdb-1.4.3`). The version is read per record. Rules:

- no plain run at that version: `n/a`, "no plain DuckDB run at version X" (never compared with
  another version);
- no recorded version (`unknown`): `n/a`, "DuckDB version not recorded";
- either side not `ok` (timeout, wrong_result, ...): `n/a` naming the status; a plain time of zero
  is `n/a` too;
- `extra.version_gap` is set when the version differs from the reference version, so the reader
  knows the baseline is not the reference one.

Two summary items per system:

- `geomean`: geometric mean over the system's ok ratios, with `extra.queries_used`,
  `extra.queries_total` and `extra.excluded`. A system that times out on its slow queries scores
  better here, so `extra.partial` is true whenever any query is excluded; `dbench overhead` and the
  campaign summary print `used/total queries` next to it. It is `n/a` ("mixed DuckDB versions: a,
  b") when the ok ratios come from more than one DuckDB version.
- `geomean_common`: geometric mean over the queries where every Track A system of the run has an ok
  ratio (`extra.queries` lists them), so all systems are scored on the same queries. `n/a` with a
  reason when no query is common. Compare systems on this item, not on `geomean`.

A campaign computes M7 when it ends. Recompute from the stored M1 records at any time (M7
records of the run are replaced; nothing else is touched):

```bash
DBENCH_RESULTS=/srv/dbench/results scripts/dev.sh dbench overhead talk-sf100
```

The reference version comes from the run's cached reference (`_reference/`); without it, pass
`--reference-version`. The rewrite is not atomic: if it is interrupted, rerun to repair.

The campaign's end summary also lists the cells that were not checked against the reference
(`extra.compared` false: the reference had no result for the query).

## Where the results land

`$DBENCH_RESULTS/<run_id>/<system>/M1-sf<N>-q<NN>.json` (power run), `M2-sf<N>-load_bulk.json` /
`M2-sf<N>-load_convert.json` (load paths) and `M3-sf<N>-after_load.json` /
`M3-sf<N>-after_checkpoint.json` (storage; see `docs/methodology/storage.md`), plus `<run_id>/_reference/` with the
cached reference rows. Throughput (M4) writes one `M4-sf<N>-streams=<S>.json` per stream count, in
the campaign's optional `streams:` list (default 1, 4, 8, 16, 32; the smoke file uses 1 and 4);
see `docs/methodology/throughput.md`. Track B results are under `<run_id>-trackb/`.

## Committing the results

Results stay on a local `results` branch that is never pushed. Commit from a separate worktree so
the results directory and the checkout used for running are never switched under a campaign.

```bash
# first time only (creates the branch); later: git worktree add ../bench-results results
git worktree add -b results ../bench-results

mkdir -p ../bench-results/results ../bench-results/systems
cp -r /srv/dbench/results/gate-sf10 /srv/dbench/results/gate-sf10-trackb ../bench-results/results/
cp systems/versions.lock.json ../bench-results/systems/
cd ../bench-results
git add -f results/ systems/versions.lock.json
git commit -m "results: gate-sf10"
cd -
```

Never push the `results` branch.
