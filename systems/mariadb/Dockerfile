# MariaDB 12.3.3 (LTS) with the DuckDB storage engine (the official Docker image does not ship it).
# Official MariaDB 12.3 RPM repo for EL9; server, client and engine are version-pinned.
FROM rockylinux:9@sha256:d7be1c094cc5845ee815d4632fe377514ee6ebcf8efaed6892889657e5ddaaa6
ARG MARIADB_VERSION=12.3.3-1.el9
RUN set -eux; \
    printf '%s\n' \
        '[mariadb]' \
        'name = MariaDB 12.3' \
        'baseurl = https://rpm.mariadb.org/12.3/rhel/9/x86_64' \
        'gpgkey = https://supplychain.mariadb.com/MariaDB-Server-GPG-KEY' \
        'gpgcheck = 1' \
        'enabled = 1' > /etc/yum.repos.d/mariadb.repo; \
    rpm --import https://supplychain.mariadb.com/MariaDB-Server-GPG-KEY; \
    dnf install -y \
        "MariaDB-server-${MARIADB_VERSION}" \
        "MariaDB-client-${MARIADB_VERSION}" \
        "MariaDB-duckdb-engine-${MARIADB_VERSION}"; \
    dnf clean all; rm -rf /var/cache/dnf; \
    rm -rf /var/lib/mysql/* /var/lib/mysql/.[!.]*  # the RPM pre-creates a datadir; start empty
COPY entrypoint.sh /entrypoint.sh
VOLUME /var/lib/mysql
EXPOSE 3306
ENTRYPOINT ["/entrypoint.sh"]
