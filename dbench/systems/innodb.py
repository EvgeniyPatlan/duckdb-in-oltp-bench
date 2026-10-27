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
    image = "mysql:9.7"
    datadir = "/var/lib/mysql"
    engine_clause = "ENGINE=InnoDB"
    # Binlog files are a logging artifact, not table storage, so they are not counted.
    du_command = "du -sb --exclude='binlog.*' /var/lib/mysql"

    def container_env(self) -> dict[str, str]:
        return {"MYSQL_ALLOW_EMPTY_PASSWORD": "yes", "MYSQL_DATABASE": DATABASE}

    def config_mounts(self, limits: Limits) -> dict:
        path = baseline_conf.write_rendered(
            baseline_conf.render_innodb_cnf(limits), self.name, "my.cnf", self.rendered_dir)
        return {dockerctl.host_path(path): {"bind": CONF_TARGET, "mode": "ro"}}
