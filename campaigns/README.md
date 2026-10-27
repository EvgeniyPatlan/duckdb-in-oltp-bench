# Campaigns

A campaign file names the systems, measures, scale factor and container limits of one pass.
`dbench campaign <file>` runs the systems one at a time (start, load, measure, stop) and writes
one JSON record per cell under `results/<run_id>/`.

| File | Where | Limits |
|---|---|---|
| `smoke-sf1.yaml` | 20-core development machine | database `0-7`, runner `8-11`, 16 GB |
| `gate-sf10.yaml` | 80-core / 180 GB bench host | database `0-15`, runner `16-23`, 64 GB |

## Running the SF10 gate on the bench host

Only Docker and git are needed on the host. Everything else runs in containers.

```bash
# 1. clone
git clone https://github.com/EvgeniyPatlan/duckdb-in-oltp-bench.git
cd duckdb-in-oltp-bench
git switch feat/initial-harness        # until it is merged

# 2. build the tool's container (first call builds it)
scripts/dev.sh dbench --version

# 3. build the locally built images (DuckDB 1.5.5 and 1.4.3, AliSQL, MariaDB, VillageSQL,
#    pg_ducklake) and pull the rest; the pg_ducklake build takes about 20 minutes
scripts/build-images.sh

# 4. generate the dataset (outside the repository)
export DBENCH_DATA=/data/dbench
scripts/dev.sh dbench datagen --sf 10

# 5. pin the images of this host
scripts/dev.sh dbench lock

# 6. run the gate (long: use tmux or nohup)
scripts/dev.sh dbench campaign campaigns/gate-sf10.yaml
```

### Resume

Run the same command again. A cell whose result file exists is skipped, a system whose cells are
all present is not even started, and no record is duplicated:

```bash
scripts/dev.sh dbench campaign campaigns/gate-sf10.yaml
```

The output ends with the number of records written and cells skipped, and a status count per
system. To redo a cell, delete its file under `results/<run_id>/<system>/`.

### What the gate checks

- Before any container starts: the cpusets and memory limits are checked against the CPUs and
  RAM that `docker info` reports, and every image is checked against `systems/versions.lock.json`.
- At SF1 and SF10 the reference is plain DuckDB 1.5.5. InnoDB and Postgres run first and must
  agree with it on every query that finishes. A disagreement stops the campaign with a message and
  is recorded in `extra.reference_disagreement`. Fix the cause (or report it), delete the
  `wrong_result` files and run the campaign again.
- At SF100 and above the reference is cross-checked against DuckDB 1.4.3; a disagreement stops the
  campaign in the same way.
- A measure a system does not support is recorded as `n/a` with the reason. A system that cannot
  start or load is recorded as `failed` with the error for all of its cells.
- Track B (VillageSQL and the raw DuckDB entry points) runs after Track A under the run id
  `<run_id>-trackb`.

### A system that does not work

After the 2-day time-box, record it. This writes a `failed` record for every cell the system has
in the campaign:

```bash
scripts/dev.sh dbench mark-failed --system percona \
    --error "server does not start: <message>" \
    --repro "docker run ... ; <steps>" \
    --campaign campaigns/gate-sf10.yaml
```

### The version lock

`dbench lock` writes `systems/versions.lock.json`: image, digest, server and DuckDB version and
`build_inputs` for every system. Registry images are pinned by their registry digest. Locally
built images have no registry digest, so the local image id is recorded, and that id is specific
to the host that built the image. Run `dbench lock` on the bench host after building the images.
`build_inputs` (base image digests, pinned package versions, tarball SHA-256 values, git commit)
come from the Dockerfiles and build scripts and must equal those in the committed lock; the
campaign refuses to start when they differ, or when a running image differs from the lock on the
host.

## Where the results land

`results/<run_id>/<system>/M1-sf<N>-q<NN>.json`, plus `results/<run_id>/_reference/` with the
cached reference rows. Track B results are under `results/<run_id>-trackb/`.

## Committing the results

Results stay on a local `results` branch that is never pushed.

```bash
git switch -c results          # first time only; later: git switch results
git add results/
git commit -m "results: gate-sf10"
git switch -
```
