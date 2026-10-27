#!/usr/bin/env bash
# Export the 22 canonical TPC-H queries from DuckDB's tpch extension.
# Runs in a throwaway container; nothing is installed on the host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/dbench/queries/tpch"
DUCKDB_VERSION=1.5.5
mkdir -p "$OUT"

docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -v "$OUT:/out" python:3.12-slim bash -c '
set -euo pipefail
pip install --quiet --no-cache-dir --target /tmp/pkgs duckdb=='"$DUCKDB_VERSION"'
PYTHONPATH=/tmp/pkgs python - <<PY
import duckdb

con = duckdb.connect()
con.execute("INSTALL tpch; LOAD tpch;")
rows = con.execute("SELECT query_nr, query FROM tpch_queries() ORDER BY query_nr").fetchall()
assert [r[0] for r in rows] == list(range(1, 23)), "expected 22 queries"
for nr, query in rows:
    with open(f"/out/q{nr:02d}.sql", "w", newline="\n") as fh:
        fh.write(query + "\n")
PY
'
echo "wrote 22 queries to $OUT"
