#!/usr/bin/env bash
# Build every locally built image and pull the registry images at the digests pinned in
# systems/versions.lock.json, so `dbench lock` can refresh the local image ids.
# Tags must match the images named in dbench/systems/. Needs docker with buildx.
# TMPDIR defaults to <parent of $DBENCH_DATA>/tmp.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Builds write large temporary files. Keep them beside the data (the big disk), not in /tmp on
# the root disk. An explicit TMPDIR wins; otherwise use <parent of $DBENCH_DATA>/tmp.
if [ -z "${TMPDIR:-}" ] && [ -n "${DBENCH_DATA:-}" ]; then
    data_real="$(realpath -m "$DBENCH_DATA")"
    data_parent="$(dirname "$data_real")"
    case "$data_parent" in
        / | /tmp | /tmp/*)
            echo "error: DBENCH_DATA=$DBENCH_DATA resolves to $data_real, whose parent" \
                 "$data_parent is the root disk or /tmp; put the data under a big disk" \
                 "(e.g. /srv/dbench/data) or set TMPDIR explicitly." >&2
            exit 1 ;;
    esac
    TMPDIR="$data_parent/tmp"
fi
if [ -n "${TMPDIR:-}" ]; then
    mkdir -p "$TMPDIR"
    export TMPDIR
    echo "using TMPDIR=$TMPDIR"
else
    echo "warning: TMPDIR and DBENCH_DATA are unset; builds will use /tmp (root disk)" >&2
fi
if ! docker buildx version >/dev/null 2>&1; then
    echo "error: 'docker buildx' is not available; install the Docker buildx plugin" \
         "(docker-buildx-plugin) and run this script again." >&2
    exit 1
fi

DUCKDB="$ROOT/systems/duckdb"

build_duckdb() { # version [extra build args...]
    local v="$1"; shift
    docker build -t "dbench/duckdb:$v" --build-arg "DUCKDB_VERSION=$v" "$@" "$DUCKDB"
}

# From PyPI.
for v in 1.5.5 1.4.3 1.5.3; do build_duckdb "$v"; done
# From a DuckDB source commit (keep in step with DUCKDB_SOURCE_BUILDS in dbench/lock.py).
build_duckdb 1.5.2-dev736 \
    --build-arg DUCKDB_SOURCE_COMMIT=8a66223119b16000f3814fa7150cfa7370b63209 \
    --build-arg DUCKDB_PYTHON_TAG=v1.5.2 \
    --build-arg DUCKDB_GIT_DESCRIBE=v1.5.1-736-g8a6622311

docker build -t dbench/alisql:8.0.44-3 "$ROOT/systems/alisql"
docker build -t dbench/mariadb-duckdb:13.0.2 "$ROOT/systems/mariadb"
docker build -t dbench/villagesql:0.0.7-duckdb-0.0.3 "$ROOT/systems/villagesql"
bash "$ROOT/systems/pgducklake/build.sh"   # clones pg_ducklake at a pinned commit; ~20 minutes

# Registry images: pull by the committed digest, then give the digest the tag the adapters use.
"$ROOT/scripts/dev.sh" python -c '
import json
for system, e in json.load(open("systems/versions.lock.json")).items():
    if e["digest_kind"] == "registry":
        print(e["image"], e["digest"])
' | tr -d '\r' | while read -r image digest; do
    repo="${image%%@*}"; repo="${repo%:*}"
    docker pull "${repo}@${digest}"
    case "$image" in *@*) ;; *) docker tag "${repo}@${digest}" "$image" ;; esac
done
