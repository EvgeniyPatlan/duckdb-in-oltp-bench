"""pg_duckdb 1.1.1 on PostgreSQL 18: DuckDB executes analytical queries over ordinary heap tables.

Image ``pgduckdb/pgduckdb:18-v1.1.1`` (bundles DuckDB 1.4.3); settings come from
``systems/pgduckdb/postgresql.conf``, rendered from the limits and bind-mounted.
"""
from __future__ import annotations

from typing import Any

from dbench import dockerctl
from dbench.config import Limits
from dbench.datagen import DatasetPaths
from dbench.systems import baseline_conf
from dbench.systems.base import LoadOutcome, Pushed
from dbench.systems.postgres import CONF_TARGET
from dbench.systems.postgres_family import DATABASE, PostgresFamilyAdapter

IMAGE = "pgduckdb/pgduckdb:18-v1.1.1"
NO_CONVERSION = "queries normal Postgres tables directly; no conversion step"
NO_COPY = "no separate DuckDB copy to keep fresh"
DUCKDB_VERSION_SQL = "SELECT * FROM duckdb.query('SELECT version()')"
EXTVERSION_SQL = "SELECT extversion FROM pg_extension WHERE extname = '{ext}'"


def render_config(limits: Limits, config_dir: str = "pgduckdb", mode: str = "pgduckdb") -> str:
    return baseline_conf.render(
        baseline_conf.SYSTEMS_DIR / config_dir / "postgresql.conf",
        baseline_conf.duckdb_pg_settings(limits, mode))


class PgDuckBase(PostgresFamilyAdapter):
    """Container, config and pushdown glue shared by pg_duckdb and pg_ducklake."""

    track = "A"
    mode: str  # duckdb_pg_settings mode
    datadir = "/var/lib/postgresql"
    du_command = 'du -sb "$PGDATA"'
    extension: str  # pg_extension name

    def container_env(self) -> dict[str, str]:
        return {"POSTGRES_HOST_AUTH_METHOD": "trust", "POSTGRES_DB": DATABASE}

    def container_command(self, limits: Limits) -> list[str]:
        return ["postgres", "-c", f"config_file={CONF_TARGET}"]

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            render_config(limits, self.config_dir, self.mode), self.name, "postgresql.conf",
            self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}

    def connect(self) -> Any:
        conn = self._connect()
        self._session_setup(conn)
        return conn

    def _query_begin(self, conn, sql: str) -> Pushed:
        return self._explain_pushed(conn, sql)

    def _query_end(self, conn, token: Pushed) -> Pushed:
        return token

    def pushdown(self, sql: str) -> Pushed:
        """Classify on the persistent session, the one ``execute()`` runs on."""
        try:
            return self._explain_pushed(self._session(), sql)
        except Exception:  # noqa: BLE001 - detection must never raise
            self._drop_session()
            return "unknown"

    def _duckdb_version_sql(self) -> str:
        return DUCKDB_VERSION_SQL

    def versions(self) -> dict:
        info = super().versions()
        ext = duck = None
        try:
            conn = self._connect()
            try:
                ext = conn.execute(EXTVERSION_SQL.format(ext=self.extension)).fetchone()
                duck = conn.execute(self._duckdb_version_sql()).fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - versions() must not fail on a missing function
            pass
        return {**info, "extension": f"{self.extension} {ext[0] if ext else 'unknown'}",
                "duckdb": str(duck[0]).removeprefix("v") if duck else "unknown",
                "duckdb_source": self._duckdb_version_sql()}


class PgDuckDB(PgDuckBase):
    """Heap tables with the row-store schema, queried with ``duckdb.force_execution = true``.
    Pushdown is read from a plan-only ``EXPLAIN`` taken before the timed region."""

    name = "pg_duckdb"
    config_dir = "pgduckdb"
    mode = "pgduckdb"
    image = IMAGE
    bench_schema_kind = "rowstore"
    extension = "pg_duckdb"

    def supports(self, measure: str) -> tuple[bool, str | None]:
        if measure == "M11":
            return False, NO_COPY
        return True, None

    def load_convert(self, data: DatasetPaths) -> LoadOutcome:
        return LoadOutcome("n/a", None, None, {"reason": NO_CONVERSION})

    # pushdown
    def _session_setup(self, conn) -> None:
        conn.execute("SET duckdb.force_execution = true")
