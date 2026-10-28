#!/usr/bin/env bash
# Run a command inside the dev container with the repo mounted at /w.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${DBENCH_DEV_IMAGE:-dbench-dev:latest}"

if [ "$#" -eq 0 ]; then
    echo "usage: scripts/dev.sh <command...>" >&2
    exit 2
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" -f "$ROOT/docker/dev.Dockerfile" "$ROOT"
fi

args=(--rm -v "$ROOT:/w" -w /w -e HOME=/tmp
      --user "$(id -u):$(id -g)")

# Pin the runner to its own cores (a campaign refuses to start unless its affinity is inside
# the campaign's runner_cpuset).
if [ -n "${DBENCH_RUNNER_CPUSET:-}" ]; then
    args+=(--cpuset-cpus "$DBENCH_RUNNER_CPUSET")
fi

if [ -S /var/run/docker.sock ]; then
    args+=(-v /var/run/docker.sock:/var/run/docker.sock
           --group-add "$(stat -c %g /var/run/docker.sock)")
fi

while IFS= read -r name; do
    args+=(-e "$name")
done < <(env | grep -o '^DBENCH_[A-Za-z0-9_]*' || true)

# Data and results roots outside the repo are bind-mounted at the same absolute path, so
# the same spelling works inside the container and for the docker daemon.
for var in DBENCH_DATA DBENCH_RESULTS; do
    dir="${!var:-}"
    if [ -n "$dir" ]; then
        mkdir -p "$dir"
        dir="$(cd "$dir" && pwd)"
        args+=(-v "$dir:$dir" -e "$var=$dir")
    fi
done

# Set last so a host value cannot override it.
args+=(-e "DBENCH_HOST_REPO=$ROOT")

[ -t 0 ] && [ -t 1 ] && args+=(-it)

exec docker run "${args[@]}" "$IMAGE" "$@"
