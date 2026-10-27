#!/bin/sh
# Minimal entrypoint: initialise an empty datadir, then run mysqld with the rendered config
# (mounted at /etc/alisql/dbench.cnf). Root has no password and may connect from any host; the
# server is reachable only on the benchmark's docker network (no published port).
set -eu
DATADIR=/var/lib/mysql
CNF=/etc/alisql/dbench.cnf
SPILL=$DATADIR/duckdb_tmp
if [ ! -d "$DATADIR/mysql" ]; then
    cat > /tmp/init.sql <<SQL
CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED WITH mysql_native_password BY '';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;
CREATE DATABASE IF NOT EXISTS tpch;
SQL
    mysqld --defaults-file="$CNF" --user=root --datadir="$DATADIR" \
        --initialize-insecure --init-file=/tmp/init.sql
fi
mkdir -p "$SPILL"  # after --initialize, which needs an empty datadir
exec mysqld --defaults-file="$CNF" --user=root --datadir="$DATADIR" "$@"
