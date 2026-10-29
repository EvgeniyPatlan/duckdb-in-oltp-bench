# Reproducing the benchmark

A step-by-step guide from a clean Linux host with Docker to a results site. Every command was
checked against `scripts/dev.sh dbench --help` and the help of each subcommand.

Contents: [requirements](#requirements) - [1. Set the environment](#1-set-the-environment-all-three-variables) -
[2. Clone](#2-clone) - [3. Build images](#3-build-the-dev-image-and-the-system-images) -
[4. Generate data](#4-generate-the-dataset) - [5. Pin the local images](#5-record-the-local-image-ids) -
[6. Run a campaign](#6-run-a-campaign) - [7. Resume](#7-resume-after-an-interruption) -
[8. Follow progress](#8-follow-progress) - [9. Check results](#9-check-the-results) -
[10. Build the site](#10-build-the-site) - [Troubleshooting](#when-a-campaign-stops)

## Requirements

- Linux with Docker and the buildx plugin (`docker buildx version`), and git. Nothing else is
  installed on the host: the tool, the database servers and the data generator all run in
  containers.
- Your user can talk to the Docker socket (`docker ps` works without `sudo`).
- Enough cores and RAM for the campaign's limits. The committed campaigns use:

| Campaign | Intended host | Database container | Runner | Memory per database |
|---|---|---|---|---|
| `campaigns/smoke-sf1.yaml` | 20-core development machine | cores 0-7 | cores 8-11 | 16 GB |
| `campaigns/gate-sf10.yaml` | 80-core bench host | cores 0-15 | cores 16-23 | 64 GB |
| `campaigns/talk-sf100.yaml` | 80-core bench host | cores 0-15 | cores 16-23 | 64 GB |

The campaign checks the limits against the CPUs and RAM Docker reports and refuses to start when
they do not fit.

### Disk space

The dataset is generated once per scale factor (SF) as CSV and Parquet. Measured at SF1:
CSV 1.1 GB, Parquet 0.31 GB, so **roughly 1.4 GB per SF**: about 14 GB at SF10 and about 140 GB at
SF100. Larger scale factors scale about linearly. On top of that:

- a system's loaded database, one system at a time (the campaign removes each system's data when
  it finishes the system). The size is system-specific and is what M3 measures; look at the
  `M3-sf<N>-after_load` records of a smaller run and scale them up, and keep spill space for
  larger-than-memory queries;
- Docker images and build cache, in Docker's own root directory
  (`docker info --format '{{.DockerRootDir}}'`); the pg_ducklake and AliSQL builds are the large
  ones;
- temporary files of image builds (see the TMPDIR rule below);
- the result records, which are small (JSON, a few thousand files).

Check what you have before starting:

```bash
df -h /srv/dbench "$(docker info --format '{{.DockerRootDir}}')"
du -sh /srv/dbench/data/tpch/sf*          # after generating data
```

## 1. Set the environment (all three variables)

The tool reads three environment variables. **Set all three, in the shell you run the commands
from.** The simplest way is to export them once:

```bash
export DBENCH_DATA=/srv/dbench/data          # datasets; also decides where image builds put TMPDIR
export DBENCH_RESULTS=/srv/dbench/results    # result records, one folder per run id
export DBENCH_RUNNER_CPUSET=16-23            # cores the runner container is pinned to
```

Use directories on a large disk; `/srv/dbench` is only an example. Do not put `DBENCH_DATA`
directly under `/` or under `/tmp` (see the TMPDIR rule).

`DBENCH_RUNNER_CPUSET` must equal the `runner_cpuset` of the campaign file you run (`16-23` for the
gate and talk campaigns, `8-11` for the smoke campaign). `scripts/dev.sh` passes it to Docker as
`--cpuset-cpus`, so the runner cannot use the database's cores.

### What happens when one is missing

- **`DBENCH_RUNNER_CPUSET` unset.** The runner container may use every core, including the
  database's, and the campaign refuses to start:

  ```text
  campaign stopped: the runner may use CPUs [0, 1, 2, ..., 15, 24, ..., 79] outside runner_cpuset
  '16-23'. Start it pinned: DBENCH_RUNNER_CPUSET=16-23 scripts/dev.sh dbench campaign <file>
  ```

  Nothing has been started or written at that point.
- **`DBENCH_DATA` unset.** The tool looks for the dataset under `data/` inside the repository
  checkout, not where you generated it elsewhere, and stops with
  `dataset missing or corrupt at ...; run: dbench datagen --sf <N>`. If you run `datagen` without
  it, the data is written into the checkout's `data/` (ignored by git), likely on the wrong disk.
- **`DBENCH_RESULTS` unset.** Records are written to `results/` inside the checkout (ignored by
  git). The campaign starts and runs, but results end up on the checkout's disk, and a later
  shell that sets the variable will not see them, so a resume starts from scratch.

Setting a variable inline (`DBENCH_DATA=... scripts/dev.sh ...`) also works, but then every command
needs all of them; an `export` avoids forgetting one.

## 2. Clone

```bash
git clone https://github.com/EvgeniyPatlan/duckdb-in-oltp-bench.git
cd duckdb-in-oltp-bench
```

## 3. Build the dev image and the system images

`scripts/dev.sh <command>` runs a command inside the tool's container with the repository mounted.
The first call builds the container (`dbench-dev:latest`):

```bash
scripts/dev.sh dbench --version
```

Then build the locally built images and pull the registry images at the digests in the lock file:

```bash
scripts/build-images.sh
```

This builds plain DuckDB (2.0.0, 1.5.5, 1.4.3, 1.5.3 and 1.5.2-dev736), AliSQL, MariaDB, VillageSQL and
pg_ducklake, and pulls MySQL, PostgreSQL, Percona's engine and pg_duckdb by digest. The pg_ducklake
build takes about 20 minutes.

**The TMPDIR rule.** Image builds write large temporary files. `build-images.sh` keeps them beside
the data, not in `/tmp` on the root disk: when `TMPDIR` is unset and `DBENCH_DATA` is set, it uses
`<parent of DBENCH_DATA>/tmp` (`/srv/dbench/tmp` for the example above) and prints
`using TMPDIR=...`. It refuses to run when that parent is `/` or `/tmp`, and it warns when neither
variable is set. An explicit `TMPDIR` always wins.

## 4. Generate the dataset

```bash
scripts/dev.sh dbench datagen --sf 1      # small, for a first run
scripts/dev.sh dbench datagen --sf 10     # the gate
scripts/dev.sh dbench datagen --sf 100    # the talk campaign
```

The data goes to `$DBENCH_DATA/tpch/sf<N>/` (CSV, Parquet and a checksummed `manifest.json`; the
campaign verifies the manifest before it starts). `--out` overrides the parent directory.

## 5. Record the local image ids

Locally built images have no registry digest, so the lock records an image id that is specific to
the host that built them. Refresh the ids on this host once, after building:

```bash
scripts/dev.sh dbench lock
```

`dbench lock` changes only local ids. It refuses to change registry digests, image names or build
inputs; `--force` moves those pins on purpose. The campaign refuses to start when a running image
or its build inputs differ from the lock. This rewrites `systems/versions.lock.json` in your
checkout.

## 6. Run a campaign

Start with the smoke campaign on a development machine, or the SF10 gate on the bench host:

```bash
export DBENCH_RUNNER_CPUSET=8-11                # smoke campaign only; use 16-23 otherwise
scripts/dev.sh dbench campaign campaigns/smoke-sf1.yaml
scripts/dev.sh dbench campaign campaigns/gate-sf10.yaml
```

The conference campaign runs for days. Use `tmux`, `screen` or `nohup`, and pipe the output to a
log (see [Follow progress](#8-follow-progress)):

```bash
scripts/dev.sh env PYTHONUNBUFFERED=1 dbench campaign campaigns/talk-sf100.yaml 2>&1 \
    | tee -a /srv/dbench/talk-sf100.log
```

The campaign runs the systems one at a time (start, load, measure, stop) and writes one JSON
record per cell to `$DBENCH_RESULTS/<run_id>/<system>/`. Before anything starts it checks the
container limits against the host, the runner's CPU affinity and every image against the lock.
Track B runs after Track A under the run id `<run_id>-trackb`. At the end it prints the integration
overhead (M7) and a status count per system.

`dbench campaign --results DIR` overrides `DBENCH_RESULTS` for one call.

## 7. Resume after an interruption

Run the same command again with the same variables. A cell that has a record is skipped, a
system whose cells are all present is not started, and no record is duplicated. Failures the
campaign recorded itself (a system that did not start or load, a measure that aborted) are
retried. To redo any other cell, delete its file under `$DBENCH_RESULTS/<run_id>/<system>/` and
run the campaign again.

If a system cannot be made to work, record that explicitly. This writes a `failed` record, with
the reason and the steps to reproduce, for every planned cell of the system that has no real
measurement:

```bash
scripts/dev.sh dbench mark-failed --system percona \
    --error "server does not start: <message>" \
    --repro "docker run ... ; <steps>" \
    --campaign campaigns/gate-sf10.yaml
```

## 8. Follow progress

The campaign prints one line per recorded cell, plus a line when a system starts and finishes:

```text
14:02:11 [innodb] start: M1 22, M5 120 to run; 3 already recorded
14:51:40 [innodb] M1 5/22 q05: ok 12.3 s (match)
16:10:02 [innodb] done in 2h07m: ok 141, n/a 1
```

(Example format.) When the output is piped to `tee`, Python buffers it and lines can appear late
or not at all. Run with `PYTHONUNBUFFERED=1`, as in the command in step 6 (`scripts/dev.sh env
PYTHONUNBUFFERED=1 dbench campaign ...`). Then:

```bash
tail -f /srv/dbench/talk-sf100.log
find "$DBENCH_RESULTS/talk-sf100" -name '*.json' | wc -l     # records written so far
docker ps --filter name=dbench-                                # the system currently running
```

CPU pinning layout: database containers use `db_cpuset` (cores 0-15 by default), the runner uses
`runner_cpuset` (16-23), and the two must not overlap. Both come from the campaign file's
`limits`; the defaults are in `dbench/config.py`.

## 9. Check the results

The end-of-run summary lists records written, cells skipped, a status count per system, and the
cells that were not checked against the reference. To count statuses from the stored records at any
time:

```bash
scripts/dev.sh python -c '
import os
from collections import Counter
from pathlib import Path
from dbench.results import iter_records

root = Path(os.environ["DBENCH_RESULTS"]) / "talk-sf100"
counts = Counter((r.system, r.status) for r in iter_records(root))
for (system, status), n in sorted(counts.items()):
    print(f"{system:24} {status:14} {n}")
'
```

Statuses are `ok`, `timeout`, `error`, `crashed`, `wrong_result`, `failed` and `n/a`. A
`wrong_result` or `n/a` record always carries the reason. Recompute the integration overhead (M7)
from the stored M1 records at any time:

```bash
scripts/dev.sh dbench overhead talk-sf100
```

Other commands: `dbench power` (one system's M1 run) and `dbench trackb` (Track B alone); see
`scripts/dev.sh dbench <command> --help`.

## 10. Build the site

```bash
scripts/dev.sh python -m sitegen.build --results "$DBENCH_RESULTS" --out _site
python3 -m http.server --directory _site 8000      # then open http://localhost:8000/
```

The site is static (HTML, vendored ECharts and `data/*.json`). The root pages show every result,
using for each system and scale factor only one run, the one with the most records. Each campaign
is also a report of its own under `reports/<run_id>/`, with the same pages built from that
campaign's records alone (Track B's `<run_id>-trackb` belongs to it); `reports.html` lists the
reports and compares a measure across them. A report is dated by its records' start times (UTC):
the switcher, report banner, report cards and comparison columns show the day its last record
started, with the time added when two reports end on the same day. Every overview opens with findings computed from its
records. To publish a run for reference only, list it under `test_runs` in `<results>/runs.yaml` with a reason: it keeps its own report, labelled as a test run, and is left out of the root pages and the comparison across reports. To add your own reading of a report, put it in `<results>/notes/<run_id>.md`; it is shown
on that report's overview under "Author's notes", separate from the findings. The Explore page
compares two systems query by query, shows one query across all systems and filters every probe
result; its choices are kept in the page address, and its tables download as CSV. Publishing is deliberate: the Pages workflow
builds from the tracked `published/` directory, so copy the records you want to publish there
and commit them; see `published/README.md`.

## When a campaign stops

The campaign stops with a message and a non-zero exit code when its gate fails, for example when
InnoDB or PostgreSQL disagrees with the reference, or when the reference has no result for a
query. [campaigns/README.md](../campaigns/README.md#when-the-gate-blocks) lists each case, what
to delete and how to continue. A system left running after a failed cleanup is removed with
`docker rm -f dbench-<system>`.
