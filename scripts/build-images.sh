#!/usr/bin/env bash
# Build every locally built image and pull the registry images, so `dbench lock` can pin them.
# Tags must match the images named in dbench/systems/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DUCKDB_VERSIONS="${DUCKDB_VERSIONS:-1.5.5 1.4.3}"

for v in $DUCKDB_VERSIONS; do
    docker build -t "dbench/duckdb:$v" --build-arg "DUCKDB_VERSION=$v" "$ROOT/systems/duckdb"
done
docker build -t dbench/alisql:8.0.44-3 "$ROOT/systems/alisql"
docker build -t dbench/mariadb-duckdb:13.0.2 "$ROOT/systems/mariadb"
docker build -t dbench/villagesql:0.0.7-duckdb-0.0.3 "$ROOT/systems/villagesql"
bash "$ROOT/systems/pgducklake/build.sh"   # clones pg_ducklake at a pinned commit; ~20 minutes

docker pull mysql:9.7
docker pull postgres:18
docker pull pgduckdb/pgduckdb:18-v1.1.1
docker pull perconalab/ducksdb-mysql-engine:9.7-duckdb-v0.2.3@sha256:5e4af8b2bede83db7b296be977a9c22fb30e8a05a565e3b3b0fe08fea9a96640
