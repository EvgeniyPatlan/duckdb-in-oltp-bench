"""PostgreSQL 18: the row-store baseline for the Postgres-family engines."""
from __future__ import annotations

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems import baseline_conf
from dbench.systems.postgres_family import DATABASE, PostgresFamilyAdapter

CONF_TARGET = "/etc/postgresql/postgresql.conf"


class Postgres(PostgresFamilyAdapter):
    bench_schema_kind = "rowstore"
    name = "postgres"
    config_dir = "postgres"
    image = "postgres:18"
    # The 18+ image keeps PGDATA under /var/lib/postgresql/<major>/docker.
    datadir = "/var/lib/postgresql"
    du_command = 'du -sb "$PGDATA"'

    def container_env(self) -> dict[str, str]:
        # The container is only reachable on the private docker network.
        return {"POSTGRES_HOST_AUTH_METHOD": "trust", "POSTGRES_DB": DATABASE}

    def container_command(self, limits: Limits) -> list[str]:
        return ["postgres", "-c", f"config_file={CONF_TARGET}"]

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            baseline_conf.render_postgres_conf(limits), self.name, "postgresql.conf",
            self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}
