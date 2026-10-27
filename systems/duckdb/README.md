# Plain DuckDB reference

DuckDB behind a small TCP JSON-lines query server (`server.py`), so query timing is
measured server-side and excludes client startup. One cursor per TCP connection on the
database file `/var/lib/duckdb/tpch.duckdb`.

Images (`dbench/duckdb:<version>`):

```
docker build -t dbench/duckdb:1.4.3 --build-arg DUCKDB_VERSION=1.4.3 systems/duckdb
docker build -t dbench/duckdb:1.5.5 --build-arg DUCKDB_VERSION=1.5.5 systems/duckdb
# AliSQL snapshot (duckdb/duckdb commit 8a66223), built from source, version v1.5.2-dev736
docker build -t dbench/duckdb:1.5.2-dev736 \
  --build-arg DUCKDB_VERSION=1.5.2-dev736 \
  --build-arg DUCKDB_SOURCE_COMMIT=8a66223119b16000f3814fa7150cfa7370b63209 \
  --build-arg DUCKDB_PYTHON_TAG=v1.5.2 \
  --build-arg DUCKDB_GIT_DESCRIBE=v1.5.1-736-g8a6622311 systems/duckdb
```

Settings (from the container limits): `threads` = cores in the DB cpuset,
`memory_limit` = 80% of the container memory (headroom for non-buffer allocations).

Schema: TPC-H tables with primary keys, no other indexes. There is no row store, so
M11 and M12 are n/a.
