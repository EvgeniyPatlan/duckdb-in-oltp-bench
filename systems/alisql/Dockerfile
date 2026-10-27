# AliSQL 8.0.44-3 (MySQL 8.0.44 with the DuckDB storage engine) from the official release tarball.
# There is no official Docker image. The tarball is verified by SHA-256 (computed on 2026-10-27
# from the asset published at the URL below; size 653924236 bytes).
# The binary is built on CentOS 7 and links OpenSSL 1.0.2 (libssl.so.10, libcrypto.so.10), which
# Ubuntu 24.04 no longer ships; the matching CentOS 7.9 openssl-libs RPM is added, also by SHA-256.
FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS fetch
ARG ALISQL_VERSION=8.0.44-3
ARG ALISQL_SHA256=9b3ca762483d14e4ee0206b543c1541df91efd02d7f1ba16fcd5a3c1d359fbf7
ARG OPENSSL_RPM=openssl-libs-1.0.2k-19.el7.x86_64.rpm
ARG OPENSSL_RPM_SHA256=8a907020f9b1697cb8971158b3a5279e5b54e5637f55dcce751f1610a2ed6452
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils rpm2cpio cpio \
    && curl -fsSL -o /alisql.tar.xz \
        "https://github.com/alibaba/AliSQL/releases/download/AliSQL-${ALISQL_VERSION}/alisql-${ALISQL_VERSION}-linux-glibc2.17-x86_64.tar.xz" \
    && echo "${ALISQL_SHA256}  /alisql.tar.xz" | sha256sum -c - \
    && mkdir /alisql \
    && tar -xJf /alisql.tar.xz -C /alisql --strip-components=1 \
    && curl -fsSL -o /openssl.rpm "https://vault.centos.org/7.9.2009/os/x86_64/Packages/${OPENSSL_RPM}" \
    && echo "${OPENSSL_RPM_SHA256}  /openssl.rpm" | sha256sum -c - \
    && mkdir /openssl && cd /openssl && rpm2cpio /openssl.rpm | cpio -idm \
    && cp -a /openssl/usr/lib64/libssl.so.1.0.2k /openssl/usr/lib64/libssl.so.10 \
            /openssl/usr/lib64/libcrypto.so.1.0.2k /openssl/usr/lib64/libcrypto.so.10 /alisql/lib/private/

FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c
# Runtime libraries mysqld needs (see `ldd bin/mysqld`); Ubuntu names libaio's soname libaio.so.1t64.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libaio1t64 libnuma1 libncurses6 zlib1g libgssapi-krb5-2 libkrb5-3 libk5crypto3 libcom-err2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /alisql /opt/alisql
RUN ln -s libaio.so.1t64 /usr/lib/x86_64-linux-gnu/libaio.so.1
ENV PATH=/opt/alisql/bin:$PATH \
    LD_LIBRARY_PATH=/opt/alisql/lib/private
RUN mkdir -p /var/lib/mysql /etc/alisql /var/run/mysqld
COPY entrypoint.sh /entrypoint.sh
VOLUME /var/lib/mysql
EXPOSE 3306
ENTRYPOINT ["/entrypoint.sh"]
