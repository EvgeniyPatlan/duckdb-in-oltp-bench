# duckdb-in-oltp-bench

A neutral, reproducible benchmark of DuckDB integrations in MySQL, MariaDB and
PostgreSQL servers. Everything runs in Docker through the `dbench` command-line
tool.

Status: work in progress.

## Results site

`python -m sitegen.build --results <dir> --out _site` builds the static site (pages, charts,
`data/*.json`) from result records; charts use a vendored ECharts (`sitegen/static/`, Apache-2.0).
The Pages workflow (`.github/workflows/pages.yml`) builds from the tracked `published/` directory:
curated records copied there and committed deliberately at publication time. It runs the test
suite, then refuses to deploy when `published/` holds no records or the built site is marked
synthetic. `tests/fixtures/synthetic.py` writes a clearly labelled fixture set for previews.
