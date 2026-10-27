# VillageSQL Server 0.0.7 (MySQL 8.4 flavour) with the vsql_duckdb extension 0.0.3, built here.
# Stage 1 builds the extension against the 0.0.7 extension SDK (the release asset
# villagesql-extension-sdk-0.0.7.tar.gz, verified by SHA-256) with DuckDB v1.5.5, the extension's
# own default. The build image is Ubuntu 24.04, the same base as the server image, so the .so
# links against the same glibc and libstdc++. Stage 2 is the official server image, pinned by
# digest, with the .veb copied into the server's veb_dir (/usr/lib/veb).
FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS build
ARG SDK_VERSION=0.0.7
ARG SDK_SHA256=3aa9338dfe203f9092e9d73cd4ef70e3fcbac968cbeca19f33d28a631b01e82c
# Commit of tag 0.0.3 of github.com/villagesql/vsql-duckdb.
ARG EXT_TAG=0.0.3
ARG EXT_COMMIT=a3cefdb04dd477b5ab25f5e56203cc5a66b01101
ARG DUCKDB_TAG=v1.5.5
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates curl libssl-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /sdk.tar.gz \
        "https://github.com/villagesql/villagesql-server/releases/download/release/${SDK_VERSION}/villagesql-extension-sdk-${SDK_VERSION}.tar.gz" \
    && echo "${SDK_SHA256}  /sdk.tar.gz" | sha256sum -c - \
    && mkdir /sdk && tar -xzf /sdk.tar.gz -C /sdk --strip-components=1
RUN git clone --quiet https://github.com/villagesql/vsql-duckdb.git /src \
    && cd /src && git checkout --quiet "${EXT_COMMIT}" \
    && test "$(git rev-parse "${EXT_TAG}^{commit}")" = "${EXT_COMMIT}"
WORKDIR /src
RUN cmake -S . -B build -DVillageSQL_SDK_DIR=/sdk -DDUCKDB_TAG="${DUCKDB_TAG}" \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --parallel "$(nproc)" \
    && ls -l build/vsql_duckdb.veb

FROM villagesql/server:mysql-8.4_0.0.7@sha256:a8326d3daeded00b147d47dc50d68a2ac4f8a750e51a7d1374e91aa7a97e88da
COPY --from=build /src/build/vsql_duckdb.veb /usr/lib/veb/vsql_duckdb.veb
# Runs once, when the entrypoint initialises an empty datadir.
COPY install-extension.sql /docker-entrypoint-initdb.d/10-install-extension.sql
