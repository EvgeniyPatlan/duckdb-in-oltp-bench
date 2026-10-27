"""System name -> adapter. Later tasks register more systems here."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from dbench.systems.base import SystemAdapter

DUCKDB_PREFIX = "duckdb-"


def _factories() -> dict[str, Callable[[Path | str | None], SystemAdapter]]:
    from dbench.systems.innodb import InnoDB
    from dbench.systems.mariadb import MariaDBDuckDB
    from dbench.systems.percona import PerconaEngine
    from dbench.systems.postgres import Postgres

    return {"innodb": InnoDB, "mariadb": MariaDBDuckDB, "percona": PerconaEngine,
            "postgres": Postgres}


def known_systems() -> list[str]:
    return [f"{DUCKDB_PREFIX}<version>", *sorted(_factories())]


def get_adapter(name: str, data_dir: Path | str | None) -> SystemAdapter:
    if name.startswith(DUCKDB_PREFIX) and len(name) > len(DUCKDB_PREFIX):
        from dbench.systems.duckdb_native import DuckDBNative

        return DuckDBNative(name.removeprefix(DUCKDB_PREFIX), data_dir=data_dir)
    factory = _factories().get(name)
    if factory is None:
        raise ValueError(f"unknown system {name!r}; expected one of {known_systems()}")
    return factory(data_dir)
