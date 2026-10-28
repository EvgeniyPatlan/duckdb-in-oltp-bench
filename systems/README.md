# Systems

One row per pinned system in [`versions.lock.json`](versions.lock.json). Each system has a folder
with its image recipe, its server configuration (every non-default setting with the reason for
it) and notes on how it was set up; follow the link in the first column.

- **System** is the name used on the command line and in campaign files. **Label on the site** is
  the name shown on the results site.
- **Track A** systems are compared with each other; **Track B** (VillageSQL) is reported
  separately because it is built differently. See the [README](../README.md#the-two-tracks).
- **DuckDB** is the DuckDB version inside the system. `bundled; see versions in records` means the
  system ships its own DuckDB and the version is read from the running server and stored in every
  result record, not fixed here.
- **Pin**: `registry digest` images are pulled by the digest in the lock file. `local build`
  images are built from the recipe in the system's folder with every input pinned (base image
  digests, package versions, checksums, commits); the lock records the local image id, which is
  specific to the host that built the image and is refreshed there with `dbench lock`.
- Track B also runs the raw DuckDB entry points of three of these images: plain DuckDB 1.5.5,
  pg_duckdb (`duckdb.query`) and MariaDB (`run_in_duckdb`). They use the same images, so they have
  no row of their own.

This table is generated from the lock file and the site's labels. A test
(`tests/test_docs.py`) fails when the two disagree. To regenerate it:

```bash
scripts/dev.sh env PYTHONPATH=. python tests/test_docs.py
```

<!-- table:begin -->
| System | Track | Label on the site | Server | Integration | DuckDB | Image | Pin |
|---|---|---|---|---|---|---|---|
| [`alisql`](alisql/README.md) | A | AliSQL | AliSQL 8.0.44-3 (MySQL 8.0.44) | built into the server | 1.5.2-dev736 | `dbench/alisql:8.0.44-3` | local build |
| [`duckdb-1.4.3`](duckdb/README.md) | A | DuckDB 1.4.3 | DuckDB 1.4.3 | none (plain DuckDB) | 1.4.3 | `dbench/duckdb:1.4.3` | local build |
| [`duckdb-1.5.2-dev736`](duckdb/README.md) | A | DuckDB 1.5.2-dev736 | DuckDB 1.5.2-dev736 | none (plain DuckDB) | 1.5.2-dev736 | `dbench/duckdb:1.5.2-dev736` | local build |
| [`duckdb-1.5.3`](duckdb/README.md) | A | DuckDB 1.5.3 | DuckDB 1.5.3 | none (plain DuckDB) | 1.5.3 | `dbench/duckdb:1.5.3` | local build |
| [`duckdb-1.5.5`](duckdb/README.md) | A | DuckDB 1.5.5 | DuckDB 1.5.5 | none (plain DuckDB) | 1.5.5 | `dbench/duckdb:1.5.5` | local build |
| [`innodb`](innodb/README.md) | A | MySQL InnoDB | MySQL 9.7 (InnoDB) | none (row-store baseline) | none | `mysql:9.7` | registry digest |
| [`mariadb`](mariadb/README.md) | A | MariaDB | MariaDB 13.0.2 | MariaDB-duckdb-engine | bundled; see versions in records | `dbench/mariadb-duckdb:13.0.2` | local build |
| [`percona`](percona/README.md) | A | Percona | MySQL 9.7.0 | Percona ducksdb-mysql-engine v0.2.3 | 1.5.3 | `perconalab/ducksdb-mysql-engine:9.7-duckdb-v0.2.3` | registry digest |
| [`pg_duckdb`](pgduckdb/README.md) | A | pg_duckdb | PostgreSQL 18 | pg_duckdb v1.1.1 | bundled; see versions in records | `pgduckdb/pgduckdb:18-v1.1.1` | registry digest |
| [`pg_ducklake`](pgducklake/README.md) | A | pg_ducklake | PostgreSQL 18 | pg_ducklake v1.0.3 | bundled; see versions in records | `dbench/pgducklake:18-v1.0.3` | local build |
| [`postgres`](postgres/README.md) | A | PostgreSQL | PostgreSQL 18 | none (row-store baseline) | none | `postgres:18` | registry digest |
| [`villagesql`](villagesql/README.md) | B | VillageSQL | VillageSQL 0.0.7 (MySQL 8.4) | vsql_duckdb 0.0.3 | 1.5.5 | `dbench/villagesql:0.0.7-duckdb-0.0.3` | local build |
<!-- table:end -->
