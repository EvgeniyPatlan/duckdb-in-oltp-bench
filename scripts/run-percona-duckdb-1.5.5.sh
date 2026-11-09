#!/usr/bin/env bash
# Build Percona's engine v0.2.4 on DuckDB 1.5.5 and add it to the talk-sf100 run as
# percona-duckdb-1.5.5 (see systems/percona/README.md). Run on the bench host from the bench
# repo, inside screen: every step checks its result and the script stops at the first failure.
# Steps that are already done are skipped, so it can be run again after a fix.
#
#   scripts/run-percona-duckdb-1.5.5.sh            # everything, ending with the SF100 run
#   SKIP_CAMPAIGN=1 scripts/run-percona-duckdb-1.5.5.sh   # stop after the checks
#
# Environment (defaults for the bench host):
#   ENGINE_DIR      engine checkout (default: ../ducksdb-mysql-engine next to this repo)
#   DBENCH_DATA     dataset parent          DBENCH_RESULTS  results root
#   JOBS            build parallelism (default 64)
#   SMOKE_SF        scale factor of the small check (default: 1 if generated, else 10)
#   SKIP_CAMPAIGN   set to stop after the checks, before the SF100 run
set -euo pipefail

BENCH=$(cd "$(dirname "$0")/.." && pwd)
ENGINE_DIR=${ENGINE_DIR:-$(cd "$BENCH/.." && pwd)/ducksdb-mysql-engine}
export DBENCH_DATA=${DBENCH_DATA:-/data/bench/DUCKSDB/dbench/data}
export DBENCH_RESULTS=${DBENCH_RESULTS:-/data/bench/DUCKSDB/dbench/results}
JOBS=${JOBS:-64}
# The small check is its own run under the results root (scripts/dev.sh mounts only
# DBENCH_DATA and DBENCH_RESULTS); it is not part of talk-sf100.
SMOKE_RUN=smoke-percona-duckdb-1.5.5

ENGINE_COMMIT=4033a7a52736418f13f879ce8c9f83911cf1d3a1
MYSQL_TAG=mysql-9.7.0
MYSQL_COMMIT=845d525d49c8027a4d0cdcc43372c96ba295c857
DUCKDB_TAG=v1.5.5
DUCKDB_SHA=d8cdaa33fd
IMAGE=dbench/percona-duckdb:9.7-v0.2.4-duckdb-1.5.5
SYSTEM=percona-duckdb-1.5.5
BUILD_REL=vendor/mysql-server/build-duckdb-1.5.5
PREFIX_REL=vendor/duckdb-prefix-1.5.5

step() { printf '\n=== %s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }
builder() {  # run a command in the engine builder container, engine repo at /work
    docker run --rm -v "$ENGINE_DIR":/work -e REPO=/work -e JOBS="$JOBS" "$@"
}

step "0. bench checkout"
cd "$BENCH"
git merge-base --is-ancestor 9de0757 HEAD \
    || die "this checkout lacks the percona-duckdb-1.5.5 commits: git fetch && git checkout percona-duckdb-1.5.5"
grep -q "$ENGINE_COMMIT" dbench/systems/percona.py || die "dbench/systems/percona.py names another engine commit"

step "1. engine checkout at $ENGINE_COMMIT"
if [ ! -d "$ENGINE_DIR/.git" ]; then
    git clone https://github.com/EvgeniyPatlan/ducksdb-mysql-engine.git "$ENGINE_DIR"
fi
if [ -n "$(git -C "$ENGINE_DIR" status --porcelain -- common engine scripts server-patches docker)" ]; then
    die "the engine checkout has local changes in common/ engine/ scripts/ server-patches/ docker/"
fi
git -C "$ENGINE_DIR" fetch -q origin
git -C "$ENGINE_DIR" checkout -q "$ENGINE_COMMIT"  # exactly the recorded commit (detached)
[ "$(git -C "$ENGINE_DIR" rev-parse HEAD)" = "$ENGINE_COMMIT" ] || die "engine checkout failed"

step "1b. MySQL $MYSQL_TAG source and the storage/duckdb link"
cd "$ENGINE_DIR"
if [ ! -d vendor/mysql-server/.git ]; then
    git clone --depth 1 --branch "$MYSQL_TAG" https://github.com/mysql/mysql-server.git vendor/mysql-server
fi
[ "$(git -c safe.directory='*' -C vendor/mysql-server rev-parse HEAD)" = "$MYSQL_COMMIT" ] \
    || die "vendor/mysql-server is not $MYSQL_TAG ($MYSQL_COMMIT)"
[ -L vendor/mysql-server/storage/duckdb ] || ln -s ../../../engine vendor/mysql-server/storage/duckdb
if ! docker image inspect ducksdb-builder:latest >/dev/null 2>&1; then
    docker build -t ducksdb-builder:latest -f docker/builder.Dockerfile .
fi

step "2. DuckDB $DUCKDB_TAG prefix"
if [ ! -f "$PREFIX_REL/lib/libduckdb.a" ]; then
    builder ducksdb-builder:latest bash -lc "cd /work && DUCKDB_TAG=$DUCKDB_TAG scripts/build-duckdb-prefix.sh" \
        | tail -3
fi
[ -f "$PREFIX_REL/lib/libduckdb.a" ] || die "no $PREFIX_REL/lib/libduckdb.a"
# cloned as root inside the builder: read it without trusting it in the global git config
got=$(git -c safe.directory='*' -C "vendor/duckdb-src-${DUCKDB_TAG#v}" rev-parse --short=10 HEAD)
[ "$got" = "$DUCKDB_SHA" ] || die "DuckDB source is $got, expected the $DUCKDB_TAG release $DUCKDB_SHA"

step "3. server on DuckDB $DUCKDB_TAG in $BUILD_REL (RelWithDebInfo)"
builder -e BUILD_TYPE=RelWithDebInfo -e DUCKDB_PREFIX="/work/$PREFIX_REL" \
    -e MYSQL_BUILD="/work/$BUILD_REL" ducksdb-builder:latest \
    bash -lc 'cd /work && scripts/build-server.sh' > "$BENCH/percona-1.5.5-build.log" 2>&1 \
    || { tail -40 "$BENCH/percona-1.5.5-build.log"; die "server build failed (log: $BENCH/percona-1.5.5-build.log)"; }
grep -q "using prebuilt prefix (lib=/work/$PREFIX_REL" "$BENCH/percona-1.5.5-build.log" \
    || grep -q "reusing existing RelWithDebInfo configure" "$BENCH/percona-1.5.5-build.log" \
    || die "the build did not use $PREFIX_REL (log: $BENCH/percona-1.5.5-build.log)"
[ -x "$BUILD_REL/runtime_output_directory/mysqld" ] || die "no mysqld in $BUILD_REL"

step "4. image $IMAGE"
MYSQL_BUILD="$BUILD_REL" scripts/release-image.sh "$IMAGE" > "$BENCH/percona-1.5.5-image.log" 2>&1 \
    || { tail -30 "$BENCH/percona-1.5.5-image.log"; die "image build failed"; }

step "5. the image runs DuckDB 1.5.5 and pushes queries down"
docker rm -f p155check >/dev/null 2>&1 || true
docker run -d --name p155check -e MYSQL_ROOT_PASSWORD= "$IMAGE" >/dev/null
trap 'docker rm -f p155check >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 60); do
    docker exec p155check mysqladmin -uroot ping >/dev/null 2>&1 && break
    sleep 2
done
q() { docker exec p155check mysql -uroot -N -s -e "$1"; }
version=$(q "SHOW GLOBAL STATUS LIKE 'Ducksdb_duckdb_version'" | cut -f2)
source_id=$(q "SHOW GLOBAL STATUS LIKE 'Ducksdb_duckdb_source_id'" | cut -f2)
echo "  embedded DuckDB: $version ($source_id)"
[ "$version" = "$DUCKDB_TAG" ] || die "the server reports DuckDB $version, expected $DUCKDB_TAG"
case "$source_id" in "$DUCKDB_SHA"*) ;; *) die "source id $source_id is not the $DUCKDB_TAG release $DUCKDB_SHA";; esac
q "CREATE DATABASE t; CREATE TABLE t.x (id INT PRIMARY KEY, v INT) ENGINE=DuckDB;
   INSERT INTO t.x VALUES (1,10),(2,20),(3,30)"
before=$(q "SHOW GLOBAL STATUS LIKE 'Ducksdb_pushdown_count'" | cut -f2)
answer=$(q "SELECT SUM(v), AVG(v) FROM t.x")
after=$(q "SHOW GLOBAL STATUS LIKE 'Ducksdb_pushdown_count'" | cut -f2)
echo "  SUM, AVG = $answer; pushdown count $before -> $after"
[ "$(echo "$answer" | cut -f1)" = "60" ] || die "wrong answer: $answer"
[ "$after" -gt "$before" ] || die "the aggregate was not pushed down (extensions missing in the 1.5.5 build?)"
docker rm -f p155check >/dev/null
trap - EXIT

step "6. lock $SYSTEM (every other entry stays)"
cd "$BENCH"
cp systems/versions.lock.json "systems/versions.lock.json.before-$SYSTEM"
scripts/dev.sh dbench lock --system "$SYSTEM"
python3 - "systems/versions.lock.json.before-$SYSTEM" systems/versions.lock.json "$SYSTEM" <<'EOF'
import json, sys
old, new, system = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
changed = [k for k in old if old[k] != new.get(k)]
if changed:
    sys.exit(f"STOPPED: the lock changed other entries: {changed}")
e = new[system]
print(f"  {system}: {e['digest_kind']} {e['digest'][:19]}  duckdb {e['duckdb_version']}")
EOF

step "7. small check: M1 at a small scale against plain DuckDB 1.5.5"
SMOKE_SF=${SMOKE_SF:-$([ -d "$DBENCH_DATA/tpch/sf1" ] && echo 1 || echo 10)}
[ -d "$DBENCH_DATA/tpch/sf$SMOKE_SF" ] || die "no dataset at $DBENCH_DATA/tpch/sf$SMOKE_SF"
rm -rf "${DBENCH_RESULTS:?}/$SMOKE_RUN"
DBENCH_RUNNER_CPUSET=8-11 scripts/dev.sh dbench power --system "$SYSTEM" --sf "$SMOKE_SF" \
    --compare-with duckdb-1.5.5 --run-id "$SMOKE_RUN" | tail -25
python3 - "$DBENCH_RESULTS/$SMOKE_RUN/$SYSTEM" <<'EOF'
import collections, glob, json, sys
recs = [json.load(open(p)) for p in glob.glob(f"{sys.argv[1]}/M1-*.json")]
status = collections.Counter(r["status"] for r in recs)
pushed = collections.Counter(r["extra"].get("pushed") for r in recs)
duck = collections.Counter(r["versions"].get("duckdb") for r in recs)
print(f"  {len(recs)} records; status {dict(status)}; pushed {dict(pushed)}; duckdb {dict(duck)}")
problems = []
if status != collections.Counter({"ok": 22}):
    problems.append("not 22 ok results (a mismatch is wrong_result)")
if pushed != collections.Counter({"duckdb": 22}):
    problems.append("not every query was pushed down")
if duck != collections.Counter({"1.5.5": 22}):
    problems.append("records do not all name DuckDB 1.5.5")
if problems:
    sys.exit("STOPPED: " + "; ".join(problems))
EOF

if [ -n "${SKIP_CAMPAIGN:-}" ]; then
    step "checks passed; SKIP_CAMPAIGN set, not starting the SF100 run"
    exit 0
fi

step "8. SF100: $SYSTEM in talk-sf100 (load, M1, M4 at 1-32 streams; about 2.5-3 h)"
export DBENCH_RUNNER_CPUSET=16-23
scripts/dev.sh dbench campaign campaigns/talk-sf100.yaml

step "9. lock check and summary"
scripts/dev.sh dbench lock-manifest talk-sf100 || true
python3 - "$DBENCH_RESULTS/talk-sf100/$SYSTEM" <<'EOF'
import collections, glob, json, sys
recs = [json.load(open(p)) for p in glob.glob(f"{sys.argv[1]}/*.json")]
print("  status per measure:", dict(collections.Counter((r["measure"], r["status"]) for r in recs)))
for r in sorted((r for r in recs if r["measure"] == "M4"), key=lambda r: int(r["item"].split("=")[1])):
    print(f"  M4 {r['item']:11} {r['status']:8} {r['value'] if r['value'] is None else round(r['value'], 1)}"
          f"  {r['extra'].get('counts_by_status')}")
EOF
echo
echo "Done. Copy $DBENCH_RESULTS/talk-sf100/{$SYSTEM,_lock} and systems/versions.lock.json back."
