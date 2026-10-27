"""Command-line entry point for dbench."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from dbench import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbench",
        description="Benchmark DuckDB integrations in MySQL, MariaDB and PostgreSQL servers.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
