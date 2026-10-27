#!/usr/bin/env bash
# Build dbench/pgducklake:18-v1.0.3 from the v1.0.3 tag with the project's own Dockerfile
# (Docker Hub has no v1.0.3 image; the newest release tag there is 18-v1.0.2).
# Needs git and docker buildx; takes roughly 15-20 minutes and several GB of disk.
set -euo pipefail

TAG="v1.0.3"
COMMIT="9fef142965aa4af3e9da17be7ce210ed90f12ed6"  # commit of the tag, checked after the clone
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone --depth 1 --branch "$TAG" --recurse-submodules --shallow-submodules \
    https://github.com/relytcloud/pg_ducklake.git "$WORK/src"
test "$(git -C "$WORK/src" rev-parse HEAD)" = "$COMMIT" \
    || { echo "tag $TAG is not commit $COMMIT" >&2; exit 1; }
(cd "$WORK/src" && REPO=dbench/pgducklake POSTGRES_VERSION=18 bash pg_ducklake/docker/docker-build.sh)
docker tag dbench/pgducklake:18-local "dbench/pgducklake:18-${TAG}"
docker rmi dbench/pgducklake:18-local
