#!/bin/sh
# Minimal entrypoint: initialise an empty datadir, then run mariadbd with the rendered config
# (mounted at /etc/my.cnf.d/dbench.cnf). Root has no password; the server is reachable only on
# the benchmark's docker network (no published port).
set -eu
DATADIR=/var/lib/mysql
SPILL=$DATADIR/duckdb_tmp
if [ ! -d "$DATADIR/mysql" ]; then
    cat > /tmp/init.sql <<SQL
FLUSH PRIVILEGES;
CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED VIA mysql_native_password USING PASSWORD('');
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;
CREATE DATABASE IF NOT EXISTS tpch;
SQL
    mariadb-install-db --user=root --datadir="$DATADIR" --skip-test-db \
        --auth-root-authentication-method=normal --extra-file=/tmp/init.sql
fi
mkdir -p "$SPILL"
exec mariadbd --user=root --datadir="$DATADIR" "$@"
