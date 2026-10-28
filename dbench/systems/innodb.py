"""MySQL 9.7 with InnoDB: the row-store baseline for the MySQL-family engines."""
from __future__ import annotations

from dbench import dockerctl
from dbench.config import Limits
from dbench.systems import baseline_conf
from dbench.systems.mysql_family import DATABASE, MySQLFamilyAdapter

CONF_TARGET = "/etc/mysql/conf.d/dbench.cnf"


class InnoDB(MySQLFamilyAdapter):
    name = "innodb"
    config_dir = "innodb"
    bench_schema_kind = "rowstore"
    image = "mysql:9.7"
    engine_clause = "ENGINE=InnoDB"
    no_checkpoint_reason = "MySQL has no checkpoint statement; InnoDB flushes pages continuously"
    # Started as mysql so the image entrypoint does not try to chown the read-only /data
    # (secure_file_priv) mount.
    container_user = "mysql"

    def container_env(self) -> dict[str, str]:
        return {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes", "MYSQL_DATABASE": DATABASE}

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            baseline_conf.render_innodb_cnf(limits), self.name, "my.cnf", self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}
